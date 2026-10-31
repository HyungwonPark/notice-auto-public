"""KT's 작업 연기 공지 ('[kt cloud 작업 연기 공지]', 작업일시 '추후에 안내 예정'): a round of the work it puts off,
sent to customers like any notice, even once the work's window is over (2026-10-25)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from mailcollect import headers, ktform
from mailcollect.headers import HeaderInfo, classify
from mailcollect.models import MessageHeader, Notice, Work
from mailcollect.notice_store import parse_pending
from notifications import dispatch
from notifications.models import Notification, Record
from notifications.records import sync
from notifications.service import match_copies, prepare_notifications
from tests.test_dispatch import LIVE, fake, switch_on  # noqa: F401 (fixture)
from tests.test_notice_store import stored
from tests.test_notifications import TITLE, header, kt_text, master

pytestmark = pytest.mark.django_db
ACCOUNT = "owner-acct@example.invalid"
SUBJECT = "[kt cloud 작업 연기 공지]"


def postpone_text(account=ACCOUNT, title=TITLE, schedule="추후에 안내 예정"):
    return f"""○ 대상계정명 : {account}
○ 대상AI-DX명 : 예시기관*
{SUBJECT}
안녕하세요. kt cloud 입니다.
하기 작업 일부 완료되었으나 재작업 예정으로 안내드립니다.
ㅇ 작업내용: {title}
ㅇ 작업일시: {schedule}
ㅇ 대상고객: 예시 존의 이용 고객
ㅇ 작업영향: 서비스 지연 2회 발생
서비스 이용에 참고하시기 바라며, 안정적인 서비스 제공을 위하여 최선을 다하겠습니다.
:phone:고객센터 무료 연결번호: 080-2580-005 (편집됨)
* 본 메일은 발신전용 메일입니다."""


def kind(subject, form=ktform.DEFAULT, sender="cloudmaster@ktcloud.com"):
    return classify(HeaderInfo(subject, True, sender, "", "", None, "", ()), form=form)


def mail(uidl, category, text, when, lines=None):
    seen = stored(uidl, category, text, lines or [b"From: kt cloud <cloudmaster@ktcloud.com>"])
    MessageHeader.objects.filter(seen=seen).update(date=when)
    return seen


def work_then_postponed(started_hours_ago=7, hours=12, account=ACCOUNT, twins=True):
    """KT's notice of a work (already sent and checked), then its postponement while it runs, mailed twice."""
    master(account)
    start = timezone.localtime(timezone.now() - timedelta(hours=started_hours_ago)).replace(second=0, microsecond=0)
    end = start + timedelta(hours=hours)
    schedule = f"{start:%Y. %m. %d %H:%M} ~ {end:%H:%M}"
    mail("w1", headers.KT_NOTICE, kt_text(account, schedule=schedule), timezone.now() - timedelta(days=2))
    parse_pending()
    prepare_notifications()
    Notification.objects.update(status="staff_confirmed")
    at = timezone.now() - timedelta(minutes=5)
    mail("p1", headers.KT_POSTPONE, postpone_text(account), at)
    if twins:
        mail("p2", headers.KT_POSTPONE, postpone_text(account), at + timedelta(seconds=2))
    parse_pending()
    prepare_notifications()
    return Work.objects.get()


def test_the_subject_makes_a_postponement_and_its_copy_is_kept():
    assert kind(SUBJECT) == headers.KT_POSTPONE and headers.KT_POSTPONE in headers.MATCHED
    assert kind("[kt cloud 작업 일정 변경 안내]") == headers.KT_CANCEL  # 변경 still goes to a person
    assert kind("[kt cloud 작업 공지]") == headers.KT_NOTICE
    assert kind(SUBJECT, ktform.KtForm(postpone=())) == headers.KT_NOTICE  # emptied on 환경 설정
    assert kind("[예시클라우드] 작업 연기 공지 안내", sender="system@example.com") == headers.NC24_POSTPONE


def test_a_postponement_without_a_date_joins_its_work_as_one_notice():
    work = work_then_postponed()
    notices = Notice.objects.filter(category=headers.KT_POSTPONE)
    assert [(n.status, n.problems, n.work_id) for n in notices] == [("ok", "", work.pk)] * 2
    n = Notification.objects.get(notice__category=headers.KT_POSTPONE)  # KT's twin mails are one notice
    assert n.work == work and n.round == 2 and n.status == "draft_ready" and n.kind_label == "작업 연기 공지"
    assert n.subject == "[예시클라우드] 작업 연기 공지 안내"
    assert n.body.split("\n")[2] == f"kt cloud 측으로부터 {TITLE} 연기 공지가 수신되어 안내드립니다."
    assert "재작업 예정으로 안내드립니다." in n.body and "고객센터 무료" not in n.body
    assert n.events.get().note == "작업 연기 안내문 생성"
    sync()
    assert Record.objects.get(notification=n).kind_label == "작업 연기 공지"


