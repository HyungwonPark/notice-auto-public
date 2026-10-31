"""Sending customer notices through SENS, never twice (user decisions, 2026-10-21).

Runs inside every scan (scan_mail), after the mailboxes were read and copies matched: a notice someone already
sent by hand has left 발송 대기 before this looks at it. The rules:

1. Automatic sending takes 발송 대기 notices whose KT mail arrived within a day, with recipients, not of a 별도
   발송 account; a 사전 공지 whose work has ended is left to a person. 사전 공지 2차, 3차 are sent like the first.
   KT outage notices (발생, 복구) are sent at once like any other (user, 2026-10-22), and so is a 작업 연기 공지,
   whose work's window may already be over (user, 2026-10-25).
   Notices made before sending was switched on go too, after rule 6's check (user, 2026-10-21: the day's
   waiting notices go out once ON; older KT mail never does).
2. Per notice and mode at most one attempt is under way, unanswered, accepted or done (Dispatch constraint).
3. An attempt SENS may have received (timeout, 5xx) is never repeated blindly: SENS's list of accepted
   requests is checked first, and a request is taken as ours only if its body is the one we sent.
4. A retry follows only a failure that proves nothing was queued (refused connection, throttling), 5 minutes
   later. The third failure in a row, or one that can never pass, makes it 발송 실패: a person presses 발송하기.
5. Once SENS accepted a mail, this site never sends it again; SENS's result decides: 완료 → 고객 발송 확인,
   일부 실패 → 발송 추정, 실패 → 발송 실패.
6. Before any mail goes out (automatic, 발송하기, 테스트 발송), SENS's list is asked for a request to one of the
   notice's recipients with its subject since the KT mail arrived that this site did not send (staff send by
   hand through the same SENS project; user decision 2026-10-21: always check). One found holds the notice
   (발송 보류 → 발송 실패, 확인 필요; its copy usually confirms it). 발송하기 then sends unless a newer one turned
   up. A failed lookup sends nothing and counts like a refused attempt.

Test mode (its button left the pages on 2026-10-22; the tests and a rehearsal server still use it) sends every mail to MAIL_TEST_TO only, titled "[테스트] …" so its copy is
never taken for a customer send (headers.NC24_PREFIXES must start the subject), and leaves the notice's status
alone; rule 6 still looks up the real recipients, so a held notice shows what live would have done."""
import fcntl
import hashlib
import os
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import escape

from customers.models import PartnerChannel
from mailcollect import headers

from .models import Dispatch, Notification, NotificationEvent, SendingSince, SendSetting
from . import sens
from .sens import Client, SensConfig, SensError
from .service import sent_at

S = Notification.Status
D = Dispatch.State
RETRY_AFTER = timedelta(minutes=5)
MAX_FAILURES = 3
FRESH = timedelta(days=1)  # KT mail older than this (e.g. a backlog read later) is not sent automatically
UNKNOWN_GRACE = timedelta(minutes=2)  # SENS lists an accepted request well within this
STALE_SENDING = timedelta(minutes=2)  # a 'sending' attempt this old lost its process mid-call
LOOKBACK = timedelta(minutes=10)  # rule 6 looks this far before the KT mail's Date (clocks differ)
LOOKUP_MAX = timedelta(days=30)  # and never further back than this
TEST_PREFIX = "[테스트] "
MAIL_TEMPLATE = Path(__file__).with_name("mail_template.html").read_text(encoding="utf-8")
assert MAIL_TEMPLATE.count("__CONTENT__") == 1
SENT_DONE, SENT_PARTIAL, SENT_FAILED = "COMPLETED", "PARTIAL_FAILED", ("FAILED", "CANCELED")
REASONS = ("sending failed 3 times", "SENS rejected the mail", "SENS could not deliver", "some recipients failed (SENS)",
           "SENS has the same mail")


class Busy(Exception):
    pass


