"""Notice parsing on synthetic bodies that follow the real template families (made-up values only)."""
from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mailcollect.notices import KST, parse_notice, parse_schedule

FAMILY_A = """안녕하세요 kt cloud 입니다.
항상 kt cloud를 이용해 주시는 고객님께 감사드립니다.
○ 대상계정명 : 26j3_test@example.invalid
○ 대상서버명 : web-01,db-01
ㅇ 작업내용 : KT Cloud 네트워크 개선 작업
– 상세내용 : 안정적인 서비스 제공을 위해 장비를 교체합니다.
ㅇ 작업일시 : 2026. 10. 24 22:00 ~ 10:00
ㅇ 작업영향 : 작업 중 일시적인 네트워크 단절이 발생할 수 있습니다.
감사합니다.
☎고객센터 무료 연결번호: 080-2580-005"""

FAMILY_B = """○ 대상계정명 : User@Example.invalid
○ 대상AI-DX명 : (주)예*
ㅇ 작업내용 : 스토리지 운영 개선
ㅇ 작업일시 : 26.10.24(토) 22:00 ~ 02:00
ㅇ 대상고객 : 공공 D-Cloud 이용 고객
ㅇ 작업영향 : 작업 중 서비스 영향 없음"""

FAMILY_C = """○ 대상계정명 : a_b@example.invalid
ㅇ 작업 대상 : KR-M2 Zone 내 서버
ㅇ 작업 내용 : DB 서버 점검
ㅇ 작업 일시 : 2026.10.24 (토) 00:00 ~ 04:00 (4시간)
ㅇ 작업 영향 : 작업 중 접속 불가"""

CANCEL = """안녕하세요. kt cloud 입니다.
아래 작업이 취소되었음을 알려드립니다,
1. 작업 내용 : KT Cloud 네트워크 개선 작업
- 작업 일시 : 2026.10.24(토) 22:00~02:00(4시간)
- 취소 사유 : 일정 조정
2. 대상 계정명 :
x1@example.invalid
☎고객센터 무료 연결번호: 080-2580-005"""

NC24_COPY = """안녕하세요. 예시클라우드입니다.
자세한 사항은 하기 내용 참고 부탁드리겠습니다.
-----------------------------------------------------------------------------------
안녕하세요 kt cloud 입니다.
○ 대상계정명 : c1@example.invalid, c2@example.invalid
○ 대상LB명 : lb-01
ㅇ 작업내용 : 로드밸런서 점검
ㅇ 작업일시 : 2026.10.24(토) 22:00 ~ 23:00 (작업 시간 변동 가능)
ㅇ 작업영향 : 작업 중 1분 이내 단절
감사합니다.
성실한 고객센터 운영"""

TABLE = "대상계정명\tt@example.invalid\n작업내용\t점검\n작업일시\t2026.10.24 01:00 ~ 02:00"


def kst(*args):
    return datetime(*args, tzinfo=KST)


def test_family_a():
    info = parse_notice(FAMILY_A)
    assert info.accounts == ["26j3_test@example.invalid"]
    assert info.servers == ["web-01", "db-01"]
    assert info.title == "KT Cloud 네트워크 개선 작업"
    assert info.detail.startswith("안정적인")
    assert info.schedule == "2026. 10. 24 22:00 ~ 10:00"
    assert info.windows == [(kst(2026, 10, 24, 22), kst(2026, 10, 25, 10))]
    assert info.impact.startswith("작업 중")
    assert info.problems == []


def test_family_b_lower_cases_accounts_and_keeps_masked_customer():
    info = parse_notice(FAMILY_B)
    assert info.accounts == ["user@example.invalid"]
    assert info.masked_customer == "(주)예*"
    assert info.audience == "공공 D-Cloud 이용 고객"
    assert info.windows == [(kst(2026, 10, 24, 22), kst(2026, 10, 25, 2))]
    assert info.problems == []


def test_family_c_spaced_labels():
    info = parse_notice(FAMILY_C)
    assert (info.title, info.target) == ("DB 서버 점검", "KR-M2 Zone 내 서버")
    assert info.windows == [(kst(2026, 10, 24, 0), kst(2026, 10, 24, 4))]
    assert info.problems == []


def test_cancel_numbered_labels_and_value_on_next_line():
    info = parse_notice(CANCEL)
    assert info.title == "KT Cloud 네트워크 개선 작업"
    assert info.accounts == ["x1@example.invalid"]
    assert info.windows == [(kst(2026, 10, 24, 22), kst(2026, 10, 25, 2))]
    assert info.extra == {"취소사유": "일정 조정"}
    assert info.problems == []


def test_nc24_copy_quotes_the_kt_notice():
    info = parse_notice(NC24_COPY)
    assert info.accounts == ["c1@example.invalid", "c2@example.invalid"]
    assert info.lbs == ["lb-01"]
    assert info.windows == [(kst(2026, 10, 24, 22), kst(2026, 10, 24, 23))]


URGENT_RELOCATION = """○ 대상계정명 : x9@example.invalid
○ 대상서버명 :
[kt cloud 장애 예방 긴급 작업 공지]

안녕하세요. kt cloud입니다.
고객님 가상서버가 위치한 물리서버를 이전하는 긴급 작업이 필요한 상황입니다
ㅇ 작업 내용 : 장애 예방을 위한 긴급 재배치 작업
ㅇ 작업 일시 : 2026.10.20 (화) 00:00 ~ 07:00
ㅇ 작업 영향 : 작업 시간 내에 수초의 서비스 지연이 발생할 수 있습니다.
1) 재배치 시간 내에 지연 현상
2) 재생성 필요시 내부 IP가 변경될 수 있습니다.
- 이중화 솔루션을 사용하는 VM의 경우 재기동이 발생될 수 있습니다.
감사합니다."""


