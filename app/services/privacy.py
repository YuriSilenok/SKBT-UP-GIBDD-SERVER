from datetime import UTC, timedelta
from uuid import UUID
from fastapi import HTTPException
from app.db.database import database_proxy
from app.models.device import Device, utc_now
from app.models.message import Message
from app.models.media import Media
from app.models.static_location import StaticLocation
from app.models.location_point import LocationPoint
from app.models.live_location_session import LiveLocationSession
from app.models.privacy import ConsentEvent, Receipt

CONSENT_VERSION = "2026-11-17-v1"
RETENTION = timedelta(days=30)
ACKNOWLEDGEMENT = "Ваше сообщение зарегистрировано и передано в компетентные органы"


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def has_consent(device: Device) -> bool:
    event = ConsentEvent.select().where(ConsentEvent.device_id == device.id).order_by(
        ConsentEvent.created_at.desc(), ConsentEvent.id.desc()
    ).first()
    return bool(event and event.action == "GRANTED" and event.version == CONSENT_VERSION
                and _aware(event.created_at) + RETENTION > utc_now())


def require_consent(device: Device) -> None:
    if not has_consent(device):
        raise HTTPException(403, "Current privacy consent is required")


def grant_consent(device: Device, version: str, clicked_at=None) -> dict:
    if version != CONSENT_VERSION:
        raise HTTPException(409, "Consent text has changed")
    event = ConsentEvent.create(device_id=device.id, version=version, action="GRANTED", clicked_at=clicked_at or utc_now())
    return {"version": version, "consented_at": event.created_at.isoformat()}


def delete_message_data(message: Message) -> None:
    # Keep only the separate receipt, never message content, coordinates or media.
    from app.services.messages import _delete_media_file_if_exists
    for media in Media.select().where(Media.message == message.id):
        _delete_media_file_if_exists(media.storage_key)
    with database_proxy.atomic():
        Media.delete().where(Media.message == message.id).execute()
        LocationPoint.delete().where(LocationPoint.message == message.id).execute()
        LiveLocationSession.delete().where(LiveLocationSession.message == message.id).execute()
        StaticLocation.delete().where(StaticLocation.message == message.id).execute()
        Message.delete().where(Message.id == message.id).execute()


def withdraw_consent(device: Device) -> dict:
    for message in Message.select().where(Message.observer_device == device.id):
        delete_message_data(message)
    ConsentEvent.create(device_id=device.id, version=CONSENT_VERSION, action="WITHDRAWN")
    Device.update(push_token=None).where(Device.id == device.id).execute()
    return {"deleted": True, "notice": "Данные удалены из приложения. Если сведения уже переданы в Госавтоинспекцию, дальнейшее удаление осуществляется Госавтоинспекцией по вашему обращению."}


def acknowledge_receipt(actor: Device, message_id: UUID) -> dict:
    from app.services.messages import require_employee_chat_access, get_message_or_404
    from app.services.realtime import manager
    require_employee_chat_access(actor)
    existing = Receipt.get_or_none(Receipt.message_id == message_id)
    if existing:
        return {"message_id": str(existing.message_id), "received_at": existing.received_at.isoformat()}
    message = get_message_or_404(message_id)
    from app.services.messages import _require_chat_visible_to_actor, _require_message_visible_to_actor
    from app.schemas.device import ClientApp
    _require_chat_visible_to_actor(actor=actor, client_app=ClientApp.EMPLOYEE, observer_device_id=message.observer_device_id)
    _require_message_visible_to_actor(actor=actor, client_app=ClientApp.EMPLOYEE, message=message)
    receipt, _ = Receipt.get_or_create(message_id=message.id, defaults={"received_by": actor.id})
    observer_id = str(message.observer_device_id)
    manager.publish_to_devices({observer_id}, {"event": "receipt_confirmed", "message_id": str(message_id), "text": ACKNOWLEDGEMENT})
    return {"message_id": str(message_id), "received_at": receipt.received_at.isoformat()}


def cleanup_personal_data() -> int:
    cutoff = utc_now() - RETENTION
    count = 0
    for message in Message.select().where(Message.created_at <= cutoff):
        delete_message_data(message)
        count += 1
    ConsentEvent.delete().where(ConsentEvent.created_at <= cutoff).execute()
    Receipt.delete().where(Receipt.received_at <= cutoff).execute()
    return count
