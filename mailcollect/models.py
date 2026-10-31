from django.db import models

from . import headers


class Mailbox(models.Model):
    """One POP3 account. UIDLs are unique only within a mailbox, so they are scoped by it."""

    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField()
    username = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    # From the env file on every scan: "main" or the extra file's name, its label and what it collects.
    name = models.CharField(max_length=60, blank=True)
    label = models.CharField(max_length=100, blank=True)
    collect = models.CharField(max_length=10, default="all")
    # Extra mailboxes start disabled: check their size first (`manage.py mailboxes --probe`), then enable.
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["host", "port", "username"], name="uniq_mailbox")]

    def __str__(self):
        return f"{self.username}@{self.host}:{self.port}"

    @property
    def title(self) -> str:
        return self.label or ("기본 메일함" if self.name in ("", "main") else self.name)


class ScanRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running"
        OK = "ok"  # every message on the server classified, every needed body retrieved
        UNCHANGED = "unchanged"  # STAT equal to the last run and nothing pending: no listing sent
        PARTIAL = "partial"  # batch or time limit reached, or a TOP/RETR was refused
        LOGIN_FAILED = "login_failed"  # wrong credentials or maildrop locked by another client
        FAILED = "failed"  # connection or protocol error

    mailbox = models.ForeignKey(Mailbox, on_delete=models.PROTECT, related_name="runs")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    stat_count = models.PositiveIntegerField(null=True)
    stat_octets = models.PositiveBigIntegerField(null=True)
    server_count = models.PositiveIntegerField(null=True)
    new_count = models.PositiveIntegerField(default=0)
    headers_fetched = models.PositiveIntegerField(default=0)
    matched = models.PositiveIntegerField(default=0)  # KT originals: notice, urgent, complete, cancel
    bodies_fetched = models.PositiveIntegerField(default=0)
    gone_count = models.PositiveIntegerField(default=0)
    gone_unscanned = models.PositiveIntegerField(default=0)  # left the server before we read its header
    body_lost = models.PositiveIntegerField(default=0)  # left the server before we retrieved its body
    pending_left = models.PositiveIntegerField(default=0)  # headers still to read
    bodies_left = models.PositiveIntegerField(default=0)  # bodies still to retrieve
    skipped = models.PositiveIntegerField(default=0)  # first listing of a mailbox: older than its backfill, not read
    error = models.TextField(blank=True)


class MessageSeen(models.Model):
    """Every UIDL seen on the server. Mail outside headers.KEPT keeps only this row: no subject or sender."""

    class State(models.TextChoices):
        PENDING = "pending"
        CLASSIFIED = "classified"
        SKIPPED = "skipped"  # already on the server when the mailbox was added, beyond its backfill: never read

    mailbox = models.ForeignKey(Mailbox, on_delete=models.PROTECT, related_name="messages")
    uidl = models.CharField(max_length=70)  # RFC 1939: 1-70 printable characters
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()  # first seen, or seen again after having left the server
    gone_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    category = models.CharField(max_length=20, choices=[(c, c) for c in headers.CATEGORIES], blank=True)
    classified_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    body_attempts = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["mailbox", "uidl"], name="uniq_mailbox_uidl")]
        indexes = [models.Index(fields=["mailbox", "state"])]


class MessageHeader(models.Model):
    """Header of a message whose category is in headers.KEPT."""

    seen = models.OneToOneField(MessageSeen, on_delete=models.CASCADE, related_name="header")
    raw = models.BinaryField()
    subject = models.TextField()
    from_addr = models.CharField(max_length=320, blank=True)
    from_display = models.TextField(blank=True)
    date_raw = models.TextField(blank=True)
    date = models.DateTimeField(null=True)
    message_id = models.TextField(blank=True)
    parse_errors = models.TextField(blank=True)


class Work(models.Model):
    """One announced work: KT sends the same notice once per affected account."""

    key = models.CharField(max_length=64, unique=True)  # sha256 of normalised title + start
    title = models.TextField()
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True)
    # A KT outage (headers.INCIDENTS): starts when KT's 발생 mail was sent, ends with its 복구 mail (a 서비스 점검 요청
    # has only its first mail). Its 발생 and
    # 복구 notices take the places of 사전 and 완료 공지.
    incident = models.BooleanField(default=False)
    # KT's next notice of the same title after a 작업 연기 공지: the postponed work it does again (2026-10-25).
    rework_of = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="reworks")
    created_at = models.DateTimeField(auto_now_add=True)

    def postponement(self):
        """KT's latest 작업 연기 공지 of this work, or None."""
        return (self.notices.filter(category="kt_postpone").select_related("seen__header")
                .order_by("-seen__header__date", "-pk").first())


class Notice(models.Model):
    """Fields parsed from a stored body. Raw values are kept as written; parsed values sit beside them."""

    class Status(models.TextChoices):
        OK = "ok"
        REVIEW = "review"  # something missing or inconsistent: a person decides

    seen = models.OneToOneField(MessageSeen, on_delete=models.CASCADE, related_name="notice")
    work = models.ForeignKey(Work, on_delete=models.SET_NULL, null=True, related_name="notices")
    category = models.CharField(max_length=20)  # copy of seen.category at parse time
    title = models.TextField(blank=True)
    detail = models.TextField(blank=True)
    schedule = models.TextField(blank=True)
    starts_at = models.DateTimeField(null=True)
    ends_at = models.DateTimeField(null=True)
    windows = models.JSONField(default=list)  # [[start ISO, end ISO], ...]
    impact = models.TextField(blank=True)
    audience = models.TextField(blank=True)
    target = models.TextField(blank=True)
    masked_customer = models.TextField(blank=True)
    servers = models.JSONField(default=list)
    lbs = models.JSONField(default=list)
    extra = models.JSONField(default=dict)
    recipients = models.JSONField(default=list)  # To/Cc of a 예시클라우드 copy
    status = models.CharField(max_length=10, choices=Status.choices)
    problems = models.TextField(blank=True)
    # A person looked at a mail on 확인 필요 (unreadable, or KT mail that is no work notice) and took it off.
    dismissed_at = models.DateTimeField(null=True)
    dismissed_by = models.CharField(max_length=50, blank=True)
    parser_version = models.PositiveSmallIntegerField()
    parsed_at = models.DateTimeField(auto_now=True)


class NoticeTarget(models.Model):
    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="targets")
    account = models.CharField(max_length=320)  # lower-cased 대상계정명

    class Meta:
        constraints = [models.UniqueConstraint(fields=["notice", "account"], name="uniq_notice_account")]
        indexes = [models.Index(fields=["account"])]


class MessageBody(models.Model):
    """Full message of a category in headers.BODY, exactly as RETR delivered it (CRLF line ends)."""

    seen = models.OneToOneField(MessageSeen, on_delete=models.CASCADE, related_name="body")
    raw = models.BinaryField()
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    fetched_at = models.DateTimeField(auto_now_add=True)
    text = models.TextField(blank=True)
    html = models.TextField(blank=True)
    parse_errors = models.TextField(blank=True)


class KtFormSetting(models.Model):
    """환경 설정 > KT 공지 양식: one row per save, the newest applies (ktform.current()). Rules, never mail."""

    values = models.JSONField()  # KtForm.values()
    by = models.CharField(max_length=50)
    at = models.DateTimeField(auto_now_add=True)
