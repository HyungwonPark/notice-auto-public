import re

from django.db import models

from .rows import derive


class ImportBatch(models.Model):
    """One loaded source file (later: one sync from the operations DB)."""

    kind = models.CharField(max_length=10)
    file_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    rows = models.PositiveIntegerField()
    review_rows = models.PositiveIntegerField()
    raw = models.BinaryField(null=True)  # the file as loaded, for history
    imported_at = models.DateTimeField(auto_now_add=True)


class CustomerAccount(models.Model):
    """One customer row exactly as the source gives it: the CSV now, the operations DB query later.

    The seven source columns are stored verbatim. Derived fields are recomputed on every save, so a web
    edit of a source column updates them too."""

    class Kind(models.TextChoices):
        PUBLIC = "public", "공공"
        GENERAL = "general", "일반"

    class Source(models.TextChoices):
        CSV = "csv", "CSV 파일"
        WEB = "web", "화면에서 입력"
        DB = "db", "운영 DB"

    class Status(models.TextChoices):
        OK = "ok"
        REVIEW = "review"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    account = models.TextField(blank=True)
    userid = models.TextField(blank=True)
    name = models.TextField(blank=True)
    company = models.TextField(blank=True)  # 회사명
    manager = models.TextField(blank=True)  # 담당자
    email = models.TextField(blank=True)
    phone = models.TextField(blank=True)

    account_key = models.CharField(max_length=320, db_index=True)
    display_name = models.TextField(blank=True)
    emails = models.JSONField(default=list)
    problems = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)

    source = models.CharField(max_length=10, choices=Source.choices)
    # 웹에서만 관리: a web row the company DB never replaces, even for an account its view lists (e.g. 기관A).
    web_only = models.BooleanField(default=False)
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, null=True, related_name="accounts")
    active = models.BooleanField(default=True)  # replaced rows stay, inactive, as history
    replaces = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, related_name="+")
    edited_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    SOURCE_COLUMNS = ("account", "userid", "name", "company", "manager", "email", "phone")

    def edit(self, user, web_only: bool | None = None, **values) -> "CustomerAccount":
        """A web edit never rewrites a row: the old one becomes inactive history, a new `web` row replaces it."""
        new = CustomerAccount(
            kind=self.kind, source=self.Source.WEB, replaces=self, edited_by=user,
            web_only=self.web_only if web_only is None else web_only,
            **{col: values.get(col, getattr(self, col)) for col in self.SOURCE_COLUMNS},
        )
        self.active = False
        self.save(update_fields=["active", "updated_at"])
        new.save()
        return new

    def refresh_derived(self):
        d = derive(self.account, self.name, self.company, self.email)
        self.account_key, self.display_name, self.emails = d.account_key, d.display_name, d.emails
        self.problems = "\n".join(d.problems)
        self.status = self.Status.REVIEW if d.problems else self.Status.OK

    def save(self, *args, **kwargs):
        if kwargs.get("update_fields") is None:
            self.refresh_derived()
        super().save(*args, **kwargs)


class OpsDbSync(models.Model):
    """One read of the operations DB view (customers/opsdb.py): when, whether it worked, what it changed."""

    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    ok = models.BooleanField(default=False)
    error = models.TextField(blank=True)  # the driver's words, never a value from the view
    rows = models.PositiveIntegerField(default=0)
    batch = models.ForeignKey(ImportBatch, on_delete=models.SET_NULL, null=True)  # set when the rows changed
    lifted = models.PositiveIntegerField(default=0)  # 안 쓰는 계정 this read gave new data (InternalAccount.lift_unused)

    KEEP = 200  # latest reads kept; older ones go on each sync

    @classmethod
    def last(cls):
        return cls.objects.order_by("-pk").first()


DB_UPDATED = "운영 DB 갱신"  # InternalAccount.removed_by when the company DB lifted an 안 쓰는 계정


