#!/usr/bin/env bash
# Load a bundle made by ops/migrate/export.sh into this server, after ops/migrate/bootstrap.sh.
# Run as root; docs/MIGRATION.md has the whole move.
#   import.sh BUNDLE.tgz --rehearsal   load it and start only the web, to look around; nothing is collected
#   import.sh BUNDLE.tgz               the move: load it and start collection, backup and web
# A DB already here is kept aside as db.sqlite3.before-<time>, never overwritten.
set -euo pipefail

usage() { sed -n '2,6p' "$0"; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
bundle=${1:-}
[ -f "$bundle" ] || usage
case "${2:-}" in
  --rehearsal) move=0 ;;
  "") move=1 ;;
  *) usage ;;
esac
root=${CSP_ROOT:-}         # a scratch dir only to try this script out: then no service is touched
svc=${CSP_USER:-cspnotice}
data=$root/data/csp-notice
etc=$root/etc/csp-notice
here=$(cd "$(dirname "$0")" && pwd)
python=/data/csp-notice/venv/bin/python

[ -f "$bundle.sha256" ] || { echo "missing $bundle.sha256: copy it together with the bundle" >&2; exit 1; }
(cd "$(dirname "$bundle")" && sha256sum --quiet -c "$(basename "$bundle").sha256")

install -d -m 0700 "$data/migrate"
stage=$(mktemp -d "$data/migrate/import.XXXX")
trap 'rm -rf "$stage"' EXIT
tar -xzf "$bundle" -C "$stage" --no-same-owner
"$python" "$here/dbcheck.py" "$stage/db.sqlite3" > "$stage/check"
grep -v '^\(revision\|created\|mode\) ' "$stage/MANIFEST" | diff - "$stage/check" >/dev/null ||
  { echo "the DB in the bundle does not match its MANIFEST" >&2; exit 1; }

if [ -z "$root" ]; then
  # Code older than the DB would run on a newer schema: pull first.
  rev=$(sed -n 's/^revision //p' "$stage/MANIFEST")
  git -C "$data/app" merge-base --is-ancestor "$rev" HEAD 2>/dev/null ||
    { echo "the old server ran $rev, which $data/app does not contain yet: git pull first" >&2; exit 1; }
  systemctl stop csp-notice-scan.timer csp-notice-scan.path csp-notice-backup.timer csp-notice-web.service 2>/dev/null || true
  while systemctl is-active --quiet csp-notice-scan.service csp-notice-backup.service; do sleep 1; done
fi

db=$data/var/db.sqlite3
install -d -m 0750 -o "$svc" -g "$svc" "$data/var"
if [ -e "$db" ]; then
  aside=$db.before-$(date +%Y%m%d-%H%M%S)
  for suffix in "" -wal -shm; do
    if [ -e "$db$suffix" ]; then mv "$db$suffix" "$aside$suffix"; fi
  done
  echo "kept the previous DB as $aside"
fi
install -m 0640 -o "$svc" -g "$svc" "$stage/db.sqlite3" "$db"

# Mail logins and the secret key, readable by the service only. site.env names this server's own
# address, so the old server's is never brought along.
install -d -m 0750 -o root -g "$svc" "$etc" "$etc/mailboxes"
(cd "$stage/etc/csp-notice" && find . -type f ! -name site.env -print0) |
  while IFS= read -r -d '' f; do install -D -m 0640 -o root -g "$svc" "$stage/etc/csp-notice/$f" "$etc/$f"; done
if [ ! -e "$etc/site.env" ]; then
  hosts=$(hostname -I | tr ' ' '\n' | grep -E '^[0-9]+(\.[0-9]+){3}$' | paste -sd, -)
  printf '# Addresses this site answers to (Django ALLOWED_HOSTS). Add a DNS name here if one is used.\nCSP_ALLOWED_HOSTS=%s\n' \
    "${hosts:+$hosts,}localhost,127.0.0.1" > "$etc/site.env"
  chown root:"$svc" "$etc/site.env"
  chmod 0640 "$etc/site.env"
fi

install -d -m 0700 "$data/private"
cp -R "$stage/private/." "$data/private/"
chown -R root:root "$data/private"
chmod -R go-rwx "$data/private"

echo "loaded:"
cat "$stage/MANIFEST"
[ -z "$root" ] || exit 0

"$data/app/ops/deploy.sh"  # migrate, units, nginx, static files
if command -v getenforce >/dev/null && [ "$(getenforce)" = Enforcing ]; then restorecon -R /data/csp-notice/static; fi
if [ "$move" -eq 1 ]; then
  "$data/release/ops/systemd/install.sh" --enable
  echo "collection runs every 5 minutes; check it with: journalctl -u csp-notice-scan -n 20 --no-pager"
else
  systemctl enable --now nginx.service
  systemctl start csp-notice-web.service
  echo "rehearsal: web only (it stops at the next reboot); collection and backup stay off on this server"
fi
echo "the site answers to these addresses (edit $etc/site.env, then restart csp-notice-web, to change them):"
sed -n 's/^CSP_ALLOWED_HOSTS=//p' "$etc/site.env"
