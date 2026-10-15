"""Global configuration and tunable constants for the Dynamic Flight Route Engine."""

import os
from pathlib import Path
import dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
AIRPORTS_PATH = DATA_DIR / "airports.dat"   # OpenFlights airports dataset (nodes)
ROUTES_PATH = DATA_DIR / "routes.dat"       # OpenFlights routes dataset (edges)

# --- NEW: Explicitly load the .env file from the BASE_DIR ---
env_path = BASE_DIR / ".env"
dotenv.load_dotenv(dotenv_path=env_path)

# ---------------------------------------------------------------------------
# Live API credentials
# Live API credentials (optional — the engine degrades to deterministic
# simulation when a key is missing or a request fails).
# ---------------------------------------------------------------------------
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()
AVIATIONSTACK_API_KEY = os.getenv("AVIATIONSTACK_API_KEY", "").strip()
CARTO_API_KEY = os.getenv("CARTO_API_KEY", "").strip()
# ---------------------------------------------------------------------------
# Dynamic modifier knobs
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 4.0        # seconds before an external API call is abandoned
GRID_CELL_DEG = 5.0       # geographic cell size used to cache API penalties

# ---------------------------------------------------------------------------
# Realtime provider chains (Phase 2)
# ---------------------------------------------------------------------------
# Two-level budgeting keeps the dashboard responsive:
#   * per-round caps (LIVE_REFINEMENT_BUDGETS) limit how many *cells* a single
#     routing pass may attempt, and
#   * per-process caps limit how many calls each provider may receive in total
#     (free-tier friendly), after which cells fall back to simulation.
# Cells fetched once are cached for the process lifetime; "Refresh live data"
# in the dashboard clears the caches and refills these budgets.
OWM_BUDGET = 40           # OpenWeatherMap calls (free tier: 60/min, 1,000/day)
OPENMETEO_BUDGET = 400    # keyless Open-Meteo forecast *cells*
OPENSKY_BUDGET = 15       # OpenSky anonymous states calls (rate limited)
ADSB_BUDGET = 15          # keyless adsb.lol ADS-B queries
LIVE_WORKERS = 12         # parallel weather fetch workers
TRAFFIC_WORKERS = 6       # parallel traffic fetch workers
TRAFFIC_PACE_S = 0.3      # polite delay before every traffic request
LIVE_RETRY_TTL_S = 120.0  # re-attempt cells whose providers all failed after this
LIVE_REFINEMENT_BUDGETS = [(100, 12), (60, 12)]  # per round: (weather, traffic) cells

# Open-Meteo rate-limit protection (their free tier answers 429 under bursts):
# requests are batched (one call covers METEO_BATCH_SIZE cells), globally paced
# to METEO_PACE_S between calls and retried with backoff on 429/503.
METEO_BATCH_SIZE = 15     # cells per batched Open-Meteo request (~15x fewer calls)
METEO_PACE_S = 0.2        # global minimum interval between Open-Meteo requests
METEO_MAX_RETRIES = 2     # backoff retries per request on 429/503/network error
METEO_BACKOFF_S = 1.5     # base backoff delay (multiplied by attempt number)

# ---------------------------------------------------------------------------
# Multi-objective routing modes
# ---------------------------------------------------------------------------
DEFAULT_MODE = "Eco-Mode"
MODES = ["Eco-Mode", "Fastest Time", "Maximum Fuel Efficiency"]

# Each mode blends the weather / traffic penalty coefficients that are applied
# to an edge: current_weight = base * (1 + w_coef*(weather-1) + t_coef*(traffic-1))
MODE_WEIGHTS = {
    "Eco-Mode":               {"weather": 0.9, "traffic": 0.8},
    "Fastest Time":           {"weather": 0.5, "traffic": 1.2},
    "Maximum Fuel Efficiency": {"weather": 1.2, "traffic": 0.6},
}

MODE_COLORS = {
    "Eco-Mode": "#2E7D32",
    "Fastest Time": "#E65100",
    "Maximum Fuel Efficiency": "#1565C0",
}

# ---------------------------------------------------------------------------
# CARTO Voyager basemap (Leaflet raster tiles)
# ---------------------------------------------------------------------------
# Set CARTO_API_KEY (e.g. in .env) if you have a CARTO Maps API key:
#   https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=...
# Without a key the public keyless CDN is used (identical Voyager style):
#   https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png
# NOTE: CARTO_API_KEY is defined exactly ONCE — in the credentials block near
# the top of this file. Re-declaring it here (with os.getenv) would shadow the
# configured key with an empty string because the later assignment wins.
# Reference only — Folium renders raster tiles; the vector/MapLibre style JSON
# below is for GL-based viewers (MapLibre GL JS, deck.gl, etc.).
CARTO_GL_STYLE_URL = ("https://basemaps.cartocdn.com/gl/"
                      "voyager-gl-style/style.json")

# ---------------------------------------------------------------------------
# Evaluation targets (Section 6 of the requirement document)
# ---------------------------------------------------------------------------
LATENCY_TARGET_MS = 500   # A* compute-time target on graphs > 10,000 nodes