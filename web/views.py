import csv
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from customers import opsdb
from customers.models import (
    CustomerAccount, InternalAccount, InternalCompany, OpsDbSync, PartnerChannel, PartnerGroup,
)
from mailcollect import headers, ktform
from mailcollect.models import KtFormSetting, Mailbox, Notice, NoticeTarget, ScanRun, Work
from mailcollect.scanner import AlreadyRunning, request_scan, scan_lock
from mailcollect.config import ConfigError
from notifications import dispatch, drafts, sens
from notifications.models import Dispatch, Notification, Record, SendSetting, WorkClosure
from notifications.records import MAIN_TAB, sync as sync_records
from notifications.service import (
    KIND_OF_KT, NO_SEND, ROUND_GAP, STAFF_CONFIRMABLE, apply_internal_accounts, apply_internal_lists, confirm_by_staff,
    match_copies, prepare_notifications, record_copied, record_manual_sent,
    redraft_unsent, refresh_from_master, sent_at, undo_staff_confirm, unmatched_copies,
)
from suggestions.models import Suggestion

from . import config as config_tools
from . import groups as group_tools
from . import labels
from .models import PasswordPolicy
from .scope import accounts_q, late_q, needs_attention, needs_attention_all

S = Notification.Status
OK_SCANS = [ScanRun.Status.OK, ScanRun.Status.UNCHANGED, ScanRun.Status.PARTIAL]
STALE_AFTER = timedelta(minutes=15)  # three missed 5-minute runs
OUR_MAIL_CATEGORIES = ["nc24_notice", "nc24_complete", "nc24_postpone"]  # 보낸 안내 메일 tab
PARTNER_MAIL_CATEGORIES = ["partner_notice", "partner_complete"]  # each 별도 발송 tab
SENT_MAIL_CATEGORIES = OUR_MAIL_CATEGORIES + PARTNER_MAIL_CATEGORIES
KINDS = (Notification.Kind.NOTICE, Notification.Kind.COMPLETE)
PAST_DAYS = 30  # 지난 작업 lists works that started within this many days; unfinished ones stay listed


def _back(request, default="/"):
    """Return path from a form, limited to this site."""
    back = request.POST.get("next") or default
    return back if back.startswith("/") and not back.startswith("//") else default


def _actor(request) -> str:
    return request.POST.get("actor", "").strip()[:50]


def _work_rows(works, channel=None):
    """Works with per-kind counts of the customer notices of one tab (see web/scope.py)."""
    now = timezone.now()
    scope = accounts_q(channel, prefix="notifications__")

    def count(kind, extra=None):
        q = scope & Q(notifications__kind=kind)
        return Count("notifications", filter=q & extra if extra is not None else q)

    def status(*statuses):
        return Q(notifications__status__in=statuses)

    counts = {"accounts": Count("notifications__account", filter=scope, distinct=True)}
    for kind in KINDS:
        counts.update({
            f"{kind}_total": count(kind),
            f"{kind}_staff": count(kind, status(S.STAFF_CONFIRMED)),
            f"{kind}_sent": count(kind, status(S.SENT_COPY_CONFIRMED, S.MANUAL_SENT_RECORDED)),
            f"{kind}_wait": count(kind, status(S.DRAFT_READY, S.COPIED, S.SEND_REQUESTED)),
            f"{kind}_review": count(kind, status(S.NEEDS_REVIEW, S.SENT_COPY_CANDIDATE, S.SEND_FAILED)),
            f"{kind}_late": count(kind, late_q(now, prefix="notifications__")),
            f"{kind}_internal": count(kind, status(S.INTERNAL, S.UNUSED)),  # no customer to tell
        })
    counts["postponed"] = Exists(Notice.objects.filter(work=OuterRef("pk"), category=headers.KT_POSTPONE))
    # A 서비스 점검 요청 work has one stage: KT sends nothing after it.
    counts["single"] = Exists(Notice.objects.filter(work=OuterRef("pk"), category=headers.KT_CHECK))
    return works.annotate(**counts).filter(accounts__gt=0).select_related("rework_of")


def _stage(w, kind) -> dict:
    """One kind of notice (사전 or 완료) for one work: done once every customer's is checked by a person."""
    get = lambda name: getattr(w, f"{kind}_{name}")  # noqa: E731
    stage = {name: get(name) for name in ("total", "staff", "sent", "wait", "review", "late", "internal")}
    stage["need"] = stage["total"] - stage["internal"]
    stage["done"] = stage["total"] > 0 and stage["staff"] == stage["need"]
    stage["left"] = stage["need"] - stage["staff"]  # still to be checked by a person (sort key)
    return stage


def postponed(w) -> dict | None:
    """A work KT put off (작업 연기 공지): when it comes again, as KT wrote it ('추후에 안내 예정'), and the
    work that does it again once KT sent one (Work.rework_of)."""
    p = w.postponement()
    if p is None:
        return None
    start, end = p.starts_at, p.ends_at
    nxt = (drafts.window(timezone.localtime(start), timezone.localtime(end) if end else None) if start
           else " ".join(p.schedule.split()) or "추후 안내")
    return {"next": nxt, "rework": w.reworks.order_by("starts_at").first()}


def _row(w, now, closure=None) -> dict:
    notice, complete = _stage(w, "notice"), _stage(w, "complete")
    internal_only = notice["need"] + complete["need"] == 0
    later = postponed(w) if getattr(w, "postponed", False) else None
    if closure is not None:
        done = True
    elif later and later["rework"]:  # KT sent the new date: that work carries on
        done = True
    elif getattr(w, "single", False):
        done = notice["done"] or (notice["need"] == 0 and w.starts_at < now)
    elif internal_only:  # nothing to send or check
        done = (w.ends_at or w.starts_at) < now
    else:
        done = notice["done"] and complete["done"]
    attention = notice["review"] + complete["review"] + notice["late"] + complete["late"]  # problems
    unchecked = notice["sent"] + complete["sent"]  # sent, 담당자 확인 still owed
    if attention and not done:
        state = "warn"
    elif internal_only:
        state = "internal"
    elif done:
        state = "done"
    elif notice["wait"] or complete["wait"]:
        state = "pending"
    elif notice["done"] and not complete["total"]:
        state = "waiting"  # 사전 공지 all checked; KT's completion notice has not come yet
    else:
        state = "sent"
    return {"w": w, "notice": notice, "complete": complete, "state": state, "done": done, "closure": closure,
            "postponed": later, "single": getattr(w, "single", False),
            "review": 0 if closure else attention + unchecked, "problems": 0 if closure else attention,
            "unchecked": 0 if closure else unchecked}


