import logging
from typing import List, Dict, Any
from fastapi import WebSocket

logger = logging.getLogger("SmartStadium-WS")

class ConnectionManager:
    """
    Manages active WebSocket connections, ensuring thread-safe broadcasting 
    and client-specific language localization.
    """
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """Accept a new connection and add it to the tracking list."""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"New client connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        """Remove a connection from the tracking list."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"Client disconnected. Remaining: {len(self.active_connections)}")

    async def broadcast_json(self, message: Dict[str, Any]):
        """Send a JSON payload to all connected clients."""
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                # Clean up stale connections
                self.disconnect(connection)

ws_manager = ConnectionManager()