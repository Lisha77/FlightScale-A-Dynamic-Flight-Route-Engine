"""Phase 2 — Live API integration: weather & airspace congestion penalty providers.

Every provider returns a *penalty multiplier* >= 1.0 for a geographic point:

* ``1.0``  -> clear skies / empty sector (no resistance)
* ``5.0``  -> severe storm cell (matches the reference implementation)
* ``>1.0`` -> scaled linearly by the user's "simulated constraints" severity

Realtime architecture
---------------------
All network I/O happens in :func:`prefetch_live`, which fetches *whole grid
cells at once* in two parallel pools (weather + paced traffic) and applies a
**provider fallback chain** so a single broken/rate-limited provider can never
stall the pipeline:

* Weather: OpenWeatherMap (key)  -> Open-Meteo (keyless) -> simulation
* Traffic: OpenSky (anonymous)   -> adsb.lol (keyless)   -> simulation

Failed cells are retried automatically after ``LIVE_RETRY_TTL_S`` instead of
being permanently poisoned with simulated values, and every successful lookup
records which provider served it for the dashboard telemetry.  The per-cell
penalties are cached for the process lifetime; ``clear_api_caches()`` resets
caches *and* provider budgets ("Refresh live data" button).

The read side (:func:`get_live_weather_penalty`, :func:`get_traffic_penalty`)
never blocks on the network: on a cache miss it derives the deterministic
simulation locally — the prefetch pass is what upgrades cells to live data.
"""

import hashlib
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import (
    ADSB_BUDGET,
    GRID_CELL_DEG,
    HTTP_TIMEOUT,
    LIVE_RETRY_TTL_S,
    LIVE_WORKERS,
    METEO_BACKOFF_S,
    METEO_BATCH_SIZE,
    METEO_MAX_RETRIES,
    METEO_PACE_S,
    OPENMETEO_BUDGET,
    OPENSKY_BUDGET,
    OPENWEATHER_API_KEY,
    OWM_BUDGET,
    TRAFFIC_PACE_S,
    TRAFFIC_WORKERS,
)

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"
METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPENSKY_URL = "https://opensky-network.org/api/states/all"
ADSB_URL = "https://api.adsb.lol/v2/point/{lat}/{lon}/250"   # radius in NM

MAX_PENALTY = 12.0
MAX_TRAFFIC_PENALTY = 6.0

# Condition -> excess penalty above the 1.0 baseline (OpenWeatherMap `main`)
WEATHER_CONDITION_EXCESS = {
    "Thunderstorm": 4.0,   # 1 + 4.0 = 5.0 for a severe storm cell
    "Snow": 2.0,
    "Rain": 1.5,
    "Drizzle": 0.6,
    "Fog": 0.5,
    "Mist": 0.4,
    "Haze": 0.4,
    "Smoke": 0.6,
    "Dust": 0.7,
    "Sand": 0.7,
    "Ash": 1.5,
    "Squall": 3.0,
    "Tornado": 5.0,
    "Clouds": 0.1,
    "Clear": 0.0,
}

# Major hubs used to synthesise realistic airspace congestion in simulation mode.
_MAJOR_HUBS = [
    (40.64, -73.78), (34.05, -118.41), (41.98, -87.90), (33.64, -84.43),
    (47.45, -122.31), (37.62, -122.38), (25.79, -80.29), (43.68, -79.63),
    (19.44, -99.07), (-23.43, -46.47), (-34.82, -58.54), (51.47, -0.45),
    (49.01, 2.55), (50.03, 8.56), (52.31, 4.76), (40.47, -3.56),
    (41.80, 12.25), (59.81, 17.92), (25.25, 55.36), (25.27, 51.61),
    (1.36, 103.99), (13.69, 100.75), (22.31, 113.91), (31.14, 121.81),
    (40.08, 116.58), (35.55, 139.78), (37.46, 126.44), (39.17, 125.94),
    (-33.94, 151.18), (-37.67, 144.84), (28.56, 77.10), (19.09, 72.87),
    (6.58, 3.32), (-26.13, 28.24), (30.12, 31.41),
]

