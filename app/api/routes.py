from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
import asyncio
import json
import traceback
from ..services.map_service import MapService
from ..services.robot_controller import RobotController
from ..services.database_service import DatabaseService
from ..api.websocket_manager import ConnectionManager, RobotUpdateBroadcaster
from ..models.delivery import DeliveryStatus
from ..config import logger

router = APIRouter()

# Request/Response models
class DeliveryRequest(BaseModel):
    pickup_lat: float = Field(..., ge=-90, le=90)
    pickup_lon: float = Field(..., ge=-180, le=180)
    delivery_lat: float = Field(..., ge=-90, le=90)
    delivery_lon: float = Field(..., ge=-180, le=180)
    algorithm: str = Field("astar", pattern="^(astar|dijkstra)$")

class DeliveryResponse(BaseModel):
    success: bool
    delivery_id: Optional[int] = None
    message: str
    distance_m: Optional[float] = None
    duration_min: Optional[float] = None
    path_points: Optional[int] = None

class RobotStatusResponse(BaseModel):
    robot_id: str
    mode: str
    position: Dict[str, float]
    battery: float
    speed: float
    delivery_id: Optional[int]
    obstacle_detected: bool
    battery_details: Dict

# Global instances
map_service = MapService(use_google_maps=False)
robot = RobotController()
db = DatabaseService()
manager = ConnectionManager()
broadcaster = RobotUpdateBroadcaster(manager, robot)

@router.on_event("startup")
async def startup_event():
    """Start background tasks on server startup"""
    try:
        asyncio.create_task(broadcaster.start_broadcasting())
        logger.info("Server startup complete - Robot state broadcasting started")
    except Exception as e:
        logger.error(f"Error during startup: {str(e)}")

@router.on_event("shutdown")
async def shutdown_event():
    """Cleanup on server shutdown"""
    try:
        await broadcaster.stop_broadcasting()
        logger.info("Server shutdown complete")
    except Exception as e:
        logger.error(f"Error during shutdown: {str(e)}")

