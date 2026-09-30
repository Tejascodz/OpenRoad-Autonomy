"""Map / road-network service.

* OSM (no API key): downloads the drivable road network of the operating area once
  with OSMnx, saves it to data/graphs/, and hands out a RoutePlanner. Trips must stay
  inside that loaded map (no surprise downloads per trip).
* Offline: OFFLINE_MAPS=true builds a synthetic grid (tests & demos without internet).

All functions here are blocking; call them from a worker thread (FastAPI's
`run_in_threadpool`) so the event loop and robot control loop never stall.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import logger, settings
from .planning import RoutePlanner, VehicleLimits
from .planning.geo import LatLon, haversine
from .planning.graph import build_planning_graph, index_nodes, synthetic_grid_graph

NOT_LOADED_MSG = ("The road map is not loaded. Click 'Retry download' (or run: python -m app.cli fetch-map), "
                  "or use the offline demo map.")
STILL_DOWNLOADING_MSG = ("The road map is still downloading in the background - watch the top bar and try "
                         "again when it says 'Map ready'.")
BUSY_MSG = ("OpenStreetMap map servers are busy right now. The app keeps retrying in the background - "
            "try again in a few minutes, or click 'Use offline demo map'.")
MAX_SNAP_DISTANCE_M = 300.0
AREA_MARGIN_M = 300.0  # keep trips this far inside the downloaded map edge
CACHE_SIZE = 3
GRAPH_CACHE_MAX_AGE_S = 30 * 24 * 3600
RETRY_BACKOFF_S = (30, 90, 180)


def save_graph(D, path) -> None:
    """Persist a planning graph as gzipped JSON (safe format - never pickle)."""
    data = {"v": 1, "robot_max_kmh": D.graph.get("robot_max_kmh"),
            "nodes": [[n, d["y"], d["x"]] for n, d in D.nodes(data=True)],
            "edges": [[u, v, round(d["length"], 2), d["highway"], d["speed_kmh"],
                       [[round(a, 7), round(b, 7)] for a, b in d["geometry"]]] for u, v, d in D.edges(data=True)]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    tmp.replace(path)


def load_graph(path):
    import networkx as nx
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("v") != 1:
        raise ValueError("unknown graph cache version")
    D = nx.DiGraph()
    for n, y, x in data["nodes"]:
        D.add_node(n, y=float(y), x=float(x))
    for u, v, length, hw, spd, geom in data["edges"]:
        D.add_edge(u, v, length=float(length), highway=str(hw), speed_kmh=float(spd),
                   geometry=[(float(a), float(b)) for a, b in geom])
    D.graph["robot_max_kmh"] = data.get("robot_max_kmh")
    index_nodes(D)
    return D


class MapDataUnavailable(Exception):
    """Road network could not be obtained (network down, Overpass error...)."""


@dataclass
class _Region:
    center: LatLon
    radius_m: float
    planner: RoutePlanner

    def covers(self, p: LatLon, margin: float = AREA_MARGIN_M) -> bool:
        return haversine(self.center, p) + margin <= self.radius_m


def vehicle_limits() -> VehicleLimits:
    return VehicleLimits(
        max_kmh=settings.ROBOT_MAX_SPEED_KMH,
        consumption_kwh_per_km=settings.BATTERY_CONSUMPTION_KWH_PER_KM,
        a_lat=settings.MAX_LAT_ACCEL,
        a_acc=settings.MAX_ACCEL,
        a_dec=settings.MAX_DECEL,
    )


class MapService:
    def __init__(self) -> None:
        self._regions: "OrderedDict[Tuple, _Region]" = OrderedDict()
        self._lock = threading.Lock()
        self._offline: Optional[_Region] = None
        self._status: dict = {"state": "offline" if settings.OFFLINE_MAPS else "idle", "message": "",
                              "nodes": 0, "edges": 0, "updated": time.time()}
        self._ox = None
        self._warm_thread: Optional[threading.Thread] = None
        if not settings.OFFLINE_MAPS:
            self._init_osm()

    def _init_osm(self) -> None:
        if self._ox is None:
            import osmnx as ox  # heavy import, only when needed

            settings.OSM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            ox.settings.use_cache = True
            ox.settings.cache_folder = str(settings.OSM_CACHE_DIR)
            ox.settings.log_console = False
            ox.settings.requests_timeout = settings.OSM_TIMEOUT_S
            ox.settings.http_user_agent = settings.OSM_USER_AGENT
            # We probe servers ourselves (below). osmnx's own status check waits a fixed 60 s
            # when a server is unreachable, which made every failure look like a hang.
            ox.settings.overpass_rate_limit = False
            # One query for the whole warm-up area (12 x 12 km): many small sub-queries trigger the
            # servers' per-IP rate limit (HTTP 429). Finished answers are cached on disk.
            ox.settings.max_query_area_size = 200_000_000  # m^2
            from .overpass_client import install
            install(ox)  # bounded retries instead of OSMnx's endless 429/504 loop
            self._ox = ox

    # ---- status / warm-up ------------------------------------------------------
    def _set_status(self, state: str, message: str = "", G=None) -> None:
        self._status = {"state": state, "message": message,
                        "nodes": G.number_of_nodes() if G is not None else self._status.get("nodes", 0),
                        "edges": G.number_of_edges() if G is not None else self._status.get("edges", 0),
                        "updated": time.time()}

    def status(self) -> dict:
        s = dict(self._status)
        s["regions"] = [{"lat": round(r.center[0], 5), "lon": round(r.center[1], 5), "radius_m": round(r.radius_m)}
                        for r in self._regions.values()]
        s["area"] = self.operating_area()
        return s

    def _active_region(self) -> "Optional[_Region]":
        if settings.OFFLINE_MAPS:
            return self._offline
        return max(self._regions.values(), key=lambda r: r.radius_m, default=None)

    def operating_area(self) -> Optional[dict]:
        """The area robots can operate in = the road map that is loaded (minus a safety margin)."""
        r = self._active_region()
        if r is None:
            return None
        return {"lat": round(r.center[0], 6), "lon": round(r.center[1], 6),
                "radius_m": round(r.radius_m - AREA_MARGIN_M)}

    def in_area(self, p: LatLon) -> bool:
        r = self._active_region()
        return bool(r and r.covers(p))

    def set_offline(self, enabled: bool) -> None:
        """Switch between real OSM data and the synthetic demo grid at runtime."""
        settings.OFFLINE_MAPS = bool(enabled)
        if enabled:
            self._offline_region()
            self._set_status("offline", "offline demo grid")
        else:
            self._init_osm()
            self._set_status("ready" if self._regions else "idle", "")
            if not self._regions:
                self.start_warm_up()

    def start_warm_up(self) -> bool:
        """Start (or restart) the background map download; returns False if one is running."""
        if self._warm_thread is not None and self._warm_thread.is_alive():
            return False
        self._warm_thread = threading.Thread(target=self.warm_up, name="map-warmup", daemon=True)
        self._warm_thread.start()
        return True

    def _graph_cache_path(self, center: LatLon, radius: float):
        return settings.OSM_CACHE_DIR.parent / "graphs" / f"drive_{center[0]:.4f}_{center[1]:.4f}_{int(radius)}.json.gz"

    def warm_up(self, radius: Optional[float] = None, force_download: bool = False) -> None:
        """Blocking: get the operating-area road map (disk cache first, then download with
        patient retries) and pre-compute ALT landmarks."""
        c = (settings.GEOFENCE_CENTER_LAT, settings.GEOFENCE_CENTER_LON)
        radius = radius or settings.MAP_WARMUP_RADIUS_M
        try:
            if settings.OFFLINE_MAPS:
                planner = self._offline_region().planner
            else:
                region = None if force_download else self._load_cached_region(c, radius)
                if region is None:
                    region = self._download(c, radius, rounds=len(RETRY_BACKOFF_S) + 1, background=True)
                    self._save_region(region)
                with self._lock:
                    self._store(region)
                planner = region.planner
            self._set_status("preparing", "pre-computing ALT landmarks", planner.G)
            planner.landmarks("fastest")
            self._set_status("ready" if not settings.OFFLINE_MAPS else "offline",
                             "road map ready" if not settings.OFFLINE_MAPS else "offline demo grid", planner.G)
        except Exception as exc:  # details already logged
            if settings.OFFLINE_MAPS:
                return  # user switched to the demo grid; keep its status
            if self._status.get("state") != "error":
                self._set_status("error", f"warm-up failed: {type(exc).__name__}")

    def _save_region(self, region: "_Region") -> None:
        try:
            save_graph(region.planner.G, self._graph_cache_path(region.center, region.radius_m))
        except OSError as exc:
            logger.warning("Could not save road-map cache: %s", exc)

    def _load_cached_region(self, center: LatLon, radius: float) -> "Optional[_Region]":
        """Load the largest saved map for this centre (e.g. one saved by `fetch-map --radius 10000`)."""
        folder = self._graph_cache_path(center, radius).parent
        prefix = f"drive_{center[0]:.4f}_{center[1]:.4f}_"
        candidates = []
        for path in folder.glob(prefix + "*.json.gz") if folder.is_dir() else []:
            try:
                r = int(path.name[len(prefix):-len(".json.gz")])
            except ValueError:
                continue
            if time.time() - path.stat().st_mtime < GRAPH_CACHE_MAX_AGE_S:
                candidates.append((r, path))
        for r, path in sorted(candidates, reverse=True):
            try:
                D = load_graph(path)
                logger.info("Road map loaded from disk (%d junctions, %.1f km radius) - no download needed",
                            D.number_of_nodes(), r / 1000)
                return _Region(center, float(r), RoutePlanner(D, vehicle_limits()))
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("Ignoring unreadable road-map cache %s: %s", path.name, exc)
        return None

    # ---- public -------------------------------------------------------------
    def planner_for(self, points: List[LatLon]) -> RoutePlanner:
        """Planner for the loaded road map. Points outside it are rejected (no surprise downloads)."""
        if settings.OFFLINE_MAPS:
            region = self._offline_region()
        else:
            region = self._active_region()
            if region is None:
                if self._status.get("state") in ("downloading", "preparing") or (
                        self._warm_thread is not None and self._warm_thread.is_alive()):
                    raise MapDataUnavailable(STILL_DOWNLOADING_MSG)
                raise MapDataUnavailable(NOT_LOADED_MSG)
        outside = [p for p in points if not region.covers(p)]
        if outside:
            raise ValueError(f"That location is outside the loaded road map (the dashed circle, "
                             f"{(region.radius_m - AREA_MARGIN_M) / 1000:.1f} km around the centre). Pick a point "
                             "inside it, or enlarge the map with: python -m app.cli fetch-map --radius 10000")
        return region.planner

    def _store(self, region: "_Region") -> None:
        self._regions[(round(region.center[0], 4), round(region.center[1], 4), int(region.radius_m))] = region
        while len(self._regions) > CACHE_SIZE:
            self._regions.popitem(last=False)

    def snap_or_raise(self, planner: RoutePlanner, p: LatLon, label: str):
        node, dist = planner.snap(*p)
        if dist > MAX_SNAP_DISTANCE_M:
            raise ValueError(f"{label} is {dist:.0f} m from the nearest road; must be within {MAX_SNAP_DISTANCE_M:.0f} m")
        return node

    # ---- internals ----------------------------------------------------------
    def _offline_region(self) -> _Region:
        if self._offline is None:
            c = (settings.GEOFENCE_CENTER_LAT, settings.GEOFENCE_CENTER_LON)
            G = synthetic_grid_graph(c[0], c[1], size=41, spacing_m=150.0,
                                     robot_max_kmh=settings.ROBOT_MAX_SPEED_KMH)
            self._offline = _Region(c, 20 * 150.0, RoutePlanner(G, vehicle_limits()))
            logger.info("Offline synthetic road grid ready (%d nodes)", G.number_of_nodes())
        return self._offline

    def _download(self, center: LatLon, radius: float, rounds: int = 1, background: bool = False) -> _Region:
        logger.info("Downloading OSM drive network: center=(%.5f, %.5f) radius=%.0f m (first time can take 30-90 s)",
                    center[0], center[1], radius)
        t0 = time.perf_counter()
        G, last_err = None, "no server reachable"
        for attempt in range(1, rounds + 1):
            if settings.OFFLINE_MAPS and background:
                break  # user switched to the demo grid meanwhile
            if background and attempt > 1:
                cached = self._load_cached_region(center, radius)  # e.g. written by `app.cli fetch-map`
                if cached is not None:
                    self._set_status("ready", "road map ready", cached.planner.G)
                    return cached
            self._set_status("downloading", f"downloading road map ({radius / 1000:.1f} km radius)"
                             + (f" - attempt {attempt}/{rounds}" if attempt > 1 else ""))
            for base in settings.OVERPASS_URLS:
                host = base.split("/")[2]
                self._set_status("downloading", f"contacting {host}" + (f" (round {attempt}/{rounds})" if attempt > 1 else ""))
                good, why = probe_overpass(base)
                if not good:
                    logger.warning("Overpass server %s not available: %s", base, why)
                    last_err = why
                    continue
                self._set_status("downloading", f"downloading road map from {host} - usually 20-90 s")
                self._ox.settings.overpass_url = base
                try:
                    logger.info("Downloading from Overpass server %s", base)
                    G = self._ox.graph_from_point(center, dist=radius, dist_type="bbox",
                                                  network_type="drive", simplify=True)
                    break
                except Exception as exc:  # network errors, server overload, empty results
                    last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
                    logger.error("OSM download from %s failed: %s", base, last_err)
            if G is not None or attempt == rounds:
                break
            wait = RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)]
            logger.warning("All OpenStreetMap servers busy - retrying in %d s (attempt %d/%d)", wait, attempt, rounds)
            self._set_status("error", f"OpenStreetMap servers busy - retrying automatically in {wait} s "
                                      f"(attempt {attempt}/{rounds}). You can use the offline demo map meanwhile.")
            time.sleep(wait)
        if G is None and settings.OFFLINE_MAPS and background:
            logger.info("Background map download stopped: offline demo map selected")
            raise MapDataUnavailable("download cancelled (offline demo map selected)")
        if G is None:
            reason = last_err.split(":")[0][:40]
            self._set_status("error", f"OpenStreetMap servers did not answer ({reason}). They are often overloaded - "
                                      "click 'Retry download' in a few minutes, or use the offline demo map. "
                                      "(Diagnose: python -m app.cli check-network)")
            raise MapDataUnavailable(BUSY_MSG)
        D = build_planning_graph(G, settings.ROBOT_MAX_SPEED_KMH)
        logger.info("Road graph ready: %d nodes, %d edges (%.1f s)", D.number_of_nodes(), D.number_of_edges(),
                    time.perf_counter() - t0)
        self._set_status("ready", "road map ready", D)
        return _Region(center, radius, RoutePlanner(D, vehicle_limits()))


def probe_overpass(base: str, timeout=(6, 25)) -> tuple[bool, str]:
    """Tiny, cheap Overpass request to check a server is reachable and answering."""
    import requests
    try:
        r = requests.get(base + "/interpreter", params={"data": "[out:json][timeout:10];out;"},
                         headers={"User-Agent": settings.OSM_USER_AGENT}, timeout=timeout)
        if r.status_code == 200:
            return True, "ok"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"
