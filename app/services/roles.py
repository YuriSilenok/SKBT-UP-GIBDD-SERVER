from fastapi import HTTPException, status

from app.db.database import database_proxy
from app.models.device import Device, utc_now
from app.models.role_event import RoleEvent
from app.schemas.device import DeviceRole, RoleAction
from app.services.push import notify_role_changed
from app.services.realtime import publish_role_changed

ROLE_ASSIGNMENT_RIGHTS = {
    DeviceRole.ADMIN.value: {DeviceRole.INSPECTOR.value, DeviceRole.ADMIN.value},
    DeviceRole.CHIEF.value: {
        DeviceRole.INSPECTOR.value,
        DeviceRole.ADMIN.value,
        DeviceRole.CHIEF.value,
    },
}


def require_role_manager(actor: Device, target_role: DeviceRole | None = None) -> None:
    allowed_roles = ROLE_ASSIGNMENT_RIGHTS.get(actor.current_role)
    if allowed_roles is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Role management is not allowed for this device",
        )

    if target_role is not None and target_role.value not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This role cannot be assigned by the current device",
        )


def get_device_or_404(device_id: str) -> Device:
    device = Device.get_or_none(Device.id == device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )
    return device


def list_employee_devices(actor: Device) -> list[Device]:
    require_role_manager(actor)
    return list(
        Device.select()
        .where(Device.current_role.is_null(False))
        .order_by(Device.current_role.asc(), Device.last_activity_at.desc())
    )


def _validate_role_change(actor: Device | None, target: Device, new_role: str | None) -> None:
    if new_role is not None and target.client_app == "eyewitness":
        raise HTTPException(403, "Only employee devices can receive a staff role")
    if actor is not None:
        require_role_manager(actor, DeviceRole(new_role) if new_role else None)
        if target.current_role == DeviceRole.CHIEF.value and actor.current_role != DeviceRole.CHIEF.value:
            raise HTTPException(403, "Only CHIEF can change another CHIEF")
    if target.current_role == DeviceRole.CHIEF.value and new_role != DeviceRole.CHIEF.value:
        if Device.select().where(Device.current_role == DeviceRole.CHIEF.value).count() <= 1:
            raise HTTPException(409, "The last CHIEF cannot be removed or demoted")


def change_role(*, actor: Device | None, target: Device, new_role: str | None) -> tuple[Device, RoleAction | None]:
    # Registration and every role-changing path use this same transaction lock.
    from app.services.devices import _lock_chief_bootstrap_for_transaction
    with database_proxy.atomic():
        _lock_chief_bootstrap_for_transaction()
        target = get_device_or_404(str(target.id))
        if actor is not None:
            actor = get_device_or_404(str(actor.id))
        _validate_role_change(actor, target, new_role)
        old_role = target.current_role
        if old_role == new_role:
            return target, None
        action = RoleAction.REMOVED if new_role is None else (
            RoleAction.ASSIGNED if old_role is None else RoleAction.REPLACED
        )
        Device.update(current_role=new_role, last_activity_at=utc_now()).where(Device.id == target.id).execute()
        role_event = RoleEvent.create(
            actor_device=actor.id if actor else None, target_device=target.id,
            action=action.value, role=new_role or old_role,
        )
    updated = Device.get_by_id(target.id)
    notify_role_changed(role_event, old_role=old_role, new_role=new_role)
    publish_role_changed(role_event, old_role=old_role, new_role=new_role)
    return updated, action


def assign_role(*, actor: Device, target: Device, role: DeviceRole) -> tuple[Device, RoleAction | None]:
    return change_role(actor=actor, target=target, new_role=role.value)


def remove_role(*, actor: Device, target: Device) -> tuple[Device, RoleAction | None]:
    return change_role(actor=actor, target=target, new_role=None)