def test_empty_server_label_does_not_swallow_the_title_line():
    info = parse_notice(URGENT_RELOCATION)
    assert info.accounts == ["x9@example.invalid"]
    assert info.servers == []
    assert info.title == "장애 예방을 위한 긴급 재배치 작업"
    assert info.windows == [(kst(2026, 10, 20, 0), kst(2026, 10, 20, 7))]
    assert info.problems == []


def test_staff_written_account_label_without_myeong():
    info = parse_notice("○ 대상계정 : s@example.invalid\nㅇ 작업내용 : 점검\nㅇ 작업일시 : 2026.10.24 01:00 ~ 02:00")
    assert (info.accounts, info.problems) == (["s@example.invalid"], [])


def test_change_notice_uses_the_original_window_but_stays_for_review():
    info = parse_notice(
        "1. 작업 내용 : KT Cloud 네트워크 개선 작업\n- 작업 일시 : 일정 변경, 추후 재공지 예정.\n"
        "- 변경전 : 2026.10.24(토) 22:00~02:00(4시간)\n- 변경후 : 추후 공지\n2. 대상 계정명 : x@example.invalid"
    )
    assert info.windows == [(kst(2026, 10, 24, 22), kst(2026, 10, 25, 2))]
    assert info.problems == ["unparsed 작업일시"]


def test_table_cells_from_html():
    info = parse_notice(TABLE)
    assert (info.accounts, info.title) == (["t@example.invalid"], "점검")


def test_missing_fields_are_problems_without_values():
    info = parse_notice("안녕하세요\nㅇ 작업일시 : 추후 공지")
    assert info.problems == ["unparsed 작업일시", "no 대상계정명", "no 작업내용"]
    assert info.schedule == "추후 공지"  # raw kept


def test_account_label_without_email():
    info = parse_notice("○ 대상계정명 : 별도 안내\nㅇ 작업내용 : 점검\nㅇ 작업일시 : 2026.10.24 01:00 ~ 02:00")
    assert "대상계정명 without an email address" in info.problems


@pytest.mark.parametrize(
    "raw, windows, problems",
    [
        ("2026.10.24 22:00 ~ 익일 06:00", [(kst(2026, 10, 24, 22), kst(2026, 10, 25, 6))], []),
        ("2026.10.24 22:00 ~ 2026.10.26 06:00", [(kst(2026, 10, 24, 22), kst(2026, 10, 26, 6))], []),
        ("2026-10-24 20:00 ~ 24:00", [(kst(2026, 10, 24, 20), kst(2026, 10, 25, 0))], []),
        ("2026.10.24 (토) 00:00 ~ 04:00 (2시간)", [(kst(2026, 10, 24, 0), kst(2026, 10, 24, 4))],
         ["작업일시 duration note disagrees with the times"]),
        ("2026.10.24 22:00 ~ 22:00", [(kst(2026, 10, 24, 22), kst(2026, 10, 25, 22))],
         ["작업일시 start and end times are equal"]),
        ("2026.13.40 22:00 ~ 23:00", [], ["invalid date or time in 작업일시"]),
        ("2026.10.26 06:00 ~ 2026.10.24 22:00", [], ["작업일시 ends before it starts"]),
        ("1차 2026.10.24 22:00 ~ 23:00, 2차 2026.10.31 22:00 ~ 23:00",
         [(kst(2026, 10, 24, 22), kst(2026, 10, 24, 23)), (kst(2026, 10, 31, 22), kst(2026, 10, 31, 23))], []),
    ],
)
def test_schedule_rules(raw, windows, problems):
    assert parse_schedule(raw) == (windows, problems)


WEEKDAYS = "월화수목금토일"
FORMATS = [
    lambda s, e, h: f"{s:%Y}. {s:%m}. {s:%d} {s:%H:%M} ~ {e:%H:%M}",
    lambda s, e, h: f"{s:%y}.{s:%m}.{s:%d}({WEEKDAYS[s.weekday()]}) {s:%H:%M} ~ {e:%H:%M}",
    lambda s, e, h: f"{s:%Y}.{s:%m}.{s:%d}({WEEKDAYS[s.weekday()]}) {s:%H:%M} ~ {e:%H:%M} (작업 시간 변동 가능)",
    lambda s, e, h: f"{s:%Y}.{s:%m}.{s:%d} ({WEEKDAYS[s.weekday()]}) {s:%H:%M} ~ {e:%H:%M}" + (f" ({h}시간)" if h else ""),
    lambda s, e, h: f"{s:%Y}.{s:%m}.{s:%d}({WEEKDAYS[s.weekday()]}) {s:%H:%M}~{e:%H:%M}" + (f"({h}시간)" if h else ""),
]


@given(
    st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2030, 12, 31)),
    st.integers(min_value=1, max_value=24 * 12 - 1),  # 5-minute steps, under 24 hours
    st.sampled_from(FORMATS),
)
def test_every_observed_format_round_trips(start, steps, fmt):
    start = start.replace(minute=start.minute - start.minute % 5, second=0, microsecond=0, tzinfo=KST)
    end = start + timedelta(minutes=5 * steps)
    hours = steps // 12 if steps % 12 == 0 else None
    assert parse_schedule(fmt(start, end, hours)) == ([(start, end)], [])
