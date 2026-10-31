import base64

import pytest

from mailcollect.headers import (
    KT_CANCEL, KT_COMPLETE, KT_NOTICE, KT_OTHER, KT_URGENT, NC24_COMPLETE, NC24_NOTICE, OTHER, SUSPECT,
    classify, decode_value, parse_headers, split_headers,
)

NC24 = "ExampleCloud <system@example.com>"
from tests.mailfactory import KT, NOTICE, URGENT, encoded_word as ew, message, raw_message, top_lines


def categorize(raw: bytes) -> str:
    return classify(parse_headers(top_lines(raw)))


CASES = [
    ("utf-8 base64", message(ew(NOTICE)), KT_NOTICE),
    ("utf-8 quoted-printable", message(ew(NOTICE, enc="q")), KT_NOTICE),
    ("folded across encoded-words", message(ew(NOTICE[:9]) + "\r\n " + ew(NOTICE[9:])), KT_NOTICE),
    ("ks_c_5601-1987 label", message(ew(NOTICE, "ks_c_5601-1987", "cp949")), KT_NOTICE),
    ("euc-kr label, CP949-only syllable", message(ew("[kt cloud 작업 공지] 똠방각하", "euc-kr", "cp949")), KT_NOTICE),
    ("raw 8-bit UTF-8", message(NOTICE.encode("utf-8")), KT_NOTICE),
    ("raw 8-bit CP949", message(NOTICE.encode("cp949")), KT_NOTICE),
    ("urgent", message(ew(URGENT)), KT_URGENT),
    ("completion notice, real subject form", message(ew("[kt cloud 작업 완료 공지]")), KT_COMPLETE),
    ("cancel notice, real subject form (kt Cloud)", message(ew("[kt Cloud 작업 취소 공지]")), KT_CANCEL),
    ("KT snapshot mail", message(ew("스냅샷 생성 완료 안내")), KT_OTHER),
    ("예시클라우드 notice copy", message(ew("[예시클라우드] 작업 공지 안내"), sender=NC24), NC24_NOTICE),
    ("예시클라우드 completion copy", message(ew("[예시클라우드] 작업 완료 공지 안내"), sender=NC24), NC24_COMPLETE),
    ("notice copy from another sender", message(ew("[예시클라우드] 작업 공지 안내"), sender="staff@example.invalid"), SUSPECT),
    ("예시클라우드 Q&A answer", message(ew("[예시클라우드]예시회사 님의 질문에 대한 답변입니다."), sender=NC24), OTHER),
    ("customer reply to a notice", message(ew("RE: [예시클라우드] 작업 공지 안내"), sender="customer@example.invalid"), OTHER),
    ("bare sender address", message(ew(NOTICE), sender="cloudmaster@ktcloud.com"), KT_NOTICE),
    ("sender address in other case", message(ew(NOTICE), sender="KT <CloudMaster@KTCloud.com>"), KT_NOTICE),
    ("leading space inside encoded text", message(ew(" " + NOTICE)), KT_NOTICE),
    ("quoted display-name spoof", message(ew(NOTICE), sender='"cloudmaster@ktcloud.com" <attacker@example.invalid>'), SUSPECT),
    ("unquoted display-name spoof", message(ew(NOTICE), sender="cloudmaster@ktcloud.com <attacker@example.invalid>"), SUSPECT),
    ("look-alike domain", message(ew(NOTICE), sender="cloudmaster@ktcloud.com.example.invalid"), SUSPECT),
    ("forwarded by a colleague", message(ew("FW: " + NOTICE), sender="동료 <colleague@example.invalid>"), SUSPECT),
    ("KT reply prefix still mentions 작업", message(ew("RE: " + NOTICE)), KT_NOTICE),
    ("KT mixed case", message(ew("[KT Cloud 작업 공지] 점검")), KT_NOTICE),
    ("mixed case from another sender", message(ew("[KT Cloud 작업 공지]"), sender="x@example.invalid"), SUSPECT),
    ("KT no-break space in prefix", message(ew("[kt cloud 작업 공지] 점검")), KT_NOTICE),
    ("KT urgent with a lead-in (seen 2026-10-18)", message(ew("[kt cloud 장애 예방 긴급 작업 공지]")), KT_URGENT),
    ("KT other lead-in", message(ew("[kt cloud 정기 점검 작업 공지]")), KT_NOTICE),
    ("KT urgent completion", message(ew("[kt cloud 긴급 작업 완료 공지]")), KT_COMPLETE),
    ("KT 작업 result mail is kept", message(ew("[kt cloud 작업 결과 안내]")), KT_NOTICE),
    ("KT schedule change", message(ew("[kt cloud 작업 일정 변경 안내]")), KT_CANCEL),
    ("KT mail without 작업", message(ew("[kt cloud] 긴급 점검 안내")), KT_OTHER),
    ("new-style notice forwarded by a colleague", message(ew("FW: [kt cloud 장애 예방 긴급 작업 공지]"),
                                                         sender="동료 <colleague@example.invalid>"), SUSPECT),
    ("KT 작업 anywhere in the subject", message(ew("공지: " + NOTICE)), KT_NOTICE),
    ("KT other mail", message(ew("[kt cloud] 이용 요금 안내")), KT_OTHER),
    ("unrelated mail", message(ew("주간 회의록"), sender="팀 <team@example.invalid>"), OTHER),
]


