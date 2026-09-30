"""D* Lite (Koenig & Likhachev, 2002) - incremental replanning.

When the robot discovers that a road ahead is blocked, D* Lite repairs the
existing search tree instead of planning from scratch, so replanning cost is
proportional to the part of the map that changed. The search runs backwards
from the goal, which lets the start (the robot) move between replans.
"""
from __future__ import annotations

import heapq
import itertools
import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import networkx as nx

from .search import NoRouteError

Key = Tuple[float, float]


class DStarLite:
    def __init__(self, G: nx.DiGraph, start: Any, goal: Any,
                 cost: Callable[[Any, Any, dict], float],
                 h: Callable[[Any, Any], float]):
        """
        cost(u, v, data): current cost of edge u->v (math.inf if blocked); read on every access,
                          so update the underlying model then call `notify_edge_changes`.
        h(a, b): consistent lower bound on the cost from a to b.
        """
        self.G = G
        self.cost = cost
        self.h = h
        self.start = start
        self.goal = goal
        self.last = start
        self.km = 0.0
        self.g: Dict[Any, float] = {}
        self.rhs: Dict[Any, float] = {goal: 0.0}
        self._heap: List[Tuple[Key, int, Any]] = []
        self._inq: Dict[Any, Key] = {}
        self._cnt = itertools.count()
        self.expanded_total = 0
        self._push(goal, self._key(goal))

    # --- helpers -------------------------------------------------------------
    def _gv(self, s: Any) -> float:
        return self.g.get(s, math.inf)

    def _rv(self, s: Any) -> float:
        return self.rhs.get(s, math.inf)

    def _key(self, s: Any) -> Key:
        m = min(self._gv(s), self._rv(s))
        return (m + self.h(self.start, s) + self.km, m)

    def _push(self, s: Any, k: Key) -> None:
        self._inq[s] = k
        heapq.heappush(self._heap, (k, next(self._cnt), s))

    def _top(self) -> Tuple[Key, Optional[Any]]:
        while self._heap:
            k, _, s = self._heap[0]
            if self._inq.get(s) == k:
                return k, s
            heapq.heappop(self._heap)  # stale
        return (math.inf, math.inf), None

    def _update_vertex(self, u: Any) -> None:
        if u != self.goal:
            best = math.inf
            for v, d in self.G._succ[u].items():
                c = self.cost(u, v, d)
                if c != math.inf:
                    val = c + self._gv(v)
                    if val < best:
                        best = val
            self.rhs[u] = best
        self._inq.pop(u, None)
        if self._gv(u) != self._rv(u):
            self._push(u, self._key(u))

    # --- public API ------------------------------------------------------------
    def compute_shortest_path(self, max_expansions: int = 5_000_000) -> int:
        expanded = 0
        while True:
            k_old, u = self._top()
            if u is None:
                break
            if not (k_old < self._key(self.start) or self._rv(self.start) != self._gv(self.start)):
                break
            expanded += 1
            if expanded > max_expansions:
                raise NoRouteError("D* Lite expansion budget exceeded")
            k_new = self._key(u)
            if k_old < k_new:
                self._push(u, k_new)
            elif self._gv(u) > self._rv(u):
                self.g[u] = self._rv(u)
                self._inq.pop(u, None)
                for p in self.G._pred[u]:
                    self._update_vertex(p)
            else:
                self.g[u] = math.inf
                for p in list(self.G._pred[u]) + [u]:
                    self._update_vertex(p)
        self.expanded_total += expanded
        return expanded

    def move_to(self, new_start: Any) -> None:
        """The robot has moved: account for the heuristic shift (km) before the next repair."""
        if new_start == self.start:
            return
        self.km += self.h(self.last, new_start)
        self.last = new_start
        self.start = new_start

    def notify_edge_changes(self, edges: List[Tuple[Any, Any]]) -> int:
        """Call after the cost of `edges` changed; returns nodes expanded by the repair."""
        for u, _v in edges:
            self._update_vertex(u)
        return self.compute_shortest_path()

    def path(self) -> List[Any]:
        if self._gv(self.start) == math.inf:
            raise NoRouteError("goal unreachable")
        path = [self.start]
        seen = {self.start}
        s = self.start
        limit = self.G.number_of_nodes() + 1
        while s != self.goal:
            best, best_v = math.inf, None
            for v, d in self.G._succ[s].items():
                c = self.cost(s, v, d)
                if c == math.inf:
                    continue
                val = c + self._gv(v)
                if val < best:
                    best, best_v = val, v
            if best_v is None or best_v in seen or len(path) > limit:
                raise NoRouteError("D* Lite path extraction failed")
            path.append(best_v)
            seen.add(best_v)
            s = best_v
        return path

    def path_cost(self) -> float:
        return self._gv(self.start)

    def plan(self) -> Tuple[List[Any], int, float]:
        t0 = time.perf_counter()
        exp = self.compute_shortest_path()
        return self.path(), exp, (time.perf_counter() - t0) * 1000