def _work_lists(channel=None):
    """(upcoming, past) of a tab: a work stays upcoming until both notices are checked, or it is closed by hand,
    however old it is. 지난 작업 shows the last month; older ones are on the calendar until purge_old."""
    now = timezone.now()
    closures = {c.work_id: c for c in WorkClosure.objects.filter(channel=channel)}
    rows = [_row(w, now, closures.get(w.pk)) for w in _work_rows(Work.objects.all(), channel)]
    upcoming = sorted((r for r in rows if not r["done"]), key=lambda r: r["w"].starts_at)
    since = now - timedelta(days=PAST_DAYS)
    past = sorted((r for r in rows if r["done"] and r["w"].starts_at >= since),
                  key=lambda r: r["w"].starts_at, reverse=True)
    return upcoming, past


def _collection_status():
    now = timezone.now()
    boxes = []
    for mailbox in Mailbox.objects.filter(enabled=True).order_by("pk"):
        ok = mailbox.runs.filter(status__in=OK_SCANS).order_by("-pk").first()
        boxes.append({"title": mailbox.title, "last_ok": ok, "stale": ok is None or now - ok.finished_at > STALE_AFTER})
    day = ScanRun.objects.filter(started_at__gte=now - timedelta(days=1))
    return {
        "boxes": boxes,
        "last_ok": boxes[0]["last_ok"] if boxes else None,  # the main mailbox, registered first
        "stale": not boxes or boxes[0]["stale"],
        "stale_extra": [b["title"] for b in boxes[1:] if b["stale"]],
        "login_failed_24h": day.filter(status=ScanRun.Status.LOGIN_FAILED).count(),
        "failed_24h": day.filter(status=ScanRun.Status.FAILED).count(),
        "lost_24h": sum(r.gone_unscanned + r.body_lost for r in day.only("gone_unscanned", "body_lost")),
    }


def _sending_status() -> dict:
    """The dashboard's line on automatic sending: its mode, failures, and a settings or key problem."""
    try:
        cfg = dispatch.config()
        problem = ""
    except ConfigError as exc:
        cfg, problem = sens.OFF, str(exc)
    last = dispatch.last_global_error()
    if last and not problem:
        problem = last.error
    return {"label": SEND_MODE_LABEL[cfg.mode], "problem": problem,
            "failed": Notification.objects.filter(status=S.SEND_FAILED).count()}


def _counts(upcoming, channel=None):
    rows = Notification.objects.filter(accounts_q(channel), work__in=[r["w"].pk for r in upcoming])
    review, late, unchecked = needs_attention(channel)
    problems = review.count() + late.count()
    return {
        "review": problems + unchecked.count(),
        "problems": problems,
        "unchecked": unchecked.count(),
        "waiting": rows.filter(status__in=[S.DRAFT_READY, S.COPIED, S.SEND_REQUESTED]).count(),
        "to_check": rows.filter(status__in=STAFF_CONFIRMABLE).count(),
        "checked": rows.filter(status=S.STAFF_CONFIRMED).count(),
    }


@login_required
def dashboard(request):
    upcoming, past = _work_lists()
    return render(request, "web/dashboard.html", {
        "status": _collection_status(),
        "sending": _sending_status(),
        "upcoming": upcoming,
        "past": past,
        "counts": _counts(upcoming),
        "tab": "main",
        "min_interval": settings.CSP_SCAN["request_min_interval"],
    })


@login_required
def channel(request, pk):
    ch = get_object_or_404(PartnerChannel, pk=pk, active=True)
    upcoming, past = _work_lists(ch)
    mails = (
        Notice.objects.filter(category__in=PARTNER_MAIL_CATEGORIES, seen__header__from_addr=ch.sender)
        .select_related("seen__header").prefetch_related("confirms").order_by("-seen__header__date")[:100]
    )
    review, late, unchecked = needs_attention(ch)
    return render(request, "web/channel.html", {
        "ch": ch, "channel": ch, "upcoming": upcoming, "past": past, "mails": mails, "tab": ch.pk,
        "group_count": ch.groups.filter(active=True).count(),
        "confirmable": set(STAFF_CONFIRMABLE),
        "counts": _counts(upcoming, ch),
        "attention": [{"n": n, "why": why} for qs, why in ((review, ""), (late, "late"), (unchecked, "unchecked"))
                      for n in qs.select_related("work", "notice", "group")
                      .order_by("work__starts_at", "kind", "round", "group__name")],
    })


@login_required
def channel_groups(request, pk):
    """받는 사람 그룹 of a 별도 발송 tab: one notice per group whose servers KT names (user, 2026-10-21)."""
    ch = get_object_or_404(PartnerChannel, pk=pk, active=True)
    editing = None
    if request.GET.get("edit"):
        editing = get_object_or_404(PartnerGroup, pk=request.GET["edit"], channel=ch, active=True)
    form, problems, aliases, alias_problems = None, [], ch.server_aliases, []
    if request.method == "POST":
        action, name = request.POST.get("action"), _actor(request)
        group = get_object_or_404(PartnerGroup, pk=request.POST["group"], channel=ch,
                                  active=action != "restore") if request.POST.get("group") else None
        if not name:
            messages.error(request, "변경하는 사람 이름을 입력하세요.")
            if action == "save":
                form, editing = request.POST, group
            elif action == "aliases":
                aliases = request.POST.get("aliases", "")
        elif action == "save":
            data, problems = group_tools.from_post(request.POST, ch, group)
            if problems:
                form, editing = request.POST, group
            else:
                group = group or PartnerGroup(channel=ch)
                for field, value in data.items():
                    setattr(group, field, value)
                group.updated_by = name
                group.save()
                _regroup_now(request, f"그룹 '{group.name}'을 저장했습니다.")
                return redirect("channel_groups", pk=ch.pk)
        elif action == "retire" and group is not None:
            group.active, group.updated_by = False, name
            group.save()
            _regroup_now(request, f"그룹 '{group.name}'을 뺐습니다.")
            return redirect("channel_groups", pk=ch.pk)
        elif action == "restore" and group is not None:
            if ch.groups.filter(active=True, name=group.name).exists():
                messages.error(request, f"같은 이름의 그룹 '{group.name}'이 있어 되살리지 못했습니다.")
                return redirect("channel_groups", pk=ch.pk)
            group.active, group.updated_by = True, name
            group.save()
            _regroup_now(request, f"그룹 '{group.name}'을 되살렸습니다.")
            return redirect("channel_groups", pk=ch.pk)
        elif action == "aliases":
            aliases, alias_problems = group_tools.aliases_from_post(request.POST.get("aliases", ""))
            if alias_problems:
                aliases = request.POST.get("aliases", "")
            else:
                ch.server_aliases = aliases
                ch.save(update_fields=["server_aliases"])
                _regroup_now(request, "KT 서버 이름을 저장했습니다.")
                return redirect("channel_groups", pk=ch.pk)
    groups = list(ch.groups.filter(active=True).order_by("name", "pk"))
    shown = form or ({"name": editing.name, "mail_to": editing.mail_to, "mail_cc": editing.mail_cc,
                      "servers": editing.servers} if editing else {})
    return render(request, "web/channel_groups.html", {
        "ch": ch, "tab": ch.pk, "groups": groups, "editing": editing, "form": shown, "problems": problems,
        "aliases": aliases, "alias_problems": alias_problems, "alias_count": len(ch.aliases()),
        "unmapped": group_tools.unmapped(ch) if groups else [], "days": group_tools.DAYS,
        "retired": ch.groups.filter(active=False).order_by("-updated_at")[:20],
    })