# ---------------------------------------------------------------------------
# Mutable fetch statistics (reset per routing pass by the dashboard)
# ---------------------------------------------------------------------------
STATS = {
    "weather_live": 0, "weather_sim": 0, "weather_failed": 0,
    "traffic_live": 0, "traffic_sim": 0, "traffic_failed": 0,
    "cache_hits": 0, "total_ms": 0.0, "requests": 0,
    "providers": {},     # provider name -> successful lookups this pass
    "last_error": "",    # most recent provider-chain failure message
    "retries": 0,        # backoff retries after 429/503/network errors
    "rate_limited": 0,   # times a provider answered 429/503
}

_WX_CACHE: dict = {}      # (cell, live) -> (raw_penalty, mode)
_TRF_CACHE: dict = {}
_WX_FAIL: dict = {}       # cell -> monotonic timestamp of last total failure
_TRF_FAIL: dict = {}

# Process-wide provider budgets (refilled only by clear_api_caches()).
_BUDGETS = {"owm": OWM_BUDGET, "meteo": OPENMETEO_BUDGET,
            "sky": OPENSKY_BUDGET, "adsb": ADSB_BUDGET}
_BUDGET_LOCK = threading.Lock()
_LOCAL = threading.local()   # one requests.Session per worker thread


def reset_api_stats() -> None:
    """Clear per-pass fetch statistics (caches and provider budgets persist)."""
    STATS.update({
        "weather_live": 0, "weather_sim": 0, "weather_failed": 0,
        "traffic_live": 0, "traffic_sim": 0, "traffic_failed": 0,
        "cache_hits": 0, "total_ms": 0.0, "requests": 0,
        "providers": {}, "last_error": "",
        "retries": 0, "rate_limited": 0,
    })


def clear_api_caches() -> None:
    """Drop every cached cell, failure mark and provider budget.

    Used by the dashboard's "Refresh live data" button so the next routing
    pass re-fetches realtime weather/traffic from scratch.
    """
    with _BUDGET_LOCK:
        _WX_CACHE.clear()
        _TRF_CACHE.clear()
        _WX_FAIL.clear()
        _TRF_FAIL.clear()
        _BUDGETS.update({"owm": OWM_BUDGET, "meteo": OPENMETEO_BUDGET,
                         "sky": OPENSKY_BUDGET, "adsb": ADSB_BUDGET})
    reset_api_stats()


def get_api_stats() -> dict:
    """Return a snapshot of API resolution metrics for the evaluation section."""
    made = STATS["requests"]
    avg_ms = STATS["total_ms"] / made if made else 0.0
    return {
        **STATS,
        "live_calls": STATS["weather_live"] + STATS["traffic_live"],
        "simulated": STATS["weather_sim"] + STATS["traffic_sim"],
        "failed": STATS["weather_failed"] + STATS["traffic_failed"],
        "avg_resolution_ms": round(avg_ms, 2),
        "total_resolution_ms": round(STATS["total_ms"], 2),
    }


def get_cache_summary() -> dict:
    """Share of grid cells that currently hold live (non-simulated) data."""
    wx_total = sum(1 for k in _WX_CACHE if k[1])
    wx_live = sum(1 for k, (_raw, mode) in _WX_CACHE.items()
                  if k[1] and mode != "sim")
    trf_total = sum(1 for k in _TRF_CACHE if k[1])
    trf_live = sum(1 for k, (_raw, mode) in _TRF_CACHE.items()
                   if k[1] and mode != "sim")
    return {"wx_live": wx_live, "wx_total": wx_total,
            "trf_live": trf_live, "trf_total": trf_total}


