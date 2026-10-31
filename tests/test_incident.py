"""KT outage notices (컴퓨팅 노드 Down 발생/복구): customers are told at once, under KT's own title (user, 2026-10-22)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from customers.models import InternalAccount
from mailcollect import headers, ktform
from mailcollect.headers import HeaderInfo, classify
from mailcollect.models import MessageHeader, Notice, Work
from mailcollect.notice_store import parse_pending
from notifications import dispatch
from notifications.models import Notification
from notifications.service import prepare_notifications
from tests.test_dispatch import LIVE, fake, switch_on  # noqa: F401 (fixture)
from tests.test_notice_store import stored
from tests.test_notifications import master

pytestmark = pytest.mark.django_db
ACCOUNT = "owner-acct@example.invalid"
DOWN = "[kt cloud 컴퓨팅 노드 Down 발생 공지]"
UP = "[kt cloud 컴퓨팅 노드 Down 복구 공지]"


def outage_text(subject, account=ACCOUNT, server="web-01"):
    return f"""○ 대상계정명 : {account}
○ 대상서버명 : {server}
{subject}

안녕하세요. kt cloud 입니다.

고객님의 가상 서버가 수용된 컴퓨팅 노드에 Down이 발생하였습니다.
다른 컴퓨팅 노드로 이전(HA) 진행 중입니다.

서비스 운영에 불편을 겪으신 고객님께 사과 말씀 드리며, 긴급히 서비스 확인 부탁 드립니다.
감사합니다."""


def outage(uidl, subject, when, account=ACCOUNT):
    category = headers.KT_RECOVERY if "복구" in subject else headers.KT_INCIDENT
    seen = stored(uidl, category, outage_text(subject, account), [b"From: kt cloud <cloudmaster@ktcloud.com>"])
    MessageHeader.objects.filter(seen=seen).update(subject=subject, date=when)
    return seen


def kind(subject):
    return classify(HeaderInfo(subject, True, "cloudmaster@ktcloud.com", "", "", None, "", ()))


def test_down_and_its_recovery_are_kt_notices_but_not_other_restores():
    assert kind(DOWN) == headers.KT_INCIDENT and kind(UP) == headers.KT_RECOVERY
    assert kind("[kt cloud] 스냅샷 복구 완료 안내") == headers.KT_OTHER
    assert kind("[kt cloud 작업 공지]") == headers.KT_NOTICE  # work notices as before
    assert headers.KT_INCIDENT in headers.MATCHED  # its body is read
    form = ktform.KtForm(incident=(), recovery=())  # emptied on 환경 설정: back to KT 기타
    assert classify(HeaderInfo(DOWN, True, form.sender, "", "", None, "", ()), form=form) == headers.KT_OTHER


def test_down_and_recovery_make_one_outage_with_two_customer_notices():
    master(ACCOUNT)
    down_at = timezone.now() - timedelta(minutes=10)
    outage("d1", DOWN, down_at)
    outage("u1", UP, down_at + timedelta(minutes=4))
    parse_pending()
    prepare_notifications()
    down, up = Notice.objects.get(seen__uidl="d1"), Notice.objects.get(seen__uidl="u1")
    assert down.status == up.status == "ok" and down.title == "컴퓨팅 노드 Down 발생" and down.servers == ["web-01"]
    work = Work.objects.get()
    assert work.incident and work.title == "컴퓨팅 노드 Down" and down.work == up.work == work
    assert work.starts_at == down_at and work.ends_at == down_at + timedelta(minutes=4)
    n = Notification.objects.get(kind="notice")
    assert n.status == "draft_ready" and n.kind_label == "장애 발생 공지"
    assert n.subject == "[예시클라우드] 컴퓨팅 노드 Down 발생 안내"
    lines = n.body.split("\n")
    assert lines[:3] == ["안녕하세요. 예시클라우드입니다.", "", "kt cloud 측으로부터 컴퓨팅 노드 Down 발생 공지가 수신되어 안내드립니다."]
    assert "긴급히 서비스 확인 부탁 드립니다." in n.body
    m = Notification.objects.get(kind="complete")
    assert m.subject == "[예시클라우드] 컴퓨팅 노드 Down 복구 안내" and m.kind_label == "장애 복구 공지"


def test_outage_notices_go_out_at_once_though_the_outage_began(fake):
    switch_on()
    master(ACCOUNT)
    outage("d1", DOWN, timezone.now() - timedelta(minutes=10))
    parse_pending()
    prepare_notifications()
    assert dispatch.run(LIVE, fake)["live accepted"] == 1
    assert Notification.objects.get().status == "send_requested"


def test_internal_accounts_are_left_out():
    InternalAccount.objects.create(account_key=ACCOUNT, kind="internal", added_at=timezone.now())
    outage("d1", DOWN, timezone.now())
    parse_pending()
    prepare_notifications()
    assert Notification.objects.get().status == "internal"


def test_an_unsent_outage_notice_is_late_after_an_hour(client, django_user_model):
    from web.scope import late_q
    master(ACCOUNT)
    outage("d1", DOWN, timezone.now() - timedelta(minutes=30))
    parse_pending()
    prepare_notifications()
    assert not Notification.objects.filter(late_q()).exists()
    MessageHeader.objects.update(date=timezone.now() - timedelta(hours=2))
    assert Notification.objects.filter(late_q()).count() == 1
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    work = Work.objects.get()
    page = client.get(f"/works/{work.pk}/").content.decode()
    assert "장애 발생 공지" in page and "아직 KT 복구 공지가 오지 않았습니다." in page
