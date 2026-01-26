import asyncio
import json
import logging
import websockets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signaling-server")

connected_clients = {} # {session_id: {websocket: role}}
status_subscribers = {} # {session_id: set(websocket)}

async def broadcast_status(session_id, is_online):
    if session_id in status_subscribers:
        # Create a list of sockets to remove if they are closed
        to_remove = []
        message = json.dumps({
            "type": "status-update",
            "sessionId": session_id,
            "isOnline": is_online
        })
        
        for ws in status_subscribers[session_id]:
            try:
                await ws.send(message)
            except Exception:
                to_remove.append(ws)
        
        for ws in to_remove:
            status_subscribers[session_id].discard(ws)

async def handler(websocket):
    session_id = None
    role = None
    subscribed_sessions = set()
    
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

                # If host joined, notify subscribers
                if role == "host":
                    await broadcast_status(session_id, True)

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
            
            elif msg_type == "subscribe-status":
                target_ids = data.get("sessionIds", [])
                
                # Register subscription
                for sid in target_ids:
                    if sid not in status_subscribers:
                        status_subscribers[sid] = set()
                    status_subscribers[sid].add(websocket)
                    subscribed_sessions.add(sid)
                
                # Send immediate initial status
                results = {}
                for sid in target_ids:
                    is_online = False
                    if sid in connected_clients:
                        for r in connected_clients[sid].values():
                            if r == 'host':
                                is_online = True
                                break
                    results[sid] = is_online
                
                await websocket.send(json.dumps({
                    "type": "status-response",
                    "statuses": results
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
        # 1. Handle Session Disconnect (Host/Viewer leaving)
        if session_id and session_id in connected_clients:
            if websocket in connected_clients[session_id]:
                del connected_clients[session_id][websocket]
                logger.info(f"Client disconnected from session {session_id}")
                
                # If host left, notify subscribers
                if role == "host":
                    await broadcast_status(session_id, False)

            if not connected_clients[session_id]:
                del connected_clients[session_id]

        # 2. Handle Subscriber Disconnect
        for sid in subscribed_sessions:
            if sid in status_subscribers:
                status_subscribers[sid].discard(websocket)
                if not status_subscribers[sid]:
                    del status_subscribers[sid]

async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        logger.info("Signaling server started on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try: 
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
