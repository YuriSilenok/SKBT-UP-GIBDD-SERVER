from unittest.mock import patch
from app.services import push


def test_no_external_push_or_payload_transmission():
    with patch("socket.socket.connect", side_effect=AssertionError("External connection forbidden")):
        push.notify_message_created(object())
        push.notify_observer_banned(object())
        push.notify_role_changed(object(), old_role=None, new_role="ADMIN")