def _regroup_now(request, done: str):
    """Saved: notices nobody has worked on follow the new groups now, or at the next mail check if one runs."""
    try:
        with scan_lock(settings.CSP_SCAN["lock_path"]):
            counts = prepare_notifications()
            match_copies()
    except AlreadyRunning:
        messages.success(request, f"{done} 메일 수집이 끝나면 반영됩니다.")
        return
    changed = counts["regrouped"] + counts["regrouped away"]
    messages.success(request, f"{done} 안내 {counts['regrouped']}건을 그룹에 맞춰 새로 만들었습니다." if changed else done)


@login_required
def calendar_view(request):
    today = timezone.localdate()
    try:
        year, month = (int(x) for x in request.GET.get("m", "").split("-"))
        first = date(year, month, 1)
    except ValueError:
        first = today.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    start = first - timedelta(days=first.weekday())  # Monday
    last = next_month - timedelta(days=1)
    end = last + timedelta(days=6 - last.weekday())

    by_day = defaultdict(list)
    now = timezone.now()
    works = _work_rows(Work.objects.filter(starts_at__date__gte=start, starts_at__date__lte=end))
    closures = {c.work_id: c for c in WorkClosure.objects.filter(channel=None)}
    for w in works.order_by("starts_at"):  # the 작업 공지 tab's customers; 별도 발송 tabs list their own works
        by_day[timezone.localtime(w.starts_at).date()].append(_row(w, now, closures.get(w.pk)))
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    weeks = [
        [{"date": d, "in_month": d.month == first.month, "today": d == today, "works": by_day[d]} for d in days[i:i + 7]]
        for i in range(0, len(days), 7)
    ]
    return render(request, "web/calendar.html", {
        "first": first, "weeks": weeks, "weekdays": "월화수목금토일",
        "prev": f"{prev_month:%Y-%m}", "next": f"{next_month:%Y-%m}", "this_month": f"{today:%Y-%m}",
    })


@login_required
@require_POST
def request_scan_view(request):
    back = _back(request)
    if "?" in back:
        back = "/"
    requested, last = request_scan(settings.CSP_SCAN["request_path"], settings.CSP_SCAN["request_min_interval"])
    if requested:
        messages.success(request, "새 메일을 확인하고 있습니다. 잠시 후 화면이 새로 고쳐집니다.")
        return redirect(f"{back}?requested=1")
    seconds = int((timezone.now() - last.finished_at).total_seconds())
    messages.info(request, f"{seconds}초 전에 확인을 마쳤습니다. 지금 보이는 내용이 최신입니다.")
    return redirect(back)


def _group_servers(n, partner) -> dict:
    """A 별도 발송 customer split by group: the notice's servers of this notification's group, and the servers
    no group has (the reason it went to the whole list)."""
    if partner is None:
        return {}
    servers = n.notice.servers + n.notice.lbs
    groups, unknown = partner.split(servers)
    if not groups and not unknown:
        return {}
    mine = [s for s in servers if n.group_id and n.group in partner.split([s])[0]]
    return {"mine": mine, "unknown": unknown}


def _work_url(work_pk, channel=None) -> str:
    return reverse("channel_work", args=[channel.pk, work_pk]) if channel else reverse("work", args=[work_pk])


def _channel_of(accounts) -> PartnerChannel | None:
    """The 별도 발송 tab whose customers these accounts all are, else None (the 작업 공지 tab)."""
    by_account = PartnerChannel.by_account()
    channels = {by_account.get(a) for a in accounts}
    return channels.pop() if len(channels) == 1 and None not in channels else None


KT_MAIL_CATEGORIES = [*KIND_OF_KT, "kt_cancel"]


def _kt_list(mails, notifications) -> list[dict]:
    """Every KT mail of a work for one tab, oldest first. KT sends each one twice at once (two of our
    addresses): an identical mail within ROUND_GAP of the last is counted on that row, not listed again."""
    names = {n.account: n.display_name for n in notifications if n.display_name}
    rows = []
    for mail in sorted(mails, key=sent_at):
        accounts = sorted(t.account for t in mail.targets.all())
        key = (mail.category, tuple(accounts), " ".join(mail.seen.body.text.split()))
        last = rows[-1] if rows else None
        if last and last["key"] == key and sent_at(mail) - last["at"] <= ROUND_GAP:
            last["count"] += 1
            last["pks"].append(mail.pk)
            continue
        rows.append({"key": key, "mail": mail, "at": sent_at(mail), "count": 1, "pks": [mail.pk],
                     "who": ", ".join(names.get(a, a) for a in accounts) or "대상 계정 없음"})
    return rows


@login_required
def work_detail(request, pk, channel_pk=None):
    work = get_object_or_404(Work, pk=pk)
    channel = get_object_or_404(PartnerChannel, pk=channel_pk, active=True) if channel_pk else None
    notifications = list(
        work.notifications.filter(accounts_q(channel)).select_related("notice__seen__header", "group")
        .prefetch_related("copies").order_by("display_name", "account", "group__name")
    )
    # KT's own mails for this tab's customers only: the same work reaches other tabs through other mails.
    kt_mails = list(work.notices.filter(accounts_q(channel, prefix="targets__"), category__in=KT_MAIL_CATEGORIES)
                    .select_related("seen__header", "seen__body").prefetch_related("targets").distinct())
    groups = []
    for kind, label in Notification.Kind.choices:
        rows = [n for n in notifications if n.kind == kind]
        for number in sorted({n.round for n in rows}) or [1]:
            in_round = [n for n in rows if n.round == number]
            groups.append({
                "kind": kind, "rows": in_round,
                "label": (in_round[0].kind_label if in_round
                          else ("장애 발생 공지" if kind == Notification.Kind.NOTICE else "장애 복구 공지") if work.incident
                          else f"{label} {number}차" if kind == Notification.Kind.NOTICE else label),
                "kt": min((n.notice for n in in_round), key=sent_at, default=None),
            })
    closure = WorkClosure.objects.filter(work=work, channel=channel).first()
    annotated = _work_rows(Work.objects.filter(pk=work.pk), channel).first()
    if annotated and annotated.single:  # a 서비스 점검 요청 has no 완료 stage to wait for
        groups = [g for g in groups if g["rows"] or g["kind"] != Notification.Kind.COMPLETE]
    return render(request, "web/work.html", {
        "work": work, "channel": channel, "tab": channel.pk if channel else "main",
        "notice": min((m for m in kt_mails if m.category in KIND_OF_KT), key=sent_at, default=None),
        "kt_list": _kt_list(kt_mails, notifications),
        "row": _row(annotated, timezone.now(), closure) if annotated else None,
        "groups": groups,
        "confirmable": set(STAFF_CONFIRMABLE),
    })


