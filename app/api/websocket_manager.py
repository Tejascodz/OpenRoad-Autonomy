from fastapi import WebSocket, WebSocketDisconnect
from typing import List, Dict, Set
import asyncio
import json
import logging
from ..config import logger

class ConnectionManager:
    """Manage WebSocket connections for live tracking"""
    
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.robot_subscriptions: Dict[str, str] = {}  # robot_id -> connection_id
    
    async def connect(self, websocket: WebSocket, robot_id: str = "R001"):
        await websocket.accept()
        if robot_id not in self.active_connections:
            self.active_connections[robot_id] = set()
        self.active_connections[robot_id].add(websocket)
        logger.info(f"New WebSocket connection for robot {robot_id}")
    
    def disconnect(self, websocket: WebSocket, robot_id: str):
        if robot_id in self.active_connections:
            self.active_connections[robot_id].discard(websocket)
            if not self.active_connections[robot_id]:
                del self.active_connections[robot_id]
        logger.info(f"WebSocket disconnected for robot {robot_id}")
    
    async def broadcast_robot_state(self, robot_id: str, state: dict):
        """Broadcast robot state to all subscribers"""
        if robot_id in self.active_connections:
            message = json.dumps({
                'type': 'robot_update',
                'robot_id': robot_id,
                'data': state,
                'timestamp': asyncio.get_event_loop().time()
            })
            
            disconnected = set()
            for connection in self.active_connections[robot_id]:
                try:
                    await connection.send_text(message)
                except WebSocketDisconnect:
                    disconnected.add(connection)
                except Exception as e:
                    logger.error(f"Error sending WebSocket message: {str(e)}")
                    disconnected.add(connection)
            
            # Clean up disconnected clients
            for conn in disconnected:
                self.active_connections[robot_id].discard(conn)

class RobotUpdateBroadcaster:
    """Broadcast robot updates periodically"""
    
    def __init__(self, manager: ConnectionManager, robot_controller):
        self.manager = manager
        self.robot = robot_controller
        self.broadcast_task = None
    
    async def start_broadcasting(self):
        """Start periodic state broadcasting"""
        self.broadcast_task = asyncio.create_task(self._broadcast_loop())
        logger.info("Started robot state broadcasting")
    
    async def stop_broadcasting(self):
        """Stop broadcasting"""
        if self.broadcast_task:
            self.broadcast_task.cancel()
            logger.info("Stopped robot state broadcasting")
    
    async def _broadcast_loop(self):
        """Broadcast robot state every second"""
        while True:
            try:
                state = self.robot.get_state()
                await self.manager.broadcast_robot_state(self.robot.robot_id, state)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in broadcast loop: {str(e)}")
                await asyncio.sleep(1)