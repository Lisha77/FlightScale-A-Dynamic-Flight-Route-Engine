# ✈️ FlightScale — Dynamic Flight Route Engine

An event-driven, multi-objective flight-routing system that computes the most
efficient path between any two airports in the OpenFlights network. Instead of
static, distance-based routing it ingests live meteorological and air-traffic
data, multiplying each edge's geographic distance by penalty coefficients so the
optimizer routes around storms and saturated airspace — cutting fuel burn,
delays and CO₂e emissions.

## Quick start

```bash
pip install -r requirements.txt
python smoke_test.py        # headless end-to-end verification (offline)
python live_test.py         # realtime pipeline verification (needs internet)
streamlit run app.py        # interactive dashboard (http://localhost:8501)
```

Test suites:

```bash
python smoke_test.py      # graph, A*, weighting, map, metrics — fully offline
python ui_test.py         # dashboard renders & computes in simulation mode
python live_test.py       # live provider chains, cold/warm timing, admissibility
python ui_live_test.py    # dashboard with the "Use live APIs" toggle enabled
python scalability_test.py  # scaling sweep: weighting & A* vs graph size
```

**Research write-up:** `RESEARCH_PAPER.md` is the full academic report
(formal problem definition, admissibility theorem & proof, experiments,
scalability analysis, limitations, references). Regenerate the submittable
DOCX with:

```bash
pandoc RESEARCH_PAPER.md -o FlightScale_Research_Paper.docx --toc --toc-depth=2 --standalone
```

Optional environment variable for richer live weather:

```bash
set OPENWEATHER_API_KEY=your_key      # live wind vectors & storm cells
set CARTO_API_KEY=your_key            # optional CARTO Voyager tile key (keyless CDN works too)
```

Realtime works **without any key** via fallback chains:

| Domain | Primary | Fallback | Last resort |
| --- | --- | --- | --- |
| Weather | OpenWeatherMap (key) | Open-Meteo (keyless) | deterministic simulation |
| Airspace traffic | OpenSky (anonymous) | adsb.lol (keyless) | deterministic simulation |

Failed/rate-limited cells fail fast to the next fallback instead of stalling
on backoff loops, and are retried automatically after a 120 s cooldown,
and every provider is budgeted per process so the free tiers are never
exhausted. Open-Meteo specifically is **batched** (15 cells per request) and
globally paced (≥ 0.2 s between calls); an HTTP 429/503 degrades that batch
to simulation immediately rather than blocking the prefetch — roughly 15×
fewer requests than the naive one-call-per-cell loop, which is what
triggered its rate limit before. Results outside the live
budgets fall back to a **deterministic simulation** seeded by the 5°
geographic grid cell, so results are stable and the pipeline never blocks.

## Architecture

| Layer | Technology |
| --- | --- |
| Framework | Python 3.10+ / Streamlit |
| Graph processing | NetworkX (in-memory topology + `nx.astar_path`) |
| Data manipulation | Pandas (OpenFlights CSV ingestion & cleaning) |
| Geospatial rendering | Folium + streamlit-folium on **CARTO Voyager** basemap |
| Static baseline | `data/airports.dat` (nodes), `data/routes.dat` (edges) |
| Dynamic modifiers | OpenWeatherMap + Open-Meteo (weather), OpenSky + adsb.lol (traffic) |

```
flight_engine/
├── config.py         paths, mode coefficients, provider budgets
├── utils.py          haversine, calculate_fuel_burn, calculate_emissions
├── data_loader.py    Phase 1 — pandas cleaning of OpenFlights .dat files
├── api_clients.py    Phase 2 — parallel prefetch + weather/traffic chains
├── graph_engine.py   Phase 1/3 — initialize_graph, update_edge_weights, A*, refine_live_weights
└── visualization.py  Phase 4 — generate_map (Folium polylines & markers)
app.py                Phase 4 — render_dashboard (Streamlit main loop)
smoke_test.py         offline verification of every phase
ui_test.py            dashboard smoke test (simulation mode)
live_test.py          realtime verification (provider chains, cold/warm)
ui_live_test.py       dashboard smoke test with live mode enabled
```

## Realtime data path

Enabling **Use live APIs** in the dashboard runs a *simulate-first corridor
refinement* (`graph_engine.refine_live_weights`):

1. **Instant hint** — weight the whole graph from the deterministic
   simulation and run A* to get a corridor hint (~60 ms).
2. **Parallel prefetch** — `api_clients.prefetch_live` fetches realtime
   weather/traffic for the hint's grid cells *first* (priority) and the
   busiest remaining cells (hub order) in two concurrent thread pools
   (12 weather workers, 6 paced traffic workers), applying the fallback
   chains above. ~5–10 s on a cold cache, then every cell is cached for the
   session.
3. **Refine** — re-weight with live penalties and re-run A*; repeat once if
   the route changed (at most 2 rounds).

Telemetry in the dashboard's *API resolution* expander shows which providers
served this run, live-vs-simulated cell coverage, and the last provider error.
The **↻ Refresh live data** button clears all caches and budgets so the next
Compute fetches fresh realtime data.

## Algorithm

**A\* with a Haversine geographic heuristic**, not MST or plain Dijkstra:

* Kruskal/Prim connect *every* node with minimum wiring — irrelevant for one
  aircraft travelling A → B.
* Dijkstra expands uniformly in all directions, wasting expansion budget.
* A\* is guided by straight-line distance, keeping the search at **O(E log V)**
  and returning a provably optimal route.

**Admissibility guarantee:** `current_weight = base × (1 + w(weather−1) + t(traffic−1))`
with penalties ≥ 1.0, so weighted costs never fall below the Haversine base —
the heuristic can never overestimate and A* stays optimal.

| Priority | Weather weight `w` | Traffic weight `t` |
| --- | --- | --- |
| Eco-Mode | 0.9 | 0.8 |
| Fastest Time | 0.5 | 1.2 |
| Maximum Fuel Efficiency | 1.2 | 0.6 |

Penalty model: clear skies / empty sector = `1.0`, severe storm cell = `5.0`
(severity slider scales 0 → 3×), congestion grows with aircraft count in the
sector; results are averaged across each edge's two endpoint airports.

## Evaluation metrics (dashboard)

* **Algorithmic latency** — A* compute time in ms (target **< 500 ms**;
  measured ≈ 30–110 ms on the 3,257-node / 18,930-edge graph) plus the
  dynamic weighting pass time.
* **Optimality gap** — fuel/time saved by the dynamically optimized route vs.
  the static shortest-distance baseline, evaluated under identical weights.
* **API resolution time** — average/total fetch time, live vs simulated calls,
  and grid-cache hits.

## Data

* [OpenFlights airports dataset](https://openflights.org/data.html) → `data/airports.dat`
* [OpenFlights routes dataset](https://openflights.org/data.html) → `data/routes.dat`

Cleaning drops rows with missing IATA codes (`\N`), null/out-of-range
coordinates, duplicate registrations, self-loops and isolated airports.