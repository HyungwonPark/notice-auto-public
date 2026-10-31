#!/usr/bin/env bash
# Pack what a new server needs that git does not carry: a consistent copy of the DB, /etc/csp-notice
# (mail logins, extra mailboxes, the web secret key) and the customer originals in /data/csp-notice/private.
# Run as root on the old server; docs/MIGRATION.md has the whole move.
#   export.sh --rehearsal   everything keeps running here (for a trial import on the new server)
#   export.sh               the move: collection, backup and web stop here for good first
# The bundle holds real customer data and passwords: copy it server to server (scp), never through git,
# mail or a PC, and delete it on both servers once the move is checked.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
case "${1:-}" in
  --rehearsal) move=0 ;;
  "") move=1 ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
root=${CSP_ROOT:-}         # a scratch dir only to try this script out: then no service is touched
svc=${CSP_USER:-cspnotice}
data=$root/data/csp-notice
here=$(cd "$(dirname "$0")" && pwd)
python=/data/csp-notice/venv/bin/python
units="csp-notice-scan.timer csp-notice-scan.path csp-notice-backup.timer csp-notice-web.service"

if [ "$move" -eq 1 ] && [ -z "$root" ]; then
  # Disabled, not only stopped: a reboot must not bring back a second collector.
  systemctl disable --now $units
  while systemctl is-active --quiet csp-notice-scan.service csp-notice-backup.service; do sleep 1; done
  trap 'echo "failed; this server stays stopped. To resume it: systemctl enable --now $units" >&2' ERR
fi

install -d -m 0700 "$data/migrate"
stage=$(mktemp -d "$data/migrate/export.XXXX")
tmp=$data/var/export.tmp
trap 'rm -rf "$stage" "$tmp"' EXIT
# VACUUM INTO by the service account, so the live DB's -wal/-shm files never become root's.
rm -rf "$tmp"
install -d -m 0700 -o "$svc" -g "$svc" "$tmp"
runuser -u "$svc" -- env CSP_VAR_DIR="$data/var" "$python" "$data/release/manage.py" backup_db --dir "$tmp" >/dev/null
mv "$tmp"/db-*.sqlite3 "$stage/db.sqlite3"

"$python" "$here/dbcheck.py" "$stage/db.sqlite3" > "$stage/MANIFEST"
{
  echo "revision $(cat "$data/release/REVISION")"
  echo "created $(date '+%F %T %z') on $(hostname)"
  echo "mode $([ "$move" -eq 1 ] && echo move || echo rehearsal)"
} >> "$stage/MANIFEST"

name=csp-notice-$(date +%Y%m%d-%H%M%S)$([ "$move" -eq 1 ] || echo -rehearsal).tgz
(umask 077
 tar -czf "$data/migrate/$name" -C "$stage" db.sqlite3 MANIFEST -C "${root:-/}" etc/csp-notice -C "$data" private
 cd "$data/migrate" && sha256sum "$name" > "$name.sha256")

cat "$stage/MANIFEST"
echo
echo "bundle: $data/migrate/$name ($(du -h "$data/migrate/$name" | cut -f1)) and $name.sha256"
echo "copy:   scp $data/migrate/$name $data/migrate/$name.sha256 root@<new server>:/data/csp-notice/migrate/"
if [ "$move" -eq 1 ]; then
  echo "this server no longer collects or serves the web. To go back: systemctl enable --now $units"
fi
