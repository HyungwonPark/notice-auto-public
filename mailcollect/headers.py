"""Parse the header lines returned by POP3 TOP and classify the message. Pure functions, no I/O."""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email.errors import HeaderParseError
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime

from .ktform import DEFAULT, KtForm

KT_NOTICE = "kt_notice"
KT_URGENT = "kt_urgent"
KT_COMPLETE = "kt_complete"
KT_CANCEL = "kt_cancel"
KT_INCIDENT = "kt_incident"  # KT outage notice ('컴퓨팅 노드 Down 발생 공지'): customers are told at once
KT_RECOVERY = "kt_recovery"  # KT's notice that the outage is over ('… Down 복구 공지')
KT_CHECK = "kt_check"  # '[KT cloud 서비스 점검 요청 공지]': an event on the customer's resource, told at once
KT_POSTPONE = "kt_postpone"  # '[kt cloud 작업 연기 공지]': a work put off, often with no new date yet
KT_OTHER = "kt_other"  # KT sender, not a work notice (snapshots, inquiries, …): counted, header not kept
NC24_NOTICE = "nc24_notice"  # in-house copy of a 예시클라우드 work notice sent to a customer
NC24_COMPLETE = "nc24_complete"  # in-house copy of a 예시클라우드 work-completion notice
NC24_POSTPONE = "nc24_postpone"  # in-house copy of a 예시클라우드 postponement notice
PARTNER_NOTICE = "partner_notice"  # a partner's mail of a work notice to a customer it serves (PartnerChannel)
PARTNER_COMPLETE = "partner_complete"  # the same for a completion notice
SUSPECT = "suspect"  # confirmed subject from an unexpected sender: kept for review
OTHER = "other"  # everything else: only the UIDL is recorded
# A mail of a kept category whose Message-ID was already collected (another mailbox, a forward of the same
# mail, or one delivered twice): only the UIDL is recorded, so nothing is read or sent twice.
DUPLICATE = "duplicate"
CATEGORIES = (
    KT_NOTICE, KT_URGENT, KT_COMPLETE, KT_CANCEL, KT_INCIDENT, KT_RECOVERY, KT_CHECK, KT_POSTPONE, KT_OTHER, NC24_NOTICE,
    NC24_COMPLETE, NC24_POSTPONE, PARTNER_NOTICE, PARTNER_COMPLETE, SUSPECT, OTHER, DUPLICATE,
)
MATCHED = frozenset({KT_NOTICE, KT_URGENT, KT_COMPLETE, KT_CANCEL, KT_INCIDENT, KT_RECOVERY, KT_CHECK,
                     KT_POSTPONE})  # KT originals
# Notices about a customer's resource, not a planned work: titled by KT's subject, timed by the mail, sent at once.
# A 서비스 점검 요청 has no follow-up mail, so its work has one stage only.
INCIDENTS = frozenset({KT_INCIDENT, KT_RECOVERY, KT_CHECK})
BODY = MATCHED | {NC24_NOTICE, NC24_COMPLETE, NC24_POSTPONE, PARTNER_NOTICE, PARTNER_COMPLETE}  # full message retrieved
KEPT = BODY | {SUSPECT}  # categories whose header is stored


def restrict(category: str, collect: str) -> str:
    """A mailbox set to collect only KT work notices keeps nothing else (no copies, no suspects)."""
    if collect == "kt" and category not in MATCHED and category != KT_OTHER:
        return OTHER
    return category

# Subject rules. KT: user decision 2026-10-18 — every mail from cloudmaster@ktcloud.com whose subject
# contains "작업" is kept (header and body), after KT used a subject outside the earlier exact list.
# 완료 → completion, 취소/변경 → change/cancel, 연기 → postponement, 긴급 → urgent, otherwise a work notice. These KT words and the
# sender are the defaults of ktform.KtForm (환경 설정 > KT 공지 양식). 예시클라우드 copies: every one seen came
# from system@example.com with the first two subjects below (exact); the third is this site's postponement.
KT_SENDER = DEFAULT.sender
NC24_SENDER = "system@example.com"
NC24_PREFIXES = (
    ("[예시클라우드] 작업 완료 공지 안내", NC24_COMPLETE),
    ("[예시클라우드] 작업 공지 안내", NC24_NOTICE),
    ("[예시클라우드] 작업 연기 공지 안내", NC24_POSTPONE),
)

_ANGLE_ADDR = re.compile(r"<([^<>\s@]+@[^<>\s@]+)>")

# Korean mail often labels CP949 text as EUC-KR or ks_c_5601-1987. EUC-KR cannot decode the
# extended Hangul syllables, so every one of these labels is decoded as CP949.
_CP949_LABELS = frozenset(
    {"ks_c_5601-1987", "ks_c_5601", "ksc5601", "ksc_5601", "euc-kr", "euc_kr",
     "cp949", "windows-949", "x-windows-949", "uhc"}
)


@dataclass(frozen=True)
class HeaderInfo:
    subject: str
    subject_ok: bool  # one Subject header, decoded without guessing; required for an exact match
    from_addr: str  # lower-cased addr-spec; "" unless the header holds exactly one address
    from_display: str
    date_raw: str
    date: datetime | None
    message_id: str
    errors: tuple[str, ...]


