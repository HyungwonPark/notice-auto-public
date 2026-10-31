"""One read-only scan: STAT, list UIDLs, read headers of new messages (TOP), retrieve bodies of
confirmed categories (RETR), record. Nothing on the server is ever deleted or changed."""
import fcntl
import hashlib
import logging
import os
import poplib
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from customers.models import PartnerChannel

from . import body, headers, ktform
from .models import Mailbox, MessageBody, MessageHeader, MessageSeen, ScanRun
from .pop3 import LoginRejected, open_mailbox, server_text

log = logging.getLogger(__name__)

_CHUNK = 500  # ids per IN (...) query, well under SQLite's bound-parameter limit
PENDING = MessageSeen.State.PENDING


class AlreadyRunning(Exception):
    pass


@contextmanager
def scan_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(f"another scan is running (lock {path})") from None
        yield
    finally:
        os.close(fd)


def request_scan(request_path, min_interval: float) -> tuple[bool, ScanRun | None]:
    """Ask systemd (csp-notice-scan.path) for a scan now, unless one finished within min_interval seconds.

    Never contacts the mail server itself; the systemd service does, under the scan lock.
    """
    last = ScanRun.objects.filter(finished_at__isnull=False).order_by("-finished_at").first()
    if last and timezone.now() - last.finished_at < timedelta(seconds=min_interval):
        return False, last
    Path(request_path).write_text(timezone.now().isoformat(), encoding="ascii")
    return True, last


def mailbox_for(cfg) -> Mailbox:
    """The Mailbox row of a config; a new extra mailbox is registered disabled. No server access."""
    mailbox, created = Mailbox.objects.get_or_create(
        host=cfg.host, port=cfg.port, username=cfg.username, defaults={"enabled": cfg.name == "main"})
    fields = {"name": cfg.name, "label": cfg.label, "collect": cfg.collect}
    stale = [k for k, v in fields.items() if getattr(mailbox, k) != v]
    if stale:
        for k in stale:
            setattr(mailbox, k, fields[k])
        mailbox.save(update_fields=stale)
    return mailbox


def scan(cfg, connect=open_mailbox) -> ScanRun:
    with scan_lock(cfg.lock_path):
        mailbox = mailbox_for(cfg)
        run = ScanRun.objects.create(mailbox=mailbox)
        try:
            with connect(cfg) as conn:
                deadline = time.monotonic() + cfg.max_seconds
                run.stat_count, run.stat_octets = conn.stat()
                if _nothing_to_do(mailbox, run):
                    run.status, run.server_count = ScanRun.Status.UNCHANGED, run.stat_count
                else:
                    listing = list_uidls(conn)
                    run.server_count = len(listing)
                    sync_seen(mailbox, listing, run, cfg.backfill)
                    fetch_headers(conn, mailbox, listing, run, cfg, deadline)
                    fetch_bodies(conn, mailbox, listing, run, cfg, deadline)
        except LoginRejected as exc:
            run.status, run.error = ScanRun.Status.LOGIN_FAILED, str(exc)
        except (poplib.error_proto, OSError) as exc:
            run.status, run.error = ScanRun.Status.FAILED, f"{type(exc).__name__}: {server_text(exc)}"

        run.pending_left = _pending_headers(mailbox).count()
        run.bodies_left = _pending_bodies(mailbox).count()
        if run.status == ScanRun.Status.RUNNING:
            done = run.pending_left == 0 and run.bodies_left == 0
            run.status = ScanRun.Status.OK if done else ScanRun.Status.PARTIAL
        run.finished_at = timezone.now()
        run.save()
        log.info(
            "scan %s %s: server=%s new=%d skipped=%d headers=%d matched=%d bodies=%d gone=%d gone_unscanned=%d "
            "body_lost=%d pending=%d bodies_left=%d",
            mailbox.name or "main", run.status, run.server_count, run.new_count, run.skipped, run.headers_fetched,
            run.matched, run.bodies_fetched, run.gone_count, run.gone_unscanned, run.body_lost, run.pending_left,
            run.bodies_left,
        )
        return run


def _pending_headers(mailbox):
    return MessageSeen.objects.filter(mailbox=mailbox, state=PENDING, gone_at__isnull=True)


def _pending_bodies(mailbox):
    return MessageSeen.objects.filter(
        mailbox=mailbox, category__in=headers.BODY, body__isnull=True, gone_at__isnull=True
    )


def _nothing_to_do(mailbox, run) -> bool:
    """Same STAT as the last complete run and no pending work: skip the UIDL listing."""
    last = (
        mailbox.runs.exclude(pk=run.pk)
        .filter(status__in=[ScanRun.Status.OK, ScanRun.Status.UNCHANGED])
        .order_by("-pk")
        .first()
    )
    return (
        last is not None
        and (last.stat_count, last.stat_octets) == (run.stat_count, run.stat_octets)
        and not _pending_headers(mailbox).exists()
        and not _pending_bodies(mailbox).exists()
    )


def list_uidls(conn) -> dict[str, int]:
    """UIDL -> message number. Message numbers are valid only inside this session."""
    _, lines, _ = conn.uidl()
    listing = {}
    for line in lines:
        num, _, uidl = line.decode("ascii", "replace").strip().partition(" ")
        if uidl.strip() and num.isdigit():
            listing[uidl.strip()] = int(num)
    return listing


