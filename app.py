"""Phase 4 — FlightScale Streamlit dashboard.

Run with:  streamlit run app.py
"""

import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from flight_engine.api_clients import (
    clear_api_caches,
    get_api_stats,
    get_cache_summary,
    reset_api_stats,
)
from flight_engine.config import (
    AIRPORTS_PATH,
    LATENCY_TARGET_MS,
    MODES,
    MODE_COLORS,
    OPENWEATHER_API_KEY,
    ROUTES_PATH,
)
from flight_engine.data_loader import load_airports, load_routes
from flight_engine.graph_engine import (
    LAST_RUN,
    find_optimal_path,
    find_reference_path,
    initialize_graph,
    path_stats,
    refine_live_weights,
    update_edge_weights,
)
from flight_engine.utils import AIRCRAFT, calculate_emissions, calculate_fuel_burn, flight_time_hours
from flight_engine.visualization import generate_map

st.set_page_config(
    page_title="FlightScale — Dynamic Flight Route Engine",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Backend pipeline
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading OpenFlights data & building the route graph…")
def build_graph():
    """Phase 1 cached across Streamlit re-runs: CSVs -> DataFrames -> NetworkX graph."""
    airports_df = load_airports(AIRPORTS_PATH)
    routes_df = load_routes(ROUTES_PATH)
    G = initialize_graph(airports_df, routes_df)
    return G


def compute_route(G, origin, dest, mode, weather_severity, traffic_severity,
                  use_live, aircraft) -> dict:
    """Phases 2+3: refresh dynamic weights, run A* and evaluate both routes."""
    reset_api_stats()

    if use_live:
        # Realtime mode: instant simulate-first corridor hint, then prefetch
        # live cells (parallel provider chains) and refine — see
        # graph_engine.refine_live_weights.
        path, weight_ms, refinements = refine_live_weights(
            G, origin, dest, mode=mode,
            weather_severity=weather_severity,
            traffic_severity=traffic_severity,
        )
    else:
        weight_ms = update_edge_weights(
            G, mode=mode,
            weather_severity=weather_severity,
            traffic_severity=traffic_severity,
            use_live=False,
        )
        path = find_optimal_path(G, origin, dest)
        refinements = 0
    astar_ms = LAST_RUN["astar_ms"]

    result = {
        "mode": mode,
        "origin": origin,
        "dest": dest,
        "aircraft": aircraft,
        "path": path,
        "refinements": refinements,
        "weight_ms": weight_ms,
        "astar_ms": astar_ms,
        "total_ms": weight_ms + astar_ms,
        "api": get_api_stats(),
        "stormy_edges": LAST_RUN.get("stormy_edges", 0),
        "congested_edges": LAST_RUN.get("congested_edges", 0),
        "nodes": LAST_RUN.get("nodes", G.number_of_nodes()),
        "edges": LAST_RUN.get("edges", G.number_of_edges()),
    }
    if not path:
        return result

    # --- dynamically optimized route -------------------------------------
    dyn = path_stats(G, path)
    dyn_fuel = calculate_fuel_burn(dyn["weighted_cost"], aircraft)
    result.update({
        "dynamic": dyn,
        "dyn_fuel_kg": dyn_fuel,
        "dyn_co2e_kg": calculate_emissions(dyn_fuel),
        "dyn_hours": flight_time_hours(dyn["weighted_cost"], aircraft, dyn["hops"]),
    })

    # --- static, shortest-distance baseline (optimality gap) --------------
    ref_path = find_reference_path(G, origin, dest)
    if ref_path:
        ref = path_stats(G, ref_path)
        ref_fuel = calculate_fuel_burn(ref["weighted_cost"], aircraft)
        ref_hours = flight_time_hours(ref["weighted_cost"], aircraft, ref["hops"])
        result.update({
            "static": ref,
            "static_path": ref_path,
            "static_fuel_kg": ref_fuel,
            "static_co2e_kg": calculate_emissions(ref_fuel),
            "static_hours": ref_hours,
            "fuel_saved_pct": 100.0 * (ref_fuel - dyn_fuel) / ref_fuel if ref_fuel else 0.0,
            "time_saved_pct": 100.0 * (ref_hours - result["dyn_hours"]) / ref_hours if ref_hours else 0.0,
            "km_delta_pct": 100.0 * (ref["base_km"] - dyn["base_km"]) / ref["base_km"] if ref["base_km"] else 0.0,
        })
    return result


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def render_dashboard():
    G = build_graph()

    all_iata = sorted(G.nodes)
    city_of = lambda i: G.nodes[i].get("city") or G.nodes[i].get("name") or i
    labels = {f"{iata} — {city_of(iata)} ({iata})": iata for iata in all_iata}
    label_list = list(labels)

    default_origin = next((l for l, v in labels.items() if v == "JFK"), label_list[0])
    default_dest = next((l for l, v in labels.items() if v == "SIN"), label_list[-1])

    # ------------------------- Sidebar -----------------------------------
    with st.sidebar:
        st.markdown("## ✈️ FlightScale")
        st.caption("Dynamic Flight Route Engine")

        origin_label = st.selectbox("Origin airport", label_list,
                                    index=label_list.index(default_origin))
        dest_label = st.selectbox("Destination airport", label_list,
                                  index=label_list.index(default_dest))
        origin, dest = labels[origin_label], labels[dest_label]

        st.divider()
        st.markdown("### 🧭 Routing priority")
        mode = st.radio("Objective", MODES, index=0,
                        help="Selects how weather and traffic penalties are blended into each edge weight.")
        st.caption(f"Edge color: :small[{MODE_COLORS[mode]}]")

        st.divider()
        st.markdown("### ⛈️ Simulated constraints")
        weather_severity = st.slider("Storm severity", 0.0, 3.0, 1.0, 0.1,
                                     help="0 = ignore weather, 1 = nominal, 3 = extreme storm cells.")
        traffic_severity = st.slider("Airspace congestion", 0.0, 3.0, 1.0, 0.1,
                                     help="0 = ignore traffic, 1 = nominal, 3 = saturated sectors.")

        st.divider()
        st.markdown("### 🛰️ Data source")
        use_live = st.toggle("Use live APIs", value=False,
                             help="Queries OpenWeatherMap / OpenSky over a cached geographic grid. "
                                  "Falls back to deterministic simulation on failure or budget exhaustion.")
        if use_live:
            if OPENWEATHER_API_KEY:
                st.caption("Live chain: OpenWeatherMap → Open-Meteo · "
                           "OpenSky → adsb.lol — first Compute warms the "
                           "live cache (~5–10 s), later runs are instant.")
            else:
                st.info("OPENWEATHER_API_KEY not set — weather uses the "
                        "keyless Open-Meteo feed; traffic uses "
                        "OpenSky → adsb.lol.")

        aircraft = st.selectbox("Aircraft type", list(AIRCRAFT), index=0)
        show_network = st.toggle("Show background network", value=False)

        st.divider()
        compute_clicked = st.button("Compute optimal route", type="primary", width="stretch")
        if use_live and st.button(
                "↻ Refresh live data",
                help="Clears the cached live cells and re-fetches realtime "
                     "weather & traffic on the next Compute."):
            clear_api_caches()
            st.session_state.pop("route_result", None)
            st.info("Live caches cleared — press **Compute optimal route** "
                    "to fetch fresh realtime data.")

        st.divider()
        st.caption(f"Graph: {G.number_of_nodes():,} airports · {G.number_of_edges():,} routes")
        st.caption(f"Latency target: < {LATENCY_TARGET_MS} ms")

    # ------------------------- Main area ---------------------------------
    st.title("✈️ FlightScale — Dynamic Flight Route Engine")
    st.caption(
        "A* search with a Haversine geographic heuristic over a dynamically weighted NetworkX graph — "
        "live weather and airspace congestion multiply each edge's baseline Haversine distance, "
        "so the optimizer routes around storms and saturated sectors."
    )

    if compute_clicked:
        if origin == dest:
            st.error("Origin and destination must be different airports.")
        else:
            spinner_label = (
                "Fetching realtime weather & traffic, refining route with A* "
                "(first run ~5–10 s, cached afterwards)…"
                if use_live else
                "Updating edge weights & running A* pathfinding…"
            )
            with st.spinner(spinner_label):
                st.session_state["route_result"] = compute_route(
                    G, origin, dest, mode, weather_severity, traffic_severity,
                    use_live, aircraft,
                )

    result = st.session_state.get("route_result")
    if result is None:
        st.info("Configure your route in the sidebar and press **Compute optimal route**.")
        _render_explainer()
        return

    if not result["path"]:
        st.error(f"No valid route found between {result['origin']} and {result['dest']} in the "
                 f"active network. Try another pair.")
        return

    dyn = result["dynamic"]
    origin_attrs, dest_attrs = G.nodes[result["origin"]], G.nodes[result["dest"]]

    st.subheader(f"{result['origin']} ({origin_attrs.get('city', '')})  →  "
                 f"{result['dest']} ({dest_attrs.get('city', '')}) · {result['mode']}")

    # --------------------- Evaluation metrics (§6) ------------------------
    within_target = result["astar_ms"] < LATENCY_TARGET_MS
    m1 = st.columns(4)
    m1[0].metric("A* compute time", f"{result['astar_ms']:.1f} ms",
                 delta=f"{'✅' if within_target else '⚠️'} target < {LATENCY_TARGET_MS} ms",
                 delta_color="normal" if within_target else "inverse")
    m1[1].metric("Weighting pass", f"{result['weight_ms']:.1f} ms")
    m1[2].metric("Route distance", f"{dyn['base_km']:,.0f} km",
                 delta=f"{dyn['hops']} legs")
    m1[3].metric("Effective cost", f"{dyn['weighted_cost']:,.0f} p·km",
                 delta=f"wx {dyn['avg_weather_penalty']:.2f}× · trf {dyn['avg_traffic_penalty']:.2f}×")

    m2 = st.columns(4)
    m2[0].metric("Fuel burn", f"{result['dyn_fuel_kg']:,.0f} kg", delta=result["aircraft"])
    m2[1].metric("CO₂e emissions", f"{result['dyn_co2e_kg'] / 1000:.2f} t")
    m2[2].metric("Block time", f"{result['dyn_hours']:.1f} h")
    if "fuel_saved_pct" in result:
        m2[3].metric("Optimality gap (fuel saved)",
                     f"{result['fuel_saved_pct']:+.1f} %",
                     delta=f"vs static route ({result['time_saved_pct']:+.1f} % time)")

    # ----------------------------- Map -----------------------------------
    st.markdown("#### 🗺️ Route map")
    st.caption(
        "Solid coloured line = **dynamic optimal route** (A* over weather/traffic-"
        "weighted costs) · dark dashed line = **static shortest-distance route** "
        "(pure Haversine baseline that ignores conditions). The gap between the "
        "two is quantified in the comparison table below — use the map's ⚙ layer "
        "control to toggle them."
    )
    fmap = generate_map(G, result["path"], mode=result["mode"],
                        show_network=show_network,
                        reference_path=result.get("static_path"))
    # st_folium's own use_container_width flag (not a core-Streamlit widget)
    # stretches the map to the container width.
    st_folium(fmap, height=560, use_container_width=True, returned_objects=[])

    # ------------------------ Itinerary table -----------------------------
    legs_df = pd.DataFrame(dyn["legs"]).rename(columns={
        "leg": "#", "from": "From", "to": "To", "distance_km": "Distance (km)",
        "weather_penalty": "Weather ×", "traffic_penalty": "Traffic ×",
        "effective_cost": "Effective cost",
    })
    st.markdown("#### 🧾 Flight itinerary")
    st.dataframe(
        legs_df.style.format({
            "Distance (km)": "{:,.0f}",
            "Weather ×": "{:.2f}",
            "Traffic ×": "{:.2f}",
            "Effective cost": "{:,.0f}",
        }),
        width="stretch", hide_index=True,
    )

    # ------------------- Optimality-gap comparison ------------------------
    if "static" in result:
        static = result["static"]
        st.markdown("#### 📊 Optimality gap — dynamic vs. static shortest-distance route")
        compare = pd.DataFrame([
            {
                "Route": "Dynamic (optimized)",
                "Legs": dyn["hops"],
                "Distance (km)": dyn["base_km"],
                "Effective cost": dyn["weighted_cost"],
                "Fuel (kg)": result["dyn_fuel_kg"],
                "CO₂e (kg)": result["dyn_co2e_kg"],
                "Block time (h)": result["dyn_hours"],
                "Path": " → ".join(result["path"]),
            },
            {
                "Route": "Static (shortest distance)",
                "Legs": static["hops"],
                "Distance (km)": static["base_km"],
                "Effective cost": static["weighted_cost"],
                "Fuel (kg)": result["static_fuel_kg"],
                "CO₂e (kg)": result["static_co2e_kg"],
                "Block time (h)": result["static_hours"],
                "Path": " → ".join(result["static_path"]),
            },
        ])
        st.dataframe(
            compare.style.format({
                "Distance (km)": "{:,.0f}",
                "Effective cost": "{:,.0f}",
                "Fuel (kg)": "{:,.0f}",
                "CO₂e (kg)": "{:,.0f}",
                "Block time (h)": "{:.1f}",
            }),
            width="stretch", hide_index=True,
        )

    # ---------------------- API resolution metrics ------------------------
    api = result["api"]
    with st.expander("📡 API resolution & network telemetry"):
        c = st.columns(4)
        c[0].metric("Live API calls", api["live_calls"])
        c[1].metric("Simulated lookups", api["simulated"])
        c[2].metric("Avg resolution", f"{api['avg_resolution_ms']:.0f} ms")
        c[3].metric("Grid-cache hits", f"{api['cache_hits']:,}")
        providers = api.get("providers") or {}
        if providers:
            st.caption(
                "Providers this run: "
                + " · ".join(f"{name} {count}" for name, count in providers.items())
                + f" · live refinements: {result.get('refinements', 0)}"
            )
        cov = get_cache_summary()
        if cov["wx_total"] or cov["trf_total"]:
            st.caption(
                f"Live cell coverage: weather {cov['wx_live']}/{cov['wx_total']} · "
                f"traffic {cov['trf_live']}/{cov['trf_total']}"
            )
        st.caption(
            f"Weather-failed requests: {api['weather_failed']} · "
            f"Traffic-failed requests: {api['traffic_failed']} · "
            f"Storm-affected legs in graph: {result['stormy_edges']:,} · "
            f"Congested legs: {result['congested_edges']:,} · "
            f"Graph: {result['nodes']:,} nodes / {result['edges']:,} edges"
        )
        if api.get("retries"):
            st.caption(
                f"Rate-limit handling: {api['retries']} backoff retries after "
                f"{api.get('rate_limited', 0)} HTTP 429/503 responses"
            )
        if api.get("last_error"):
            st.caption(f"Last provider error: {api['last_error']}")

    _render_explainer()


def _render_explainer():
    with st.expander("ℹ️ How the engine works"):
        st.markdown(
            """
            **1. Static baseline (Phase 1)** — OpenFlights airports (nodes) and routes (edges) are cleaned
            with Pandas; every edge stores its Haversine `base_distance`.

            **2. Live modifiers (Phase 2)** — weather (OpenWeatherMap) and airspace congestion (OpenSky)
            are resolved per 5° geographic grid cell and cached. Missing keys, failed requests or exhausted
            budgets fall back to a deterministic simulation so the pipeline never blocks.

            **3. Dynamic weighting (Phase 3)** — `current_weight = base × (1 + w(weather−1) + t(traffic−1))`
            where `w`/`t` depend on the selected priority:
            | Priority | Weather w | Traffic t |
            |---|---|---|
            | Eco-Mode | 0.9 | 0.8 |
            | Fastest Time | 0.5 | 1.2 |
            | Maximum Fuel Efficiency | 1.2 | 0.6 |

            **4. Pathfinding** — `nx.astar_path` with the straight-line Haversine heuristic. Since penalties
            only ever inflate edge costs, the heuristic stays admissible: A* returns the *provably optimal*
            route for the chosen objective in O(E log V), unlike MST algorithms (which connect all nodes)
            or Dijkstra (which expands blindly in every direction).
            """
        )


if __name__ == "__main__":
    render_dashboard()