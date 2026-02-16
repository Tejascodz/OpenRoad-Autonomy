import os
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from typing import Optional
import logging

load_dotenv()

class Settings(BaseSettings):
    # API Keys
    GOOGLE_MAPS_API_KEY: Optional[str] = os.getenv("GOOGLE_MAPS_API_KEY")
    OSM_USER_AGENT: str = "DeliveryRobot/1.0"
    
    # Robot Configuration
    ROBOT_SPEED_KMH: float = 15.0  # Average speed
    ROBOT_MAX_SPEED_KMH: float = 25.0
    BATTERY_CAPACITY_KWH: float = 2.5  # 2.5 kWh battery
    BATTERY_CONSUMPTION_KWH_PER_KM: float = 0.08  # 80 Wh/km
    MAX_RANGE_KM: float = 30.0
    
    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    RELOAD: bool = False
    
    # Database
    DATABASE_URL: str = "sqlite:///./data/deliveries.db"
    
    # Redis (for production scaling)
    REDIS_URL: Optional[str] = os.getenv("REDIS_URL")
    
    # Logging
    LOG_LEVEL: str = "INFO"
    
    class Config:
        env_file = ".env"

settings = Settings()

# Logging configuration
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/robot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)