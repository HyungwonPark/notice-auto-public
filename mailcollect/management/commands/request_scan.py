from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from mailcollect.scanner import request_scan


class Command(BaseCommand):
    help = "Ask systemd to run a collection now (same path the web 'check now' button will use)."

    def handle(self, *args, **options):
        requested, last = request_scan(
            settings.CSP_SCAN["request_path"], settings.CSP_SCAN["request_min_interval"]
        )
        if requested:
            self.stdout.write("requested: csp-notice-scan.service will run shortly")
        else:
            age = (timezone.now() - last.finished_at).total_seconds()
            self.stdout.write(f"skipped: last scan finished {age:.0f}s ago (status={last.status})")
