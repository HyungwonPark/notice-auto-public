import re

import pytest

from customers.models import CustomerAccount
from mailcollect import headers
from mailcollect.notice_store import parse_pending
from notifications import drafts
from notifications.models import Notification
from notifications.service import match_copies, prepare_notifications, unmatched_copies
from tests.test_notice_store import stored

TITLE = "KT Cloud 네트워크 개선 작업"


def kt_text(account, schedule="2026. 10. 24 22:00 ~ 10:00", title=TITLE):
    return f"""안녕하세요 kt cloud 입니다.
○ 대상계정명 : {account}
○ 대상서버명 : web-01
ㅇ 작업내용 : {title}
ㅇ 작업일시 : {schedule}
ㅇ 작업영향 : 작업 중 일시적인 단절
감사합니다.
☎고객센터 무료 연결번호: 080-2580-005
* 본 메일과 관련된 문의사항은 고객센터(080-2580-005)를 이용하시기 바랍니다.
* 본 메일은 발신전용 메일입니다. 메일수신을 원하지 않을 경우 [여기]를 클릭하여 이메일 수신거부를 신청해 주십시오."""


def copy_text(account, schedule="2026.10.24(토) 22:00 ~ 10:00", number=None):
    tail = f"\n안내번호: {number}" if number else ""
    return f"""안녕하세요. 예시클라우드입니다.
KT Cloud에서 아래와 같이 작업을 진행할 예정입니다.
자세한 사항은 하기 내용 참고 부탁드리겠습니다.
{drafts.SEPARATOR}
○ 대상계정명 : {account}
ㅇ 작업내용 : {TITLE}
ㅇ 작업일시 : {schedule}
감사합니다.{tail}"""


def master(account, email="owner@example.invalid", company="예시회사"):
    return CustomerAccount.objects.create(kind="general", source="csv", account=account, email=email, company=company)


def header(to):
    return [b"From: ExampleCloud <system@example.com>", b"To: " + to.encode(), b"Cc: staff@example.invalid", b"Subject: s"]


# --- drafts -----------------------------------------------------------------------------------------------

def test_draft_quotes_kt_text_without_kt_footer():
    subject, body = drafts.build("notice", kt_text("a@example.invalid"))
    lines = body.split("\n")
    assert subject == "[예시클라우드] 작업 공지 안내"
    assert lines[:8] == ["안녕하세요. 예시클라우드입니다.", "", drafts.OPENINGS["notice"], "",
                         "자세한 사항은 하기 내용 참고 부탁드리겠습니다.", "", "-" * 83, ""]
    assert "080-2580-005" not in body and "발신전용" not in body
    assert body.count("감사합니다.") == 1
    assert "ㅇ 작업일시 : 2026. 10. 24 22:00 ~ 10:00" in lines  # KT values unchanged
    assert lines[-1] == "감사합니다." and "안내번호" not in body  # not in the body (user decision 2026-10-19)
    assert drafts.number_in("감사합니다.\n\n안내번호: CSP-261018-0001") == "CSP-261018-0001"  # older copies


def test_completion_draft_and_closing_added_when_missing():
    subject, body = drafts.build("complete", "ㅇ 작업내용 : 점검")
    assert subject == "[예시클라우드] 작업 완료 공지 안내"
    assert body.split("\n")[-3:] == ["ㅇ 작업내용 : 점검", "", "감사합니다."]


def test_kt_text_is_spaced_like_a_hand_written_mail():
    """User's example (2026-10-19): sentences apart, item lines and the bracketed title together."""
    text = "\n".join(["○ 대상계정명 : a@example.invalid", "○ 대상서버명 : web-01", "[kt cloud 작업 완료 공지]",
                     "안녕하세요 kt cloud 입니다.", "작업이 완료 되었습니다.", "ㅇ 작업내용: 점검",
                     "– 상세내용: 재배치", "ㅇ 작업일시: 2026.10.19 (월) 00:00 ~ 02:08", "※ 단, 재기동될 수 있습니다.",
                     "", "", "서비스 이용에 참고하시기 바랍니다.", "감사합니다."])
    assert drafts.spaced(text).split("\n") == [
        "○ 대상계정명 : a@example.invalid", "○ 대상서버명 : web-01", "[kt cloud 작업 완료 공지]", "",
        "안녕하세요 kt cloud 입니다.", "", "작업이 완료 되었습니다.", "",
        "ㅇ 작업내용: 점검", "– 상세내용: 재배치", "ㅇ 작업일시: 2026.10.19 (월) 00:00 ~ 02:08", "※ 단, 재기동될 수 있습니다.", "",
        "서비스 이용에 참고하시기 바랍니다.", "", "감사합니다."]


