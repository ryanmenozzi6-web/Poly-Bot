"""
Polymarket history check - read-only, no money, no API keys, no wallet.
Looks back over past Bitcoin 15-minute markets and counts how often the side priced
at 96-97c late in the period went on to lose. Results show on the report page.
(The old copy / weather / arb paper strategies are switched off. Their files and
saved results are left alone, they just don't run or show any more.)
Run:  python bot.py --loop
"""
import sys, time, threading, html, json, math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

from common import (TZ, REPORT_FILE, notify, money,
                    get, as_list, parse_json_field, fnum, GAMMA, CLOB, DATA, DATA_DIR)


# ---------- one-time check: buying the 96-97c side late in Bitcoin 15-min markets ----------
# Looks back over past Bitcoin 15-minute markets. For each one, finds the first moment in the
# last 4 minutes where one side was priced in a band (e.g. 96-97c) and records whether that
# side went on to lose. Read-only, runs once in the background, results are kept on disk.
BT_FILE = os.path.join(DATA_DIR, "scalp_check3.json")
BT_DAYS = 30
BT_FEE_RATE = 0.07              # Polymarket's fee on these crypto markets: rate * price * (1 - price) per share
BT_WINDOW = (30, 240)           # only look between 4:00 and 0:30 left on the clock
DIP_BUY, DIP_SELL, DIP_SECS = 0.25, 0.45, 120   # early dip: buy at 25c in the first 2 min, sell at 45c
BT_BANDS = [("90-95c", 0.895, 0.955), ("96-97c", 0.955, 0.975), ("98-99c", 0.975, 0.995)]
BT = {"end_ts": 0, "finished": 0, "markets": {}}
BT_LOCK = threading.Lock()


def bt_save():
    with BT_LOCK:
        data = json.dumps(BT)
    tmp = BT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, BT_FILE)


def bt_prices(tok_up, cond, i_up, ts):
    """Price of the Up side through one 15-min period: sorted [(time, price)]."""
    lo, hi = ts, ts + 900
    for params in ({"market": tok_up, "startTs": lo, "endTs": hi + 60, "fidelity": 1},
                   {"market": tok_up, "interval": "max", "fidelity": 1}):
        h = get(CLOB, "/prices-history", params, tries=2, quiet=True)
        pts = []
        for x in (h.get("history") if isinstance(h, dict) else None) or []:
            t, p = fnum(x.get("t")), fnum(x.get("p"))
            if t is not None and p is not None and lo <= t < hi:
                pts.append((int(t), p))
        if pts:
            return sorted(pts)
    # fallback: rebuild the price from actual trades
    pts = []
    for x in as_list(get(DATA, "/trades", {"market": cond, "limit": 500}, tries=2, quiet=True)):
        t, p = fnum(x.get("timestamp")), fnum(x.get("price"))
        if t is None or p is None or not lo <= t < hi:
            continue
        pts.append((int(t), p if str(x.get("outcomeIndex")) == str(i_up) else 1 - p))
    return sorted(pts)


