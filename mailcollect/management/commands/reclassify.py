from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from customers.models import PartnerChannel
from mailcollect import headers, ktform
from mailcollect.models import MessageBody, MessageHeader, MessageSeen, Notice


class Command(BaseCommand):
    help = "Re-apply the current parsing and classification rules to stored headers (no mail server access)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--recheck-other",
            action="store_true",
            help="queue messages without a stored header for a fresh TOP on the next scan",
        )
        parser.add_argument(
            "--recheck",
            choices=[c for c in headers.CATEGORIES if c not in headers.KEPT],
            help="queue only this header-less category (e.g. kt_other) for a fresh TOP on the next scan",
        )

    def handle(self, *args, recheck_other, recheck, **options):
        changes = Counter()
        refreshed = 0
        dropped = 0
        partners = PartnerChannel.senders()
        form = ktform.current()
        with transaction.atomic():
            for header in MessageHeader.objects.select_related("seen", "seen__mailbox"):
                seen = header.seen
                info = headers.parse_headers(bytes(header.raw).split(b"\r\n"))
                category = headers.restrict(headers.classify(info, partners, form), seen.mailbox.collect)
                if category != seen.category:
                    changes[f"{seen.category}->{category}"] += 1
                    seen.category = category
                    seen.save(update_fields=["category"])
                if category not in headers.BODY:  # only BODY categories keep the message and its parse
                    notice = Notice.objects.filter(seen=seen).first()
                    if notice is not None and (notice.notifications.exists() or notice.confirms.exists()):
                        changes[f"kept, linked to a notification: {category}"] += 1
                    else:
                        gone = MessageBody.objects.filter(seen=seen).delete()[0]
                        if notice is not None:
                            notice.delete()
                            gone += 1
                        dropped += bool(gone)
                if category not in headers.KEPT:
                    header.delete()  # only KEPT categories store a header
                    continue
                fields = {
                    "subject": info.subject,
                    "from_addr": info.from_addr,
                    "from_display": info.from_display,
                    "date_raw": info.date_raw,
                    "date": info.date,
                    "message_id": info.message_id,
                    "parse_errors": "\n".join(info.errors),
                }
                stale = [name for name, value in fields.items() if getattr(header, name) != value]
                if stale:
                    for name in stale:
                        setattr(header, name, fields[name])
                    header.save(update_fields=stale)
                    refreshed += 1
            requeued = 0
            if recheck_other or recheck:
                rows = MessageSeen.objects.filter(state=MessageSeen.State.CLASSIFIED, gone_at__isnull=True)
                rows = rows.filter(category=recheck) if recheck else rows.exclude(category__in=headers.KEPT)
                requeued = rows.update(state=MessageSeen.State.PENDING, category="", classified_at=None)
        self.stdout.write("reclassified: " + (", ".join(f"{k}={v}" for k, v in sorted(changes.items())) or "none"))
        self.stdout.write(f"refreshed headers: {refreshed}")
        self.stdout.write(f"mail no longer kept, body and parse dropped: {dropped}")
        if recheck_other or recheck:
            self.stdout.write(f"requeued without header: {requeued}")