@contextmanager
def send_lock():
    """One sender at a time (the scan and a test button may meet)."""
    fd = os.open(settings.CSP_SEND_LOCK, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another sending run is in progress") from None
        yield
    finally:
        os.close(fd)


def html(text: str) -> str:
    """The plain draft in the company mail form (mail_template.html, the one every notice sent so far uses):
    its __CONTENT__ becomes the lines, escaped and joined by "<br />" as in those mails."""
    lines = "<br />\n".join(escape(line) for line in text.split("\n"))
    return MAIL_TEMPLATE.replace("__CONTENT__", lines)


def digest(body: str | None) -> str:
    return hashlib.sha256(body.encode()).hexdigest() if body else ""


def build(cfg: SensConfig, n: Notification) -> tuple[str, list[str], list[str], list[str], dict]:
    """(subject, to, cc, bcc, request body). Test mode: to MAIL_TEST_TO only, the real recipients named on top."""
    if cfg.mode == "test":
        subject, to, cc, bcc = TEST_PREFIX + n.subject, list(cfg.test_to), [], []
        text = f"테스트 발송입니다. 실제 받는 사람: {', '.join(n.recipients) or '없음'}\n\n{n.body}"
    else:
        subject, to = n.subject, list(n.recipients)
        cc = [a for a in cfg.cc if a not in to][:30]  # SENS takes at most 30 CC and 30 BCC
        bcc = [a for a in cfg.bcc if a not in to and a not in cc][:30]
        text = n.body
    body = {
        "senderAddress": cfg.sender_address, "title": subject, "body": html(text),
        "recipients": [{"address": a, "type": "TO"} for a in to] + [{"address": a, "type": "CC"} for a in cc]
                      + [{"address": a, "type": "BCC"} for a in bcc],
        "individual": False,  # one mail to the customer's contacts, in-house CC and BCC: as the notices are sent now
        "advertising": False,
    }
    if cfg.sender_name:
        body["senderName"] = cfg.sender_name
    return subject, to, cc, bcc, body


MODES = ("off", "live")  # 자동 발송 OFF / ON on 환경 설정 (user, 2026-10-21: no test mode there)


def chosen_mode() -> str:
    """OFF or ON as chosen in 환경 설정; OFF until someone turns it on."""
    row = SendSetting.objects.order_by("-pk").first()
    return "live" if row and row.mode == "live" else "off"


def config(path=None) -> SensConfig:
    """sens.env's settings, sending only when 환경 설정 says ON and sens.env does not say off."""
    cfg = sens.load_config(path)
    mode = "live" if cfg.mode == "live" and chosen_mode() == "live" else "off"
    return cfg if mode == cfg.mode else replace(cfg, mode=mode)


def choose(mode: str, by: str) -> None:
    """환경 설정's switch. SendingSince only records when it was last turned ON (rule 1 no longer uses it)."""
    with transaction.atomic():
        SendSetting.objects.create(mode=mode, by=by)
        if mode == "live":
            SendingSince.objects.update_or_create(mode=mode, defaults={"since": timezone.now()})


def register(cfg: SensConfig) -> None:
    if cfg.on:
        SendingSince.objects.get_or_create(mode=cfg.mode, defaults={"since": timezone.now()})


def eligible(cfg: SensConfig, now=None):
    """Notices to send automatically in this run, oldest first (rule 1)."""
    now = now or timezone.now()
    active = Dispatch.objects.filter(mode=cfg.mode, state__in=Dispatch.ACTIVE).values("notification_id")
    rows = (Notification.objects.filter(status=S.DRAFT_READY).exclude(pk__in=active)
            .exclude(account__in=list(PartnerChannel.by_account()))
            .select_related("work", "notice__seen__header").order_by("pk"))
    for n in rows:
        last = n.dispatches.filter(mode=cfg.mode, state=D.FAILED).order_by("-pk").first()
        if last and now - last.created_at < RETRY_AFTER:
            continue
        if automatic(n, cfg, now):
            yield n


def automatic(n: Notification, cfg: SensConfig, now=None) -> bool:
    """Rule 1 for one notice: the scans send it by themselves, so the page offers no 발송하기 (user, 2026-10-22).
    A 별도 발송 account is never sent (eligible leaves those out)."""
    now = now or timezone.now()
    if n.status != S.DRAFT_READY or not n.recipients:
        return False
    if n.send_requested_by:  # 발송하기: a person decided, the automatic limits below do not apply
        return True
    if n.dispatches.filter(mode=cfg.mode, state=D.HELD).exists():  # rule 6: a person decides from here
        return False
    if sent_at(n.notice) < now - FRESH:
        return False
    if (n.kind == Notification.Kind.NOTICE and n.work_id and not n.work.incident
            and n.notice.category != headers.KT_POSTPONE and (n.work.ends_at or n.work.starts_at) < now):
        return False
    return not (cfg.mode == "test" and n.dispatches.filter(mode=cfg.mode, state=D.FAILED).count() >= MAX_FAILURES)


def _event(n: Notification, note: str, by: str = "") -> None:
    NotificationEvent.objects.create(notification=n, status=n.status, note=note, actor=by)


def _give_up(n: Notification, reason: str, note: str) -> None:
    n.status = S.SEND_FAILED
    n.add_reason(reason)
    n.send_requested_by = ""
    n.save()
    _event(n, note)


def send_one(cfg: SensConfig, client: Client, n: Notification, by: str = "", now=None) -> str:
    """One attempt; returns accepted / unknown / failed / gave up / held / global / already."""
    subject, to, cc, bcc, body = build(cfg, n)
    try:
        with transaction.atomic():
            d = Dispatch.objects.create(notification=n, mode=cfg.mode, state=D.SENDING, by=by or n.send_requested_by,
                                        subject=subject, to=to, cc=cc, bcc=bcc, body_sha=digest(body["body"]))
    except IntegrityError:
        return "already"  # rule 2: another attempt is under way, unanswered, accepted or done
    live = cfg.mode == "live"
    try:
        found = sent_elsewhere(client, n, cfg.mode, passed=bool(d.by), now=now)
    except SensError as exc:  # a lookup sends nothing: never 'unknown'
        return _failed(d, n, live, SensError("global" if exc.kind == "global" else "retry",
                                             f"발송 전 확인 실패: {exc}", exc.status, exc.code))
    if found:
        return _hold(d, n, live, found)
    try:
        answer = client.send(body)
    except SensError as exc:
        return _failed(d, n, live, exc)
    d.state, d.request_id, d.finished_at = D.ACCEPTED, str(answer.get("requestId", ""))[:100], timezone.now()
    d.save()
    if live:
        n.status, n.send_failures, n.send_requested_by = S.SEND_REQUESTED, 0, ""
        n.save()
        _event(n, "SENS 발송 접수 (" + ("발송하기" if d.by else "자동") + ")", d.by)
    return "accepted"


def sent_elsewhere(client: Client, n: Notification, mode: str, passed: bool = False, now=None) -> list[str]:
    """Rule 6: requests SENS has for this notice's subject and any of its recipients since its KT mail, other
    than this site's own sends (and, once a person pressed 발송하기, other than those already held for)."""
    now = now or timezone.now()
    since = max(sent_at(n.notice) - LOOKBACK, now - LOOKUP_MAX)
    skip = set(Dispatch.objects.exclude(request_id="").exclude(state=D.HELD).values_list("request_id", flat=True))
    if passed:
        skip |= set(n.dispatches.filter(mode=mode, state=D.HELD).values_list("request_id", flat=True))
    found = []
    for address in dict.fromkeys(n.recipients):
        for r in client.find_requests(since, now, address, n.subject):
            rid = str(r.get("requestId") or "")
            if rid and rid not in skip and rid not in found and r.get("status") not in SENT_FAILED:
                found.append(rid)
    return found


def _hold(d: Dispatch, n: Notification, live: bool, found: list[str]) -> str:
    d.state, d.request_id, d.finished_at = D.HELD, found[0][:100], timezone.now()
    d.error = ("받는 사람과 제목이 같은 메일이 SENS에 있음 (사이트에서 보낸 것 아님): " + ", ".join(found))[:500]
    d.save()
    for rid in found[1:]:  # one row per request, so 발송하기 passes over each of them
        Dispatch.objects.create(notification=n, mode=d.mode, state=D.HELD, by=d.by, subject=d.subject, to=d.to,
                                request_id=rid[:100], error=d.error, finished_at=d.finished_at)
    if live:
        _give_up(n, "SENS has the same mail", "발송 보류: SENS에 같은 메일 있음")
    return "held"


def _failed(d: Dispatch, n: Notification, live: bool, exc: SensError) -> str:
    d.error, d.error_kind, d.finished_at = str(exc)[:500], exc.kind, timezone.now()
    if exc.kind == "unknown":
        d.state = D.UNKNOWN
        d.save()
        if live:
            _event(n, "발송 응답 없음, SENS 접수 여부 확인 중")
        return "unknown"
    d.state = D.FAILED
    d.save()
    if exc.kind == "global" or not live:
        return "global" if exc.kind == "global" else "failed"
    return _count_failure(n, exc.kind == "permanent", str(exc))


def _count_failure(n: Notification, permanent: bool, error: str) -> str:
    n.send_failures += 1
    if permanent:
        _give_up(n, "SENS rejected the mail", f"발송 실패: SENS 거절 ({error[:120]})")
        return "gave up"
    if n.send_failures >= MAX_FAILURES:
        _give_up(n, "sending failed 3 times", f"발송 실패 {n.send_failures}회, 자동 발송 중단 ({error[:120]})")
        return "gave up"
    n.save()  # a 발송하기 stands for the retries: an old notice would otherwise fall out of rule 1 and stall
    _event(n, f"발송 실패 {n.send_failures}회, 5분 뒤 다시 보냄 ({error[:120]})")
    return "failed"


def _resolve_unknown(client: Client, d: Dispatch, now) -> str:
    """Rule 3: was the unanswered attempt accepted? Only a request whose mail body is the one sent counts
    (compared by the digest kept at sending: the draft or the form may have changed since)."""
    if now - d.created_at < UNKNOWN_GRACE or not d.to:
        return "waiting"
    known = set(Dispatch.objects.exclude(request_id="").values_list("request_id", flat=True))
    found = [r for r in client.find_requests(d.created_at - timedelta(minutes=1), now, d.to[0], d.subject)
             if r.get("requestId") and r["requestId"] not in known]
    for r in found:
        if digest(_body_of(client, r["requestId"])) == d.body_sha:
            d.state, d.request_id, d.error = D.ACCEPTED, r["requestId"][:100], ""
            d.save()
            if d.mode == "live":
                n = d.notification
                n.status, n.send_failures, n.send_requested_by = S.SEND_REQUESTED, 0, ""
                n.save()
                _event(n, "SENS 발송 접수 확인 (응답 없던 요청)")
            return "adopted"
    d.state, d.error = D.FAILED, (d.error + ", SENS에 접수 기록 없음")[:500]
    d.save()
    if d.mode == "live" and d.notification.status == S.DRAFT_READY:
        return _count_failure(d.notification, False, "응답 없음, 접수 기록 없음")
    return "failed"


def _body_of(client: Client, request_id: str) -> str | None:
    base = f"{client.base}/{request_id}/mails"
    mails = client._call("GET", base).get("mails") or []
    if not mails:
        return None
    return client._call("GET", f"{base}/{mails[0]['mailId']}").get("body")


def _settle(d: Dispatch, status: str) -> str:
    """Rule 5: SENS's final word on an accepted mail."""
    n = d.notification
    live = d.mode == "live" and n.status == S.SEND_REQUESTED  # a matched copy may have moved it already
    if status == SENT_DONE:
        d.state = D.DONE
        if live:
            n.status, n.match_basis = S.SENT_COPY_CONFIRMED, n.match_basis or "sens"
            n.save()
            _event(n, "SENS 발송 완료")
    elif status == SENT_PARTIAL:
        d.state = D.PARTIAL
        if live:
            n.status = S.SENT_COPY_CANDIDATE
            n.add_reason("some recipients failed (SENS)")
            n.save()
            _event(n, "SENS 일부 받는 사람 전달 실패")
    else:
        d.state, d.error = D.FAILED, f"SENS {status}"
        if live:
            _give_up(n, "SENS could not deliver", f"발송 실패: SENS {status}")
    d.finished_at = timezone.now()
    d.save()
    return d.state


def resolve(cfg: SensConfig, client: Client, now=None) -> Counter:
    """Bring every open attempt forward: lost ones become unknown, unknown ones are looked up, accepted ones
    get SENS's result."""
    now = now or timezone.now()
    counts = Counter()
    Dispatch.objects.filter(state=D.SENDING, created_at__lt=now - STALE_SENDING).update(
        state=D.UNKNOWN, error="process ended mid-call")
    for d in Dispatch.objects.filter(state__in=[D.UNKNOWN, D.ACCEPTED]).select_related("notification").order_by("pk"):
        try:
            if d.state == D.UNKNOWN:
                counts["unknown " + _resolve_unknown(client, d, now)] += 1
            elif d.request_id:
                status = client.request_status(d.request_id).get("status", "")
                if status == SENT_DONE or status == SENT_PARTIAL or status in SENT_FAILED:
                    counts[_settle(d, status)] += 1
        except SensError as exc:
            counts["check failed"] += 1
            if exc.kind == "global":
                break
        d.checked_at = now
        Dispatch.objects.filter(pk=d.pk).update(checked_at=now)
    return counts


def run(cfg: SensConfig | None = None, client: Client | None = None, now=None) -> Counter:
    """The scan's sending step. Never raises for SENS trouble: it is recorded on the attempts instead."""
    cfg = cfg or config()
    if not cfg.access_key:
        return Counter()
    client = client or Client(cfg)
    try:
        with send_lock():
            register(cfg)
            counts = resolve(cfg, client, now)
            if cfg.on:
                for n in list(eligible(cfg, now)):
                    result = send_one(cfg, client, n, now=now)
                    counts[f"{cfg.mode} {result}"] += 1
                    if result == "global":
                        break  # keys, permission or quota: nothing else will pass either
            return counts
    except Busy:
        return Counter({"busy": 1})


CAN_REQUEST = (S.SEND_FAILED, S.DRAFT_READY, S.COPIED)


def request_send(n: Notification, name: str) -> bool:
    """발송하기 on a 발송 실패 notice, or one waiting that sending will not take by itself (e.g. it came while
    sending was off): back to 발송 대기 for the next scan, which reads the mailboxes first. Rule 6 still applies."""
    if n.status not in CAN_REQUEST or not name or n.dispatches.filter(mode="live", state__in=Dispatch.ACTIVE).exists():
        return False
    with transaction.atomic():
        n.status, n.send_failures, n.send_requested_by = S.DRAFT_READY, 0, name
        n.review_reasons = "\n".join(r for r in n.review_reasons.split("\n") if r and r not in REASONS)
        n.save()
        _event(n, "발송하기 요청", name)
    return True


def last_global_error(within=timedelta(days=1)) -> Dispatch | None:
    return (Dispatch.objects.filter(error_kind="global", created_at__gte=timezone.now() - within)
            .order_by("-pk").first())