def bt_check(ts):
    try:
        slug = f"btc-updown-15m-{ts}"
        ev = get(GAMMA, f"/events/slug/{slug}", tries=2, quiet=True)
        m = ev["markets"][0] if isinstance(ev, dict) and ev.get("markets") else None
        if not m:
            r = as_list(get(GAMMA, "/markets", {"slug": slug, "closed": "true"}, tries=2, quiet=True))
            m = r[0] if r else None
        if not m:
            return {"x": "market not found"}
        toks = [str(t) for t in parse_json_field(m.get("clobTokenIds"))]
        outs = [str(o).lower() for o in parse_json_field(m.get("outcomes"))]
        prices = parse_json_field(m.get("outcomePrices"))
        if len(toks) != 2 or len(prices) != 2:
            return {"x": "market not found"}
        i_up = outs.index("up") if "up" in outs else 0
        pu = fnum(prices[i_up])
        if pu is None or 0.01 < pu < 0.99:
            return {"x": "not settled yet"}
        up_won = pu >= 0.99
        allpts = [(ts + 900 - t, p) for t, p in bt_prices(toks[i_up], m.get("conditionId"), i_up, ts)]
        pts = [(left, p) for left, p in allpts if BT_WINDOW[0] <= left <= BT_WINDOW[1]]
        if not pts:
            return {"x": "no price data"}
        # early dip: first time in the first 2 minutes either side is at 25c or less
        dip = None
        for i, (left, p) in enumerate(allpts):
            if 900 - left > DIP_SECS:
                break
            if min(p, 1 - p) <= DIP_BUY:
                up_side = p <= DIP_BUY                      # which side got cheap
                later = [q if up_side else 1 - q for _, q in allpts[i + 1:]]
                side_won = up_side == up_won
                bounced = any(q >= DIP_SELL for q in later) or side_won
                dip = [round(max(min(p, 1 - p), 0.001), 4), bounced, 900 - left, side_won]
                break
        hits = {}
        for name, lo, hi in BT_BANDS:
            for left, p in pts:                       # earliest first
                fav = max(p, 1 - p)
                if lo <= fav < hi:
                    hits[name] = [round(fav, 4), (p > 0.5) == up_won, left]
                    break
        return {"x": "ok", "hits": hits, "dip": dip}
    except Exception as e:
        return {"x": f"error: {type(e).__name__}"}


def bt_run():
    try:
        if os.path.exists(BT_FILE):
            with open(BT_FILE) as f:
                BT.update(json.load(f))
        if BT["finished"]:
            return
        if not BT["end_ts"]:
            BT["end_ts"] = int((time.time() - 3600) // 900 * 900)
        todo = [t for t in (BT["end_ts"] - i * 900 for i in range(BT_DAYS * 96))
                if BT["markets"].get(str(t), {}).get("x") != "ok"]
        print(f"Scalp check: {len(todo)} Bitcoin 15-min markets to look at")
        with ThreadPoolExecutor(4) as ex:
            for n, (t, res) in enumerate(zip(todo, ex.map(bt_check, todo))):
                with BT_LOCK:
                    BT["markets"][str(t)] = res
                if n % 50 == 49:
                    bt_save()
        BT["finished"] = time.time()
        bt_save()
        notify("Scalp check done", "The 96-97c history check finished. Open the report page.", "mag")
    except Exception as e:
        print(f"! scalp check stopped: {e}")


def bt_range(k, n):
    """95% range for a true rate when we saw k out of n."""
    if not n:
        return 0, 0
    z, p = 1.96, k / n
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0, mid - half), min(1, mid + half)


