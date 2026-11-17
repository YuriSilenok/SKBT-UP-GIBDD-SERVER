from uuid import UUID
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, AwareDatetime
from pathlib import Path
from app.api.dependencies import get_authorized_device, require_employee_client
from app.models.device import Device
from app.services.privacy import CONSENT_VERSION, grant_consent, withdraw_consent, has_consent, acknowledge_receipt

router = APIRouter(tags=["privacy"])
LEGAL_DIR = Path(__file__).resolve().parents[1] / "legal"


class ConsentRequest(BaseModel):
    version: str = Field(max_length=64)
    clicked_at: AwareDatetime | None = None


@router.get("/api/v1/privacy")
def privacy_status(actor: Device = Depends(get_authorized_device)):
    return {"version": CONSENT_VERSION, "active": has_consent(actor)}


@router.post("/api/v1/privacy/consent")
def consent(payload: ConsentRequest, actor: Device = Depends(get_authorized_device)):
    return grant_consent(actor, payload.version, payload.clicked_at)


@router.delete("/api/v1/privacy/consent")
def withdraw(actor: Device = Depends(get_authorized_device)):
    return withdraw_consent(actor)


@router.get("/legal/{document}", response_class=PlainTextResponse)
def legal_document(document: str):
    from fastapi import HTTPException
    if document not in {"policy", "consent", "regulation", "licenses"}:
        raise HTTPException(404)
    return (LEGAL_DIR / (document + ".txt")).read_text(encoding="utf-8")


@router.post("/api/v1/messages/{message_id}/receipt", dependencies=[Depends(require_employee_client)])
def receipt(message_id: UUID, actor: Device = Depends(get_authorized_device)):
    return acknowledge_receipt(actor, message_id)


@router.post("/api/v1/chats/{observer_id}/receipt", dependencies=[Depends(require_employee_client)])
def chat_receipt(observer_id: UUID, actor: Device = Depends(get_authorized_device)):
    from app.services.messages import list_chat_messages
    from app.schemas.device import ClientApp
    messages = list_chat_messages(actor=actor, client_app=ClientApp.EMPLOYEE, observer_device_id=observer_id, limit=100, unreceived_only=True)
    for message in messages:
        acknowledge_receipt(actor, message.id)
    return {"acknowledged": len(messages)}


@router.get("/api/v1/chats/{observer_id}/receipts")
def list_receipts(observer_id: UUID, actor: Device = Depends(get_authorized_device)):
    from app.services.messages import _require_chat_participant, _require_chat_visible_to_actor
    from app.schemas.device import ClientApp
    from app.models.message import Message
    from app.models.privacy import Receipt
    kind = ClientApp.EMPLOYEE if actor.client_app == "employee" or actor.current_role else ClientApp.EYEWITNESS
    _require_chat_participant(actor=actor, client_app=kind, observer_device_id=observer_id)
    _require_chat_visible_to_actor(actor=actor, client_app=kind, observer_device_id=observer_id)
    query = Receipt.select().where(Receipt.message_id.in_(Message.select(Message.id).where(Message.observer_device == observer_id)))
    return {"receipts": [{"message_id": str(r.message_id), "received_at": r.received_at.isoformat()} for r in query.limit(100)]}
