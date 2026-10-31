#!/usr/bin/env bash
# Prepare a new Rocky/RHEL 8 or 9 server for the CSP notice service: packages, the cspnotice system
# account, directories and the Python 3.12 venv. Run as root after cloning the repo to /data/csp-notice/app.
# Idempotent. Data and secrets come next with ops/migrate/import.sh (docs/MIGRATION.md).
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
repo=/data/csp-notice/app
[ -f "$repo/manage.py" ] || { echo "clone the repo to $repo first" >&2; exit 1; }

dnf -y install python3.12 python3.12-pip nginx git tar
id cspnotice >/dev/null 2>&1 ||
  useradd --system --home-dir /data/csp-notice --no-create-home --shell /sbin/nologin cspnotice

install -d -m 0755 /data/csp-notice
install -d -m 0750 -o cspnotice -g cspnotice /data/csp-notice/var /data/csp-notice/backup
install -d -m 0755 -o cspnotice -g cspnotice /data/csp-notice/static  # read by nginx
install -d -m 0700 /data/csp-notice/private /data/csp-notice/migrate
install -d -m 0750 -o root -g cspnotice /etc/csp-notice /etc/csp-notice/mailboxes

[ -x /data/csp-notice/venv/bin/python ] || python3.12 -m venv /data/csp-notice/venv
/data/csp-notice/venv/bin/pip install --quiet --upgrade pip
/data/csp-notice/venv/bin/pip install --quiet -r "$repo/requirements.txt"

# The old server runs with SELinux disabled and no firewalld; on a server that has them, let nginx reach
# gunicorn and serve the static files, and open port 80.
if command -v getenforce >/dev/null && [ "$(getenforce)" = Enforcing ]; then
  dnf -y install policycoreutils-python-utils
  setsebool -P httpd_can_network_connect 1
  semanage fcontext -a -t httpd_sys_content_t '/data/csp-notice/static(/.*)?' 2>/dev/null ||
    semanage fcontext -m -t httpd_sys_content_t '/data/csp-notice/static(/.*)?'
  restorecon -R /data/csp-notice/static
  echo "SELinux is enforcing: if a csp-notice unit fails to start, see: ausearch -m avc -ts recent"
fi
if systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-service=http && firewall-cmd --reload
fi

echo "== checks"
timedatectl show -p Timezone -p NTPSynchronized
[ "$(timedatectl show -p Timezone --value)" = Asia/Seoul ] ||
  echo "time zone is not Asia/Seoul: the 03:30 backup runs by this clock (timedatectl set-timezone Asia/Seoul)"
if timeout 5 bash -c '</dev/tcp/mail.example.com/110' 2>/dev/null; then
  echo "mail.example.com:110 reachable"
else
  echo "mail.example.com:110 NOT reachable from this server: collection will fail until it is"
fi
/data/csp-notice/venv/bin/python -c 'import django, pysqlite3; print("django", django.get_version(), "sqlite", pysqlite3.sqlite_version)'
echo "next: copy the bundle from the old server and run ops/migrate/import.sh (docs/MIGRATION.md)"