def sync_seen(mailbox, listing, run, backfill=0):
    """Record new UIDLs and mark the ones that left the server (Outlook's 14-day removal).

    On a mailbox's first listing with a backfill limit, only the newest `backfill` messages are queued; older
    ones are recorded as skipped and never read, so a large existing mailbox does not mean days of TOP commands."""
    now = timezone.now()
    known = {
        uidl: (pk, gone_at)
        for pk, uidl, gone_at in MessageSeen.objects.filter(mailbox=mailbox).values_list("pk", "uidl", "gone_at")
    }
    fresh = sorted((u for u in listing if u not in known), key=listing.get, reverse=True)  # newest first
    keep = len(fresh) if known or not backfill else backfill
    new = [
        MessageSeen(mailbox=mailbox, uidl=u, first_seen_at=now, last_seen_at=now,
                    state=PENDING if i < keep else MessageSeen.State.SKIPPED)
        for i, u in enumerate(fresh)
    ]
    MessageSeen.objects.bulk_create(new, batch_size=_CHUNK)
    run.skipped = len(new) - min(keep, len(new))
    gone = [pk for uidl, (pk, gone_at) in known.items() if gone_at is None and uidl not in listing]
    back = [pk for uidl, (pk, gone_at) in known.items() if gone_at is not None and uidl in listing]
    for ids in _chunks(gone):
        leaving = MessageSeen.objects.filter(pk__in=ids)
        run.gone_unscanned += leaving.filter(state=PENDING).count()
        run.body_lost += leaving.filter(category__in=headers.BODY, body__isnull=True).count()
        leaving.update(gone_at=now)
    for ids in _chunks(back):
        MessageSeen.objects.filter(pk__in=ids).update(gone_at=None, last_seen_at=now)
    run.new_count, run.gone_count = len(new), len(gone)
    if run.gone_unscanned or run.body_lost:
        log.warning(
            "left the server before being read: %d header(s), %d body(ies)", run.gone_unscanned, run.body_lost
        )


def fetch_headers(conn, mailbox, listing, run, cfg, deadline):
    batch = sorted(_pending_headers(mailbox), key=lambda seen: listing[seen.uidl], reverse=True)  # newest first
    partners = PartnerChannel.senders()
    form = ktform.current()
    for seen in batch[: cfg.batch_size]:
        if time.monotonic() >= deadline:
            break
        try:
            _, lines, _ = conn.top(listing[seen.uidl], 0)
        except poplib.error_proto as exc:
            _raise_unless_server_refusal(exc)
            seen.attempts += 1  # the server refused this message; retry next run
            seen.save(update_fields=["attempts"])
            continue
        info = headers.parse_headers(lines)
        category = headers.restrict(headers.classify(info, partners, form), cfg.collect)
        if category in headers.KEPT and is_duplicate(info.message_id):
            category = headers.DUPLICATE
        with transaction.atomic():
            seen.state, seen.category, seen.classified_at = MessageSeen.State.CLASSIFIED, category, timezone.now()
            seen.attempts += 1
            seen.save(update_fields=["state", "category", "classified_at", "attempts"])
            if category in headers.KEPT:
                MessageHeader.objects.create(
                    seen=seen,
                    raw=b"\r\n".join(lines),
                    subject=info.subject,
                    from_addr=info.from_addr,
                    from_display=info.from_display,
                    date_raw=info.date_raw,
                    date=info.date,
                    message_id=info.message_id,
                    parse_errors="\n".join(info.errors),
                )
        run.headers_fetched += 1
        if category in headers.MATCHED:
            run.matched += 1


def is_duplicate(message_id: str) -> bool:
    """The same mail (by Message-ID) was already kept, from any mailbox."""
    return bool(message_id.strip()) and MessageHeader.objects.filter(message_id=message_id).exists()


def fetch_bodies(conn, mailbox, listing, run, cfg, deadline):
    batch = sorted(_pending_bodies(mailbox), key=lambda seen: listing[seen.uidl], reverse=True)  # newest first
    for seen in batch[: cfg.body_batch_size]:
        if time.monotonic() >= deadline:
            break
        try:
            _, lines, octets = conn.retr(listing[seen.uidl])
        except poplib.error_proto as exc:
            _raise_unless_server_refusal(exc)
            seen.body_attempts += 1
            seen.save(update_fields=["body_attempts"])
            continue
        raw = b"\r\n".join(lines) + b"\r\n"  # poplib strips line ends and dot-stuffing
        info = body.extract(raw)
        errors = list(info.errors)
        if len(raw) != octets:
            errors.append(f"size {len(raw)} differs from the {octets} octets received")
        with transaction.atomic():
            MessageBody.objects.create(
                seen=seen,
                raw=raw,
                size=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                text=info.text,
                html=info.html,
                parse_errors="\n".join(errors),
            )
            seen.body_attempts += 1
            seen.save(update_fields=["body_attempts"])
        run.bodies_fetched += 1


def prune_seen(days: int = 30) -> int:
    """Forget messages that left the server more than `days` ago and were never kept (UIDL and category only).

    A busy alarm mailbox would otherwise add a row per message forever. Kept categories stay: their header and
    body are evidence. A pruned UIDL that comes back is simply read again."""
    cutoff = timezone.now() - timedelta(days=days)
    old = MessageSeen.objects.filter(gone_at__lt=cutoff).exclude(category__in=headers.KEPT)
    deleted, _ = old.filter(header__isnull=True, body__isnull=True).delete()
    return deleted


def _raise_unless_server_refusal(exc: poplib.error_proto):
    """A -ERR reply carries the server's bytes; poplib reports a closed connection as the str "-ERR EOF"."""
    if not (exc.args and isinstance(exc.args[0], bytes)):
        raise exc


def _chunks(items):
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]
