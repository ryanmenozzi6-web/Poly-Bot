"""
Polymarket PAPER bot - three strategies side by side, fake money only:
  1. copy   - copy top traders' buys (with filters so we don't copy junk)
  2. weather - daily temperature markets vs free ensemble forecasts
  3. arb    - one-winner events where buying every option locks in a profit
No real money, no API keys, no wallet. Read-only public data.
Run:  python bot.py --loop   (always-on, serves the report page)
"""
import sys, time, threading, html
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

from common import (CFG, TZ, DONE, LOCK, REPORT_FILE, load_state, save_state, update_positions,
                    clear_market_cache, notify, money, clean)
import copytrade, weather, arb

STRATS = [("copy", "Copy trading"), ("weather", "Weather"), ("arb", "Arb scanner")]


# ---------- numbers ----------
def summarize(ps):
    closed = [p for p in ps if p["status"] in DONE]
    opn = [p for p in ps if p["status"] == "open"]
    return {
        "closed": closed, "open": opn,
        "realized": sum(p["pnl"] for p in closed),
        "unreal": sum(p["shares"] * p["current_price"] - p["stake"] - p["fee"] for p in opn),
        "wins": sum(1 for p in closed if p["pnl"] > 0),
        "staked": sum(p["stake"] for p in closed),
    }


def pct(a, b):
    return f"{a/b*100:.0f}%" if b else "-"


def ago(ts):
    if not ts:
        return "never"
    m = int((time.time() - ts) / 60)
    return f"{m} min ago" if m < 120 else f"{m//60} h ago"


def breakdown(L, title, closed, keyfn):
    rows = {}
    for p in closed:
        k = keyfn(p)
        r = rows.setdefault(k, [0, 0, 0.0, 0.0])
        r[0] += 1; r[1] += p["pnl"] > 0; r[2] += p["pnl"]; r[3] += p["stake"]
    if not rows:
        return
    L += ["", f"### {title}", "| | Closed | Profitable | P&L | Return |", "|---|---|---|---|---|"]
    for k, (n, w, pnl, st) in sorted(rows.items(), key=lambda kv: -kv[1][2]):
        L.append(f"| {k} | {n} | {w} | {money(pnl)} | {pct(pnl, st)} |")


def price_band(p):
    e = p["entry_price"]
    return "under 30c" if e < 0.30 else "30-60c" if e < 0.60 else "60c+"


