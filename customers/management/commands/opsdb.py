from django.core.management.base import BaseCommand, CommandError

from customers import opsdb
from mailcollect.config import ConfigError


class Command(BaseCommand):
    help = ("The company operations DB view of customer accounts. --probe connects and reports columns and a row "
            "count; --sync loads the view as the current 일반 customers (done before every scan as well).")

    def add_arguments(self, parser):
        parser.add_argument("--probe", action="store_true", help="connect, list the view's columns, count its rows")
        parser.add_argument("--sync", action="store_true", help="load the view's rows as the 일반 customers")
        parser.add_argument("--force", action="store_true", help="with --sync: load even if nothing changed")

    def handle(self, *args, probe, sync, force, **options):
        try:
            cfg = opsdb.load_config()
        except ConfigError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"{cfg.user}@{cfg.host}:{cfg.port} {cfg.table}")
        if probe:
            try:
                result = opsdb.probe(cfg)
            except Exception as exc:  # driver errors carry the server's words; show them, not a traceback
                raise CommandError(f"{type(exc).__name__}: {exc}") from None
            self.stdout.write(f"connected in {result['connect_seconds']}s, done in {result['total_seconds']}s")
            self.stdout.write(f"columns ({len(result['columns'])}): " + ", ".join(result["columns"]))
            self.stdout.write(f"rows: {result['rows']}")
        if sync:
            run = opsdb.sync(cfg, force=force)
            if not run.ok:
                raise CommandError(run.error)
            self.stdout.write(f"rows: {run.rows}, " + (f"loaded as batch {run.batch.pk} ({run.batch.rows} rows, "
                              f"{run.batch.review_rows} to review)" if run.batch else "unchanged since the last load"))
            from notifications.service import refresh_from_master  # notifications depends on customers, not back

            moved = refresh_from_master()
            if moved:
                self.stdout.write("notifications: " + ", ".join(f"{k}={v}" for k, v in sorted(moved.items())))
