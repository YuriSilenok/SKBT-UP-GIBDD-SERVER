#!/usr/bin/env bash
# Install explicitly as the VDS administrator, with the public key as argument.
set -euo pipefail
[[ $EUID == 0 && $# == 1 ]] || exit 2
base=/opt/gibdd-test-20261117
[[ -d "$base/staging-ready" && ! -L "$base" ]] || exit 2
systemctl cat gibdd-test.service >/dev/null
public_key=$(cat "$1")
[[ "$public_key" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || exit 2
scripts=$(cd -- "$(dirname -- "$0")" && pwd)
chown root:gibddtest "$base"
chmod 0750 "$base"
install -o root -g root -m 0755 "$scripts/update-test-vds.sh" /usr/local/sbin/gibdd-test-deploy
install -o root -g root -m 0755 "$scripts/request-test-deploy.sh" /usr/local/bin/gibdd-test-deploy-request
id gibdd-deploy >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/gibdd-deploy --shell /bin/bash gibdd-deploy
install -d -o root -g root -m 0755 /var/lib/gibdd-deploy /var/lib/gibdd-deploy/.ssh
printf 'restrict,command="/usr/local/bin/gibdd-test-deploy-request" %s\n' "$public_key" > /var/lib/gibdd-deploy/.ssh/authorized_keys
chown root:root /var/lib/gibdd-deploy/.ssh/authorized_keys
chmod 0644 /var/lib/gibdd-deploy/.ssh/authorized_keys
printf 'gibdd-deploy ALL=(root) NOPASSWD: /usr/local/sbin/gibdd-test-deploy *\n' > /etc/sudoers.d/gibdd-test-deploy
chmod 0440 /etc/sudoers.d/gibdd-test-deploy
visudo -cf /etc/sudoers.d/gibdd-test-deploy
echo 'Restricted deployment access installed; no application restart performed.'
