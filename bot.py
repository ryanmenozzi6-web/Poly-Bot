"""
Polymarket copy-trade PAPER bot.
Watches top Polymarket traders, "copies" their new buys with fake money,
and tracks whether copying them would actually make money.
No real money, no API keys, no wallet. Read-only public data.
"""
import json, os, sys, time, shutil, threading, html, urllib.request, urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

DATA = "https://data-api.polymarket.com"
DONE = ("won", "lost", "sold")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
TZ = ZoneInfo("America/New_York")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
REPORT_FILE = os.path.join(DATA_DIR, "REPORT.md")
os.makedirs(DATA_DIR, exist_ok=True)

with open("config.json") as f:
    CFG = json.load(f)


# ---------- helpers ----------
def get(base, path, params=None, tries=3):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "paper-bot"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                print(f"  ! request failed: {url} -> {e}")
                return None
            time.sleep(2 * (i + 1))


def notify(title, body, tags=""):
    print(f"[notify] {title} | {body}")
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
            headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": tags},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  ! notify failed: {e}")


def as_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("data", "results", "items", "leaderboard", "markets"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


def parse_json_field(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return v or []


def fee(price, shares):
    return CFG["taker_fee_rate"] * price * (1 - price) * shares


def clean(t):
    return str(t).replace("|", "/")


def money(x):
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


# ---------- market data ----------
_market_cache = {}

def market_info(condition_id, asset, outcome_name=None):
    """Returns (current_price, closed) for our outcome, or (None, None)."""
    key = condition_id
    if key not in _market_cache:
        m = as_list(get(GAMMA, "/markets", {"condition_ids": condition_id}))
        if not m:
            m = as_list(get(GAMMA, "/markets", {"condition_ids": condition_id, "closed": "true"}))
        _market_cache[key] = m[0] if m else None
    m = _market_cache[key]
    if not m:
        return None, None
    tokens = [str(t) for t in parse_json_field(m.get("clobTokenIds"))]
    prices = parse_json_field(m.get("outcomePrices"))
    outcomes = parse_json_field(m.get("outcomes"))
    idx = None
    if str(asset) in tokens:
        idx = tokens.index(str(asset))
    elif outcome_name and outcome_name in outcomes:
        idx = outcomes.index(outcome_name)
    if idx is None or idx >= len(prices):
        return None, bool(m.get("closed"))
    try:
        return float(prices[idx]), bool(m.get("closed"))
    except Exception:
        return None, bool(m.get("closed"))


def best_ask(asset):
    """Price we'd actually pay right now (lowest ask in the order book)."""
    book = get(CLOB, "/book", {"token_id": asset}, tries=2)
    if isinstance(book, dict) and book.get("asks"):
        try:
            return min(float(a["price"]) for a in book["asks"])
        except Exception:
            pass
    return None


def best_bid(asset):
    """Price we'd actually get selling right now (highest bid)."""
    book = get(CLOB, "/book", {"token_id": asset}, tries=2)
    if isinstance(book, dict) and book.get("bids"):
        try:
            return max(float(b["price"]) for b in book["bids"])
        except Exception:
            pass
    return None


# ---------- state ----------
def load_state():
    if not os.path.exists(STATE_FILE) and STATE_FILE != "state.json" and os.path.exists("state.json"):
        os.makedirs(DATA_DIR, exist_ok=True)
        shutil.copy("state.json", STATE_FILE)  # carry over data from the GitHub version
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"traders": [], "traders_refreshed": 0, "last_seen": {}, "positions": [],
            "skipped": {}, "last_summary_date": ""}


def save_state(s):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE_FILE)


def skip(s, reason):
    s["skipped"][reason] = s["skipped"].get(reason, 0) + 1


