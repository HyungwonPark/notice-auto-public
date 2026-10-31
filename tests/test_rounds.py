"""사전 공지 rounds: KT sends the same notice again days later and staff send the customer notice again."""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from mailcollect import headers
from mailcollect.models import MessageHeader, Work
from mailcollect.notice_store import parse_pending
from notifications.models import Notification
from notifications.service import match_copies, prepare_notifications
from tests.test_notice_store import stored
from tests.test_notifications import copy_text, header, kt_text, master

pytestmark = pytest.mark.django_db
ACCOUNT = "a@example.invalid"
T0 = timezone.now() - timedelta(days=9)


def sent(uidl, when):
    """Give a stored mail its Date header."""
    seen = MessageHeader.objects.filter(seen__uidl=uidl)
    if seen.exists():
        seen.update(date=when)
    else:
        from mailcollect.models import MessageSeen
        MessageHeader.objects.create(seen=MessageSeen.objects.get(uidl=uidl), raw=b"x", subject="s", date=when)


def kt(uidl, when, account=ACCOUNT):
    stored(uidl, headers.KT_NOTICE, kt_text(account))
    sent(uidl, when)


def copy(uidl, when):
    stored(uidl, headers.NC24_NOTICE, copy_text(ACCOUNT), header("owner@example.invalid"))
    sent(uidl, when)


def rounds():
    return list(Notification.objects.filter(kind="notice").order_by("round").values_list("round", "status"))


def test_a_later_send_is_the_next_round_and_duplicates_are_not():
    master(ACCOUNT)
    kt("k1", T0)
    kt("k2", T0 + timedelta(minutes=1))  # KT's duplicate to our second address
    parse_pending()
    prepare_notifications()
    assert rounds() == [(1, "draft_ready")]

    kt("k3", T0 + timedelta(hours=190))
    kt("k4", T0 + timedelta(hours=190, minutes=1))
    parse_pending()
    assert prepare_notifications()["next round"] == 1
    assert rounds() == [(1, "draft_ready"), (2, "draft_ready")]
    second = Notification.objects.get(round=2)
    assert second.kind_label == "사전 공지 2차" and second.number != Notification.objects.get(round=1).number
    assert "2차 안내문 생성" in second.events.get().note


def test_each_copy_goes_to_the_round_it_answers():
    master(ACCOUNT)
    kt("k1", T0)
    kt("k2", T0 + timedelta(hours=190))
    copy("c1", T0 + timedelta(minutes=40))
    copy("c2", T0 + timedelta(hours=190, minutes=30))
    parse_pending()
    prepare_notifications()
    match_copies()
    first, second = Notification.objects.order_by("round")
    assert [c.seen.uidl for c in first.copies.all()] == ["c1"]
    assert [c.seen.uidl for c in second.copies.all()] == ["c2"]
    assert rounds() == [(1, "sent_copy_confirmed"), (2, "sent_copy_confirmed")]


def test_copies_matched_before_the_next_round_move_to_it():
    master(ACCOUNT)
    kt("k1", T0)
    copy("c1", T0 + timedelta(minutes=40))
    copy("c2", T0 + timedelta(hours=190, minutes=30))  # staff's 2차 mail, read before KT's 2차 notice
    parse_pending()
    prepare_notifications()
    match_copies()
    assert Notification.objects.get().copies.count() == 2

    kt("k2", T0 + timedelta(hours=190))
    parse_pending()
    prepare_notifications()
    assert match_copies()["moved to its round"] == 1
    first, second = Notification.objects.order_by("round")
    assert [c.seen.uidl for c in first.copies.all()] == ["c1"] and first.status == "sent_copy_confirmed"
    assert [c.seen.uidl for c in second.copies.all()] == ["c2"] and second.status == "sent_copy_confirmed"


def test_an_older_send_read_later_keeps_the_first_notification_as_round_one():
    master(ACCOUNT)
    kt("k2", T0 + timedelta(hours=190))  # a mailbox read newest first
    parse_pending()
    prepare_notifications()
    first = Notification.objects.get()
    kt("k1", T0)
    parse_pending()
    assert prepare_notifications() == {"moved to an earlier send": 1, "next round": 1}
    first.refresh_from_db()
    assert (first.round, first.notice.seen.uidl) == (1, "k1")
    assert Notification.objects.get(round=2).notice.seen.uidl == "k2"
    assert prepare_notifications() == {}


def test_completion_notices_have_no_rounds():
    master(ACCOUNT)
    for uidl, hours in (("k1", 0), ("k2", 30)):
        stored(uidl, headers.KT_COMPLETE, kt_text(ACCOUNT))
        sent(uidl, T0 + timedelta(hours=hours))
    parse_pending()
    prepare_notifications()
    assert Notification.objects.get(kind="complete").kind_label == "완료 공지"


def test_work_page_splits_rounds(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    master(ACCOUNT)
    kt("k1", T0)
    kt("k2", T0 + timedelta(hours=190))
    parse_pending()
    prepare_notifications()
    page = client.get(reverse("work", args=[Work.objects.get().pk])).content.decode()
    assert '<h3 class="group-title">사전 공지 1차' in page and '<h3 class="group-title">사전 공지 2차' in page
    assert page.count("KT 공지 ") >= 2 and "받음" in page
