"""Shared helpers for all three paper strategies (copy, weather, arb)."""
import json, os, time, shutil, threading, urllib.request, urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DONE = ("won", "lost", "sold")
TZ = ZoneInfo("America/New_York")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
REPORT_FILE = os.path.join(DATA_DIR, "REPORT.md")
os.makedirs(DATA_DIR, exist_ok=True)

with open("config.json") as f:
    CFG = json.load(f)

LOCK = threading.RLock()   # guards the shared state dict


# ---------- http ----------
def get(base, path, params=None, tries=3, quiet=False):
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
                if not quiet:
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
        for k in ("data", "results", "items", "leaderboard", "markets", "events"):
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


def fnum(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def parse_time(v):
    """ISO-ish timestamp -> unix seconds, or None."""
    if not v:
        return None
    s = str(v).strip().replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) >= 3 and (s[-3] in "+-") and s[-3:].lstrip("+-").isdigit():
        s += ":00"   # "+00" -> "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def fee(price, shares):
    return CFG["taker_fee_rate"] * price * (1 - price) * shares


def clean(t):
    return str(t).replace("|", "/")


def money(x):
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


# ---------- market data ----------
_market_cache = {}


def get_market(condition_id):
    """Gamma market dict for a condition id (cached), or None."""
    if not condition_id:
        return None
    if condition_id not in _market_cache:
        m = as_list(get(GAMMA, "/markets", {"condition_ids": condition_id}))
        if not m:
            m = as_list(get(GAMMA, "/markets", {"condition_ids": condition_id, "closed": "true"}))
        _market_cache[condition_id] = m[0] if m else None
    return _market_cache[condition_id]


def clear_market_cache():
    _market_cache.clear()


def market_info(condition_id, asset, outcome_name=None):
    """Returns (current_price, closed) for our outcome, or (None, None)."""
    m = get_market(condition_id)
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
    return fnum(prices[idx]), bool(m.get("closed"))


def book(asset):
    b = get(CLOB, "/book", {"token_id": asset}, tries=2, quiet=True)
    return b if isinstance(b, dict) else {}


def best_ask(asset, with_size=False):
    """Lowest ask (price we'd pay). with_size=True -> (price, shares available at that price)."""
    asks = []
    for a in book(asset).get("asks") or []:
        p, sz = fnum(a.get("price")), fnum(a.get("size"), 0)
        if p is not None:
            asks.append((p, sz))
    if not asks:
        return (None, 0) if with_size else None
    p = min(x[0] for x in asks)
    return (p, sum(sz for q, sz in asks if q == p)) if with_size else p


def best_bid(asset):
    bids = [fnum(b.get("price")) for b in book(asset).get("bids") or []]
    bids = [b for b in bids if b is not None]
    return max(bids) if bids else None


def market_tokens(m):
    """(yes_token, no_token) for a binary gamma market."""
    toks = [str(t) for t in parse_json_field(m.get("clobTokenIds"))]
    outs = parse_json_field(m.get("outcomes"))
    if len(toks) != 2:
        return None, None
    if outs and len(outs) == 2 and str(outs[0]).lower() == "no":
        return toks[1], toks[0]
    return toks[0], toks[1]


# ---------- state ----------
def empty_state():
    return {"traders": [], "traders_refreshed": 0, "last_seen": {}, "positions": [],
            "skipped": {}, "last_summary_date": "", "their_sizes": {}, "benched": {},
            "scans": {}}


def load_state():
    if not os.path.exists(STATE_FILE) and STATE_FILE != "state.json" and os.path.exists("state.json"):
        shutil.copy("state.json", STATE_FILE)
    s = empty_state()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s.update(json.load(f))
    for p in s["positions"]:
        p.setdefault("strategy", "copy")
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE_FILE)


def skip(s, reason, strategy="copy"):
    key = reason if strategy == "copy" else f"{strategy}: {reason}"
    s["skipped"][key] = s["skipped"].get(key, 0) + 1


def open_position(s, **kw):
    """Record a paper buy. Needs: strategy, title, outcome, condition_id, asset, entry_price, stake."""
    entry, stake = kw["entry_price"], kw["stake"]
    shares = kw.pop("shares", None) or stake / entry
    pos = dict(kw)
    pos.update({"shares": shares, "fee": fee(entry, shares), "opened": time.time(),
                "status": "open", "current_price": entry, "pnl": None})
    s["positions"].append(pos)
    return pos


def realized(s, strategy=None):
    return sum(p["pnl"] for p in s["positions"]
               if p["status"] in DONE and (strategy is None or p["strategy"] == strategy))


def update_positions(s):
    """Mark open positions and settle resolved ones. Works for every strategy."""
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
            if p["strategy"] == "arb":
                continue   # arb legs are summarized per basket, not one ping per leg
            label = {"copy": f"copied {p.get('trader')}", "weather": "weather"}.get(p["strategy"], p["strategy"])
            notify(f"{'WIN' if p['status']=='won' else 'LOSS'} {money(p['pnl'])} ({p['strategy']})",
                   f"{p['title']} ({p['outcome']}) - {label}\n{p['strategy']} total: {money(realized(s, p['strategy']))}",
                   "white_check_mark" if p["status"] == "won" else "x")