class InternalAccount(models.Model):
    """KT Cloud accounts with no customer to tell: 예시클라우드's own (사내 계정), or accounts nobody uses any more
    (안 쓰는 계정, left without customer data on purpose). Their work is listed, but no customer notice is sent
    or checked. Chosen one by one: most customer accounts use the example.com domain too."""

    class Kind(models.TextChoices):  # the values are also the notifications' statuses
        INTERNAL = "internal", "사내 계정"
        UNUSED = "unused", "안 쓰는 계정"

    account_key = models.CharField(max_length=320, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.INTERNAL)
    note = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)  # a removed account stays, inactive, as history
    added_by = models.CharField(max_length=50, blank=True)  # name typed in the page (shared login)
    added_at = models.DateTimeField()
    removed_by = models.CharField(max_length=50, blank=True)
    removed_at = models.DateTimeField(null=True)

    @staticmethod
    def key(account: str) -> str:
        return account.strip().lower()

    @classmethod
    def keys(cls) -> set[str]:
        return set(cls.objects.filter(active=True).values_list("account_key", flat=True))

    @classmethod
    def lift_unused(cls, accounts, now) -> int:
        """안 쓰는 계정 the company DB has just given new or changed data are in use again (user, 2026-10-21).
        The caller brings notifications in line (notifications.service.apply_internal_lists)."""
        return cls.objects.filter(active=True, kind=cls.Kind.UNUSED, account_key__in=set(accounts)).update(
            active=False, removed_by=DB_UPDATED, removed_at=now)

    @classmethod
    def kinds(cls) -> dict[str, str]:
        """{account: "internal" | "unused"} of the active rows."""
        return dict(cls.objects.filter(active=True).values_list("account_key", "kind"))


class InternalCompany(models.Model):
    """A customer name (회사명, else 이름) all of whose accounts are 사내 계정 (user, 2026-10-21: 예시사내 and
    (주)예시사내시스템). Accounts the company DB or a CSV adds later under such a name become 사내 계정 at the
    next scan; one a person took off the list stays off."""

    name = models.CharField(max_length=200)
    key = models.CharField(max_length=200, unique=True)  # the name without spaces, case-folded
    active = models.BooleanField(default=True)
    added_by = models.CharField(max_length=50, blank=True)
    added_at = models.DateTimeField()
    removed_by = models.CharField(max_length=50, blank=True)
    removed_at = models.DateTimeField(null=True)

    @staticmethod
    def key_of(name: str) -> str:
        return re.sub(r"\s+", "", name or "").casefold()

    @classmethod
    def mark_accounts(cls, now) -> int:
        """사내 계정 rows for the active customers under an active name, but never for an account that ever had a
        row (a person removed it, or chose 안 쓰는 계정). Returns the rows made; the caller brings notifications
        in line (notifications.service.apply_internal_accounts)."""
        names = {c.key: c for c in cls.objects.filter(active=True)}
        if not names:
            return 0
        known = set(InternalAccount.objects.values_list("account_key", flat=True))
        made = 0
        for row in CustomerAccount.objects.filter(active=True).exclude(account_key=""):
            company = names.get(cls.key_of(row.display_name))
            if company is None or row.account_key in known:
                continue
            InternalAccount.objects.create(account_key=row.account_key, kind=InternalAccount.Kind.INTERNAL,
                                           note=f"고객명 {company.name}"[:200], added_by=company.added_by, added_at=now)
            known.add(row.account_key)
            made += 1
        return made


