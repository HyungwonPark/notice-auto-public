"""작업 기록: keep one flat row per customer notice up to date with the notice itself (see models.Record)."""
from django.db.models import Min, Prefetch

from customers.models import PartnerChannel
from mailcollect.models import Notice

from .models import Dispatch, Notification, NotificationEvent, Record, WorkClosure
from .service import sent_at

MAIN_TAB = "작업 공지"
FIELDS = ("tab", "work_key", "work_title", "starts_at", "ends_at", "kind", "round", "label", "account", "customer",
          "recipients", "status", "kt_received_at", "sent_at", "staff_name", "staff_confirmed_at", "closed_by",
          "closed_at")


def _values(n: Notification, partners, closures, manual) -> dict:
    channel = partners.get(n.account)
    closure = closures.get((n.work_id, channel.pk if channel else None)) if n.work_id else None
    copies = [sent_at(c) for c in n.copies.all()] + manual.get(("sens", n.pk), [])
    return {
        "tab": channel.label if channel else MAIN_TAB,
        "work_key": n.work.key if n.work_id else "",
        "work_title": n.work.title if n.work_id else n.notice.title,
        "starts_at": n.work.starts_at if n.work_id else n.notice.starts_at,
        "ends_at": n.work.ends_at if n.work_id else n.notice.ends_at,
        "kind": n.kind, "round": n.round, "label": n.kind_label, "account": n.account, "customer": n.display_name,
        "recipients": "; ".join(n.recipients), "status": n.status,
        "kt_received_at": sent_at(n.notice),
        "sent_at": min(copies) if copies else manual.get(n.pk),
        "staff_name": n.staff_name, "staff_confirmed_at": n.staff_confirmed_at,
        "closed_by": closure.closed_by if closure else "", "closed_at": closure.closed_at if closure else None,
    }


def sync() -> int:
    """Create a row for every notice without one and refresh the rows that changed. Returns how many changed.

    Rows whose notice is gone (purged) are left as they are: that is the point of them."""
    partners = PartnerChannel.by_account()
    closures = {(c.work_id, c.channel_id): c for c in WorkClosure.objects.all()}
    manual = dict(NotificationEvent.objects.filter(status=Notification.Status.MANUAL_SENT_RECORDED)
                  .values_list("notification_id").annotate(at=Min("at")).values_list("notification_id", "at"))
    for pk, at in Dispatch.objects.filter(mode=Dispatch.Mode.LIVE, state__in=[Dispatch.State.DONE, Dispatch.State.PARTIAL]
                                          ).values_list("notification_id", "created_at"):
        manual.setdefault(("sens", pk), []).append(at)  # sent by this site through SENS
    existing = {r.notification_id: r for r in Record.objects.exclude(notification=None)}
    rows = (Notification.objects.select_related("work", "notice__seen__header")
            .prefetch_related(Prefetch("copies", queryset=Notice.objects.select_related("seen__header"))))
    new, changed = [], []
    for n in rows:
        values = _values(n, partners, closures, manual)
        record = existing.get(n.pk)
        if record is None:
            new.append(Record(notification=n, **values))
        elif any(getattr(record, f) != v for f, v in values.items()):
            for f, v in values.items():
                setattr(record, f, v)
            changed.append(record)
    Record.objects.bulk_create(new)
    Record.objects.bulk_update(changed, [*FIELDS, "updated_at"])
    return len(new) + len(changed)
