"""별도 발송 (a partner sends to some customers) and the two notice stages of a work."""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from customers.models import InternalAccount, PartnerChannel
from mailcollect import headers
from mailcollect.headers import HeaderInfo
from mailcollect.models import MessageHeader, Work
from mailcollect.notice_store import parse_pending
from notifications.models import Notification, WorkClosure
from notifications.service import match_copies, prepare_notifications
from tests.test_notice_store import stored
from tests.test_notifications import kt_text, master

pytestmark = pytest.mark.django_db
AGENCY = "agency@example.invalid"
PARTNER = "notice@partner.example.invalid"
FUTURE = "2099. 10. 24 22:00 ~ 10:00"


def info(sender, subject):
    return HeaderInfo(subject, True, sender, "", "", None, "", ())


def test_partner_sender_with_작업_is_its_own_copy_category():
    partners = frozenset({PARTNER})
    assert headers.classify(info(PARTNER, "[협력사] 환경 kt cloud 작업 공지"), partners) == headers.PARTNER_NOTICE
    assert headers.classify(info(PARTNER, "[협력사] kt cloud 작업 완료 공지"), partners) == headers.PARTNER_COMPLETE
    assert headers.classify(info(PARTNER, "[협력사] kt cloud 작업 공지"), frozenset()) == headers.SUSPECT
    assert headers.restrict(headers.PARTNER_NOTICE, "kt") == headers.OTHER  # alarm mailboxes keep KT only


@pytest.fixture
def channel():
    return PartnerChannel.objects.create(label="협력기관", accounts=f"{AGENCY}\n", sender=PARTNER)


def kt(uidl, account, category=headers.KT_NOTICE, schedule=FUTURE):
    stored(uidl, category, kt_text(account, schedule=schedule))
    parse_pending()
    prepare_notifications()


def partner_mail(uidl, account=AGENCY, category=headers.PARTNER_NOTICE):
    text = "kt cloud로부터 받은 공지를 전달드립니다.\n" + kt_text(account, schedule=FUTURE)
    stored(uidl, category, text, [f"From: 협력사 <{PARTNER}>".encode(), b"To: someone@agency.example.invalid",
                                  b"Subject: s"])
    MessageHeader.objects.filter(seen__uidl=uidl).update(from_addr=PARTNER)  # the scanner parses it from the header
    parse_pending()
    return match_copies()


def test_partner_customer_needs_no_master_and_is_confirmed_by_the_partners_mail(channel):
    kt("k1", AGENCY)
    n = Notification.objects.get()
    assert (n.status, n.review_reasons, n.display_name, n.recipients) == ("draft_ready", "", "협력기관", [])
    assert partner_mail("p1") == {"confirmed by account, kind and window": 1}
    n.refresh_from_db()
    assert n.status == "sent_copy_confirmed"


def test_partner_mail_naming_another_account_proves_nothing(channel):
    master("other@example.invalid")
    kt("k1", "other@example.invalid")
    assert partner_mail("p1", account="other@example.invalid") == {}
    assert Notification.objects.get().status == "draft_ready"


def test_adding_a_channel_clears_the_missing_customer_review():
    kt("k1", AGENCY)
    assert Notification.objects.get().status == "needs_review"
    PartnerChannel.objects.create(label="협력기관", accounts=AGENCY, sender=PARTNER)
    prepare_notifications()
    assert Notification.objects.get().status == "draft_ready"


@pytest.fixture
def staff(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))


def test_partner_customers_have_their_own_tab(client, staff, channel):
    master("c@example.invalid")
    kt("k1", AGENCY)
    stored("k2", headers.KT_NOTICE, kt_text("c@example.invalid", schedule=FUTURE, title="다른 작업"))
    parse_pending()
    prepare_notifications()
    dash = client.get(reverse("dashboard")).content.decode()
    assert "다른 작업" in dash and "KT Cloud 네트워크 개선 작업" not in dash  # only partner customers on that work
    assert f'href="{reverse("channel", args=[channel.pk])}"' in dash and "협력기관" in dash

    page = client.get(reverse("channel", args=[channel.pk])).content.decode()
    assert "KT Cloud 네트워크 개선 작업" in page and "다른 작업" not in page and PARTNER in page
    assert AGENCY not in client.get(reverse("review")).content.decode()

    n = Notification.objects.get(account=AGENCY)
    detail = client.get(reverse("notification", args=[n.pk])).content.decode()
    assert "별도 발송" in detail and 'id="draft-body"' not in detail