@login_required
@require_POST
def work_close(request, pk):
    """Move a work to 지난 작업 of one tab by hand, e.g. when KT sends no completion notice. Undone with reopen."""
    work = get_object_or_404(Work, pk=pk)
    channel_pk = request.POST.get("channel")
    channel = get_object_or_404(PartnerChannel, pk=channel_pk) if channel_pk else None
    name = _actor(request)
    if request.POST.get("reopen"):
        WorkClosure.objects.filter(work=work, channel=channel).delete()
        messages.info(request, "다시 진행 중인 작업으로 옮겼습니다.")
    elif not name:
        messages.error(request, "넘기는 사람 이름을 입력하세요.")
    else:
        WorkClosure.objects.update_or_create(work=work, channel=channel,
                                             defaults={"closed_at": timezone.now(), "closed_by": name})
        messages.success(request, f"{name} 님 이름으로 지난 작업으로 넘겼습니다.")
    sync_records()
    return redirect(_work_url(work.pk, channel))


@login_required
def notice_detail(request, pk):
    notice = get_object_or_404(Notice.objects.select_related("seen__body", "seen__header", "work"), pk=pk)
    targets = list(notice.targets.all())
    channel = _channel_of([t.account for t in targets])
    return render(request, "web/notice.html", {
        "notice": notice, "targets": targets, "tab": channel.pk if channel else "main",
        "back_url": _work_url(notice.work_id, channel) if notice.work_id else None,
    })


@login_required
def notification_detail(request, pk):
    n = get_object_or_404(Notification.objects.select_related("notice", "work", "group"), pk=pk)
    partner = PartnerChannel.by_account().get(n.account)
    sent = [
        {"mail": copy, "matched": sorted(set(copy.recipients) & set(n.recipients))}
        for copy in n.copies.select_related("seen__header", "seen__body").order_by("seen__header__date")
    ]
    template = partner is not None and partner.has_template
    group = n.group if n.group_id else None
    # KT's mails to this account about this work, in this page (a later 작업 연기 공지 included).
    mails = (Notice.objects.filter(work=n.work_id, targets__account=n.account, category__in=KT_MAIL_CATEGORIES)
             if n.work_id else Notice.objects.filter(pk=n.notice_id))
    kt_mails = _kt_list(list(mails.select_related("seen__header", "seen__body").prefetch_related("targets")
                             .distinct()), [n])
    to = (group.to_list if group else partner.to_list) if template else n.recipients
    cc = list(dict.fromkeys((group.cc_list if group else []) + partner.cc_list)) if template else []
    return render(request, "web/notification.html", {
        "n": n,
        "to": "; ".join(to),
        "to_label": f"받는 사람 ({len(to)}명)",
        "body_html": mark_safe(drafts.partner_html(n.body) or "") if template else "",  # escaped line by line
        "cc": "; ".join(cc),
        "bcc": "; ".join(partner.bcc_list) if template else "",
        "sent": sent,
        "events": [(e, S(e.status).label if e.status in S.values else e.status)
                   for e in n.events.select_related("by").order_by("at")],
        "servers": ", ".join(n.notice.servers + n.notice.lbs),
        "group_servers": _group_servers(n, partner),
        "confirmable": n.status in STAFF_CONFIRMABLE,
        "partner": partner, "tab": partner.pk if partner else "main",
        "back_url": _work_url(n.work_id, partner) if n.work_id else None,
        "sending": _sending(n), "dispatches": n.dispatches.order_by("-pk")[:20],
        "kt_mails": kt_mails, "postponed": postponed(n.work) if n.work_id else None,
    })


SEND_MODE_LABEL = {"off": "OFF", "test": "테스트", "live": "ON"}


def _sens():
    """The sending settings, or None when the file is missing or broken (the dashboard says which)."""
    try:
        return dispatch.config()
    except ConfigError:
        return None


def _sending(n) -> dict | None:
    cfg = _sens()
    if cfg is None or not cfg.access_key:
        return None if not n.dispatches.exists() else {"mode_label": "설정 없음"}
    live = cfg.mode == "live"
    active = n.dispatches.filter(mode="live", state__in=Dispatch.ACTIVE).exists()
    automatic = live and not active and dispatch.automatic(n, cfg)
    return {
        "mode_label": SEND_MODE_LABEL[cfg.mode],
        "automatic": automatic,  # the next scan sends it: 발송하기 only where automatic sending will not
        "can_resend": live and not active and not automatic and n.status in dispatch.CAN_REQUEST and bool(n.recipients),
    }


@login_required
@require_POST
def notification_send(request, pk):
    """발송하기 after automatic sending gave up: the next scan reads the mailboxes, then sends."""
    n = get_object_or_404(Notification, pk=pk)
    name = _actor(request)
    cfg = _sens()
    if cfg is None or cfg.mode != "live":
        messages.error(request, "자동 발송이 OFF입니다.")
    elif not name:
        messages.error(request, "발송하는 사람 이름을 입력하세요.")
    elif dispatch.request_send(n, name):
        refresh_from_master()  # today's recipients, in case the customer's address was the problem
        request_scan(settings.CSP_SCAN["request_path"], settings.CSP_SCAN["request_min_interval"])
        messages.success(request, "메일함을 다시 확인한 뒤 곧 보냅니다. 1분 안에 이 화면에 결과가 나옵니다.")
    else:
        messages.error(request, "지금 상태에서는 보낼 수 없습니다.")
    return redirect(reverse("notification", args=[pk]) + "#sending")


@login_required
@require_POST
def notification_copied(request, pk):
    n = get_object_or_404(Notification, pk=pk)
    what = {"from": "보내는 사람", "to": "받는 사람", "cc": "참조", "bcc": "숨은참조", "subject": "제목",
            "body": "본문"}.get(request.POST.get("what"), "안내문")
    record_copied(n, request.user, what, _actor(request))
    return JsonResponse({"status": n.status, "label": n.get_status_display()})


@login_required
@require_POST
def notification_sent(request, pk):
    n = get_object_or_404(Notification, pk=pk)
    record_manual_sent(n, request.user, request.POST.get("note", "").strip()[:500], _actor(request))
    sync_records()
    messages.success(request, "발송했다고 기록했습니다. 보낸 메일이 메일함에 들어오면 '고객 발송 확인'으로 바뀝니다.")
    return redirect("notification", pk=pk)


@login_required
@require_POST
def staff_confirm(request):
    name = _actor(request)
    back = _back(request)
    if not name:
        messages.error(request, "확인한 사람 이름을 입력하세요.")
        return redirect(back)
    ids = request.POST.getlist("ids")
    if not ids:
        messages.error(request, "확인할 안내를 선택하세요.")
        return redirect(back)
    done = sum(confirm_by_staff(n, request.user, name) for n in Notification.objects.filter(pk__in=ids))
    sync_records()
    skipped = len(ids) - done
    messages.success(request, f"{name} 님 이름으로 {done}건을 담당자 확인했습니다."
                     + (f" {skipped}건은 아직 발송이 확인되지 않아 건너뛰었습니다." if skipped else ""))
    return redirect(back)


