from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.schemas.device import ClientApp
from app.services.auth import get_device_by_access_token
from app.services.realtime import manager
import asyncio

router = APIRouter(tags=["realtime"])


@router.websocket("/api/v1/realtime")
async def realtime(websocket: WebSocket) -> None:
    client_app = _client_app_from_header(websocket)
    device = _device_from_authorization(websocket)
    if client_app is None or device is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    if device.client_app is not None and device.client_app != client_app.value:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if manager.connection_count(str(device.id)) >= 4 or manager.connection_count() >= 128:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    connection = manager.connect(device=device, client_app=client_app)
    incoming = asyncio.create_task(websocket.receive())
    outgoing = asyncio.create_task(connection.queue.get())
    try:
        await websocket.send_json(
            {
                "event": "connected",
                "device_id": str(device.id),
                "client_app": client_app.value,
                "role": device.current_role,
            }
        )
        while True:
            current = _device_from_authorization(websocket)
            if current is None:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            ready, _ = await asyncio.wait({incoming, outgoing}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            if incoming in ready:
                message = incoming.result()
                if message["type"] == "websocket.disconnect":
                    return
                # This channel is server-to-client; clients use authenticated HTTP for writes.
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            if outgoing not in ready:
                continue
            event = outgoing.result()
            outgoing = asyncio.create_task(connection.queue.get())
            # A token may be revoked while waiting for an event.
            current = _device_from_authorization(websocket)
            if current is None:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            if not _event_visible(current, client_app, event):
                continue
            await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        manager.disconnect(connection)
        incoming.cancel()
        outgoing.cancel()
        try:
            await asyncio.gather(incoming, outgoing, return_exceptions=True)
        except asyncio.CancelledError:
            pass


def _client_app_from_header(websocket: WebSocket) -> ClientApp | None:
    raw_value = websocket.headers.get("x-client-app")
    try:
        return ClientApp(raw_value)
    except ValueError:
        return None


def _device_from_authorization(websocket: WebSocket):
    authorization = websocket.headers.get("authorization")
    if authorization is None:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    return get_device_by_access_token(token)


def _event_visible(device, client_app, event):
    if client_app == ClientApp.EYEWITNESS:
        return True
    name = event.get("event")
    if name == "role_changed":
        return device.current_role == "CHIEF" or event.get("target_device_id") == str(device.id)
    if name == "observer_banned":
        return device.current_role == "CHIEF"
    if name in {"message_created", "message_delivered", "live_location_point", "live_location_stopped"}:
        from app.models.message import Message
        from app.services.realtime import _employee_roles_for_message
        message_id = event.get("message_id") or event.get("message", {}).get("message_id")
        message = Message.get_or_none(Message.id == message_id)
        return message is not None and device.current_role in _employee_roles_for_message(message)
    return False
