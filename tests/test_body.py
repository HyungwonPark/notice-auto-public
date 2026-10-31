import base64

from mailcollect.body import extract, html_to_text

KT_HTML = (
    "<html><head><style>td {color: red}</style></head><body>"
    "<p>안녕하세요. kt cloud입니다.</p>"
    "<table><tr><td>대상계정명</td><td>gov_xxxx@example.invalid</td></tr>"
    "<tr><td>작업일시</td><td>2026-10-24 22:00 ~ 10:00</td></tr></table>"
    "<p>문의:&nbsp;고객센터&amp;담당자</p></body></html>"
)


def mime(parts: list[tuple[str, str, bytes, str]], boundary="b1") -> bytes:
    """parts: (content type, charset label, payload bytes, transfer encoding)."""
    out = [b"From: x@example.invalid", b"Subject: t", b"MIME-Version: 1.0",
           f'Content-Type: multipart/alternative; boundary="{boundary}"'.encode(), b""]
    for ctype, charset, payload, cte in parts:
        body = base64.encodebytes(payload) if cte == "base64" else payload
        out += [f"--{boundary}".encode(), f'Content-Type: {ctype}; charset="{charset}"'.encode(),
                f"Content-Transfer-Encoding: {cte}".encode(), b"", body.rstrip(b"\n")]
    out += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(out)


def single(ctype: str, charset: str, payload: bytes, cte: str = "base64") -> bytes:
    body = base64.encodebytes(payload) if cte == "base64" else payload
    return b"\r\n".join([
        b"From: x@example.invalid", b"Subject: t", b"MIME-Version: 1.0",
        f'Content-Type: {ctype}; charset="{charset}"'.encode(), f"Content-Transfer-Encoding: {cte}".encode(),
        b"", body,
    ])


def test_html_only_notice_gets_table_aware_text():
    info = extract(single("text/html", "utf-8", KT_HTML.encode()))
    assert info.html == KT_HTML
    assert info.text.splitlines() == [
        "안녕하세요. kt cloud입니다.",
        "대상계정명\tgov_xxxx@example.invalid",
        "작업일시\t2026-10-24 22:00 ~ 10:00",
        "문의: 고객센터&담당자",
    ]
    assert info.errors == ()


def test_multipart_prefers_plain_and_keeps_html():
    info = extract(mime([("text/plain", "utf-8", "평문 본문".encode(), "base64"),
                         ("text/html", "utf-8", KT_HTML.encode(), "base64")]))
    assert info.text == "평문 본문"
    assert "대상계정명" in info.html


def test_ks_c_5601_label_decodes_cp949_only_syllables():
    info = extract(single("text/plain", "ks_c_5601-1987", "똠방각하 작업 안내".encode("cp949")))
    assert info.text == "똠방각하 작업 안내"
    assert info.errors == ()


def test_mislabelled_charset_is_recovered_and_reported():
    info = extract(single("text/plain", "us-ascii", "작업 안내".encode("utf-8"), cte="8bit"))
    assert info.text == "작업 안내"
    assert info.errors == ("text/plain labelled us-ascii decoded as utf-8",)


def test_attachment_is_ignored_for_text():
    raw = mime([("text/plain", "utf-8", "본문".encode(), "base64")]).replace(
        b"--b1--", b"--b1\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=a.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0x\r\n--b1--",
    )
    assert extract(raw).text == "본문"


def test_garbage_never_raises():
    info = extract(b"\x00\xff not a message \r\n\r\n\x80\x81")
    assert isinstance(info.text, str)


def test_html_to_text_drops_script_and_style():
    assert html_to_text("<script>var x=1</script><style>p{}</style><p>내용</p>") == "내용"