@pytest.mark.parametrize("raw, expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_classification(raw, expected):
    assert categorize(raw) == expected


def test_cp949_only_syllable_really_needs_cp949():
    with pytest.raises(UnicodeDecodeError):
        "똠".encode("cp949").decode("euc-kr")


def test_decoded_fields():
    info = parse_headers(top_lines(message(ew(NOTICE), sender="kt cloud <cloudmaster@ktcloud.com>")))
    assert info.subject == NOTICE
    assert info.from_addr == "cloudmaster@ktcloud.com"
    assert info.from_display == "kt cloud"
    assert info.date.isoformat() == "2026-10-18T10:00:00+09:00"
    assert info.message_id.endswith("@example.invalid>")
    assert info.errors == ()


def test_comma_inside_display_name_comment_still_yields_the_address():
    info = parse_headers(top_lines(message("x", sender="홍길동 매니저(기술지원팀,예시클라우드) <staff@example.invalid>")))
    assert info.from_addr == "staff@example.invalid"
    assert info.from_display == "홍길동 매니저(기술지원팀,예시클라우드)"
    assert info.errors == ()


def test_guessed_charset_is_never_auto_accepted():
    data = base64.b64encode("[kt cloud 작업 공지]".encode()).decode()
    info = parse_headers(top_lines(message(f"=?x-unknown?B?{data}?=")))
    assert "undecodable subject" in info.errors
    assert info.subject == "[kt cloud 작업 공지]"  # UTF-8 guess happens to fit
    assert classify(info) == KT_OTHER  # still only for review


def test_broken_base64_is_tolerated_and_not_matched():
    info = parse_headers(top_lines(message("=?utf-8?B?***?=")))
    assert classify(info) == KT_OTHER


def test_two_subject_headers_are_not_auto_accepted():
    raw = raw_message([b"From: " + KT.encode(), b"Subject: " + ew(NOTICE).encode(), b"Subject: other"])
    assert categorize(raw) == KT_OTHER


def test_two_from_headers_mean_no_trusted_sender():
    raw = raw_message([b"From: " + KT.encode(), b"From: other@example.invalid", b"Subject: " + ew(NOTICE).encode()])
    info = parse_headers(top_lines(raw))
    assert info.from_addr == ""
    assert "multiple from headers" in info.errors
    assert classify(info) == SUSPECT


def test_missing_subject_and_sender():
    info = parse_headers(top_lines(raw_message([b"To: ops@example.invalid"])))
    assert (info.subject, info.from_addr) == ("", "")
    assert classify(info) == OTHER


def test_split_headers_unfolds_and_stops_at_blank_line():
    lines = [b"Received: from a", b"\tby b", b"Subject: first", b"  second", b"", b"Subject: body line"]
    assert split_headers(lines) == {"received": [b"from a\tby b"], "subject": [b"first  second"]}


def test_decode_value_plain_ascii():
    assert decode_value(b"  Weekly report ") == ("Weekly report", True)
