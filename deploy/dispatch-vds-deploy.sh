#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || exit 2
if [[ -f /etc/gibdd/production.enabled ]]; then
    exec /usr/local/sbin/gibdd-production-deploy "$@"
else
    exec /usr/local/sbin/gibdd-staging-deploy "$@"
fi
