import asyncio
import json
import logging
import websockets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signaling-server")

connected_clients = {}  # {session_id: {websocket: {role, clientId}}}
status_subscribers = {}  # {session_id: set(websocket)}

# ------------------------------
# Safe send helper
# ------------------------------
async def safe_send(ws, message, timeout=0.2):
    try:
        await asyncio.wait_for(ws.send(message), timeout=timeout)
    except Exception:
        pass

# ------------------------------
# Broadcast host status
# ------------------------------
async def broadcast_status(session_id, is_online):
    if session_id in status_subscribers:
        message = json.dumps({
            "type": "status-update",
            "sessionId": session_id,
            "isOnline": is_online
        })
        subscribers = list(status_subscribers[session_id])
        await asyncio.gather(
            *(safe_send(ws, message) for ws in subscribers),
            return_exceptions=True
        )

# ------------------------------
# Main websocket handler
# ------------------------------
async def handler(websocket):
    session_id = None
    role = None
    subscribed_sessions = set()
    
    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")

            # --------------------------
            # Client joins a session
            # --------------------------
            if msg_type == "join":
                session_id = data.get("sessionId")
                role = data.get("role")
                client_id = data.get("senderId", "unknown")

                if session_id not in connected_clients:
                    connected_clients[session_id] = {}

                connected_clients[session_id][websocket] = {
                    "role": role,
                    "clientId": client_id
                }
                logger.info(f"Client joined session {session_id} as {role} (ID: {client_id})")

                # Notify subscribers if host
                if role == "host":
                    asyncio.create_task(broadcast_status(session_id, True))

                # Send room info to joiner
                clients_list = list(connected_clients[session_id].values())
                peer_count = len(connected_clients[session_id])
                await safe_send(websocket, json.dumps({
                    "type": "room_info",
                    "peer_count": peer_count,
                    "clients": clients_list
                }))

                # Notify other peers
                others = [ws for ws in connected_clients[session_id] if ws != websocket]
                await asyncio.gather(
                    *(safe_send(ws, json.dumps({
                        "type": "peer_joined",
                        "role": role,
                        "clientId": client_id
                    })) for ws in others),
                    return_exceptions=True
                )

            # --------------------------
            # Subscribe to status updates
            # --------------------------
            elif msg_type == "subscribe-status":
                target_ids = data.get("sessionIds", [])

                for sid in target_ids:
                    if sid not in status_subscribers:
                        status_subscribers[sid] = set()
                    status_subscribers[sid].add(websocket)
                    subscribed_sessions.add(sid)

                # Send initial status immediately
                results = {}
                for sid in target_ids:
                    is_online = any(
                        client_info["role"] == "host"
                        for client_info in connected_clients.get(sid, {}).values()
                    )
                    results[sid] = is_online

                await safe_send(websocket, json.dumps({
                    "type": "status-response",
                    "statuses": results
                }))

            # --------------------------
            # Routing messages intelligently
            # --------------------------
            elif session_id:
                client_info = connected_clients[session_id].get(websocket)
                if not client_info:
                    continue

                current_role = client_info["role"]
                session = connected_clients.get(session_id, {})
                target_id = data.get("targetId")

                # Targeted message
                if target_id:
                    for ws, info in session.items():
                        if info.get("clientId") == target_id:
                            await safe_send(ws, message)
                            break
                else:
                    # Broadcast logic
                    if current_role in ("host", "agent"):
                        viewers = [ws for ws, info in session.items() if info["role"] == "viewer" and ws != websocket]
                        await asyncio.gather(
                            *(safe_send(ws, message) for ws in viewers),
                            return_exceptions=True
                        )
                    elif current_role == "viewer":
                        hosts_agents = [ws for ws, info in session.items() if info["role"] in ("host", "agent")]
                        await asyncio.gather(
                            *(safe_send(ws, message) for ws in hosts_agents),
                            return_exceptions=True
                        )

    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        # --------------------------
        # Cleanup on disconnect
        # --------------------------
        if session_id and session_id in connected_clients:
            if websocket in connected_clients[session_id]:
                del connected_clients[session_id][websocket]
                logger.info(f"Client disconnected from session {session_id}")

                if role == "host":
                    asyncio.create_task(broadcast_status(session_id, False))

            if not connected_clients[session_id]:
                del connected_clients[session_id]

        for sid in subscribed_sessions:
            if sid in status_subscribers:
                status_subscribers[sid].discard(websocket)
                if not status_subscribers[sid]:
                    del status_subscribers[sid]

# ------------------------------
# Start server
# ------------------------------
async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        logger.info("Signaling server started on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try: 
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
