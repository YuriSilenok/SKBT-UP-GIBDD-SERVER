from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from uuid import uuid4
from threading import Lock

MEDIA_STORAGE_LOCK = Lock()

from fastapi import HTTPException, status
from peewee import fn
from app.models.ban import Ban

from app.core.config import get_settings
from app.db.database import database_proxy
from app.models.device import Device, utc_now
from app.models.live_location_session import LiveLocationSession
from app.models.location_point import LocationPoint
from app.models.media import Media
from app.models.message import Message
from app.models.static_location import StaticLocation
from app.schemas.device import ClientApp, DeviceRole
from app.schemas.messages import MessageType
from app.services.bans import get_active_ban, get_ban_at
from app.services.push import notify_message_created
from app.services.realtime import (
    publish_live_location_point,
    publish_live_location_stopped,
    publish_message_created,
    publish_message_delivered,
)

EMPLOYEE_CHAT_ROLES = {"INSPECTOR", "ADMIN", "CHIEF"}
LIVE_LOCATION_DURATION = timedelta(minutes=15)
MEDIA_TTL = timedelta(days=30)
MEDIA_PHOTO_MAX_BYTES = 10 * 1024 * 1024
MEDIA_VIDEO_GIF_MAX_BYTES = 100 * 1024 * 1024
MEDIA_UPLOAD_MAX_BYTES = MEDIA_VIDEO_GIF_MAX_BYTES
PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
GIF_MIME_TYPES = {"image/gif"}
VIDEO_MIME_TYPES = {"video/mp4"}
SUPPORTED_MEDIA_MIME_TYPES = PHOTO_MIME_TYPES | GIF_MIME_TYPES | VIDEO_MIME_TYPES


def require_employee_chat_access(device: Device) -> None:
    if device.current_role not in EMPLOYEE_CHAT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chat access is not allowed for this device",
        )


def create_text_message(
    *,
    sender: Device,
    client_app: ClientApp,
    text: str | None,
    observer_device_id: UUID | None,
) -> Message:
    _require_sender_can_create_message(sender=sender, client_app=client_app)

    if text is None or not text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Text message cannot be empty",
        )

    observer = _resolve_observer_device(
        sender=sender,
        client_app=client_app,
        observer_device_id=observer_device_id,
    )

    with database_proxy.atomic():
        message = Message.create(
            observer_device=observer.id,
            sender_device=sender.id,
            message_type=MessageType.TEXT.value,
            text=text.strip(),
        )
        Device.update(last_activity_at=utc_now()).where(Device.id == sender.id).execute()

    notify_message_created(message)
    publish_message_created(message)
    return message


def create_static_location_message(
    *,
    sender: Device,
    client_app: ClientApp,
    latitude: float,
    longitude: float,
    observer_device_id: UUID | None,
) -> Message:
    _require_sender_can_create_message(sender=sender, client_app=client_app)

    observer = _resolve_observer_device(
        sender=sender,
        client_app=client_app,
        observer_device_id=observer_device_id,
    )

    with database_proxy.atomic():
        message = Message.create(
            observer_device=observer.id,
            sender_device=sender.id,
            message_type=MessageType.STATIC_LOCATION.value,
        )
        StaticLocation.create(
            message=message.id,
            latitude=latitude,
            longitude=longitude,
        )
        Device.update(last_activity_at=utc_now()).where(Device.id == sender.id).execute()

    notify_message_created(message)
    publish_message_created(message)
    return message


def create_media_message(
    *,
    sender: Device,
    client_app: ClientApp,
    storage_key: str,
    mime_type: str,
    observer_device_id: UUID | None,
) -> Message:
    _require_sender_can_create_message(sender=sender, client_app=client_app)

    storage_key = storage_key.strip()
    if not storage_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="storage_key cannot be empty",
        )
    normalized_mime_type = _normalize_media_mime_type(mime_type)
    _validate_stored_media_file(
        storage_key=storage_key,
        mime_type=normalized_mime_type,
    )

    observer = _resolve_observer_device(
        sender=sender,
        client_app=client_app,
        observer_device_id=observer_device_id,
    )

    with database_proxy.atomic():
        message = Message.create(
            observer_device=observer.id,
            sender_device=sender.id,
            message_type=MessageType.MEDIA.value,
        )
        Media.create(
            message=message.id,
            storage_key=storage_key,
            mime_type=normalized_mime_type,
        )
        Device.update(last_activity_at=utc_now()).where(Device.id == sender.id).execute()

    notify_message_created(message)
    publish_message_created(message)
    return message