@login_required
@require_POST
def staff_unconfirm(request, pk):
    n = get_object_or_404(Notification, pk=pk)
    if undo_staff_confirm(n, request.user, _actor(request)):
        sync_records()
        messages.info(request, "담당자 확인을 취소했습니다.")
    return redirect("notification", pk=pk)


# --- 작업 기록: the flat rows that outlive the yearly purge, on a page and as a CSV for Excel -----------------

RECORD_COLUMNS = (
    ("작업 시작", lambda r: _when(r.starts_at)), ("작업 종료", lambda r: _when(r.ends_at)),
    ("작업 내용", lambda r: r.work_title), ("구분", lambda r: r.tab), ("공지", lambda r: r.kind_label),
    ("고객", lambda r: r.customer), ("계정", lambda r: r.account), ("받는 사람", lambda r: r.recipients),
    ("상태", lambda r: r.status_label), ("KT 공지 수신", lambda r: _when(r.kt_received_at)),
    ("발송 확인", lambda r: _when(r.sent_at)), ("확인자", lambda r: r.staff_name),
    ("확인 시각", lambda r: _when(r.staff_confirmed_at)), ("지난 작업 처리", lambda r: r.closed_by),
    ("처리 시각", lambda r: _when(r.closed_at)),
)


def _when(dt) -> str:
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M") if dt else ""


def _records(request):
    """Rows for the page and the CSV: a year (default this year, "" for all), a month ("" for all) and a tab
    ("" for all)."""
    year = request.GET.get("year", str(timezone.localdate().year))
    month = request.GET.get("month", "")
    tab = request.GET.get("tab", "")
    rows = Record.objects.order_by("-starts_at", "tab", "account", "kind", "round")
    if year.isdigit():
        rows = rows.filter(starts_at__year=int(year))
    else:
        year = ""
    if month.isdigit() and 1 <= int(month) <= 12:
        rows = rows.filter(starts_at__month=int(month))
    else:
        month = ""
    if tab == "main":
        rows = rows.filter(tab=MAIN_TAB)
    elif tab.isdigit():
        channel = PartnerChannel.objects.filter(pk=int(tab)).first()
        rows = rows.filter(tab=channel.label) if channel else rows.none()
    else:
        tab = ""
    return rows, year, month, tab


@login_required
def records(request):
    rows, year, month, tab = _records(request)
    return render(request, "web/records.html", {
        "rows": rows, "year": year, "month": month, "tab": tab, "months": range(1, 13),
        "years": [d.year for d in Record.objects.dates("starts_at", "year", order="DESC")],
        "channels": PartnerChannel.objects.filter(active=True).order_by("pk"),
        "query": "&".join(f"{k}={v}" for k, v in (("year", year or "all"), ("month", month), ("tab", tab)) if v),
    })


@login_required
def records_csv(request):
    """UTF-8 with a BOM and CRLF, so Excel on Windows opens the Korean text as is."""
    rows, year, month, tab = _records(request)
    name = "작업기록_" + (f"{year}-{int(month):02d}" if year and month else year or "전체") + ".csv"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    response.write("﻿")
    writer = csv.writer(response, lineterminator="\r\n")
    writer.writerow([title for title, _ in RECORD_COLUMNS])
    for r in rows:
        writer.writerow([cell(r) for _, cell in RECORD_COLUMNS])
    return response


@login_required
def sent_mails(request):
    mails = (
        Notice.objects.filter(category__in=OUR_MAIL_CATEGORIES)
        .select_related("seen__header").prefetch_related("confirms", "targets")
        .order_by("-seen__header__date")[:300]
    )
    return render(request, "web/sent_mails.html", {"mails": mails})


@login_required
def sent_mail_detail(request, pk):
    mail = get_object_or_404(
        Notice.objects.select_related("seen__header", "seen__body"), pk=pk, category__in=SENT_MAIL_CATEGORIES)
    return render(request, "web/sent_mail.html", {
        "mail": mail, "notifications": mail.confirms.select_related("work").order_by("display_name"),
        "targets": mail.targets.all(),
    })


@login_required
def review(request):
    known = set(CustomerAccount.objects.filter(active=True).values_list("account_key", flat=True))
    missing = (
        NoticeTarget.objects.filter(notice__category__startswith="kt_")
        .exclude(account__in=known | InternalAccount.keys() | set(PartnerChannel.by_account()))
        .values("account").annotate(notices=Count("notice")).order_by("-notices")
    )
    unread = (Notice.objects.filter(status=Notice.Status.REVIEW, dismissed_at__isnull=True)
              .select_related("seen__header").order_by("-pk"))
    # KT mail with 작업 in the subject but none of the notice fields (e.g. '수작업 요청' inquiries) is kept
    # on purpose (user decision) and shown apart so it does not crowd real problems.
    no_fields = Q(title="", schedule="", targets__isnull=True)
    review_rows, late, unchecked = needs_attention_all()
    partners = PartnerChannel.by_account()

    def tabbed(rows):  # n.tab: the 별도 발송 tab of the row, for its label and its work link
        rows = list(rows.select_related("notice", "work"))
        for n in rows:
            n.tab = partners.get(n.account)
        return rows

    return render(request, "web/review.html", {
        "notifications": tabbed(review_rows.order_by("-notice__starts_at")),
        "late": tabbed(late.order_by("work__starts_at", "kind")),
        "unchecked": tabbed(unchecked.order_by("work__starts_at", "kind", "round", "account")),
        "confirmable": set(STAFF_CONFIRMABLE),
        "notices": unread.exclude(no_fields),
        "other_kt": unread.filter(no_fields, category__startswith="kt_"),
        "dismissed": Notice.objects.filter(dismissed_at__isnull=False).select_related("seen__header")
        .order_by("-dismissed_at")[:200],
        "customers": CustomerAccount.objects.filter(active=True, status=CustomerAccount.Status.REVIEW),
        "missing": missing,
        "unmatched_copies": unmatched_copies(),
    })


@login_required
@require_POST
def notices_dismiss(request):
    """Take mails a person has looked at off 확인 필요 (unreadable, or KT mail that is no work notice), or put
    one back. Nothing is deleted: the mail and its parse stay, and the list keeps who took it off."""
    ids = request.POST.getlist("ids")
    if request.POST.get("undo"):
        Notice.objects.filter(pk__in=ids).update(dismissed_at=None, dismissed_by="")
        messages.info(request, "확인 필요 목록으로 되돌렸습니다.")
        return redirect("review")
    name = _actor(request)
    if not name:
        messages.error(request, "확인한 사람 이름을 입력하세요.")
    elif not ids:
        messages.error(request, "목록에서 뺄 메일을 고르세요.")
    else:
        done = Notice.objects.filter(pk__in=ids, dismissed_at__isnull=True).update(
            dismissed_at=timezone.now(), dismissed_by=name)
        messages.success(request, f"{name} 님 이름으로 메일 {done}건을 목록에서 뺐습니다.")
    return redirect("review")


