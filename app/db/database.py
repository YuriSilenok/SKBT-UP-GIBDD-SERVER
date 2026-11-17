from peewee import DatabaseProxy, PostgresqlDatabase

from app.core.config import Settings, get_settings

database_proxy = DatabaseProxy()


def init_database(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not settings.db_password:
        raise RuntimeError("DB_PASSWORD must be explicitly configured")
    database = PostgresqlDatabase(
        settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        options="-c timezone=UTC",
    )
    database_proxy.initialize(database)


def connect_database() -> None:
    database_proxy.connect(reuse_if_open=True)


def close_database() -> None:
    if not database_proxy.is_closed():
        database_proxy.close()


def create_tables() -> None:
    from app.models.privacy import ConsentEvent, Receipt
    from app.models.ban import Ban
    from app.models.device import Device
    from app.models.live_location_session import LiveLocationSession
    from app.models.location_point import LocationPoint
    from app.models.media import Media
    from app.models.message import Message
    from app.models.role_event import RoleEvent
    from app.models.static_location import StaticLocation

    database_proxy.create_tables(
        [
            Device,
            RoleEvent,
            Message,
            StaticLocation,
            Media,
            LiveLocationSession,
            LocationPoint,
            Ban,
            ConsentEvent,
            Receipt,
        ],
        safe=True,
    )
    database_proxy.execute_sql("DROP INDEX IF EXISTS devices_single_chief_idx")
    _ensure_device_access_token_hash()
    database_proxy.execute_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS device_access_token_hash_unique
        ON devices (access_token_hash)
        """
    )
    database_proxy.execute_sql(
        "CREATE INDEX IF NOT EXISTS messages_observer_created_idx "
        "ON messages (observer_device_id, created_at DESC, id DESC)"
    )
    database_proxy.execute_sql(
        "CREATE INDEX IF NOT EXISTS bans_observer_period_idx "
        "ON bans (observer_device_id, started_at, ends_at)"
    )


def _ensure_device_access_token_hash() -> None:
    column_names = {column.name for column in database_proxy.get_columns("devices")}
    if "access_token_hash" not in column_names:
        database_proxy.execute_sql(
            "ALTER TABLE devices ADD COLUMN access_token_hash VARCHAR(128)"
        )
    if "access_token_expires_at" not in column_names:
        database_proxy.execute_sql("ALTER TABLE devices ADD COLUMN access_token_expires_at TIMESTAMP")
        from datetime import timedelta
        from app.models.device import Device, utc_now
        Device.update(access_token_expires_at=utc_now() + timedelta(days=30)).where(
            Device.access_token_hash.is_null(False)
        ).execute()
    if "client_app" not in column_names:
        database_proxy.execute_sql("ALTER TABLE devices ADD COLUMN client_app VARCHAR(16)")
        database_proxy.execute_sql("UPDATE devices SET client_app = 'employee' WHERE current_role IS NOT NULL")
