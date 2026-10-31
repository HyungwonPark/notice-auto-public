from django.db import models
from django.db.models import Q

from mailcollect import headers


class Notification(models.Model):
    """One customer account's notice for one KT work (handoff §8): a draft, then evidence that it was sent.

    A copy in the in-house mailbox proves only that 예시클라우드 sent it, never that the customer received it."""

    class Kind(models.TextChoices):
        NOTICE = "notice", "사전 공지"
        COMPLETE = "complete", "완료 공지"

    class Status(models.TextChoices):
        NEEDS_REVIEW = "needs_review", "확인 필요"
        DRAFT_READY = "draft_ready", "발송 대기"
        COPIED = "copied", "작성 중"  # copied in the web page; copying is not sending
        SENT_COPY_CANDIDATE = "sent_copy_candidate", "발송 추정"  # sent mail found, customer address missing
        SENT_COPY_CONFIRMED = "sent_copy_confirmed", "고객 발송 확인"  # sent mail found in the mailbox
        MANUAL_SENT_RECORDED = "manual_sent_recorded", "발송 기록"  # staff said they sent it
        STAFF_CONFIRMED = "staff_confirmed", "담당자 확인"  # final check by a named person
        INTERNAL = "internal", "사내 계정"  # 예시클라우드's own account: nothing to send or check
        UNUSED = "unused", "안 쓰는 계정"  # nobody uses the account any more: nothing to send or check
        SEND_REQUESTED = "send_requested", "발송 요청됨"  # this site sent it through SENS; result pending
        SEND_FAILED = "send_failed", "발송 실패"  # automatic sending gave up: a person presses 발송하기

    number = models.CharField(max_length=20, unique=True, null=True)  # 안내번호, set right after insert
    notice = models.ForeignKey("mailcollect.Notice", on_delete=models.PROTECT, related_name="notifications")
    work = models.ForeignKey("mailcollect.Work", on_delete=models.SET_NULL, null=True, related_name="notifications")
    account = models.CharField(max_length=320)
    # A 별도 발송 customer split by system: one notification per group the notice's servers belong to.
    # Empty for everyone else and for a notice that goes to the whole 받는 사람 list.
    group = models.ForeignKey("customers.PartnerGroup", on_delete=models.PROTECT, null=True, blank=True,
                              related_name="notifications")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # 차수: KT sends the same 사전 공지 again days later (e.g. a week and a day before), and staff send the
    # customer notice again each time. One notification per round; a 완료 공지 has only round 1.
    round = models.PositiveSmallIntegerField(default=1)
    display_name = models.TextField(blank=True)
    recipients = models.JSONField(default=list)  # customer master emails when the draft was made
    subject = models.TextField(blank=True)
    body = models.TextField(blank=True)
    status = models.CharField(max_length=24, choices=Status.choices)
    review_reasons = models.TextField(blank=True)  # generic descriptions, one per line
    # 예시클라우드 copies that matched. Several are normal: staff resend, or mail each contact separately.
    copies = models.ManyToManyField("mailcollect.Notice", related_name="confirms", blank=True)
    match_basis = models.CharField(max_length=24, blank=True)  # first match: "number" or "account_kind_window"
    # One shared login is used, so the person confirming types their name.
    staff_name = models.CharField(max_length=50, blank=True)
    staff_confirmed_at = models.DateTimeField(null=True)
    status_before_staff = models.CharField(max_length=24, blank=True)  # restored if the check is undone
    send_failures = models.PositiveSmallIntegerField(default=0)  # failed SENS attempts in a row (see dispatch.py)
    send_requested_by = models.CharField(max_length=50, blank=True)  # 발송하기 pressed by this name, not yet sent
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["work", "account", "kind", "round"], condition=Q(work__isnull=False, group__isnull=True),
                name="uniq_work_account_kind_round"
            ),
            models.UniqueConstraint(fields=["notice", "account", "kind"], condition=Q(group__isnull=True),
                                    name="uniq_notice_account_kind"),
            models.UniqueConstraint(
                fields=["work", "account", "kind", "round", "group"],
                condition=Q(work__isnull=False, group__isnull=False), name="uniq_work_account_kind_round_group"
            ),
            models.UniqueConstraint(fields=["notice", "account", "kind", "group"], condition=Q(group__isnull=False),
                                    name="uniq_notice_account_kind_group"),
        ]
        indexes = [models.Index(fields=["account", "kind"])]

    @property
    def kind_label(self) -> str:
        """사전 공지 1차, 사전 공지 2차, …, 완료 공지; an outage's 장애 발생 공지 and 장애 복구 공지; 작업 연기 공지."""
        if self.work_id and self.work.incident:
            if self.notice.category == headers.KT_CHECK:
                return "서비스 점검 요청 공지"
            return "장애 발생 공지" if self.kind == self.Kind.NOTICE else "장애 복구 공지"
        if self.kind == self.Kind.NOTICE and self.notice.category == headers.KT_POSTPONE:
            return "작업 연기 공지"
        return f"사전 공지 {self.round}차" if self.kind == self.Kind.NOTICE else self.get_kind_display()

    def add_reason(self, reason: str) -> bool:
        reasons = [r for r in self.review_reasons.split("\n") if r]
        if reason in reasons:
            return False
        self.review_reasons = "\n".join([*reasons, reason])
        return True


class NotificationEvent(models.Model):
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=24)
    note = models.TextField(blank=True)
    by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True)  # empty for automatic steps
    actor = models.CharField(max_length=50, blank=True)  # name typed in the page (shared login)


