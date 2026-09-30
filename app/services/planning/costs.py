"""Edge cost models ("routing profiles").

Every model exposes:
  * cost(u, v, data)     -> float edge cost (math.inf if blocked)
  * lower_bound_per_m    -> a constant c such that cost(e) >= c * length(e) for every edge,
                            which makes `c * straight_line_distance` an admissible and
                            consistent A* heuristic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Set, Tuple

# Relative risk of operating a small robot on each road class (1.0 = calm residential street).
ROAD_RISK: Dict[str, float] = {
    "motorway": 6.0, "motorway_link": 5.0, "trunk": 4.0, "trunk_link": 3.5,
    "primary": 2.5, "primary_link": 2.2, "secondary": 1.8, "secondary_link": 1.6,
    "tertiary": 1.3, "tertiary_link": 1.3, "unclassified": 1.2, "road": 1.2,
    "residential": 1.0, "living_street": 1.0, "service": 1.1, "track": 1.5,
}

PROFILES = ("shortest", "fastest", "energy", "safest")


@dataclass
class CostModel:
    profile: str = "shortest"
    robot_max_kmh: float = 25.0
    consumption_kwh_per_km: float = 0.08
    blocked: Set[Tuple[Any, Any]] = field(default_factory=set)
    penalties: Dict[Tuple[Any, Any], float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile {self.profile!r}")

    # --- cost ---------------------------------------------------------------
    def base_cost(self, u: Any, v: Any, d: Dict[str, Any]) -> float:
        length = d["length"]
        if self.profile == "shortest":
            return length
        if self.profile == "fastest":
            return length / (d["speed_kmh"] / 3.6)  # seconds
        if self.profile == "energy":
            v_kmh = d["speed_kmh"]
            speed_factor = 1.0 + 0.01 * (v_kmh / 10.0) ** 2  # same drag term as BatteryModel
            return length / 1000.0 * self.consumption_kwh_per_km * speed_factor  # kWh
        # safest
        return length * ROAD_RISK.get(d.get("highway", ""), 1.2)

    def cost(self, u: Any, v: Any, d: Dict[str, Any]) -> float:
        if (u, v) in self.blocked:
            return math.inf
        c = self.base_cost(u, v, d)
        p = self.penalties.get((u, v))
        return c * p if p else c

    __call__ = cost

    # --- heuristic scale ----------------------------------------------------
    @property
    def lower_bound_per_m(self) -> float:
        if self.profile == "fastest":
            return 1.0 / (self.robot_max_kmh / 3.6)
        if self.profile == "energy":
            return self.consumption_kwh_per_km / 1000.0
        return 1.0  # shortest; safest (min risk factor is 1.0)

    def copy(self) -> "CostModel":
        return CostModel(self.profile, self.robot_max_kmh, self.consumption_kwh_per_km,
                         set(self.blocked), dict(self.penalties))
