from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from customers.importer import CsvError, import_csv
from customers.models import CustomerAccount


class Command(BaseCommand):
    help = (
        "Load a customer CSV (account,userid,name,회사명,담당자,email,phone) as the current rows of one kind. "
        "Previous CSV rows of that kind become inactive. Prints counts and problem types, never values."
    )

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--kind", required=True, choices=CustomerAccount.Kind.values)
        parser.add_argument("--force", action="store_true", help="load even if this exact file was loaded last")

    def handle(self, *args, path, kind, force, **options):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise CommandError(f"cannot read {path}: {exc.strerror}") from None
        try:
            r = import_csv(raw, path.name, kind, force)
        except CsvError as exc:
            raise CommandError(str(exc)) from None
        if r.batch is None:
            self.stdout.write(f"unchanged: same file as batch {r.unchanged_batch.pk}, nothing loaded (use --force to reload)")
            return
        self.stdout.write(
            f"batch {r.batch.pk} ({kind}): rows={r.rows} ok={r.rows - r.review} review={r.review} "
            f"replaced={r.replaced} repeated_accounts={r.repeated} emails={r.emails} kept_web_edits={r.kept_web}"
        )
        for problem, n in r.problems.most_common():
            self.stdout.write(f"  problem: {problem} x{n}")
        if r.ignored:
            self.stdout.write(f"  ignored column(s): {', '.join(r.ignored)}")
        from notifications.service import refresh_from_master  # notifications depends on customers, not back

        moved = refresh_from_master()
        if moved:
            self.stdout.write("notifications: " + ", ".join(f"{k}={v}" for k, v in sorted(moved.items())))