# ---------- steps ----------
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
    traders = []
    for r in rows:
        w = r.get("proxyWallet") or r.get("user") or r.get("wallet")
        try:
            pnl, vol = float(r.get("pnl") or 0), float(r.get("vol") or 0)
        except Exception:
            continue
        if not w or vol <= 0:
            continue
        # skip whales/market makers: huge volume, tiny return on it
        if vol > CFG["max_trader_volume"] or pnl < CFG["min_trader_profit"]:
            continue
        if pnl / vol < CFG["min_trader_roi"]:
            continue
        traders.append({"wallet": w.lower(), "name": r.get("userName") or r.get("name") or w[:8],
                        "pnl": pnl, "vol": vol})
        if len(traders) >= CFG["top_n_traders"]:
            break
    for w in CFG.get("extra_wallets", []):
        if w.lower() not in [t["wallet"] for t in traders]:
            traders.append({"wallet": w.lower(), "name": w[:8], "pnl": None, "vol": None})
    if traders:
        s["traders"] = traders
        s["traders_refreshed"] = time.time()
        print(f"Following {len(traders)} traders (scanned {len(rows)}): " +
              ", ".join(f"{t['name']} (${t['pnl'] or 0:,.0f} on ${t['vol'] or 0:,.0f})" for t in traders))
    else:
        print("! No traders passed the filters (or leaderboard failed); keeping old list.")


def check_new_trades(s):
    now = time.time()
    open_keys = {(p["wallet"], p["asset"]) for p in s["positions"] if p["status"] == "open"}
    for t in s["traders"]:
        w = t["wallet"]
        if w not in s["last_seen"]:
            s["last_seen"][w] = now  # first time: don't copy old trades
            continue
        trades = as_list(get(DATA, "/trades", {"user": w, "limit": 100, "takerOnly": "false"}))
        new = [x for x in trades if float(x.get("timestamp", 0)) > s["last_seen"][w]]
        if not new:
            continue
        s["last_seen"][w] = max(float(x["timestamp"]) for x in new)

        # they sold? follow them out
        if CFG["sell_when_they_sell"]:
            sells = {}
            for x in new:
                if x.get("side") == "SELL":
                    a = str(x.get("asset"))
                    sells[a] = sells.get(a, 0.0) + float(x.get("size", 0))
            for p in s["positions"]:
                if p["status"] == "open" and p["wallet"] == w and p["asset"] in sells:
                    p["their_sold"] = p.get("their_sold", 0) + sells[p["asset"]]
                    if p["their_sold"] >= CFG["exit_when_they_sold_pct"] / 100 * p.get("their_shares", 0):
                        close_early(s, p)
                        open_keys.discard((w, p["asset"]))

        # group fills of the same buy together
        buys = {}
        for x in new:
            if x.get("side") != "BUY":
                continue
            a = str(x.get("asset"))
            g = buys.setdefault(a, {"size": 0.0, "cost": 0.0, "ts": 0, "x": x})
            sz, px = float(x.get("size", 0)), float(x.get("price", 0))
            g["size"] += sz
            g["cost"] += sz * px
            g["ts"] = max(g["ts"], float(x["timestamp"]))

        for asset, g in buys.items():
            x = g["x"]
            their_usd = g["cost"]
            their_px = g["cost"] / g["size"] if g["size"] else 0
            title = x.get("title", "?")
            if their_usd < CFG["min_their_trade_usd"]:
                skip(s, "their bet too small"); continue
            if (w, asset) in open_keys:
                skip(s, "already copied"); continue
            if now - g["ts"] > CFG["max_trade_age_minutes"] * 60:
                skip(s, "saw it too late"); continue

            entry = best_ask(asset)
            if entry is None:
                entry, closed = market_info(x.get("conditionId"), asset, x.get("outcome"))
                if closed:
                    skip(s, "market closed"); continue
            if entry is None:
                skip(s, "no price"); continue
            if entry > CFG["skip_price_above"] or entry < CFG["skip_price_below"]:
                skip(s, "price too extreme"); continue
            if entry - their_px > CFG["max_slippage_cents"] / 100:
                skip(s, "price already moved"); continue

            stake = CFG["stake_per_trade"]
            shares = stake / entry
            pos = {
                "wallet": w, "trader": t["name"], "title": title, "outcome": x.get("outcome"),
                "condition_id": x.get("conditionId"), "asset": asset, "slug": x.get("slug"),
                "their_price": round(their_px, 4), "their_usd": round(their_usd, 2),
                "their_shares": g["size"], "their_sold": 0.0,
                "entry_price": entry, "shares": shares, "stake": stake,
                "fee": fee(entry, shares), "opened": time.time(), "status": "open",
                "current_price": entry, "pnl": None,
            }
            s["positions"].append(pos)
            open_keys.add((w, asset))
            notify(f"Copy: {t['name']}",
                   f"{title}\n{x.get('outcome')} @ {entry*100:.0f}c (they paid {their_px*100:.0f}c, bet {money(their_usd)})\nPaper stake {money(stake)}",
                   "eyes")


