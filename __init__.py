"""FlightScale — Dynamic Flight Route Engine package.

Modules
-------
config            Global configuration, paths, mode definitions and API keys.
utils             Mathematical utilities (Haversine, fuel burn, emissions).
data_loader       Pandas-based ingestion/cleaning of the OpenFlights datasets.
api_clients       Live weather / air-traffic penalty providers (with simulation fallback).
graph_engine      NetworkX graph construction, dynamic weighting and A* pathfinding.
visualization     Folium map generation for computed flight paths.
"""

from .config import AIRPORTS_PATH, MODES, ROUTES_PATH
from .utils import haversine, calculate_fuel_burn, calculate_emissions
from .data_loader import load_airports, load_routes
from .api_clients import (
    get_live_weather_penalty,
    get_traffic_penalty,
    get_api_stats,
    reset_api_stats,
    prefetch_live,
    clear_api_caches,
    get_cache_summary,
)
from .graph_engine import (
    initialize_graph,
    update_edge_weights,
    refine_live_weights,
    heuristic_distance,
    find_optimal_path,
    find_reference_path,
    path_stats,
)
from .visualization import generate_map

__all__ = [
    "AIRPORTS_PATH",
    "ROUTES_PATH",
    "MODES",
    "haversine",
    "calculate_fuel_burn",
    "calculate_emissions",
    "load_airports",
    "load_routes",
    "initialize_graph",
    "get_live_weather_penalty",
    "get_traffic_penalty",
    "get_api_stats",
    "reset_api_stats",
    "prefetch_live",
    "clear_api_caches",
    "get_cache_summary",
    "update_edge_weights",
    "refine_live_weights",
    "heuristic_distance",
    "find_optimal_path",
    "find_reference_path",
    "path_stats",
    "generate_map",
]