"""Draft one notification per (KT work, account, kind) and link 예시클라우드 copies to them. Local only."""
import re
from collections import Counter, defaultdict
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from customers.models import CustomerAccount, InternalAccount, InternalCompany, PartnerChannel
from mailcollect import headers, ktform
from mailcollect.models import Notice
from mailcollect.notices import work_title

from . import drafts
from .models import Notification, NotificationEvent

Status = Notification.Status
KIND_OF_KT = {headers.KT_NOTICE: "notice", headers.KT_URGENT: "notice", headers.KT_COMPLETE: "complete",
              headers.KT_INCIDENT: "notice", headers.KT_RECOVERY: "complete",  # an outage: 발생 and 복구
              headers.KT_CHECK: "notice",  # a 서비스 점검 요청: one notice, no follow-up
              headers.KT_POSTPONE: "notice"}  # a 작업 연기 공지: a round of its work's 사전 공지 with its own wording
KIND_OF_COPY = {headers.NC24_NOTICE: "notice", headers.NC24_COMPLETE: "complete", headers.NC24_POSTPONE: "notice",
                headers.PARTNER_NOTICE: "notice", headers.PARTNER_COMPLETE: "complete"}
PARTNER_COPIES = (headers.PARTNER_NOTICE, headers.PARTNER_COMPLETE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


ROUND_GAP = timedelta(hours=2)  # KT's duplicate mails of one send arrive together; a later send is a new round
CHANGED_NOTICE = "KT sent a different notice for the same work"
NOT_IN_GROUP = "server not in any group"
REGROUP_COMPLETE = timedelta(days=1)  # a 완료 공지 nobody touched follows group changes this long after KT's mail


def sent_at(notice: Notice):
    """When the mail was sent (Date header), else when it was first seen."""
    try:
        date = notice.seen.header.date
    except ObjectDoesNotExist:
        date = None
    return date or notice.seen.first_seen_at


def rounds_of(notices: list, kind: str) -> list[list]:
    """KT mails for one (work, account, kind), oldest first, cut into sends. KT mails the same notice twice at
    once (two of our addresses), and again days later before the work: each later send is a 사전 공지 round. A
    작업 연기 공지 is a round of its own, however close to another send."""
    notices = sorted(notices, key=lambda no: (sent_at(no), no.pk))
    if kind != Notification.Kind.NOTICE:
        return [notices]
    waves = []
    for no in notices:
        if (waves and sent_at(no) - sent_at(waves[-1][-1]) <= ROUND_GAP
                and postponed(no) == postponed(waves[-1][-1])):
            waves[-1].append(no)
        else:
            waves.append([no])
    return waves


def postponed(notice: Notice) -> bool:
    return notice.category == headers.KT_POSTPONE


def prepare_notifications() -> Counter:
    counts = Counter()
    master = _master()
    internal = InternalAccount.kinds()
    partners = PartnerChannel.by_account()
    kt = (
        Notice.objects.filter(category__in=KIND_OF_KT)
        .select_related("seen__body", "seen__header", "work")
        .prefetch_related("targets")
        .order_by("pk")
    )
    groups, targets_of = defaultdict(list), {}
    for notice in kt:
        targets = [t.account for t in notice.targets.all()]
        targets_of[notice.pk] = len(targets)
        for account in targets:
            key = ("work", notice.work_id) if notice.work_id else ("notice", notice.pk)
            groups[(key, account, KIND_OF_KT[notice.category])].append(notice)

    now = timezone.now()
    for ((field, pk), account, kind), notices in groups.items():
        # Existing rounds keep their order (1차 stays 1차); each sits on its own send, oldest first. Only when an
        # older send is read after a newer one (a mailbox read newest first) does a round move to the older send,
        # and the newer send gets the next round. A round split by group (PartnerGroup) moves as a whole.
        by_round = defaultdict(list)
        for n in Notification.objects.filter(**{field: pk}, account=account, kind=kind).order_by("pk"):
            by_round[n.round].append(n)
        existing = [by_round[number] for number in sorted(by_round)]
        partner = partners.get(account)
        for number, wave in enumerate(rounds_of(notices, kind), start=1):
            first = wave[0]
            ns = existing[number - 1] if number <= len(existing) else []
            if not ns:
                split, unknown = _split(partner, wave)
                ns = [_create(first, account, kind, targets_of[first.pk], master, internal, partners, number, g,
                              unknown) for g in split]
                counts["created" if number == 1 else "next round"] += len(ns)
            else:
                for n in ns:
                    if n.notice_id not in {no.pk for no in wave} or n.round != number:
                        _anchor(n, first, number)
                        counts["moved to an earlier send"] += 1
                if partner is not None and _untouched(ns, now):
                    ns = _regroup(ns, wave, targets_of[first.pk], master, internal, partners, counts)
            changed = len({_text(no) for no in wave}) > 1  # KT often sends the identical notice twice
            for n in ns:
                if changed and n.add_reason(CHANGED_NOTICE):
                    _save(n, Status.NEEDS_REVIEW if n.status == Status.DRAFT_READY else n.status,
                          "KT가 같은 작업에 내용이 다른 공지를 보냄")
                    counts["changed notice"] += 1

    for cancel in Notice.objects.filter(category=headers.KT_CANCEL, work__isnull=False).prefetch_related("targets"):
        accounts = list(cancel.targets.values_list("account", flat=True))
        for n in Notification.objects.filter(work=cancel.work, account__in=accounts):
            if n.add_reason("KT change/cancel notice received"):
                _save(n, Status.NEEDS_REVIEW if n.status in (Status.DRAFT_READY, Status.COPIED) else n.status,
                      "KT 작업 취소/변경 공지 수신")
                counts["cancel flagged"] += 1
    counts.update(apply_internal_accounts())
    counts.update(refresh_from_master())
    return counts


def _split(partner: PartnerChannel | None, wave: list) -> tuple[list, list[str]]:
    """Who a send goes to: one notification per group its servers belong to, or [None] for the whole 받는 사람
    list (no groups, no server named, or a server outside the groups: those come back to be flagged)."""
    if partner is None:
        return [None], []
    groups, unknown = partner.split([s for no in wave for s in no.servers + no.lbs])
    return ([None], unknown) if unknown or not groups else (groups, [])


def _untouched(ns: list, now) -> bool:
    """Nobody has worked on this round and its time has not passed: nothing copied, matched or sent, and the
    work still to come (a 완료 공지: KT's mail under a day old)."""
    if any(n.status not in (Status.NEEDS_REVIEW, Status.DRAFT_READY) or _copied(n) or n.copies.exists()
           or n.dispatches.exists() for n in ns):
        return False
    notice = ns[0].notice
    if ns[0].kind == Notification.Kind.NOTICE:
        return notice.starts_at is not None and notice.starts_at > now
    return sent_at(notice) > now - REGROUP_COMPLETE


def _regroup(ns: list, wave: list, target_count: int, master, internal, partners, counts: Counter) -> list:
    """Bring an untouched round in line with the current groups: once a person puts a flagged server in a group,
    the whole-list notification becomes one per group (and back when a group changes). Notifications of groups
    still wanted stay as they are; the others go, nobody having used them."""
    split, unknown = _split(partners.get(ns[0].account), wave)
    want = {g.pk if g else None: g for g in split}
    keep = [n for n in ns if n.group_id in want]
    for n in ns:
        if n.group_id not in want:
            n.delete()
            counts["regrouped away"] += 1
    have = {n.group_id for n in keep}
    first = ns[0].notice
    for pk, g in want.items():
        if pk not in have:
            keep.append(_create(first, ns[0].account, ns[0].kind, target_count, master, internal, partners,
                                ns[0].round, g, unknown))
            counts["regrouped"] += 1
    for n in keep:  # the whole-list one: flagged while a server is outside the groups
        if n.group_id is None and n.status in (Status.NEEDS_REVIEW, Status.DRAFT_READY):
            reasons = [r for r in n.review_reasons.split("\n") if r and r != NOT_IN_GROUP]
            reasons += [NOT_IN_GROUP] if unknown else []
            if "\n".join(reasons) != n.review_reasons:
                n.review_reasons = "\n".join(reasons)
                _save(n, Status.NEEDS_REVIEW if reasons else Status.DRAFT_READY,
                      "그룹에 없는 서버" if unknown else "그룹에 없는 서버 해소")
    return sorted(keep, key=lambda n: n.pk)


MASTER_REASONS = ("account not in customer master", "no email address in customer master")
NO_MASTER_EMAIL_IN_COPY = "copy To/Cc has none of the master emails"


def refresh_from_master() -> Counter:
    """Re-read the customer master for notifications not yet settled, after a CSV upload or a customer edit.

    Unsent ones get the current name and recipients, and the master-related reasons are re-judged: a customer
    added to the master moves 확인 필요 to 발송 대기 when nothing else is open. A 발송 추정 becomes 고객 발송 확인
    once a sent mail's To/Cc holds one of the master emails. Other reasons and settled notifications stay."""
    counts = Counter()
    master = _master()
    partners = PartnerChannel.by_account()
    open_ = Notification.objects.filter(status__in=[*OPEN, Status.SENT_COPY_CANDIDATE]).prefetch_related("copies")
    for n in open_:
        rows = master.get(n.account, [])
        emails = list(dict.fromkeys(e for row in rows for e in row.emails))
        partner = partners.get(n.account)
        name = next((r.display_name for r in rows if r.display_name), partner.label if partner else "")
        reasons = [r for r in n.review_reasons.split("\n") if r and r not in MASTER_REASONS]
        status = n.status
        if n.status == Status.SENT_COPY_CANDIDATE:
            other_doubt = {"several notifications match one copy", "number matches but account differs"} & set(reasons)
            if (NO_MASTER_EMAIL_IN_COPY in reasons and not other_doubt
                    and any(set(c.recipients) & set(emails) for c in n.copies.all())):
                reasons.remove(NO_MASTER_EMAIL_IN_COPY)
                status = Status.SENT_COPY_CONFIRMED
        else:
            if partner:
                pass  # the partner sends it: no master row or email is needed
            elif not rows:
                reasons.append("account not in customer master")
            elif not emails:
                reasons.append("no email address in customer master")
            if any(r in MASTER_REASONS for r in reasons):
                status = Status.NEEDS_REVIEW
            elif n.status == Status.NEEDS_REVIEW and not reasons:
                status = Status.DRAFT_READY
        text = "\n".join(reasons)
        if (name, emails, text, status) == (n.display_name, n.recipients, n.review_reasons, n.status):
            continue
        n.display_name, n.recipients, n.review_reasons = name, emails, text
        if status != n.status:
            counts[f"{n.status}->{status}"] += 1
            _save(n, status, "고객 정보 반영")
        else:
            n.save()
            counts["recipients updated"] += 1
    return counts


# Not yet sent: an account added to the 사내/안 쓰는 계정 list takes these out of the sending work.
OPEN = (Status.NEEDS_REVIEW, Status.DRAFT_READY, Status.COPIED)
NO_SEND = (Status.INTERNAL, Status.UNUSED)  # no customer to tell: nothing sent or checked


def apply_internal_accounts() -> Counter:
    """Bring open notifications in line with the 사내/안 쓰는 계정 list, both ways (counts by new status:
    "internal", "unused", "internal removed"). Notifications with sent-mail evidence or a person's check keep
    their status."""
    counts = Counter()
    kinds = InternalAccount.kinds()
    for n in Notification.objects.filter(status__in=[*OPEN, *NO_SEND], account__in=kinds):
        status = kinds[n.account]
        if n.status == status:
            continue
        n.review_reasons = ""  # customer checks do not apply; recomputed if the account leaves the list
        _save(n, status, f"{Status(status).label}으로 지정")
        counts[status] += 1
    leaving = Notification.objects.filter(status__in=NO_SEND).exclude(account__in=kinds).select_related("notice")
    master = _master() if leaving.exists() else {}
    for n in leaving:
        was = n.get_status_display()
        n.display_name, n.recipients, reasons = _initial(
            n.notice, n.account, n.notice.targets.count(), master, PartnerChannel.by_account().get(n.account))
        n.review_reasons = "\n".join(reasons)
        _save(n, Status.NEEDS_REVIEW if reasons else Status.DRAFT_READY, f"{was}에서 해제")
        counts["internal removed"] += 1
    return counts


def apply_internal_lists() -> Counter:
    """After the customer master changed: accounts newly found under a 사내 고객명 become 사내 계정, and open
    notifications follow the lists both ways (an 안 쓰는 계정 the company DB updated is a customer again)."""
    made = InternalCompany.mark_accounts(timezone.now())
    counts = apply_internal_accounts()
    counts["accounts marked"] = made
    return counts


def match_copies() -> Counter:
    """Link copies to notifications. Counts only links made in this call; see unmatched_copies()."""
    counts = settle_rounds()
    channels = {ch.sender.strip().lower(): ch for ch in PartnerChannel.objects.filter(active=True)}
    copies = (
        Notice.objects.filter(category__in=KIND_OF_COPY)
        .select_related("seen__body", "seen__header").prefetch_related("targets")
    )
    for copy in copies:
        kind = KIND_OF_COPY[copy.category]
        accounts = [t.account for t in copy.targets.all()]
        channel = None
        if copy.category in PARTNER_COPIES:
            channel = channels.get(copy.seen.header.from_addr) if hasattr(copy.seen, "header") else None
            if channel is None:
                continue  # its channel was removed: nothing it can prove
            # The partner serves only its channel's accounts; a mail naming none of them proves nothing here.
            accounts = [a for a in accounts if a in channel.account_keys] if accounts else channel.account_keys
        number = drafts.number_in(copy.seen.body.text)
        if number:
            n = Notification.objects.filter(number=number).first()
            if n is not None and not n.copies.filter(pk=copy.pk).exists():
                same = not accounts or n.account in accounts
                _link(n, copy, "number", same, None if same else "number matches but account differs")
                counts["confirmed by number" if same else "candidate"] += 1
            continue
        postpone = copy.category == headers.NC24_POSTPONE
        if not accounts or (copy.starts_at is None and not postpone):
            continue
        for account in accounts:
            rows = Notification.objects.filter(account=account, kind=kind)
            # A 작업 연기 공지 often names no time: its copy answers the postponement of the work of its title.
            rows = (rows.filter(notice__category=headers.KT_POSTPONE, work__title=work_title(copy.title)) if postpone
                    else rows.filter(notice__starts_at=copy.starts_at).exclude(notice__category=headers.KT_POSTPONE))
            found = list(rows.select_related("work", "group", "notice__seen__header"))
            if not found or any(n.copies.filter(pk=copy.pk).exists() for n in found):
                continue
            if len(found) > 1:  # several works start together for this account: the work title decides
                title = work_title(copy.title)
                found = [n for n in found if n.work and n.work.title == title] or found
            found = _round_for(found, sent_at(copy))
            by_group = False
            if channel is not None and len(found) > 1 and len({_slot(n) for n in found}) == 1:
                # One round split by group: the copy answers the groups whose people it was sent to.
                hit = [n for n in found if set(copy.recipients) & set(partner_recipients(n, channel))]
                found, by_group = (hit, True) if hit else (found, False)
            if len(found) > 1 and not by_group:
                for n in found:
                    _link(n, copy, "account_kind_window", False, "several notifications match one copy")
                counts["ambiguous"] += 1
            else:
                for n in found:
                    # A partner's mail is evidence by its sender; ours by a master email among its recipients.
                    overlap = channel is not None or bool(set(copy.recipients) & set(n.recipients))
                    _link(n, copy, "account_kind_window", overlap,
                          None if overlap else "copy To/Cc has none of the master emails")
                    counts["confirmed by account, kind and window" if overlap else "candidate"] += 1
    return counts


def _copied(n: Notification) -> bool:
    """Staff copied part of the draft from this site (record_copied)."""
    return n.events.filter(note__endswith=" 복사").exists()


def _anchor(n: Notification, notice: Notice, number: int):
    """Point a notification at another KT mail of its work (an earlier send read late). Its draft follows
    unless staff already copied it."""
    n.notice, n.round = notice, number
    if not _copied(n):
        n.subject, n.body = _draft(n.kind, notice, number, PartnerChannel.by_account().get(n.account))
    n.save()
    NotificationEvent.objects.create(notification=n, status=n.status,
                                     note=f"더 먼저 온 KT 공지에 연결 ({number}차)")


def _round_for(found: list, when) -> list:
    """Of the rounds of one work, keep the one a copy answers: the last KT send before the copy went out (all
    its notifications when the round is split by group)."""
    by_work = defaultdict(list)
    for n in found:
        by_work[n.work_id or -n.notice_id].append(n)
    picked = []
    for rows in by_work.values():
        numbers = sorted({n.round for n in rows})
        before = [r for r in numbers if any(sent_at(n.notice) <= when for n in rows if n.round == r)]
        number = before[-1] if before else numbers[0]
        picked += sorted((n for n in rows if n.round == number), key=lambda n: n.pk)
    return picked


def _slot(n: Notification) -> tuple:
    return n.work_id or -n.notice_id, n.round


def partner_recipients(n: Notification, channel: PartnerChannel) -> list[str]:
    """The addresses a 별도 발송 notification goes to: its group's 받는 사람 and 참조, or the channel's whole list."""
    listed = n.group.to_list + n.group.cc_list if n.group_id else channel.to_list
    return [a.lower() for item in listed for a in EMAIL.findall(item)]


COPY_REASONS = (NO_MASTER_EMAIL_IN_COPY, "several notifications match one copy", "number matches but account differs")


def settle_rounds() -> Counter:
    """A copy matched before KT's next send existed sits on the earlier round: move it to the round it answers.
    Copies carrying one of the 안내번호 stay where the number put them."""
    counts = Counter()
    groups = (Notification.objects.filter(round__gt=1, work__isnull=False)
              .values_list("work_id", "account", "kind").distinct())
    for work_id, account, kind in groups:
        rounds = list(
            Notification.objects.filter(work_id=work_id, account=account, kind=kind)
            .select_related("notice__seen__header").prefetch_related("copies__seen__header", "copies__seen__body")
        )
        numbers = {n.number for n in rounds}
        for n in rounds:
            for copy in list(n.copies.all()):
                same = [m for m in rounds if postponed(m.notice) == (copy.category == headers.NC24_POSTPONE)]
                picked = _round_for(same or rounds, sent_at(copy))
                right = next((m for m in picked if m.group_id == n.group_id), picked[0])
                if right.pk == n.pk or drafts.number_in(copy.seen.body.text) in numbers:
                    continue
                n.copies.remove(copy)
                if not right.copies.filter(pk=copy.pk).exists():
                    overlap = copy.category in PARTNER_COPIES or bool(set(copy.recipients) & set(right.recipients))
                    _link(right, copy, "account_kind_window", overlap, None if overlap else NO_MASTER_EMAIL_IN_COPY)
                note = f"보낸 메일을 {right.round}차 안내로 옮김"
                if not n.copies.exists() and n.status in (Status.SENT_COPY_CONFIRMED, Status.SENT_COPY_CANDIDATE):
                    n.match_basis = ""
                    n.review_reasons = "\n".join(r for r in n.review_reasons.split("\n") if r and r not in COPY_REASONS)
                    _save(n, Status.NEEDS_REVIEW if n.review_reasons else Status.DRAFT_READY, note)
                else:
                    NotificationEvent.objects.create(notification=n, status=n.status, note=note)
                counts["moved to its round"] += 1
    return counts


def unmatched_copies() -> int:
    """예시클라우드 copies that no notification points to (e.g. notices that are not about a KT work)."""
    return Notice.objects.filter(category__in=KIND_OF_COPY, confirms__isnull=True).count()


def _master() -> dict[str, list[CustomerAccount]]:
    rows = {}
    for row in CustomerAccount.objects.filter(active=True).order_by("pk"):
        rows.setdefault(row.account_key, []).append(row)
    return rows


def _initial(notice: Notice, account: str, target_count: int, master, partner=None) -> tuple[str, list[str], list[str]]:
    """Display name, recipients and review reasons of a customer notification, from the master.

    A partner channel's customer needs no master row: the partner has the addresses and sends it."""
    rows = master.get(account, [])
    emails = list(dict.fromkeys(e for row in rows for e in row.emails))
    reasons = []
    if notice.status != Notice.Status.OK:
        reasons.append("notice needs review")
    if partner is None and not rows:
        reasons.append("account not in customer master")
    elif partner is None and not emails:
        reasons.append("no email address in customer master")
    if target_count > 1 and partner is None:
        reasons.append("notice lists several accounts; check the quote")
    name = next((r.display_name for r in rows if r.display_name), partner.label if partner else "")
    return name, emails, reasons


def _create(notice: Notice, account: str, kind: str, target_count: int, master, internal=None, partners=None,
            round_: int = 1, group=None, unknown=()) -> Notification:
    display_name, emails, reasons = _initial(notice, account, target_count, master, (partners or {}).get(account))
    if unknown:
        reasons.append(NOT_IN_GROUP)
    internal = internal or {}
    if account in internal:  # {account: "internal" | "unused"}
        status, reasons = internal[account], []
    else:
        status = Status.NEEDS_REVIEW if reasons else Status.DRAFT_READY
    with transaction.atomic():
        n = Notification.objects.create(
            notice=notice, work=notice.work, account=account, group=group, kind=kind, round=round_,
            display_name=display_name,
            recipients=emails, review_reasons="\n".join(reasons), status=status,
        )
        n.number = f"CSP-{timezone.localtime(n.created_at):%y%m%d}-{n.pk:04d}"
        n.subject, n.body = _draft(kind, notice, round_, (partners or {}).get(account))
        n.save(update_fields=["number", "subject", "body"])
        note = ("작업 연기 안내문 생성" if postponed(notice) else "안내문 생성" if round_ == 1
                else f"{round_}차 안내문 생성 (KT가 같은 공지를 다시 보냄)")
        NotificationEvent.objects.create(notification=n, status=n.status, note=note)
    return n


BASIS_LABEL = {"number": "안내번호", "account_kind_window": "계정, 공지 종류, 작업 일시"}
SENT = (Status.SENT_COPY_CONFIRMED, Status.STAFF_CONFIRMED)
STAFF_CONFIRMABLE = (Status.SENT_COPY_CONFIRMED, Status.SENT_COPY_CANDIDATE, Status.MANUAL_SENT_RECORDED)


def _link(n: Notification, copy: Notice, basis: str, confirmed: bool, reason: str | None):
    n.copies.add(copy)
    if n.status in SENT or n.status in NO_SEND:  # proven, checked, or nothing to send: log the mail only
        NotificationEvent.objects.create(
            notification=n, status=n.status, note=f"보낸 메일 추가 확인 ({BASIS_LABEL[basis]} 일치)")
        return
    n.match_basis = n.match_basis or basis
    if reason:
        n.add_reason(reason)
    _save(n, Status.SENT_COPY_CONFIRMED if confirmed else Status.SENT_COPY_CANDIDATE,
          f"보낸 메일 확인 ({BASIS_LABEL[basis]} 일치)")


def record_copied(n: Notification, user, what: str, actor: str = "") -> None:
    """Staff copied part of the draft. Copying is not sending: only 발송 대기 moves to 작성 중."""
    status = Status.COPIED if n.status == Status.DRAFT_READY else n.status
    with transaction.atomic():
        if status != n.status:
            n.status = status
            n.save(update_fields=["status", "updated_at"])
        NotificationEvent.objects.create(notification=n, status=status, note=f"{what} 복사", by=user, actor=actor)


def record_manual_sent(n: Notification, user, note: str, actor: str = "") -> None:
    """Staff state they sent it from Outlook. Mail evidence and a person's check stay stronger."""
    status = n.status if n.status in SENT else Status.MANUAL_SENT_RECORDED
    with transaction.atomic():
        n.status = status
        n.save(update_fields=["status", "updated_at"])
        NotificationEvent.objects.create(notification=n, status=status, note=note or "발송했다고 기록",
                                         by=user, actor=actor)


def confirm_by_staff(n: Notification, user, name: str) -> bool:
    """Final check by a named person, after the customer mail was sent. False if it cannot be checked yet."""
    if n.status not in STAFF_CONFIRMABLE or not name:
        return False
    with transaction.atomic():
        n.status_before_staff, n.status = n.status, Status.STAFF_CONFIRMED
        n.staff_name, n.staff_confirmed_at = name, timezone.now()
        n.save()
        NotificationEvent.objects.create(notification=n, status=n.status, note="담당자 확인", by=user, actor=name)
    return True


def undo_staff_confirm(n: Notification, user, name: str) -> bool:
    if n.status != Status.STAFF_CONFIRMED:
        return False
    with transaction.atomic():
        n.status = n.status_before_staff or Status.SENT_COPY_CONFIRMED
        n.status_before_staff, n.staff_name, n.staff_confirmed_at = "", "", None
        n.save()
        NotificationEvent.objects.create(notification=n, status=n.status, note="담당자 확인 취소", by=user, actor=name)
    return True


def _title(notice: Notice) -> str:
    return notice.work.title if notice.work_id else notice.title


def _draft(kind: str, notice: Notice, round_: int, partner: PartnerChannel | None = None) -> tuple[str, str]:
    """Subject and body: 예시클라우드's wording, or a 별도 발송 partner's once its template is filled in. A KT
    outage notice has its own wording, whoever sends it."""
    if notice.category in headers.INCIDENTS:
        return drafts.build_incident(notice.seen.body.text, notice.title, ktform.current().footer)
    wording = drafts.POSTPONE if postponed(notice) else kind
    if partner is not None and partner.has_template:
        start, end = notice.starts_at, notice.ends_at
        when = (drafts.window(timezone.localtime(start), timezone.localtime(end) if end else None)
                if start else notice.schedule)
        return drafts.build_partner(wording, notice.seen.body.text, _title(notice), round_, when,
                                    partner.greeting, partner.subject_prefix, ktform.current().footer)
    return drafts.build(wording, notice.seen.body.text, _title(notice), ktform.current().footer)


def redraft_unsent() -> int:
    """Rewrite subject and body after the template changed (the 안내번호 itself stays on the notification).

    Only text nobody copied from this site: copies matched to a notification were written in Outlook by staff
    (they are kept apart as 보낸 메일), so its draft can follow the template even after it was sent."""
    done = 0
    copied = NotificationEvent.objects.filter(note__endswith=" 복사").values("notification_id")  # see _copied
    rows = Notification.objects.exclude(pk__in=copied).select_related("notice__seen__body", "notice__work")
    partners = PartnerChannel.by_account()
    for n in rows:
        subject, body = _draft(n.kind, n.notice, n.round, partners.get(n.account))
        if (subject, body) != (n.subject, n.body):
            n.subject, n.body = subject, body
            n.save(update_fields=["subject", "body", "updated_at"])
            done += 1
    return done


def _text(notice: Notice) -> str:
    return " ".join(notice.seen.body.text.split())


def rebuild() -> int:
    """Drop all notifications so they are drafted again with the current rules. Refused once staff acted."""
    touched = Notification.objects.filter(
        status__in=[Status.COPIED, Status.MANUAL_SENT_RECORDED, Status.STAFF_CONFIRMED]).count()
    if touched:
        raise RuntimeError(f"{touched} notification(s) were copied or recorded by staff; not rebuilding")
    deleted, _ = Notification.objects.all().delete()
    return deleted


def _save(n: Notification, status: str, note: str):
    with transaction.atomic():
        n.status = status
        n.save()
        NotificationEvent.objects.create(notification=n, status=status, note=note)
