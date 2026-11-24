"""One work under two spellings of its title (2026-11-22): KT's 사전 공지 said 'kt cloud …' and its 완료 공지
'KT Cloud …' with the real end time; three rounds wrote '작업 (X)', '작업 ( X)' and '작업(X)'."""
import importlib
from datetime import timedelta

import pytest
from django.apps import apps
from django.utils import timezone

from mailcollect import headers
from mailcollect.models import MessageHeader, Notice, Work
from mailcollect.notice_store import parse_pending
from notifications.models import Notification
from notifications.service import prepare_notifications
from tests.test_notice_store import stored
from tests.test_notifications import master

pytestmark = pytest.mark.django_db
ACCOUNT = "owner-acct@example.invalid"


def text(title, schedule):
    return f"○ 대상계정명 : {ACCOUNT}\n○ 대상서버명 : web-01\nㅇ 작업내용 : {title}\nㅇ 작업일시 : {schedule}\n감사합니다."


def mail(uidl, category, title, schedule, sent):
    seen = stored(uidl, category, text(title, schedule), [b"From: kt cloud <cloudmaster@ktcloud.com>"])
    MessageHeader.objects.filter(seen=seen).update(date=sent)


def test_case_spaces_and_a_shorter_end_keep_one_work():
    master(ACCOUNT)
    now = timezone.now()
    mail("n1", headers.KT_NOTICE, "kt cloud 스토리지 안정화 작업 (G1)", "2099.11.22 (일) 00:00 ~ 07:00", now - timedelta(days=9))
    mail("n2", headers.KT_NOTICE, "kt cloud 스토리지 안정화 작업( G1)", "2099.11.22 (일) 00:00 ~ 07:00", now - timedelta(days=3))
    mail("c1", headers.KT_COMPLETE, "KT Cloud 스토리지 안정화 작업(G1)", "2099.11.22 (일) 00:00 ~ 04:00", now)
    parse_pending()
    prepare_notifications()
    work = Work.objects.get()
    assert set(Notice.objects.values_list("work", flat=True)) == {work.pk}
    assert work.title == "kt cloud 스토리지 안정화 작업 (G1)"
    assert sorted(Notification.objects.values_list("kind", "round")) == [("complete", 1), ("notice", 1), ("notice", 2)]


def test_the_migration_merges_works_split_by_spelling():
    master(ACCOUNT)
    now = timezone.now()
    for i, title in enumerate(("작업 (CHUB-VPN)", "작업 ( CHUB-VPN)", "작업(CHUB-VPN)")):
        mail(f"n{i}", headers.KT_NOTICE, title, "2099.11.22 (일) 00:00 ~ 04:00", now - timedelta(days=9 - 3 * i))
    mail("c", headers.KT_COMPLETE, "작업(chub-vpn)", "2099.11.22 (일) 00:00 ~ 03:00", now)
    parse_pending()
    prepare_notifications()
    # As before title_key: every spelling its own work, each send a 1차.
    assert Notification.objects.count() == 4
    for no in Notice.objects.order_by("pk"):
        w = Work.objects.create(key=f"old-{no.pk}", title=no.title, starts_at=no.starts_at)
        Notice.objects.filter(pk=no.pk).update(work=w)
        Notification.objects.filter(notice=no).update(work=w, round=1)
    Work.objects.exclude(key__startswith="old-").delete()
    Work.objects.update(title_key="")
    first = Work.objects.order_by("pk").first()

    importlib.import_module("mailcollect.migrations.0016_work_title_key").merge(apps, None)

    assert list(Work.objects.values_list("pk", flat=True)) == [first.pk]
    assert sorted(Notification.objects.values_list("kind", "round")) == [
        ("complete", 1), ("notice", 1), ("notice", 2), ("notice", 3)]
    assert prepare_notifications()["moved to an earlier send"] == 0
    assert Notification.objects.count() == 4


def test_a_completion_under_the_whole_title_joins_the_part_its_account_was_told():
    """2026-11-22: 사전 공지 '작업 (CHUB-VPN)' to one customer, '작업' to the rest; the 완료 공지 came as '작업'."""
    other = "other-acct@example.invalid"
    master(ACCOUNT)
    master(other)
    now = timezone.now()
    when = "2099.11.22 (일) 00:00 ~ 04:00"
    mail("p", headers.KT_NOTICE, "목동 장비 대개체 작업 (CHUB-VPN)", when, now - timedelta(days=5))
    seen = stored("w", headers.KT_NOTICE, text("목동 장비 대개체 작업", when).replace(ACCOUNT, other),
                  [b"From: kt cloud <cloudmaster@ktcloud.com>"])
    MessageHeader.objects.filter(seen=seen).update(date=now - timedelta(days=5))
    mail("c", headers.KT_COMPLETE, "목동 장비 대개체 작업", "2099.11.22 (일) 00:00 ~ 03:00", now)
    seen = stored("c2", headers.KT_COMPLETE, text("목동 장비 대개체 작업", when).replace(ACCOUNT, other),
                  [b"From: kt cloud <cloudmaster@ktcloud.com>"])
    MessageHeader.objects.filter(seen=seen).update(date=now)
    parse_pending()
    prepare_notifications()
    part = Work.objects.get(title__endswith="(CHUB-VPN)")
    whole = Work.objects.get(title="목동 장비 대개체 작업")
    assert sorted(Notification.objects.filter(work=part).values_list("account", "kind")) == [
        (ACCOUNT, "complete"), (ACCOUNT, "notice")]
    assert sorted(Notification.objects.filter(work=whole).values_list("account", "kind")) == [
        (other, "complete"), (other, "notice")]