def test_it_goes_out_even_after_the_work_ended(fake):
    switch_on()
    work_then_postponed(started_hours_ago=14, hours=12)  # the window closed two hours ago
    assert dispatch.run(LIVE, fake)["live accepted"] == 1
    assert fake.sent[0]["title"] == "[예시클라우드] 작업 연기 공지 안내"
    assert Notification.objects.get(notice__category=headers.KT_POSTPONE).status == "send_requested"


def test_a_postponement_with_no_work_and_no_date_goes_to_a_person():
    master(ACCOUNT)
    mail("p1", headers.KT_POSTPONE, postpone_text(), timezone.now())
    parse_pending()
    prepare_notifications()
    notice = Notice.objects.get()
    assert notice.status == "review" and notice.problems == "작업 연기 공지 without a matching work"
    assert Notification.objects.get().status == "needs_review"


def test_its_copy_confirms_the_postponement_not_the_earlier_notice():
    work_then_postponed(twins=False)
    n = Notification.objects.get(notice__category=headers.KT_POSTPONE)
    copy = postpone_text().replace("○ 대상계정명", "안녕하세요. 예시클라우드입니다.\n○ 대상계정명")
    mail("c1", headers.NC24_POSTPONE, copy, timezone.now(), header("owner@example.invalid"))
    parse_pending()
    match_copies()
    n.refresh_from_db()
    assert n.status == "sent_copy_confirmed" and n.copies.count() == 1
    assert Notification.objects.get(round=1).copies.count() == 0


def test_an_unsent_postponement_is_late_an_hour_after_kt_mail():
    from web.scope import late_q
    work_then_postponed()  # the work started hours ago: that alone does not make it late
    assert not Notification.objects.filter(late_q()).exists()
    MessageHeader.objects.filter(seen__uidl__in=["p1", "p2"]).update(date=timezone.now() - timedelta(hours=2))
    assert Notification.objects.filter(late_q()).count() == 1


def test_the_work_page_names_the_round(client, django_user_model):
    work = work_then_postponed()
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    page = client.get(f"/works/{work.pk}/").content.decode()
    assert "사전 공지 1차" in page and "작업 연기 공지" in page


def test_a_postponed_work_says_so_and_stays_until_kt_sends_the_new_date(client, django_user_model):
    from web.views import _work_lists
    old = work_then_postponed()
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    upcoming, _ = _work_lists()
    assert [r["w"].pk for r in upcoming] == [old.pk] and upcoming[0]["postponed"]["next"] == "추후에 안내 예정"
    page = client.get("/").content.decode()
    assert "연기</span>" in page and "다음 일정: 추후에 안내 예정" in page
    n = Notification.objects.get(round=1)
    page = client.get(f"/n/{n.pk}/").content.decode()
    assert "다음 일정: 추후에 안내 예정" in page

    start = timezone.localtime(timezone.now() + timedelta(days=7)).replace(hour=22, minute=0, second=0, microsecond=0)
    mail("w2", headers.KT_NOTICE, kt_text(ACCOUNT, schedule=f"{start:%Y. %m. %d %H:%M} ~ 10:00"), timezone.now())
    parse_pending()
    prepare_notifications()
    new = Work.objects.exclude(pk=old.pk).get()
    assert new.rework_of == old and new.title == old.title
    upcoming, past = _work_lists()
    assert [r["w"].pk for r in upcoming] == [new.pk] and [r["w"].pk for r in past] == [old.pk]
    assert past[0]["postponed"]["rework"] == new
    page = client.get(f"/works/{new.pk}/").content.decode()
    assert "재작업</span>" in page and "연기된 작업" in page
    assert f"/works/{new.pk}/" in client.get(f"/works/{old.pk}/").content.decode()


def test_a_work_of_the_same_title_without_a_postponement_is_no_rework():
    master(ACCOUNT)
    mail("w1", headers.KT_NOTICE, kt_text(ACCOUNT, schedule="2026. 10. 10 22:00 ~ 10:00"), timezone.now())
    mail("w2", headers.KT_NOTICE, kt_text(ACCOUNT, schedule="2026. 10. 17 22:00 ~ 10:00"), timezone.now())
    parse_pending()
    assert Work.objects.count() == 2 and not Work.objects.filter(rework_of__isnull=False).exists()


def test_the_notice_page_shows_kt_mails_of_its_work(client, django_user_model):
    work_then_postponed()
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    n = Notification.objects.get(round=1)
    page = client.get(f"/n/{n.pk}/").content.decode()
    assert "KT 공지 원문 <span class=\"muted\">2통</span>" in page  # the notice, then KT's twin postponements
    assert "KT 작업 연기 공지" in page and "같은 메일 2통" in page and "이 안내</span>" in page
    assert "재작업 예정으로 안내드립니다." in page
