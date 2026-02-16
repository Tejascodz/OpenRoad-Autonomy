from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, JSON, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import enum
import json

Base = declarative_base()

class DeliveryStatus(enum.Enum):
    PENDING = "pending"
    PICKUP = "pickup"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    RETURNING = "returning"
    COMPLETED = "completed"
    FAILED = "failed"

class Delivery(Base):
    __tablename__ = 'deliveries'
    
    id = Column(Integer, primary_key=True)
    pickup_lat = Column(Float, nullable=False)
    pickup_lon = Column(Float, nullable=False)
    delivery_lat = Column(Float, nullable=False)
    delivery_lon = Column(Float, nullable=False)
    status = Column(Enum(DeliveryStatus), default=DeliveryStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    path_planned = Column(JSON)  # Store path as JSON
    total_distance_m = Column(Float)
    estimated_duration_min = Column(Float)
    actual_duration_min = Column(Float, nullable=True)
    
    def to_dict(self):
        return {
            'id': self.id,
            'pickup': {'lat': self.pickup_lat, 'lon': self.pickup_lon},
            'delivery': {'lat': self.delivery_lat, 'lon': self.delivery_lon},
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'total_distance_m': self.total_distance_m,
            'estimated_duration_min': self.estimated_duration_min
        }

class PathHistory(Base):
    __tablename__ = 'path_history'
    
    id = Column(Integer, primary_key=True)
    delivery_id = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    battery_level = Column(Float)
    speed_kmh = Column(Float)