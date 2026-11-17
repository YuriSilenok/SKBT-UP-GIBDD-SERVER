from datetime import timedelta
from io import BytesIO
from uuid import UUID
import pytest
from PIL import Image
from starlette.websockets import WebSocketDisconnect
from app.models.device import Device, utc_now
from app.models.message import Message
from app.models.privacy import ConsentEvent, Receipt
from app.models.role_event import RoleEvent
from app.services.privacy import CONSENT_VERSION, cleanup_personal_data


def register(client, kind="employee", fingerprint="a", consent=True):
    response = client.post("/api/v1/devices/register", headers={"X-Client-App": kind},
                           json={"fingerprint_hash": fingerprint * 64})
    assert response.status_code == 200, response.text
    data = response.json()
    if consent and kind == "eyewitness":
        assert client.post("/api/v1/privacy/consent", headers=headers(data, kind),
                           json={"version": CONSENT_VERSION}).status_code == 200
    return data


def headers(device, kind="employee"):
    return {"Authorization": "Bearer " + device["access_token"], "X-Client-App": kind}


def send(client, observer):
    response = client.post("/api/v1/messages", headers=headers(observer, "eyewitness"),
                           json={"message_type": "TEXT", "text": "Событие у перекрёстка"})
    assert response.status_code == 200, response.text
    return response.json()


def test_known_fingerprint_does_not_issue_token_or_change_push(client):
    chief = register(client)
    before = Device.get_by_id(chief["device_id"]).access_token_hash
    attack = client.post("/api/v1/devices/register", headers={"X-Client-App": "employee"},
                         json={"fingerprint_hash": "a" * 64, "push_token": "attacker"})
    assert attack.status_code == 401
    device = Device.get_by_id(chief["device_id"])
    assert device.access_token_hash == before and device.push_token is None


def test_wrong_device_token_cannot_claim_fingerprint(client):
    chief = register(client)
    other = register(client, fingerprint="b")
    assert client.post("/api/v1/devices/register", headers=headers(other),
                       json={"fingerprint_hash": "a" * 64}).status_code == 401


@pytest.mark.parametrize("method,new_role", [("DELETE", None), ("PUT", "ADMIN"), ("PUT", "INSPECTOR")])
def test_admin_cannot_change_chief(client, method, new_role):
    chief = register(client)
    admin = register(client, fingerprint="b")
    assert client.put(f"/api/v1/employee/devices/{admin['device_id']}/role", headers=headers(chief), json={"role": "ADMIN"}).status_code == 200
    response = client.request(method, f"/api/v1/employee/devices/{chief['device_id']}/role",
                              headers=headers(admin), json={"role": new_role} if new_role else None)
    assert response.status_code == 403
    assert Device.get_by_id(chief["device_id"]).current_role == "CHIEF"


@pytest.mark.parametrize("method,new_role", [("DELETE", None), ("PUT", "ADMIN")])
def test_last_chief_is_protected_even_from_itself(client, method, new_role):
    chief = register(client)
    response = client.request(method, f"/api/v1/employee/devices/{chief['device_id']}/role",
                              headers=headers(chief), json={"role": new_role} if new_role else None)
    assert response.status_code == 409
    assert Device.get_by_id(chief["device_id"]).current_role == "CHIEF"


def test_chief_can_handover_then_remove_own_role(client):
    chief = register(client)
    replacement = register(client, fingerprint="b")
    assert client.put(f"/api/v1/employee/devices/{replacement['device_id']}/role", headers=headers(chief), json={"role": "CHIEF"}).status_code == 200
    assert client.delete(f"/api/v1/employee/devices/{chief['device_id']}/role", headers=headers(chief)).status_code == 200
    assert Device.select().where(Device.current_role == "CHIEF").count() == 1


def test_bootstrap_never_reopens_after_initial_assignment(client):
    chief = register(client)
    # Simulate an out-of-band database mistake, not an allowed API action.
    Device.update(current_role=None).where(Device.id == UUID(chief["device_id"])).execute()
    new = register(client, fingerprint="b")
    assert new["role"] is None
    assert RoleEvent.select().where(RoleEvent.action == "AUTO_ASSIGNED").count() == 1


