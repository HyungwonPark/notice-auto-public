"""What this site reads from KT Cloud's work-notice mails, editable in 환경 설정 > KT 공지 양식 (meeting request,
2026-10-20: when KT changes its template, staff change these instead of the code): KT's sender, the subject words
that make a work notice and decide its kind, the item labels of the body, and the footer lines a draft leaves
out. The defaults are the rules read from the real mail (2026-10-18/19). The parsers stay pure and take a KtForm;
the scan, the commands and the web pass current()."""
import re
from dataclasses import asdict, dataclass

# NoticeInfo fields that take a labelled line, in the order the settings page lists them.
FIELDS = (
    ("accounts", "대상 계정"), ("servers", "대상 서버"), ("lbs", "대상 LB"), ("masked_customer", "대상 AI-DX"),
    ("audience", "대상 고객"), ("target", "작업 대상"), ("title", "작업 내용"), ("detail", "상세 내용"),
    ("schedule", "작업 일시"), ("impact", "작업 영향"),
)
REQUIRED = ("accounts", "title", "schedule")  # without these no customer notice can be made
WORD_LISTS = ("work", "complete", "cancel", "postpone", "urgent", "skip", "incident", "recovery", "check")
MAX_LABEL = 20  # notices._split_line ignores longer labels (they are sentences, not items)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass(frozen=True)
class KtForm:
    sender: str = "cloudmaster@ktcloud.com"
    work: tuple[str, ...] = ("작업",)  # a KT subject must have one of these to be a work notice
    complete: tuple[str, ...] = ("완료",)
    cancel: tuple[str, ...] = ("취소", "변경")
    # '[kt cloud 작업 연기 공지]' (2026-10-25): sent to customers like a notice, even with no new date
    postpone: tuple[str, ...] = ("연기",)
    urgent: tuple[str, ...] = ("긴급",)
    # KT mail with 작업 in the subject that is no work notice: '수작업 요청' inquiry receipts (user, 2026-10-19)
    skip: tuple[str, ...] = ("수작업 요청", "고객문의가 접수")
    # KT's outage notices: '[kt cloud 컴퓨팅 노드 Down 발생 공지]', then '… Down 복구 공지' (user, 2026-10-22).
    # Not 복구 alone: KT also mails '스냅샷 복구' and the like.
    incident: tuple[str, ...] = ("Down 발생",)
    recovery: tuple[str, ...] = ("Down 복구",)
    # '[KT cloud 서비스 점검 요청 공지]' (2026-10-27): KT asks the customer to check a service after an event on
    # it (a virtual router, …). Needs no 작업 in the subject; sent at once like an outage notice (user).
    check: tuple[str, ...] = ("점검 요청",)
    labels: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("accounts", ("대상계정명", "대상계정")),  # 예시클라우드 staff sometimes drop the 명
        ("servers", ("대상서버명",)),
        ("lbs", ("대상LB명",)),
        ("masked_customer", ("대상AI-DX명",)),
        ("audience", ("대상고객",)),
        ("target", ("작업대상",)),
        ("title", ("작업내용",)),
        ("detail", ("상세내용",)),
        ("schedule", ("작업일시",)),
        ("impact", ("작업영향",)),
    )
    footer: tuple[str, ...] = ("☎ 고객센터", "* 본 메일")  # a quoted line starting so is dropped (spaces ignored)

    def labels_of(self, name: str) -> tuple[str, ...]:
        return dict(self.labels).get(name, ())

    def values(self) -> dict:
        data = asdict(self)
        data["labels"] = {name: list(texts) for name, texts in self.labels}
        return data


DEFAULT = KtForm()


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text)


def from_values(data: dict) -> KtForm:
    """A stored form; keys it lacks keep their default (a later field added to KtForm)."""
    values = {}
    if data.get("sender"):
        values["sender"] = str(data["sender"]).strip().lower()
    for key in (*WORD_LISTS, "footer"):
        if key in data:
            values[key] = tuple(str(w) for w in data[key])
    if "labels" in data:
        stored = data["labels"]
        values["labels"] = tuple((name, tuple(stored.get(name, DEFAULT.labels_of(name)))) for name, _ in FIELDS)
    return KtForm(**values)


def split_words(text: str) -> tuple[str, ...]:
    """'완료, 종료' -> ('완료', '종료'): comma or line separated, blanks dropped, order and spaces inside kept."""
    words = []
    for word in re.split(r"[,\n]", text or ""):
        word = " ".join(word.split())
        if word and word not in words:
            words.append(word)
    return tuple(words)


def from_post(post) -> tuple[KtForm, list[str]]:
    """The settings form's fields -> (form, problems). Problems are for the page; the form is used only if none."""
    problems = []
    sender = (post.get("sender") or "").strip().lower()
    if not _EMAIL.fullmatch(sender):
        problems.append("KT 보내는 주소가 메일 주소가 아닙니다.")
    words = {key: split_words(post.get(key, "")) for key in WORD_LISTS}
    if not words["work"]:
        problems.append("작업 공지로 볼 제목 단어가 없습니다.")
    labels, owner = [], {}
    for name, title in FIELDS:
        texts = split_words(post.get(f"label_{name}", ""))
        if name in REQUIRED and not texts:
            problems.append(f"{title} 항목 이름이 없습니다.")
        for text in texts:
            key = squeeze(text).lower()
            if len(key) > MAX_LABEL:
                problems.append(f"'{text}'는 {MAX_LABEL}자(공백 제외)보다 깁니다.")
            elif key in owner and owner[key] != title:
                problems.append(f"'{text}'가 {owner[key]}와 {title}에 함께 있습니다.")
            owner.setdefault(key, title)
        labels.append((name, texts))
    footer = tuple(line.strip() for line in (post.get("footer") or "").splitlines() if line.strip())
    form = KtForm(sender=sender, **words, labels=tuple(labels), footer=footer)
    return form, problems


def current() -> KtForm:
    from .models import KtFormSetting

    row = KtFormSetting.objects.order_by("-pk").first()
    return from_values(row.values) if row else DEFAULT


def save(form: KtForm, by: str) -> None:
    from .models import KtFormSetting

    KtFormSetting.objects.create(values=form.values(), by=by)