def create_streamed_media_message(*, sender, client_app, filename, mime_type, source, observer_device_id):
    _require_sender_can_create_message(sender=sender, client_app=client_app)
    normalized = _normalize_media_mime_type(mime_type)
    _resolve_observer_device(sender=sender, client_app=client_app, observer_device_id=observer_device_id)
    storage_key = _new_media_storage_key(filename)
    path = _media_file_path(storage_key)
    root = Path(get_settings().media_storage_dir)
    # Bound total disk use as well as individual requests. Production proxy adds shared quotas.
    with MEDIA_STORAGE_LOCK:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0
        if used >= 2 * 1024 ** 3:
            raise HTTPException(507, "Media storage quota exceeded")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            import os
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    _validate_media_size(mime_type=normalized, size_bytes=size)
                    if used + size > 2 * 1024 ** 3:
                        raise HTTPException(507, "Media storage quota exceeded")
                    target.write(chunk)
            if not size:
                raise HTTPException(422, "Media file cannot be empty")
            _validate_media_content(path, normalized)
            return create_media_message(sender=sender, client_app=client_app, storage_key=storage_key,
                mime_type=normalized, observer_device_id=observer_device_id)
        except Exception:
            path.unlink(missing_ok=True)
            raise


def _validate_media_content(path: Path, mime_type: str) -> None:
    if mime_type == "video/mp4":
        with path.open("rb") as stream:
            header = stream.read(64)
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise HTTPException(415, "Invalid MP4 container")
        return
    from PIL import Image, UnidentifiedImageError
    import warnings
    expected = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP", "image/gif": "GIF"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format != expected[mime_type] or image.width * image.height > 40_000_000:
                    raise HTTPException(415, "Image content does not match MIME or exceeds pixel limit")
                if getattr(image, "n_frames", 1) > 1000:
                    raise HTTPException(415, "Too many image frames")
                image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, "Invalid or excessively large image")


def create_live_location_message(
    *,
    sender: Device,
    client_app: ClientApp,
    observer_device_id: UUID | None,
) -> Message:
    _require_sender_can_create_message(sender=sender, client_app=client_app)

    observer = _resolve_observer_device(
        sender=sender,
        client_app=client_app,
        observer_device_id=observer_device_id,
    )

    with database_proxy.atomic():
        now = utc_now()
        message = Message.create(
            observer_device=observer.id,
            sender_device=sender.id,
            message_type=MessageType.LIVE_LOCATION.value,
            created_at=now,
        )
        LiveLocationSession.create(
            message=message.id,
            ends_at=now + LIVE_LOCATION_DURATION,
        )
        Device.update(last_activity_at=now).where(Device.id == sender.id).execute()

    notify_message_created(message)
    publish_message_created(message)
    return message


def add_live_location_point(
    *,
    actor: Device,
    client_app: ClientApp,
    message_id: UUID,
    latitude: float,
    longitude: float,
) -> LocationPoint:
    message = _get_live_location_message_or_404(message_id)
    _require_live_location_sender(actor=actor, message=message)
    _require_chat_participant(
        actor=actor,
        client_app=client_app,
        observer_device_id=message.observer_device_id,
    )

    session = get_live_location_session_for_message(message)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Live location session not found",
        )

    now = utc_now()
    if _as_utc_aware(session.ends_at) <= now:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live location session has ended",
        )

    with database_proxy.atomic():
        point = LocationPoint.create(
            message=message.id,
            recorded_at=now,
            latitude=latitude,
            longitude=longitude,
        )
        Device.update(last_activity_at=now).where(Device.id == actor.id).execute()

    publish_live_location_point(message, point)
    return point