def test_client_header_cannot_switch_identity(client):
    witness = register(client, "eyewitness", "w")
    assert client.get("/api/v1/employee/me", headers=headers(witness)).status_code == 403


def test_consent_required_and_version_checked(client):
    observer = register(client, "eyewitness", "w", consent=False)
    response = client.post("/api/v1/messages", headers=headers(observer, "eyewitness"), json={"message_type": "TEXT", "text": "Report"})
    assert response.status_code == 403
    assert client.post("/api/v1/privacy/consent", headers=headers(observer, "eyewitness"), json={"version": "old"}).status_code == 409
    assert ConsentEvent.select().count() == 0


def test_receipt_retains_shared_message_for_other_employee(client):
    chief = register(client)
    inspector = register(client, fingerprint="b")
    assert client.put(f"/api/v1/employee/devices/{inspector['device_id']}/role", headers=headers(chief), json={"role": "INSPECTOR"}).status_code == 200
    observer = register(client, "eyewitness", "w")
    message = send(client, observer)
    endpoint = f"/api/v1/messages/{message['message_id']}/receipt"
    assert client.post(endpoint, headers=headers(chief)).status_code == 200
    assert client.post(endpoint, headers=headers(inspector)).status_code == 200
    assert Receipt.select().count() == 1
    shared = client.get(f"/api/v1/chats/{observer['device_id']}/messages", headers=headers(inspector))
    assert shared.json()["messages"][0]["text"] == message["text"]


def test_witness_cannot_confirm_department_receipt(client):
    observer = register(client, "eyewitness", "w")
    message = send(client, observer)
    assert client.post(f"/api/v1/messages/{message['message_id']}/receipt", headers=headers(observer, "eyewitness")).status_code == 403


def test_withdraw_deletes_only_own_reports_and_blocks_sending(client):
    own = register(client, "eyewitness", "w")
    other = register(client, "eyewitness", "x")
    send(client, own)
    send(client, other)
    response = client.delete("/api/v1/privacy/consent", headers=headers(own, "eyewitness"))
    assert response.status_code == 200
    assert Message.select().count() == 1
    assert str(Message.get().observer_device_id) == other["device_id"]
    assert client.get("/api/v1/privacy", headers=headers(own, "eyewitness")).json()["active"] is False
    assert client.post("/api/v1/messages", headers=headers(own, "eyewitness"), json={"message_type": "TEXT", "text": "New"}).status_code == 403


def test_30_day_cleanup_does_not_delete_recent_reports(client):
    observer = register(client, "eyewitness", "w")
    old = send(client, observer)
    recent = send(client, observer)
    Message.update(created_at=utc_now()-timedelta(days=31)).where(Message.id == UUID(old["message_id"])).execute()
    assert cleanup_personal_data() == 1
    assert str(Message.get().id) == recent["message_id"]


def test_expired_token_rejected(client):
    chief = register(client)
    Device.update(access_token_expires_at=utc_now()-timedelta(seconds=1)).where(Device.id == UUID(chief["device_id"])).execute()
    assert client.get("/api/v1/employee/me", headers=headers(chief)).status_code == 401