def close_early(s, p):
    px = best_bid(p["asset"])
    if px is None:
        px, _ = market_info(p["condition_id"], p["asset"], p.get("outcome"))
    if px is None:
        return
    proceeds = p["shares"] * px - fee(px, p["shares"])
    p["pnl"] = proceeds - p["stake"] - p["fee"]
    p["status"] = "sold"
    p["exit_price"] = px
    p["current_price"] = px
    p["closed"] = time.time()
    tot = sum(q["pnl"] for q in s["positions"] if q["status"] in DONE)
    notify(f"Sold (followed {p['trader']}) {money(p['pnl'])}",
           f"{p['title']} ({p['outcome']})\nIn {p['entry_price']*100:.0f}c -> out {px*100:.0f}c\nRealized total: {money(tot)}",
           "outbox_tray")


def update_positions(s):
    for p in s["positions"]:
        if p["status"] != "open":
            continue
        price, closed = market_info(p["condition_id"], p["asset"], p.get("outcome"))
        if price is None:
            continue
        p["current_price"] = price
        if closed and (price >= 0.99 or price <= 0.01):
            payout = p["shares"] * (1.0 if price >= 0.99 else 0.0)
            p["pnl"] = payout - p["stake"] - p["fee"]
            p["status"] = "won" if price >= 0.99 else "lost"
            p["closed"] = time.time()
            tot = sum(q["pnl"] for q in s["positions"] if q["status"] in DONE)
            notify(f"{'WIN' if p['status']=='won' else 'LOSS'} {money(p['pnl'])}",
                   f"{p['title']} ({p['outcome']}) - copied {p['trader']}\nRealized total: {money(tot)}",
                   "white_check_mark" if p["status"] == "won" else "x")


def stats(s):
    closed = [p for p in s["positions"] if p["status"] in DONE]
    opn = [p for p in s["positions"] if p["status"] == "open"]
    realized = sum(p["pnl"] for p in closed)
    unreal = sum(p["shares"] * p["current_price"] - p["stake"] - p["fee"] for p in opn)
    wins = sum(1 for p in closed if p["pnl"] > 0)
    return closed, opn, realized, unreal, wins


