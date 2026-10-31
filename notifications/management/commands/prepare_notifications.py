from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from notifications.models import Notification
from notifications.service import match_copies, prepare_notifications, redraft_unsent, unmatched_copies
from notifications.service import rebuild as rebuild_all


class Command(BaseCommand):
    help = "Draft notifications for parsed KT notices and link 예시클라우드 copies (local only, counts only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild", action="store_true",
            help="drop and redraft every notification (refused once staff copied or recorded one)",
        )
        parser.add_argument(
            "--redraft", action="store_true",
            help="rewrite every draft nobody copied from this site with the current template",
        )

    def handle(self, *args, rebuild, redraft, **options):
        if rebuild:
            try:
                self.stdout.write(f"rebuild: dropped {rebuild_all()} row(s)")
            except RuntimeError as exc:
                raise CommandError(str(exc)) from None
        if redraft:
            self.stdout.write(f"redraft: {redraft_unsent()} draft(s) rewritten")
        prepared = prepare_notifications()
        matched = match_copies()
        self.stdout.write("prepare: " + (", ".join(f"{k}={v}" for k, v in sorted(prepared.items())) or "nothing new"))
        self.stdout.write("copies: " + (", ".join(f"{k}={v}" for k, v in sorted(matched.items())) or "nothing new")
                          + f"; copies linked to nothing: {unmatched_copies()}")
        statuses = Counter(Notification.objects.values_list("status", flat=True))
        self.stdout.write("status: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
        reasons = Counter(r for text in Notification.objects.exclude(review_reasons="").values_list("review_reasons", flat=True)
                          for r in text.split("\n"))
        for reason, n in reasons.most_common():
            self.stdout.write(f"  reason: {reason} x{n}")