@router.post("/start_delivery", response_model=DeliveryResponse)
async def start_delivery(request: DeliveryRequest):
    """Start a new delivery mission"""
    logger.info("=" * 50)
    logger.info("START DELIVERY REQUEST RECEIVED")
    logger.info(f"Pickup: {request.pickup_lat}, {request.pickup_lon}")
    logger.info(f"Delivery: {request.delivery_lat}, {request.delivery_lon}")
    logger.info(f"Algorithm: {request.algorithm}")
    logger.info("=" * 50)
    
    try:
        # Step 1: Fetch road network
        logger.info("Step 1: Fetching road network...")
        try:
            map_service.fetch_road_network(
                request.pickup_lat, 
                request.pickup_lon, 
                radius_m=10000
            )
            logger.info("Road network fetched successfully")
        except Exception as e:
            logger.error(f"Failed to fetch road network: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Failed to fetch road network: {str(e)}")
        
        # Step 2: Get route
        logger.info("Step 2: Calculating route...")
        try:
            path, distance, duration = map_service.get_route(
                request.pickup_lat, request.pickup_lon,
                request.delivery_lat, request.delivery_lon,
                algorithm=request.algorithm
            )
            logger.info(f"Route calculated: {len(path)} points, {distance:.2f}m, {duration:.2f}min")
        except Exception as e:
            logger.error(f"Failed to calculate route: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Failed to calculate route: {str(e)}")
        
        # Step 3: Create database record - FIXED: Now returns dict, not object
        logger.info("Step 3: Creating database record...")
        try:
            delivery_data = db.create_delivery(
                request.pickup_lat, request.pickup_lon,
                request.delivery_lat, request.delivery_lon,
                path, distance, duration
            )
            delivery_id = delivery_data['id']  # Get ID from dict
            logger.info(f"Database record created with ID: {delivery_id}")
        except Exception as e:
            logger.error(f"Failed to create database record: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
        
        # Step 4: Start robot
        logger.info("Step 4: Starting robot delivery...")
        try:
            result = await robot.start_delivery(
                (request.pickup_lat, request.pickup_lon),
                (request.delivery_lat, request.delivery_lon),
                path
            )
            logger.info(f"Robot start result: {result}")
        except Exception as e:
            logger.error(f"Failed to start robot: {str(e)}")
            logger.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Robot error: {str(e)}")
        
        if result['success']:
            robot.state.current_delivery_id = delivery_id
            try:
                db.update_delivery_status(delivery_id, DeliveryStatus.IN_TRANSIT)
                logger.info(f"Delivery status updated to IN_TRANSIT")
            except Exception as e:
                logger.error(f"Failed to update delivery status: {str(e)}")
            
            logger.info("=" * 50)
            logger.info("DELIVERY STARTED SUCCESSFULLY")
            logger.info("=" * 50)
            
            return DeliveryResponse(
                success=True,
                delivery_id=delivery_id,
                message="Delivery started",
                distance_m=distance,
                duration_min=duration,
                path_points=len(path)
            )
        else:
            error_msg = result.get('error', 'Unknown error')
            logger.error(f"Robot start failed: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in start_delivery: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/robot_status", response_model=RobotStatusResponse)
async def get_robot_status():
    """Get current robot status"""
    try:
        state = robot.get_state()
        logger.debug(f"Robot status requested: {state['mode']}")
        return state
    except Exception as e:
        logger.error(f"Error getting robot status: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get robot status")

@router.post("/robot/emergency_stop")
async def emergency_stop():
    """Emergency stop the robot"""
    try:
        logger.warning("EMERGENCY STOP ACTIVATED")
        robot.emergency_stop()
        return {"success": True, "message": "Emergency stop activated"}
    except Exception as e:
        logger.error(f"Error during emergency stop: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to execute emergency stop")

@router.post("/robot/resume")
async def resume_mission():
    """Resume robot mission"""
    try:
        logger.info("Resuming mission")
        robot.resume_mission()
        return {"success": True, "message": "Mission resumed"}
    except Exception as e:
        logger.error(f"Error resuming mission: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to resume mission")

@router.get("/deliveries/active")
async def get_active_deliveries():
    """Get all active deliveries"""
    try:
        deliveries = db.get_active_deliveries()
        return deliveries
    except Exception as e:
        logger.error(f"Error fetching active deliveries: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch active deliveries")

@router.get("/deliveries/history")
async def get_delivery_history(limit: int = 100):
    """Get delivery history"""
    try:
        deliveries = db.get_delivery_history(limit)
        return deliveries
    except Exception as e:
        logger.error(f"Error fetching delivery history: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch delivery history")

@router.get("/deliveries/{delivery_id}")
async def get_delivery(delivery_id: int):
    """Get specific delivery details"""
    try:
        delivery = db.get_delivery(delivery_id)
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        return delivery
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching delivery {delivery_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch delivery details")

@router.websocket("/ws/{robot_id}")
async def websocket_endpoint(websocket: WebSocket, robot_id: str):
    """WebSocket endpoint for live tracking"""
    logger.info(f"WebSocket connection request for robot {robot_id}")
    await manager.connect(websocket, robot_id)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get('type') == 'ping':
                await websocket.send_text(json.dumps({'type': 'pong'}))
                logger.debug(f"Ping-pong with robot {robot_id}")
            elif message.get('type') == 'request_state':
                state = robot.get_state()
                await websocket.send_text(json.dumps({
                    'type': 'state_update',
                    'data': state
                }))
                logger.debug(f"Sent state update to robot {robot_id}")
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for robot {robot_id}")
        manager.disconnect(websocket, robot_id)
    except Exception as e:
        logger.error(f"WebSocket error for robot {robot_id}: {str(e)}")
        logger.error(traceback.format_exc())
        manager.disconnect(websocket, robot_id)