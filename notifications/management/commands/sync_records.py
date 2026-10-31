from django.core.management.base import BaseCommand

from notifications.records import sync


class Command(BaseCommand):
    help = "Bring the 작업 기록 rows up to date with the notices (done by every scan as well)."

    def handle(self, *args, **options):
        self.stdout.write(f"records: {sync()} added or changed")
