"""Strategy 3: arbitrage inside Polymarket (paper).

Events like "Who will win X?" list several candidates and exactly one wins. So:
  * if every candidate's Yes costs less than $1 in total, buy one Yes of each -> one pays $1
  * if every candidate's No costs less than (candidates - 1) dollars, buy one No of each ->
    all but one pay $1
Profit is locked in once bought (after fees), no guessing. We only paper-trade it here.
"""
import time, uuid
from datetime import datetime, timedelta, timezone
from common import (CFG, GAMMA, DONE, LOCK, get, as_list, notify, money, fnum, fee, best_ask,
                    market_tokens, skip, open_position, parse_json_field)


def A():
    return CFG["arb"]


def fetch_events():
    now = datetime.now(timezone.utc)
    out = []
    for offset in range(0, A()["max_events_scanned"], 100):
        page = as_list(get(GAMMA, "/events", {
            "closed": "false", "active": "true", "limit": 100, "offset": offset,
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": (now + timedelta(days=A()["max_days_to_end"])).strftime("%Y-%m-%dT%H:%M:%SZ")}))
        out += page
        if len(page) < 100:
            break
    return out


def legs_for(ev):
    """Tradable candidate markets, or None if this event isn't a clean one-winner set."""
    if not (ev.get("negRisk") or ev.get("enableNegRisk")) or ev.get("negRiskAugmented"):
        return None
    legs = []
    for m in ev.get("markets") or []:
        yes_tok, no_tok = market_tokens(m)
        if not yes_tok:
            return None
        if m.get("closed"):
            prices = [fnum(p) for p in parse_json_field(m.get("outcomePrices"))]
            if prices and prices[0] and prices[0] >= 0.99:
                return None      # someone already won
            continue             # this candidate already lost - fine to leave out
        if m.get("active") is False or m.get("acceptingOrders") is False:
            return None
        legs.append((m, yes_tok, no_tok))
    return legs if len(legs) >= 2 else None


def check_basket(legs, side):
    """Uses the live order books. Returns (profit_per_set, sets, [(m, token, price)]) or None."""
    picks, sizes = [], []
    for m, yes_tok, no_tok in legs:
        tok = yes_tok if side == "Yes" else no_tok
        p, sz = best_ask(tok, with_size=True)
        if p is None or sz <= 0:
            return None
        picks.append((m, tok, p)); sizes.append(sz)
    cost = sum(p + fee(p, 1) for _, _, p in picks)
    payout = 1.0 if side == "Yes" else len(picks) - 1.0
    profit = payout - cost
    if profit <= 0:
        return None
    sets = min(min(sizes), A()["max_cost_per_arb"] / sum(p for _, _, p in picks))
    return profit, sets, picks


def scan(s):
    events = fetch_events()
    with LOCK:
        held = {p.get("event") for p in s["positions"] if p["strategy"] == "arb" and p["status"] == "open"}
    checked = found = 0
    for ev in events:
        slug = ev.get("slug")
        if slug in held:
            continue
        legs = legs_for(ev)
        if not legs:
            continue
        checked += 1
        # quick screen with the prices Gamma already gives us (no extra requests)
        asks = [fnum(m.get("bestAsk")) for m, _, _ in legs]
        bids = [fnum(m.get("bestBid")) for m, _, _ in legs]
        tries = []
        if all(a for a in asks) and 1 - sum(asks) >= A()["min_profit_per_set"]:
            tries.append("Yes")
        if all(b for b in bids) and sum(bids) - 1 >= A()["min_profit_per_set"]:
            tries.append("No")
        for side in tries:
            res = check_basket(legs, side)
            if not res:
                with LOCK: skip(s, "gap gone on order book", "arb")
                continue
            profit, sets, picks = res
            if profit < A()["min_profit_per_set"] or profit * sets < A()["min_dollars"]:
                with LOCK: skip(s, "too small after fees", "arb")
                continue
            group = uuid.uuid4().hex[:8]
            with LOCK:
                for m, tok, p in picks:
                    open_position(s, strategy="arb", title=ev.get("title", "?"),
                                  outcome=f"{side}: {m.get('groupItemTitle') or m.get('question')}",
                                  condition_id=m.get("conditionId"), asset=tok, event=slug,
                                  group=group, entry_price=p, stake=p * sets, shares=sets,
                                  category=side, locked_profit=round(profit * sets, 2))
            found += 1
            notify("Arb found",
                   f"{ev.get('title')}\nBuy {side} on all {len(picks)} options, {sets:.0f} sets\n"
                   f"Locked profit after fees: {money(profit * sets)} ({profit*100:.1f}c per set)",
                   "moneybag")
            break
    with LOCK:
        s["scans"]["arb"] = {"at": time.time(), "events": len(events), "checked": checked, "found": found}


def announce_settled(s):
    groups = {}
    for p in s["positions"]:
        if p["strategy"] == "arb":
            groups.setdefault(p["group"], []).append(p)
    for g, legs in groups.items():
        if legs[0].get("announced") or any(p["status"] not in DONE for p in legs):
            continue
        pnl = sum(p["pnl"] for p in legs)
        for p in legs:
            p["announced"] = True
        notify(f"Arb paid out {money(pnl)}", f"{legs[0]['title']}", "moneybag")
