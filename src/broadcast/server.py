import json
import logging
from collections import deque
from decimal import Decimal
from enum import Enum
from typing import Any, Deque, Optional, Set

import websockets

from src.broadcast.types import WSEvent

logger = logging.getLogger("BroadcastServer")


class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Decimal and Enum types."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            # Convert Decimal to float for frontend compatibility
            return float(obj)
        if isinstance(obj, Enum):
            # Convert Enum to its value
            return obj.value
        return super(DecimalEncoder, self).default(obj)


class StatusBroadcaster:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.clients: Set[Any] = set()

        # State Cache
        self.last_config: Optional[WSEvent] = None
        self.last_info: Optional[WSEvent] = None
        self.last_summary: Optional[WSEvent] = None
        self.last_grid_state: Optional[WSEvent] = None
        self.last_market_update: Optional[WSEvent] = None

        # Order History (Keep last 50)
        self.order_history: Deque[WSEvent] = deque(maxlen=50)

        self.server: Optional[Any] = None

    async def start(self):
        logger.info(
            "Starting WebSocket status server ws://%s:%s",
            self.host,
            self.port,
        )
        self.server = await websockets.serve(self._handler, self.host, self.port)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            logger.info("WebSocket Status Server stopped.")

    async def _handler(self, websocket: Any):
        # Register Client
        self.clients.add(websocket)
        logger.info(
            "WebSocket client connected remote=%s client_count=%d",
            websocket.remote_address,
            len(self.clients),
        )

        try:
            # Send Initial State
            if self.last_config:
                await self._send_to_client(websocket, self.last_config)

            if self.last_info:
                await self._send_to_client(websocket, self.last_info)

            if self.last_summary:
                await self._send_to_client(websocket, self.last_summary)

            if self.last_grid_state:
                await self._send_to_client(websocket, self.last_grid_state)

            if self.last_market_update:
                await self._send_to_client(websocket, self.last_market_update)

            # Send History
            for event in self.order_history:
                await self._send_to_client(websocket, event)

            # Keep Connection Open and Handle Incoming
            async for _message in websocket:
                # We mostly ignore incoming messages, but could handle pings
                pass

        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            logger.info(
                "WebSocket client disconnected remote=%s client_count=%d",
                websocket.remote_address,
                len(self.clients),
            )

    async def _send_to_client(self, websocket: Any, event: WSEvent):
        try:
            msg = json.dumps(event.to_dict(), cls=DecimalEncoder)
            await websocket.send(msg)
        except Exception as e:
            self.clients.discard(websocket)
            logger.warning(
                "Failed to send event=%s to client=%s removed_client=true err=%s",
                event.event_type,
                getattr(websocket, "remote_address", "unknown"),
                e,
            )

    def send(self, event: WSEvent):
        # Update Cache based on event type
        if event.event_type == "config":
            self.last_config = event
        elif event.event_type == "info":
            self.last_info = event
        elif event.event_type in ("spot_grid_summary", "perp_grid_summary"):
            self.last_summary = event
        elif event.event_type == "grid_state":
            self.last_grid_state = event
        elif event.event_type == "market_update":
            self.last_market_update = event
        elif event.event_type == "order_update":
            self.order_history.append(event)

        # Broadcast
        if not self.clients:
            return

        message = json.dumps(event.to_dict(), cls=DecimalEncoder)

        # Create tasks for sending to all clients
        # We assume this is called from the main event loop
        # Since we can't await in a non-async method, this method assumes it's called
        # from a context where we can create tasks or sync?
        # Actually StatusBroadcaster.send in Rust is sync but spawns tasks or uses channel.
        # Javascript/Python `websockets.broadcast` is sync if we have the loop.

        clients_snapshot = tuple(self.clients)
        if not clients_snapshot:
            return

        try:
            websockets.broadcast(clients_snapshot, message)
        except Exception as e:
            logger.error(
                "Failed to broadcast event=%s clients=%d err=%s",
                event.event_type,
                len(clients_snapshot),
                e,
                exc_info=True,
            )