def test_websocket_revoked_after_token_rotation(client):
    chief = register(client)
    with client.websocket_connect("/api/v1/realtime", headers=headers(chief)) as websocket:
        websocket.receive_json()
        assert client.post("/api/v1/devices/register", headers=headers(chief), json={"fingerprint_hash": "a" * 64}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_json()
        assert error.value.code == 1008


def test_fake_image_mime_rejected_and_file_not_left_behind(client, tmp_path):
    observer = register(client, "eyewitness", "w")
    response = client.post("/api/v1/messages/media/upload", headers=headers(observer, "eyewitness"), files={"file": ("image.jpg", b"not-an-image", "image/jpeg")})
    assert response.status_code == 415
    assert not list((tmp_path / "media").rglob("*.jpg"))


def test_known_media_key_cannot_be_reused_by_other_witness(client):
    own = register(client, "eyewitness", "w")
    other = register(client, "eyewitness", "x")
    data = BytesIO()
    Image.new("RGB", (2, 2)).save(data, format="JPEG")
    uploaded = client.post("/api/v1/messages/media/upload", headers=headers(own, "eyewitness"), files={"file": ("photo.jpg", data.getvalue(), "image/jpeg")})
    assert uploaded.status_code == 200
    attack = client.post("/api/v1/messages/media", headers=headers(other, "eyewitness"), json={"storage_key": uploaded.json()["media"]["storage_key"], "mime_type": "image/jpeg"})
    assert attack.status_code == 410
    assert client.get(f"/api/v1/messages/{uploaded.json()['message_id']}/media", headers=headers(other, "eyewitness")).status_code == 403


def test_write_rate_limit_and_body_limit(client):
    chief = register(client)
    for _ in range(9):
        client.post("/api/v1/devices/register", headers=headers(chief), json={"fingerprint_hash": "a" * 64})
    assert client.post("/api/v1/devices/register", json={"fingerprint_hash": "z" * 64}, headers={"X-Client-App": "employee"}).status_code == 429
    assert client.post("/api/v1/messages", content=b"x" * 65537, headers={"Content-Type": "application/json"}).status_code == 413


def test_legal_documents_are_public_and_path_restricted(client):
    assert "Костромской" in client.get("/legal/policy").text
    assert client.get("/legal/unknown").status_code == 404


def test_closed_websockets_release_connection_slots(client):
    from app.services.realtime import manager
    chief = register(client)
    for _ in range(6):
        with client.websocket_connect("/api/v1/realtime", headers=headers(chief)) as websocket:
            websocket.receive_json()
        assert manager.connection_count(chief["device_id"]) == 0


def test_bulk_receipt_moves_past_first_page_and_retains_all_messages(client):
    chief = register(client)
    observer = register(client, "eyewitness", "w")
    now = utc_now()
    Message.insert_many([{"id": UUID(int=i+1), "observer_device": UUID(observer["device_id"]),
        "sender_device": UUID(observer["device_id"]), "created_at": now,
        "message_type": "TEXT", "text": "Synthetic report"} for i in range(105)]).execute()
    endpoint = f"/api/v1/chats/{observer['device_id']}/receipt"
    assert client.post(endpoint, headers=headers(chief)).json()["acknowledged"] == 100
    assert client.post(endpoint, headers=headers(chief)).json()["acknowledged"] == 5
    assert client.post(endpoint, headers=headers(chief)).json()["acknowledged"] == 0
    assert Message.select().count() == Receipt.select().count() == 105
    messages = f"/api/v1/chats/{observer['device_id']}/messages"
    first = client.get(messages, headers=headers(chief), params={"limit": 100}).json()["messages"]
    second = client.get(messages, headers=headers(chief), params={"after_message_id": first[-1]["message_id"]}).json()["messages"]
    assert len(first) == 100 and len(second) == 5
    assert not ({m["message_id"] for m in first} & {m["message_id"] for m in second})


def test_upload_authentication_precedes_reading_request_body(client):
    read = []
    def content():
        read.append(True)
        yield b"untrusted multipart payload"
    response = client.post("/api/v1/messages/media/upload", content=content(), headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert response.status_code == 401
    assert not read


def test_admin_panel_requires_same_origin_and_protects_last_chief(client, monkeypatch):
    from fastapi.testclient import TestClient
    from scripts.admin_panel import app
    from app.core.config import get_settings
    chief = register(client)
    monkeypatch.setenv("ADMIN_PANEL_PASSWORD", "test-panel-password")
    get_settings.cache_clear()
    panel = TestClient(app)  # No lifespan: continue to use the isolated fixture database.
    try:
        endpoint = f"/devices/{chief['device_id']}/role"
        auth = ("admin", "test-panel-password")
        assert panel.post(endpoint, auth=auth, data={"role": ""}, headers={"Origin": "https://other.invalid"}).status_code == 403
        result = panel.post(endpoint, auth=auth, data={"role": ""}, headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert result.status_code == 303
        assert "last" in result.headers["location"]
        assert Device.get_by_id(chief["device_id"]).current_role == "CHIEF"
    finally:
        panel.close()
