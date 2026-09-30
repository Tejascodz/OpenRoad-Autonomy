"""Graph search: Dijkstra, A* and ALT (A* + Landmarks + Triangle inequality).

ALT (Goldberg & Harrelson) pre-computes exact distances to/from a handful of
"landmark" nodes and uses the triangle inequality to get a much tighter lower
bound than straight-line distance. It is the same family of goal-directed
techniques production routers use, and typically expands several times fewer
nodes than haversine-A* on real road networks.
"""
from __future__ import annotations

import heapq
import itertools
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import networkx as nx
import numpy as np

from .costs import CostModel
from .graph import node_latlon
from .geo import haversine

HEURISTIC_SAFETY = 0.999  # guard against float rounding breaking admissibility


class NoRouteError(Exception):
    pass


@dataclass
class SearchResult:
    path: List[Any]
    cost: float
    expanded: int
    time_ms: float
    algorithm: str
    extra: Dict[str, Any] = field(default_factory=dict)


def _reconstruct(parent: Dict[Any, Any], target: Any) -> List[Any]:
    path = [target]
    while parent[path[-1]] is not None:
        path.append(parent[path[-1]])
    path.reverse()
    return path


def astar(G: nx.DiGraph, source: Any, target: Any, cost: Callable[[Any, Any, dict], float],
          heuristic: Optional[Callable[[Any], float]] = None, algorithm: str = "astar") -> SearchResult:
    """A* with lazy deletion (correct even for merely admissible heuristics). h=None -> Dijkstra."""
    t0 = time.perf_counter()
    h = heuristic or (lambda n: 0.0)
    counter = itertools.count()
    g: Dict[Any, float] = {source: 0.0}
    parent: Dict[Any, Any] = {source: None}
    heap = [(h(source), next(counter), 0.0, source)]
    expanded = 0
    succ = G._succ  # fast adjacency access
    while heap:
        _, _, g_push, u = heapq.heappop(heap)
        gu = g[u]
        if g_push > gu:  # stale entry (a cheaper path to u was found later)
            continue
        expanded += 1
        if u == target:
            return SearchResult(_reconstruct(parent, target), gu, expanded,
                                (time.perf_counter() - t0) * 1000, algorithm)
        for v, d in succ[u].items():
            c = cost(u, v, d)
            if c == math.inf:
                continue
            ng = gu + c
            if ng < g.get(v, math.inf) - 1e-12:
                g[v] = ng
                parent[v] = u
                heapq.heappush(heap, (ng + h(v), next(counter), ng, v))
    raise NoRouteError(f"no route from {source} to {target}")


def haversine_heuristic(G: nx.DiGraph, target: Any, model: CostModel) -> Callable[[Any], float]:
    tgt = node_latlon(G, target)
    k = model.lower_bound_per_m * HEURISTIC_SAFETY
    nodes = G.nodes
    return lambda n: haversine((nodes[n]["y"], nodes[n]["x"]), tgt) * k


class Landmarks:
    """ALT pre-processing for one graph + cost profile."""

    def __init__(self, G: nx.DiGraph, model: CostModel, k: int = 8, seed: int = 7):
        t0 = time.perf_counter()
        self.G = G
        self.profile = model.profile
        base = CostModel(model.profile, model.robot_max_kmh, model.consumption_kwh_per_km)  # no blocks/penalties
        w = lambda u, v, d: base.base_cost(u, v, d)  # noqa: E731
        idx = G.graph["_node_idx"]
        n = len(idx)
        rng = random.Random(seed)  # nosec B311 - landmark sampling, not security
        nodes = G.graph["_node_ids"]
        R = G.reverse(copy=False)
        self.d_from: List[np.ndarray] = []  # d(L, v)
        self.d_to: List[np.ndarray] = []    # d(v, L)
        self.landmarks: List[Any] = []
        k = max(1, min(k, n))
        # Farthest-point landmark selection (on forward distances).
        candidate = nodes[rng.randrange(n)]
        min_dist = np.full(n, np.inf)
        for _ in range(k):
            fwd = self._sssp(G, candidate, w, idx, n)
            bwd = self._sssp(R, candidate, lambda u, v, d: w(v, u, d), idx, n)
            self.landmarks.append(candidate)
            self.d_from.append(fwd)
            self.d_to.append(bwd)
            both = np.where(np.isfinite(fwd), fwd, 0.0) + np.where(np.isfinite(bwd), bwd, 0.0)
            min_dist = np.minimum(min_dist, both)
            md = min_dist.copy()
            for lm in self.landmarks:
                md[idx[lm]] = -1
            candidate = nodes[int(np.argmax(md))]
        self._F = np.vstack(self.d_from)
        self._T = np.vstack(self.d_to)
        self.build_ms = (time.perf_counter() - t0) * 1000

    @staticmethod
    def _sssp(G, src, w, idx, n) -> np.ndarray:
        arr = np.full(n, np.inf)
        for node, dist in nx.single_source_dijkstra_path_length(G, src, weight=w).items():
            arr[idx[node]] = dist
        return arr

    def heuristic(self, target: Any) -> Callable[[Any], float]:
        idx = self.G.graph["_node_idx"]
        t = idx[target]
        F_t = self._F[:, t]
        T_t = self._T[:, t]
        F, T = self._F, self._T

        def h(nd: Any) -> float:
            i = idx[nd]
            f_v = F[:, i]
            t_v = T[:, i]
            with np.errstate(invalid="ignore"):  # inf - inf -> nan, filtered below
                a = F_t - f_v  # d(L,t) - d(L,v) <= d(v,t)
                b = t_v - T_t  # d(v,L) - d(t,L) <= d(v,t)
            a = a[np.isfinite(a)]
            b = b[np.isfinite(b)]
            best = 0.0
            if a.size:
                best = max(best, float(a.max()))
            if b.size:
                best = max(best, float(b.max()))
            return best * HEURISTIC_SAFETY

        return h
