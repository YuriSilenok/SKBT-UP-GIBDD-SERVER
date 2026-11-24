#!/usr/bin/env bash
# Root-owned controller installed OUTSIDE the checkout. GitHub may request a SHA,
# never an arbitrary command or a database reset. Production PostgreSQL deployment.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run as root'; exit 1; }
base=/opt/gibdd-test-20261117
service=gibdd.service
data="$base/staging-ready"
dropin=/etc/systemd/system/gibdd.service.d/deployment.conf
pg=/opt/gibdd-postgresql/17.12/bin
export PGHOST=/run/gibdd-postgresql PGPORT=55432
repo=https://github.com/YuriSilenok/SKBT-UP-GIBDD-SERVER.git
requested=${1:-}
reset=
[[ -z "$requested" || "$requested" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ $# -le 1 && -d "$base" && ! -L "$base" && -d "$data/media" && ! -L "$data" ]] || exit 2
[[ $(realpath "$base") == "$base" && $(realpath "$data") == "$data" ]] || exit 2
exec 9>"$base/deploy.lock"
flock -w 900 9
mkdir -p "$base/releases" "$base/backups" "$base/environments"
chown root:gibddtest "$base/releases" "$base/environments"
chmod 0750 "$base/releases" "$base/environments"
chmod 700 "$base/backups"
if [[ ! -d "$base/repository.git" ]]; then
    git init --bare "$base/repository.git" >/dev/null
    git --git-dir="$base/repository.git" remote add origin "$repo"
fi
[[ $(git --git-dir="$base/repository.git" remote get-url origin) == "$repo" ]] || exit 2
git --git-dir="$base/repository.git" fetch --quiet --no-tags origin +refs/heads/main:refs/remotes/origin/main
sha=$(git --git-dir="$base/repository.git" rev-parse refs/remotes/origin/main)
if [[ -n "$requested" && "$requested" != "$sha" ]]; then
    echo "Superseded request $requested; current main is $sha. No changes made."
    exit 0
fi
if [[ -z "$reset" && -f "$base/deployment.json" ]]; then
    current=$("$base/venv/bin/python" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["commit"] if d.get("profile") == "production" else "")' "$base/deployment.json")
    if [[ "$current" == "$sha" ]] && systemctl is-active --quiet "$service"; then
        curl --fail --silent --max-time 5 http://127.0.0.1:8441/health
        echo "Already deployed $sha. Data preserved."
        exit 0
    fi
fi
available=$(df -Pk "$base" | awk 'NR==2 {print $4}')
[[ "$available" -ge 524288 ]] || { echo 'Need at least 512 MiB free'; exit 1; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
release="$base/releases/$stamp-${sha:0:12}"
backup="$base/backups/$stamp-${sha:0:12}"
install -d -o gibddtest -g gibddtest -m 0750 "$release"
git --git-dir="$base/repository.git" archive "$sha" | runuser -u gibddtest -- tar -x -C "$release"
if find "$release" -type l -print -quit | grep -q .; then
    echo 'Symbolic links are not allowed in deployment sources' >&2
    exit 2
fi
printf '%s\n' "$sha" > "$release/DEPLOYED_COMMIT"
chown -Rh gibddtest:gibddtest "$release"
locksha=$(sha256sum "$release/requirements.lock.txt" | awk '{print $1}')
venv="$base/environments/$locksha"
if [[ ! -f "$venv/.installed" ]]; then
    install -d -o gibddtest -g gibddtest -m 0750 "$venv"
    runuser -u gibddtest -- "$base/venv/bin/python" -m venv "$venv"
    runuser -u gibddtest -- "$venv/bin/python" -m pip install --no-input --disable-pip-version-check -r "$release/requirements.lock.txt"
    touch "$venv/.installed"
fi
# Only temporary SQLite fixtures are used here. No test touches the live database.
(
    cd "$release"
    runuser -u gibddtest -- env PYTHONDONTWRITEBYTECODE=1 "$venv/bin/python" -m pytest -q -p no:cacheprovider --tb=short --junitxml="$release/deployment-tests.xml"
)
chown -Rh root:gibddtest "$release" "$venv"
find "$release" "$venv" -type d -exec chmod 0750 {} +
find "$release" "$venv" -type f -exec chmod g+rX {} +
install -d -m 0700 "$backup"
cp /etc/systemd/system/"$service" "$backup/service.original"
if [[ -f "$dropin" ]]; then cp "$dropin" "$backup/dropin.original"; fi
stopped=0
backup_complete=0
rollback() {
    result=$?
    trap - ERR
    if [[ "$stopped" == 1 ]]; then
        systemctl stop "$service" || true
        if [[ "$backup_complete" == 1 ]]; then
            [[ ! -L "$data" && $(realpath "$data") == "$data" ]] || exit "$result"
            mv -- "$data/media" "$backup/failed-media"
            tar -xzf "$backup/media.tar.gz" -C "$data"
            cat "$backup/database.dump" | runuser -u gibddpg -- "$pg/pg_restore" --clean --if-exists --single-transaction --exit-on-error -d gibdd
        fi
        if [[ -f "$backup/dropin.original" ]]; then
            cp "$backup/dropin.original" "$dropin"
        else
            rm -f -- "$dropin"
        fi
        systemctl daemon-reload
        systemctl start "$service" || true
        echo "Deployment failed; previous service and data restored. Backup: $backup" >&2
    fi
    exit "$result"
}
trap rollback ERR
trap 'false' HUP INT TERM
systemctl stop "$service"
stopped=1
runuser -u gibddpg -- "$pg/pg_dump" --format=custom --dbname=gibdd > "$backup/database.dump"
tar -czf "$backup/media.tar.gz" -C "$data" media
backup_complete=1
systemd-run --quiet --wait --pipe --collect --unit="gibdd-schema-$stamp" \
    --uid=gibddtest --gid=gibddtest --working-directory="$release" \
    -p EnvironmentFile=/etc/gibdd/production.env \
    "$venv/bin/python" -B -m scripts.init_db
install -d -m 0755 "$(dirname "$dropin")"
cat > "$dropin" <<UNIT
[Service]
WorkingDirectory=$release
ExecStart=
ExecStart=$venv/bin/python -B -m uvicorn app.main:app --host 127.0.0.1 --port 8441 --workers 1 --limit-concurrency 24 --ws-max-size 1024 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log
Environment=DEPLOYED_COMMIT=$sha
UNIT
systemctl daemon-reload
systemctl start "$service"
healthy=0
for attempt in $(seq 1 45); do
    if curl --fail --silent --max-time 2 http://127.0.0.1:8441/health | grep -q '"status":"ok"'; then healthy=1; break; fi
    sleep 1
done
if [[ "$healthy" != 1 ]]; then
    echo 'New version did not become healthy' >&2
    false
fi
systemctl is-active --quiet "$service"
"$venv/bin/python" - "$base/deployment.json" "$sha" "$release" "$backup" "$reset" <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
path, sha, release, backup, reset = sys.argv[1:]
temporary = Path(path + '.tmp')
temporary.write_text(json.dumps({'commit': sha, 'release': release, 'backup': backup,
    'profile': 'production', 'database': 'PostgreSQL', 'data_reset': False, 'deployed_at': datetime.now(timezone.utc).isoformat()}, indent=2))
os.replace(temporary, path)
PY
stopped=0
trap - ERR
trap - HUP INT TERM
echo "Deployed production $sha. PostgreSQL/media backup: $backup. Data preserved."
