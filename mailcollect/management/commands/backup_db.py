import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = "Write a consistent copy of the SQLite DB (VACUUM INTO) and delete copies older than --keep-days."

    def add_arguments(self, parser):
        parser.add_argument("--dir", type=Path, default=settings.CSP_BACKUP_DIR)
        parser.add_argument("--keep-days", type=int, default=settings.CSP_BACKUP_KEEP_DAYS)

    def handle(self, *args, dir, keep_days, **options):
        dir.mkdir(exist_ok=True)
        target = dir / f"db-{timezone.localtime():%Y%m%d-%H%M%S}.sqlite3"
        with connection.cursor() as cursor:
            cursor.execute("VACUUM INTO %s", [str(target)])
        cutoff = time.time() - keep_days * 86400
        removed = [p for p in dir.glob("db-*.sqlite3") if p != target and p.stat().st_mtime < cutoff]
        for path in removed:
            path.unlink()
        self.stdout.write(f"backup {target} ({target.stat().st_size} bytes), removed {len(removed)} old")
