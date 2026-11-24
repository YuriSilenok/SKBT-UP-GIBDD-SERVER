#!/usr/bin/env bash
# One-time promotion of the EXISTING VDS, after its source commit is deployed.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 && $# == 1 && "$1" =~ ^[0-9a-f]{40}$ ]] || exit 2
base=/opt/gibdd-test-20261117
expected=$1
[[ ! -f /etc/gibdd/production.enabled ]] || { echo 'Already production'; exit 2; }
exec 8>"$base/dispatch.lock"
flock -w 900 8
exec 9>"$base/deploy.lock"
flock -w 900 9
readarray -t state < <("$base/venv/bin/python" - "$base/deployment.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1]));print(s['commit']);print(s['release'])
PY
)
[[ ${state[0]} == "$expected" ]] || { echo 'Requested source is not deployed yet'; exit 2; }
release=${state[1]}
[[ "$release" == "$base/releases/"* && ! -L "$release" ]] || exit 2
locksha=$(sha256sum "$release/requirements.lock.txt" | awk '{print $1}')
venv="$base/environments/$locksha"
folder=$(cd -- "$(dirname -- "$0")" && pwd)
systemctl is-active --quiet gibdd-postgresql
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$base/backups/$stamp-production-cutover"
install -d -m 0700 "$backup"
cp /etc/nginx/sites-available/pond "$backup/nginx-pond.original"
cp /etc/nginx/sites-available/gibdd-test "$backup/nginx-4443.original"
cp "$base/deployment.json" "$backup/deployment.original.json"
[[ ! -f /etc/nginx/conf.d/gibdd-upgrade.conf ]] || cp /etc/nginx/conf.d/gibdd-upgrade.conf "$backup/upgrade.original"
stopped=0
rollback() {
 result=$?;trap - ERR HUP INT TERM
 if [[ "$stopped" == 1 ]]; then
  systemctl stop gibdd.service || true
  systemctl disable gibdd.service || true
  rm -f /etc/gibdd/production.enabled
  cp "$backup/nginx-pond.original" /etc/nginx/sites-available/pond
  cp "$backup/nginx-4443.original" /etc/nginx/sites-available/gibdd-test
  if [[ -f "$backup/upgrade.original" ]]; then cp "$backup/upgrade.original" /etc/nginx/conf.d/gibdd-upgrade.conf; else rm -f /etc/nginx/conf.d/gibdd-upgrade.conf; fi
  nginx -t && systemctl reload nginx
  systemctl start gibdd-test.service || true
  systemctl enable gibdd-test.service || true
 fi
 echo "Promotion failed; previous API restored. Backup: $backup" >&2
 exit "$result"
}
trap rollback ERR
trap 'false' HUP INT TERM
systemctl stop gibdd-test.service
stopped=1
tar -czf "$backup/sqlite-and-media.tar.gz" -C "$base" staging-ready
install -d -o gibddtest -g gibddtest -m 0700 "$base/staging-ready/media"
systemd-run --quiet --wait --pipe --collect --unit="gibdd-import-$stamp" \
 --uid=gibddtest --gid=gibddtest --working-directory="$release" \
 -p EnvironmentFile=/etc/gibdd/production.env \
 "$venv/bin/python" -B -m scripts.migrate_sqlite_to_postgres --source "$base/staging-ready/test.sqlite3"
install -o root -g root -m 0644 "$folder/gibdd-vds-production.service" /etc/systemd/system/gibdd.service
install -d -m 0755 /etc/systemd/system/gibdd.service.d
cat > /etc/systemd/system/gibdd.service.d/deployment.conf <<UNIT
[Service]
WorkingDirectory=$release
ExecStart=
ExecStart=$venv/bin/python -B -m uvicorn app.main:app --host 127.0.0.1 --port 8441 --workers 1 --limit-concurrency 24 --ws-max-size 1024 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log
Environment=DEPLOYED_COMMIT=$expected
UNIT
systemctl daemon-reload
systemctl start gibdd.service
healthy=0
for attempt in $(seq 1 45); do
 if curl --fail --silent --max-time 2 http://127.0.0.1:8441/health | grep -q '"status":"ok"'; then healthy=1;break;fi
 sleep 1
done
if [[ "$healthy" != 1 ]]; then false;fi
install -o root -g root -m 0644 "$folder/nginx-gibdd-domain-locations.conf" /etc/nginx/snippets/gibdd-production.conf
cat > /etc/nginx/conf.d/gibdd-upgrade.conf <<'MAP'
map $http_upgrade $gibdd_connection_upgrade {
 default upgrade;
 '' close;
}
MAP
"$base/venv/bin/python" - <<'PY'
from pathlib import Path
p=Path('/etc/nginx/sites-available/pond');s=p.read_text()
include='    include /etc/nginx/snippets/gibdd-production.conf;\n'
if include not in s:
 marker='    location / {'
 if marker not in s: raise RuntimeError('Unexpected existing nginx layout')
 s=s.replace(marker,include+marker,1)
p.write_text(s)
PY
cat > /etc/nginx/sites-available/gibdd-test <<'NGINX'
server {
 listen 4443 ssl;
 server_name xn--e1afhclgq.xn--p1ai;
 ssl_certificate /etc/letsencrypt/live/xn--e1afhclgq.xn--p1ai/fullchain.pem;
 ssl_certificate_key /etc/letsencrypt/live/xn--e1afhclgq.xn--p1ai/privkey.pem;
 ssl_protocols TLSv1.2 TLSv1.3;
 return 308 https://xn--e1afhclgq.xn--p1ai$request_uri;
}
NGINX
nginx -t
systemctl reload nginx
curl --fail --silent --max-time 10 --resolve xn--e1afhclgq.xn--p1ai:443:127.0.0.1 https://xn--e1afhclgq.xn--p1ai/health
bash "$folder/activate-production-controller.sh"
systemctl enable gibdd.service
systemctl disable gibdd-test.service
stopped=0
trap - ERR HUP INT TERM
echo "Production activated at https://силенок.рф/. Backup: $backup. Run server CI again to record the production deployment."