def stop_live_location(
    *,
    actor: Device,
    client_app: ClientApp,
    message_id: UUID,
) -> Message:
    message = _get_live_location_message_or_404(message_id)
    _require_live_location_sender(actor=actor, message=message)
    _require_chat_participant(
        actor=actor,
        client_app=client_app,
        observer_device_id=message.observer_device_id,
    )

    session = get_live_location_session_for_message(message)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Live location session not found",
        )

    now = utc_now()
    if _as_utc_aware(session.ends_at) > now:
        LiveLocationSession.update(ends_at=now).where(
            LiveLocationSession.message == message.id
        ).execute()

    message = Message.get_by_id(message.id)
    publish_live_location_stopped(message)
    return message


def list_live_location_points(
    *,
    actor: Device,
    client_app: ClientApp,
    message_id: UUID,
    after_recorded_at: datetime | None = None,
    limit: int = 100,
) -> list[LocationPoint]:
    message = _get_live_location_message_or_404(message_id)
    _require_chat_participant(
        actor=actor,
        client_app=client_app,
        observer_device_id=message.observer_device_id,
    )
    _require_chat_visible_to_actor(
        actor=actor,
        client_app=client_app,
        observer_device_id=message.observer_device_id,
    )
    _require_message_visible_to_actor(
        actor=actor,
        client_app=client_app,
        message=message,
    )

    query = LocationPoint.select().where(LocationPoint.message == message.id)
    if after_recorded_at is not None:
        query = query.where(LocationPoint.recorded_at > after_recorded_at)

    return list(query.order_by(LocationPoint.recorded_at.asc()).limit(limit))


def _not_banned_at(moment):
    return ~fn.EXISTS(Ban.select(Ban.id).where(
        (Ban.observer_device == Message.observer_device)
        & (Ban.started_at <= moment)
        & (Ban.ends_at.is_null() | (Ban.ends_at > moment))
    ))


def list_chats(actor: Device, *, limit: int = 200, offset: int = 0) -> list[Message]:
    require_employee_chat_access(actor)
    ranked = Message.select(Message, fn.ROW_NUMBER().over(
        partition_by=[Message.observer_device],
        order_by=[Message.created_at.desc(), Message.id.desc()],
    ).alias("position")).where(Message.created_at > utc_now() - MEDIA_TTL)
    if actor.current_role != DeviceRole.CHIEF.value:
        ranked = ranked.where(_not_banned_at(utc_now()), _not_banned_at(Message.created_at))
    ranked = ranked.alias("ranked_messages")
    columns = [getattr(ranked.c, field.column_name) for field in Message._meta.sorted_fields]
    return list(Message.select(*columns).from_(ranked).where(ranked.c.position == 1)
                .order_by(ranked.c.created_at.desc(), ranked.c.id.desc()).limit(limit).offset(offset))


def count_unread_chat_messages(*, actor: Device, observer_device_id: UUID) -> int:
    require_employee_chat_access(actor)
    query = Message.select().where(
        (Message.observer_device == observer_device_id)
        & (Message.sender_device == observer_device_id)
        & Message.delivered_at.is_null()
    )
    if actor.current_role != DeviceRole.CHIEF.value:
        query = query.where(_not_banned_at(Message.created_at))
    return query.count()


def list_chat_messages(
    *,
    actor: Device,
    client_app: ClientApp,
    observer_device_id: UUID,
    after_message_id: UUID | None = None,
    limit: int = 50,
    unreceived_only: bool = False,
) -> list[Message]:
    _require_chat_participant(
        actor=actor,
        client_app=client_app,
        observer_device_id=observer_device_id,
    )
    _require_chat_visible_to_actor(
        actor=actor,
        client_app=client_app,
        observer_device_id=observer_device_id,
    )

    query = Message.select().where(
        (Message.observer_device == observer_device_id)
        & (Message.created_at > utc_now() - MEDIA_TTL)
    )
    if unreceived_only:
        from app.models.privacy import Receipt
        query = query.where(Message.id.not_in(Receipt.select(Receipt.message_id)))
    if after_message_id is not None:
        after_message = get_message_or_404(after_message_id)
        if str(after_message.observer_device_id) != str(observer_device_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="after_message_id belongs to another chat",
            )
        query = query.where(
            (Message.created_at > after_message.created_at)
            | ((Message.created_at == after_message.created_at) & (Message.id > after_message.id))
        )

    messages = []
    for message in query.order_by(Message.created_at.asc(), Message.id.asc()):
        if _message_visible_to_actor(actor=actor, client_app=client_app, message=message):
            messages.append(message)
        if len(messages) >= limit:
            break

    return messages


