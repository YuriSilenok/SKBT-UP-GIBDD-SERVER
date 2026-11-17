import uuid
from peewee import UUIDField, CharField, DateTimeField
from app.models.base import BaseModel, UTCDateTimeField
from app.models.device import utc_now


class ConsentEvent(BaseModel):
    id = UUIDField(primary_key=True, default=uuid.uuid4)
    device_id = UUIDField(index=True)
    version = CharField(max_length=64)
    action = CharField(max_length=16)
    created_at = UTCDateTimeField(default=utc_now, index=True)
    clicked_at = UTCDateTimeField(null=True)

    class Meta:
        table_name = "consent_events"


class Receipt(BaseModel):
    message_id = UUIDField(primary_key=True)
    received_by = UUIDField()
    received_at = UTCDateTimeField(default=utc_now, index=True)

    class Meta:
        table_name = "message_receipts"
