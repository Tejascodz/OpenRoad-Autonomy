"""Place search (forward geocoding) via OpenStreetMap Nominatim.

Respects the Nominatim usage policy: identifying User-Agent, max 1 request per
second (global throttle), results cached, searches bounded to the geofence.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from typing import Dict, List

import requests

from ..config import logger, settings

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_lock = threading.Lock()
_last_call = 0.0
_cache: "OrderedDict[str, List[Dict]]" = OrderedDict()
CACHE_SIZE = 512


class GeocodeUnavailable(Exception):
    pass


def clean_query(q: str) -> str:
    q = _CTRL.sub(" ", q or "").strip()
    return re.sub(r"\s+", " ", q)[:120]


def _viewbox() -> str:
    lat, lon = settings.GEOFENCE_CENTER_LAT, settings.GEOFENCE_CENTER_LON
    dlat = settings.GEOFENCE_RADIUS_KM / 111.195
    dlon = dlat / max(0.1, math.cos(math.radians(lat)))
    return f"{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f},{lat - dlat:.5f}"  # left,top,right,bottom


def search(query: str, limit: int = 6) -> List[Dict]:
    q = clean_query(query)
    if len(q) < 3:
        return []
    key = q.lower()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    global _last_call
    with _lock:  # serialise + throttle to <= 1 request / second (Nominatim policy)
        wait = 1.05 - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            r = requests.get(settings.NOMINATIM_URL, timeout=10, params={
                "q": q, "format": "jsonv2", "limit": limit, "viewbox": _viewbox(), "bounded": 1,
            }, headers={"User-Agent": settings.OSM_USER_AGENT, "Accept-Language": "en"})
            r.raise_for_status()
            rows = r.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Place search failed: %s", type(exc).__name__)
            raise GeocodeUnavailable("place search unavailable") from exc
    from .planning.geo import inside_geofence
    center = (settings.GEOFENCE_CENTER_LAT, settings.GEOFENCE_CENTER_LON)
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not inside_geofence((lat, lon), center, settings.GEOFENCE_RADIUS_KM):
            continue
        name = str(row.get("display_name", ""))[:160]
        out.append({"name": name, "short": name.split(",")[0][:60], "lat": round(lat, 6), "lon": round(lon, 6)})
    with _lock:
        _cache[key] = out
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return out