class PartnerChannel(models.Model):
    """Customers whose work notices go out under another company's name (e.g. a partner mails one public agency).

    Their notices get their own tab and need no master emails. Mail from `sender` whose subject mentions 작업 is the
    sent copy: it proves it was sent, as an 예시클라우드 copy does for the rest. With the mail fields filled in,
    the tab gets Outlook drafts in the partner's own wording (drafts.build_partner); addresses live here, in the
    DB only, never in the source."""

    label = models.CharField(max_length=40)  # tab name, e.g. the customer's short name
    accounts = models.TextField()  # KT Cloud accounts, one per line
    sender = models.CharField(max_length=320)  # the partner's sending address: 보내는 사람 of the draft
    mail_to = models.TextField(blank=True)  # 받는 사람, one per line or separated by ;
    mail_cc = models.TextField(blank=True)  # 참조
    mail_bcc = models.TextField(blank=True)  # 숨은참조 (e.g. the address whose copy reaches this site)
    subject_prefix = models.CharField(max_length=100, blank=True)  # e.g. "[회사] 고객명" before "kt cloud …"
    greeting = models.TextField(blank=True)  # first lines of the body, e.g. "안녕하세요.\n○○ 운영팀 입니다."
    # KT writes some servers under another name than the customer's list: "KT 이름 = 목록의 이름", one per line
    server_aliases = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.label

    @staticmethod
    def _addresses(text: str) -> list[str]:
        return [a.strip() for a in re.split(r"[;\n]", text) if a.strip()]

    @property
    def to_list(self) -> list[str]:
        return self._addresses(self.mail_to)

    @property
    def cc_list(self) -> list[str]:
        return self._addresses(self.mail_cc)

    @property
    def bcc_list(self) -> list[str]:
        return self._addresses(self.mail_bcc)

    @property
    def has_template(self) -> bool:
        return bool(self.to_list and self.greeting.strip())

    @property
    def account_keys(self) -> list[str]:
        return [a.strip().lower() for a in self.accounts.splitlines() if a.strip()]

    @classmethod
    def by_account(cls) -> dict[str, "PartnerChannel"]:
        return {a: ch for ch in cls.objects.filter(active=True) for a in ch.account_keys}

    @classmethod
    def senders(cls) -> frozenset[str]:
        return frozenset(s.strip().lower() for s in cls.objects.filter(active=True).values_list("sender", flat=True))

    def aliases(self) -> dict[str, str]:
        """{KT's server name: the customer's}, both as server_key."""
        pairs = (line.split("=", 1) for line in self.server_aliases.splitlines() if "=" in line)
        return {server_key(kt): server_key(ours) for kt, ours in pairs if server_key(kt) and server_key(ours)}

    def split(self, servers: list[str]) -> tuple[list["PartnerGroup"], list[str]]:
        """The groups whose servers a notice names, and the servers no group (with a 받는 사람) has.

        No groups at all means the channel does not split: ([], []). A notice naming no server, or any server
        outside the groups, goes to the whole 받는 사람 list instead (user, 2026-10-21)."""
        groups = [g for g in self.groups.filter(active=True).order_by("name", "pk") if g.to_list]
        if not groups:
            return [], []
        aliases = self.aliases()
        found, unknown = [], []
        for server in dict.fromkeys(servers):
            key = aliases.get(server_key(server), server_key(server))
            hits = [g for g in groups if key in g.server_keys]
            if not hits:
                unknown.append(server)
            found += [g for g in hits if g not in found]
        return sorted(found, key=groups.index), unknown


def server_key(name: str) -> str:
    """Server names compare without case, spaces, hyphens or underscores: KT writes "ecoaswas01" for "ecoas-was01"."""
    return re.sub(r"[\s_-]+", "", name).lower()


class PartnerGroup(models.Model):
    """One system of a 별도 발송 customer: its people get the notices naming its servers, one mail per group
    (user, 2026-10-21). A server used by several systems is listed in each. Addresses live in the DB only."""

    channel = models.ForeignKey(PartnerChannel, on_delete=models.PROTECT, related_name="groups")
    name = models.CharField(max_length=60)
    mail_to = models.TextField(blank=True)  # 받는 사람, one per line or separated by ;
    mail_cc = models.TextField(blank=True)  # 참조, on top of the channel's
    servers = models.TextField(blank=True)  # one per line, as the customer's list writes them
    active = models.BooleanField(default=True)  # a group notifications point to is retired, never deleted
    updated_by = models.CharField(max_length=50, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    @property
    def to_list(self) -> list[str]:
        return PartnerChannel._addresses(self.mail_to)

    @property
    def cc_list(self) -> list[str]:
        return PartnerChannel._addresses(self.mail_cc)

    @property
    def server_list(self) -> list[str]:
        return [s.strip() for s in self.servers.splitlines() if s.strip()]

    @property
    def server_keys(self) -> set[str]:
        return {server_key(s) for s in self.server_list}
