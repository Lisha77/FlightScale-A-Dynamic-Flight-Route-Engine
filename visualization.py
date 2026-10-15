"""Phase 4 — Folium map generation for computed flight paths."""

import folium

from .config import CARTO_API_KEY, MODE_COLORS, DEFAULT_MODE

# Standard CARTO attribution (required by their tile policy).
CARTO_ATTR = ('&copy; <a href="https://www.openstreetmap.org/copyright">'
              'OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>')


def carto_voyager_layer() -> "folium.TileLayer":
    """CARTO Voyager basemap as the default Leaflet tile layer.

    Uses the keyed Maps URL when ``CARTO_API_KEY`` is configured:

        https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=...

    otherwise the public keyless CDN (same Voyager style, subdomains a–d):

        https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png

    (The ``voyager-gl-style/style.json`` vector URL from the CARTO docs is a
    MapLibre-GL style and is not consumable by raster Leaflet/Folium; it is
    kept in config for reference.)
    """
    if CARTO_API_KEY:
        tiles = ("https://basemaps.cartocdn.com/rastertiles/voyager/"
                 f"{{z}}/{{x}}/{{y}}.png?key={CARTO_API_KEY}")
        return folium.TileLayer(tiles=tiles, attr=CARTO_ATTR,
                                name="CARTO Voyager", control=True)
    tiles = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
    return folium.TileLayer(tiles=tiles, attr=CARTO_ATTR, subdomains="abcd",
                            name="CARTO Voyager", control=True)


def generate_map(G, path_nodes, mode: str = DEFAULT_MODE, show_network: bool = False,
                 network_sample: int = 900, reference_path=None):
    """Render both comparison routes on an interactive Folium map.

    Draws the two route types from the optimality-gap analysis:

    * **Dynamic optimal route** — solid, mode-coloured polyline with origin
      (green), stopover (blue) and destination (red) markers.  Its edge costs
      are the weather/traffic-weighted ``current_weight`` values, so it may be
      longer in kilometres yet cheaper/faster under adverse conditions.
    * **Static shortest-distance baseline** — dark dashed polyline computed
      over the raw Haversine ``base_distance`` (ignores weather & traffic);
      it is the reference the optimality-gap metrics compare against.

    An optional sampled backdrop of the wider route network and a layer
    control (toggle every layer) sit on a CARTO Voyager basemap.
    """
    color = MODE_COLORS.get(mode, MODE_COLORS[DEFAULT_MODE])
    fmap = folium.Map(location=[25.0, 10.0], zoom_start=3, tiles=None,
                      control_scale=True)
    carto_voyager_layer().add_to(fmap)

    if show_network and G.number_of_edges() > 0:
        # Deterministic sample of the background network for context.
        net_group = folium.FeatureGroup(name="Background network", overlay=True)
        step = max(1, G.number_of_edges() // max(1, network_sample))
        sample = list(G.edges(data=True))[::step][:network_sample]
        for u, v, _data in sample:
            folium.PolyLine(
                locations=[(G.nodes[u]["lat"], G.nodes[u]["lon"]),
                           (G.nodes[v]["lat"], G.nodes[v]["lon"])],
                weight=0.6, color="#9E9E9E", opacity=0.35,
            ).add_to(net_group)
        net_group.add_to(fmap)

    # --- Comparison route 2: static shortest-distance baseline (dashed) ----
    if reference_path and len(reference_path) >= 2:
        ref_km = sum(G[reference_path[i]][reference_path[i + 1]]["base_distance"]
                     for i in range(len(reference_path) - 1))
        ref_group = folium.FeatureGroup(
            name="Static shortest-distance route (baseline)", overlay=True)
        folium.PolyLine(
            locations=[(G.nodes[n]["lat"], G.nodes[n]["lon"])
                       for n in reference_path],
            color="#37474F", weight=4, opacity=0.9, dash_array="10 10",
            tooltip=(f"Static shortest distance — {ref_km:,.0f} km, "
                     f"{len(reference_path) - 1} legs "
                     f"(ignores weather & traffic)"),
        ).add_to(ref_group)
        ref_group.add_to(fmap)

    if not path_nodes:
        folium.LayerControl(collapsed=False).add_to(fmap)
        return fmap

    coords = [(G.nodes[n]["lat"], G.nodes[n]["lon"]) for n in path_nodes]

    # --- Comparison route 1: dynamic optimal route (with markers) ---------
    dyn_group = folium.FeatureGroup(name="Dynamic optimal route", overlay=True)
    folium.PolyLine(
        locations=coords,
        color=color,
        weight=5,
        opacity=0.9,
        tooltip="Dynamic optimal — " + " → ".join(path_nodes),
    ).add_to(dyn_group)

    for idx, node in enumerate(path_nodes):
        attrs = G.nodes[node]
        if idx == 0:
            marker_color, icon, label = "green", "plane-departure", "Origin"
        elif idx == len(path_nodes) - 1:
            marker_color, icon, label = "red", "plane-arrival", "Destination"
        else:
            marker_color, icon, label = "blue", "plane", "Stopover"

        popup_html = (
            f"<b>{node}</b> — {attrs.get('city', '')}, {attrs.get('country', '')}<br>"
            f"<i>{attrs.get('name', '')}</i><br>"
            f"Lat {attrs['lat']:.3f}, Lon {attrs['lon']:.3f} · {label}"
        )
        folium.Marker(
            location=(attrs["lat"], attrs["lon"]),
            popup=folium.Popup(popup_html, max_width=320),
            tooltip=f"{node} · {label}",
            icon=folium.Icon(color=marker_color, icon=icon, prefix="fa"),
        ).add_to(dyn_group)
    dyn_group.add_to(fmap)

    # Frame the whole itinerary.
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    if len(coords) >= 2:
        fmap.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(40, 40))
    else:
        fmap.location = coords[0]
        fmap.zoom_start = 6

    folium.LayerControl(collapsed=False).add_to(fmap)
    return fmap