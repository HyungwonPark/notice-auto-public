"""Extract readable text from a retrieved message. The raw bytes are stored separately and never altered."""
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

from .headers import codec_for


@dataclass(frozen=True)
class BodyInfo:
    text: str  # text/plain part, or text derived from the HTML part
    html: str
    errors: tuple[str, ...]


def extract(raw: bytes) -> BodyInfo:
    errors: list[str] = []
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        text = _decode(msg.get_body(preferencelist=("plain",)), errors)
        html = _decode(msg.get_body(preferencelist=("html",)), errors)
    except Exception as exc:  # external input: record any parser failure, keep the raw message
        return BodyInfo("", "", (f"MIME parse failed: {type(exc).__name__}",))
    if not text and html:
        text = html_to_text(html)
    return BodyInfo(text, html, tuple(errors))


def _decode(part, errors: list[str]) -> str:
    if part is None:
        return ""
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "us-ascii"
    try:
        return payload.decode(codec_for(charset))
    except (LookupError, ValueError):
        pass
    for codec in ("utf-8", "cp949"):  # mislabelled parts are common in Korean mail
        try:
            text = payload.decode(codec)
        except UnicodeDecodeError:
            continue
        errors.append(f"{part.get_content_type()} labelled {charset} decoded as {codec}")
        return text
    errors.append(f"undecodable {part.get_content_type()} part ({charset})")
    return payload.decode("utf-8", "replace")


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("\t")  # keep table cells apart for later field parsing

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (re.sub(r"[  ]+", " ", line).strip(" \t") for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
