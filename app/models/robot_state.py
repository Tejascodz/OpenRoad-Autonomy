from enum import Enum
from dataclasses import dataclass
from typing import Optional, Tuple
import time

class RobotMode(Enum):
    IDLE = "idle"
    PICKUP = "pickup"
    TRANSIT = "transit"
    DELIVERY = "delivery"
    RETURN = "return"
    CHARGING = "charging"
    EMERGENCY = "emergency"
    OFFLINE = "offline"

@dataclass
class RobotState:
    mode: RobotMode = RobotMode.IDLE
    current_lat: float = 0.0
    current_lon: float = 0.0
    battery_percentage: float = 100.0
    current_speed_kmh: float = 0.0
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None
    current_delivery_id: Optional[int] = None
    path_index: int = 0
    total_path: list = None
    distance_traveled_m: float = 0.0
    last_update: float = time.time()
    obstacle_detected: bool = False
    error_message: Optional[str] = None
    
    def to_dict(self):
        return {
            'mode': self.mode.value,
            'position': {'lat': self.current_lat, 'lon': self.current_lon},
            'battery': self.battery_percentage,
            'speed': self.current_speed_kmh,
            'delivery_id': self.current_delivery_id,
            'obstacle_detected': self.obstacle_detected,
            'progress': self.get_progress_percentage() if self.total_path else 0
        }
    
    def get_progress_percentage(self):
        if not self.total_path:
            return 0
        return (self.path_index / len(self.total_path)) * 100