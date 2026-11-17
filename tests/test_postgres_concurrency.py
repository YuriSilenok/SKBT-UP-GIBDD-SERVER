"""Integration tests run only against the task-local PostgreSQL test database."""
import os
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
import pytest
from app.db.database import database_proxy
from app.models.device import Device
from app.models.role_event import RoleEvent
from app.schemas.device import ClientApp, DeviceRole
from app.services.devices import register_device
from app.services.roles import assign_role, remove_role

pytestmark = pytest.mark.skipif(os.environ.get("GIBDD_TEST_POSTGRES") != "1", reason="Requires isolated PostgreSQL integration database")


def test_concurrent_first_registrations_assign_one_chief(client):
    def register(index):
        with database_proxy.connection_context():
            return register_device(fingerprint_hash=f"{index:064x}", client_app=ClientApp.EMPLOYEE)[0].current_role
    with ThreadPoolExecutor(max_workers=6) as pool:
        roles = list(pool.map(register, range(6)))
    assert roles.count("CHIEF") == 1
    assert Device.select().where(Device.current_role == "CHIEF").count() == 1
    assert RoleEvent.select().where(RoleEvent.action == "AUTO_ASSIGNED").count() == 1


def test_concurrent_chief_removals_cannot_remove_both(client):
    first, _ = register_device(fingerprint_hash="a" * 64, client_app=ClientApp.EMPLOYEE)
    second, _ = register_device(fingerprint_hash="b" * 64, client_app=ClientApp.EMPLOYEE)
    second, _ = assign_role(actor=first, target=second, role=DeviceRole.CHIEF)
    def remove(device):
        with database_proxy.connection_context():
            try:
                remove_role(actor=device, target=device)
                return 200
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(remove, [first, second]))
    assert sorted(results) == [200, 409]
    assert Device.select().where(Device.current_role == "CHIEF").count() == 1


def test_migration_adds_missing_device_security_columns(client):
    # Simulate the pre-fix device table in this fixture's unique schema.
    for column in ["access_token_expires_at", "client_app"]:
        database_proxy.execute_sql(f'ALTER TABLE devices DROP COLUMN "{column}"')
    from app.db.database import create_tables
    create_tables()
    columns = {column.name for column in database_proxy.get_columns("devices")}
    assert {"access_token_expires_at", "client_app"} <= columns
