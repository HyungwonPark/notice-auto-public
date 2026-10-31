"""Build synthetic raw messages. Only made-up addresses: no real customer data in tests."""
import base64
import itertools
from email import quoprimime

KT = "kt cloud <cloudmaster@ktcloud.com>"
NOTICE = "[kt cloud 작업 공지] 네트워크 개선 작업 안내"
URGENT = "[kt cloud 긴급 작업 공지] 스토리지 긴급 점검"

_ids = itertools.count(1)


def encoded_word(text: str, label: str = "utf-8", codec: str | None = None, enc: str = "b") -> str:
    data = text.encode(codec or label)
    if enc == "b":
        return f"=?{label}?B?{base64.b64encode(data).decode()}?="
    return quoprimime.header_encode(data, charset=label)


def raw_message(header_lines: list[bytes], body: str = "본문입니다.") -> bytes:
    return b"\r\n".join(header_lines) + b"\r\n\r\n" + body.encode("utf-8") + b"\r\n"


def message(subject: str | bytes, sender: str = KT) -> bytes:
    """subject is the raw header text: str (ASCII, may hold encoded-words) or bytes (raw 8-bit)."""
    subject_raw = subject.encode("ascii") if isinstance(subject, str) else subject
    return raw_message([
        b"Return-Path: <bounce@example.invalid>",
        b"Received: from mx.example.invalid by mail.example.invalid;",
        b"\tSun, 18 Oct 2026 10:00:00 +0900",
        b"From: " + sender.encode("utf-8"),
        b"To: ops@example.invalid",
        b"Subject: " + subject_raw,
        b"Date: Sun, 18 Oct 2026 10:00:00 +0900",
        f"Message-ID: <{next(_ids)}@example.invalid>".encode(),
        b"MIME-Version: 1.0",
        b'Content-Type: text/plain; charset="utf-8"',
    ])


def top_lines(raw: bytes) -> list[bytes]:
    """What poplib returns for TOP n 0: header lines and the blank separator."""
    return raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n") + [b""]