def _claim(budget: str) -> bool:
    """Atomically consume one call from a process-wide provider budget."""
    with _BUDGET_LOCK:
        if _BUDGETS.get(budget, 0) > 0:
            _BUDGETS[budget] -= 1
            return True
        return False


def _budget_left(kind: str) -> bool:
    """True while at least one provider in the chain can still be attempted."""
    with _BUDGET_LOCK:
        if kind == "wx":
            return _BUDGETS["meteo"] > 0 or bool(OPENWEATHER_API_KEY and _BUDGETS["owm"] > 0)
        return _BUDGETS["adsb"] > 0 or _BUDGETS["sky"] > 0


def _session() -> requests.Session:
    """Thread-local HTTP session (connection reuse across pooled workers)."""
    sess = getattr(_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        _LOCAL.session = sess
    return sess


def _record_provider(name: str) -> None:
    with _BUDGET_LOCK:
        STATS["providers"][name] = STATS["providers"].get(name, 0) + 1


def _cell(lat: float, lon: float):
    """Coarse geographic cell used as the penalty cache key."""
    return (math.floor(lat / GRID_CELL_DEG), math.floor(lon / GRID_CELL_DEG))


def _cell_rng(cell, salt: str) -> random.Random:
    """Deterministic RNG per grid cell -> stable 'live-like' simulated data."""
    digest = hashlib.blake2b(f"{cell[0]}:{cell[1]}:{salt}".encode(), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _scale(raw: float, severity: float, cap: float) -> float:
    """Apply the user's simulated-constraint severity to a raw penalty."""
    value = 1.0 + (raw - 1.0) * max(0.0, severity)
    return max(1.0, min(cap, value))


def _wind_excess(wind_ms: float, gust_ms: float = 0.0) -> float:
    """Penalty contribution of headwind-equivalent wind speed (m/s)."""
    return 0.6 * (wind_ms + 0.5 * max(0.0, gust_ms)) / 30.0


# ---------------------------------------------------------------------------
# Weather — provider chain: OpenWeatherMap -> Open-Meteo -> simulation
# ---------------------------------------------------------------------------
# WMO weather codes reported by Open-Meteo, mapped to the same excess scale.
WMO_CODE_EXCESS = {
    0: 0.0, 1: 0.05, 2: 0.1, 3: 0.15,
    45: 0.5, 48: 0.5,
    51: 0.4, 53: 0.6, 55: 0.8, 56: 0.6, 57: 0.8,
    61: 1.0, 63: 1.5, 65: 2.2, 66: 1.5, 67: 2.2,
    71: 1.5, 73: 2.0, 75: 2.5, 77: 2.0,
    80: 1.2, 81: 1.8, 82: 2.5, 85: 2.0, 86: 2.5,
    95: 4.0, 96: 4.5, 99: 5.0,
}


def _fetch_weather(lat: float, lon: float) -> float:
    t0 = time.perf_counter()
    STATS["requests"] += 1
    resp = _session().get(
        OWM_URL,
        params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    STATS["total_ms"] += (time.perf_counter() - t0) * 1000.0

    condition = "Clear"
    weather_block = data.get("weather") or []
    if weather_block:
        condition = weather_block[0].get("main", "Clear")
    wind = data.get("wind") or {}
    excess = WEATHER_CONDITION_EXCESS.get(condition, 0.0)
    raw = 1.0 + excess + _wind_excess(float(wind.get("speed", 0.0) or 0.0),
                                      float(wind.get("gust", 0.0) or 0.0))
    return min(MAX_PENALTY, raw)


# --- Open-Meteo: global pacing + 429/503 backoff ---------------------------
_METEO_CURRENT = "weather_code,wind_speed_10m,wind_gusts_10m"
_METEO_STATE = {"last_request": 0.0}
_METEO_LOCK = threading.Lock()


def _meteo_pace() -> None:
    """Enforce a global minimum interval between Open-Meteo requests.

    This is the primary 429 guard: no matter how many worker threads run,
    the endpoint never sees bursts faster than one request per METEO_PACE_S.
    """
    with _METEO_LOCK:
        wait = METEO_PACE_S - (time.monotonic() - _METEO_STATE["last_request"])
        if wait > 0:
            time.sleep(wait)
        _METEO_STATE["last_request"] = time.monotonic()


def _meteo_get(params: dict):
    """GET Open-Meteo with pacing and one retry on transport errors.

    HTTP 429/503 is treated as terminal (fail fast): the provider is
    signalling backpressure, so retrying in a tight loop only prolongs the
    stall.  Failed cells fall back to simulation and are automatically
    re-attempted after ``LIVE_RETRY_TTL_S`` while provider budgets last.
    """
    last_exc = None
    for attempt in range(2):
        _meteo_pace()
        STATS["requests"] += 1
        t0 = time.perf_counter()
        try:
            resp = _session().get(METEO_URL, params=params, timeout=HTTP_TIMEOUT)
        except Exception as exc:                      # noqa: BLE001 - transport error
            STATS["total_ms"] += (time.perf_counter() - t0) * 1000.0
            last_exc = exc
            if attempt == 0:
                STATS["retries"] += 1
                time.sleep(METEO_BACKOFF_S)
                continue
            break
        STATS["total_ms"] += (time.perf_counter() - t0) * 1000.0

        if resp.status_code in (429, 503):
            # Explicit backpressure -> fail fast, do NOT retry in a loop.
            STATS["rate_limited"] += 1
            raise RuntimeError(f"Open-Meteo HTTP {resp.status_code} (rate limited)")
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"Open-Meteo failed after retry: {last_exc}")


def _meteo_raw(current: dict) -> float:
    """Map an Open-Meteo ``current`` block to the 1.0..MAX penalty scale."""
    code = int(current.get("weather_code", 0) or 0)
    excess = WMO_CODE_EXCESS.get(code, 0.1)
    wind_ms = float(current.get("wind_speed_10m", 0.0) or 0.0) / 3.6
    gust_ms = float(current.get("wind_gusts_10m", 0.0) or 0.0) / 3.6
    return min(MAX_PENALTY, 1.0 + excess + _wind_excess(wind_ms, gust_ms))


def _fetch_weather_meteo(lat: float, lon: float) -> float:
    """Single-location Open-Meteo fetch (used by the OWM fallback path)."""
    resp = _meteo_get({"latitude": lat, "longitude": lon, "current": _METEO_CURRENT})
    data = resp.json()
    if isinstance(data, list):
        data = data[0] if data else {}
    return _meteo_raw((data or {}).get("current") or {})


def _fetch_weather_meteo_batch(coords) -> list:
    """Batched fetch: ONE request serves up to METEO_BATCH_SIZE cells.

    Open-Meteo accepts comma-separated latitude/longitude arrays and answers
    with a JSON array — cutting request volume ~15x, which together with
    :func:`_meteo_pace` and the backoff in :func:`_meteo_get` keeps the free
    tier out of 429 trouble.  Returns one raw penalty (or None) per coordinate.
    """
    if not coords:
        return []
    if len(coords) == 1:
        lat, lon = coords[0]
        try:
            return [_fetch_weather_meteo(lat, lon)]
        except Exception:                             # noqa: BLE001
            return [None]
    params = {
        "latitude": ",".join(f"{la:.4f}" for la, _ in coords),
        "longitude": ",".join(f"{lo:.4f}" for _, lo in coords),
        "current": _METEO_CURRENT,
    }
    try:
        data = _meteo_get(params).json()
    except Exception:                                 # noqa: BLE001
        return [None] * len(coords)
    if isinstance(data, dict):
        data = [data]
    values = [_meteo_raw((item or {}).get("current") or {}) if item else None
              for item in data]
    values += [None] * (len(coords) - len(values))     # pad partial responses
    return values[:len(coords)]


def _simulate_weather(lat: float, lon: float) -> float:
    """Deterministic synthetic weather for the grid cell (offline fallback)."""
    rng = _cell_rng(_cell(lat, lon), "wx")
    roll = rng.random()
    if roll < 0.06:
        excess = WEATHER_CONDITION_EXCESS["Thunderstorm"]
    elif roll < 0.14:
        excess = WEATHER_CONDITION_EXCESS["Rain"]
    elif roll < 0.19:
        excess = WEATHER_CONDITION_EXCESS["Snow"]
    elif roll < 0.27:
        excess = WEATHER_CONDITION_EXCESS["Mist"]
    elif roll < 0.55:
        excess = WEATHER_CONDITION_EXCESS["Clouds"]
    else:
        excess = 0.0
    wind = rng.uniform(0.0, 24.0)
    gust = rng.uniform(0.0, 12.0) if roll < 0.14 else 0.0
    return min(MAX_PENALTY, 1.0 + excess + _wind_excess(wind, gust))


def get_live_weather_penalty(lat: float, lon: float, severity: float = 1.0,
                             use_live: bool = False) -> float:
    """Return the weather-resistance multiplier for a coordinate.

    Parameters
    ----------
    lat, lon : float
        Geographic coordinates of the airport / grid cell.
    severity : float
        Simulated storm intensity (0 = ignore weather, 3 = extreme).
    use_live : bool
        When True, read the live cache warmed by :func:`prefetch_live`
        (falling back to simulation for cells beyond the live budgets).
    """
    # The read side never touches the network: live cells are warmed up by
    # prefetch_live(); a miss simply derives the deterministic simulation.
    key = (_cell(lat, lon), bool(use_live))
    cached = _WX_CACHE.get(key)
    if cached is not None:
        STATS["cache_hits"] += 1
        raw, mode = cached
        if mode == "sim":
            STATS["weather_sim"] += 1
        else:
            STATS["weather_live"] += 1
    else:
        raw = _simulate_weather(lat, lon)
        _WX_CACHE[key] = (raw, "sim")
        STATS["weather_sim"] += 1
    return _scale(raw, severity, MAX_PENALTY)


# ---------------------------------------------------------------------------
# Airspace traffic — provider chain: OpenSky -> adsb.lol -> simulation
# ---------------------------------------------------------------------------
def _traffic_get(url: str, params: dict = None):
    """GET a traffic provider with one retry on transport errors only.

    HTTP 429/503 is terminal (fail fast) — the chain falls through to the
    next provider and the cell degrades to simulation instead of stalling
    the prefetch for seconds per cell.
    """
    last_exc = None
    for attempt in range(2):
        if attempt:
            STATS["retries"] += 1
            time.sleep(METEO_BACKOFF_S)          # generic backoff constant
        STATS["requests"] += 1
        t0 = time.perf_counter()
        try:
            resp = _session().get(url, params=params, timeout=HTTP_TIMEOUT)
        except Exception as exc:                 # noqa: BLE001
            STATS["total_ms"] += (time.perf_counter() - t0) * 1000.0
            last_exc = exc
            continue
        STATS["total_ms"] += (time.perf_counter() - t0) * 1000.0
        if resp.status_code in (429, 503):
            STATS["rate_limited"] += 1
            raise RuntimeError(f"traffic provider HTTP {resp.status_code} (rate limited)")
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"traffic provider failed after retry: {last_exc}")


def _fetch_traffic(lat: float, lon: float) -> float:
    """OpenSky anonymous states feed over the cell's bounding box (paced)."""
    time.sleep(TRAFFIC_PACE_S)
    half = GRID_CELL_DEG
    resp = _traffic_get(
        OPENSKY_URL,
        params={
            "lamin": max(-90.0, lat - half), "lamax": min(90.0, lat + half),
            "lomin": max(-180.0, lon - half), "lomax": min(180.0, lon + half),
        },
    )
    data = resp.json()
    count = len(data.get("states") or [])
    return min(MAX_TRAFFIC_PENALTY, 1.0 + count / 120.0)


def _fetch_traffic_adsb(lat: float, lon: float) -> float:
    """Keyless adsb.lol ADS-B snapshot within 250 NM of the cell centre."""
    time.sleep(TRAFFIC_PACE_S)
    resp = _traffic_get(ADSB_URL.format(lat=f"{lat:.3f}", lon=f"{lon:.3f}"))
    data = resp.json() or {}
    count = len(data.get("ac") or [])
    return min(MAX_TRAFFIC_PENALTY, 1.0 + count / 120.0)


def _resolve_traffic(lat: float, lon: float):
    """Run the traffic provider chain for one cell; (raw, provider, error)."""
    errors, attempted = [], False
    if _claim("sky"):
        attempted = True
        try:
            raw = _fetch_traffic(lat, lon)
            _record_provider("OpenSky")
            return raw, "OpenSky", ""
        except Exception as exc:      # noqa: BLE001 - any transport/HTTP error
            errors.append(f"OpenSky: {exc}")
    if _claim("adsb"):
        attempted = True
        try:
            raw = _fetch_traffic_adsb(lat, lon)
            _record_provider("adsb.lol")
            return raw, "adsb.lol", ""
        except Exception as exc:
            errors.append(f"adsb.lol: {exc}")
    if not attempted:
        return None, None, ""         # budgets exhausted -> plain simulation
    return None, None, "; ".join(errors)


def _simulate_traffic(lat: float, lon: float) -> float:
    """Synthetic sector traffic: baseline density + hub proximity falloff."""
    rng = _cell_rng(_cell(lat, lon), "trf")
    count = rng.uniform(5.0, 55.0)
    cos_lat = max(0.2, math.cos(math.radians(lat)))
    for hub_lat, hub_lon in _MAJOR_HUBS:
        d = math.hypot((hub_lat - lat) * 111.0, (hub_lon - lon) * 111.0 * cos_lat)
        if d < 900.0:
            count += 210.0 * math.exp(-d / 350.0)
    return min(MAX_TRAFFIC_PENALTY, 1.0 + min(count, 450.0) / 150.0)


def get_traffic_penalty(lat: float, lon: float, severity: float = 1.0,
                        use_live: bool = False) -> float:
    """Return the airspace-congestion multiplier for a coordinate.

    Reads the live cache warmed by :func:`prefetch_live` (OpenSky / adsb.lol
    cells) and falls back to the deterministic hub-weighted simulation for
    cells beyond the live budgets.  Never blocks on the network.
    """
    key = (_cell(lat, lon), bool(use_live))
    cached = _TRF_CACHE.get(key)
    if cached is not None:
        STATS["cache_hits"] += 1
        raw, mode = cached
        if mode == "sim":
            STATS["traffic_sim"] += 1
        else:
            STATS["traffic_live"] += 1
    else:
        raw = _simulate_traffic(lat, lon)
        _TRF_CACHE[key] = (raw, "sim")
        STATS["traffic_sim"] += 1
    return _scale(raw, severity, MAX_TRAFFIC_PENALTY)


# ---------------------------------------------------------------------------
# Parallel prefetch — the only place that performs network I/O
# ---------------------------------------------------------------------------
def _wx_needs_fetch(cell, now: float) -> bool:
    cached = _WX_CACHE.get((cell, True))
    if cached is None:
        return True                    # never attempted
    if cached[1] != "sim":
        return False                   # already live
    fail_ts = _WX_FAIL.get(cell)
    if fail_ts is None:
        return True                    # budget-excluded earlier -> retry now
    if now - fail_ts >= LIVE_RETRY_TTL_S:
        _WX_FAIL.pop(cell, None)
        return True                    # total failure -> retry after the TTL
    return False                       # recently failed -> hold off


def _trf_needs_fetch(cell, now: float) -> bool:
    cached = _TRF_CACHE.get((cell, True))
    if cached is None:
        return True
    if cached[1] != "sim":
        return False
    fail_ts = _TRF_FAIL.get(cell)
    if fail_ts is None:
        return True
    if now - fail_ts >= LIVE_RETRY_TTL_S:
        _TRF_FAIL.pop(cell, None)
        return True
    return False


def _wx_commit(cell, raw, provider, error, lat, lon) -> bool:
    """Write one weather cell's result into the live cache; True when live."""
    with _BUDGET_LOCK:
        if raw is None:
            _WX_CACHE[(cell, True)] = (_simulate_weather(lat, lon), "sim")
            _WX_FAIL[cell] = time.monotonic()
            STATS["weather_failed"] += 1
            if error:
                STATS["last_error"] = error
        else:
            _WX_CACHE[(cell, True)] = (raw, provider)
            _WX_FAIL.pop(cell, None)
    return raw is not None


def _job_weather_owm(lat: float, lon: float):
    """OpenWeatherMap cell (OWM claim pre-spent at scheduling time).

    Falls back to a single keyless Open-Meteo request when OWM fails.
    Returns ``(live_cells, attempted_cells)`` for the prefetch counters.
    """
    errors, raw, provider = [], None, None
    try:
        raw = _fetch_weather(lat, lon)
        provider = "OpenWeatherMap"
    except Exception as exc:                         # noqa: BLE001
        errors.append(f"OpenWeatherMap: {exc}")
        if _claim("meteo"):
            try:
                raw = _fetch_weather_meteo(lat, lon)
                provider = "Open-Meteo"
            except Exception as exc2:                # noqa: BLE001
                errors.append(f"Open-Meteo: {exc2}")
    error = "; ".join(errors) if raw is None else ""
    live = _wx_commit(_cell(lat, lon), raw, provider, error, lat, lon)
    if provider:
        _record_provider(provider)
    return (1, 1) if live else (0, 1)


def _job_weather_meteo_chunk(coords):
    """One batched Open-Meteo request covering a whole chunk of cells."""
    values = _fetch_weather_meteo_batch(coords)
    live = failed = 0
    with _BUDGET_LOCK:
        for (lat, lon), raw in zip(coords, values):
            cell = _cell(lat, lon)
            if raw is None:
                _WX_CACHE[(cell, True)] = (_simulate_weather(lat, lon), "sim")
                _WX_FAIL[cell] = time.monotonic()
                failed += 1
            else:
                _WX_CACHE[(cell, True)] = (raw, "Open-Meteo")
                _WX_FAIL.pop(cell, None)
                STATS["providers"]["Open-Meteo"] = STATS["providers"].get("Open-Meteo", 0) + 1
                live += 1
        if failed:
            STATS["weather_failed"] += failed
            if live == 0:
                STATS["last_error"] = "Open-Meteo: batch fetch failed (cells simulated)"
    return live, len(coords)


def _job_traffic(lat: float, lon: float) -> bool:
    """Fetch one cell through the traffic chain; True when it ended up live."""
    cell = _cell(lat, lon)
    raw, provider, error = _resolve_traffic(lat, lon)
    with _BUDGET_LOCK:
        if raw is None:
            _TRF_CACHE[(cell, True)] = (_simulate_traffic(lat, lon), "sim")
            if error:
                _TRF_FAIL[cell] = time.monotonic()
                STATS["traffic_failed"] += 1
                STATS["last_error"] = error
        else:
            _TRF_CACHE[(cell, True)] = (raw, provider)
            _TRF_FAIL.pop(cell, None)
    return raw is not None


def prefetch_live(points, priority=(), wx_budget=None, trf_budget=None) -> dict:
    """Warm the live weather/traffic caches for a set of coordinates.

    Parameters
    ----------
    points : iterable[(lat, lon)]
        Every coordinate that may be needed (e.g. all graph nodes, ordered
        busiest-first so hub cells receive live data before remote ones).
    priority : iterable[(lat, lon)]
        Cells attempted *first* — typically the corridor of the route being
        optimised, so the displayed itinerary is backed by live data.
    wx_budget, trf_budget : int, optional
        Maximum number of *cells* this call may attempt per domain; the
        process-wide provider budgets are enforced on top of this.

    Two pools run concurrently: ``LIVE_WORKERS`` weather workers and
    ``TRAFFIC_WORKERS`` traffic workers (each traffic request preceded by a
    polite ``TRAFFIC_PACE_S`` delay).  Open-Meteo cells are served in
    *batched* requests of ``METEO_BATCH_SIZE`` that are globally paced and
    retried with backoff on 429.  Returns per-call counters:
    ``{"weather": live_cells, "traffic": live_cells, "simulated": cells}``.
    """
    order, seen = [], set()
    for lat, lon in list(priority) + list(points):
        cell = _cell(lat, lon)
        if cell not in seen:
            seen.add(cell)
            order.append((lat, lon))

    now = time.monotonic()
    wx_owm, wx_meteo, trf_jobs = [], [], []
    wx_slots, wx_full = 0, False
    for lat, lon in order:
        cell = _cell(lat, lon)

        # --- weather lane: OWM claim first, else (batched) Open-Meteo ------
        if not wx_full:
            if wx_budget is not None and wx_slots >= wx_budget:
                wx_full = True
            elif _wx_needs_fetch(cell, now):
                if OPENWEATHER_API_KEY and _claim("owm"):
                    wx_owm.append((lat, lon))
                    wx_slots += 1
                elif _claim("meteo"):
                    wx_meteo.append((lat, lon))         # served in batches below
                    wx_slots += 1
                else:
                    wx_full = True                      # provider budgets spent

        # --- traffic lane --------------------------------------------------
        if ((trf_budget is None or len(trf_jobs) < trf_budget)
                and _budget_left("trf") and _trf_needs_fetch(cell, now)):
            trf_jobs.append((lat, lon))

    # Group meteo cells into batched requests (~15 cells -> 1 HTTP call).
    chunks = [wx_meteo[i:i + METEO_BATCH_SIZE]
              for i in range(0, len(wx_meteo), METEO_BATCH_SIZE)]

    results = {"weather": 0, "traffic": 0, "simulated": 0}
    if not wx_owm and not chunks and not trf_jobs:
        return results

    wx_pool = ThreadPoolExecutor(max_workers=LIVE_WORKERS)
    trf_pool = ThreadPoolExecutor(max_workers=TRAFFIC_WORKERS)
    try:
        # Submit both pools up-front so weather & traffic fetch concurrently.
        wx_futs = {}
        for lat, lon in wx_owm:
            wx_futs[wx_pool.submit(_job_weather_owm, lat, lon)] = 1
        for chunk in chunks:
            wx_futs[wx_pool.submit(_job_weather_meteo_chunk, chunk)] = len(chunk)
        trf_futs = {trf_pool.submit(_job_traffic, lat, lon): 1
                    for lat, lon in trf_jobs}

        for domain, futs in (("weather", wx_futs), ("traffic", trf_futs)):
            for fut, total in futs.items():
                try:
                    outcome = fut.result()
                except Exception as exc:               # noqa: BLE001
                    outcome = None
                    with _BUDGET_LOCK:
                        STATS["last_error"] = f"worker: {exc}"
                if domain == "weather":
                    # Weather jobs return (live_cells, attempted_cells).
                    live = outcome[0] if isinstance(outcome, tuple) else 0
                else:
                    live = 1 if outcome else 0
                results[domain] += live
                results["simulated"] += max(0, total - live)
    finally:
        wx_pool.shutdown(wait=False)
        trf_pool.shutdown(wait=False)
    return results