import hashlib
import secrets
from datetime import UTC, timedelta

from app.models.device import Device, utc_now


def hash_access_token(access_token: str) -> str:
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


def issue_access_token(device: Device) -> str:
    access_token = secrets.token_urlsafe(32)
    Device.update(
        access_token_hash=hash_access_token(access_token),
        access_token_expires_at=utc_now() + timedelta(days=30),
    ).where(
        Device.id == device.id
    ).execute()
    return access_token


def get_device_by_access_token(access_token: str) -> Device | None:
    token_hash = hash_access_token(access_token)
    device = Device.get_or_none(Device.access_token_hash == token_hash)
    if device is not None and device.access_token_expires_at is not None:
        expires = device.access_token_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= utc_now():
            return None
    return device
