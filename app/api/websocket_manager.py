"""WebSocket live tracking with authentication, origin checks and connection limits."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from ..config import logger, settings


@dataclass
class _Conn:
    ws: WebSocket
    ip: str
    username: str
    expires_at: float


class ConnectionManager:
    def __init__(self) -> None:
        self._conns: Dict[int, _Conn] = {}
        self._lock = asyncio.Lock()

    def count_for_ip(self, ip: str) -> int:
        return sum(1 for c in self._conns.values() if c.ip == ip)

    async def register(self, ws: WebSocket, ip: str, username: str, expires_at: float) -> bool:
        async with self._lock:
            if len(self._conns) >= settings.WS_MAX_CONNECTIONS or self.count_for_ip(ip) >= settings.WS_MAX_CONNECTIONS_PER_IP:
                return False
            self._conns[id(ws)] = _Conn(ws, ip, username, expires_at)
            return True

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.pop(id(ws), None)

    async def broadcast(self, message: dict) -> None:
        text = json.dumps(message, separators=(",", ":"), default=str)
        now = time.time()
        dead = []
        for key, c in list(self._conns.items()):
            if c.expires_at <= now:  # session expired: drop the socket
                dead.append((key, c, 4401))
                continue
            try:
                await asyncio.wait_for(c.ws.send_text(text), timeout=2.0)
            except Exception:
                dead.append((key, c, None))
        for key, c, code in dead:
            self._conns.pop(key, None)
            if code and c.ws.client_state == WebSocketState.CONNECTED:
                try:
                    await c.ws.close(code=code)
                except Exception as exc:
                    logger.debug("close failed: %s", type(exc).__name__)

    @property
    def size(self) -> int:
        return len(self._conns)


class FleetBroadcaster:
    """Push the whole fleet snapshot to every connected dashboard once per second."""

    def __init__(self, manager: ConnectionManager, fleet) -> None:
        self.manager = manager
        self.fleet = fleet
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="ws-broadcaster")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                if self.manager.size:
                    await self.manager.broadcast({"type": "fleet", "data": self.fleet.snapshot(), "ts": time.time()})
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("broadcast loop error")
                await asyncio.sleep(1.0)
