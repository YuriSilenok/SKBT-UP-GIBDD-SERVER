#!/bin/bash
set -euo pipefail
umask 077
pg=/opt/gibdd-postgresql/17.12/bin
[[ -x "$pg/postgres" ]] || exit 2
install -d -o gibddpg -g gibddpg -m 0700 /var/lib/gibdd-postgresql
if [[ ! -f /var/lib/gibdd-postgresql/data/PG_VERSION ]]; then
    runuser -u gibddpg -- "$pg/initdb" -D /var/lib/gibdd-postgresql/data -U gibddpg --auth-local=peer --auth-host=scram-sha-256 --encoding=UTF8 --locale=C.UTF-8
    cat >> /var/lib/gibdd-postgresql/data/postgresql.conf <<'CONFIG'
listen_addresses = '127.0.0.1'
port = 55432
unix_socket_directories = '/run/gibdd-postgresql'
max_connections = 24
shared_buffers = '32MB'
work_mem = '2MB'
maintenance_work_mem = '16MB'
autovacuum_max_workers = 1
max_parallel_workers = 0
max_worker_processes = 2
max_wal_size = '128MB'
min_wal_size = '32MB'
wal_buffers = '1MB'
timezone = 'UTC'
log_timezone = 'UTC'
password_encryption = 'scram-sha-256'
CONFIG
    cat > /var/lib/gibdd-postgresql/data/pg_hba.conf <<'HBA'
local all all peer
host gibdd gibdd_app 127.0.0.1/32 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::0/0 reject
HBA
    chown gibddpg:gibddpg /var/lib/gibdd-postgresql/data/{postgresql.conf,pg_hba.conf}
    chmod 0600 /var/lib/gibdd-postgresql/data/{postgresql.conf,pg_hba.conf}
fi
cat > /etc/systemd/system/gibdd-postgresql.service <<'UNIT'
[Unit]
Description=PostgreSQL for GIBDD production
After=network.target
[Service]
Type=simple
User=gibddpg
Group=gibddpg
ExecStart=/opt/gibdd-postgresql/17.12/bin/postgres -D /var/lib/gibdd-postgresql/data
Restart=on-failure
RestartSec=5
RuntimeDirectory=gibdd-postgresql
RuntimeDirectoryMode=0700
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/gibdd-postgresql
MemoryMax=256M
TasksMax=64
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now gibdd-postgresql
for i in $(seq 1 30); do
    if runuser -u gibddpg -- "$pg/pg_isready" -h /run/gibdd-postgresql -p 55432 >/dev/null; then break; fi
    sleep 1
 done
/opt/gibdd-test-20261117/venv/bin/python - <<'PY'
from pathlib import Path
import os,secrets,subprocess,pwd,grp
from dotenv import dotenv_values
folder=Path('/etc/gibdd');folder.mkdir(exist_ok=True,mode=0o750)
os.chown(folder,0,grp.getgrnam('gibddtest').gr_gid)
path=folder/'production.env'
existing=dotenv_values(path) if path.exists() else {}
password=existing.get('DB_PASSWORD') or secrets.token_urlsafe(48)
command=['runuser','-u','gibddpg','--','/opt/gibdd-postgresql/17.12/bin/psql','-h','/run/gibdd-postgresql','-p','55432','-d','postgres','-v','ON_ERROR_STOP=1','-At']
def sql(value):
 return subprocess.run(command,input=value,text=True,capture_output=True,check=True).stdout.strip()
if sql("SELECT 1 FROM pg_roles WHERE rolname='gibdd_app'") != '1':
 sql('CREATE ROLE gibdd_app LOGIN;')
sql("ALTER ROLE gibdd_app PASSWORD '"+password+"';")
if sql("SELECT 1 FROM pg_database WHERE datname='gibdd'") != '1':
 sql('CREATE DATABASE gibdd OWNER gibdd_app;')
path.write_text('DB_NAME=gibdd\nDB_USER=gibdd_app\nDB_PASSWORD='+password+'\nDB_HOST=127.0.0.1\nDB_PORT=55432\nMEDIA_STORAGE_DIR=/opt/gibdd-test-20261117/staging-ready/media\nCONSENT_VERSION=2026-11-17-v1\nADMIN_PANEL_PASSWORD=\n',encoding='utf-8')
os.chown(path,0,grp.getgrnam('gibddtest').gr_gid);os.chmod(path,0o640)
print('Production database credentials saved privately; no secret values displayed')
PY
systemctl is-active gibdd-postgresql
