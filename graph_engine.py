"""Phase 1 & 3 — NetworkX graph construction, dynamic weighting and A* search.

Algorithm choice
----------------
Minimum spanning trees (Kruskal/Prim) connect *all* nodes — irrelevant for a
single aircraft travelling A -> B — and Dijkstra expands uniformly in every
direction.  A* is guided by the Haversine straight-line heuristic, which is
**admissible** here because every dynamically weighted edge cost satisfies
``current_weight >= base_distance >= heuristic``.  The search therefore stays
O(E log V) and returns a provably optimal path under the selected objective.
"""

import functools
import time

import networkx as nx

from .api_clients import get_live_weather_penalty, get_traffic_penalty, prefetch_live
from .config import DEFAULT_MODE, LIVE_REFINEMENT_BUDGETS, MODE_WEIGHTS
from .utils import haversine

# Latency telemetry for the evaluation dashboard (Section 6 metrics).
LAST_RUN = {
    "weight_update_ms": 0.0,
    "astar_ms": 0.0,
    "nodes": 0,
    "edges": 0,
}


# ---------------------------------------------------------------------------
# Phase 1 — Graph initialization
# ---------------------------------------------------------------------------
def initialize_graph(airports_df, routes_df) -> nx.Graph:
    """Build the physical topology from the cleaned OpenFlights DataFrames.

    Airports become nodes carrying ``lat``/``lon``/``name``/``city`` attributes;
    established routes become undirected edges whose ``base_distance`` is the
    Haversine great-circle distance in kilometres.
    """
    G = nx.Graph()

    coords = {}
    for row in airports_df.itertuples(index=False):
        lat, lon = float(row.lat), float(row.lon)
        G.add_node(row.iata, name=row.name, city=row.city,
                   country=row.country, lat=lat, lon=lon)
        coords[row.iata] = (lat, lon)

    endpoints = zip(routes_df["src_iata"].to_numpy(), routes_df["dst_iata"].to_numpy())
    for src, dst in endpoints:
        if src == dst or src not in coords or dst not in coords:
            continue
        lat1, lon1 = coords[src]
        lat2, lon2 = coords[dst]
        distance = haversine(lon1, lat1, lon2, lat2)
        if distance <= 0.0:
            continue
        if not G.has_edge(src, dst) or distance < G[src][dst]["base_distance"]:
            G.add_edge(src, dst, base_distance=distance)

    # Drop airports that ended up isolated (no valid commercial connection).
    G.remove_nodes_from(list(nx.isolates(G)))

    LAST_RUN["nodes"] = G.number_of_nodes()
    LAST_RUN["edges"] = G.number_of_edges()
    return G


# ---------------------------------------------------------------------------
# Phase 3 — Dynamic edge weighting (multi-objective mixer)
# ---------------------------------------------------------------------------
def update_edge_weights(G, mode: str = DEFAULT_MODE,
                        weather_severity: float = 1.0,
                        traffic_severity: float = 1.0,
                        use_live: bool = False) -> float:
    """Blend live API penalties into every edge and store ``current_weight``.

    ``current_weight = base_distance * (1 + w_coef*(weather-1) + t_coef*(traffic-1))``

    Penalties are resolved once per node (cached per geographic grid cell by
    the API module) and averaged across each edge.  Because both penalty
    coefficients are >= 0 and penalties are >= 1.0, the weighted cost can only
    inflate the base distance — keeping the A* heuristic admissible.
    """
    coeffs = MODE_WEIGHTS.get(mode, MODE_WEIGHTS[DEFAULT_MODE])
    w_coef, t_coef = coeffs["weather"], coeffs["traffic"]

    t0 = time.perf_counter()

    node_weather, node_traffic = {}, {}
    for node, attrs in G.nodes(data=True):
        lat, lon = attrs["lat"], attrs["lon"]
        node_weather[node] = get_live_weather_penalty(lat, lon, weather_severity, use_live)
        node_traffic[node] = get_traffic_penalty(lat, lon, traffic_severity, use_live)

    stormy = congested = 0
    for u, v, edge in G.edges(data=True):
        wp = 0.5 * (node_weather[u] + node_weather[v])
        tp = 0.5 * (node_traffic[u] + node_traffic[v])
        base = edge["base_distance"]
        edge["weather_penalty"] = wp
        edge["traffic_penalty"] = tp
        edge["current_weight"] = max(base * (1.0 + w_coef * (wp - 1.0) + t_coef * (tp - 1.0)), base)
        if wp >= 3.0:
            stormy += 1
        if tp >= 2.5:
            congested += 1

    LAST_RUN["weight_update_ms"] = (time.perf_counter() - t0) * 1000.0
    LAST_RUN["nodes"] = G.number_of_nodes()
    LAST_RUN["edges"] = G.number_of_edges()
    LAST_RUN["stormy_edges"] = stormy
    LAST_RUN["congested_edges"] = congested
    return LAST_RUN["weight_update_ms"]