def _customer_rows(request):
    """The customers the page's search, 구분 and 확인 필요만 pick (the list and its CSV)."""
    rows = CustomerAccount.objects.filter(active=True)
    q = request.GET.get("q", "").strip()
    if q:
        rows = rows.filter(Q(account__icontains=q) | Q(company__icontains=q) | Q(name__icontains=q)
                           | Q(email__icontains=q) | Q(manager__icontains=q))
    kind = request.GET.get("kind", "")
    if kind in CustomerAccount.Kind.values:
        rows = rows.filter(kind=kind)
    if request.GET.get("review"):
        rows = rows.filter(status=CustomerAccount.Status.REVIEW)
    return rows.order_by("kind", "display_name", "account_key"), q, kind


@login_required
def customers(request):
    rows, q, kind = _customer_rows(request)
    return render(request, "web/customers.html", {
        "rows": rows[:500],
        "q": q, "kind": kind, "kinds": CustomerAccount.Kind.choices, "review_only": bool(request.GET.get("review")),
        "internal": {a: InternalAccount.Kind(k).label for a, k in InternalAccount.kinds().items()},
        "opsdb": opsdb.configured(), "opsdb_last": OpsDbSync.last(),
        "opsdb_stale": _opsdb_stale(),
    })


CUSTOMER_COLUMNS = ["구분", "계정", "회사명", "이름", "사용자 ID", "담당자", "이메일", "연락처", "상태", "입력 방식",
                    "웹에서만 관리", "발송 제외", "바뀐 날"]


@login_required
def customers_csv(request):
    """The listed customers as a file for Excel (UTF-8 with a BOM, CRLF), with the page's search and filters."""
    rows, _, kind = _customer_rows(request)
    excluded = {a: InternalAccount.Kind(k).label for a, k in InternalAccount.kinds().items()}
    name = f"고객정보_{dict(CustomerAccount.Kind.choices).get(kind, '전체')}_{timezone.localdate():%Y%m%d}.csv"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    response.write("\ufeff")
    writer = csv.writer(response, lineterminator="\r\n")
    writer.writerow(CUSTOMER_COLUMNS)

    def lines(text):
        return ", ".join(line.strip() for line in text.splitlines() if line.strip())

    for r in rows:
        writer.writerow([
            r.get_kind_display(), r.account, r.company, r.name, r.userid, lines(r.manager), lines(r.email),
            lines(r.phone), "정상" if r.status == CustomerAccount.Status.OK else "확인 필요", r.get_source_display(),
            "예" if r.web_only else "", excluded.get(r.account_key, ""), timezone.localtime(r.updated_at).strftime("%Y-%m-%d %H:%M"),
        ])
    return response


def _opsdb_stale() -> bool:
    """No successful read of the company DB for three scan intervals."""
    last_ok = OpsDbSync.objects.filter(ok=True).order_by("-pk").first()
    return opsdb.configured() and (last_ok is None or last_ok.finished_at < timezone.now() - STALE_AFTER)


@login_required
@require_POST
def customers_sync(request):
    """지금 조회: read the company DB now instead of waiting for the next scan."""
    run = opsdb.sync(interactive=True)
    if not run.ok:
        messages.error(request, f"운영 DB 조회 실패: {run.error}")
    else:
        messages.success(request, f"운영 DB {run.rows}건 조회" + (f", 고객 정보 갱신 ({run.batch.rows}건)" if run.batch else ", 변경 없음")
                         + (f", 안 쓰는 계정 해제 {run.lifted}개" if run.lifted else ""))
        if run.batch:
            apply_internal_lists()
            _refresh_notifications(request)
    return redirect("customers")


@login_required
def internal_accounts(request):
    """Accounts with no customer to tell (사내 계정, 안 쓰는 계정): their work stays visible, but no customer
    notice is sent or checked."""
    Kind = InternalAccount.Kind
    if request.method == "POST" and "company" in request.POST:
        return _internal_company_add(request)
    if request.method == "POST":
        key = InternalAccount.key(request.POST.get("account", ""))
        kind = request.POST.get("kind") if request.POST.get("kind") in Kind.values else Kind.INTERNAL
        if "@" not in key:
            messages.error(request, "KT Cloud 계정(이메일 형식)을 입력하세요.")
            return redirect(_back(request, "/customers/internal/"))
        row, _ = InternalAccount.objects.get_or_create(account_key=key, defaults={"added_at": timezone.now()})
        row.note = request.POST.get("note", "").strip()[:200] or row.note
        row.kind, row.active, row.added_by, row.added_at = kind, True, _actor(request), timezone.now()
        row.removed_by, row.removed_at = "", None
        row.save()
        moved = apply_internal_accounts()[kind]
        label = Kind(kind).label
        messages.success(request, f"{label}으로 지정했습니다. 발송 전이던 안내 {moved}건을 발송 대상에서 뺐습니다."
                         if moved else f"{label}으로 지정했습니다.")
        return redirect(_back(request, "/customers/internal/"))
    rows = list(InternalAccount.objects.filter(active=True).order_by("kind", "note", "account_key"))
    works = (Notification.objects.filter(status__in=NO_SEND).values("account")
             .annotate(n=Count("work", distinct=True)))
    per_account = {w["account"]: w["n"] for w in works}
    for r in rows:
        r.works = per_account.get(r.account_key, 0)
    since = timezone.now() - timedelta(days=14)
    return render(request, "web/internal.html", {
        "rows": rows,
        "kinds": Kind.choices,
        "removed": InternalAccount.objects.filter(active=False).order_by("-removed_at")[:20],
        "companies": InternalCompany.objects.filter(active=True).order_by("name"),
        "notifications": Notification.objects.filter(status__in=NO_SEND, notice__starts_at__gte=since)
        .select_related("notice", "work").order_by("notice__starts_at"),
    })


def _internal_company_add(request):
    """사내 고객명: every account under this customer name is 사내 계정, now and as the DB adds more."""
    name = " ".join(request.POST.get("company", "").split())[:200]
    if not name:
        messages.error(request, "고객명을 입력하세요.")
        return redirect("internal_accounts")
    row, _ = InternalCompany.objects.get_or_create(key=InternalCompany.key_of(name),
                                                   defaults={"name": name, "added_at": timezone.now()})
    row.name, row.active, row.added_by, row.added_at = name, True, _actor(request), timezone.now()
    row.removed_by, row.removed_at = "", None
    row.save()
    counts = apply_internal_lists()
    messages.success(request, f"사내 고객명 '{name}': 계정 {counts['accounts marked']}개를 사내 계정으로 지정했습니다."
                     + (f" 발송 전이던 안내 {counts['internal']}건을 발송 대상에서 뺐습니다." if counts["internal"] else ""))
    return redirect("internal_accounts")


