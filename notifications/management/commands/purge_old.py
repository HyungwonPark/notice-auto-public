from django.conf import settings
from django.core.management.base import BaseCommand

from notifications.retention import purge


class Command(BaseCommand):
    help = "Delete works, mail, scan runs and handled suggestions older than --days (counts only)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.CSP_KEEP_DAYS)
        parser.add_argument("--dry-run", action="store_true", help="count what would go, delete nothing")

    def handle(self, *args, days, dry_run, **options):
        counts = purge(days, dry_run=dry_run)
        done = ", ".join(f"{k} {v}" for k, v in counts.items()) or "nothing"
        self.stdout.write(f"{'would delete' if dry_run else 'deleted'} (older than {days} days): {done}")