def refine_live_weights(G, origin: str, dest: str, mode: str = DEFAULT_MODE,
                        weather_severity: float = 1.0,
                        traffic_severity: float = 1.0,
                        budgets=None):
    """Simulate-first, then progressively warm live API cells along the corridor.

    1. Weight every edge from the deterministic simulation (instant) and run
       A* to obtain a cheap *corridor hint*.
    2. :func:`prefetch_live` fetches realtime weather/traffic for the hint's
       cells first (``priority``) and the busiest remaining cells (hub order),
       bounded by the per-round ``budgets``.
    3. Re-weight with the live penalties and re-run A*; repeat while the route
       keeps changing (at most one pass per entry in ``budgets``).

    Because cells are cached for the process lifetime, only the first live
    routing pass of a session performs network I/O — subsequent passes are as
    fast as the pure simulation while still using live data.

    Returns ``(path, total_weight_ms, refinements)``.
    """
    budgets = budgets or LIVE_REFINEMENT_BUDGETS
    hub_points = [(G.nodes[n]["lat"], G.nodes[n]["lon"])
                  for n in sorted(G, key=G.degree, reverse=True)]

    weight_ms = update_edge_weights(G, mode, weather_severity, traffic_severity,
                                    use_live=False)
    path = find_optimal_path(G, origin, dest)
    refinements = 0
    if not path:
        return path, weight_ms, refinements

    for wx_budget, trf_budget in budgets:
        corridor = [(G.nodes[n]["lat"], G.nodes[n]["lon"]) for n in path]
        prefetch_live(hub_points, priority=corridor,
                      wx_budget=wx_budget, trf_budget=trf_budget)
        weight_ms += update_edge_weights(G, mode, weather_severity,
                                         traffic_severity, use_live=True)
        refinements += 1
        new_path = find_optimal_path(G, origin, dest)
        if new_path == path:
            break                      # route converged — skip extra rounds
        path = new_path
    return path, weight_ms, refinements


# ---------------------------------------------------------------------------
# Phase 3 — A* pathfinding
# ---------------------------------------------------------------------------
def heuristic_distance(node1, node2, graph: nx.Graph = None) -> float:
    """Admissible A* heuristic: remaining Haversine distance to the goal.

    Accepts either IATA codes (requires ``graph``) or raw coordinate
    dictionaries / (lat, lon) tuples.
    """
    if graph is not None:
        a, b = graph.nodes[node1], graph.nodes[node2]
    else:
        a, b = node1, node2
    lat1 = a["lat"] if isinstance(a, dict) else a[0]
    lon1 = a["lon"] if isinstance(a, dict) else a[1]
    lat2 = b["lat"] if isinstance(b, dict) else b[0]
    lon2 = b["lon"] if isinstance(b, dict) else b[1]
    return haversine(lon1, lat1, lon2, lat2)


def find_optimal_path(G, source: str, dest: str):
    """Execute ``nx.astar_path`` over ``current_weight``; returns IATA list.

    Returns an empty list when the airports are unknown or disconnected and
    raises ``ValueError`` when either endpoint is missing from the graph.
    """
    if source not in G:
        raise ValueError(f"Unknown origin airport: {source}")
    if dest not in G:
        raise ValueError(f"Unknown destination airport: {dest}")
    if source == dest:
        LAST_RUN["astar_ms"] = 0.0
        return [source]

    heuristic = functools.partial(heuristic_distance, graph=G)
    t0 = time.perf_counter()
    try:
        path = nx.astar_path(G, source, dest, heuristic=heuristic, weight="current_weight")
    except nx.NetworkXNoPath:
        path = []
    LAST_RUN["astar_ms"] = (time.perf_counter() - t0) * 1000.0
    return path


def find_reference_path(G, source: str, dest: str):
    """Static shortest-distance path (pure Haversine) used as the optimality-gap baseline."""
    if source == dest:
        return [source]
    heuristic = functools.partial(heuristic_distance, graph=G)
    try:
        return nx.astar_path(G, source, dest, heuristic=heuristic, weight="base_distance")
    except nx.NetworkXNoPath:
        return []


# ---------------------------------------------------------------------------
# Route evaluation helpers
# ---------------------------------------------------------------------------
def path_stats(G, path) -> dict:
    """Aggregate distance / cost metrics for an ordered list of IATA codes."""
    base_km = weighted_cost = 0.0
    weather_sum = traffic_sum = 0.0
    legs = []
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        edge = G[u][v]
        base = edge["base_distance"]
        wp = edge.get("weather_penalty", 1.0)
        tp = edge.get("traffic_penalty", 1.0)
        weight = edge.get("current_weight", base)
        base_km += base
        weighted_cost += weight
        weather_sum += wp
        traffic_sum += tp
        legs.append({
            "leg": i + 1,
            "from": u,
            "to": v,
            "distance_km": base,
            "weather_penalty": wp,
            "traffic_penalty": tp,
            "effective_cost": weight,
        })
    n_legs = max(1, len(legs))
    return {
        "path": path,
        "hops": len(legs),
        "base_km": base_km,
        "weighted_cost": weighted_cost,
        "avg_weather_penalty": weather_sum / n_legs,
        "avg_traffic_penalty": traffic_sum / n_legs,
        "legs": legs,
    }