def write_report(s):
    closed, opn, realized, unreal, wins = stats(s)
    wr = f"{wins/len(closed)*100:.0f}%" if closed else "-"
    slip = [p["entry_price"] - p["their_price"] for p in s["positions"]]
    avg_slip = f"{sum(slip)/len(slip)*100:+.1f}c" if slip else "-"
    L = [f"# Paper bot report", f"_Updated {datetime.now(TZ):%b %d, %I:%M %p} ET_", "",
         "| | |", "|---|---|",
         f"| Realized P&L | **{money(realized)}** |",
         f"| Open positions (marked now) | {money(unreal)} |",
         f"| Closed bets | {len(closed)} ({wr} profitable) |",
         f"| Open bets | {len(opn)} |",
         f"| Avg price paid vs them | {avg_slip} |", "",
         "## By trader", "| Trader | Closed | Profitable | P&L |", "|---|---|---|---|"]
    by = {}
    for p in closed:
        b = by.setdefault(p["trader"], [0, 0, 0.0])
        b[0] += 1; b[1] += p["pnl"] > 0; b[2] += p["pnl"]
    for name, (n, w, pnl) in sorted(by.items(), key=lambda kv: -kv[1][2]):
        L.append(f"| {name} | {n} | {w} | {money(pnl)} |")
    L += ["", "## Skipped (why we didn't copy)", "| Reason | Count |", "|---|---|"]
    for r, c in sorted(s["skipped"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {r} | {c} |")
    L += ["", "## Open", "| Trader | Market | Pick | Paid | Now |", "|---|---|---|---|---|"]
    for p in sorted(opn, key=lambda p: -p["opened"])[:30]:
        L.append(f"| {p['trader']} | {clean(p['title'])} | {p['outcome']} | {p['entry_price']*100:.0f}c | {p['current_price']*100:.0f}c |")
    L += ["", "## Recently closed", "| Trader | Market | Pick | How | Result |", "|---|---|---|---|---|"]
    for p in sorted(closed, key=lambda p: -p.get("closed", 0))[:30]:
        L.append(f"| {p['trader']} | {clean(p['title'])} | {p['outcome']} | {p['status']} | {money(p['pnl'])} |")
    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(L) + "\n")


def daily_summary(s):
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour < CFG["daily_summary_hour_et"] or s["last_summary_date"] == today:
        return
    s["last_summary_date"] = today
    closed, opn, realized, unreal, wins = stats(s)
    notify("Daily paper summary",
           f"Realized: {money(realized)}\nOpen (marked): {money(unreal)}\nClosed: {len(closed)}, profitable {wins}\nOpen bets: {len(opn)}",
           "bar_chart")


# ---------- web page for the report (server mode) ----------
def md_to_html(md):
    out, in_table = [], False
    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-") for c in cells):
                continue
            if not in_table:
                out.append("<table>"); in_table = True
            out.append("<tr>" + "".join(f"<td>{html.escape(c).replace('**','')}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>"); in_table = False
        if line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.strip():
            out.append(f"<p>{html.escape(line.strip('_'))}</p>")
    if in_table:
        out.append("</table>")
    return ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta http-equiv=refresh content=60><title>Paper bot</title><style>"
            "body{font-family:system-ui,sans-serif;background:#111;color:#eee;padding:16px;max-width:900px;margin:auto}"
            "table{border-collapse:collapse;width:100%;margin-bottom:12px;font-size:14px}"
            "td{border:1px solid #333;padding:6px}tr:first-child td{font-weight:600;background:#1c1c1c}"
            "h2{margin-top:28px}</style>" + "\n".join(out))


class ReportHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with open(REPORT_FILE) as f:
                body = md_to_html(f.read())
        except Exception:
            body = "<p>No report yet - check back in a minute.</p>"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def start_web():
    port = int(os.environ.get("PORT", "8080"))
    srv = HTTPServer(("0.0.0.0", port), ReportHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Report page running on port {port}")


# ---------- run ----------
def main():
    s = load_state()
    refresh_traders(s)
    check_new_trades(s)
    update_positions(s)
    daily_summary(s)
    write_report(s)
    save_state(s)
    print("Done.")


def loop():
    """Always-on mode: check trades every few seconds, forever."""
    every = CFG.get("check_every_seconds", 20)
    pos_every = CFG.get("update_positions_every_seconds", 120)
    start_web()
    s = load_state()
    last_pos = 0
    print(f"Always-on mode: checking every {every}s")
    while True:
        started = time.time()
        try:
            refresh_traders(s)
            check_new_trades(s)
            if time.time() - last_pos > pos_every:
                _market_cache.clear()
                update_positions(s)
                last_pos = time.time()
            daily_summary(s)
            write_report(s)
            save_state(s)
        except Exception as e:
            print(f"! loop error (will keep going): {e}")
        time.sleep(max(1, every - (time.time() - started)))


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop()
    else:
        main()