def write_report(s):
    by = {k: summarize([p for p in s["positions"] if p["strategy"] == k]) for k, _ in STRATS}
    L = ["# Paper bot report", f"_Updated {datetime.now(TZ):%b %d, %I:%M %p} ET_", "",
         "| Strategy | Realized | Open (marked) | Closed | Profitable | Return | Open bets |",
         "|---|---|---|---|---|---|---|"]
    for k, name in STRATS:
        b = by[k]
        n_closed, n_wins, n_open = len(b["closed"]), b["wins"], len(b["open"])
        if k == "arb":   # count whole baskets, not individual legs
            groups = {}
            for p in s["positions"]:
                if p["strategy"] == "arb":
                    groups.setdefault(p["group"], []).append(p)
            done = [g for g in groups.values() if all(p["status"] in DONE for p in g)]
            n_closed, n_open = len(done), len(groups) - len(done)
            n_wins = sum(1 for g in done if sum(p["pnl"] for p in g) > 0)
        L.append(f"| {name} | **{money(b['realized'])}** | {money(b['unreal'])} | {n_closed} | "
                 f"{pct(n_wins, n_closed)} | {pct(b['realized'], b['staked'])} | {n_open} |")
    L.append("")
    L.append("_Return = realized P&L / money put into closed bets, after fees._")

    # ----- copy -----
    b = by["copy"]
    cps = [p for p in s["positions"] if p["strategy"] == "copy"]
    slip = [p["entry_price"] - p["their_price"] for p in cps]
    L += ["", "## Copy trading",
          f"Following {len(s['traders'])} traders. Avg price paid vs them: "
          f"{(sum(slip)/len(slip)*100 if slip else 0):+.1f}c. Stakes ${CFG['min_stake']}-{CFG['max_stake']}."]
    breakdown(L, "By type", b["closed"], lambda p: p.get("category", "other"))
    breakdown(L, "By price paid", b["closed"], price_band)
    breakdown(L, "By trader", b["closed"], lambda p: p["trader"])
    L += ["", "### Skipped (why we didn't copy)", "| Reason | Count |", "|---|---|"]
    for r, c in sorted(s["skipped"].items(), key=lambda kv: -kv[1]):
        if ":" not in r:
            L.append(f"| {r} | {c} |")
    L += ["", "### Open", "| Trader | Market | Pick | Stake | Paid | Now |", "|---|---|---|---|---|---|"]
    for p in sorted(b["open"], key=lambda p: -p["opened"])[:25]:
        L.append(f"| {p['trader']} | {clean(p['title'])} | {p['outcome']} | {money(p['stake'])} | "
                 f"{p['entry_price']*100:.0f}c | {p['current_price']*100:.0f}c |")
    L += ["", "### Recently closed", "| Trader | Market | Pick | How | Result |", "|---|---|---|---|---|"]
    for p in sorted(b["closed"], key=lambda p: -p.get("closed", 0))[:25]:
        L.append(f"| {p['trader']} | {clean(p['title'])} | {p['outcome']} | {p['status']} | {money(p['pnl'])} |")

    # ----- weather -----
    b = by["weather"]
    sc = s["scans"].get("weather", {})
    L += ["", "## Weather",
          f"Last scan {ago(sc.get('at'))}: {sc.get('events', 0)} temperature events, {sc.get('bets', 0)} new bets."]
    if b["closed"]:
        exp = sum(p.get("model_prob", 0) for p in b["closed"]) / len(b["closed"])
        L.append(f"Forecast said we'd win {exp*100:.0f}% of these on average; we actually won "
                 f"{pct(b['wins'], len(b['closed']))}. If those two are close, the forecast is honest.")
    wskips = [(r.split(': ', 1)[1], c) for r, c in s["skipped"].items() if r.startswith("weather:")]
    if wskips:
        L += ["", "| Skipped | Count |", "|---|---|"] + [f"| {r} | {c} |" for r, c in sorted(wskips, key=lambda x: -x[1])]
    L += ["", "| Market | Pick | Forecast | Model | Paid | Now / Result |", "|---|---|---|---|---|---|"]
    for p in sorted(b["open"] + b["closed"], key=lambda p: -p["opened"])[:30]:
        res = f"{p['current_price']*100:.0f}c" if p["status"] == "open" else f"{p['status']} {money(p['pnl'])}"
        L.append(f"| {clean(p['title'])} | {clean(p['outcome'])} | {p.get('forecast','')} | "
                 f"{p.get('model_prob', 0)*100:.0f}% | {p['entry_price']*100:.0f}c | {res} |")

    # ----- arb -----
    sc = s["scans"].get("arb", {})
    L += ["", "## Arb scanner",
          f"Last scan {ago(sc.get('at'))}: {sc.get('events', 0)} events, {sc.get('checked', 0)} one-winner sets checked, "
          f"{sc.get('found', 0)} arbs found."]
    askips = [(r.split(': ', 1)[1], c) for r, c in s["skipped"].items() if r.startswith("arb:")]
    if askips:
        L += ["", "| Near misses | Count |", "|---|---|"] + [f"| {r} | {c} |" for r, c in sorted(askips, key=lambda x: -x[1])]
    groups = {}
    for p in s["positions"]:
        if p["strategy"] == "arb":
            groups.setdefault(p["group"], []).append(p)
    L += ["", "| Event | Side | Options | Cost | Locked profit | Status |", "|---|---|---|---|---|---|"]
    for g, legs in sorted(groups.items(), key=lambda kv: -kv[1][0]["opened"])[:30]:
        done = all(p["status"] in DONE for p in legs)
        status = f"paid {money(sum(p['pnl'] for p in legs))}" if done else "waiting"
        L.append(f"| {clean(legs[0]['title'])} | {legs[0]['category']} | {len(legs)} | "
                 f"{money(sum(p['stake'] + p['fee'] for p in legs))} | {money(legs[0].get('locked_profit', 0))} | {status} |")

    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(L) + "\n")


