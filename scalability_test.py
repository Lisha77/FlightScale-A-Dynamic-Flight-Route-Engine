"""Scalability experiment: weighting & A* latency vs induced subgraph size.

Builds nested random induced subgraphs of the full OpenFlights graph and
measures the dynamic-weighting pass and A* search on each, supporting the
scalability analysis of the research paper.

Run:  python scalability_test.py
"""

import random
import sys
import time

import networkx as nx

from flight_engine import (
    AIRPORTS_PATH,
    ROUTES_PATH,
    initialize_graph,
    load_airports,
    load_routes,
)
from flight_engine.api_clients import clear_api_caches
from flight_engine.graph_engine import LAST_RUN, find_optimal_path, update_edge_weights
from flight_engine.utils import haversine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    G_full = initialize_graph(load_airports(AIRPORTS_PATH), load_routes(ROUTES_PATH))
    random.seed(42)
    order = list(G_full.nodes)
    random.shuffle(order)

    print(f"{'target':>7} {'|V|':>6} {'|E|':>7} {'weight_ms':>10} {'astar_ms':>9} {'hops':>5}")
    for target in (250, 500, 1000, 2000, 3257):
        G = G_full.subgraph(set(order[:target])).copy()
        G.remove_nodes_from(list(nx.isolates(G)))
        if G.number_of_nodes() < 50:
            print(f"{target:>7} skipped (graph too small after sampling)")
            continue
        # Restrict to the giant component so a route always exists.
        giant = max(nx.connected_components(G), key=len)
        G = G.subgraph(giant).copy()

        clear_api_caches()          # consistent cold-cache measurement
        t0 = time.perf_counter()
        update_edge_weights(G, use_live=False)
        weight_ms = (time.perf_counter() - t0) * 1000.0

        nodes = list(G)
        src = nodes[0]
        lat0, lon0 = G.nodes[src]["lat"], G.nodes[src]["lon"]
        dst = max(nodes, key=lambda n: haversine(lon0, lat0,
                                                 G.nodes[n]["lon"], G.nodes[n]["lat"]))
        path = find_optimal_path(G, src, dst)
        astar_ms = LAST_RUN["astar_ms"]
        print(f"{target:>7} {G.number_of_nodes():>6} {G.number_of_edges():>7} "
              f"{weight_ms:>10.1f} {astar_ms:>9.1f} {len(path):>5}")

    print("DONE — scalability sweep finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())