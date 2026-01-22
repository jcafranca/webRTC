import asyncio
import json
import logging
import websockets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signaling-server")

connected_clients = {} # {session_id: {websocket: role}}

async def handler(websocket):
    session_id = None
    role = None
    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "join":
                session_id = data.get("sessionId")
                role = data.get("role")
                
                if session_id not in connected_clients:
                    connected_clients[session_id] = {}
                
                connected_clients[session_id][websocket] = role
                logger.info(f"Client joined session {session_id} as {role}")

                # Notify the joiner about existing peers
                peer_count = len(connected_clients[session_id])
                await websocket.send(json.dumps({
                    "type": "room_info",
                    "peer_count": peer_count,
                    "clients": list(connected_clients[session_id].values())
                }))
                
                # Notify others
                for ws, r in connected_clients[session_id].items():
                    if ws != websocket:
                        await ws.send(json.dumps({
                            "type": "peer_joined",
                            "role": role
                        }))
                
            elif session_id:
                # Intelligent Routing
                current_role = connected_clients[session_id].get(websocket)
                session = connected_clients.get(session_id, {})
                
                if current_role == "host" or current_role == "agent":
                    # Host -> All Viewers
                    for ws, r in session.items():
                        if ws != websocket and r == "viewer":
                            await ws.send(message)
                
                elif current_role == "viewer":
                    # Viewer -> Host Only
                    for ws, r in session.items():
                        if r == "host" or r == "agent":
                            await ws.send(message)
                         
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        if session_id and session_id in connected_clients:
            if websocket in connected_clients[session_id]:
                del connected_clients[session_id][websocket]
                logger.info(f"Client disconnected from session {session_id}")
            if not connected_clients[session_id]:
                del connected_clients[session_id]

async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        logger.info("Signaling server started on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try: 
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