@login_required
@require_POST
def internal_company_remove(request, pk):
    """Later accounts under the name are no longer marked; accounts already marked stay until removed one by one."""
    row = get_object_or_404(InternalCompany, pk=pk, active=True)
    row.active, row.removed_by, row.removed_at = False, _actor(request), timezone.now()
    row.save()
    messages.info(request, f"사내 고객명 '{row.name}'을 해제했습니다. 이미 지정된 계정은 그대로입니다.")
    return redirect("internal_accounts")


@login_required
@require_POST
def internal_remove(request, pk):
    row = get_object_or_404(InternalAccount, pk=pk, active=True)
    row.active, row.removed_by, row.removed_at = False, _actor(request), timezone.now()
    row.save()
    back = apply_internal_accounts()["internal removed"]
    label = row.get_kind_display()
    messages.info(request, f"{label}에서 해제했습니다. 안내 {back}건이 다시 발송 대상이 됐습니다."
                  if back else f"{label}에서 해제했습니다.")
    return redirect("internal_accounts")


@login_required
def suggestions(request):
    if request.method == "POST":
        body = request.POST.get("body", "").strip()[:5000]
        kind = request.POST.get("kind")
        if not body or kind not in Suggestion.Kind.values:
            messages.error(request, "종류를 고르고 내용을 적어 주세요.")
        else:
            Suggestion.objects.create(kind=kind, body=body, page=request.POST.get("page", "").strip()[:200],
                                      author=_actor(request))
            messages.success(request, "건의사항을 남겼습니다. 처리하면 이 화면에 상태와 답변이 표시됩니다.")
            return redirect("suggestions")
    show = request.GET.get("show", "open")
    rows = Suggestion.objects.order_by("-pk")
    if show == "open":
        rows = rows.filter(status__in=Suggestion.OPEN)
    elif show == "closed":
        rows = rows.exclude(status__in=Suggestion.OPEN)
    return render(request, "web/suggestions.html", {
        "rows": rows[:200], "show": show, "kinds": Suggestion.Kind.choices,
        "open_count": Suggestion.objects.filter(status__in=Suggestion.OPEN).count(),
    })


@login_required
def suggestion_detail(request, pk):
    s = get_object_or_404(Suggestion, pk=pk)
    if request.method == "POST":
        status, name = request.POST.get("status"), _actor(request)
        if status not in Suggestion.Status.values or not name:
            messages.error(request, "상태를 고르고 처리한 사람 이름을 입력하세요.")
        else:
            s.handle(status, request.POST.get("reply", "").strip()[:5000], name)
            messages.success(request, f"{s.get_status_display()} 상태로 바꿨습니다.")
            return redirect("suggestion", pk=s.pk)
    return render(request, "web/suggestion.html", {"s": s, "statuses": Suggestion.Status.choices})


def _refresh_notifications(request):
    """After a customer change: re-judge unsent notifications against the master and say what moved."""
    counts = refresh_from_master()
    moved = [
        (label, counts[key]) for key, label in [
            (f"{S.NEEDS_REVIEW}->{S.DRAFT_READY}", "확인 필요 → 발송 대기"),
            (f"{S.DRAFT_READY}->{S.NEEDS_REVIEW}", "발송 대기 → 확인 필요"),
            (f"{S.COPIED}->{S.NEEDS_REVIEW}", "작성 중 → 확인 필요"),
            (f"{S.SENT_COPY_CANDIDATE}->{S.SENT_COPY_CONFIRMED}", "발송 추정 → 고객 발송 확인"),
            ("recipients updated", "받는 사람과 고객명 갱신"),
        ] if counts[key]
    ]
    if moved:
        messages.info(request, "고객 안내에 반영했습니다: " + ", ".join(f"{label} {n}건" for label, n in moved))


def _source_values(request):
    return {col: request.POST.get(col, "") for col in CustomerAccount.SOURCE_COLUMNS}


def _web_only(request, current: bool) -> bool:
    """The 웹에서만 관리 checkbox, when the form showed it (only with the company DB set up)."""
    return bool(request.POST.get("web_only")) if request.POST.get("web_only_shown") else current


@login_required
def customer_edit(request, pk):
    row = get_object_or_404(CustomerAccount, pk=pk, active=True)
    # A row from the company DB is the DB's to change: the next read would put its values back anyway.
    readonly = row.source == CustomerAccount.Source.DB and opsdb.configured()
    if request.method == "POST" and readonly:
        if request.POST.get("take_over"):  # from now on this account is kept on the web only
            new = row.edit(request.user, web_only=True)
            messages.success(request, "웹에서만 관리하도록 바꿨습니다. 운영 DB 값으로 바뀌지 않습니다.")
            return redirect("customer_edit", pk=new.pk)
        messages.error(request, "운영 DB에서 온 고객은 운영 DB에서 고칩니다. 5분 안에 이 화면에 반영됩니다.")
        return redirect("customer_edit", pk=pk)
    if request.method == "POST":
        new = row.edit(request.user, web_only=_web_only(request, row.web_only), **_source_values(request))
        messages.success(request, "저장했습니다. 이전 내용은 아래 변경 이력에 남았습니다."
                         + (" 확인이 필요한 항목이 있습니다." if new.status == CustomerAccount.Status.REVIEW else ""))
        _refresh_notifications(request)
        return redirect("customer_edit", pk=new.pk)
    history = []
    previous = row.replaces
    while previous is not None and len(history) < 10:
        history.append(previous)
        previous = previous.replaces
    return render(request, "web/customer_form.html", {"row": row, "history": history, "kinds": None,
                                                      "readonly": readonly or not request.user.is_staff,
                                                      "db_row": readonly, "opsdb": opsdb.configured()})


@login_required
def customer_new(request):
    if not request.user.is_staff:
        messages.error(request, "관리 계정만 고객을 추가할 수 있습니다.")
        return redirect("customers")
    if request.method == "POST":
        kind = request.POST.get("kind")
        if kind not in CustomerAccount.Kind.values:
            messages.error(request, "공공 또는 일반을 고르세요.")
        else:
            row = CustomerAccount(kind=kind, source=CustomerAccount.Source.WEB, edited_by=request.user,
                                  web_only=_web_only(request, False), **_source_values(request))
            row.save()
            messages.success(request, "고객을 추가했습니다.")
            _refresh_notifications(request)
            return redirect("customer_edit", pk=row.pk)
    row = CustomerAccount(account=request.GET.get("account", ""), kind=CustomerAccount.Kind.GENERAL)
    return render(request, "web/customer_form.html", {"row": row, "history": [], "kinds": CustomerAccount.Kind.choices,
                                                      "opsdb": opsdb.configured()})