def split_headers(lines: list[bytes]) -> dict[str, list[bytes]]:
    """Unfold header lines (RFC 5322 2.2.3) into {lower-case name: [raw values]}."""
    headers: dict[str, list[bytes]] = {}
    values: list[bytes] | None = None
    for line in lines:
        if not line:
            break  # blank line ends the header section
        if line[:1] in (b" ", b"\t"):
            if values is not None:
                values[-1] += line
            continue
        name, sep, value = line.partition(b":")
        if not sep:
            values = None
            continue
        values = headers.setdefault(name.strip().lower().decode("ascii", "replace"), [])
        values.append(value.lstrip(b" \t"))
    return headers


def decode_value(raw: bytes) -> tuple[str, bool]:
    """Decode RFC 2047 encoded-words, or a raw 8-bit UTF-8/CP949 value. ok is False if text was lost."""
    raw = raw.strip()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        for codec in ("utf-8", "cp949"):
            try:
                return raw.decode(codec), True
            except UnicodeDecodeError:
                pass
        return raw.decode("utf-8", "replace"), False
    try:
        chunks = decode_header(text)
    except HeaderParseError:  # broken base64
        return text, False
    ok = True
    parts = []
    for chunk, charset in chunks:
        if isinstance(chunk, str):
            parts.append(chunk)
        elif charset is None:
            parts.append(chunk.decode("ascii", "replace"))
        else:
            try:
                parts.append(chunk.decode(codec_for(charset)))
            except (LookupError, ValueError):  # unknown label or bytes invalid for it
                parts.append(chunk.decode("utf-8", "replace"))
                ok = False
    return "".join(parts), ok


def codec_for(charset: str) -> str:
    """Python codec for a MIME charset label."""
    label = charset.lower().split("*", 1)[0]  # drop an RFC 2231 language tag
    return "cp949" if label in _CP949_LABELS else label


def parse_headers(lines: list[bytes]) -> HeaderInfo:
    headers = split_headers(lines)
    errors: list[str] = []

    def first(name: str) -> tuple[str, bool]:
        values = headers.get(name)
        if not values:
            return "", True
        if len(values) > 1:
            errors.append(f"multiple {name} headers")
        text, ok = decode_value(values[0])
        if not ok:
            errors.append(f"undecodable {name}")
        return text, ok and len(values) == 1

    subject, subject_ok = first("subject")
    sender, _ = first("from")
    addresses = [pair for pair in getaddresses([sender]) if pair[1]]
    if len(addresses) != 1:
        # Python's strict parser rejects some real headers, e.g. a comma inside a "(comment)" in
        # the display name. A single <angle-addr> is still the unambiguous sender address.
        angle = _ANGLE_ADDR.findall(sender)
        if len(angle) == 1:
            addresses = [(sender.split("<", 1)[0].strip(), angle[0])]
    if len(headers.get("from", ())) == 1 and len(addresses) == 1:
        from_display, from_addr = addresses[0][0], addresses[0][1].strip().lower()
    else:
        from_display, from_addr = sender, ""
        if sender:
            errors.append("sender is not exactly one address")

    date_raw, _ = first("date")
    date = None
    if date_raw:
        try:
            date = parsedate_to_datetime(date_raw)
        except (TypeError, ValueError):
            errors.append("unparsable date")
        else:
            if date.tzinfo is None:  # "-0000" means unknown zone
                date = date.replace(tzinfo=timezone.utc)

    message_id, _ = first("message-id")
    return HeaderInfo(subject, subject_ok, from_addr, from_display, date_raw, date, message_id, tuple(errors))


_ANY_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def recipients(lines: list[bytes]) -> list[str]:
    """Lower-cased addresses in To and Cc (a Bcc copy shows the customers here, not the Bcc)."""
    found: list[str] = []
    fields = split_headers(lines)
    for name in ("to", "cc"):
        for raw in fields.get(name, []):
            for addr in _ANY_ADDR.findall(decode_value(raw)[0]):
                if addr.lower() not in found:
                    found.append(addr.lower())
    return found


def _loose(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).casefold()


def kt_work_kind(subject: str, form: KtForm = DEFAULT) -> str | None:
    """KT category for a subject with one of the form's work words, or outage words, and none of its skip words
    (KT mail that mentions 작업 but is no work notice, e.g. '수작업 요청' receipts: user decision 2026-10-19),
    else None."""
    def has(words):
        return any(word in subject for word in words)

    if has(form.skip):
        return None
    if has(form.recovery):
        return KT_RECOVERY
    if has(form.incident):
        return KT_INCIDENT
    if has(form.check):
        return KT_CHECK
    if not has(form.work):
        return None
    if has(form.complete):
        return KT_COMPLETE
    if has(form.cancel):
        return KT_CANCEL
    if has(form.postpone):
        return KT_POSTPONE
    return KT_URGENT if has(form.urgent) else KT_NOTICE


def classify(info: HeaderInfo, partners: frozenset[str] = frozenset(), form: KtForm = DEFAULT) -> str:
    """partners: sending addresses of PartnerChannel rows (mail they send for customers they serve)."""
    from_kt = info.from_addr == form.sender
    subject = info.subject.lstrip(" \t")
    if info.subject_ok:
        kind = kt_work_kind(subject, form) if from_kt else None
        if kind:
            return kind
        if info.from_addr in partners and "작업" in subject:
            return PARTNER_COMPLETE if "완료" in subject else PARTNER_NOTICE
        for prefix, category in NC24_PREFIXES:
            if subject.startswith(prefix):
                return category if info.from_addr == NC24_SENDER else SUSPECT
    if from_kt:
        return KT_OTHER
    loose = _loose(info.subject)
    if "kt cloud" in loose and "작업" in loose:  # forwarded, replied or spoofed KT notice
        return SUSPECT
    return OTHER
