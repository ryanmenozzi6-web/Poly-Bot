"""
Pump.fun PAPER sniper - fake money only. No wallet, no keys, nothing can be spent.

What it does, all day:
  1. Sees every new coin launched on Pump.fun the moment it appears.
  2. Reads each coin's real price every couple of seconds straight from the Solana blockchain.
  3. Pretends to buy each coin twice with fake money - once almost instantly, once after
     watching it for 30 seconds - and pretends to sell on fixed rules. Fees, price impact and
     network costs are charged like a real trade.
  4. Scores a handful of strategies on those pretend trades, side by side.
  5. Learns: a small model studies every finished trade and starts picking coins itself.
     Its picks are only counted on coins it had never seen when it made the call.

Run:  python bot.py --loop
Optional settings (Railway variables): DATA_DIR, NTFY_TOPIC, RPC_URL (a private Solana RPC link)
"""
import base64, hashlib, html, json, math, os, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

try:
    import websocket            # from the "websocket-client" package
except Exception:
    websocket = None

# ---------- settings ----------
STAKE = 0.1            # fake SOL put into each trade
FEE = 0.0125           # Pump.fun fee, charged on the buy and on the sell
TX_COST = 0.0015       # network fee + tip per transaction, in SOL
FAST_DELAY = 3         # "instant" buy happens this many seconds after we hear about the coin
WAIT_SECS = 30         # the patient buy watches the coin this long first
ENTRY_LAG = 3          # ...then takes this long to actually get the buy in
STOP = -0.30           # sell if down 30%
TAKE = 1.00            # sell if up 100%
TRAIL_ARM = 0.40       # once up 40%...
TRAIL_DROP = 0.25      # ...sell if it falls 25% from its high
MAX_HOLD = 600         # sell after 10 minutes no matter what
DEAD_SECS = 90         # sell if nobody has traded the coin for this long
POLL = 2.0             # seconds between price checks
MAX_WATCH = 600        # most coins watched at once
MIN_TRAIN, RETRAIN_EVERY, TRAIN_WINDOW = 500, 300, 6000
MAX_ROWS = 20000

TZ = ZoneInfo("America/New_York")
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "sniper.json")
REPORT_FILE = os.path.join(DATA_DIR, "REPORT.md")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
RPC_URL = os.environ.get("RPC_URL", "").strip() or "https://api.mainnet-beta.solana.com"
PUMP_API = "https://frontend-api-v3.pump.fun"
FEEDS = [("PumpPortal", "wss://pumpportal.fun/api/data"), ("PumpDev", "wss://pumpdev.io/ws")]
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SOL_MINT = "So11111111111111111111111111111111111111112"
INIT_VSOL, INIT_VTOK, SUPPLY = 30e9, 1.073e15, 1e15      # every Pump.fun coin starts here
P0 = INIT_VSOL / INIT_VTOK

LOCK = threading.RLock()
ACTIVE = {}                                    # mint -> coin being watched
S = {"rows": [], "creators": {}, "started": time.time(), "launches": 0, "day": ""}
STAT = {"feed": {}, "rpc_ok": 0, "rpc_err": 0, "rpc_last_err": "", "skipped": {}, "last_launch": 0,
        "sol_usd": None, "lookups_ok": 0, "lookups_err": 0}
SYMS = []                                      # (time, symbol) seen in the last hour
MODEL = {"ready": False, "ok": False, "n": 0, "w": [], "mu": [], "sd": [], "thr": 1.0, "top_ret": 0.0, "trained_at": 0}
POOL = ThreadPoolExecutor(4)


def bump(key, n=1):
    STAT["skipped"][key] = STAT["skipped"].get(key, 0) + n


# ---------- small helpers ----------
def http_json(url, body=None, timeout=8):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0 paper-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def notify(title, body, tags=""):
    print(f"[notify] {title} | {body}")
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                                     headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": tags})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  ! notify failed: {e}")


