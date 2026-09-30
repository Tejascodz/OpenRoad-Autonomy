"""Process-wide singletons."""
from __future__ import annotations

from functools import lru_cache

from .api.websocket_manager import ConnectionManager, FleetBroadcaster
from .services.fleet import Fleet
from .services.map_service import MapService

fleet = Fleet()
manager = ConnectionManager()
broadcaster = FleetBroadcaster(manager, fleet)


@lru_cache(maxsize=1)
def get_map_service() -> MapService:
    return MapService()
