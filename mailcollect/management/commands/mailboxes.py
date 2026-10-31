from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from collections import Counter

from customers.models import PartnerChannel
from mailcollect import headers, ktform
from mailcollect.config import ConfigError, extra_env_files, load_config
from mailcollect.models import MessageSeen
from mailcollect.pop3 import LoginRejected, open_mailbox
from mailcollect.scanner import AlreadyRunning, is_duplicate, mailbox_for, scan_lock

UIDL_LINE = 40  # bytes per UIDL listing line, roughly


class Command(BaseCommand):
    help = "List the mailboxes, check one's size (STAT only, read-only), or enable/disable one for scanning."

    def add_arguments(self, parser):
        parser.add_argument("--probe", metavar="NAME", help="log in, send STAT, log out: message count and size")
        parser.add_argument("--sample", type=int, default=0, metavar="N",
                            help="with --probe: also read the headers (TOP 0) of the newest N and count their categories")
        parser.add_argument("--enable", metavar="NAME")
        parser.add_argument("--disable", metavar="NAME")
        parser.add_argument("--read-skipped", metavar="NAME",
                            help="also read the messages skipped at the first listing (still on the server), newest first")
        parser.add_argument("--leave-oldest", type=int, default=0, metavar="N",
                            help="with --read-skipped: leave the N oldest skipped, about to leave the server anyway")
        parser.add_argument("--env-file", default=settings.CSP_POP3_ENV_FILE)
        parser.add_argument("--extra-dir", default=settings.CSP_POP3_EXTRA_DIR)

    def handle(self, *args, probe, sample, enable, disable, read_skipped, leave_oldest, env_file, extra_dir, **options):
        files = [("main", env_file), *extra_env_files(extra_dir)]
        configs = {}
        for name, path in files:
            try:
                configs[name] = load_config(path, name=name, lock_path=settings.CSP_SCAN["lock_path"],
                                            timeout=settings.CSP_SCAN["timeout"], batch_size=settings.CSP_SCAN["batch_size"])
            except ConfigError as exc:
                configs[name] = exc

        target = probe or enable or disable or read_skipped
        if target:
            cfg = configs.get(target)
            if cfg is None:
                raise CommandError(f"no mailbox file named {target} (known: {', '.join(configs)})")
            if isinstance(cfg, ConfigError):
                raise CommandError(str(cfg))
            mailbox = mailbox_for(cfg)
            if probe:
                self.probe(cfg, sample)
            elif read_skipped:
                # Rows were created newest first, so the highest ids are the oldest messages.
                skipped = MessageSeen.objects.filter(
                    mailbox=mailbox, state=MessageSeen.State.SKIPPED, gone_at__isnull=True).order_by("pk")
                ids = list(skipped.values_list("pk", flat=True))
                ids = ids[: len(ids) - leave_oldest] if leave_oldest else ids
                queued = 0
                for i in range(0, len(ids), 500):
                    queued += MessageSeen.objects.filter(pk__in=ids[i:i + 500]).update(state=MessageSeen.State.PENDING)
                runs = -(-queued // settings.CSP_SCAN["batch_size"])
                self.stdout.write(f"{target}: {queued} skipped message(s) queued; about {runs} scans "
                                  f"({runs * 5 // 60} h {runs * 5 % 60} min at one scan per 5 minutes)")
            else:
                mailbox.enabled = bool(enable)
                mailbox.save(update_fields=["enabled"])
                self.stdout.write(f"{target}: {'enabled, read from the next scan' if enable else 'disabled'}")
            return

        for name, cfg in configs.items():
            if isinstance(cfg, ConfigError):
                self.stdout.write(f"{name:<12} config error: {cfg}")
                continue
            mailbox = mailbox_for(cfg)
            last = mailbox.runs.order_by("-pk").first()
            seen = MessageSeen.objects.filter(mailbox=mailbox, gone_at__isnull=True)
            self.stdout.write(
                f"{name:<12} {cfg.title}, collect={cfg.collect} backfill={cfg.backfill or 'all'}, "
                f"{'enabled' if mailbox.enabled else 'disabled'}, on server {seen.count()} "
                f"(pending {seen.filter(state=MessageSeen.State.PENDING).count()}, "
                f"skipped {seen.filter(state=MessageSeen.State.SKIPPED).count()}), last run "
                + (f"{last.status} {timezone.localtime(last.started_at):%m-%d %H:%M}" if last else "none")
            )

    def probe(self, cfg, sample=0):
        """Read-only: STAT, and with sample the headers of the newest messages (categories and counts only)."""
        found = Counter()
        try:
            with scan_lock(cfg.lock_path), open_mailbox(cfg) as conn:
                count, octets = conn.stat()
                partners, form = PartnerChannel.senders(), ktform.current()
                for number in range(count, max(count - sample, 0), -1):
                    info = headers.parse_headers(conn.top(number, 0)[1])
                    category = headers.restrict(headers.classify(info, partners, form), cfg.collect)
                    found[category] += 1
                    found["already collected"] += category in headers.KEPT and is_duplicate(info.message_id)
        except AlreadyRunning:
            raise CommandError("a scan is running; try again in a minute") from None
        except LoginRejected as exc:
            raise CommandError(f"{cfg.name}: login rejected: {exc}") from None
        except OSError as exc:
            raise CommandError(f"{cfg.name}: cannot connect: {exc}") from None
        self.stdout.write(
            f"{cfg.name}: {count} messages, {octets / 1e6:.1f} MB on the server. A full UIDL listing is about "
            f"{count * UIDL_LINE / 1e6:.1f} MB; the first scan reads headers of the newest "
            f"{min(count, cfg.backfill) if cfg.backfill else count} ({cfg.batch_size} per run)."
        )
        if sample:
            self.stdout.write(f"newest {min(sample, count)}: " + ", ".join(f"{k}={v}" for k, v in found.most_common()))
