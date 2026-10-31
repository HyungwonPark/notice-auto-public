"""Customer notice drafts. The wording follows the 79 real 예시클라우드 copies (2026-10-18); the KT notice is
quoted unchanged except for KT's own footer, which staff always remove. Outlook's header banner and the
customer-centre signature come from the staff's Outlook template, so they are not repeated here."""
import html
import re

from mailcollect.ktform import DEFAULT, squeeze

POSTPONE = "postpone"  # wording of a 작업 연기 공지 (a notice-kind round with its own subject and opening)
SUBJECTS = {
    "notice": "[예시클라우드] 작업 공지 안내",
    "complete": "[예시클라우드] 작업 완료 공지 안내",
    POSTPONE: "[예시클라우드] 작업 연기 공지 안내",  # headers.NC24_PREFIXES knows it, so its copy is kept
}
# Opening line names the work by its title (user decision 2026-10-18); without a title it stays generic.
OPENINGS = {
    "notice": "KT Cloud에서 아래와 같이 작업을 진행할 예정입니다.",
    "complete": "KT Cloud에서 진행한 아래 작업이 완료되었습니다.",
    POSTPONE: "KT Cloud에서 아래 작업을 연기했습니다.",
}


def opening(kind: str, title: str) -> str:
    """kt cloud 측으로부터 <title> 공지가 / <title> 완료 공지가 / <title> 연기 공지가 수신되어 안내드립니다."""
    title = " ".join(title.split()).rstrip(" .")
    if not title:
        return OPENINGS[kind]
    what = ("연기 공지" if kind == POSTPONE else "공지" if kind == "notice" or title.endswith("완료")
            else "완료 공지")
    return f"kt cloud 측으로부터 {title} {what}가 수신되어 안내드립니다."
SEPARATOR = "-" * 83  # as in the real copies
NUMBER_RE = re.compile(r"안내번호\s*:\s*(CSP-\d{6}-\d+)")


def kt_quote(kt_text: str, footer: tuple[str, ...] = DEFAULT.footer) -> str:
    """KT's text without its footer lines (KtForm.footer: lines starting so, spaces ignored). KT's 작업 연기 공지
    of 2026-10-25 wrote its ☎ as the chat code ':phone:' (pasted from a messenger); it counts as ☎."""
    starts = tuple(squeeze(f) for f in footer if f.strip())
    lines = [ln.rstrip() for ln in kt_text.splitlines()]
    kept = [ln for ln in lines if not (starts and squeeze(ln.replace(":phone:", "☎")).startswith(starts))]
    while kept and not kept[-1].strip():
        kept.pop()
    while kept and not kept[0].strip():
        kept.pop(0)
    return "\n".join(kept)


# KT's item lines (○ 대상계정명, ㅇ 작업일시, – 상세내용, ※ 단, 1. …) and its bracketed notice title.
_ITEM = re.compile(r"^\s*(?:[○ㅇ●■□◆◇▶▷※*·•\-–—]|o\s|\d+[.)]\s)")
_TITLE = re.compile(r"^\s*\[.*\]\s*$")


def spaced(text: str) -> str:
    """KT's text arrives with no blank lines. Lay it out as staff do by hand (user, 2026-10-19): a blank line
    between sentences, while item lines and the bracketed title right after them stay together as one block."""
    out, previous = [], None
    for line in text.splitlines():
        if not line.strip():
            continue
        kind = "item" if _ITEM.match(line) or _TITLE.match(line) else "sentence"
        if out and not (kind == previous == "item"):
            out.append("")
        out.append(line)
        previous = kind
    return "\n".join(out)


def build(kind: str, kt_text: str, title: str = "", footer: tuple[str, ...] = DEFAULT.footer) -> tuple[str, str]:
    """Subject and body. The 안내번호 is not in the body (user decision 2026-10-19): copies are matched by
    account, kind and work window; NUMBER_RE still reads it from older copies that carry it."""
    quote = kt_quote(kt_text, footer)
    lines = [
        "안녕하세요. 예시클라우드입니다.", "",
        opening(kind, title), "",
        "자세한 사항은 하기 내용 참고 부탁드리겠습니다.", "",
        SEPARATOR, "",
        spaced(quote),
    ]
    if not quote.rstrip().endswith("감사합니다."):
        lines += ["", "감사합니다."]
    return SUBJECTS[kind], "\n".join(lines)


def build_incident(kt_text: str, title: str, footer: tuple[str, ...] = DEFAULT.footer) -> tuple[str, str]:
    """A KT outage notice under KT's own title (user, 2026-10-22): '[예시클라우드] 컴퓨팅 노드 Down 발생 안내'."""
    title = " ".join(title.split()).rstrip(" .")
    quote = kt_quote(kt_text, footer)
    lines = [
        "안녕하세요. 예시클라우드입니다.", "",
        f"kt cloud 측으로부터 {title} 공지가 수신되어 안내드립니다.", "",
        "자세한 사항은 하기 내용 참고 부탁드리겠습니다.", "",
        SEPARATOR, "",
        spaced(quote),
    ]
    if not quote.rstrip().endswith("감사합니다."):
        lines += ["", "감사합니다."]
    return f"[예시클라우드] {title} 안내", "\n".join(lines)


