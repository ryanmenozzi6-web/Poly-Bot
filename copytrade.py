"""Strategy 1: copy top Polymarket traders (paper)."""
import re, time
from common import (CFG, DATA, DONE, get, as_list, notify, money, get_market, best_ask, best_bid,
                    market_info, fee, parse_time, fnum, skip, open_position, realized)


def refresh_traders(s):
    cfg_key = f'{CFG["top_n_traders"]}-{CFG["leaderboard_scan"]}-{CFG["max_trader_volume"]}-{CFG["min_trader_profit"]}-{CFG["min_trader_roi"]}'
    if time.time() - s["traders_refreshed"] < 24 * 3600 and s["traders"] and s.get("traders_cfg") == cfg_key:
        return
    s["traders_cfg"] = cfg_key
    rows = []
    for offset in range(0, CFG["leaderboard_scan"], 50):
        page = as_list(get(DATA, "/v1/leaderboard", {
            "category": CFG["leaderboard_category"], "timePeriod": CFG["leaderboard_period"],
            "orderBy": "PNL", "limit": 50, "offset": offset,
        }))
        rows += page
        if len(page) < 50:
            break
    blocked = {n.lower() for n in CFG.get("blocked_traders", [])}
    traders = []
    for r in rows:
        w = r.get("proxyWallet") or r.get("user") or r.get("wallet")
        pnl, vol = fnum(r.get("pnl"), 0), fnum(r.get("vol"), 0)
        if not w or vol <= 0:
            continue
        name = r.get("userName") or r.get("name") or w[:8]
        if name.lower() in blocked or w.lower() in blocked:
            continue
        # skip whales/market makers: huge volume, tiny return on it
        if vol > CFG["max_trader_volume"] or pnl < CFG["min_trader_profit"]:
            continue
        if pnl / vol < CFG["min_trader_roi"]:
            continue
        traders.append({"wallet": w.lower(), "name": name, "pnl": pnl, "vol": vol})
        if len(traders) >= CFG["top_n_traders"]:
            break
    for w in CFG.get("extra_wallets", []):
        if w.lower() not in [t["wallet"] for t in traders]:
            traders.append({"wallet": w.lower(), "name": w[:8], "pnl": None, "vol": None})
    if traders:
        s["traders"] = traders
        s["traders_refreshed"] = time.time()
        print(f"Following {len(traders)} traders (scanned {len(rows)})")
    else:
        print("! No traders passed the filters (or leaderboard failed); keeping old list.")


def event_key(x, m):
    if x.get("eventSlug"):
        return x["eventSlug"]
    evs = (m or {}).get("events") or []
    if evs and evs[0].get("slug"):
        return evs[0]["slug"]
    return x.get("conditionId")


def family(title):
    """Same bet at a different line -> same family. 'Crew vs Miami: O/U 3.5' ~ 'Crew vs Miami: O/U 4.5'."""
    return re.sub(r"[-+]?\d+(\.\d+)?", "#", str(title).lower()).strip()


def game_level(title):
    """Moneyline / spread / game totals, as opposed to a single player's prop."""
    t = str(title).lower()
    return any(k in t for k in (" vs", "spread", "handicap", "games total", " win on ", "end in a draw"))


def event_conflict(s, ev, title):
    """Reason to skip a second bet in the same game, or None if it's fine."""
    held = [p for p in s["positions"] if p["status"] == "open" and p.get("event") == ev]
    if not held:
        return None
    if len(held) >= CFG["max_bets_per_event"]:
        return "already max bets in this game"
    if any(family(p["title"]) == family(title) for p in held):
        return "same bet at another line"
    if game_level(title) and any(game_level(p["title"]) for p in held):
        return "already have a game bet here"
    return None


def is_benched(s, wallet):
    n, pnl = 0, 0.0
    for p in s["positions"]:
        if p["strategy"] == "copy" and p["wallet"] == wallet and p["status"] in DONE:
            n += 1; pnl += p["pnl"]
    return n >= CFG["bench_after_bets"] and pnl < 0


