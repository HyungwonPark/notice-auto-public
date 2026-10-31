"""Taking looked-at mails off 확인 필요, the one-year retention, the one-month 지난 작업 list and tab scoping."""
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from customers.models import PartnerChannel
from mailcollect import headers
from mailcollect.models import MessageSeen, Notice, ScanRun, Work
from mailcollect.notice_store import parse_pending
from notifications.models import Notification, NotificationEvent, WorkClosure
from notifications.retention import purge
from notifications.service import prepare_notifications
from suggestions.models import Suggestion
from tests.test_notice_store import stored
from tests.test_notifications import kt_text, master

pytestmark = pytest.mark.django_db
AGENCY = "agency@example.invalid"


@pytest.fixture
def staff(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    return client


def inquiry(uidl):
    """KT mail with 작업 in the subject but no notice fields ('수작업 요청' inquiry)."""
    stored(uidl, headers.KT_NOTICE, "고객문의가 접수 되었습니다.", [b"Subject: s"])
    parse_pending()
    return Notice.objects.get(seen__uidl=uidl)


def test_looked_at_mail_can_be_taken_off_and_put_back(staff):
    x = inquiry("q1")
    assert "작업 공지 형식이 아닌 KT 메일 1건" in staff.get(reverse("review")).content.decode()

    staff.post(reverse("notices_dismiss"), {"ids": [x.pk], "actor": ""})
    x.refresh_from_db()
    assert x.dismissed_at is None  # a name is required

    response = staff.post(reverse("notices_dismiss"), {"ids": [x.pk], "actor": "홍길동"}, follow=True)
    page = response.content.decode()
    assert "메일 1건을 목록에서 뺐습니다" in page
    assert "작업 공지 형식이 아닌 KT 메일" not in page and "목록에서 뺀 메일 1건" in page and "홍길동" in page
    x.refresh_from_db()
    assert x.dismissed_by == "홍길동" and Notice.objects.filter(pk=x.pk).exists()  # nothing deleted

    staff.post(reverse("notices_dismiss"), {"ids": [x.pk], "undo": "1"})
    assert "작업 공지 형식이 아닌 KT 메일 1건" in staff.get(reverse("review")).content.decode()


def test_name_fields_show_who_and_the_example(staff):
    inquiry("q1")
    page = staff.get(reverse("review")).content.decode()
    assert "확인한 사람</label>" in page and 'placeholder="예: 홍길동"' in page


def test_purge_drops_what_is_older_than_the_period_only():
    master("a@example.invalid")
    stored("old", headers.KT_NOTICE, kt_text("a@example.invalid", schedule="2025. 01. 05 22:00 ~ 23:00"))
    stored("new", headers.KT_NOTICE, kt_text("a@example.invalid", schedule="2099. 01. 05 22:00 ~ 23:00"))
    parse_pending()
    prepare_notifications()
    long_ago = timezone.now() - timedelta(days=400)
    MessageSeen.objects.filter(uidl="old").update(first_seen_at=long_ago, gone_at=long_ago)
    MessageSeen.objects.filter(uidl="new").update(gone_at=timezone.now())
    old_work = Work.objects.get(starts_at__year=2025)
    WorkClosure.objects.create(work=old_work, closed_at=long_ago, closed_by="홍길동")
    box = MessageSeen.objects.first().mailbox
    ScanRun.objects.filter(pk=ScanRun.objects.create(mailbox=box).pk).update(started_at=long_ago)
    ScanRun.objects.create(mailbox=box)
    done = Suggestion.objects.create(kind="idea", body="x", status="done", handled_at=long_ago)
    Suggestion.objects.filter(pk=done.pk).update(created_at=long_ago)
    Suggestion.objects.create(kind="idea", body="still open")

    assert purge(365, dry_run=True)["works"] == 1 and Work.objects.count() == 2
    counts = purge(365)
    assert counts == {"works": 1, "notifications": 1, "mails": 1, "scan runs": 1, "suggestions": 1}
    assert list(Work.objects.values_list("starts_at__year", flat=True)) == [2099]
    assert Notification.objects.count() == 1 and NotificationEvent.objects.count() == 1
    assert list(MessageSeen.objects.values_list("uidl", flat=True)) == ["new"]
    assert ScanRun.objects.count() == 1 and list(Suggestion.objects.values_list("body", flat=True)) == ["still open"]
    assert not WorkClosure.objects.exists()


def test_purge_command_reports_counts(capsys):
    call_command("purge_old", "--dry-run")
    assert "would delete (older than 365 days): nothing" in capsys.readouterr().out


def kt(uidl, account, schedule):
    stored(uidl, headers.KT_NOTICE, kt_text(account, schedule=schedule))
    parse_pending()
    prepare_notifications()


def test_past_list_shows_one_month_and_unfinished_works_stay(staff):
    master("a@example.invalid")
    kt("k1", "a@example.invalid", "2099. 01. 05 22:00 ~ 23:00")
    kt("k2", "a@example.invalid", "2099. 02. 05 22:00 ~ 23:00")
    kt("k3", "a@example.invalid", "2099. 03. 05 22:00 ~ 23:00")
    w1, w2, w3 = Work.objects.order_by("starts_at")
    Work.objects.filter(pk=w1.pk).update(starts_at=timezone.now() - timedelta(days=40))  # done, too old
    Work.objects.filter(pk=w2.pk).update(starts_at=timezone.now() - timedelta(days=10))  # done, recent
    Work.objects.filter(pk=w3.pk).update(starts_at=timezone.now() - timedelta(days=60))  # never finished
    for w in (w1, w2):
        WorkClosure.objects.create(work=w, closed_at=timezone.now(), closed_by="홍길동")
    page = staff.get(reverse("dashboard")).content.decode()
    upcoming, _, past = page.partition("지난 작업 ")
    assert f'/works/{w3.pk}/' in upcoming
    assert f'/works/{w2.pk}/' in past and f'/works/{w1.pk}/' not in page


def test_partner_pages_mark_their_own_tab_and_link_their_own_kt_mail(staff):
    ch = PartnerChannel.objects.create(label="협력기관", accounts=AGENCY, sender="notice@partner.example.invalid")
    master("c@example.invalid")
    kt("k1", "c@example.invalid", "2099. 01. 05 22:00 ~ 23:00")  # the ordinary customer's mail came first
    kt("k2", AGENCY, "2099. 01. 05 22:00 ~ 23:00")
    work = Work.objects.get()
    agency_mail = Notice.objects.get(seen__uidl="k2")

    ordinary_mail = Notice.objects.get(seen__uidl="k1")
    page = staff.get(reverse("channel_work", args=[ch.pk, work.pk])).content.decode()
    kt_list = page.partition('class="kt-mails"')[2].partition("</ul>")[0]
    assert reverse("notice", args=[agency_mail.pk]) in kt_list
    assert reverse("notice", args=[ordinary_mail.pk]) not in kt_list
    current = f'href="{reverse("channel", args=[ch.pk])}" aria-current="page"'
    assert current in page and f'href="{reverse("dashboard")}" aria-current' not in page

    n = Notification.objects.get(account=AGENCY)
    detail = staff.get(reverse("notification", args=[n.pk])).content.decode()
    assert current in detail and "협력기관 탭" not in detail
    main = staff.get(reverse("work", args=[work.pk])).content.decode()
    assert f'href="{reverse("dashboard")}" aria-current="page"' in main
    kt_list = main.partition('class="kt-mails"')[2].partition("</ul>")[0]
    assert reverse("notice", args=[ordinary_mail.pk]) in kt_list
    assert reverse("notice", args=[agency_mail.pk]) not in kt_list  # 협력기관 stays on its own tab


def test_work_page_lists_every_kt_mail_and_folds_identical_twins(staff):
    master("a@example.invalid")
    master("b@example.invalid")
    kt("k1", "a@example.invalid", "2099. 01. 05 22:00 ~ 23:00")
    kt("k1b", "a@example.invalid", "2099. 01. 05 22:00 ~ 23:00")  # KT's duplicate to our second address
    kt("k2", "b@example.invalid", "2099. 01. 05 22:00 ~ 23:00")
    page = staff.get(reverse("work", args=[Work.objects.get().pk])).content.decode()
    kt_list = page.partition('class="kt-mails"')[2].partition("</ul>")[0]
    assert kt_list.count("<li>") == 2 and "같은 메일 2통" in kt_list and "예시회사" in kt_list
    n = Notification.objects.get(account="b@example.invalid")
    assert f'<tr data-href="{reverse("notification", args=[n.pk])}">' in page  # any cell of the row opens it


def test_수작업_요청_inquiries_are_not_kept(capsys):
    from mailcollect.headers import HeaderInfo, KT_OTHER, KT_SENDER, classify
    subject = "'수작업 요청' 서비스에 대한 고객문의가 접수 되었습니다."
    assert classify(HeaderInfo(subject, True, KT_SENDER, "", "", None, "", ())) == KT_OTHER
    assert classify(HeaderInfo("[kt cloud 작업 공지]", True, KT_SENDER, "", "", None, "", ())) == headers.KT_NOTICE

    # Stored before the rule: reclassify drops its header, body and parse, keeping the UIDL so it is not read again.
    seen = stored("q1", headers.KT_NOTICE, "고객문의가 접수 되었습니다.",
                  [f"From: kt cloud <{KT_SENDER}>".encode(), "Subject: 수작업 요청 고객문의가 접수 되었습니다".encode()])
    parse_pending()
    call_command("reclassify")
    assert "mail no longer kept, body and parse dropped: 1" in capsys.readouterr().out
    seen.refresh_from_db()
    assert seen.category == KT_OTHER and not Notice.objects.exists()
    assert not hasattr(MessageSeen.objects.get(pk=seen.pk), "body")
