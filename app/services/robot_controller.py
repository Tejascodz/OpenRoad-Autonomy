import asyncio
import time
import math
import random
import traceback
from typing import List, Tuple, Optional, Dict
from enum import Enum
import logging
from ..models.robot_state import RobotState, RobotMode
from ..services.battery_model import BatteryModel
from ..config import settings, logger

class ObstacleType(Enum):
    NONE = "none"
    STATIC = "static"
    DYNAMIC = "dynamic"
    PEDESTRIAN = "pedestrian"
    VEHICLE = "vehicle"

class RoadType(Enum):
    HIGHWAY = "highway"
    MAIN_ROAD = "main_road"
    RESIDENTIAL = "residential"
    SERVICE = "service"
    PEDESTRIAN = "pedestrian"

class RobotController:
    """Main robot controller with state machine and simulation"""
    
    def __init__(self, robot_id: str = "R001"):
        self.robot_id = robot_id
        self.state = RobotState()
        # Set default location to Bangalore, India
        self.state.current_lat = 12.9716  # Bangalore latitude
        self.state.current_lon = 77.5946  # Bangalore longitude
        self.battery = BatteryModel()
        self.path = []
        self.obstacle_detected = False
        self.emergency_stop_activated = False
        self.current_segment_index = 0
        self.last_position_update = time.time()
        self.simulation_speed = 1.0  # Real-time by default
        self.movement_task = None
        self.is_moving = False
        
        # Realistic speed limits based on road type (km/h)
        self.speed_limits = {
            'motorway': 25.0,
            'trunk': 25.0,
            'primary': 20.0,
            'secondary': 18.0,
            'tertiary': 15.0,
            'residential': 12.0,
            'service': 8.0,
            'unclassified': 10.0,
            'path': 5.0,
            'footway': 5.0,
            'default': 15.0
        }
        
        # Traffic factors (time of day based)
        self.get_traffic_factor = self._get_traffic_factor
        
        # Sensor placeholders
        self.gps_noise = 0.000005  # ~0.5m noise (more realistic)
        self.lidar_range = 50  # meters
        self.camera_active = False
        
        logger.info(f"Robot {robot_id} initialized at {self.state.current_lat:.6f}, {self.state.current_lon:.6f}")
    
    def _get_traffic_factor(self):
        """Get traffic factor based on time of day"""
        hour = datetime.now().hour
        # Rush hours: 8-10 AM and 5-8 PM
        if (8 <= hour <= 10) or (17 <= hour <= 20):
            return 0.6  # 40% speed reduction
        # Night time: 10 PM - 6 AM
        elif hour <= 6 or hour >= 22:
            return 1.2  # 20% speed increase
        # Normal hours
        else:
            return 1.0
    
    def _get_road_type_speed(self, road_type):
        """Get speed limit for road type"""
        if isinstance(road_type, list):
            road_type = road_type[0] if road_type else 'default'
        return self.speed_limits.get(road_type, self.speed_limits['default'])
    
    async def start_delivery(self, pickup: Tuple[float, float], 
                            delivery: Tuple[float, float],
                            path: List[Tuple[float, float]]) -> Dict:
        """Start a new delivery mission"""
        logger.info("=" * 50)
        logger.info("ROBOT START DELIVERY")
        logger.info(f"Pickup: {pickup}")
        logger.info(f"Delivery: {delivery}")
        logger.info(f"Path points: {len(path)}")
        logger.info("=" * 50)
        
        try:
            # Validate path
            if not path or len(path) < 2:
                raise ValueError("Invalid path - must have at least 2 points")
            
            # Validate battery
            route_distance = self._calculate_path_distance(path)
            logger.info(f"Route distance: {route_distance:.2f}m ({route_distance/1000:.2f}km)")
            
            battery_check = self.battery.can_complete_route(route_distance, settings.ROBOT_SPEED_KMH)
            if not battery_check:
                available_range = self.battery.estimate_range()
                raise ValueError(f"Insufficient battery for route. Need {route_distance/1000:.2f}km, have {available_range/1000:.2f}km range")
            
            logger.info(f"Battery check passed: {self.battery.get_percentage():.1f}% remaining")
            
            # Initialize state
            self.state.mode = RobotMode.PICKUP
            self.state.current_lat, self.state.current_lon = pickup
            self.state.target_lat, self.state.target_lon = delivery
            self.path = path
            self.state.total_path = path
            self.state.path_index = 0
            self.state.distance_traveled_m = 0
            self.emergency_stop_activated = False
            self.is_moving = True
            
            logger.info(f"Delivery initialized - Mode: {self.state.mode.value}")
            
            # Start movement task
            if self.movement_task and not self.movement_task.done():
                self.movement_task.cancel()
                try:
                    await self.movement_task
                except asyncio.CancelledError:
                    pass
            
            self.movement_task = asyncio.create_task(self._move_along_path())
            logger.info("Movement task created and started")
            
            estimated_duration = route_distance / 1000 / settings.ROBOT_SPEED_KMH * 60
            logger.info(f"Estimated duration: {estimated_duration:.1f} minutes")
            
            return {
                'success': True,
                'message': 'Delivery started',
                'robot_id': self.robot_id,
                'estimated_duration_min': estimated_duration,
                'battery_start': self.battery.get_percentage(),
                'distance_m': route_distance,
                'path_points': len(path)
            }
            
        except ValueError as e:
            logger.error(f"Validation error: {str(e)}")
            self.state.mode = RobotMode.IDLE
            self.state.error_message = str(e)
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.error(f"Failed to start delivery: {str(e)}")
            logger.error(traceback.format_exc())
            self.state.mode = RobotMode.IDLE
            self.state.error_message = str(e)
            return {'success': False, 'error': str(e)}
    
    async def _move_along_path(self):
        """Simulate robot movement along path"""
        logger.info("Robot starting to move along path")
        
        try:
            while self.state.path_index < len(self.path) - 1 and not self.emergency_stop_activated:
                if self.emergency_stop_activated:
                    logger.warning("Emergency stop detected, pausing movement")
                    self.state.mode = RobotMode.EMERGENCY
                    await asyncio.sleep(1)
                    continue
                
                # Check for obstacles
                if await self._check_obstacles():
                    self.state.obstacle_detected = True
                    logger.warning(f"Obstacle detected at {self.state.current_lat:.6f}, {self.state.current_lon:.6f}")
                    await self._handle_obstacle()
                    continue
                else:
                    self.state.obstacle_detected = False
                
                # Move to next point
                current_index = self.state.path_index
                next_point = self.path[current_index + 1]
                
                logger.info(f"Moving to point {current_index + 1}/{len(self.path) - 1}")
                await self._move_to_point(next_point, current_index)
                
                self.state.path_index += 1
                progress = (self.state.path_index / (len(self.path) - 1)) * 100
                logger.info(f"Reached point {self.state.path_index}/{len(self.path) - 1} - Progress: {progress:.1f}%")
                
                # Update delivery status based on progress - FIXED TRANSITION
                if self.state.path_index >= len(self.path) - 1:
                    if self.state.mode == RobotMode.PICKUP:
                        self.state.mode = RobotMode.TRANSIT
                        logger.info("=" * 40)
                        logger.info("REACHED PICKUP POINT - NOW IN TRANSIT MODE")
                        logger.info(f"Moving from pickup to delivery location")
                        logger.info("=" * 40)
                    elif self.state.mode == RobotMode.TRANSIT:
                        self.state.mode = RobotMode.DELIVERY
                        logger.info("=" * 40)
                        logger.info("REACHED DELIVERY POINT - DELIVERY COMPLETE")
                        logger.info("=" * 40)
                    elif self.state.mode == RobotMode.DELIVERY:
                        self.state.mode = RobotMode.RETURN
                        logger.info("=" * 40)
                        logger.info("DELIVERY COMPLETE - RETURNING TO BASE")
                        logger.info("=" * 40)
                    elif self.state.mode == RobotMode.RETURN:
                        self.state.mode = RobotMode.IDLE
                        self.is_moving = False
                        logger.info("=" * 40)
                        logger.info("MISSION COMPLETE - ROBOT RETURNED TO BASE")
                        logger.info("=" * 40)
                
                await asyncio.sleep(1)  # 1 second between points
            
            if self.emergency_stop_activated:
                logger.warning("Movement stopped due to emergency stop")
            elif self.state.path_index >= len(self.path) - 1:
                logger.info("Movement along path completed")
                
        except asyncio.CancelledError:
            logger.info("Movement task was cancelled")
        except Exception as e:
            logger.error(f"Error in movement loop: {str(e)}")
            logger.error(traceback.format_exc())
            self.state.mode = RobotMode.EMERGENCY
            self.state.error_message = str(e)
    
    async def _move_to_point(self, target: Tuple[float, float], segment_index: int):
        """Move robot to target point with realistic speed"""
        start_time = time.time()
        
        # Calculate distance
        lat1, lon1 = self.state.current_lat, self.state.current_lon
        lat2, lon2 = target
        
        # Haversine formula
        distance = self._haversine_distance((lat1, lon1), (lat2, lon2))
        
        # Get road type for this segment (if available in path data)
        road_type = 'default'
        # In a real implementation, you'd get this from the graph
        # For now, simulate based on location
        if distance > 100:
            road_type = 'primary'
        elif distance > 50:
            road_type = 'residential'
        else:
            road_type = 'service'
        
        # Calculate realistic speed
        base_speed = self._get_road_type_speed(road_type)
        traffic_factor = self._get_traffic_factor()
        current_speed = base_speed * traffic_factor
        
        # Add small random variations (±10%)
        current_speed *= random.uniform(0.9, 1.1)
        
        # Convert to m/s
        speed_ms = current_speed / 3.6
        
        # Calculate movement time
        movement_time = distance / speed_ms if speed_ms > 0 else 0
        
        # Simulate movement with realistic timing
        steps = max(1, int(movement_time * 2))  # Update ~2 times per second
        
        logger.info(f"Segment {segment_index}: {distance:.1f}m, speed: {current_speed:.1f} km/h, time: {movement_time:.1f}s")
        
        for step in range(steps):
            if self.emergency_stop_activated:
                logger.debug("Movement interrupted by emergency stop")
                break
            
            # Linear interpolation
            fraction = (step + 1) / steps
            new_lat = lat1 + (lat2 - lat1) * fraction
            new_lon = lon1 + (lon2 - lon1) * fraction
            
            # Add realistic GPS noise
            new_lat += random.gauss(0, self.gps_noise)
            new_lon += random.gauss(0, self.gps_noise)
            
            # Update state
            self.state.current_lat = new_lat
            self.state.current_lon = new_lon
            self.state.current_speed_kmh = current_speed * random.uniform(0.95, 1.05)  # Small variations
            
            # Consume battery
            segment_distance = distance / steps
            grade = self._estimate_grade(new_lat, new_lon)
            consumption = self.battery.consume_energy(
                segment_distance, 
                self.state.current_speed_kmh,
                grade
            )
            
            if not consumption['success']:
                self.emergency_stop_activated = True
                self.state.mode = RobotMode.EMERGENCY
                logger.error("Battery critical - emergency stop")
                break
            
            self.state.distance_traveled_m += segment_distance
            self.state.battery_percentage = consumption['percentage']
            self.state.last_update = time.time()
            
            # Log progress occasionally
            if step % max(1, steps // 3) == 0:
                logger.debug(f"Pos: {new_lat:.6f}, {new_lon:.6f}, Speed: {self.state.current_speed_kmh:.1f} km/h, Battery: {self.state.battery_percentage:.1f}%")
            
            # Sleep for realistic timing
            await asyncio.sleep(movement_time / steps)
        
        actual_time = time.time() - start_time
        logger.debug(f"Segment completed in {actual_time:.1f}s")
    
    async def _check_obstacles(self) -> bool:
        """Realistic obstacle detection"""
        # 2% chance of obstacle, but make it more realistic
        # based on time of day and location
        hour = datetime.now().hour
        if 8 <= hour <= 10 or 17 <= hour <= 20:  # Rush hour
            return random.random() < 0.05  # 5% chance
        else:
            return random.random() < 0.01  # 1% chance
    
    async def _handle_obstacle(self):
        """Handle detected obstacle realistically"""
        logger.info("Obstacle detected! Handling...")
        
        # Stop
        self.state.current_speed_kmh = 0
        logger.info("Robot stopped")
        
        # Wait random time (2-8 seconds) based on obstacle type
        wait_time = random.uniform(2, 8)
        logger.info(f"Waiting {wait_time:.1f} seconds for obstacle to clear")
        await asyncio.sleep(wait_time)
        
        # Resume
        logger.info("Obstacle cleared, resuming")
    
    def _calculate_path_distance(self, path: List[Tuple[float, float]]) -> float:
        """Calculate total path distance in meters"""
        total = 0
        for i in range(len(path) - 1):
            total += self._haversine_distance(path[i], path[i + 1])
        return total
    
    def _haversine_distance(self, p1: Tuple[float, float], 
                           p2: Tuple[float, float]) -> float:
        """Calculate Haversine distance between two points"""
        lat1, lon1 = p1
        lat2, lon2 = p2
        
        R = 6371000
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        
        a = math.sin(delta_phi / 2) ** 2 + \
            math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c
    
    def _estimate_grade(self, lat: float, lon: float) -> float:
        """Estimate road grade (simplified)"""
        # In production, use elevation data
        # For now, return random small grade
        return random.uniform(-3, 3)
    
    def emergency_stop(self):
        """Emergency stop robot"""
        self.emergency_stop_activated = True
        self.state.mode = RobotMode.EMERGENCY
        self.state.current_speed_kmh = 0
        logger.warning("EMERGENCY STOP ACTIVATED")
    
    def resume_mission(self):
        """Resume after emergency stop"""
        self.emergency_stop_activated = False
        if self.state.mode == RobotMode.EMERGENCY:
            self.state.mode = RobotMode.TRANSIT
        logger.info("Mission resumed")
    
    def get_state(self) -> Dict:
        """Get current robot state"""
        try:
            state_dict = self.state.to_dict()
            state_dict['battery_details'] = self.battery.to_dict()
            state_dict['robot_id'] = self.robot_id
            state_dict['is_moving'] = self.is_moving
            state_dict['path_progress'] = f"{self.state.path_index}/{len(self.path)-1 if self.path else 0}"
            state_dict['traffic_factor'] = self._get_traffic_factor()
            return state_dict
        except Exception as e:
            logger.error(f"Error getting robot state: {str(e)}")
            return {
                'robot_id': self.robot_id,
                'mode': 'ERROR',
                'error': str(e)
            }

# Add this import at the top
from datetime import datetime