#!/usr/bin/env bash
# Forced SSH command. No shell, extra arguments, or reset option can be forwarded.
set -euo pipefail
[[ ${SSH_ORIGINAL_COMMAND:-} =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected one commit SHA'; exit 2; }
exec sudo -n /usr/local/sbin/gibdd-test-deploy "$SSH_ORIGINAL_COMMAND"