def mark_message_delivered(
    *,
    actor: Device,
    client_app: ClientApp,
    message_id: UUID,
) -> Message:
    message = get_message_or_404(message_id)
    _require_chat_participant(
        actor=actor,
        client_app=client_app,
        observer_device_id=message.observer_device_id,
    )
    _require_chat_visible_to_actor(
        actor=actor,
        client_app=client_app,
        observer_device_id=message.observer_device_id,
    )
    _require_message_visible_to_actor(
        actor=actor,
        client_app=client_app,
        message=message,
    )

    if message.delivered_at is None:
        Message.update(delivered_at=utc_now()).where(Message.id == message.id).execute()
        message = Message.get_by_id(message.id)
        publish_message_delivered(message)

    return message


def get_message_or_404(message_id: UUID) -> Message:
    message = Message.get_or_none(Message.id == message_id)
    if message is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )
    if _as_utc_aware(message.created_at) + MEDIA_TTL <= utc_now():
        from app.services.privacy import delete_message_data
        delete_message_data(message)
        raise HTTPException(410, "Message retention period has ended")
    return message


def get_media_file_for_message(
    *,
    actor: Device,
    client_app: ClientApp,
    message_id: UUID,
) -> tuple[Path, Media]:
    message = get_message_or_404(message_id)
    if message.message_type != MessageType.MEDIA.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message is not media",
        )

    _require_chat_participant(
        actor=actor,
        client_app=client_app,
        observer_device_id=message.observer_device_id,
    )
    _require_chat_visible_to_actor(
        actor=actor,
        client_app=client_app,
        observer_device_id=message.observer_device_id,
    )
    _require_message_visible_to_actor(
        actor=actor,
        client_app=client_app,
        message=message,
    )

    media = get_media_for_message(message)
    if media is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media metadata not found",
        )

    if _media_expires_at(message=message, media=media) <= utc_now():
        _delete_media_file_if_exists(media.storage_key)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Media file has expired",
        )

    file_path = _media_file_path(media.storage_key)
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media file not found",
        )

    Media.update(last_viewed_at=utc_now()).where(Media.message == message.id).execute()
    media = Media.get_by_id(message.id)
    return file_path, media


def get_static_location_for_message(message: Message) -> StaticLocation | None:
    if message.message_type != MessageType.STATIC_LOCATION.value:
        return None
    return StaticLocation.get_or_none(StaticLocation.message == message.id)


def get_media_for_message(message: Message) -> Media | None:
    if message.message_type != MessageType.MEDIA.value:
        return None
    return Media.get_or_none(Media.message == message.id)


def get_live_location_session_for_message(
    message: Message,
) -> LiveLocationSession | None:
    if message.message_type != MessageType.LIVE_LOCATION.value:
        return None
    return LiveLocationSession.get_or_none(LiveLocationSession.message == message.id)


def _resolve_observer_device(
    *,
    sender: Device,
    client_app: ClientApp,
    observer_device_id: UUID | None,
) -> Device:
    if client_app == ClientApp.EYEWITNESS:
        if observer_device_id is not None and str(observer_device_id) != str(sender.id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Eyewitness can only write to own chat",
            )
        return sender

    require_employee_chat_access(sender)
    if observer_device_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="observer_device_id is required for employee messages",
        )

    observer = Device.get_or_none(Device.id == observer_device_id)
    if observer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Observer device not found",
        )
    return observer