def stages(client):
    page = client.get(reverse("dashboard")).content.decode()
    upcoming, _, past = page.partition("지난 작업 ")
    return upcoming, past


def test_work_stays_until_both_notices_are_checked(client, staff):
    master("a@example.invalid")
    kt("k1", "a@example.invalid", schedule="2026. 01. 05 22:00 ~ 23:00")
    work = Work.objects.get()
    Work.objects.update(starts_at=timezone.now() - timedelta(days=2), ends_at=timezone.now() - timedelta(days=2))
    Notification.objects.update(status="staff_confirmed")
    upcoming, past = stages(client)
    assert "KT Cloud 네트워크 개선 작업" in upcoming and "완료 공지 전" in upcoming  # completion still to come

    kt("k2", "a@example.invalid", category=headers.KT_COMPLETE, schedule="2026. 01. 05 22:00 ~ 23:00")
    assert Notification.objects.filter(kind="complete").count() == 1
    upcoming, past = stages(client)
    assert "KT Cloud 네트워크 개선 작업" in upcoming and "담당자 확인 0/1" in upcoming

    Notification.objects.update(status="staff_confirmed")
    upcoming, past = stages(client)
    assert "KT Cloud 네트워크 개선 작업" not in upcoming and "KT Cloud 네트워크 개선 작업" in past
    assert work.pk


def test_a_work_can_be_moved_to_past_by_hand_and_back(client, staff):
    master("a@example.invalid")
    kt("k1", "a@example.invalid")
    work = Work.objects.get()
    client.post(reverse("work_close", args=[work.pk]), {"actor": ""})
    assert not WorkClosure.objects.exists()  # a name is required

    client.post(reverse("work_close", args=[work.pk]), {"actor": "홍길동"})
    assert WorkClosure.objects.get().closed_by == "홍길동"
    upcoming, past = stages(client)
    assert "KT Cloud 네트워크 개선 작업" in past and "직접 넘김 (홍길동)" in past

    client.post(reverse("work_close", args=[work.pk]), {"reopen": "1"})
    assert not WorkClosure.objects.exists()
    assert "KT Cloud 네트워크 개선 작업" in stages(client)[0]


def test_internal_only_work_leaves_once_it_is_over(client, staff):
    InternalAccount.objects.create(account_key="ops@example.invalid", added_at=timezone.now())
    kt("k1", "ops@example.invalid")
    Work.objects.update(starts_at=timezone.now() - timedelta(days=1), ends_at=timezone.now() - timedelta(hours=20))
    assert "KT Cloud 네트워크 개선 작업" in stages(client)[1]


def test_work_page_splits_the_two_notices(client, staff):
    master("a@example.invalid")
    kt("k1", "a@example.invalid")
    page = client.get(reverse("work", args=[Work.objects.get().pk])).content.decode()
    assert '<h3 class="group-title">사전 공지' in page and '<h3 class="group-title">완료 공지' in page
    assert "아직 KT 완료 공지가 오지 않았습니다" in page and "지난 작업으로 넘기기" in page


def test_top_bar_has_theme_switch_and_no_name_field(client, staff):
    page = client.get(reverse("dashboard")).content.decode()
    assert 'data-theme-set="light"' in page and 'data-theme-set="dark"' in page
    assert 'id="actor-name"' not in page and "확인자" not in page