def bt_report(L):
    with BT_LOCK:
        mk = dict(BT["markets"])
        end_ts, finished = BT["end_ts"], BT["finished"]
    if not end_ts:
        return
    total = BT_DAYS * 96
    ok = [m for m in mk.values() if m.get("x") == "ok"]
    first = datetime.fromtimestamp(end_ts - (total - 1) * 900, TZ)
    last = datetime.fromtimestamp(end_ts, TZ)
    L += ["", "## Late scalp check (Bitcoin 15-min)",
          (f"Finished. " if finished else f"Still running - {len(mk):,} of {total:,} periods looked at so far. ")
          + f"Covers {first:%b %d} to {last:%b %d}. {len(ok):,} periods had usable data.",
          "For each period: the first time in the last 4 minutes that one side was at that price, "
          "and whether that side then lost."]
    days = len(ok) / 96
    L += ["", "| Price paid | Times it came up | Favorite lost | Fail rate | Likely range | Breakeven | Avg result per $100 bet |",
          "|---|---|---|---|---|---|---|"]
    notes = []
    for name, _, _ in BT_BANDS:
        hits = [m["hits"][name] for m in ok if name in m.get("hits", {})]
        if not hits:
            L.append(f"| {name} | 0 | - | - | - | - | - |")
            continue
        n, fails = len(hits), sum(1 for h in hits if not h[1])
        avg_p = sum(h[0] for h in hits) / n
        breakeven = 1 - avg_p - BT_FEE_RATE * avg_p * (1 - avg_p)
        pnl = sum((100 / h[0] if h[1] else 0) - 100 - BT_FEE_RATE * 100 * (1 - h[0]) for h in hits) / n
        lo, hi = bt_range(fails, n)
        L.append(f"| {name} | {n:,} | {fails} | {fails/n*100:.1f}% | {lo*100:.1f}-{hi*100:.1f}% | "
                 f"under {breakeven*100:.1f}% | {money(pnl)} |")
        if name == "96-97c" and days:
            notes.append(f"96-97c came up about {n/days:.0f} times a day and failed about {fails/days:.1f} times a day. "
                         f"Betting $100 on every one would have {'made' if pnl >= 0 else 'lost'} {money(abs(pnl * n / days))} a day after fees.")
    L += [""] + notes
    L.append("Breakeven = the fail rate where you make nothing after fees. Below it you profit, above it you lose. "
             "Prices are the going price at each minute mark; a real buy usually pays a bit more, so real results are slightly worse.")
    # ----- early dip -----
    dips = [m["dip"] for m in ok if m.get("dip")]
    L += ["", "## Early dip check (buy the 25c side in the first 2 minutes)",
          "For each period: the first minute mark in the first 2 minutes where one side was 25c or less, "
          "what it cost right then, and what happened to it."]
    if dips and days:
        n = len(dips)
        won, back = sum(1 for d in dips if d[3]), sum(1 for d in dips if d[1])
        avg = sum(d[0] for d in dips) / n
        lo, hi = bt_range(won, n)
        pnl = sum((100 / d[0] - 100) if d[3] else -100 for d in dips) / n
        L += ["", "| Times it came up | Avg price | Won at the end | Likely range | Seen back at 45c | Avg result per $100, held to the end |",
              "|---|---|---|---|---|---|",
              f"| {n:,} | {avg*100:.1f}c | {won/n*100:.1f}% | {lo*100:.1f}-{hi*100:.1f}% | at least {back/n*100:.0f}% | {money(pnl)} |", "",
              f"It came up about {n/days:.1f} times a day. Buying $100 of every one and holding to the end would have "
              f"{'made' if pnl >= 0 else 'lost'} {money(abs(pnl * n / days))} a day.",
              "How to read it: if these dips really are mispriced, 'Won at the end' should be clearly higher than 'Avg price'. "
              "If they're about equal, the price was fair and selling at 45c can't turn it into a profit. "
              "'Seen back at 45c' is a low count because prices are only recorded about once a minute, so quick bounces get missed. "
              "No fees counted (limit orders don't pay them)."]
    else:
        L += ["", "No dips to 25c found yet."]
    bad = {}
    for m in mk.values():
        if m.get("x") != "ok":
            bad[m.get("x")] = bad.get(m.get("x"), 0) + 1
    if bad:
        L += ["", "| Periods left out | Count |", "|---|---|"] + [f"| {r} | {c} |" for r, c in sorted(bad.items(), key=lambda kv: -kv[1])]


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


def write_report():
    L = ["# Paper bot report", f"_Updated {datetime.now(TZ):%b %d, %I:%M %p} ET_"]
    n = len(L)
    bt_report(L)
    if len(L) == n:
        L += ["", "Scalp check is starting up - check back in a minute."]
    tmp = REPORT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(L) + "\n")
    os.replace(tmp, REPORT_FILE)


# ---------- run ----------
def loop():
    start_web()
    threading.Thread(target=bt_run, daemon=True).start()
    while True:
        try:
            write_report()
        except Exception as e:
            print(f"! report error (will keep going): {e}")
        time.sleep(20)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop()
    else:
        bt_run()
        write_report()
        print("Done.")
