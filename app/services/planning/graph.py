"""Planning graph: a simple directed graph with one (cheapest) edge per node pair.

OSMnx returns a MultiDiGraph; parallel edges make incremental planners (D* Lite)
and cost overrides awkward, so we collapse to a DiGraph keeping the shortest
parallel edge and pre-computing per-edge attributes used by the cost models.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import networkx as nx
import numpy as np

from .geo import LatLon, haversine, haversine_np

# Default speed (km/h) by OSM road class for a small road-going delivery robot.
DEFAULT_SPEED_KMH: Dict[str, float] = {
    "motorway": 25.0, "motorway_link": 20.0, "trunk": 25.0, "trunk_link": 20.0,
    "primary": 20.0, "primary_link": 18.0, "secondary": 18.0, "secondary_link": 16.0,
    "tertiary": 15.0, "tertiary_link": 14.0, "residential": 12.0, "living_street": 8.0,
    "service": 8.0, "unclassified": 10.0, "road": 10.0, "track": 6.0,
    "path": 5.0, "footway": 5.0, "cycleway": 10.0,
}
FALLBACK_SPEED_KMH = 12.0

_NUM = re.compile(r"(\d+(?:\.\d+)?)")


def _first(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def parse_maxspeed_kmh(raw: Any) -> Optional[float]:
    """Parse OSM `maxspeed` ('50', '30 mph', ['40','50'], 'walk', ...) to km/h (minimum if several)."""
    if raw is None:
        return None
    vals = raw if isinstance(raw, (list, tuple)) else [raw]
    out: List[float] = []
    for v in vals:
        s = str(v).strip().lower()
        if s in ("walk", "walking"):
            out.append(6.0)
            continue
        m = _NUM.search(s)
        if not m:
            continue
        num = float(m.group(1))
        if "mph" in s:
            num *= 1.609344
        if 0 < num < 200:
            out.append(num)
    return min(out) if out else None


def build_planning_graph(G: nx.MultiDiGraph, robot_max_kmh: float) -> nx.DiGraph:
    """Collapse a (OSMnx) MultiDiGraph to a DiGraph with planner-ready attributes."""
    D = nx.DiGraph()
    for n, d in G.nodes(data=True):
        D.add_node(n, y=float(d["y"]), x=float(d["x"]))
    for u, v, d in G.edges(data=True):
        if u == v:
            continue  # self-loops are useless for routing
        length = float(d.get("length") or 0.0)
        if length <= 0:
            length = haversine((G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"]))
        if D.has_edge(u, v) and D[u][v]["length"] <= length:
            continue
        highway = str(_first(d.get("highway")) or "unclassified")
        limit = parse_maxspeed_kmh(d.get("maxspeed"))
        speed = min(limit or DEFAULT_SPEED_KMH.get(highway, FALLBACK_SPEED_KMH), robot_max_kmh)
        geom = _edge_geometry(G, u, v, d.get("geometry"))
        D.add_edge(u, v, length=length, highway=highway, speed_kmh=max(speed, 3.0), geometry=geom)
    D.graph["robot_max_kmh"] = robot_max_kmh
    index_nodes(D)
    return D


def _edge_geometry(G, u, v, geometry) -> List[LatLon]:
    pu = (G.nodes[u]["y"], G.nodes[u]["x"])
    pv = (G.nodes[v]["y"], G.nodes[v]["x"])
    if geometry is None:
        return [pu, pv]
    try:
        pts = [(float(y), float(x)) for x, y in geometry.coords]
    except Exception:  # pragma: no cover - unexpected geometry type
        return [pu, pv]
    if len(pts) < 2:
        return [pu, pv]
    # Ensure orientation u -> v
    if haversine(pts[0], pu) > haversine(pts[-1], pu):
        pts.reverse()
    pts[0], pts[-1] = pu, pv
    return pts


def index_nodes(D: nx.DiGraph) -> None:
    nodes = list(D.nodes)
    D.graph["_node_ids"] = nodes
    D.graph["_node_idx"] = {n: i for i, n in enumerate(nodes)}
    D.graph["_lat"] = np.array([D.nodes[n]["y"] for n in nodes], dtype=float)
    D.graph["_lon"] = np.array([D.nodes[n]["x"] for n in nodes], dtype=float)


def nearest_node(D: nx.DiGraph, lat: float, lon: float) -> Tuple[Any, float]:
    """Return (node, distance_m) of the graph node nearest to (lat, lon)."""
    if "_lat" not in D.graph:
        index_nodes(D)
    dist = haversine_np(D.graph["_lat"], D.graph["_lon"], lat, lon)
    i = int(np.argmin(dist))
    return D.graph["_node_ids"][i], float(dist[i])


def node_latlon(D: nx.DiGraph, n: Any) -> LatLon:
    return D.nodes[n]["y"], D.nodes[n]["x"]


def synthetic_grid_graph(center_lat: float, center_lon: float, size: int = 31,
                         spacing_m: float = 120.0, robot_max_kmh: float = 25.0) -> nx.DiGraph:
    """Offline demo road network: a grid with arterials every 5th row/column.

    Used when OFFLINE_MAPS=true (tests, demos without internet).
    """
    G = nx.MultiDiGraph()
    dlat = spacing_m / 111_195.0
    dlon = spacing_m / (111_195.0 * math.cos(math.radians(center_lat)))
    half = size // 2
    for i in range(size):
        for j in range(size):
            G.add_node(i * size + j, y=center_lat + (i - half) * dlat, x=center_lon + (j - half) * dlon)
    for i in range(size):
        for j in range(size):
            n = i * size + j
            for di, dj in ((1, 0), (0, 1)):
                ii, jj = i + di, j + dj
                if ii >= size or jj >= size:
                    continue
                m = ii * size + jj
                arterial = (i % 5 == 0 and di == 0) or (j % 5 == 0 and dj == 0)
                hw = "primary" if arterial else "residential"
                length = haversine((G.nodes[n]["y"], G.nodes[n]["x"]), (G.nodes[m]["y"], G.nodes[m]["x"]))
                G.add_edge(n, m, length=length, highway=hw)
                G.add_edge(m, n, length=length, highway=hw)
    return build_planning_graph(G, robot_max_kmh)


def path_edges(path: Iterable[Any]) -> List[Tuple[Any, Any]]:
    p = list(path)
    return list(zip(p, p[1:]))
