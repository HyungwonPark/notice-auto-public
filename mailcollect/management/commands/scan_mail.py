from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from customers import opsdb
from mailcollect import headers
from mailcollect.config import ConfigError, extra_env_files, load_config
from mailcollect.models import Mailbox, MessageSeen, Notice, ScanRun
from mailcollect.notice_store import parse_pending
from mailcollect.scanner import AlreadyRunning, mailbox_for, prune_seen, scan
from notifications import dispatch
from notifications.records import sync as sync_records
from notifications.service import apply_internal_lists, match_copies, prepare_notifications


class Command(BaseCommand):
    help = (
        "Read-only POP3 scan of every enabled mailbox: classify new mail by header, retrieve bodies of confirmed "
        "notices (never DELE)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--env-file", default=settings.CSP_POP3_ENV_FILE)
        parser.add_argument("--extra-dir", default=settings.CSP_POP3_EXTRA_DIR, help="one <name>.env per extra mailbox")
        parser.add_argument("--batch-size", type=int, default=settings.CSP_SCAN["batch_size"])
        parser.add_argument("--body-batch-size", type=int, default=settings.CSP_SCAN["body_batch_size"])
        parser.add_argument("--max-seconds", type=float, default=settings.CSP_SCAN["max_seconds"])

    def handle(self, *args, env_file, extra_dir, batch_size, body_batch_size, max_seconds, **options):
        scan_options = dict(
            batch_size=batch_size,
            body_batch_size=body_batch_size,
            max_seconds=max_seconds,
            timeout=settings.CSP_SCAN["timeout"],
            lock_path=settings.CSP_SCAN["lock_path"],
        )
        try:
            configs = [load_config(env_file, **scan_options)]  # the main mailbox is required
        except ConfigError as exc:
            raise CommandError(str(exc)) from None
        problems, broken = [], set()
        for name, path in extra_env_files(extra_dir):
            try:
                configs.append(load_config(path, name=name, **scan_options))
            except ConfigError as exc:
                problems.append(f"{name}: {exc}")
                broken.add(name)
        # A mailbox whose file is gone (or whose login the file now names differently, e.g. pop3.env moved to
        # another account) is no longer read: switch its row off so the dashboard does not wait for it.
        current = {(c.host, c.port, c.username) for c in configs}
        for box in Mailbox.objects.filter(enabled=True).exclude(name__in=broken):
            if (box.host, box.port, box.username) not in current:
                box.enabled = False
                box.save(update_fields=["enabled"])
                self.stdout.write(f"[{box.name}] no mailbox file names this login any more: disabled")

        runs = []
        for cfg in configs:
            if not mailbox_for(cfg).enabled:
                self.stdout.write(f"[{cfg.name}] disabled: check with `manage.py mailboxes --probe {cfg.name}`, "
                                  f"then `--enable {cfg.name}`")
                continue
            try:
                runs.append(scan(cfg))
            except AlreadyRunning as exc:
                raise CommandError(str(exc)) from None

        # The company DB's customer rows first, so new drafts and re-judged notices use today's contacts.
        # A failure is recorded for the customers page and said here; it never stops the collection.
        if opsdb.configured():
            db = opsdb.sync()
            if not db.ok:
                self.stdout.write(f"opsdb: failed: {db.error}")
            elif db.batch:
                self.stdout.write(f"opsdb: {db.rows} rows, customers reloaded ({db.batch.review_rows} to review"
                                  f"{f', {db.lifted} 안 쓰는 계정 in use again' if db.lifted else ''})")

        # The 사내/안 쓰는 계정 lists before any new draft: an account the DB just added under a 사내 고객명 is never
        # sent to, and one it just lifted from 안 쓰는 계정 is a customer again.
        internal = apply_internal_lists()
        if internal["accounts marked"]:
            self.stdout.write(f"internal: {internal['accounts marked']} account(s) marked by customer name")

        # Local only, never the mail server: new bodies -> notices -> drafts -> copy evidence.
        parsed = parse_pending()
        prepared, matched = prepare_notifications(), match_copies()
        # Sending last: the mailboxes were just read and copies matched, so anything sent from Outlook in the
        # meantime has already left 발송 대기 (notifications/dispatch.py). Trouble is reported, never fatal.
        try:
            sent = dispatch.run()
        except ConfigError as exc:
            sent = None
            self.stdout.write(f"sending: settings: {exc}")
        except Exception as exc:  # noqa: BLE001 — collection must go on whatever sending does
            sent = None
            self.stdout.write(f"sending: failed: {type(exc).__name__}: {exc}")
        if sent:
            self.stdout.write("sending: " + ", ".join(f"{k}={v}" for k, v in sorted(sent.items())))
        sync_records()  # the rows that outlive the yearly purge follow every change
        pruned = prune_seen()

        for run in runs:
            self.report(run)
            if run.status in (ScanRun.Status.FAILED, ScanRun.Status.LOGIN_FAILED):
                problems.append(f"{run.mailbox.name or 'main'}: scan {run.status}: {run.error}")
        if parsed:
            review = sum(n for (_, status), n in parsed.items() if status == Notice.Status.REVIEW)
            self.stdout.write(f"notices parsed={sum(parsed.values())} review={review}")
        if prepared or matched:
            self.stdout.write("notifications: " + ", ".join(f"{k}={v}" for k, v in sorted((prepared + matched).items())))
        if pruned:
            self.stdout.write(f"forgotten: {pruned} message(s) gone from the server for over 30 days, never kept")
        if problems:
            raise CommandError("; ".join(problems))

    def report(self, run):
        self.stdout.write(
            f"[{run.mailbox.name or 'main'}] "
            f"status={run.status} server={run.server_count} new={run.new_count} headers={run.headers_fetched} "
            f"matched={run.matched} bodies={run.bodies_fetched} gone={run.gone_count} "
            f"gone_unscanned={run.gone_unscanned} body_lost={run.body_lost} "
            f"pending={run.pending_left} bodies_left={run.bodies_left}"
            + (f" skipped={run.skipped}" if run.skipped else "")
        )
        if run.status == ScanRun.Status.UNCHANGED:
            return
        classified = MessageSeen.objects.filter(mailbox=run.mailbox, classified_at__gte=run.started_at)
        counts = Counter(classified.values_list("category", flat=True))
        self.stdout.write("categories: " + " ".join(f"{c}={counts.get(c, 0)}" for c in headers.CATEGORIES))
        # Subjects only for categories whose header is kept; the rest never print more than a count.
        for seen in classified.filter(category__in=headers.KEPT).select_related("header").order_by("category", "pk"):
            self.stdout.write(f"  {seen.category:<13} {seen.header.subject}")
