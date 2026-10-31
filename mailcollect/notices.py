"""Parse notice bodies (KT originals and 예시클라우드 copies, which quote them) into fields.
Pure functions, no I/O. Templates were read from 14 days of real mail (2026-10-18, labels and value
shapes only): "○ 대상계정명 : …", "ㅇ 작업일시 : …", "– 상세내용 : …", "1. 작업 내용 : …"."""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .ktform import DEFAULT, KtForm

KST = ZoneInfo("Asia/Seoul")
LIST_FIELDS = {"accounts", "servers", "lbs"}

_BULLET = re.compile(r"^(?:[○ㅇ●◦•·\-–—*]|\d{1,2}[.)])\s*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_DATE = r"(?P<{p}y>\d{{4}}|\d{{2}})\s*[.\-/]\s*(?P<{p}m>\d{{1,2}})\s*[.\-/]\s*(?P<{p}d>\d{{1,2}})\.?\s*(?:\([^)]*\))?"
_WINDOW = re.compile(
    _DATE.format(p="s") + r"\s*(?P<sh>\d{1,2}):(?P<smin>\d{2})\s*~\s*(?P<next>익일\s*)?"
    r"(?:" + _DATE.format(p="e") + r"\s*)?(?P<eh>\d{1,2}):(?P<emin>\d{2})"
)
_HOURS_NOTE = re.compile(r"\(\s*(\d{1,2})\s*시간\s*\)")


@dataclass
class NoticeInfo:
    accounts: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    lbs: list[str] = field(default_factory=list)
    title: str = ""
    detail: str = ""
    schedule: str = ""  # raw text, never rewritten
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    impact: str = ""
    audience: str = ""
    target: str = ""
    masked_customer: str = ""
    extra: dict[str, str] = field(default_factory=dict)  # bullet-led labels we do not know yet
    problems: list[str] = field(default_factory=list)  # generic descriptions only, never values


def label_key(label: str) -> str:
    # Strip the bullet before NFKC: NFKC turns the "ㅇ" bullet (compatibility jamo) into another code point.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", _BULLET.sub("", label.strip()))).lower()


def _split_line(line: str) -> tuple[str, str, bool] | None:
    """(label key, value, bullet-led) for "label : value" or "label<TAB>value" lines."""
    if "\t" in line:
        label, _, value = line.partition("\t")
        value = " | ".join(cell.strip() for cell in value.split("\t") if cell.strip())
    else:
        label, sep, value = line.partition(":")
        if not sep:
            return None
    key = label_key(label)
    if not key or len(key) > 20:
        return None
    return key, value.strip(), bool(_BULLET.match(label.strip()))


@lru_cache(maxsize=8)
def label_map(form: KtForm) -> dict[str, str]:
    """Label with bullets and spaces removed, lower-cased -> field name (the form's item labels)."""
    return {label_key(text): name for name, texts in form.labels for text in texts}


def parse_notice(text: str, form: KtForm = DEFAULT) -> NoticeInfo:
    info = NoticeInfo()
    labels = label_map(form)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pending_accounts = False  # "대상 계정명 :" with the address on the next line (change/cancel notices)
    for line in lines:
        split = _split_line(line)
        name = labels.get(split[0]) if split else None
        if name:
            if split[1]:
                _assign(info, name, split[1])
            pending_accounts = name == "accounts" and not split[1]
            # Any other empty label stays empty: the next line is often the notice's own title.
            continue
        if pending_accounts and not split and _EMAIL.search(line):
            _assign(info, "accounts", line)
        elif split and split[2]:
            info.extra.setdefault(split[0], split[1])
        pending_accounts = False

    if info.schedule:
        info.windows, problems = parse_schedule(info.schedule)
        info.problems += problems
    else:
        info.problems.append("no 작업일시")
    if not info.windows and "변경전" in info.extra:
        # Change/cancel notices write the original window under 변경전; use it to find the work,
        # but keep the problem above so the notice still goes to a person.
        info.windows = parse_schedule(info.extra["변경전"])[0]
    if not info.accounts:
        info.problems.append("no 대상계정명")
    if not info.title:
        info.problems.append("no 작업내용")
    return info


def _assign(info: NoticeInfo, name: str, value: str):
    if name == "accounts":
        found = [a.lower() for a in _EMAIL.findall(value)]
        if not found:
            info.problems.append("대상계정명 without an email address")
        info.accounts += [a for a in found if a not in info.accounts]
    elif name in LIST_FIELDS:
        getattr(info, name).extend(v.strip() for v in re.split(r"[,/]", value) if v.strip())
    elif not getattr(info, name):
        setattr(info, name, value)
    elif getattr(info, name) != value:
        info.problems.append(f"conflicting {name}")


def parse_schedule(raw: str) -> tuple[list[tuple[datetime, datetime]], list[str]]:
    """Work windows in KST. A missing end date means the start date, or the next day ("익일", or an
    end time not after the start time). Anything that does not fit is reported, not guessed."""
    windows, problems = [], []
    for m in _WINDOW.finditer(raw):
        try:
            start_day = _day(m, "s")
            start = _at(start_day, int(m["sh"]), int(m["smin"]))
            if m["ey"]:
                end = _at(_day(m, "e"), int(m["eh"]), int(m["emin"]))
            else:
                end = _at(start_day, int(m["eh"]), int(m["emin"]))
                if end == start and not m["next"]:
                    problems.append("작업일시 start and end times are equal")
                if m["next"] or end <= start:
                    end += timedelta(days=1)
        except ValueError:
            problems.append("invalid date or time in 작업일시")
            continue
        if end <= start:
            problems.append("작업일시 ends before it starts")
            continue
        windows.append((start, end))
    if not windows and not problems:
        problems.append("unparsed 작업일시")
    note = _HOURS_NOTE.search(raw)
    if note and len(windows) == 1:
        start, end = windows[0]
        if abs((end - start) - timedelta(hours=int(note[1]))) > timedelta(minutes=30):
            problems.append("작업일시 duration note disagrees with the times")
    return windows, problems


def _day(m: re.Match, p: str) -> date:
    year = int(m[f"{p}y"])
    return date(year + 2000 if year < 100 else year, int(m[f"{p}m"]), int(m[f"{p}d"]))


def _at(day: date, hour: int, minute: int) -> datetime:
    if hour == 24 and minute == 0:  # "24:00" = midnight at the end of the day
        return datetime.combine(day + timedelta(days=1), time(0, 0), KST)
    return datetime.combine(day, time(hour, minute), KST)


def work_title(title: str) -> str:
    """Normalised work title used to group the same work announced to several accounts."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", title)).strip()
