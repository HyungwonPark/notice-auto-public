#!/usr/bin/env bash
# Deploy the committed HEAD of /data/csp-notice/app to /data/csp-notice/release, which the systemd units
# run. Uncommitted edits never reach production, and collection is paused while the code and the DB
# schema change together; the web service restarts on the new code. Run as root.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
repo=/data/csp-notice/app
release=/data/csp-notice/release
python=/data/csp-notice/venv/bin/python

[ -z "$(git -C "$repo" status --porcelain)" ] || { echo "commit first: the working tree has changes" >&2; exit 1; }

was_enabled=$(systemctl is-enabled csp-notice-scan.timer 2>/dev/null || true)
systemctl stop csp-notice-scan.timer csp-notice-scan.path 2>/dev/null || true
while systemctl is-active --quiet csp-notice-scan.service; do sleep 1; done  # let a running scan finish

new=$(mktemp -d /data/csp-notice/release.new.XXXX)
git -C "$repo" archive HEAD | tar -x -C "$new"
git -C "$repo" rev-parse HEAD > "$new/REVISION"
chgrp -R cspnotice "$new"
chmod -R u+rwX,g+rX,g-w,o-rwx "$new"
rm -rf "$release.old"
if [ -d "$release" ]; then mv "$release" "$release.old"; fi
mv "$new" "$release"

runuser -u cspnotice -- "$python" "$release/manage.py" migrate --noinput
"$release/ops/systemd/install.sh" >/dev/null
runuser -u cspnotice -- "$python" "$release/manage.py" collectstatic --noinput --clear >/dev/null
if systemctl is-enabled --quiet csp-notice-web.service 2>/dev/null; then
  systemctl restart csp-notice-web.service
fi
if [ "$was_enabled" = enabled ]; then
  systemctl start csp-notice-scan.timer csp-notice-scan.path
fi
echo "deployed $(cat "$release/REVISION") (previous kept in $release.old)"
