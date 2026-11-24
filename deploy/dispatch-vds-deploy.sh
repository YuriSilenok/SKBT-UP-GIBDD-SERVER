#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || exit 2
exec 8>/opt/gibdd-test-20261117/dispatch.lock
flock -w 900 8
if [[ -f /etc/gibdd/production.enabled ]]; then
    exec /usr/local/sbin/gibdd-production-deploy "$@"
else
    exec /usr/local/sbin/gibdd-staging-deploy "$@"
fi