def test_tabs_share_works_but_not_customers(client, staff, channel):
    master("c@example.invalid")
    kt("k1", AGENCY)
    kt("k2", "c@example.invalid")  # the same KT work for an ordinary customer
    work = Work.objects.get()
    main = client.get(reverse("work", args=[work.pk])).content.decode()
    tab = client.get(reverse("channel_work", args=[channel.pk, work.pk])).content.decode()
    assert "c@example.invalid" in main and AGENCY not in main
    assert AGENCY in tab and "c@example.invalid" not in tab

    client.post(reverse("work_close", args=[work.pk]), {"actor": "홍길동", "channel": channel.pk})
    assert WorkClosure.objects.get().channel == channel
    assert "KT Cloud 네트워크 개선 작업" in stages(client)[0]  # still open on the 작업 공지 tab
    page = client.get(reverse("channel", args=[channel.pk])).content.decode()
    assert "직접 넘김 (홍길동)" in page.partition("지난 작업 ")[2]


def test_late_sends_need_attention(client, staff):
    master("a@example.invalid")
    kt("k1", "a@example.invalid")
    Work.objects.update(starts_at=timezone.now() - timedelta(hours=1), ends_at=timezone.now() + timedelta(hours=1))
    upcoming, _ = stages(client)
    assert "발송 늦음 1" in upcoming and '<span class="tag red" title="문제 1건">1</span>' in upcoming
    review = client.get(reverse("review")).content.decode()
    assert "발송이 늦은 안내 <span class=\"muted\">1건" in review
    client.post(reverse("work_close", args=[Work.objects.get().pk]), {"actor": "홍길동"})
    assert "발송이 늦은 안내 <span class=\"muted\">0건" in client.get(reverse("review")).content.decode()


def test_completion_is_late_a_day_after_kts_mail_arrived(client, staff):
    from mailcollect.models import MessageHeader

    master("a@example.invalid")
    kt("k1", "a@example.invalid", category=headers.KT_COMPLETE)
    MessageHeader.objects.create(seen=Notification.objects.get().notice.seen, raw=b"x", subject="s",
                                 date=timezone.now() - timedelta(hours=30))
    assert "발송 늦음 1" in stages(client)[0]


def test_sent_but_unchecked_needs_attention_until_a_person_checks(client, staff, channel):
    """User, 2026-10-19: a tab whose notices were all sent but none checked showed 확인 필요 0."""
    kt("k1", AGENCY)
    n = Notification.objects.get()
    Notification.objects.update(status="sent_copy_confirmed")
    page = client.get(reverse("channel", args=[channel.pk])).content.decode()
    assert '<span class="tag yellow" title="담당자 확인 남음 1건">1</span>' in page  # yellow: nothing wrong, a check owed
    assert "담당자 확인 남음 1건, 그 외 문제 0건" in page and 'id="attention"' in page
    assert f'<tr data-href="{reverse("notification", args=[n.pk])}">' in page.partition('id="attention"')[2]

    master("c@example.invalid")
    kt("k2", "c@example.invalid", schedule="2099. 11. 01 22:00 ~ 23:00")
    other = Notification.objects.get(account="c@example.invalid")
    Notification.objects.filter(pk=other.pk).update(status="manual_sent_recorded")
    review = client.get(reverse("review")).content.decode()
    # 확인 필요 lists every tab, a 별도 발송 row with its tab's name and a checkbox (user, 2026-10-26)
    assert "담당자 확인 남은 안내 <span class=\"muted\">2건" in review
    assert client.get(reverse("dashboard")).context["nav_review"] == 2
    row = review.partition(f'value="{n.pk}"')
    assert row[1] and " disabled" not in row[2].partition(">")[0] and '<span class="tag">협력기관</span>' in row[2]
    assert reverse("channel_work", args=[channel.pk, n.work_id]) in row[2]
    response = client.post(reverse("staff_confirm"), {"ids": [other.pk, n.pk], "actor": "홍길동", "next": "/review/"},
                           follow=True)
    assert "담당자 확인 남은 안내 <span class=\"muted\">0건" in response.content.decode()
    assert client.get(reverse("dashboard")).context["nav_review"] == 0
