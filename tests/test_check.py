"""KT '서비스 점검 요청 공지' (2026-10-27): an event on a customer's resource, told at once under KT's title, with no
follow-up mail (user: 바로 자동 발송, KT 제목 그대로)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from mailcollect import headers, ktform
from mailcollect.headers import HeaderInfo, classify
from mailcollect.models import MessageHeader, Notice, Work
from mailcollect.notice_store import parse_pending
from notifications import dispatch
from notifications.models import Notification
from notifications.service import prepare_notifications
from web import views
from tests.test_dispatch import LIVE, fake, switch_on  # noqa: F401 (fixture)
from tests.test_notice_store import stored
from tests.test_notifications import master

pytestmark = pytest.mark.django_db
ACCOUNT = "owner-acct@example.invalid"
CHECK = "[KT cloud 서비스 점검 요청 공지]"


def check_text(account=ACCOUNT):
    return f"""○ 대상계정명 : {account}
○ 대상서버명 :
{CHECK}

안녕하세요. kt cloud 입니다.

고객님께서 이용 중인 가상라우터에 이벤트가 감지되었습니다.
서비스 이용에 이상이 없는지 확인 부탁 드립니다.

감사합니다."""


def check(uidl, when, account=ACCOUNT):
    seen = stored(uidl, headers.KT_CHECK, check_text(account), [b"From: kt cloud <cloudmaster@ktcloud.com>"])
    MessageHeader.objects.filter(seen=seen).update(subject=CHECK, date=when)
    return seen


def kind(subject, form=None):
    form = form or ktform.KtForm()
    return classify(HeaderInfo(subject, True, form.sender, "", "", None, "", ()), form=form)


def test_a_check_request_is_a_kt_notice_of_its_own():
    assert kind(CHECK) == headers.KT_CHECK
    assert kind("[kt cloud 작업 공지]") == headers.KT_NOTICE
    assert headers.KT_CHECK in headers.MATCHED and headers.KT_CHECK in headers.INCIDENTS
    assert kind(CHECK, ktform.KtForm(check=())) == headers.KT_OTHER  # emptied on 환경 설정


def test_a_check_request_makes_a_one_stage_work_and_a_notice_under_kts_title():
    master(ACCOUNT)
    at = timezone.now() - timedelta(minutes=5)
    check("c1", at)
    parse_pending()
    prepare_notifications()
    notice = Notice.objects.get()
    assert notice.status == "ok" and notice.title == "서비스 점검 요청"
    work = Work.objects.get()
    assert work.incident and work.title == "서비스 점검 요청" and work.starts_at == at
    n = Notification.objects.get()
    assert n.kind == "notice" and n.status == "draft_ready" and n.kind_label == "서비스 점검 요청 공지"
    assert n.subject == "[예시클라우드] 서비스 점검 요청 안내"
    assert n.body.split("\n")[2] == "kt cloud 측으로부터 서비스 점검 요청 공지가 수신되어 안내드립니다."
    assert "가상라우터" in n.body


def test_a_check_request_goes_out_at_once(fake):
    switch_on()
    master(ACCOUNT)
    check("c1", timezone.now() - timedelta(minutes=10))
    parse_pending()
    prepare_notifications()
    assert dispatch.run(LIVE, fake)["live accepted"] == 1
    assert Notification.objects.get().status == "send_requested"


def test_the_work_waits_for_no_completion(client, django_user_model):
    master(ACCOUNT)
    check("c1", timezone.now() - timedelta(minutes=10))
    parse_pending()
    prepare_notifications()
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    work = Work.objects.get()
    page = client.get(f"/works/{work.pk}/").content.decode()
    assert "서비스 점검 요청 공지" in page and "공지가 오지 않았습니다" not in page
    assert "점검 요청</span>" in client.get("/").content.decode()
    row = lambda: views._row(views._work_rows(Work.objects.all(), None).get(), timezone.now(), None)  # noqa: E731
    assert not row()["done"]
    Notification.objects.update(status="staff_confirmed", staff_name="담당자")
    assert row()["done"]  # 지난 작업 once the one notice is confirmed