def _require_sender_can_create_message(*, sender: Device, client_app: ClientApp) -> None:
    from app.api.dependencies import require_matching_client
    from app.services.privacy import require_consent
    require_matching_client(sender, client_app)
    if client_app == ClientApp.EMPLOYEE:
        raise HTTPException(403, "Replies to applicants are not permitted by the regulation")
    require_consent(sender)
    if client_app == ClientApp.EYEWITNESS and get_active_ban(sender.id) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Observer device is banned",
        )


def _require_chat_participant(
    *,
    actor: Device,
    client_app: ClientApp,
    observer_device_id: UUID,
) -> None:
    from app.api.dependencies import require_matching_client
    require_matching_client(actor, client_app)
    observer_exists = Device.select(Device.id).where(Device.id == observer_device_id).exists()
    if not observer_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Observer device not found",
        )

    if client_app == ClientApp.EYEWITNESS:
        if str(actor.id) != str(observer_device_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Eyewitness can only access own chat",
            )
        return

    require_employee_chat_access(actor)


def _require_chat_visible_to_actor(
    *,
    actor: Device,
    client_app: ClientApp,
    observer_device_id: UUID,
) -> None:
    if (
        client_app == ClientApp.EMPLOYEE
        and actor.current_role != DeviceRole.CHIEF.value
        and get_active_ban(observer_device_id) is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Banned chat is visible only to CHIEF",
        )


def _require_message_visible_to_actor(
    *,
    actor: Device,
    client_app: ClientApp,
    message: Message,
) -> None:
    if not _message_visible_to_actor(actor=actor, client_app=client_app, message=message):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Message is visible only to CHIEF",
        )


def _message_visible_to_actor(
    *,
    actor: Device,
    client_app: ClientApp,
    message: Message,
) -> bool:
    if client_app != ClientApp.EMPLOYEE:
        return True
    return _message_visible_to_employee(actor=actor, message=message)


def _message_visible_to_employee(*, actor: Device, message: Message) -> bool:
    if actor.current_role == DeviceRole.CHIEF.value:
        return True
    return get_ban_at(message.observer_device_id, message.created_at) is None


def _get_live_location_message_or_404(message_id: UUID) -> Message:
    message = get_message_or_404(message_id)
    if message.message_type != MessageType.LIVE_LOCATION.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message is not a live location",
        )
    return message


def _require_live_location_sender(*, actor: Device, message: Message) -> None:
    if str(actor.id) != str(message.sender_device_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only live location sender can update this session",
        )


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _local_timezone():
    return datetime.now().astimezone().tzinfo


def _new_media_storage_key(filename: str | None) -> str:
    now = utc_now()
    suffix = Path(filename or "").suffix.lower()
    if len(suffix) > 16 or not suffix.startswith(".") or not suffix[1:].isalnum():
        suffix = ""
    return f"{now:%Y/%m}/{uuid4().hex}{suffix}"


def _normalize_media_mime_type(mime_type: str | None) -> str:
    normalized = (mime_type or "").split(";", maxsplit=1)[0].strip().lower()
    if normalized not in SUPPORTED_MEDIA_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported media mime_type",
        )
    return normalized


def _validate_stored_media_file(*, storage_key: str, mime_type: str) -> None:
    file_path = _media_file_path(storage_key)
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media file not found",
        )
    _validate_media_size(
        mime_type=mime_type,
        size_bytes=file_path.stat().st_size,
    )


def _validate_media_size(*, mime_type: str, size_bytes: int) -> None:
    max_bytes = (
        MEDIA_VIDEO_GIF_MAX_BYTES
        if mime_type in GIF_MIME_TYPES | VIDEO_MIME_TYPES
        else MEDIA_PHOTO_MAX_BYTES
    )
    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Media file is too large",
        )


def _media_expires_at(*, message: Message, media: Media) -> datetime:
    return _as_utc_aware(message.created_at) + MEDIA_TTL


def _delete_media_file_if_exists(storage_key: str) -> bool:
    file_path = _media_file_path(storage_key)
    if file_path.is_file():
        file_path.unlink(missing_ok=True)
        return True
    return False


def _media_file_path(storage_key: str) -> Path:
    root = Path(get_settings().media_storage_dir).resolve()
    file_path = (root / storage_key).resolve()
    try:
        file_path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid media storage_key",
        ) from exc
    return file_path
