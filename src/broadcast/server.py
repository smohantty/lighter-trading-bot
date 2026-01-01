import asyncio
import logging
import json
import websockets
from typing import Optional, Set, Deque
from collections import deque
from src.broadcast.types import WSEvent

logger = logging.getLogger(__name__)

class StatusBroadcaster:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        
        # State Cache
        self.last_config: Optional[WSEvent] = None
        self.last_info: Optional[WSEvent] = None
        self.last_summary: Optional[WSEvent] = None
        self.last_grid_state: Optional[WSEvent] = None
        
        # Order History (Keep last 50)
        self.order_history: Deque[WSEvent] = deque(maxlen=50)
        
        self.server = None

    async def start(self):
        logger.info(f"Starting WebSocket Status Server on ws://{self.host}:{self.port}")
        self.server = await websockets.serve(self._handler, self.host, self.port)
        
    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            logger.info("WebSocket Status Server stopped.")

    async def _handler(self, websocket: websockets.WebSocketServerProtocol):
        # Register Client
        self.clients.add(websocket)
        logger.info(f"New WebSocket client connected: {websocket.remote_address}")
        
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
            
            # Send History
            for event in self.order_history:
                await self._send_to_client(websocket, event)
                
            # Keep Connection Open and Handle Incoming
            async for message in websocket:
                # We mostly ignore incoming messages, but could handle pings
                pass
                
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.remove(websocket)
            logger.info(f"WebSocket client disconnected: {websocket.remote_address}")

    async def _send_to_client(self, websocket: websockets.WebSocketServerProtocol, event: WSEvent):
        try:
            msg = json.dumps(event.to_dict())
            await websocket.send(msg)
        except Exception as e:
            logger.error(f"Failed to send to client: {e}")

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
        elif event.event_type == "order_update":
            self.order_history.append(event)
        
        # Broadcast
        if not self.clients:
            return
            
        message = json.dumps(event.to_dict())
        
        # Create tasks for sending to all clients
        # We assume this is called from the main event loop
        # Since we can't await in a non-async method, this method assumes it's called 
        # from a context where we can create tasks or sync?
        # Actually StatusBroadcaster.send in Rust is sync but spawns tasks or uses channel.
        # Javascript/Python `websockets.broadcast` is sync if we have the loop.
        
        websockets.broadcast(self.clients, message)
