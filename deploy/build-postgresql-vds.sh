#!/bin/bash
set -euo pipefail
umask 077
prefix=/opt/gibdd-postgresql/17.12
work=/opt/gibdd-postgresql/build
if [ -x "$prefix/bin/postgres" ]; then "$prefix/bin/postgres" --version; exit 0; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends bison flex libssl-dev libreadline-dev zlib1g-dev
id gibddpg >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/gibdd-postgresql --shell /usr/sbin/nologin gibddpg
install -d -m 0755 /opt/gibdd-postgresql
install -d -o gibddpg -g gibddpg -m 0750 "$work"
cd "$work"
curl -fSL --retry 3 https://ftp.postgresql.org/pub/source/v17.12/postgresql-17.12.tar.bz2 -o postgresql-17.12.tar.bz2
curl -fSL --retry 3 https://ftp.postgresql.org/pub/source/v17.12/postgresql-17.12.tar.bz2.sha256 -o postgresql-17.12.tar.bz2.sha256
sha256sum -c postgresql-17.12.tar.bz2.sha256
chown gibddpg:gibddpg postgresql-17.12.tar.bz2
runuser -u gibddpg -- tar -xjf postgresql-17.12.tar.bz2
cd postgresql-17.12
runuser -u gibddpg -- ./configure --prefix="$prefix" --without-icu --with-ssl=openssl
nice -n 10 runuser -u gibddpg -- make -j2
make install
chgrp gibddpg "$prefix"
chmod 0750 "$prefix"
find "$prefix/bin" "$prefix/lib" "$prefix/share" -type d -exec chgrp gibddpg {} + -exec chmod 0750 {} +
find "$prefix/share/timezone" -type f -exec chgrp gibddpg {} + -exec chmod g+r {} +
"$prefix/bin/postgres" --version
