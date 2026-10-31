"""Delete what is older than the retention period (a year by default), to keep the disk small.

Run nightly right after the backup, so whatever goes is still in the last 14 days of backups."""
from collections import Counter
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from mailcollect.models import MessageSeen, ScanRun, Work
from suggestions.models import Suggestion

from .models import Notification
from .records import sync


def purge(days: int, dry_run: bool = False) -> Counter:
    """Works that started before the cutoff with their customer notices and history, mail first seen before it
    (once it left the server, and unless a newer notice still points to it), scan runs, and handled suggestions.
    The customer master, 사내 계정, 별도 발송 settings and the 작업 기록 rows are not time records and stay."""
    cutoff = timezone.now() - timedelta(days=days)
    counts = Counter()
    with transaction.atomic():
        counts["records refreshed"] = sync()  # the flat 작업 기록 rows stay after the rows below go
        works = Work.objects.filter(starts_at__lt=cutoff)
        old_notifications = Notification.objects.filter(work__in=works) | Notification.objects.filter(
            work__isnull=True, created_at__lt=cutoff)
        counts["works"] = works.count()
        counts["notifications"] = old_notifications.count()
        if not dry_run:
            old_notifications.delete()  # their events and closures go with them
            works.delete()
        mail = (MessageSeen.objects.filter(first_seen_at__lt=cutoff, gone_at__isnull=False)
                .exclude(notice__notifications__isnull=False).exclude(notice__confirms__isnull=False))
        runs = ScanRun.objects.filter(started_at__lt=cutoff)
        handled = Suggestion.objects.exclude(status__in=Suggestion.OPEN).filter(handled_at__lt=cutoff)
        counts["mails"], counts["scan runs"], counts["suggestions"] = mail.count(), runs.count(), handled.count()
        if not dry_run:
            mail.delete()  # header, body, parsed notice and its accounts go with it
            runs.delete()
            handled.delete()
    return +counts
