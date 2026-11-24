import os
from datetime import datetime
from pathlib import Path
import sqlite3
from uuid import UUID
import pytest
from app.db.database import database_proxy
from app.models.device import Device
from scripts.migrate_sqlite_to_postgres import TABLES, migrate, quote

pytestmark = pytest.mark.skipif(os.environ.get("GIBDD_TEST_POSTGRES") != "1", reason="PostgreSQL migration integration")


def snapshot(path: Path):
    with sqlite3.connect(path) as old:
        for table in TABLES:
            cursor = database_proxy.execute_sql(f"SELECT * FROM {quote(table)}")
            names = [c[0] for c in cursor.description]
            types = {c.name: c.data_type for c in database_proxy.get_columns(table)}
            schema = ','.join(quote(n) + (' INTEGER' if types[n] == 'boolean' else ' TEXT') for n in names)
            old.execute(f"CREATE TABLE {quote(table)} ({schema})")
            for row in cursor.fetchall():
                row = [v.hex if isinstance(v, UUID) else v.isoformat() if isinstance(v, datetime)
                       else int(v) if isinstance(v, bool) else v for v in row]
                old.execute(f"INSERT INTO {quote(table)} VALUES ({','.join('?' for _ in names)})", row)


def test_migration_preserves_sessions_messages_consent_and_receipts(client, tmp_path):
    def register(kind, fingerprint):
        r = client.post('/api/v1/devices/register', headers={'X-Client-App': kind}, json={'fingerprint_hash': fingerprint * 64})
        assert r.status_code == 200
        d = r.json()
        return d, {'X-Client-App': kind, 'Authorization': 'Bearer ' + d['access_token']}
    employee, staff = register('employee', 'a')
    observer, auth = register('eyewitness', 'b')
    assert client.post('/api/v1/privacy/consent', headers=auth, json={'version': '2026-11-17-v1'}).status_code == 200
    message = client.post('/api/v1/messages', headers=auth, json={'message_type': 'TEXT', 'text': 'Migration synthetic record'}).json()
    assert client.post(f"/api/v1/messages/{message['message_id']}/receipt", headers=staff).status_code == 200
    source = tmp_path / 'source.sqlite3'
    snapshot(source)
    with database_proxy.atomic():
        for table in reversed(TABLES):
            database_proxy.execute_sql(f"DELETE FROM {quote(table)}")
    counts = migrate(source)
    assert counts['devices'] == 2 and counts['messages'] == 1
    assert counts['consent_events'] == 1 and counts['message_receipts'] == 1
    assert Device.get_by_id(employee['device_id']).current_role == 'CHIEF'
    assert client.get('/api/v1/privacy', headers=auth).json()['active'] is True
    messages = client.get(f"/api/v1/chats/{observer['device_id']}/messages", headers=staff).json()['messages']
    assert messages[0]['text'] == 'Migration synthetic record'
    assert sqlite3.connect(source).execute('SELECT count(*) FROM devices').fetchone()[0] == 2


def test_migration_refuses_nonempty_target(client, tmp_path):
    assert client.post('/api/v1/devices/register', headers={'X-Client-App': 'employee'}, json={'fingerprint_hash': 'c' * 64}).status_code == 200
    source = tmp_path / 'source.sqlite3'
    snapshot(source)
    with pytest.raises(RuntimeError, match='not empty'):
        migrate(source)
    assert Device.select().count() == 1