class Dispatch(models.Model):
    """One attempt to send a customer notice through SENS (notifications/dispatch.py).

    The constraint below is the last line against sending twice: per notice and mode there can be only one
    attempt that is under way, unanswered, accepted or done. Only a failed attempt makes room for another."""

    class Mode(models.TextChoices):
        TEST = "test", "테스트"
        LIVE = "live", "실제 발송"

    class State(models.TextChoices):
        SENDING = "sending", "보내는 중"
        UNKNOWN = "unknown", "응답 확인 중"  # no answer: SENS is asked before anything else happens
        ACCEPTED = "accepted", "접수됨"
        DONE = "done", "발송 완료"
        PARTIAL = "partial", "일부 실패"
        FAILED = "failed", "실패"
        HELD = "held", "보류"  # rule 6: SENS already has a mail of this notice the site did not send

    ACTIVE = (State.SENDING, State.UNKNOWN, State.ACCEPTED, State.DONE, State.PARTIAL)

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="dispatches")
    mode = models.CharField(max_length=4, choices=Mode.choices)
    state = models.CharField(max_length=10, choices=State.choices)
    by = models.CharField(max_length=50, blank=True)  # "" automatic; the typed name for 발송하기 / 테스트 발송
    subject = models.TextField()
    to = models.JSONField(default=list)
    cc = models.JSONField(default=list)
    bcc = models.JSONField(default=list)
    body_sha = models.CharField(max_length=64, blank=True)  # SHA-256 of the HTML body sent (rule 3), not the mail
    request_id = models.CharField(max_length=100, blank=True)
    error = models.TextField(blank=True)  # SENS's code and message, never the mail
    error_kind = models.CharField(max_length=10, blank=True)  # retry / unknown / permanent / global (sens.py)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    checked_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(  # the states of ACTIVE (a class body is out of Meta's reach)
            fields=["notification", "mode"], condition=Q(state__in=["sending", "unknown", "accepted", "done", "partial"]),
            name="uniq_active_dispatch")]


class SendSetting(models.Model):
    """환경 설정 > 자동 발송 ON/OFF: one row per change, the newest applies. SEND_MODE=off in /etc/csp-notice/sens.env
    overrides it (dispatch.config()), so a restored backup or a rehearsal server never sends on a choice made elsewhere."""

    mode = models.CharField(max_length=4, choices=[("off", "OFF"), ("live", "ON")])
    by = models.CharField(max_length=50)
    at = models.DateTimeField(auto_now_add=True)


class SendingSince(models.Model):
    """When a sending mode was first switched on: only notices made after it are sent automatically, so turning
    sending on never mails the backlog."""

    mode = models.CharField(max_length=4, unique=True)
    since = models.DateTimeField()


class Record(models.Model):
    """작업 기록: one flat row per customer notice, like a line of a CSV, kept for good.

    The yearly purge deletes mail, notices and their history (CSP_KEEP_DAYS); this is what must not go with
    them: when the work was, what it was, which customer was told, who sent and who checked. No mail text.
    Filled by notifications.records.sync (every scan and after staff actions), read by the 작업 기록 page."""

    notification = models.OneToOneField(Notification, on_delete=models.SET_NULL, null=True, related_name="record")
    tab = models.CharField(max_length=100)  # 작업 공지, or a 별도 발송 tab's label
    work_key = models.CharField(max_length=64, blank=True)
    work_title = models.TextField(blank=True)
    starts_at = models.DateTimeField(null=True)
    ends_at = models.DateTimeField(null=True)
    kind = models.CharField(max_length=10, choices=Notification.Kind.choices)
    round = models.PositiveSmallIntegerField(default=1)
    label = models.CharField(max_length=40, blank=True)  # Notification.kind_label (장애, 작업 연기 공지 included)
    account = models.CharField(max_length=320)
    customer = models.TextField(blank=True)  # the customer's name when the draft was made
    recipients = models.TextField(blank=True)  # "; " joined
    status = models.CharField(max_length=24, choices=Notification.Status.choices)
    kt_received_at = models.DateTimeField(null=True)  # KT's mail
    sent_at = models.DateTimeField(null=True)  # first 보낸 메일 seen, else when staff recorded sending
    staff_name = models.CharField(max_length=50, blank=True)
    staff_confirmed_at = models.DateTimeField(null=True)
    closed_by = models.CharField(max_length=50, blank=True)  # moved to 지난 작업 by hand, on this tab
    closed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["starts_at"]), models.Index(fields=["tab", "starts_at"])]

    @property
    def kind_label(self) -> str:
        if self.label:
            return self.label
        return f"사전 공지 {self.round}차" if self.kind == Notification.Kind.NOTICE else "완료 공지"

    @property
    def status_label(self) -> str:
        return Notification.Status(self.status).label if self.status in Notification.Status.values else self.status


class WorkClosure(models.Model):
    """A work moved to 지난 작업 by hand, per tab: 작업 공지 (no channel) or one 별도 발송 tab.

    The tabs share KT's works but not their customers, so each tab closes a work on its own."""

    work = models.ForeignKey("mailcollect.Work", on_delete=models.CASCADE, related_name="closures")
    channel = models.ForeignKey("customers.PartnerChannel", on_delete=models.CASCADE, null=True, blank=True)
    closed_at = models.DateTimeField()
    closed_by = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["work"], condition=Q(channel__isnull=True), name="uniq_main_closure"),
            models.UniqueConstraint(fields=["work", "channel"], condition=Q(channel__isnull=False),
                                    name="uniq_channel_closure"),
        ]