def daily_summary(s):
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour < CFG["daily_summary_hour_et"] or s["last_summary_date"] == today:
        return
    s["last_summary_date"] = today
    lines = []
    for k, name in STRATS:
        b = summarize([p for p in s["positions"] if p["strategy"] == k])
        lines.append(f"{name}: {money(b['realized'])} ({len(b['closed'])} closed, {len(b['open'])} open)")
    notify("Daily paper summary", "\n".join(lines), "bar_chart")


# ---------- web page ----------
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
        if line.startswith("### "):
            out.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.strip():
            out.append(f"<p>{html.escape(line.strip('_'))}</p>")
    if in_table:
        out.append("</table>")
    return ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta http-equiv=refresh content=60><title>Paper bot</title><style>"
            "body{font-family:system-ui,sans-serif;background:#111;color:#eee;padding:16px;max-width:960px;margin:auto}"
            "table{border-collapse:collapse;width:100%;margin-bottom:12px;font-size:14px;display:block;overflow-x:auto}"
            "td{border:1px solid #333;padding:6px}tr:first-child td{font-weight:600;background:#1c1c1c}"
            "h2{margin-top:36px;border-top:1px solid #333;padding-top:16px}h3{margin-top:22px;color:#bbb}"
            "p{color:#ccc}</style>" + "\n".join(out))


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
def maybe_reset(s):
    rid = CFG.get("reset_id", 0)
    if s.get("reset_id", 0) != rid:
        s["positions"], s["skipped"], s["scans"], s["reset_id"] = [], {}, {}, rid
        print("Stats reset (reset_id changed).")


def scanners(s):
    """Background thread: weather + arb scans, so they never slow down copying."""
    last = {"weather": 0, "arb": 0}
    while True:
        for name, mod, every in (("weather", weather, CFG["weather"]["scan_every_minutes"]),
                                 ("arb", arb, CFG["arb"]["scan_every_minutes"])):
            if not CFG[name]["enabled"] or time.time() - last[name] < every * 60:
                continue
            last[name] = time.time()
            try:
                mod.scan(s)
            except Exception as e:
                print(f"! {name} scan error (will keep going): {e}")
        time.sleep(15)


def tick(s, do_positions):
    copytrade.refresh_traders(s)
    copytrade.check_new_trades(s)
    if do_positions:
        clear_market_cache()
        update_positions(s)
        arb.announce_settled(s)
    daily_summary(s)
    write_report(s)
    save_state(s)


def main():
    s = load_state()
    maybe_reset(s)
    with LOCK:
        tick(s, True)
    for name, mod in (("weather", weather), ("arb", arb)):
        if CFG[name]["enabled"]:
            mod.scan(s)
    with LOCK:
        write_report(s)
        save_state(s)
    print("Done.")


def loop():
    every = CFG.get("check_every_seconds", 20)
    pos_every = CFG.get("update_positions_every_seconds", 120)
    start_web()
    s = load_state()
    maybe_reset(s)
    threading.Thread(target=scanners, args=(s,), daemon=True).start()
    last_pos = 0
    print(f"Always-on mode: checking every {every}s")
    while True:
        started = time.time()
        try:
            with LOCK:
                do_pos = time.time() - last_pos > pos_every
                tick(s, do_pos)
                if do_pos:
                    last_pos = time.time()
        except Exception as e:
            print(f"! loop error (will keep going): {e}")
        time.sleep(max(1, every - (time.time() - started)))


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop()
    else:
        main()
