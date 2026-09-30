"""Turn a node path into a drivable reference trajectory.

* dense polyline following real road geometry
* curvature estimate per point
* velocity profile respecting road speed limits, lateral-acceleration limit in
  curves, and longitudinal accel/decel limits (forward/backward pass), starting
  and ending at standstill.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, List, Sequence

import networkx as nx
import numpy as np

from .geo import LatLon, LocalFrame, densify


@dataclass
class Trajectory:
    points: List[LatLon]          # dense lat/lon points
    xy: np.ndarray                # (N,2) local metres
    s: np.ndarray                 # cumulative arc length (m)
    curvature: np.ndarray         # 1/m
    v_limit: np.ndarray           # m/s legal/road limit
    v_ref: np.ndarray             # m/s planned speed profile
    edge_index: np.ndarray        # index i -> point lies on edge (path[i], path[i+1])
    frame: LocalFrame

    @property
    def length(self) -> float:
        return float(self.s[-1]) if len(self.s) else 0.0

    def eta_s(self, max_accel: float = 1.0) -> float:
        return float(segment_times(self.s, self.v_ref, max_accel).sum())


def segment_times(s: np.ndarray, v: np.ndarray, a: float) -> np.ndarray:
    ds = np.diff(s)
    vs = v[:-1] + v[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(vs > 0.1, 2 * ds / np.maximum(vs, 1e-9), 2 * np.sqrt(ds / max(a, 1e-3)))
    return t


def curvature_3pt(xy: np.ndarray, s: np.ndarray, window_m: float = 8.0,
                  reversal_curvature: float = 0.6) -> np.ndarray:
    """Menger curvature using neighbours ~window_m away (robust to dense sampling).

    Direction reversals (U-turns, > 120 deg heading change) have ~zero Menger curvature
    because the three points are collinear, so they get `reversal_curvature` instead.
    """
    n = len(xy)
    k = np.zeros(n)
    if n < 3:
        return k
    j_lo = np.searchsorted(s, s - window_m, side="right") - 1
    j_hi = np.searchsorted(s, s + window_m, side="left")
    j_lo = np.clip(j_lo, 0, n - 1)
    j_hi = np.clip(j_hi, 0, n - 1)
    for i in range(1, n - 1):
        a, b, c = xy[j_lo[i]], xy[i], xy[j_hi[i]]
        if j_lo[i] == i or j_hi[i] == i:
            continue
        ab = np.linalg.norm(b - a)
        bc = np.linalg.norm(c - b)
        ca = np.linalg.norm(a - c)
        if ab < 1e-6 or bc < 1e-6:
            continue
        cos_turn = float((b - a) @ (c - b)) / (ab * bc)
        if cos_turn < -0.5:  # heading change > 120 deg
            k[i] = reversal_curvature
            continue
        if ca < 1e-6:
            continue
        cross = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        k[i] = 2.0 * cross / (ab * bc * ca)
    return k


def velocity_profile(s: np.ndarray, v_limit: np.ndarray, curvature: np.ndarray,
                     a_lat: float, a_acc: float, a_dec: float) -> np.ndarray:
    with np.errstate(divide="ignore"):
        v_curve = np.where(curvature > 1e-6, np.sqrt(a_lat / np.maximum(curvature, 1e-9)), np.inf)
    v = np.minimum(v_limit, v_curve)
    if len(v) == 0:
        return v
    v[0] = 0.0
    v[-1] = 0.0
    ds = np.diff(s)
    for i in range(len(v) - 1):  # forward: acceleration limit
        v[i + 1] = min(v[i + 1], math.sqrt(v[i] ** 2 + 2 * a_acc * ds[i]))
    for i in range(len(v) - 2, -1, -1):  # backward: deceleration limit
        v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * a_dec * ds[i]))
    return v


def build_trajectory(G: nx.DiGraph, path: Sequence[Any], *, a_lat: float, a_acc: float,
                     a_dec: float, spacing_m: float = 4.0,
                     start_point: LatLon | None = None) -> Trajectory:
    if len(path) < 2:
        raise ValueError("path must contain at least 2 nodes")
    pts: List[LatLon] = []
    edge_idx: List[int] = []
    limits: List[float] = []
    for i, (u, v) in enumerate(zip(path, path[1:])):
        d = G[u][v]
        seg = densify(d["geometry"], spacing_m)
        if pts:
            seg = seg[1:]  # shared node
        pts.extend(seg)
        edge_idx.extend([i] * len(seg))
        limits.extend([d["speed_kmh"] / 3.6] * len(seg))
    if start_point is not None:
        # Prepend the robot's actual position (mid-edge replans / off-node starts).
        lead = densify([start_point, pts[0]], spacing_m)[:-1]
        pts = lead + pts
        edge_idx = [0] * len(lead) + edge_idx
        limits = [limits[0]] * len(lead) + limits
    return _finish(pts, np.array(edge_idx), np.array(limits, dtype=float), a_lat, a_acc, a_dec)


def _finish(pts: List[LatLon], edge_idx: np.ndarray, limits: np.ndarray,
            a_lat: float, a_acc: float, a_dec: float, reversal_curvature: float = 0.6) -> Trajectory:
    # drop consecutive duplicates
    keep = [0]
    for i in range(1, len(pts)):
        if pts[i] != pts[keep[-1]]:
            keep.append(i)
    pts = [pts[i] for i in keep]
    edge_idx = edge_idx[keep]
    limits = limits[keep]
    frame = LocalFrame(*pts[0])
    xy = frame.many_to_xy(pts)
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))])
    curv = curvature_3pt(xy, s, reversal_curvature=reversal_curvature)
    v_ref = velocity_profile(s, limits.copy(), curv, a_lat, a_acc, a_dec)
    return Trajectory(pts, xy, s, curv, limits, v_ref, edge_idx, frame)
