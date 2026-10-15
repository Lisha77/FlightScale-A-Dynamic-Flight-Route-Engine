"""Mathematical & utility functions: geodesy, fuel burn and emissions."""

import math

EARTH_RADIUS_KM = 6371.0088          # mean Earth radius (IUGG)
CO2E_PER_KG_FUEL = 3.16              # kg CO2e per kg of Jet-A1 (IATA factor)

# Aircraft performance coefficients: fuel is a function of *weighted* distance,
# divided by the airframe efficiency and scaled by its drag/specific
# consumption factor.  `kg_per_km` is measured at a representative payload.
AIRCRAFT = {
    "Airbus A320neo":  {"kg_per_km": 2.55, "cruise_kmh": 840, "efficiency": 1.00, "seats": 180},
    "Boeing 737-800":  {"kg_per_km": 2.85, "cruise_kmh": 830, "efficiency": 0.94, "seats": 189},
    "Airbus A350-900": {"kg_per_km": 5.90, "cruise_kmh": 900, "efficiency": 1.12, "seats": 315},
    "Boeing 787-9":    {"kg_per_km": 5.40, "cruise_kmh": 900, "efficiency": 1.15, "seats": 296},
    "Boeing 777-300ER": {"kg_per_km": 7.60, "cruise_kmh": 890, "efficiency": 0.97, "seats": 396},
}
DEFAULT_AIRCRAFT = "Airbus A320neo"


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in kilometres between two lon/lat points.

    Used both for baseline edge weights (Phase 1) and as the A* heuristic
    (Phase 3).  Admissible because every dynamically weighted edge cost is
    always >= its Haversine base distance.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def calculate_fuel_burn(distance: float, aircraft: str = DEFAULT_AIRCRAFT) -> float:
    """Estimated fuel consumption in kg for a *dynamically weighted* distance.

    Parameters
    ----------
    distance : float
        Weighted route distance in kilometres (base Haversine distance
        multiplied by the live weather / traffic penalty coefficients).
    aircraft : str
        Key of the :data:`AIRCRAFT` performance table.
    """
    cfg = AIRCRAFT.get(aircraft, AIRCRAFT[DEFAULT_AIRCRAFT])
    per_km = cfg["kg_per_km"] / cfg["efficiency"]
    taxi_reserve = 250.0                      # engine start + taxi fuel [kg]
    return distance * per_km + taxi_reserve


def calculate_emissions(fuel_burn_kg: float) -> float:
    """Convert raw fuel burn (kg) into CO2-equivalent emissions (kg CO2e)."""
    return fuel_burn_kg * CO2E_PER_KG_FUEL


def flight_time_hours(distance_km: float, aircraft: str = DEFAULT_AIRCRAFT, stops: int = 0) -> float:
    """Block time estimate: cruise time plus a fixed ground buffer per stop."""
    cfg = AIRCRAFT.get(aircraft, AIRCRAFT[DEFAULT_AIRCRAFT])
    return distance_km / cfg["cruise_kmh"] + 0.25 * stops


def fmt_km(value: float) -> str:
    return f"{value:,.0f} km"


def fmt_tonnes(kg: float) -> str:
    return f"{kg / 1000.0:,.2f} t"