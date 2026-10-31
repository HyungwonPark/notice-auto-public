"""Which customers a page shows. The 작업 공지 tab shows everyone except 별도 발송 customers; each 별도 발송 tab
shows only its own. KT works are shared, their customer notices and hand closures are not."""
from datetime import timedelta
from functools import reduce
from operator import or_

from django.db.models import Q
from django.utils import timezone

from customers.models import PartnerChannel
from mailcollect import headers
from notifications.models import Notification, WorkClosure

S = Notification.Status
WAITING = (S.DRAFT_READY, S.COPIED, S.SEND_REQUESTED)  # not proven sent yet
COMPLETE_GRACE = timedelta(days=1)  # a 완료 공지 still unsent this long after KT's completion notice is late
INCIDENT_GRACE = timedelta(hours=1)  # an outage notice or 작업 연기 공지 still unsent this long after KT's mail


def partner_accounts() -> list[str]:
    return list(PartnerChannel.by_account())


def accounts_q(channel=None, prefix="") -> Q:
    """Q on Notification.account (prefix "notifications__" from Work) for a tab."""
    field = f"{prefix}account__in"
    if channel is not None:
        return Q(**{field: channel.account_keys})
    others = partner_accounts()
    return ~Q(**{field: others}) if others else Q()


def late_q(now=None, prefix="") -> Q:
    """Unsent although it should be out: a 사전 공지 once the work started, a 완료 공지 a day after KT's, an
    outage's 발생 or 복구 notice and a 작업 연기 공지 an hour after KT's."""
    now = now or timezone.now()
    p = prefix
    work = "starts_at__lt" if p else "work__starts_at__lt"
    incident = Q(**{"incident" if p else "work__incident": True})
    postpone = Q(**{f"{p}notice__category": headers.KT_POSTPONE})
    mail = f"{p}notice__seen__header__date__lt"  # when KT's mail arrived
    return Q(**{f"{p}status__in": WAITING}) & (
        (Q(**{f"{p}kind": Notification.Kind.NOTICE}) & Q(**{work: now}) & ~incident & ~postpone)
        | (Q(**{f"{p}kind": Notification.Kind.COMPLETE}) & Q(**{mail: now - COMPLETE_GRACE}) & ~incident)
        | ((incident | postpone) & Q(**{mail: now - INCIDENT_GRACE}))
    )


def closed_work_ids(channel=None) -> set[int]:
    return set(WorkClosure.objects.filter(channel=channel).values_list("work_id", flat=True))


UNCHECKED = (S.SENT_COPY_CONFIRMED, S.MANUAL_SENT_RECORDED)  # sent; a person's 담당자 확인 is still owed


def needs_attention(channel=None):
    """(review, late, unchecked) notification querysets for a tab: data problems and 발송 추정, late sends, and
    sent ones still waiting for 담당자 확인 (user, 2026-10-19: those need a person too). Works moved to 지난 작업
    by hand are left out of the last two."""
    rows = Notification.objects.filter(accounts_q(channel))
    closed = closed_work_ids(channel)
    review = rows.filter(status__in=[S.NEEDS_REVIEW, S.SENT_COPY_CANDIDATE, S.SEND_FAILED])
    late = rows.filter(late_q()).exclude(work__in=closed)
    unchecked = rows.filter(status__in=UNCHECKED).exclude(work__in=closed)
    return review, late, unchecked


def needs_attention_all():
    """needs_attention for every tab at once (확인 필요 page and its count): 작업 공지 and each 별도 발송 tab, each
    leaving out the works closed on that tab (user, 2026-10-26: partner tabs too, checkable there)."""
    parts = [needs_attention()] + [needs_attention(ch) for ch in PartnerChannel.objects.filter(active=True)]
    return tuple(reduce(or_, kind) for kind in zip(*parts))
