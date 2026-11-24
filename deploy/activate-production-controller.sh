#!/usr/bin/env bash
# Root administrator, after the PostgreSQL migration and gibdd.service are ready.
set -euo pipefail
[[ $EUID == 0 ]] || exit 2
folder=$(cd -- "$(dirname -- "$0")" && pwd)
[[ -f /etc/gibdd/production.env ]] || exit 2
systemctl is-active --quiet gibdd.service
systemctl is-active --quiet gibdd-postgresql.service
install -o root -g root -m 0755 "$folder/update-test-vds.sh" /usr/local/sbin/gibdd-staging-deploy
install -o root -g root -m 0755 "$folder/update-production-vds.sh" /usr/local/sbin/gibdd-production-deploy
install -o root -g root -m 0755 "$folder/dispatch-vds-deploy.sh" /usr/local/sbin/gibdd-test-deploy
install -o root -g root -m 0600 /dev/null /etc/gibdd/production.enabled
echo 'Production auto-deployment activated; database reset is not supported.'
