"""Strategy 2: daily temperature markets vs free weather forecasts (paper).

Polymarket lists markets like "Highest temperature in NYC on September 29?" split into
buckets (62-63F, 64-65F, ...). We pull an ensemble forecast (dozens of model runs) for the
station the market resolves on, turn it into a probability for each bucket, and paper-buy
when the market price is clearly off from that probability.
"""
import math, re, time
from datetime import datetime, timedelta, timezone
from common import (CFG, GAMMA, LOCK, get, as_list, notify, money,
                    fnum, fee, best_ask, market_tokens, skip, open_position)

ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
STATION = "https://aviationweather.gov/api/data/stationinfo"
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                      "august", "september", "october", "november", "december"], 1)}
TITLE_RE = re.compile(r"^(highest|lowest) temperature in (.+?) on ", re.I)
SLUG_DATE_RE = re.compile(r"-on-([a-z]+)-(\d{1,2})-(\d{4})")


def W():
    return CFG["weather"]


# ---------- where is the station? ----------
def station_codes(desc):
    codes = re.findall(r"site=([A-Za-z]{4})\b", desc)
    codes += re.findall(r"/([A-Z]{4})(?=[/\s\"'),.]|$)", desc)
    seen, out = set(), []
    for c in codes:
        c = c.upper()
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def locate(s, city, desc):
    with LOCK:
        cache = s.setdefault("weather_places", {})
        if city in cache:
            return cache[city]
    spot = None
    for code in station_codes(desc):
        r = as_list(get(STATION, "", {"ids": code, "format": "json"}, tries=2, quiet=True))
        if r and fnum(r[0].get("lat")) is not None:
            spot = {"lat": fnum(r[0]["lat"]), "lon": fnum(r[0]["lon"]), "src": code}
            break
    if not spot:
        name = {"NYC": "New York"}.get(city, city)
        r = get(GEOCODE, "", {"name": name, "count": 1}, tries=2, quiet=True)
        res = (r or {}).get("results") or []
        if res:
            spot = {"lat": res[0]["latitude"], "lon": res[0]["longitude"], "src": "city"}
    if spot:
        with LOCK:
            cache[city] = spot
    return spot


# ---------- forecast -> probabilities ----------
_fc_cache = {}


def member_extremes(spot, unit, day, kind):
    """List of daily max (or min) temps on `day` (a date), one per ensemble member, plus local 'today'."""
    key = (spot["lat"], spot["lon"], unit)
    if key not in _fc_cache:
        _fc_cache[key] = get(ENSEMBLE, "", {
            "latitude": spot["lat"], "longitude": spot["lon"], "hourly": "temperature_2m",
            "models": ",".join(W()["models"]), "timezone": "auto", "forecast_days": 5,
            "temperature_unit": "fahrenheit" if unit == "F" else "celsius"}, tries=2)
    fc = _fc_cache[key]
    if not fc or "hourly" not in fc:
        return None, None
    offset = fc.get("utc_offset_seconds", 0)
    local_today = (datetime.now(timezone.utc) + timedelta(seconds=offset)).date()
    times = fc["hourly"].get("time", [])
    want = [i for i, t in enumerate(times) if t.startswith(day.isoformat())]
    if len(want) < 20:
        return None, local_today
    vals = []
    for k, series in fc["hourly"].items():
        if not k.startswith("temperature_2m") or not isinstance(series, list):
            continue
        day_vals = [series[i] for i in want if i < len(series) and series[i] is not None]
        if len(day_vals) >= 20:
            vals.append(max(day_vals) if kind == "highest" else min(day_vals))
    return vals, local_today


def parse_bucket(label):
    t = label.replace("−", "-")
    nums = [int(n) for n in re.findall(r"-?\d+", t.replace("°", " "))]
    if not nums:
        return None
    low = t.lower()
    if "below" in low or "or less" in low or "lower" in low:
        return (-math.inf, nums[0])
    if "higher" in low or "above" in low or "or more" in low:
        return (nums[0], math.inf)
    m = re.search(r"(-?\d+)\s*(?:°\s*[CF]?)?\s*(?:-|to)\s*(-?\d+)", t)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (nums[0], nums[0])


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bucket_prob(members, lo, hi, sigma):
    """Chance the whole-degree reading lands in [lo, hi], smoothing each member by sigma."""
    tot = 0.0
    for v in members:
        a = 0.0 if lo == -math.inf else ncdf((lo - 0.5 - v) / sigma)
        b = 1.0 if hi == math.inf else ncdf((hi + 0.5 - v) / sigma)
        tot += max(0.0, b - a)
    return tot / len(members)