def test_opening_names_the_work_title():
    title = "G-Cloud D 플랫폼, DX-G 존 VM 라이브 마이그레이션 작업"
    assert drafts.opening("notice", title) == f"kt cloud 측으로부터 {title} 공지가 수신되어 안내드립니다."
    assert drafts.opening("complete", title) == f"kt cloud 측으로부터 {title} 완료 공지가 수신되어 안내드립니다."
    assert drafts.opening("complete", "DB 점검 완료") == "kt cloud 측으로부터 DB 점검 완료 공지가 수신되어 안내드립니다."
    assert drafts.opening("notice", "  ") == drafts.OPENINGS["notice"]
    _, body = drafts.build("complete", "ㅇ 작업내용 : 점검", title)
    assert body.split("\n")[2] == f"kt cloud 측으로부터 {title} 완료 공지가 수신되어 안내드립니다."


# --- prepare ----------------------------------------------------------------------------------------------

pytestmark = pytest.mark.django_db


def test_draft_ready_with_master_emails():
    master("a@example.invalid", email="x@example.invalid\r\ny@example.invalid>")
    stored("k1", headers.KT_NOTICE, kt_text("a@example.invalid"))
    parse_pending()
    assert prepare_notifications() == {"created": 1}

    n = Notification.objects.get()
    assert (n.status, n.kind, n.display_name) == ("draft_ready", "notice", "예시회사")
    assert n.recipients == ["x@example.invalid", "y@example.invalid"]
    assert re.fullmatch(r"CSP-\d{6}-\d{4}", n.number)
    assert n.body.endswith("감사합니다.") and n.number not in n.body
    assert n.events.count() == 1


def test_review_reasons():
    master("noemail@example.invalid", email="")
    stored("k1", headers.KT_NOTICE, kt_text("missing@example.invalid"))
    stored("k2", headers.KT_NOTICE, kt_text("noemail@example.invalid"))
    stored("k3", headers.KT_NOTICE, kt_text("missing@example.invalid", schedule="추후 공지", title="다른 작업"))
    parse_pending()
    prepare_notifications()

    reasons = {n.notice.seen.uidl: n.review_reasons.split("\n") for n in Notification.objects.all()}
    assert reasons["k1"] == ["account not in customer master"]
    assert reasons["k2"] == ["no email address in customer master"]
    assert reasons["k3"] == ["notice needs review", "account not in customer master"]
    assert set(Notification.objects.values_list("status", flat=True)) == {"needs_review"}


def test_identical_resend_is_silent_and_completion_is_another_notification():
    master("a@example.invalid")
    stored("k1", headers.KT_NOTICE, kt_text("a@example.invalid"))
    stored("k2", headers.KT_NOTICE, kt_text("a@example.invalid"))  # KT sends the same notice twice
    stored("k3", headers.KT_COMPLETE, kt_text("a@example.invalid"))
    parse_pending()
    assert prepare_notifications() == {"created": 2}
    assert prepare_notifications() == {}  # nothing new on the next scan

    assert Notification.objects.get(kind="notice").review_reasons == ""
    assert Notification.objects.filter(kind="complete").count() == 1


def test_different_notice_for_the_same_work_needs_review():
    master("a@example.invalid")
    stored("k1", headers.KT_NOTICE, kt_text("a@example.invalid"))
    stored("k2", headers.KT_NOTICE, kt_text("a@example.invalid").replace("web-01", "web-02"))
    parse_pending()
    assert prepare_notifications() == {"created": 1, "changed notice": 1}
    n = Notification.objects.get()
    assert (n.status, n.review_reasons) == ("needs_review", "KT sent a different notice for the same work")


def test_cancel_notice_sends_the_work_back_to_review():
    master("a@example.invalid")
    stored("k1", headers.KT_NOTICE, kt_text("a@example.invalid"))
    stored("c1", headers.KT_CANCEL, f"1. 작업 내용 : {TITLE}\n- 작업 일시 : 일정 변경\n"
                                      "- 변경전 : 2026.10.24(토) 22:00~10:00\n2. 대상 계정명 : a@example.invalid")
    parse_pending()
    prepare_notifications()

    n = Notification.objects.get()
    assert (n.status, n.review_reasons) == ("needs_review", "KT change/cancel notice received")


# --- copies -----------------------------------------------------------------------------------------------

def prepared(account="a@example.invalid"):
    master(account)
    stored("k1", headers.KT_NOTICE, kt_text(account))
    parse_pending()
    prepare_notifications()
    return Notification.objects.get()


def test_copy_with_number_confirms_exactly():
    n = prepared()
    stored("n1", headers.NC24_NOTICE, copy_text("a@example.invalid", number=n.number), header("someone@example.invalid"))
    parse_pending()
    assert match_copies() == {"confirmed by number": 1}
    n.refresh_from_db()
    assert (n.status, n.match_basis) == ("sent_copy_confirmed", "number")
    assert list(n.copies.values_list("seen__uidl", flat=True)) == ["n1"]