def pick_stake(s, wallet, their_usd):
    """Bigger copy when they bet bigger than they usually do."""
    base = CFG["stake_per_trade"]
    hist = s["their_sizes"].get(wallet, [])
    if len(hist) < 5:
        return base
    med = sorted(hist)[len(hist) // 2]
    ratio = their_usd / med if med else 1
    return round(min(CFG["max_stake"], max(CFG["min_stake"], base * ratio)), 2)


def check_new_trades(s):
    now = time.time()
    open_copy = [p for p in s["positions"] if p["strategy"] == "copy" and p["status"] == "open"]
    open_keys = {(p["wallet"], p["asset"]) for p in open_copy}
    for t in list(s["traders"]):
        w = t["wallet"]
        if w not in s["last_seen"]:
            s["last_seen"][w] = now  # first time: don't copy old trades
            continue
        trades = as_list(get(DATA, "/trades", {"user": w, "limit": 100, "takerOnly": "false"}))
        new = [x for x in trades if fnum(x.get("timestamp"), 0) > s["last_seen"][w]]
        if not new:
            continue
        s["last_seen"][w] = max(float(x["timestamp"]) for x in new)

        # they sold? follow them out
        if CFG["sell_when_they_sell"]:
            sells, sell_cost = {}, {}
            for x in new:
                if x.get("side") == "SELL":
                    a = str(x.get("asset"))
                    sz = fnum(x.get("size"), 0)
                    sells[a] = sells.get(a, 0.0) + sz
                    sell_cost[a] = sell_cost.get(a, 0.0) + sz * fnum(x.get("price"), 0)
            for p in open_copy:
                if p["status"] == "open" and p["wallet"] == w and p["asset"] in sells:
                    p["their_sold"] = p.get("their_sold", 0) + sells[p["asset"]]
                    if p["their_sold"] >= CFG["exit_when_they_sold_pct"] / 100 * p.get("their_shares", 0):
                        their_exit = sell_cost[p["asset"]] / sells[p["asset"]] if sells[p["asset"]] else None
                        close_early(s, p, their_exit)
                        open_keys.discard((w, p["asset"]))

        # group fills of the same buy together
        buys = {}
        for x in new:
            if x.get("side") != "BUY":
                continue
            a = str(x.get("asset"))
            g = buys.setdefault(a, {"size": 0.0, "cost": 0.0, "ts": 0, "x": x})
            sz, px = fnum(x.get("size"), 0), fnum(x.get("price"), 0)
            g["size"] += sz
            g["cost"] += sz * px
            g["ts"] = max(g["ts"], fnum(x.get("timestamp"), 0))

        for asset, g in buys.items():
            x = g["x"]
            their_usd = g["cost"]
            their_px = g["cost"] / g["size"] if g["size"] else 0
            title = x.get("title", "?")

            # remember how big this trader usually bets (for sizing)
            hist = s["their_sizes"].setdefault(w, [])
            hist.append(round(their_usd, 2))
            del hist[:-50]

            if their_usd < CFG["min_their_trade_usd"]:
                skip(s, "their bet too small"); continue
            if (w, asset) in open_keys:
                skip(s, "already copied"); continue
            if now - g["ts"] > CFG["max_trade_age_minutes"] * 60:
                skip(s, "saw it too late"); continue
            if is_benched(s, w):
                skip(s, "trader benched (losing for us)"); continue

            m = get_market(x.get("conditionId"))
            ev = event_key(x, m)
            why = event_conflict(s, ev, title)
            if why:
                skip(s, why); continue
            if m:
                if m.get("closed"):
                    skip(s, "market closed"); continue
                start = parse_time(m.get("gameStartTime"))
                if CFG["skip_live_games"] and start and now >= start:
                    skip(s, "game already started"); continue
                end = parse_time(m.get("endDate"))
                if end and end - now > CFG["max_days_to_end"] * 86400:
                    skip(s, "ends too far away"); continue

            entry = best_ask(asset)
            if entry is None:
                entry, closed = market_info(x.get("conditionId"), asset, x.get("outcome"))
                if closed:
                    skip(s, "market closed"); continue
            if entry is None:
                skip(s, "no price"); continue
            if entry < their_px:
                entry = their_px   # can't realistically beat their price after a delay
            if entry > CFG["skip_price_above"] or entry < CFG["skip_price_below"]:
                skip(s, "price too extreme"); continue
            if entry - their_px > CFG["max_slippage_cents"] / 100:
                skip(s, "price already moved"); continue

            stake = pick_stake(s, w, their_usd)
            sports = bool(m and (m.get("gameStartTime") or m.get("sportsMarketType")))
            open_position(
                s, strategy="copy", wallet=w, trader=t["name"], title=title, outcome=x.get("outcome"),
                condition_id=x.get("conditionId"), asset=asset, slug=x.get("slug"), event=ev,
                category="sports" if sports else "other",
                their_price=round(their_px, 4), their_usd=round(their_usd, 2),
                their_shares=g["size"], their_sold=0.0, entry_price=entry, stake=stake)
            open_keys.add((w, asset))
            notify(f"Copy: {t['name']}",
                   f"{title}\n{x.get('outcome')} @ {entry*100:.0f}c (they paid {their_px*100:.0f}c, bet {money(their_usd)})\nPaper stake {money(stake)}",
                   "eyes")


def close_early(s, p, their_exit=None):
    px = best_bid(p["asset"])
    if px is None:
        px, _ = market_info(p["condition_id"], p["asset"], p.get("outcome"))
    if px is None:
        px = their_exit
    if px is None:
        return
    if their_exit is not None and px > their_exit:
        px = their_exit  # can't sell better than they did after a delay
    proceeds = p["shares"] * px - fee(px, p["shares"])
    p["pnl"] = proceeds - p["stake"] - p["fee"]
    p["status"] = "sold"
    p["exit_price"] = px
    p["current_price"] = px
    p["closed"] = time.time()
    notify(f"Sold (followed {p['trader']}) {money(p['pnl'])}",
           f"{p['title']} ({p['outcome']})\nIn {p['entry_price']*100:.0f}c -> out {px*100:.0f}c\nCopy total: {money(realized(s, 'copy'))}",
           "outbox_tray")
