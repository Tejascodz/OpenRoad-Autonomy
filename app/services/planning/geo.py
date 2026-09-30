"""Small geodesy helpers (no external deps beyond numpy)."""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

import numpy as np

EARTH_R = 6_371_000.0  # metres (slightly below OSMnx's 6_371_009 -> heuristics stay admissible)

LatLon = Tuple[float, float]


def haversine(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def haversine_np(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> np.ndarray:
    p1 = np.radians(lat)
    p0 = math.radians(lat0)
    dp = p1 - p0
    dl = np.radians(lon) - math.radians(lon0)
    h = np.sin(dp / 2) ** 2 + np.cos(p1) * math.cos(p0) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.minimum(1.0, np.sqrt(h)))


class LocalFrame:
    """Equirectangular East-North projection around an origin (accurate to <0.1% over ~20 km)."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0 = lat0
        self.lon0 = lon0
        self._kx = math.radians(1) * EARTH_R * math.cos(math.radians(lat0))
        self._ky = math.radians(1) * EARTH_R

    def to_xy(self, lat: float, lon: float) -> Tuple[float, float]:
        return (lon - self.lon0) * self._kx, (lat - self.lat0) * self._ky

    def to_latlon(self, x: float, y: float) -> LatLon:
        return self.lat0 + y / self._ky, self.lon0 + x / self._kx

    def many_to_xy(self, pts: Sequence[LatLon]) -> np.ndarray:
        arr = np.asarray(pts, dtype=float).reshape(-1, 2)
        return np.column_stack(((arr[:, 1] - self.lon0) * self._kx, (arr[:, 0] - self.lat0) * self._ky))


def densify(points: Iterable[LatLon], spacing_m: float = 5.0) -> List[LatLon]:
    """Insert intermediate points so consecutive points are <= spacing_m apart."""
    pts = list(points)
    if len(pts) < 2:
        return pts
    out: List[LatLon] = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        d = haversine(a, b)
        if d < 1e-3:
            continue
        n = max(1, int(math.ceil(d / spacing_m)))
        for k in range(1, n + 1):
            t = k / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def inside_geofence(p: LatLon, center: LatLon, radius_km: float) -> bool:
    return haversine(p, center) <= radius_km * 1000.0
