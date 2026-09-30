"""Battery model: energy use vs. distance, speed and grade, with a planning reserve."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from ..config import settings


@dataclass
class BatteryStats:
    capacity_kwh: float
    current_charge_kwh: float
    cycles: float = 0.0
    charging: bool = False


class BatteryModel:
    RESERVE_FRACTION = 0.10   # never plan to use the last 10 %
    FADE_PER_CYCLE = 0.0002   # 0.02 % capacity loss per full cycle

    def __init__(self, capacity_kwh: float = settings.BATTERY_CAPACITY_KWH):
        self.nominal_capacity = capacity_kwh
        self.stats = BatteryStats(capacity_kwh=capacity_kwh, current_charge_kwh=capacity_kwh)
        self.consumption_rate = settings.BATTERY_CONSUMPTION_KWH_PER_KM
        self.regen_efficiency = 0.7
        self._discharged_kwh = 0.0

    # Energy for a segment in kWh (negative grade -> partial regen).
    def calculate_consumption(self, distance_m: float, speed_kmh: float, grade_percent: float = 0.0) -> float:
        base = distance_m / 1000.0 * self.consumption_rate
        speed_factor = 1.0 + 0.01 * (speed_kmh / 10.0) ** 2
        grade_factor = max(0.0, 1.0 + 0.05 * grade_percent)
        consumption = base * speed_factor * grade_factor
        if grade_percent < 0:
            consumption *= 1.0 - min(-grade_percent * 0.01 * self.regen_efficiency, 0.5)
        return consumption

    def consume_energy(self, distance_m: float, speed_kmh: float, grade_percent: float = 0.0) -> Dict:
        if self.stats.charging:
            return {"success": False, "message": "Robot is charging"}
        need = self.calculate_consumption(distance_m, speed_kmh, grade_percent)
        if need > self.stats.current_charge_kwh:
            return {"success": False, "message": "Insufficient battery",
                    "required_kwh": need, "available_kwh": self.stats.current_charge_kwh}
        self.stats.current_charge_kwh -= need
        self._discharged_kwh += need
        # one cycle per nominal capacity discharged; capacity fades slowly with cycles
        self.stats.cycles = self._discharged_kwh / self.nominal_capacity
        self.stats.capacity_kwh = self.nominal_capacity * max(0.6, 1.0 - self.FADE_PER_CYCLE * self.stats.cycles)
        self.stats.current_charge_kwh = min(self.stats.current_charge_kwh, self.stats.capacity_kwh)
        return {"success": True, "consumption_kwh": need,
                "remaining_kwh": self.stats.current_charge_kwh, "percentage": self.get_percentage()}

    def charge(self, energy_kwh: float) -> Dict:
        old = self.stats.current_charge_kwh
        self.stats.current_charge_kwh = min(old + max(0.0, energy_kwh), self.stats.capacity_kwh)
        self.stats.charging = energy_kwh > 0
        return {"charged_kwh": self.stats.current_charge_kwh - old, "percentage": self.get_percentage()}

    def get_percentage(self) -> float:
        return self.stats.current_charge_kwh / self.stats.capacity_kwh * 100.0

    def usable_kwh(self) -> float:
        return max(0.0, self.stats.current_charge_kwh - self.RESERVE_FRACTION * self.stats.capacity_kwh)

    def can_complete_energy(self, required_kwh: float, margin: float = 1.2) -> bool:
        return required_kwh * margin <= self.usable_kwh()

    def estimate_range(self) -> float:
        """Remaining range in metres (excluding reserve)."""
        return self.usable_kwh() / self.consumption_rate * 1000.0