def test_legacy_copy_confirms_by_account_kind_window_and_recipients():
    n = prepared()
    stored("n1", headers.NC24_NOTICE, copy_text("a@example.invalid"), header("Owner@Example.invalid"))
    parse_pending()
    assert match_copies() == {"confirmed by account, kind and window": 1}
    assert match_copies() == {}  # idempotent
    n.refresh_from_db()
    assert (n.status, n.match_basis) == ("sent_copy_confirmed", "account_kind_window")
    assert n.events.count() == 2
    assert unmatched_copies() == 0


def test_one_mail_per_contact_links_every_copy():
    n = prepared()
    stored("n1", headers.NC24_NOTICE, copy_text("a@example.invalid"), header("owner@example.invalid"))
    stored("n2", headers.NC24_NOTICE, copy_text("a@example.invalid"), header("owner@example.invalid"))
    parse_pending()
    assert match_copies() == {"confirmed by account, kind and window": 2}
    n.refresh_from_db()
    assert (n.status, n.copies.count(), unmatched_copies()) == ("sent_copy_confirmed", 2, 0)
    assert list(n.events.values_list("note", flat=True)) == [
        "안내문 생성", "보낸 메일 확인 (계정, 공지 종류, 작업 일시 일치)", "보낸 메일 추가 확인 (계정, 공지 종류, 작업 일시 일치)"]


def test_works_starting_together_are_told_apart_by_title():
    master("a@example.invalid")
    stored("k1", headers.KT_NOTICE, kt_text("a@example.invalid"))
    stored("k2", headers.KT_NOTICE, kt_text("a@example.invalid", title="스토리지 점검"))
    parse_pending()
    prepare_notifications()
    stored("n1", headers.NC24_NOTICE, copy_text("a@example.invalid"), header("owner@example.invalid"))
    parse_pending()
    assert match_copies() == {"confirmed by account, kind and window": 1}
    confirmed = Notification.objects.get(status="sent_copy_confirmed")
    assert confirmed.work.title == TITLE


def test_rebuild_is_refused_after_staff_acted():
    from notifications.service import rebuild

    prepared()
    assert rebuild() == 2  # notification + its event
    assert prepare_notifications() == {"created": 1}  # drafted again from the stored notice
    Notification.objects.update(status="copied")
    with pytest.raises(RuntimeError, match="copied or recorded"):
        rebuild()


def test_rebuild_command(capsys):
    from io import StringIO

    from django.core.management import call_command

    prepared()
    out = StringIO()
    call_command("prepare_notifications", rebuild=True, stdout=out)
    assert "rebuild: dropped 2 row(s)" in out.getvalue()
    assert "prepare: created=1" in out.getvalue()


def test_copy_without_master_recipients_is_only_a_candidate():
    n = prepared()
    stored("n1", headers.NC24_NOTICE, copy_text("a@example.invalid"), header("other@example.invalid"))
    parse_pending()
    match_copies()
    n.refresh_from_db()
    assert (n.status, n.review_reasons) == ("sent_copy_candidate", "copy To/Cc has none of the master emails")


def test_copy_of_other_kind_or_window_does_not_match():
    prepared()
    stored("n1", headers.NC24_COMPLETE, copy_text("a@example.invalid"), header("owner@example.invalid"))
    stored("n2", headers.NC24_NOTICE, copy_text("a@example.invalid", schedule="2026.10.25(일) 22:00 ~ 10:00"),
           header("owner@example.invalid"))
    parse_pending()
    assert match_copies() == {}
    assert unmatched_copies() == 2
    assert Notification.objects.get().status == "draft_ready"


def test_redraft_rewrites_every_draft_nobody_copied():
    """Sent ones too: their mail was written in Outlook by staff and is kept apart as 보낸 메일."""
    from notifications.service import record_copied, redraft_unsent

    n = prepared()
    Notification.objects.filter(pk=n.pk).update(body="old text", status="sent_copy_confirmed")
    master("b@example.invalid")
    stored("k2", headers.KT_NOTICE, kt_text("b@example.invalid"))
    parse_pending()
    prepare_notifications()
    copied = Notification.objects.get(account="b@example.invalid")
    record_copied(copied, None, "본문")
    Notification.objects.filter(pk=copied.pk).update(body="old text")
    assert redraft_unsent() == 1
    n.refresh_from_db()
    copied.refresh_from_db()
    assert n.body.split("\n")[2] == f"kt cloud 측으로부터 {TITLE} 공지가 수신되어 안내드립니다."
    assert n.body.endswith("감사합니다.") and copied.body == "old text"
