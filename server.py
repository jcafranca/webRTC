import asyncio
import json
import logging
import websockets
import time
import urllib.request
import urllib.parse
import re
import ipaddress

import os
import sys

# ------------------------------
# Data Persistence
# ------------------------------
def get_data_dir():
    """Get the path to the data directory in AppData"""
    if os.name == "nt":
        app_data = os.getenv("APPDATA")
        data_dir = os.path.join(app_data, "RemoteDesktop")
    else:
        home = os.path.expanduser("~")
        data_dir = os.path.join(home, ".config", "remote-desktop")

    os.makedirs(data_dir, exist_ok=True)
    return data_dir


HOSTS_FILE = os.path.join(get_data_dir(), "hosts.json")


def load_persisted_hosts():
    """Load hosts from JSON file"""
    if os.path.exists(HOSTS_FILE):
        try:
            with open(HOSTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            if "logger" in globals():
                logger.error(f"Failed to load persisted hosts: {e}")
            else:
                print(f"Failed to load persisted hosts: {e}")
    return {}


def save_persisted_hosts(hosts_data):
    """Save hosts to JSON file"""
    try:
        log_file = os.path.join(get_data_dir(), "connected_hosts.json")
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(hosts_data, f, indent=4)

        with open(HOSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(hosts_data, f, indent=4)
    except Exception as e:
        if "logger" in globals():
            logger.error(f"Failed to save hosts: {e}")
        else:
            print(f"Failed to save hosts: {e}")


persisted_hosts = load_persisted_hosts()

# ------------------------------
# Logging
# ------------------------------
def get_log_file():
    """Get the path to the log file in AppData"""
    if os.name == "nt":
        app_data = os.getenv("APPDATA")
        log_dir = os.path.join(app_data, "RemoteDesktop")
    else:
        home = os.path.expanduser("~")
        log_dir = os.path.join(home, ".config", "remote-desktop")

    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "server.log")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(get_log_file(), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("signaling-server")

# Suppress noisy handshake errors from websockets
logging.getLogger("websockets").setLevel(logging.CRITICAL)
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
logging.getLogger("websockets.protocol").setLevel(logging.CRITICAL)
logging.getLogger("websockets.asyncio.server").setLevel(logging.CRITICAL)

# ------------------------------
# License Management
# ------------------------------
VALID_LICENSE_KEYS = {
    "OMNIDESK-LITE-2025",
    "SECRET-KEY-123",
    "DEV-LICENSE-001",
}

connected_clients = {}   # {session_id: {websocket: {role, clientId, ...}}}
status_subscribers = {}  # {session_id: set(websocket)}
peer_last_seen = {}      # {session_id: {"lastSeen": timestamp, "computerName": name, ...}}

# ------------------------------
# Helpers
# ------------------------------
def is_local_ip(ip: str) -> bool:
    """
    Check whether IP should be treated as local/internal.
    Keeps your server public IP special-cased as local if needed.
    """
    if not ip:
        return True

    if ip in ("127.0.0.1", "::1", "210.213.193.4"):
        return True

    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


def get_request_headers(websocket):
    """
    Supports multiple websockets versions.
    """
    headers = getattr(websocket, "request_headers", None)
    if headers is not None:
        return headers

    request_obj = getattr(websocket, "request", None)
    if request_obj is not None:
        req_headers = getattr(request_obj, "headers", None)
        if req_headers is not None:
            return req_headers

    return {}


def get_client_ip(websocket) -> str:
    """
    Get real client IP behind NGINX if available.
    Falls back to websocket.remote_address.
    """
    headers = get_request_headers(websocket)

    xff = headers.get("X-Forwarded-For") if headers else None
    if xff:
        return xff.split(",")[0].strip()

    xri = headers.get("X-Real-IP") if headers else None
    if xri:
        return xri.strip()

    remote = getattr(websocket, "remote_address", None)
    if isinstance(remote, tuple) and len(remote) > 0:
        return remote[0]

    return "127.0.0.1"


# ------------------------------
# Geolocation Helper
# ------------------------------
async def get_location(ip):
    """Fetch geolocation data for a given IP address"""
    if is_local_ip(ip):
        return {
            "city": "Local",
            "regionName": "Network",
            "country": "Same Location",
            "lat": 0,
            "lon": 0,
            "status": "success",
            "as": "Internal",
            "isLocal": True,
        }

    try:
        url = f"http://ip-api.com/json/{ip}"

        def fetch():
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.loads(response.read().decode())

        data = await asyncio.to_thread(fetch)
        return data
    except Exception as e:
        logger.warning(f"Failed to get location for {ip}: {e}")
        return {"status": "fail", "message": str(e)}


def format_location_string(data):
    if not data:
        return "Unknown Location"

    if data.get("status") == "success":
        parts = [
            p for p in [
                data.get("city"),
                data.get("regionName"),
                data.get("zip"),
                data.get("country"),
            ] if p
        ]
        return ", ".join(parts) if parts else "Unknown Location"

    if "lat" in data and "lon" in data:
        parts = [
            p for p in [
                data.get("city"),
                data.get("regionName"),
                data.get("country"),
            ] if p
        ]
        if not parts:
            return f"Coords: {data['lat']:.2f}, {data['lon']:.2f}"
        return ", ".join(parts)

    return "Unknown Location"


# ------------------------------
# Safe send helper
# ------------------------------
async def safe_send(ws, message, timeout=0.5):
    try:
        await asyncio.wait_for(ws.send(message), timeout=timeout)
    except Exception:
        pass


# ------------------------------
# Broadcast host status
# ------------------------------
async def broadcast_status(session_id, is_online, computer_name=None, location_data=None, ip=None):
    current_time = time.time()

    if session_id not in peer_last_seen:
        peer_last_seen[session_id] = {}

    peer_last_seen[session_id]["lastSeen"] = current_time
    if computer_name:
        peer_last_seen[session_id]["computerName"] = computer_name
    if location_data:
        peer_last_seen[session_id]["locationData"] = location_data
    if ip:
        peer_last_seen[session_id]["ip"] = ip

    if session_id in status_subscribers:
        stored_loc = peer_last_seen[session_id].get("locationData", {})
        message = json.dumps({
            "type": "status-update",
            "sessionId": session_id,
            "isOnline": is_online,
            "lastSeen": current_time,
            "computerName": computer_name or peer_last_seen[session_id].get("computerName"),
            "location": format_location_string(stored_loc),
            "lat": stored_loc.get("lat", 0),
            "lon": stored_loc.get("lon", 0),
            "ip": ip or peer_last_seen[session_id].get("ip"),
        })

        subscribers = set(status_subscribers[session_id])
        if "GLOBAL_STATUS" in status_subscribers:
            subscribers.update(status_subscribers["GLOBAL_STATUS"])

        await asyncio.gather(
            *(safe_send(ws, message) for ws in subscribers),
            return_exceptions=True,
        )


# ------------------------------
# Notify host of client list changes
# ------------------------------
async def notify_host_of_clients(session_id):
    if session_id not in connected_clients:
        return

    session = connected_clients[session_id]
    hosts = [ws for ws, info in session.items() if info["role"] == "host"]
    if not hosts:
        return

    host_info = next((info for info in session.values() if info["role"] == "host"), {})
    host_loc_data = host_info.get("locationData", {})

    clients_list = []
    for ws, info in session.items():
        if info["role"] == "host":
            continue

        c_loc = info.get("location", "Unknown")
        c_lat = info.get("locationData", {}).get("lat", 0)
        c_lon = info.get("locationData", {}).get("lon", 0)

        if info.get("locationData", {}).get("isLocal"):
            c_lat = host_loc_data.get("lat", 0)
            c_lon = host_loc_data.get("lon", 0)
            c_loc = f"Local ({host_info.get('location', 'Same Network')})"

        clients_list.append({
            "clientId": info["clientId"],
            "role": info["role"],
            "computerName": info.get("computerName"),
            "location": c_loc,
            "lat": c_lat,
            "lon": c_lon,
            "connectedAt": info.get("connectedAt", time.time()),
        })

    message = json.dumps({
        "type": "client-list-update",
        "sessionId": session_id,
        "clients": clients_list,
        "peer_count": len(clients_list),
    })

    targets = list(hosts)

    if session_id in status_subscribers:
        targets.extend(list(status_subscribers[session_id]))

    if "GLOBAL_STATUS" in status_subscribers:
        targets.extend(list(status_subscribers["GLOBAL_STATUS"]))

    if targets:
        await asyncio.gather(
            *(safe_send(ws, message) for ws in set(targets)),
            return_exceptions=True,
        )


# ------------------------------
# Dashboard Data Helpers
# ------------------------------
def get_all_hosts_data():
    """Merge active hosts with persisted hosts for dashboard view"""
    active_sids = set()
    all_hosts_result = []
    current_time = time.time()

    for sid, clients in connected_clients.items():
        host_ws = next((ws for ws, info in clients.items() if info["role"] == "host"), None)
        if not host_ws:
            continue

        active_sids.add(sid)
        info = clients[host_ws]
        loc_data = info.get("locationData", {})
        peer_count = len([ws for ws in clients if ws != host_ws])

        clients_list = []
        for c_ws, c_info in clients.items():
            if c_info["role"] == "host":
                continue

            c_loc = c_info.get("location", "Unknown")
            c_lat = c_info.get("locationData", {}).get("lat", 0)
            c_lon = c_info.get("locationData", {}).get("lon", 0)

            if c_info.get("locationData", {}).get("isLocal"):
                c_lat = loc_data.get("lat", 0)
                c_lon = loc_data.get("lon", 0)
                c_loc = f"Local ({info.get('location', 'Same Network')})"

            clients_list.append({
                "clientId": c_info["clientId"],
                "role": c_info["role"],
                "computerName": c_info.get("computerName"),
                "location": c_loc,
                "lat": c_lat,
                "lon": c_lon,
                "connectedAt": c_info.get("connectedAt", current_time),
            })

        host_entry = {
            "id": sid,
            "name": info.get("computerName", "Unknown Host"),
            "ip": info.get("ip", "0.0.0.0"),
            "status": "online",
            "clients": peer_count,
            "connectedClients": clients_list,
            "location": info.get("location", "Unknown"),
            "lat": loc_data.get("lat", 0),
            "lon": loc_data.get("lon", 0),
            "lastSeen": current_time,
        }
        all_hosts_result.append(host_entry)

        persisted_hosts[sid] = {
            "name": host_entry["name"],
            "ip": host_entry["ip"],
            "location": host_entry["location"],
            "lat": host_entry["lat"],
            "lon": host_entry["lon"],
            "lastSeen": current_time,
        }

    for sid, p_info in persisted_hosts.items():
        if sid not in active_sids:
            all_hosts_result.append({
                "id": sid,
                "name": p_info.get("name", "Unknown Host"),
                "ip": p_info.get("ip", "0.0.0.0"),
                "status": "offline",
                "clients": 0,
                "connectedClients": [],
                "location": p_info.get("location", "Unknown"),
                "lat": p_info.get("lat", 0),
                "lon": p_info.get("lon", 0),
                "lastSeen": p_info.get("lastSeen", 0),
            })

    return all_hosts_result


# ------------------------------
# Non-websocket request handler
# ------------------------------
async def process_request(path, request_headers):
    """
    Handle non-websocket requests.
    Returns HTTP response tuple, or None to continue WebSocket handshake.
    """
    if path == "/":
        return (200, [("Content-Type", "text/plain")], b"Signaling Server Online\n")
    return None


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
                if session_id:
                    session_id = urllib.parse.unquote(str(session_id)).strip()
                    session_id = session_id.replace("+", " ").replace("_", " ").replace("-", " ")
                    digits = re.findall(r"\d", session_id)
                    if len(digits) == 9:
                        session_id = f"{''.join(digits[0:3])} {''.join(digits[3:6])} {''.join(digits[6:9])}"

                role = data.get("role")
                client_id = data.get("senderId", "unknown")

                if role in ("host", "agent"):
                    license_key = data.get("licenseKey")
                    if license_key not in VALID_LICENSE_KEYS:
                        logger.warning(
                            f"Connection rejected for {role} (ID: {client_id}): Invalid license '{license_key}'"
                        )
                        await safe_send(websocket, json.dumps({
                            "type": "error",
                            "message": "Authentication Failed: Invalid or missing license key. Access denied."
                        }))
                        await asyncio.sleep(0.5)
                        return

                if session_id not in connected_clients:
                    connected_clients[session_id] = {}

                client_ip = get_client_ip(websocket)
                is_ip_local = is_local_ip(client_ip)

                location_data = data.get("locationData")
                if not location_data:
                    location_data = await get_location(client_ip)
                else:
                    logger.info(f"Using client-provided location data for {client_id}")
                    if is_ip_local:
                        location_data["isLocal"] = True

                connected_clients[session_id][websocket] = {
                    "role": role,
                    "clientId": client_id,
                    "computerName": data.get("computerName"),
                    "location": format_location_string(location_data),
                    "locationData": location_data,
                    "ip": client_ip,
                    "connectedAt": time.time(),
                    "ipAddresses": data.get("ipAddresses", []),
                }

                if role == "host":
                    comp_name = data.get("computerName") or "Unknown Host"
                    logger.info(f"Host Joined | Session: {session_id} | Computer Name: {comp_name} | ID: {client_id}")
                else:
                    logger.info(f"Client Joined | Session: {session_id} | Role: {role} | ID: {client_id}")

                asyncio.create_task(notify_host_of_clients(session_id))

                if role == "host":
                    computer_name = data.get("computerName") or "Unknown Host"
                    asyncio.create_task(
                        broadcast_status(session_id, True, computer_name, location_data, client_ip)
                    )

                    now = time.time()
                    formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

                    persisted_hosts[session_id] = {
                        "name": computer_name,
                        "ip": client_ip,
                        "location": format_location_string(location_data),
                        "lat": location_data.get("lat", 0),
                        "lon": location_data.get("lon", 0),
                        "lastSeen": now,
                        "connectedAt": formatted_time,
                        "disconnectedAt": None,
                        "ipAddresses": data.get("ipAddresses", []),
                    }
                    save_persisted_hosts(persisted_hosts)

                clients_list = list(connected_clients[session_id].values())
                peer_count = len(connected_clients[session_id])

                await safe_send(websocket, json.dumps({
                    "type": "room_info",
                    "peer_count": peer_count,
                    "clients": clients_list,
                }))

                others = [ws for ws in connected_clients[session_id] if ws != websocket]
                await asyncio.gather(
                    *(safe_send(ws, json.dumps({
                        "type": "peer_joined",
                        "role": role,
                        "clientId": client_id,
                        "computerName": data.get("computerName"),
                    })) for ws in others),
                    return_exceptions=True,
                )

            # --------------------------
            # Subscribe to status updates
            # --------------------------
            elif msg_type == "subscribe-status":
                target_ids = data.get("sessionIds", [])
                is_all_hosts_subscriber = data.get("allHosts", False)

                for sid in target_ids:
                    if sid not in status_subscribers:
                        status_subscribers[sid] = set()
                    status_subscribers[sid].add(websocket)
                    subscribed_sessions.add(sid)

                if is_all_hosts_subscriber:
                    if "GLOBAL_STATUS" not in status_subscribers:
                        status_subscribers["GLOBAL_STATUS"] = set()
                    status_subscribers["GLOBAL_STATUS"].add(websocket)
                    subscribed_sessions.add("GLOBAL_STATUS")

                results = {}
                last_seen_times = {}
                computer_names = {}
                locations = {}
                lats = {}
                lons = {}
                ips = {}

                for sid in target_ids:
                    host_info = next(
                        (info for info in connected_clients.get(sid, {}).values() if info["role"] == "host"),
                        None,
                    )
                    is_online = host_info is not None
                    results[sid] = is_online

                    stored_info = peer_last_seen.get(sid, {})
                    persisted_info = persisted_hosts.get(sid, {})

                    last_seen_times[sid] = (
                        time.time() if is_online
                        else (stored_info.get("lastSeen") or persisted_info.get("lastSeen") or 0)
                    )

                    if is_online and host_info:
                        computer_names[sid] = host_info.get("computerName")
                        locations[sid] = host_info.get("location")
                        loc_data = host_info.get("locationData", {})
                        lats[sid] = loc_data.get("lat", 0)
                        lons[sid] = loc_data.get("lon", 0)
                        ips[sid] = host_info.get("ip")
                    else:
                        computer_names[sid] = stored_info.get("computerName") or persisted_info.get("name")
                        loc_data = stored_info.get("locationData") or {
                            "lat": persisted_info.get("lat", 0),
                            "lon": persisted_info.get("lon", 0),
                        }
                        locations[sid] = (
                            format_location_string(loc_data)
                            if stored_info.get("locationData")
                            else persisted_info.get("location")
                        )
                        lats[sid] = loc_data.get("lat", 0)
                        lons[sid] = loc_data.get("lon", 0)
                        ips[sid] = stored_info.get("ip") or persisted_info.get("ip")

                await safe_send(websocket, json.dumps({
                    "type": "status-response",
                    "statuses": results,
                    "lastSeen": last_seen_times,
                    "computerNames": computer_names,
                    "locations": locations,
                    "lats": lats,
                    "lons": lons,
                    "ips": ips,
                }))

            # --------------------------
            # Get all active hosts
            # --------------------------
            elif msg_type == "get-all-hosts":
                all_hosts = get_all_hosts_data()
                await safe_send(websocket, json.dumps({
                    "type": "hosts-list",
                    "hosts": all_hosts,
                }))

            # --------------------------
            # Admin Disconnect Request
            # --------------------------
            elif msg_type == "disconnect-client":
                target_session_id = data.get("sessionId")
                target_client_id = data.get("clientId")

                logger.info(f"Admin request: Disconnect client {target_client_id} from session {target_session_id}")

                if target_session_id in connected_clients:
                    client_ws = next(
                        (ws for ws, info in connected_clients[target_session_id].items()
                         if info["clientId"] == target_client_id),
                        None,
                    )
                    if client_ws:
                        logger.info(f"Found client {target_client_id}. Closing connection.")
                        await client_ws.close(code=1000, reason="Disconnected by administrator")
                    else:
                        logger.warning(f"Client {target_client_id} not found in session {target_session_id}")
                else:
                    logger.warning(f"Session {target_session_id} not found")

            # --------------------------
            # Metrics Update
            # --------------------------
            elif msg_type == "metric-update":
                if session_id:
                    subscribers = set()
                    if session_id in status_subscribers:
                        subscribers.update(status_subscribers[session_id])
                    if "GLOBAL_STATUS" in status_subscribers:
                        subscribers.update(status_subscribers["GLOBAL_STATUS"])

                    if subscribers:
                        message = json.dumps({
                            "type": "metric-update",
                            "sessionId": session_id,
                            "metrics": data.get("metrics", {}),
                        })
                        await asyncio.gather(
                            *(safe_send(ws, message) for ws in subscribers),
                            return_exceptions=True,
                        )

            # --------------------------
            # Explicit Leave
            # --------------------------
            elif msg_type == "leave":
                logger.info(f"Client explicitly requested to leave session {session_id}")
                break

            # --------------------------
            # Route session messages
            # --------------------------
            elif session_id:
                client_info = connected_clients.get(session_id, {}).get(websocket)
                if not client_info:
                    continue

                current_role = client_info["role"]
                current_client_id = client_info["clientId"]
                session = connected_clients.get(session_id, {})
                target_id = data.get("targetId")

                data["senderId"] = current_client_id
                relayed_message = json.dumps(data)

                if target_id:
                    for ws, info in session.items():
                        if info.get("clientId") == target_id:
                            await safe_send(ws, relayed_message)
                            break
                else:
                    if current_role in ("host", "agent"):
                        viewers = [
                            ws for ws, info in session.items()
                            if info["role"] == "viewer" and ws != websocket
                        ]
                        await asyncio.gather(
                            *(safe_send(ws, relayed_message) for ws in viewers),
                            return_exceptions=True,
                        )
                    elif current_role == "viewer":
                        hosts_agents = [
                            ws for ws, info in session.items()
                            if info["role"] in ("host", "agent")
                        ]
                        await asyncio.gather(
                            *(safe_send(ws, relayed_message) for ws in hosts_agents),
                            return_exceptions=True,
                        )

    except Exception as e:
        logger.error(f"Error: {e}")

    finally:
        if session_id and session_id in connected_clients:
            if websocket in connected_clients[session_id]:
                del connected_clients[session_id][websocket]
                logger.info(f"Client Disconnected | Session: {session_id}")

                asyncio.create_task(notify_host_of_clients(session_id))

                if role == "host":
                    if session_id in persisted_hosts:
                        now = time.time()
                        formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                        persisted_hosts[session_id]["lastSeen"] = now
                        persisted_hosts[session_id]["disconnectedAt"] = formatted_time
                        save_persisted_hosts(persisted_hosts)

                    remaining_hosts = any(
                        info["role"] == "host"
                        for info in connected_clients.get(session_id, {}).values()
                    )

                    if remaining_hosts:
                        logger.info(
                            f"Host disconnected, but session {session_id} remains ONLINE (other active host detected)"
                        )
                        remaining_host_info = next(
                            (info for info in connected_clients.get(session_id, {}).values()
                             if info["role"] == "host"),
                            {},
                        )
                        remaining_ip = remaining_host_info.get("ip")
                        remaining_name = remaining_host_info.get("computerName")
                        remaining_loc = remaining_host_info.get("locationData")
                        asyncio.create_task(
                            broadcast_status(
                                session_id,
                                True,
                                computer_name=remaining_name,
                                location_data=remaining_loc,
                                ip=remaining_ip,
                            )
                        )
                    else:
                        asyncio.create_task(broadcast_status(session_id, False))

            if session_id in connected_clients and not connected_clients[session_id]:
                del connected_clients[session_id]

        for sid in list(subscribed_sessions):
            if sid in status_subscribers:
                status_subscribers[sid].discard(websocket)
                if not status_subscribers[sid]:
                    del status_subscribers[sid]


# ------------------------------
# Dashboard Background Tasks
# ------------------------------
async def dashboard_broadcaster():
    """Periodically send full host list to all dashboard subscribers"""
    while True:
        try:
            all_hosts = get_all_hosts_data()
            dashboard_clients = set()

            for subscribers in status_subscribers.values():
                dashboard_clients.update(subscribers)

            if dashboard_clients:
                message = json.dumps({
                    "type": "hosts-list",
                    "hosts": all_hosts,
                })
                await asyncio.gather(
                    *(safe_send(ws, message) for ws in dashboard_clients),
                    return_exceptions=True,
                )
        except Exception as e:
            logger.error(f"Error in dashboard_broadcaster: {e}")

        await asyncio.sleep(2)


# ------------------------------
# Start server
# ------------------------------
async def main():
    asyncio.create_task(dashboard_broadcaster())

    async with websockets.serve(
        handler,
        "127.0.0.1",
        8765,
        process_request=process_request
    ):
        logger.info("Signaling server started on ws://127.0.0.1:8765")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
