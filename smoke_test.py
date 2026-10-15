"""Headless end-to-end verification of the Dynamic Flight Route Engine pipeline.

Run:  python smoke_test.py
"""

import sys
import time

from flight_engine import (
    AIRPORTS_PATH,
    ROUTES_PATH,
    calculate_emissions,
    calculate_fuel_burn,
    find_optimal_path,
    find_reference_path,
    generate_map,
    get_api_stats,
    haversine,
    initialize_graph,
    load_airports,
    load_routes,
    path_stats,
    reset_api_stats,
    update_edge_weights,
)
from flight_engine.config import LATENCY_TARGET_MS, MODES
from flight_engine.graph_engine import LAST_RUN


def main() -> int:
    failures = []

    # --- utility math -----------------------------------------------------
    d = haversine(-73.7781, 40.6413, 103.9944, 1.3502)   # JFK -> SIN
    assert 15_000 < d < 16_000, f"unexpected JFK-SIN haversine: {d}"
    fuel = calculate_fuel_burn(10_000, "Airbus A320neo")
    co2 = calculate_emissions(fuel)
    assert fuel > 0 and co2 > fuel, "fuel/emissions invariants violated"
    print(f"[OK] haversine JFK-SIN = {d:,.0f} km; 10,000 p·km fuel = {fuel:,.0f} kg, CO2e = {co2:,.0f} kg")

    # --- Phase 1: ingestion + graph --------------------------------------
    t0 = time.perf_counter()
    airports = load_airports(AIRPORTS_PATH)
    routes = load_routes(ROUTES_PATH)
    G = initialize_graph(airports, routes)
    load_ms = (time.perf_counter() - t0) * 1000
    print(f"[OK] cleaned data: {len(airports):,} airports / {len(routes):,} routes "
          f"-> graph {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges ({load_ms:,.0f} ms)")
    if G.number_of_nodes() < 1_000:
        failures.append(f"graph too small: {G.number_of_nodes()} nodes")

    origin, dest = "JFK", "SIN"
    if origin not in G or dest not in G:
        origin, dest = "LHR", "SIN"
    if origin not in G or dest not in G:
        failures.append("neither JFK/SIN nor LHR/SIN present in graph")

    if not failures:
        # --- Phases 2+3: dynamic weighting + A* per mode -------------------
        for mode in MODES:
            reset_api_stats()
            weight_ms = update_edge_weights(G, mode=mode,
                                            weather_severity=1.5,
                                            traffic_severity=1.0,
                                            use_live=False)
            path = find_optimal_path(G, origin, dest)
            astar_ms = LAST_RUN["astar_ms"]

            if not path:
                failures.append(f"{mode}: no path {origin}->{dest}")
                continue
            if path[0] != origin or path[-1] != dest:
                failures.append(f"{mode}: path endpoints wrong -> {path[:3]}..{path[-3:]}")
            if astar_ms >= LATENCY_TARGET_MS:
                failures.append(f"{mode}: A* latency {astar_ms:.0f} ms >= {LATENCY_TARGET_MS} ms target")

            dyn = path_stats(G, path)
            ref_path = find_reference_path(G, origin, dest)
            ref = path_stats(G, ref_path)
            # optimality: dynamic effective cost <= static effective cost (+ tiny epsilon)
            if dyn["weighted_cost"] > ref["weighted_cost"] * 1.0001 + 1e-6:
                failures.append(f"{mode}: dynamic cost {dyn['weighted_cost']:,.0f} > "
                                f"static cost {ref['weighted_cost']:,.0f}")

            # every dynamically weighted edge must remain >= its base distance
            bad = sum(1 for _u, _v, e in G.edges(data=True)
                      if e["current_weight"] + 1e-9 < e["base_distance"])
            if bad:
                failures.append(f"{mode}: {bad} edges violate admissibility (weight < base)")

            api = get_api_stats()
            print(f"[{'OK' if not failures else '!!'}] {mode:<24} {origin}->{dest}: "
                  f"{len(path)} nodes, {dyn['base_km']:,.0f} km, "
                  f"cost {dyn['weighted_cost']:,.0f}, A* {astar_ms:.1f} ms, "
                  f"weighting {weight_ms:.1f} ms, sim lookups {api['simulated']}, "
                  f"fuel {calculate_fuel_burn(dyn['weighted_cost']):,.0f} kg")

        # --- Phase 4: map generation (both comparison routes) --------------
        reset_api_stats()
        update_edge_weights(G, mode=MODES[0], use_live=False)
        path = find_optimal_path(G, origin, dest)
        ref_path = find_reference_path(G, origin, dest)
        fmap = generate_map(G, path, mode=MODES[0], show_network=True,
                            network_sample=300, reference_path=ref_path)
        html = fmap.get_root().render()
        if len(html) < 10_000 or "polyline" not in html.lower():
            failures.append("generated Folium map looks invalid")
        if "Static shortest distance" not in html:
            failures.append("static shortest-distance baseline missing from map")
        if "dashArray" not in html:
            failures.append("dashed baseline styling missing from map")
        if "Dynamic optimal" not in html:
            failures.append("dynamic route tooltip missing from map")
        print(f"[OK] Folium map rendered: {len(html):,} bytes HTML, "
              f"{len(path)} route markers + static baseline (dashed)")

        # --- latency recorded on graph > 10k-node target context ----------
        print(f"[OK] graph telemetry: {LAST_RUN['nodes']:,} nodes, {LAST_RUN['edges']:,} edges, "
              f"storm-affected legs {LAST_RUN.get('stormy_edges', 0):,}, "
              f"congested legs {LAST_RUN.get('congested_edges', 0):,}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())