def fnum(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except Exception:
        return default


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + n.to_bytes((n.bit_length() + 7) // 8, "big")


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(b):
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    return xx == 0 or pow(xx, (_P - 1) // 2, _P) == 1


def curve_address(mint):
    """The blockchain account that holds a Pump.fun coin's price (its bonding curve)."""
    seeds = b"bonding-curve" + b58decode(mint).rjust(32, b"\x00")
    prog = b58decode(PUMP_PROGRAM).rjust(32, b"\x00")
    for b in range(255, -1, -1):
        h = hashlib.sha256(seeds + bytes([b]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    return None


# ---------- price math (Pump.fun bonding curve) ----------
def buy_tokens(vs, vt, sol):
    """Tokens you'd get for `sol` SOL (after the fee) at this curve state."""
    lam = sol * (1 - FEE) * 1e9
    return vt - (vs * vt) / (vs + lam)


def sell_value(vs, vt, tokens):
    """Net SOL you'd get back selling `tokens` right now, after fee and network cost."""
    out = (vs - (vs * vt) / (vt + tokens)) / 1e9
    return out * (1 - FEE) - TX_COST


def trade_return(vs, vt, tokens):
    return sell_value(vs, vt, tokens) / (STAKE + TX_COST) - 1


# ---------- launches ----------
def add_launch(mint, creator, curve=None, name="", symbol="", dev_sol=None, seen=None, src="", info=None):
    now = time.time()
    if not mint or not isinstance(mint, str):
        return
    with LOCK:
        if mint in ACTIVE or mint in RECENT:
            return
        RECENT[mint] = now
        S["launches"] += 1
        STAT["last_launch"] = now
        STAT["feed"].setdefault(src, {"n": 0, "state": "live"})["n"] += 1
        if len(ACTIVE) >= MAX_WATCH:
            bump("too many coins at once")
            return
    try:
        curve = curve or curve_address(mint)
    except Exception:
        curve = None
    if not curve:
        bump("couldn't find price account")
        return
    sym = (symbol or "").strip().upper()[:16]
    with LOCK:
        prior = S["creators"].get(creator, 0) if creator else 0
        if creator:
            S["creators"][creator] = prior + 1
        cutoff = now - 3600
        while SYMS and SYMS[0][0] < cutoff:
            SYMS.pop(0)
        dup = sum(1 for _, x in SYMS if x == sym) if sym else 0
        SYMS.append((now, sym))
        ACTIVE[mint] = {"mint": mint, "curve": curve, "creator": creator, "name": (name or "")[:40], "symbol": sym,
                        "seen": seen or now, "dev": dev_sol, "prior": prior, "dup": dup, "info": info or {},
                        "hist": [], "changes": 0, "sells": 0, "last_change": now, "last_read": 0,
                        "fast": None, "wait": None, "feat": None, "enrich": False, "complete": False}


RECENT = {}
S_BOOT = time.time()


def on_feed_message(src, raw):
    try:
        m = json.loads(raw)
    except Exception:
        return
    if not isinstance(m, dict) or m.get("txType") != "create":
        return
    if m.get("pool") not in (None, "pump"):
        return
    if m.get("quoteMint") not in (None, SOL_MINT):
        bump("not priced in SOL")
        return
    STAT["last_ws"] = time.time()
    add_launch(m.get("mint"), m.get("traderPublicKey"), m.get("bondingCurveKey"), m.get("name"), m.get("symbol"),
               fnum(m.get("solAmount"), fnum(m.get("quoteAmount"))), src=src)


def feed_loop(name, url):
    wait = 30
    while True:
        STAT["feed"].setdefault(name, {"n": 0, "state": "connecting"})
        try:
            def on_open(ws):
                STAT["feed"][name]["state"] = "live"
                ws.send(json.dumps({"method": "subscribeNewToken"}))
            app = websocket.WebSocketApp(url, on_open=on_open, on_message=lambda ws, msg: on_feed_message(name, msg))
            t0 = time.time()
            app.run_forever(ping_interval=25, ping_timeout=10)
            wait = 30 if time.time() - t0 > 300 else min(wait * 2, 600)
        except Exception as e:
            print(f"! {name} feed error: {e}")
        STAT["feed"][name]["state"] = "reconnecting"
        time.sleep(wait)


def rest_feed_loop():
    """Backup: if the live feeds go quiet, read Pump.fun's own newest-coins list."""
    name = "Pump.fun list"
    while True:
        time.sleep(4)
        if time.time() - max(STAT.get("last_ws", 0), S_BOOT + 20 - 45) < 45:
            if name in STAT["feed"]:
                STAT["feed"][name]["state"] = "standby"
            continue
        try:
            coins = http_json(f"{PUMP_API}/coins?offset=0&limit=50&sort=created_timestamp&order=DESC&includeNsfw=true")
            STAT["feed"].setdefault(name, {"n": 0})["state"] = "live"
            now = time.time()
            for c in coins if isinstance(coins, list) else []:
                born = (fnum(c.get("created_timestamp"), 0) or 0) / 1000
                if c.get("complete") or now - born > 20 or c.get("quote_mint") not in (None, SOL_MINT):
                    continue
                add_launch(c.get("mint"), c.get("creator"), c.get("bonding_curve"), c.get("name"), c.get("symbol"),
                           seen=max(born, now - 20), src=name, info=social_info(c))
        except Exception as e:
            STAT["feed"].setdefault(name, {"n": 0})["state"] = f"down ({type(e).__name__})"
            time.sleep(30)


def social_info(c):
    return {"social": 1 if any(c.get(k) for k in ("twitter", "telegram", "website")) else 0,
            "replies": fnum(c.get("reply_count"), 0) or 0}


# ---------- blockchain reads ----------
def rpc(method, params, timeout=8):
    r = http_json(RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout)
    if "error" in r:
        raise RuntimeError(str(r["error"])[:120])
    return r["result"]


def read_curves(addresses):
    """{address: (virtual_sol, virtual_tokens, complete)} for up to 100 bonding curve accounts."""
    res = rpc("getMultipleAccounts", [addresses, {"encoding": "base64", "commitment": "processed"}])
    out = {}
    for addr, acc in zip(addresses, res.get("value") or []):
        if not acc:
            continue
        d = base64.b64decode(acc["data"][0])
        if len(d) < 49:
            continue
        vt, vs = int.from_bytes(d[8:16], "little"), int.from_bytes(d[16:24], "little")
        out[addr] = (vs, vt, bool(d[48]))
    return out


def enrich(tok):
    """Extra homework on a coin that's getting real buying: socials and who holds it."""
    info = tok["info"]
    try:
        if "social" not in info:
            info.update(social_info(http_json(f"{PUMP_API}/coins/{tok['mint']}", timeout=5)))
        STAT["lookups_ok"] += 1
    except Exception:
        STAT["lookups_err"] += 1
    try:
        big = rpc("getTokenLargestAccounts", [tok["mint"], {"commitment": "confirmed"}], timeout=5).get("value") or []
        amts = sorted((fnum(a.get("amount"), 0) or 0 for a in big), reverse=True)
        if len(amts) >= 2:                      # biggest account is the curve itself; next is the biggest holder
            info["top1"] = amts[1] / SUPPLY
            info["top5"] = sum(amts[1:6]) / SUPPLY
        STAT["lookups_ok"] += 1
    except Exception:
        STAT["lookups_err"] += 1


# ---------- the learner ----------
FEATS = ["dev buy size", "money in by 10s", "money in by 30s", "price vs launch", "drop from its high",
         "how active", "sell-offs seen", "still climbing", "creator's earlier coins", "copycat name",
         "has socials", "biggest holder", "comments"]


def vec(f):
    return [math.log1p(max(f["dev"], 0)), math.log1p(max(f["in10"], 0)), math.log1p(max(f["in30"], 0)),
            math.log(max(f["mult"], 0.01)), f["dd"], math.log1p(f["act"]), math.log1p(f["sells"]), f["mom"],
            min(f["prior"], 5), min(f["dup"], 5), f["social"] if f["social"] is not None else 0,
            f["top1"] if f["top1"] is not None else 0.0, math.log1p(f.get("replies") or 0)]


def predict(x):
    z = MODEL["w"][0] + sum(w * (v - m) / s for w, v, m, s in zip(MODEL["w"][1:], x, MODEL["mu"], MODEL["sd"]))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def train():
    with LOCK:
        rows = [r for r in S["rows"] if r.get("wait") is not None and r.get("f")][-TRAIN_WINDOW:]
    if len(rows) < MIN_TRAIN:
        return
    X = [vec(r["f"]) for r in rows]
    y = [1.0 if r["wait"] > 0 else 0.0 for r in rows]
    k, n = len(X[0]), len(X)
    mu = [sum(x[j] for x in X) / n for j in range(k)]
    sd = [max(1e-6, math.sqrt(sum((x[j] - mu[j]) ** 2 for x in X) / n)) for j in range(k)]
    Z = [[(x[j] - mu[j]) / sd[j] for j in range(k)] for x in X]
    base = min(0.99, max(0.01, sum(y) / n))
    w = [math.log(base / (1 - base))] + [0.0] * k
    for _ in range(150):
        g = [0.0] * (k + 1)
        for z, t in zip(Z, y):
            p = 1 / (1 + math.exp(-max(-30, min(30, w[0] + sum(a * b for a, b in zip(w[1:], z))))))
            e = p - t
            g[0] += e
            for j in range(k):
                g[j + 1] += e * z[j]
        w[0] -= 0.5 * g[0] / n
        for j in range(k):
            w[j + 1] -= 0.5 * (g[j + 1] / n + 0.01 * w[j + 1])
    MODEL.update({"w": w, "mu": mu, "sd": sd})
    scored = sorted(((predict(x), r["wait"]) for x, r in zip(X, rows)), reverse=True)
    top = scored[:max(20, n // 10)]
    MODEL.update({"thr": top[-1][0], "top_ret": sum(r for _, r in top) / len(top), "n": n,
                  "ok": sum(r for _, r in top) / len(top) > 0, "ready": True, "trained_at": time.time()})
    print(f"Learner trained on {n} coins; its favorite 10% averaged {MODEL['top_ret']*100:+.1f}%")


def train_loop():
    last = 0
    while True:
        time.sleep(30)
        try:
            n = len(S["rows"])
            if n >= MIN_TRAIN and (not MODEL["ready"] or n - last >= RETRAIN_EVERY):
                last = n
                train()
        except Exception as e:
            print(f"! learner error: {e}")


# ---------- watching and paper trading ----------
def open_pos(vs, vt, now):
    return {"t": now, "tok": buy_tokens(vs, vt, STAKE), "peak": -1.0, "ret": None, "why": None}


def check_exit(pos, tok, vs, vt, now):
    r = trade_return(vs, vt, pos["tok"])
    pos["peak"] = max(pos["peak"], r)
    why = None
    if tok["complete"]:
        why = "graduated"
    elif r <= STOP:
        why = "stop"
    elif r >= TAKE:
        why = "target"
    elif pos["peak"] >= TRAIL_ARM and (1 + r) <= (1 + pos["peak"]) * (1 - TRAIL_DROP):
        why = "trail"
    elif now - pos["t"] >= MAX_HOLD:
        why = "time"
    elif now - tok["last_change"] >= DEAD_SECS:
        why = "dead"
    if why:
        pos["ret"], pos["why"] = r, why


def features(tok):
    h = tok["hist"]
    def inflow(at):
        pts = [vs for t, vs, vt in h if t <= at]
        return ((pts[-1] if pts else h[0][1]) - INIT_VSOL) / 1e9
    vs, vt = h[-1][1], h[-1][2]
    prices = [a / b for _, a, b in h]
    in30 = inflow(WAIT_SECS + POLL)
    dev = tok["dev"] if tok["dev"] is not None else (h[0][1] - INIT_VSOL) / 1e9
    i = tok["info"]
    return {"dev": round(max(dev, 0), 3), "in10": round(inflow(10), 3), "in30": round(in30, 3),
            "mult": round((vs / vt) / P0, 3), "dd": round(1 - prices[-1] / max(prices), 3),
            "act": tok["changes"], "sells": tok["sells"], "mom": round(in30 - inflow(20), 3),
            "prior": tok["prior"], "dup": tok["dup"], "social": i.get("social"), "top1": i.get("top1"),
            "replies": i.get("replies")}


def update(tok, vs, vt, complete, now):
    age = now - tok["seen"]
    h = tok["hist"]
    if h:
        if (vs, vt) != (h[-1][1], h[-1][2]):
            tok["changes"] += 1
            tok["last_change"] = now
            if vs < h[-1][1]:
                tok["sells"] += 1
    tok["complete"] = complete
    tok["last_read"] = now
    if len(h) < 400:
        h.append((round(age, 1), vs, vt))
    else:
        h[-1] = (round(age, 1), vs, vt)

    if tok["fast"] is None and age >= FAST_DELAY and not complete:
        tok["fast"] = open_pos(vs, vt, now)
    if not tok["enrich"] and age >= WAIT_SECS - 8 and (vs - INIT_VSOL) / 1e9 >= 1.0:
        tok["enrich"] = True
        POOL.submit(enrich, tok)
    if tok["wait"] is None and age >= WAIT_SECS + ENTRY_LAG and not complete:
        tok["feat"] = features(tok)
        tok["wait"] = open_pos(vs, vt, now)
        if MODEL["ready"]:
            p = predict(vec(tok["feat"]))
            tok["p"], tok["pick"] = round(p, 3), bool(MODEL["ok"] and p >= MODEL["thr"])
    for k in ("fast", "wait"):
        pos = tok[k]
        if pos and pos["ret"] is None:
            check_exit(pos, tok, vs, vt, now)

    fast_done = tok["fast"] is not None and tok["fast"]["ret"] is not None
    wait_done = tok["wait"] is not None and tok["wait"]["ret"] is not None
    if (fast_done and wait_done) or (complete and age > 5) or age > WAIT_SECS + MAX_HOLD + 120:
        finish(tok, now)


def finish(tok, now):
    f, w = tok["fast"], tok["wait"]
    row = {"t": int(now), "s": tok["symbol"] or tok["mint"][:6], "m": tok["mint"],
           "fast": round(f["ret"], 4) if f and f["ret"] is not None else None,
           "wait": round(w["ret"], 4) if w and w["ret"] is not None else None,
           "why": w["why"] if w else (f["why"] if f else None), "f": tok["feat"]}
    if "p" in tok:
        row["p"], row["pick"] = tok["p"], tok["pick"]
    with LOCK:
        ACTIVE.pop(tok["mint"], None)
        if row["fast"] is None and row["wait"] is None:
            bump("gone before we could buy")
            return
        S["rows"].append(row)
        if len(S["rows"]) > MAX_ROWS:
            del S["rows"][:len(S["rows"]) - MAX_ROWS]


def poll_once():
    now = time.time()
    with LOCK:
        toks = list(ACTIVE.values())
        for m in [m for m, t in RECENT.items() if now - t > 3600]:
            RECENT.pop(m, None)
    for i in range(0, len(toks), 100):
        chunk = toks[i:i + 100]
        try:
            got = read_curves([t["curve"] for t in chunk])
            STAT["rpc_ok"] += 1
        except Exception as e:
            STAT["rpc_err"] += 1
            STAT["rpc_last_err"] = f"{type(e).__name__}: {e}"[:140]
            time.sleep(1.5)
            continue
        now = time.time()
        for t in chunk:
            v = got.get(t["curve"])
            if not v:
                continue
            vs, vt, done = v
            if not (20e9 <= vs <= 400e9 and vt > 0):
                with LOCK:
                    ACTIVE.pop(t["mint"], None)
                bump("price account unreadable")
                continue
            try:
                update(t, vs, vt, done, now)
            except Exception as e:
                print(f"! update error {t['mint']}: {e}")
                with LOCK:
                    ACTIVE.pop(t["mint"], None)
    now = time.time()
    with LOCK:
        for t in list(ACTIVE.values()):
            if not t["last_read"] and now - t["seen"] > 60:
                ACTIVE.pop(t["mint"], None)
                bump("never got a price")
            elif t["last_read"] and now - t["last_read"] > 120:
                ACTIVE.pop(t["mint"], None)
                bump("lost its price feed")


def poll_loop():
    while True:
        t0 = time.time()
        try:
            poll_once()
        except Exception as e:
            print(f"! poll error (will keep going): {e}")
        time.sleep(max(0.3, POLL - (time.time() - t0)))


# ---------- strategies (all judged on the same pretend trades) ----------
def others_money(f):
    return f["in30"] - f["dev"]


STRATS = [
    ("Buy everything instantly", "fast", lambda r: True,
     "Blind sniping: buy every coin about 3 seconds after launch."),
    ("Buy everything after 30s", "wait", lambda r: True,
     "No filter, just late. The baseline the smart ones have to beat."),
    ("Momentum", "wait", lambda r: others_money(r["f"]) >= 2 and r["f"]["dd"] <= 0.15 and r["f"]["dev"] <= 5,
     "Only coins where other people put in 2+ SOL in 30s, it isn't already dumping, and the creator didn't buy a huge bag."),
    ("Picky", "wait", lambda r: others_money(r["f"]) >= 2 and r["f"]["dd"] <= 0.15 and r["f"]["dev"] <= 5
        and r["f"]["prior"] == 0 and r["f"]["dup"] == 0 and r["f"]["social"] == 1
        and (r["f"]["top1"] is None or r["f"]["top1"] < 0.10),
     "Momentum, plus: first coin from this creator, not a copycat name, has socials, no single wallet holding 10%+."),
    ("Learner", "wait", lambda r: r.get("pick") is True,
     "The model's own picks. Only counts coins it had never seen when it chose."),
]


def strat_rows(rows, key, cond):
    out = []
    for r in rows:
        if r.get(key) is None:
            continue
        if key == "wait" and not r.get("f"):
            continue
        try:
            if cond(r):
                out.append(r)
        except Exception:
            pass
    return out


# ---------- report ----------
def money(x):
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def sol(x):
    usd = STAT["sol_usd"]
    return f"{x:+.2f} SOL" + (f" ({money(x * usd)})" if usd else "")


def pct(x):
    return f"{x*100:+.1f}%"


def ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s//60} min ago" if s < 7200 else f"{s//3600} h ago"


def bucket_table(L, title, rows, keyfn, order):
    b = {}
    for r in rows:
        try:
            k = keyfn(r["f"])
        except Exception:
            k = None
        if k is None:
            continue
        x = b.setdefault(k, [0, 0, 0.0])
        x[0] += 1; x[1] += r["wait"] > 0; x[2] += r["wait"]
    if not b:
        return
    L += ["", f"### {title}", "| | Coins | Made money | Avg result |", "|---|---|---|---|"]
    for k in order:
        if k in b:
            n, w, t = b[k]
            L.append(f"| {k} | {n:,} | {w/n*100:.0f}% | {pct(t/n)} |")


def write_report():
    with LOCK:
        rows = list(S["rows"])
        watching = len(ACTIVE)
        launches = S["launches"]
    hours = max((time.time() - S["started"]) / 3600, 1e-9)
    L = ["# Paper sniper report", f"_Updated {datetime.now(TZ):%b %d, %I:%M %p} ET_", "",
         "Fake money only. Every trade is 0.1 SOL with real fees, price impact and network costs charged.", "",
         "## Is it running",
         f"{launches:,} launches seen ({launches/hours:,.0f} an hour), last one {ago(STAT['last_launch'])}. "
         f"Watching {watching} coins right now. {len(rows):,} coins finished."]
    feeds = ", ".join(f"{k}: {v.get('state', '?')} ({v.get('n', 0):,})" for k, v in STAT["feed"].items()) or "none yet"
    tot = STAT["rpc_ok"] + STAT["rpc_err"]
    L.append(f"Launch feeds - {feeds}. Price reads: {STAT['rpc_ok']:,} ok, {STAT['rpc_err']:,} failed"
             + (f" ({STAT['rpc_err']/tot*100:.0f}% failing)" if tot else "") + ".")
    if tot > 20 and STAT["rpc_err"] / tot > 0.3:
        L.append(f"Price reads are failing a lot. Last error: {STAT['rpc_last_err']}")
    if websocket is None:
        L.append("Live feed library is missing (requirements.txt wasn't updated), so it's using the slower backup list.")

    L += ["", "## Strategies", "| Strategy | Trades | Made money | Avg per trade | Total | Per day |", "|---|---|---|---|---|---|"]
    days = max(hours / 24, 1e-9)
    for name, key, cond, _ in STRATS:
        rs = strat_rows(rows, key, cond)
        if not rs:
            L.append(f"| {name} | 0 | - | - | - | - |")
            continue
        rets = [r[key] for r in rs]
        total = sum(rets) * (STAKE + TX_COST)
        L.append(f"| {name} | {len(rs):,} | {sum(1 for x in rets if x > 0)/len(rs)*100:.0f}% | "
                 f"{pct(sum(rets)/len(rs))} | {sol(total)} | {sol(total/days)} |")
    L.append("")
    for name, _, _, desc in STRATS:
        L.append(f"{name}: {desc}")
    L.append(f"Selling rules (same for all): out at {STOP*100:.0f}%, out at +{TAKE*100:.0f}%, once up {TRAIL_ARM*100:.0f}% "
             f"sell if it drops {TRAIL_DROP*100:.0f}% from the high, out after {MAX_HOLD//60} minutes or if trading stops.")

    # ----- learner -----
    L += ["", "## The learner"]
    usable = sum(1 for r in rows if r.get("wait") is not None and r.get("f"))
    if not MODEL["ready"]:
        L.append(f"Still studying: {usable:,} of {MIN_TRAIN} coins needed before it starts picking.")
    else:
        L.append(f"Last studied {MODEL['n']:,} coins {ago(MODEL['trained_at'])}. On those, its favorite 10% averaged "
                 f"{pct(MODEL['top_ret'])} per trade. " + ("It is picking coins now." if MODEL["ok"] else
                 "That's a loss, so it is sitting out until it finds something that works."))
        scored = [r for r in rows if r.get("p") is not None and r.get("wait") is not None]
        if scored:
            L += ["", "| How much the learner liked it | Coins | Made money | Avg result |", "|---|---|---|---|"]
            scored.sort(key=lambda r: -r["p"])
            n = len(scored)
            for label, a, b in (("Top 10%", 0, n // 10), ("Next 20%", n // 10, n * 3 // 10), ("Bottom 70%", n * 3 // 10, n)):
                g = scored[a:b]
                if g:
                    L.append(f"| {label} | {len(g):,} | {sum(1 for r in g if r['wait'] > 0)/len(g)*100:.0f}% | "
                             f"{pct(sum(r['wait'] for r in g)/len(g))} |")
            L.append("")
            L.append("These are coins it scored before knowing the result. If 'Top 10%' is clearly better than the rest, it's really learning something.")
        top = sorted(zip(FEATS, MODEL["w"][1:]), key=lambda kv: -abs(kv[1]))[:6]
        L += ["", "What it weighs most: " + ", ".join(f"{n} ({'good' if w > 0 else 'bad'} sign)" for n, w in top) + "."]

    # ----- what predicts winners -----
    wr = [r for r in rows if r.get("wait") is not None and r.get("f")]
    if wr:
        L += ["", "## What separates winners from losers (buying at 30s)"]
        bucket_table(L, "Other people's money in the first 30s", wr,
                     lambda f: "under 0.5 SOL" if others_money(f) < 0.5 else "0.5-2 SOL" if others_money(f) < 2
                     else "2-5 SOL" if others_money(f) < 5 else "5+ SOL", ["under 0.5 SOL", "0.5-2 SOL", "2-5 SOL", "5+ SOL"])
        bucket_table(L, "Creator's own buy at launch", wr,
                     lambda f: "none" if f["dev"] < 0.01 else "under 1 SOL" if f["dev"] < 1 else "1-3 SOL" if f["dev"] < 3 else "3+ SOL",
                     ["none", "under 1 SOL", "1-3 SOL", "3+ SOL"])
        bucket_table(L, "Creator launched other coins while we watched", wr,
                     lambda f: "first one" if f["prior"] == 0 else "1-2 before" if f["prior"] <= 2 else "3+ before (serial launcher)",
                     ["first one", "1-2 before", "3+ before (serial launcher)"])
        bucket_table(L, "Socials (only checked on coins with 1+ SOL in)", wr,
                     lambda f: None if f["social"] is None else "has socials" if f["social"] else "no socials",
                     ["has socials", "no socials"])
        bucket_table(L, "Biggest single holder (only checked on coins with 1+ SOL in)", wr,
                     lambda f: None if f["top1"] is None else "under 5%" if f["top1"] < 0.05 else "5-10%" if f["top1"] < 0.10 else "10%+",
                     ["under 5%", "5-10%", "10%+"])

    # ----- how trades ended + recent -----
    if wr:
        why = {}
        for r in wr:
            x = why.setdefault(r.get("why") or "?", [0, 0.0])
            x[0] += 1; x[1] += r["wait"]
        names = {"stop": "Hit the -30% stop", "target": "Hit +100%", "trail": "Sold on the way down from a high",
                 "time": "Timed out at 10 min", "dead": "Trading dried up", "graduated": "Graduated off Pump.fun"}
        L += ["", "## How the 30s trades ended", "| Ending | Coins | Avg result |", "|---|---|---|"]
        for k, (n, t) in sorted(why.items(), key=lambda kv: -kv[1][0]):
            L.append(f"| {names.get(k, k)} | {n:,} | {pct(t/n)} |")
        L += ["", "## Latest finished coins", "| Coin | Instant buy | 30s buy | Others' money at 30s | Learner picked |", "|---|---|---|---|---|"]
        for r in rows[-20:][::-1]:
            f = r.get("f") or {}
            L.append(f"| {str(r['s']).replace('|', '/')} | {pct(r['fast']) if r.get('fast') is not None else '-'} | "
                     f"{pct(r['wait']) if r.get('wait') is not None else '-'} | "
                     f"{others_money(f):.1f} SOL | {'yes' if r.get('pick') else ''} |" if f else
                     f"| {str(r['s']).replace('|', '/')} | {pct(r['fast']) if r.get('fast') is not None else '-'} | - | - | |")
    if STAT["skipped"]:
        L += ["", "| Coins left out | Count |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in sorted(STAT["skipped"].items(), key=lambda kv: -kv[1])]
    tmp = REPORT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(L) + "\n")
    os.replace(tmp, REPORT_FILE)


def md_to_html(md):
    out, in_table = [], False
    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-") for c in cells):
                continue
            if not in_table:
                out.append("<table>"); in_table = True
            out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
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
            "<meta http-equiv=refresh content=60><title>Paper sniper</title><style>"
            "body{font-family:system-ui,sans-serif;background:#111;color:#eee;padding:16px;max-width:960px;margin:auto}"
            "table{border-collapse:collapse;margin-bottom:12px;font-size:14px;display:block;overflow-x:auto}"
            "td{border:1px solid #333;padding:6px 10px}tr:first-child td{font-weight:600;background:#1c1c1c}"
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


# ---------- housekeeping ----------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                S.update(json.load(f))
        except Exception as e:
            print(f"! couldn't read saved results, starting fresh: {e}")


def save_state():
    with LOCK:
        if len(S["creators"]) > 60000:
            S["creators"] = {k: v for k, v in S["creators"].items() if v > 1}
        data = json.dumps(S)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, STATE_FILE)


def sol_price_loop():
    while True:
        try:
            STAT["sol_usd"] = float(http_json("https://api.coinbase.com/v2/prices/SOL-USD/spot")["data"]["amount"])
        except Exception:
            pass
        time.sleep(600)


def daily_summary():
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour < 21 or S.get("day") == today:
        return
    S["day"] = today
    with LOCK:
        rows = list(S["rows"])
    lines = []
    for name, key, cond, _ in STRATS:
        rs = strat_rows(rows, key, cond)
        if rs:
            lines.append(f"{name}: {len(rs)} trades, avg {pct(sum(r[key] for r in rs)/len(rs))}")
    if lines:
        notify("Paper sniper daily summary", "\n".join(lines), "bar_chart")


def loop():
    load_state()
    port = int(os.environ.get("PORT", "8080"))
    srv = HTTPServer(("0.0.0.0", port), ReportHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Report page running on port {port}")
    if websocket is not None:
        for name, url in FEEDS:
            threading.Thread(target=feed_loop, args=(name, url), daemon=True).start()
    for fn in (rest_feed_loop, poll_loop, train_loop, sol_price_loop):
        threading.Thread(target=fn, daemon=True).start()
    last_save = time.time()
    while True:
        try:
            write_report()
            daily_summary()
            if time.time() - last_save > 60:
                save_state()
                last_save = time.time()
        except Exception as e:
            print(f"! report error (will keep going): {e}")
        time.sleep(15)


if __name__ == "__main__":
    loop()
