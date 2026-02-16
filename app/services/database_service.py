from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError
from contextlib import contextmanager
from typing import Optional, List, Dict
import json
from datetime import datetime
import logging
from ..models.delivery import Base, Delivery, PathHistory, DeliveryStatus
from ..config import settings, logger

class DatabaseService:
    """Production database service with connection pooling"""
    
    def __init__(self):
        self.engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"check_same_thread": False} if 'sqlite' in settings.DATABASE_URL else {},
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        logger.info(f"Database initialized: {settings.DATABASE_URL}")
    
    @contextmanager
    def get_session(self) -> Session:
        """Get database session with automatic cleanup"""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"Database error: {str(e)}")
            raise
        finally:
            session.close()
    
    def create_delivery(self, pickup_lat: float, pickup_lon: float,
                       delivery_lat: float, delivery_lon: float,
                       path: list, distance_m: float, duration_min: float) -> Dict:
        """Create a new delivery record and return as dictionary"""
        with self.get_session() as session:
            delivery = Delivery(
                pickup_lat=pickup_lat,
                pickup_lon=pickup_lon,
                delivery_lat=delivery_lat,
                delivery_lon=delivery_lon,
                path_planned=json.dumps(path),
                total_distance_m=distance_m,
                estimated_duration_min=duration_min,
                status=DeliveryStatus.PENDING
            )
            session.add(delivery)
            session.flush()  # This assigns the ID without closing session
            
            # Create a dictionary with the data we need
            delivery_data = {
                'id': delivery.id,
                'pickup_lat': delivery.pickup_lat,
                'pickup_lon': delivery.pickup_lon,
                'delivery_lat': delivery.delivery_lat,
                'delivery_lon': delivery.delivery_lon,
                'status': delivery.status.value,
                'created_at': delivery.created_at.isoformat() if delivery.created_at else None,
                'total_distance_m': delivery.total_distance_m,
                'estimated_duration_min': delivery.estimated_duration_min,
                'path_planned': delivery.path_planned
            }
            
            logger.info(f"Created delivery {delivery.id}")
            return delivery_data
    
    def update_delivery_status(self, delivery_id: int, status: DeliveryStatus):
        """Update delivery status"""
        with self.get_session() as session:
            delivery = session.query(Delivery).filter(Delivery.id == delivery_id).first()
            if delivery:
                delivery.status = status
                if status == DeliveryStatus.IN_TRANSIT:
                    delivery.started_at = datetime.utcnow()
                elif status in [DeliveryStatus.COMPLETED, DeliveryStatus.DELIVERED]:
                    delivery.completed_at = datetime.utcnow()
                    if delivery.started_at:
                        delta = delivery.completed_at - delivery.started_at
                        delivery.actual_duration_min = delta.total_seconds() / 60
                session.commit()
                logger.info(f"Updated delivery {delivery_id} status to {status.value}")
    
    def add_path_point(self, delivery_id: int, lat: float, lon: float, 
                      battery: float, speed: float):
        """Add a path point to history"""
        with self.get_session() as session:
            point = PathHistory(
                delivery_id=delivery_id,
                latitude=lat,
                longitude=lon,
                battery_level=battery,
                speed_kmh=speed
            )
            session.add(point)
            session.commit()
    
    def get_delivery(self, delivery_id: int) -> Optional[Dict]:
        """Get delivery by ID"""
        with self.get_session() as session:
            delivery = session.query(Delivery).filter(Delivery.id == delivery_id).first()
            if delivery:
                return {
                    'id': delivery.id,
                    'pickup': {'lat': delivery.pickup_lat, 'lon': delivery.pickup_lon},
                    'delivery': {'lat': delivery.delivery_lat, 'lon': delivery.delivery_lon},
                    'status': delivery.status.value,
                    'created_at': delivery.created_at.isoformat() if delivery.created_at else None,
                    'started_at': delivery.started_at.isoformat() if delivery.started_at else None,
                    'completed_at': delivery.completed_at.isoformat() if delivery.completed_at else None,
                    'path_planned': delivery.path_planned,
                    'total_distance_m': delivery.total_distance_m,
                    'estimated_duration_min': delivery.estimated_duration_min,
                    'actual_duration_min': delivery.actual_duration_min
                }
            return None
    
    def get_active_deliveries(self) -> List[Dict]:
        """Get all active deliveries"""
        with self.get_session() as session:
            deliveries = session.query(Delivery).filter(
                Delivery.status.in_([
                    DeliveryStatus.PENDING,
                    DeliveryStatus.PICKUP,
                    DeliveryStatus.IN_TRANSIT,
                    DeliveryStatus.RETURNING
                ])
            ).all()
            return [{
                'id': d.id,
                'pickup': {'lat': d.pickup_lat, 'lon': d.pickup_lon},
                'delivery': {'lat': d.delivery_lat, 'lon': d.delivery_lon},
                'status': d.status.value,
                'created_at': d.created_at.isoformat() if d.created_at else None,
                'total_distance_m': d.total_distance_m,
                'estimated_duration_min': d.estimated_duration_min
            } for d in deliveries]
    
    def get_delivery_history(self, limit: int = 100) -> List[Dict]:
        """Get delivery history"""
        with self.get_session() as session:
            deliveries = session.query(Delivery).order_by(
                Delivery.created_at.desc()
            ).limit(limit).all()
            return [{
                'id': d.id,
                'pickup': {'lat': d.pickup_lat, 'lon': d.pickup_lon},
                'delivery': {'lat': d.delivery_lat, 'lon': d.delivery_lon},
                'status': d.status.value,
                'created_at': d.created_at.isoformat() if d.created_at else None,
                'total_distance_m': d.total_distance_m,
                'estimated_duration_min': d.estimated_duration_min,
                'actual_duration_min': d.actual_duration_min
            } for d in deliveries]