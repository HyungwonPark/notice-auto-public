#!/usr/bin/env bash
# Install or update the CSP notice systemd units and the nginx site. Idempotent; run as root.
#   ops/systemd/install.sh            copy units and nginx config, create runtime paths, reload
#   ops/systemd/install.sh --enable   also enable and start timers, trigger path, web and nginx
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
nginx_src="$here/../nginx"
units=(csp-notice-scan.service csp-notice-scan.timer csp-notice-scan.path csp-notice-backup.service
       csp-notice-backup.timer csp-notice-web.service)

install -d -m 0750 -o cspnotice -g cspnotice /data/csp-notice/var /data/csp-notice/backup
install -d -m 0755 -o cspnotice -g cspnotice /data/csp-notice/static  # read by nginx
[ -e /data/csp-notice/var/scan.request ] || install -m 0640 -o cspnotice -g cspnotice /dev/null /data/csp-notice/var/scan.request
# Extra mailboxes: one <name>.env each (POP3_USER, POP3_PASSWORD, LABEL, ...), readable by the service only.
install -d -m 0750 -o root -g cspnotice /etc/csp-notice/mailboxes
if [ ! -e /etc/csp-notice/secret_key ]; then  # Django session/CSRF signing key, never in git
  (umask 027; /data/csp-notice/venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(50))' > /etc/csp-notice/secret_key)
  chgrp cspnotice /etc/csp-notice/secret_key
fi

for unit in "${units[@]}"; do
  install -m 0644 "$here/$unit" "/etc/systemd/system/$unit"
done
systemd-analyze verify "${units[@]/#//etc/systemd/system/}"
systemctl daemon-reload

[ -e /etc/nginx/nginx.conf.orig ] || cp -p /etc/nginx/nginx.conf /etc/nginx/nginx.conf.orig
install -m 0644 "$nginx_src/nginx.conf" /etc/nginx/nginx.conf
install -m 0644 "$nginx_src/csp-notice.conf" /etc/nginx/conf.d/csp-notice.conf
nginx -t -q
if systemctl is-active --quiet nginx; then systemctl reload nginx; fi

if [ "${1:-}" = "--enable" ]; then
  systemctl enable --now csp-notice-scan.timer csp-notice-scan.path csp-notice-backup.timer \
    csp-notice-web.service nginx.service
fi
systemctl list-unit-files 'csp-notice*' --no-legend
