"""환경 설정 > KT 공지 양식: what a changed form would read differently (before saving), and reading again after.
Counts and KT's own label words only; never values from the mail."""
from collections import Counter
from datetime import timedelta

from django.utils import timezone

from customers.models import PartnerChannel
from mailcollect import headers, notices
from mailcollect.ktform import KtForm
from mailcollect.models import MessageHeader, MessageSeen, Notice
from mailcollect.notice_store import store
from notifications.models import Notification, NotificationEvent
from notifications.records import sync as sync_records
from notifications.service import prepare_notifications, redraft_unsent

DAYS = 30  # preview and 다시 읽기 look at KT mail of this many days
RECHECK = timedelta(days=1)  # KT 기타 다시 분류: mail first seen within this
NOTICE_REVIEW = "notice needs review"  # service._initial's reason for a notice read with problems
CATEGORY = {
    headers.KT_NOTICE: "KT 작업 공지", headers.KT_URGENT: "KT 긴급 공지", headers.KT_COMPLETE: "KT 완료 공지",
    headers.KT_CANCEL: "KT 취소/변경", headers.KT_POSTPONE: "KT 작업 연기", headers.KT_CHECK: "KT 점검 요청", headers.KT_OTHER: "KT 기타",
    headers.SUSPECT: "의심 메일",
    headers.OTHER: "그 밖의 메일",
}


def _since():
    return timezone.now() - timedelta(days=DAYS)


def preview(old: KtForm, new: KtForm) -> dict:
    """Stored KT mail of the last DAYS days read with both forms: category moves, and how many give each field."""
    partners = PartnerChannel.senders()
    moves = Counter()
    kt_like = headers.MATCHED | {headers.SUSPECT}  # the stored headers a KT rule can move (KT 기타 keeps none)
    for header in MessageHeader.objects.filter(date__gte=_since(), seen__category__in=kt_like):
        info = headers.parse_headers(bytes(header.raw).split(b"\r\n"))
        a, b = headers.classify(info, partners, old), headers.classify(info, partners, new)
        if a != b:
            moves[(CATEGORY.get(a, a), CATEGORY.get(b, b))] += 1
    rows = {"작업 일시": [0, 0], "대상 계정": [0, 0], "작업 내용": [0, 0], "문제 없음": [0, 0]}
    unknown, total = Counter(), 0
    bodies = Notice.objects.filter(category__in=headers.MATCHED, seen__header__date__gte=_since())
    for notice in bodies.select_related("seen__body"):
        total += 1
        for i, form in enumerate((old, new)):
            info = notices.parse_notice(notice.seen.body.text, form)
            rows["작업 일시"][i] += bool(info.windows)
            rows["대상 계정"][i] += bool(info.accounts)
            rows["작업 내용"][i] += bool(info.title)
            rows["문제 없음"][i] += not info.problems
            if i:
                unknown.update(info.extra)
    return {"days": DAYS, "total": total, "rows": [(k, *v) for k, v in rows.items()],
            "moves": sorted(((a, b, n) for (a, b), n in moves.items()), key=lambda r: -r[2]),
            "unknown": unknown.most_common(8)}


def reread_candidates():
    """KT notices of the last DAYS days read with problems (e.g. a label KT renamed), except those already
    under a work with customer notices: reading those again could move them to another work and make a second
    notice for the same customer. KT's twin mails and later sends of a well-read work are status OK: never here."""
    return (Notice.objects.filter(category__in=headers.MATCHED, seen__header__date__gte=_since(),
                                  status=Notice.Status.REVIEW)
            .exclude(work__isnull=False, notifications__isnull=False).distinct())


def reread(form: KtForm) -> Counter:
    """Run under the scan lock (prepare_notifications must not run twice at once). A customer notice made from
    a notice without a work joins the work once found (so prepare_notifications finds it, never makes another),
    and loses the 'notice needs review' reason when the notice now reads cleanly."""
    counts = Counter()
    for notice in list(reread_candidates().select_related("seen__body", "seen__header")):
        notice = store(notice.seen.body, form)
        counts["read"] += 1
        if notice.work_id:
            Notification.objects.filter(notice=notice, work__isnull=True).update(work=notice.work)
        if notice.status != Notice.Status.OK:
            continue
        counts["ok"] += 1
        for n in Notification.objects.filter(notice=notice, status=Notification.Status.NEEDS_REVIEW):
            reasons = [r for r in n.review_reasons.split("\n") if r and r != NOTICE_REVIEW]
            n.review_reasons = "\n".join(reasons)
            if not reasons:
                n.status = Notification.Status.DRAFT_READY
            n.save()
            NotificationEvent.objects.create(notification=n, status=n.status, note="KT 공지 다시 읽음 (KT 공지 양식)")
    counts.update({f"notifications {k}": v for k, v in prepare_notifications().items()})
    counts["redrafted"] = redraft_unsent()  # drafts follow the new reading and the form's footer
    sync_records()
    return counts


def recheck_candidates():
    return MessageSeen.objects.filter(category=headers.KT_OTHER, state=MessageSeen.State.CLASSIFIED,
                                      gone_at__isnull=True, first_seen_at__gte=timezone.now() - RECHECK)


def recheck() -> int:
    """KT mail filed as 기타 (no header kept) is classified again from a fresh TOP on the next scan."""
    return recheck_candidates().update(state=MessageSeen.State.PENDING, category="", classified_at=None)
