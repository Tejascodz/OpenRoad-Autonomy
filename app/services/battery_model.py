import numpy as np
from typing import Dict, Optional
from dataclasses import dataclass
from enum import Enum
import logging
from ..config import settings, logger

class BatteryHealth(Enum):
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    CRITICAL = "critical"

@dataclass
class BatteryStats:
    capacity_kwh: float
    current_charge_kwh: float
    voltage_v: float = 48.0  # Typical for delivery robots
    current_a: float = 0.0
    temperature_c: float = 25.0
    cycles: int = 0
    health: BatteryHealth = BatteryHealth.EXCELLENT
    charging: bool = False

class BatteryModel:
    """Sophisticated battery model with real-world characteristics"""
    
    def __init__(self, capacity_kwh: float = settings.BATTERY_CAPACITY_KWH):
        self.stats = BatteryStats(
            capacity_kwh=capacity_kwh,
            current_charge_kwh=capacity_kwh  # Start fully charged
        )
        self.consumption_rate = settings.BATTERY_CONSUMPTION_KWH_PER_KM
        self.regenerative_braking_efficiency = 0.7  # 70% recovery
        self.temperature_derate_factor = 1.0
        self.cycle_degradation = 0.0002  # 0.02% per cycle
        logger.info(f"Battery model initialized: {capacity_kwh}kWh capacity")
    
    def calculate_consumption(self, distance_m: float, speed_kmh: float, 
                             grade_percent: float = 0.0) -> float:
        """
        Calculate energy consumption for a given distance
        Returns energy consumed in kWh
        """
        distance_km = distance_m / 1000
        
        # Base consumption
        base_consumption = distance_km * self.consumption_rate
        
        # Speed factor (aerodynamic drag increases with speed squared)
        speed_factor = 1 + 0.01 * (speed_kmh / 10) ** 2
        
        # Grade factor (uphill consumes more, downhill less)
        grade_factor = 1 + 0.05 * grade_percent
        
        # Temperature derating
        temp_factor = self.temperature_derate_factor
        
        # Battery health degradation
        health_factor = 1 + (1000 - self.stats.cycles) * self.cycle_degradation
        
        consumption = base_consumption * speed_factor * grade_factor * temp_factor / health_factor
        
        # Apply regenerative braking for downhill (negative grade)
        if grade_percent < 0:
            regen_recovery = -grade_percent * 0.01 * self.regenerative_braking_efficiency
            consumption *= (1 - min(regen_recovery, 0.5))  # Max 50% recovery
        
        return consumption
    
    def consume_energy(self, distance_m: float, speed_kmh: float, 
                      grade_percent: float = 0.0) -> Dict:
        """Consume energy for a movement segment"""
        if self.stats.charging:
            logger.warning("Attempted to consume energy while charging")
            return {'success': False, 'message': 'Robot is charging'}
        
        consumption = self.calculate_consumption(distance_m, speed_kmh, grade_percent)
        
        if consumption > self.stats.current_charge_kwh:
            logger.error(f"Insufficient battery: need {consumption:.3f}kWh, have {self.stats.current_charge_kwh:.3f}kWh")
            return {
                'success': False,
                'message': 'Insufficient battery',
                'required_kwh': consumption,
                'available_kwh': self.stats.current_charge_kwh
            }
        
        self.stats.current_charge_kwh -= consumption
        self.stats.current_a = (consumption * 1000) / (distance_m / speed_kmh * 3600) if distance_m > 0 else 0
        
        # Update health based on depth of discharge
        depth_of_discharge = 1 - (self.stats.current_charge_kwh / self.stats.capacity_kwh)
        if depth_of_discharge > 0.8:
            self.stats.cycles += 0.1  # Partial cycle
        
        # Update health status
        self._update_health()
        
        logger.info(f"Consumed {consumption:.3f}kWh, remaining: {self.get_percentage():.1f}%")
        
        return {
            'success': True,
            'consumption_kwh': consumption,
            'remaining_kwh': self.stats.current_charge_kwh,
            'percentage': self.get_percentage()
        }
    
    def charge(self, energy_kwh: float) -> Dict:
        """Charge the battery"""
        old_charge = self.stats.current_charge_kwh
        self.stats.current_charge_kwh = min(
            self.stats.current_charge_kwh + energy_kwh,
            self.stats.capacity_kwh
        )
        self.stats.charging = energy_kwh > 0
        
        charged = self.stats.current_charge_kwh - old_charge
        logger.info(f"Charged {charged:.3f}kWh, now at {self.get_percentage():.1f}%")
        
        return {
            'charged_kwh': charged,
            'percentage': self.get_percentage()
        }
    
    def get_percentage(self) -> float:
        """Get current battery percentage"""
        return (self.stats.current_charge_kwh / self.stats.capacity_kwh) * 100
    
    def can_complete_route(self, route_distance_m: float, avg_speed_kmh: float) -> bool:
        """Check if battery can complete a route"""
        required = self.calculate_consumption(route_distance_m, avg_speed_kmh)
        return required <= self.stats.current_charge_kwh * 0.9  # 10% safety margin
    
    def estimate_range(self, avg_speed_kmh: float = settings.ROBOT_SPEED_KMH) -> float:
        """Estimate remaining range in meters"""
        remaining_km = self.stats.current_charge_kwh / self.consumption_rate
        return remaining_km * 1000
    
    def _update_health(self):
        """Update battery health status"""
        capacity_retention = self.stats.current_charge_kwh / self.stats.capacity_kwh
        
        if capacity_retention > 0.8:
            self.stats.health = BatteryHealth.EXCELLENT
        elif capacity_retention > 0.6:
            self.stats.health = BatteryHealth.GOOD
        elif capacity_retention > 0.4:
            self.stats.health = BatteryHealth.FAIR
        elif capacity_retention > 0.2:
            self.stats.health = BatteryHealth.POOR
        else:
            self.stats.health = BatteryHealth.CRITICAL
    
    def to_dict(self):
        return {
            'percentage': self.get_percentage(),
            'range_km': self.estimate_range() / 1000,
            'health': self.stats.health.value,
            'charging': self.stats.charging,
            'cycles': self.stats.cycles,
            'temperature_c': self.stats.temperature_c
        }