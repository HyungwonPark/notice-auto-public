"""Turn stored bodies into Notice rows (local only, never touches the mail server)."""
import hashlib
import re
from collections import Counter
from datetime import timedelta

from django.db import transaction
from django.db.models import Q

from . import headers, ktform, notices
from .models import MessageBody, Notice, NoticeTarget, Work

# Bump when parsing rules change so parse_pending() re-parses older rows.
# 2: "대상계정" alias, 변경전 window for change/cancel notices
# 3: an empty label never takes the next line as its value, except 대상계정명 with an email address
PARSER_VERSION = 3
KT_ORIGINALS = headers.MATCHED


def parse_pending(reparse: bool = False) -> Counter:
    """Parse bodies that have no Notice yet (or an older parser version). Returns category/status counts."""
    bodies = MessageBody.objects.select_related("seen", "seen__header")
    if not reparse:
        bodies = bodies.filter(Q(seen__notice__isnull=True) | Q(seen__notice__parser_version__lt=PARSER_VERSION))
    counts = Counter()
    form = ktform.current()
    for body in bodies.order_by("seen__header__date", "pk").iterator():  # an outage's 발생 before its 복구
        notice = store(body, form)
        counts[(notice.category, notice.status)] += 1
    return counts


def store(body: MessageBody, form: ktform.KtForm | None = None) -> Notice:
    seen = body.seen
    info = notices.parse_notice(body.text, form or ktform.current())
    first = info.windows[0] if info.windows else (None, None)
    if seen.category in headers.INCIDENTS:
        first = (_as_incident(info, seen, body.text), None)
    work = None
    if seen.category == headers.KT_POSTPONE:
        work = _postponed_work(info, first, seen)
    recipients = []
    if seen.category not in KT_ORIGINALS and hasattr(seen, "header"):
        recipients = headers.recipients(bytes(seen.header.raw).split(b"\r\n"))
    with transaction.atomic():
        notice, _ = Notice.objects.update_or_create(
            seen=seen,
            defaults={
                "work": (work if seen.category == headers.KT_POSTPONE
                         else _incident_work(info, first[0], seen.category) if seen.category in headers.INCIDENTS
                         else _work(info, first) if seen.category in KT_ORIGINALS else None),
                "category": seen.category,
                "title": info.title,
                "detail": info.detail,
                "schedule": info.schedule,
                "starts_at": first[0],
                "ends_at": first[1],
                "windows": [[s.isoformat(), e.isoformat()] for s, e in info.windows],
                "impact": info.impact,
                "audience": info.audience,
                "target": info.target,
                "masked_customer": info.masked_customer,
                "servers": info.servers,
                "lbs": info.lbs,
                "extra": info.extra,
                "recipients": recipients,
                "status": Notice.Status.REVIEW if info.problems else Notice.Status.OK,
                "problems": "\n".join(info.problems),
                "parser_version": PARSER_VERSION,
            },
        )
        notice.targets.all().delete()
        NoticeTarget.objects.bulk_create(NoticeTarget(notice=notice, account=a) for a in info.accounts)
    return notice


INCIDENT_JOIN = timedelta(days=3)  # a 복구 mail joins the 발생 of its account sent this long before it
_TITLE = re.compile(r"\[\s*kt\s*cloud\s*(.*?)\s*(?:공지)?\s*\]", re.I)


def _as_incident(info: notices.NoticeInfo, seen, text: str):
    """An outage notice has no 작업내용 or 작업일시: its title is the subject's ('컴퓨팅 노드 Down 발생'), its
    time the mail's (returned)."""
    header = getattr(seen, "header", None)
    m = _TITLE.search(header.subject if header else "") or _TITLE.search(text)
    info.title = info.title or (m[1] if m else "") or "kt cloud 장애"
    info.problems = [p for p in info.problems if p not in ("no 작업일시", "no 작업내용")]
    return (header.date if header else None) or seen.first_seen_at


def _incident_work(info: notices.NoticeInfo, when, category: str) -> Work | None:
    """발생 opens a work; 복구 closes the latest open one of the same title for one of its accounts."""
    if when is None:
        return None
    title = re.sub(r"\s*(발생|복구)\s*$", "", notices.work_title(info.title)) or notices.work_title(info.title)
    if category == headers.KT_RECOVERY:
        work = (Work.objects.filter(incident=True, title=title, starts_at__lte=when,
                                    starts_at__gte=when - INCIDENT_JOIN, notices__targets__account__in=info.accounts)
                .order_by("-starts_at").first())
        if work is not None:
            if work.ends_at is None or work.ends_at < when:
                work.ends_at = when
                work.save(update_fields=["ends_at"])
            return work
    key = hashlib.sha256(f"incident\n{title}\n{when.isoformat()}".encode()).hexdigest()
    work, _ = Work.objects.get_or_create(key=key, defaults={"title": title, "starts_at": when, "incident": True,
                                                           "ends_at": when if category == headers.KT_RECOVERY else None})
    return work


POSTPONE_JOIN = timedelta(days=30)  # a 작업 연기 공지 joins a work of its title that started at most this before it
NO_POSTPONED_WORK = "작업 연기 공지 without a matching work"


def _postponed_work(info: notices.NoticeInfo, first, seen) -> Work | None:
    """A 작업 연기 공지 (2026-10-25: '작업일시: 추후에 안내 예정') belongs to the work it puts off: the latest one of
    its title for one of its accounts. Its 작업일시 may name no time, which is no problem here; without such a work
    a new date in it makes a work of its own, and with neither it goes to a person."""
    if not info.windows:
        info.problems = [p for p in info.problems if "작업일시" not in p]
    header = getattr(seen, "header", None)
    when = (header.date if header else None) or seen.first_seen_at
    title = notices.work_title(info.title)
    work = None
    if title and info.accounts and when is not None:
        work = (Work.objects.filter(incident=False, title=title, starts_at__gte=when - POSTPONE_JOIN,
                                    notices__targets__account__in=info.accounts)
                .order_by("-starts_at").first())
    work = work or _work(info, first)
    if work is None and info.title and info.accounts:
        info.problems.append(NO_POSTPONED_WORK)
    return work


def _work(info: notices.NoticeInfo, first) -> Work | None:
    start, end = first
    title = notices.work_title(info.title)
    if not title or start is None:
        return None
    key = hashlib.sha256(f"{title}\n{start.isoformat()}".encode()).hexdigest()
    work, created = Work.objects.get_or_create(key=key, defaults={"title": title, "starts_at": start, "ends_at": end})
    if created:
        _link_rework(work)
    return work


def _link_rework(work: Work) -> None:
    """A new work of the same title after a postponed one (a 작업 연기 공지 joined it) is its 재작업: the postponed
    one then leaves 진행 중 (web.views._row). Linked once, when the new work is made."""
    old = (Work.objects.filter(title=work.title, incident=False, starts_at__lt=work.starts_at,
                               notices__category=headers.KT_POSTPONE, reworks__isnull=True)
           .exclude(pk=work.pk).order_by("-starts_at").first())
    if old is not None:
        work.rework_of = old
        work.save(update_fields=["rework_of"])
