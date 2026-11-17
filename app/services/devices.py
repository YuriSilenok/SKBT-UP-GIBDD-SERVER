from peewee import PostgresqlDatabase
from fastapi import HTTPException
from app.services.auth import get_device_by_access_token

from app.db.database import database_proxy
from app.models.device import Device, utc_now
from app.models.role_event import RoleEvent
from app.schemas.device import ClientApp, DeviceRole, RoleAction
from app.services.auth import issue_access_token

BOOTSTRAP_CHIEF_LOCK_KEY = 20260819


def register_device(
    *,
    fingerprint_hash: str,
    client_app: ClientApp,
    push_token: str | None = None,
    access_token: str | None = None,
) -> tuple[Device, str]:
    with database_proxy.atomic():
        _lock_chief_bootstrap_for_transaction()
        device, created = Device.get_or_create(
            fingerprint_hash=fingerprint_hash,
            defaults={
                "push_token": None,
                "last_activity_at": utc_now(),
                "client_app": client_app.value,
            },
        )

        if not created:
            actor = get_device_by_access_token(access_token) if access_token else None
            if actor is None or actor.id != device.id:
                raise HTTPException(401, "Existing device requires its current bearer token")
            if device.client_app is not None and device.client_app != client_app.value:
                raise HTTPException(403, "Device belongs to another client application")

        changes: dict[str, object] = {"last_activity_at": utc_now(), "client_app": client_app.value, "push_token": None}

        if changes:
            Device.update(**changes).where(Device.id == device.id).execute()
            device = Device.get_by_id(device.id)

        if client_app == ClientApp.EMPLOYEE and device.current_role is None:
            _bootstrap_chief_if_needed(device)
            device = Device.get_by_id(device.id)

        access_token = issue_access_token(device)
        return device, access_token


def _bootstrap_chief_if_needed(device: Device) -> None:
    with database_proxy.atomic():
        _lock_chief_bootstrap_for_transaction()

        if RoleEvent.select().where(RoleEvent.action == RoleAction.AUTO_ASSIGNED.value).exists():
            return

        chief_exists = (
            Device.select(Device.id)
            .where(Device.current_role == DeviceRole.CHIEF.value)
            .exists()
        )
        if chief_exists:
            return

        updated = (
            Device.update(
                current_role=DeviceRole.CHIEF.value,
                last_activity_at=utc_now(),
            )
            .where(
                (Device.id == device.id)
                & (Device.current_role.is_null(True))
            )
            .execute()
        )
        if updated != 1:
            return

        RoleEvent.create(
            actor_device=None,
            target_device=device.id,
            action=RoleAction.AUTO_ASSIGNED.value,
            role=DeviceRole.CHIEF.value,
        )


def _lock_chief_bootstrap_for_transaction() -> None:
    if isinstance(database_proxy.obj, PostgresqlDatabase):
        database_proxy.execute_sql(
            "SELECT pg_advisory_xact_lock(%s)",
            (BOOTSTRAP_CHIEF_LOCK_KEY,),
        )