def number_in(text: str) -> str | None:
    m = NUMBER_RE.search(text)
    return m[1] if m else None


# --- 별도 발송: drafts in a partner's own wording (the partner's mails collected 2026-10-07..19) ----------------

_QUOTED_NAME = re.compile(r"공지\s*드렸던\s*['‘]([^'’]{4,80})['’]")
WEEKDAYS = "월화수목금토일"


def _subject_particle(word: str) -> str:
    """이 after a final consonant, 가 after a vowel (Hangul); 이 otherwise."""
    last = word.strip()[-1:]
    if "가" <= last <= "힣":
        return "이" if (ord(last) - 0xAC00) % 28 else "가"
    return "이"


def window(starts_at, ends_at) -> str:
    """2026.10.19(월) 00:00 ~ 06:00, as the partner writes it (end date added when it differs)."""
    start = f"{starts_at:%Y.%m.%d}({WEEKDAYS[starts_at.weekday()]}) {starts_at:%H:%M}"
    if ends_at is None:
        return start
    if ends_at.date() == starts_at.date():
        return f"{start} ~ {ends_at:%H:%M}"
    return f"{start} ~ {ends_at:%m.%d}({WEEKDAYS[ends_at.weekday()]}) {ends_at:%H:%M}"


def partner_name(kt_text: str, title: str) -> str:
    """The work's name: KT's own quoted name in a completion notice ('…'이 완료), else the 작업내용 title."""
    m = _QUOTED_NAME.search(kt_text)
    return " ".join((m[1] if m else title).split()) or "KT Cloud 작업"


def build_partner(kind: str, kt_text: str, title: str, round_: int, when: str, greeting: str,
                  prefix: str, footer: tuple[str, ...] = DEFAULT.footer) -> tuple[str, str]:
    """Subject and body the way the partner writes them: its greeting, one sentence naming the work, the KT text.
    A later 사전 공지 round is a 재안내; a 작업 연기 공지 names the work it puts off."""
    name = partner_name(kt_text, title)
    again = kind == "notice" and round_ > 1
    if kind == POSTPONE:
        opening_line = f"kt cloud로부터 {name} 연기 공지가 수신되어 안내드립니다."
        what = "연기 공지 안내"
    elif kind == "notice":
        opening_line = (f"kt cloud로부터 {when} 일정 내 {name}{_subject_particle(name)} 예정되어 "
                        f"{'재안내' if again else '안내'}드립니다.")
        what = "공지 재안내" if again else "공지 안내"
    else:
        opening_line = f"kt cloud로부터 금일 예정되어 있던 {name} 완료 공지가 수신되어 안내드립니다."
        what = "완료 공지 안내"
    # The partner's own 감사합니다 follows the KT text (which usually ends with KT's): in its mails the KT text
    # sits in a box and theirs comes after it (see partner_html).
    lines = [*[ln.rstrip() for ln in greeting.strip().splitlines()], "",
             opening_line, "",
             DETAILS, "",
             spaced(kt_quote(kt_text, footer)), "",
             "감사합니다."]
    subject = " ".join(part for part in (prefix.strip(), "kt cloud", name, what) if part)
    return subject, "\n".join(lines)


DETAILS = "자세한 사항은 하기 내용 참고 부탁드리겠습니다."
# The box the partner's mail editor puts around the KT text (its 20 mails, 2026-10): one cell, 960px, #ccc rule.
_BOX = ('<table cellpadding="0" style="border-collapse:collapse;width:960px;font-family:\'Malgun Gothic\';'
        'font-size:14px"><tr><td style="word-break:break-all;background-color:#ffffff;width:950px;'
        'border:1px solid #cccccc;padding:3px 4px 2px">')


def partner_html(body: str) -> str | None:
    """The partner draft as HTML for pasting into a mail editor: its lines, then the KT text in the box, then
    its closing line. None when the body is not laid out by build_partner (e.g. staff-held old text)."""
    lines = body.split("\n")
    if DETAILS not in lines or lines[-1] != "감사합니다.":
        return None
    cut = lines.index(DETAILS) + 1
    head, boxed, tail = lines[:cut], lines[cut:-1], lines[-1:]
    while boxed and not boxed[0].strip():
        boxed.pop(0)
    while boxed and not boxed[-1].strip():
        boxed.pop()

    def divs(rows):
        return "".join(f"<div>{html.escape(r)}</div>" if r.strip() else "<div><br></div>" for r in rows)

    return (f'<div style="font-family:\'Malgun Gothic\';font-size:14px">{divs(head)}<div><br></div>{_BOX}'
            f'{divs(boxed)}</td></tr></table><div><br></div>{divs(tail)}</div>')
