"""Phase 1 — Pandas ingestion and cleaning of the static OpenFlights datasets."""

import pandas as pd

from .config import AIRPORTS_PATH, ROUTES_PATH

AIRPORT_COLUMNS = [
    "airport_id", "name", "city", "country", "iata", "icao",
    "lat", "lon", "altitude", "timezone", "dst", "tz_database", "type", "source",
]

ROUTE_COLUMNS = [
    "airline", "airline_id", "src_iata", "src_id", "dst_iata", "dst_id",
    "codeshare", "stops", "equipment",
]


def _read_dat(path, columns) -> pd.DataFrame:
    """Read a header-less OpenFlights .dat file, silently skipping malformed rows."""
    try:
        df = pd.read_csv(path, header=None, names=columns, on_bad_lines="skip",
                         low_memory=False, dtype=str)
    except TypeError:            # pandas < 1.3
        df = pd.read_csv(path, header=None, names=columns, low_memory=False, dtype=str)
    return df


def load_airports(path=AIRPORTS_PATH) -> pd.DataFrame:
    """Load airports and clean the physical topology nodes.

    Handles missing IATA codes (``\\N``), null / unparseable coordinates and
    duplicate IATA registrations (first occurrence wins).
    """
    df = _read_dat(path, AIRPORT_COLUMNS)

    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")

    df["iata"] = df["iata"].astype("string").str.strip().str.upper()
    for col in ("name", "city", "country"):
        df[col] = df[col].fillna("").astype(str).str.strip()

    valid_iata = df["iata"].notna() & (df["iata"].str.len() == 3) & (df["iata"] != "\\N")
    df = df[valid_iata & df["lat"].notna() & df["lon"].notna()]
    df = df[df["lat"].between(-90, 90) & df["lon"].between(-180, 180)]
    df = df[df["lat"].abs() + df["lon"].abs() > 0]           # drop (0, 0) null island

    df = df.drop_duplicates(subset="iata", keep="first").reset_index(drop=True)
    return df[["iata", "name", "city", "country", "lat", "lon"]]


def load_routes(path=ROUTES_PATH) -> pd.DataFrame:
    """Load established commercial connections and drop incomplete edges."""
    df = _read_dat(path, ROUTE_COLUMNS)

    for col in ("src_iata", "dst_iata"):
        df[col] = df[col].astype("string").str.strip().str.upper()
        valid = df[col].notna() & (df[col].str.len() == 3) & (df[col] != "\\N")
        df = df[valid]

    df = df[df["src_iata"] != df["dst_iata"]]                 # no self-loops
    df = df.drop_duplicates(subset=["src_iata", "dst_iata"]).reset_index(drop=True)
    return df[["src_iata", "dst_iata", "airline"]]


def load_dataset_stats(airports_df: pd.DataFrame, routes_df: pd.DataFrame) -> dict:
    """Summary statistics for the cleaned static baseline."""
    return {
        "airports": int(len(airports_df)),
        "routes": int(len(routes_df)),
    }