# ---------- scan ----------
def fetch_events():
    now = datetime.now(timezone.utc)
    out = []
    for offset in range(0, 600, 100):
        page = as_list(get(GAMMA, "/events", {
            "tag_slug": "daily-temperature", "closed": "false", "limit": 100, "offset": offset,
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}))
        out += page
        if len(page) < 100:
            break
    return out


def scan(s):
    """Runs in the background thread. Network work happens outside the lock."""
    _fc_cache.clear()
    events = fetch_events()
    with LOCK:
        held = {p.get("event") for p in s["positions"] if p["strategy"] == "weather"}
    found = 0
    for ev in events:
        title, slug = ev.get("title", ""), ev.get("slug", "")
        m = TITLE_RE.match(title)
        d = SLUG_DATE_RE.search(slug)
        if not m or not d or slug in held:
            continue
        kind, city = m.group(1).lower(), m.group(2).strip()
        try:
            day = datetime(int(d.group(3)), MONTHS[d.group(1)], int(d.group(2))).date()
        except Exception:
            continue
        markets = [x for x in ev.get("markets") or [] if not x.get("closed")]
        if len(markets) < 3:
            continue
        labels = " ".join(x.get("groupItemTitle", "") for x in markets)
        unit = "F" if "°F" in labels or "ºF" in labels else "C"
        spot = locate(s, city, ev.get("description", "") + " " + (ev.get("resolutionSource") or ""))
        if not spot:
            with LOCK: skip(s, "can't find station", "weather")
            continue
        members, local_today = member_extremes(spot, unit, day, kind)
        if local_today and (day - local_today).days < W()["min_days_ahead"]:
            with LOCK: skip(s, "too close to the day", "weather")
            continue
        if not members or len(members) < 10:
            with LOCK: skip(s, "no forecast", "weather")
            continue
        sigma = W()["sigma_f"] if unit == "F" else W()["sigma_c"]

        best = None
        for mk in markets:
            rng = parse_bucket(mk.get("groupItemTitle", ""))
            yes_tok, no_tok = market_tokens(mk)
            if not rng or not yes_tok:
                continue
            p = bucket_prob(members, rng[0], rng[1], sigma)
            ask, bid = fnum(mk.get("bestAsk")), fnum(mk.get("bestBid"))
            cands = []
            if ask:
                cands.append(("Yes", yes_tok, p, ask))
            if bid:
                cands.append(("No", no_tok, 1 - p, round(1 - bid, 4)))
            for side, tok, prob, price in cands:
                if not (W()["min_price"] <= price <= W()["max_price"]):
                    continue
                edge = prob - price - fee(price, 1)
                if edge >= W()["min_edge"] and (not best or edge > best[0]):
                    best = (edge, side, tok, prob, price, mk)
        if not best:
            continue

        edge, side, tok, prob, price, mk = best
        real = best_ask(tok)          # confirm with the live order book
        if real is None:
            continue
        edge = prob - real - fee(real, 1)
        if edge < W()["min_edge"] or not (W()["min_price"] <= real <= W()["max_price"]):
            with LOCK: skip(s, "edge gone on order book", "weather")
            continue
        med = sorted(members)[len(members) // 2]
        with LOCK:
            open_position(
                s, strategy="weather", title=title, outcome=f"{side}: {mk.get('groupItemTitle')}",
                condition_id=mk.get("conditionId"), asset=tok, event=slug, category=kind,
                entry_price=real, stake=W()["stake"], model_prob=round(prob, 3),
                forecast=f"{med:.1f}{unit} ({len(members)} runs)")
        found += 1
        notify("Weather bet",
               f"{title}\n{side} on {mk.get('groupItemTitle')} @ {real*100:.0f}c\n"
               f"Forecast says {prob*100:.0f}% (median {med:.1f}{unit}). Paper {money(W()['stake'])}",
               "sun_behind_cloud")
    with LOCK:
        s["scans"]["weather"] = {"at": time.time(), "events": len(events), "bets": found}