class PasswordChange(auth_views.PasswordChangeView):
    """Change the (shared) sign-in's password; also the page a required change sends every request to."""

    template_name = "web/password_change.html"
    success_url = reverse_lazy("dashboard")

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "forced": PasswordPolicy.required(self.request.user)}

    def form_valid(self, form):
        response = super().form_valid(form)  # saves it and keeps this browser signed in; other sessions end
        PasswordPolicy.objects.filter(user=self.request.user).update(must_change=False, changed_at=timezone.now())
        messages.success(self.request, "비밀번호를 바꿨습니다. 다른 PC는 새 비밀번호로 다시 로그인해야 합니다.")
        return response


# --- 환경 설정 ---------------------------------------------------------------------------------------------------

def _send_options(cfg) -> list[tuple[str, str, bool]]:
    """(mode, label, can be chosen): ON needs sens.env with keys and without SEND_MODE=off."""
    return [("live", "ON", bool(cfg and cfg.access_key and cfg.mode == "live")), ("off", "OFF", True)]


# 환경 설정 > KT 공지 양식, as rule tables (user, 2026-10-22: easier to follow than one field per word list).
KT_KINDS = (  # word list, notice kind, a KT subject it matches
    ("work", "작업 공지", "[kt cloud 작업 공지]"),
    ("complete", "작업 완료 공지", "[kt cloud 작업 완료 공지]"),
    ("cancel", "작업 취소/변경 공지", "[kt cloud 작업 취소 공지]"),
    ("postpone", "작업 연기 공지", "[kt cloud 작업 연기 공지]"),
    ("urgent", "긴급 작업 공지", "[kt cloud 장애 예방 긴급 작업 공지]"),
    ("incident", "장애 발생 공지", "[kt cloud 컴퓨팅 노드 Down 발생 공지]"),
    ("recovery", "장애 복구 공지", "[kt cloud 컴퓨팅 노드 Down 복구 공지]"),
    ("check", "서비스 점검 요청 공지", "[KT cloud 서비스 점검 요청 공지]"),
    ("skip", "공지 아님 (넘김)", ""),
)
KT_USES = {  # where each item read from the body is used
    "accounts": "안내 받을 고객", "servers": "협력기관 그룹, 작업 화면", "lbs": "협력기관 그룹, 작업 화면",
    "title": "작업 이름, 안내 제목", "schedule": "작업 시간, 달력", "detail": "작업 화면", "impact": "작업 화면",
}


def _kt_fields(form: ktform.KtForm) -> dict:
    """The form as the page's inputs show it."""
    return {
        "sender": form.sender, "footer": "\n".join(form.footer),
        "kinds": [(key, title, ", ".join(getattr(form, key)), example) for key, title, example in KT_KINDS],
        "labels": [(name, title, ", ".join(form.labels_of(name)), name in ktform.REQUIRED, KT_USES.get(name, "기록만"))
                   for name, title in ktform.FIELDS],
    }


def _locked(request, work):
    """Run work() under the scan lock, or say the scan is busy."""
    try:
        with scan_lock(settings.CSP_SCAN["lock_path"]):
            return work()
    except AlreadyRunning:
        messages.info(request, "메일 수집 중입니다. 잠시 후 다시 누르세요.")
        return None


@login_required
def config(request):
    """환경 설정: 자동 발송 on/off and the KT 공지 양식 (what the site reads from KT's mails)."""
    try:
        ceiling, problem = sens.load_config(), ""
    except ConfigError as exc:
        ceiling, problem = None, str(exc)
    saved = ktform.current()
    shown, kt_problems, preview = saved, [], None
    if request.method == "POST":
        action, name = request.POST.get("action", ""), _actor(request)
        if action == "send_mode":
            mode = request.POST.get("mode", "")
            if mode not in [m for m, _, ok in _send_options(ceiling) if ok]:
                messages.error(request, "선택할 수 없는 발송 상태입니다.")
            elif not name:
                messages.error(request, "변경하는 사람 이름을 입력하세요.")
            elif mode == dispatch.chosen_mode():
                messages.info(request, f"이미 {SEND_MODE_LABEL[mode]}입니다.")
            else:
                dispatch.choose(mode, name)
                messages.success(request, f"자동 발송: {SEND_MODE_LABEL[mode]}")
            return redirect(reverse("config") + "#sending")
        if action in ("kt_preview", "kt_save", "kt_default"):
            shown, kt_problems = (ktform.DEFAULT, []) if action == "kt_default" else ktform.from_post(request.POST)
            if not kt_problems and action == "kt_preview":
                preview = config_tools.preview(saved, shown)
            elif not kt_problems and not name:
                kt_problems = ["저장하는 사람 이름을 입력하세요."]
            elif not kt_problems and shown == saved:
                messages.info(request, "바뀐 내용이 없습니다.")
                return redirect(reverse("config") + "#kt")
            elif not kt_problems:
                ktform.save(shown, name)
                redrafted = _locked(request, redraft_unsent) if shown.footer != saved.footer else 0
                messages.success(request, "KT 공지 양식을 저장했습니다." + (f" 초안 {redrafted}건을 다시 썼습니다." if redrafted else ""))
                return redirect(reverse("config") + "#kt")
        elif action == "kt_reread":
            counts = _locked(request, lambda: config_tools.reread(saved))
            if counts is not None:
                messages.success(request, f"KT 공지 {counts['read']}건을 다시 읽었습니다. 문제없이 읽힌 공지 "
                                          f"{counts['ok']}건, 새로 만든 안내 {counts['notifications created']}건, "
                                          f"다시 쓴 초안 {counts['redrafted']}건입니다.")
            return redirect(reverse("config") + "#kt")
        elif action == "kt_recheck":
            done = _locked(request, config_tools.recheck)
            if done is not None:
                request_scan(settings.CSP_SCAN["request_path"], settings.CSP_SCAN["request_min_interval"])
                messages.success(request, f"KT 기타 {done}건을 다음 수집에서 다시 분류합니다.")
            return redirect(reverse("config") + "#kt")
    effective = _sens()
    return render(request, "web/config.html", {
        "send": {
            "mode": effective.mode if effective else "off", "chosen": dispatch.chosen_mode(),
            "mode_label": SEND_MODE_LABEL[effective.mode if effective else "off"],
            "blocked": bool(ceiling and ceiling.access_key and ceiling.mode == "off"), "cfg": ceiling, "problem": problem,
            "options": _send_options(ceiling),
            "history": SendSetting.objects.order_by("-pk")[:10],
        },
        "kt": _kt_fields(shown), "kt_problems": kt_problems, "preview": preview, "kt_changed": shown != saved,
        "kt_history": KtFormSetting.objects.order_by("-pk")[:10],
        "reread": config_tools.reread_candidates().count(), "recheck": config_tools.recheck_candidates().count(),
        "recheck_days": config_tools.RECHECK.days, "kt_days": config_tools.DAYS,
    })
