from peewee import Model, DateTimeField
from datetime import UTC, datetime

from app.db.database import database_proxy


class BaseModel(Model):
    class Meta:
        database = database_proxy


class UTCDateTimeField(DateTimeField):
    """The existing TIMESTAMP columns contain UTC, even when a driver returns naive values."""
    def python_value(self, value):
        value = super().python_value(value)
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value

