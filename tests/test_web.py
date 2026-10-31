from datetime import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from customers.models import CustomerAccount
from mailcollect import headers
from mailcollect.models import Mailbox, Notice, ScanRun
from mailcollect.notice_store import parse_pending
from notifications.models import Notification
from notifications.service import match_copies, prepare_notifications
from tests.test_customers import GENERAL
from tests.test_notice_store import stored
from tests.test_notifications import copy_text, header, kt_text
from web import labels

pytestmark = pytest.mark.django_db
PAGES = ["dashboard", "calendar", "sent_mails", "review", "customers", "customer_new",
         "internal_accounts", "suggestions"]
FUTURE = "2099. 10. 24 22:00 ~ 10:00"


@pytest.fixture
def staff(client, django_user_model):
    user = django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True)
    client.force_login(user)
    return user


@pytest.fixture
def notification(staff):
    CustomerAccount.objects.create(kind="general", source="csv", account="a@example.invalid",
                                   email="owner@example.invalid", company="예시회사")
    stored("k1", headers.KT_NOTICE, kt_text("a@example.invalid", schedule=FUTURE))
    parse_pending()
    prepare_notifications()
    return Notification.objects.get()


def sent(n, uidl="c1"):
    """The 예시클라우드 mail for n arrives in the mailbox."""
    stored(uidl, headers.NC24_NOTICE, copy_text("a@example.invalid", schedule="2099.10.24(토) 22:00 ~ 10:00",
                                                number=n.number), header("owner@example.invalid"))
    parse_pending()
    match_copies()
    n.refresh_from_db()
    return n


@pytest.mark.parametrize("name", PAGES)
def test_pages_require_login(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302 and response.url.startswith(reverse("login"))


def test_login_page_renders(client):
    assert "로그인" in client.get(reverse("login")).content.decode()


@pytest.mark.parametrize("name", PAGES)
def test_pages_render_when_empty(client, staff, name):
    assert client.get(reverse(name)).status_code == 200


def test_no_internal_codes_or_old_words_on_pages(client, notification):
    old_words = ["원장", "초안", "사본", "해석", "account not in", "kt_notice", "needs_review"]
    for url in [reverse("dashboard"), reverse("work", args=[notification.work_id]),
                reverse("notification", args=[notification.pk]), reverse("review"), reverse("customers")]:
        page = client.get(url).content.decode()
        assert not [w for w in old_words if w in page], url


def test_dashboard_lists_upcoming_work(client, notification):
    page = client.get(reverse("dashboard")).content.decode()
    assert "KT Cloud 네트워크 개선 작업" in page and "새 메일 확인" in page
    assert "15분 넘게 메일을 확인하지 못했습니다" in page  # no scan has run in this test DB


def test_calendar_puts_the_work_on_its_start_day(client, notification):
    page = client.get(reverse("calendar"), {"m": "2099-10"}).content.decode()
    assert "2099년 10월" in page
    day_24 = page.split('<span aria-hidden="true">24</span>', 1)[1].split('<span aria-hidden="true">25</span>', 1)[0]
    assert "KT Cloud 네트워크 개선 작업" in day_24 and "22:00" in day_24
    assert client.get(reverse("calendar"), {"m": "bad"}).status_code == 200


def test_work_and_notification_pages(client, notification):
    n = notification
    work_page = client.get(reverse("work", args=[n.work_id])).content.decode()
    assert "예시회사" in work_page and "발송 대기" in work_page
    page = client.get(reverse("notification", args=[n.pk])).content.decode()
    assert 'value="owner@example.invalid"' in page
    assert "[예시클라우드] 작업 공지 안내" in page and n.number in page and "안내번호: CSP" not in page
    assert client.get(reverse("notice", args=[n.notice_id])).status_code == 200


def test_copy_is_recorded_with_the_typed_name_but_is_not_sending(client, staff, notification):
    n = notification
    response = client.post(reverse("notification_copied", args=[n.pk]), {"what": "body", "actor": "홍길동"})
    assert response.json() == {"status": "copied", "label": "작성 중"}
    event = n.events.order_by("-pk").first()
    assert (event.note, event.by, event.actor) == ("본문 복사", staff, "홍길동")


def test_staff_confirm_only_after_sending_and_needs_a_name(client, staff, notification):
    n = notification
    client.post(reverse("staff_confirm"), {"ids": [n.pk], "actor": "홍길동", "next": "/"})
    n.refresh_from_db()
    assert n.status == "draft_ready"  # nothing sent yet

    n = sent(n)
    assert n.status == "sent_copy_confirmed"
    response = client.post(reverse("staff_confirm"), {"ids": [n.pk], "actor": "", "next": "/"}, follow=True)
    assert "이름을 입력하세요" in response.content.decode()

    client.post(reverse("staff_confirm"), {"ids": [n.pk], "actor": "홍길동", "next": "/"})
    n.refresh_from_db()
    assert (n.status, n.staff_name, n.get_status_display()) == ("staff_confirmed", "홍길동", "담당자 확인")
    assert n.events.order_by("-pk").first().actor == "홍길동"

    client.post(reverse("staff_unconfirm", args=[n.pk]), {"actor": "홍길동"})
    n.refresh_from_db()
    assert (n.status, n.staff_name) == ("sent_copy_confirmed", "")


def test_bulk_staff_confirm_skips_unsent(client, staff, notification):
    sent(notification)
    stored("k2", headers.KT_NOTICE, kt_text("a@example.invalid", schedule="2099. 11. 01 01:00 ~ 02:00", title="다른 작업"))
    parse_pending()
    prepare_notifications()
    ids = list(Notification.objects.values_list("pk", flat=True))
    response = client.post(reverse("staff_confirm"), {"ids": ids, "actor": "이확인", "next": "/"}, follow=True)
    assert "1건을 담당자 확인" in response.content.decode() and "1건은" in response.content.decode()
    assert Notification.objects.filter(status="staff_confirmed").count() == 1


def test_later_mail_keeps_the_staff_check(client, staff, notification):
    n = sent(notification)
    client.post(reverse("staff_confirm"), {"ids": [n.pk], "actor": "홍길동", "next": "/"})
    n = sent(n, uidl="c2")  # a second copy (resend)
    assert (n.status, n.copies.count()) == ("staff_confirmed", 2)


def test_manual_record_then_mail_evidence(client, staff, notification):
    n = notification
    client.post(reverse("notification_sent", args=[n.pk]), {"note": "따로 보냄", "actor": "박담당"})
    n.refresh_from_db()
    assert (n.status, n.events.order_by("-pk").first().actor) == ("manual_sent_recorded", "박담당")
    assert sent(n).status == "sent_copy_confirmed"


def test_sent_mail_pages_show_what_was_actually_sent(client, notification):
    sent(notification)
    mail = Notice.objects.get(category="nc24_notice")
    listing = client.get(reverse("sent_mails")).content.decode()
    assert reverse("sent_mail", args=[mail.pk]) in listing and "예시회사" in listing
    page = client.get(reverse("sent_mail", args=[mail.pk])).content.decode()
    assert "실제로 나간 내용" in page and "KT Cloud에서 아래와 같이 작업을 진행할 예정입니다." in page
    assert "owner@example.invalid" in page
    assert client.get(reverse("sent_mail", args=[notification.notice_id])).status_code == 404  # KT notice, not sent mail


def test_scan_button_requests_once_then_waits(client, staff, settings, tmp_path):
    settings.CSP_SCAN = {**settings.CSP_SCAN, "request_path": str(tmp_path / "scan.request")}
    response = client.post(reverse("request_scan"), {"next": "/"})
    assert response.url == "/?requested=1" and (tmp_path / "scan.request").exists()

    box = Mailbox.objects.create(host="h", port=110, username="u")
    ScanRun.objects.create(mailbox=box, status="unchanged", finished_at=timezone.now())
    (tmp_path / "scan.request").unlink()
    response = client.post(reverse("request_scan"), {"next": "/"})
    assert response.url == "/" and not (tmp_path / "scan.request").exists()


def test_scan_button_never_redirects_off_site(client, staff, settings, tmp_path):
    settings.CSP_SCAN = {**settings.CSP_SCAN, "request_path": str(tmp_path / "scan.request")}
    assert client.post(reverse("request_scan"), {"next": "//evil.example/"}).url == "/?requested=1"


def test_customer_edit_and_search(client, staff):
    row = CustomerAccount.objects.create(kind="public", source="csv", account="gov_x@example.invalid",
                                         email="", company="기관")
    assert "gov_x@example.invalid" in client.get(reverse("customers"), {"q": "기관"}).content.decode()
    data = {"account": "gov_x@example.invalid", "userid": "", "name": "", "company": "기관",
            "manager": "담당", "email": "new@example.invalid", "phone": ""}
    response = client.post(reverse("customer_edit", args=[row.pk]), data)
    new = CustomerAccount.objects.get(active=True)
    assert response.url == reverse("customer_edit", args=[new.pk])
    assert (new.source, new.emails, new.edited_by) == ("web", ["new@example.invalid"], staff)
    assert "변경 이력" in client.get(response.url).content.decode()


def test_customer_new_prefills_account_and_creates_web_row(client, staff):
    assert 'value="missing@example.invalid"' in client.get(
        reverse("customer_new"), {"account": "missing@example.invalid"}).content.decode()
    client.post(reverse("customer_new"), {"kind": "general", "account": "missing@example.invalid",
                                          "company": "새 회사", "email": "m@example.invalid"})
    row = CustomerAccount.objects.get()
    assert (row.source, row.kind, row.emails, row.status) == ("web", "general", ["m@example.invalid"], "ok")


def test_customer_csv_download_follows_the_page_filters(client, staff):
    from customers.importer import import_csv
    import_csv(GENERAL.encode("utf-8-sig"), "general.csv", "general")
    CustomerAccount.objects.create(kind="public", source="web", account="gov@example.invalid", company="예시기관",
                                   email="a@example.invalid\nb@example.invalid")
    page = client.get(reverse("customers")).content.decode()
    assert "CSV 다운로드" in page and "CSV 가져오기" not in page
    response = client.get(reverse("customers_csv"), {"kind": "public"})
    assert "%EA%B3%B5%EA%B3%B5" in response["Content-Disposition"]  # 고객정보_공공_…
    text = response.content.decode("utf-8")
    assert text.startswith("\ufeff구분,계정,회사명") and text.count("\r\n") == 2  # header + the one 공공 row
    assert "a@example.invalid, b@example.invalid" in text  # one line per cell, for Excel
    everyone = client.get(reverse("customers_csv")).content.decode("utf-8")
    assert everyone.count("\r\n") == 1 + CustomerAccount.objects.filter(active=True).count()


def test_review_page_in_plain_korean(client, staff):
    stored("k1", headers.KT_NOTICE, kt_text("missing@example.invalid"))
    parse_pending()
    prepare_notifications()
    page = client.get(reverse("review")).content.decode()
    assert "missing@example.invalid" in page and "고객 정보에 없는 계정입니다" in page
    assert reverse("customer_new") + "?account=missing%40example.invalid" in page
    assert '<span class="count">1</span>' in client.get(reverse("dashboard")).content.decode()


def test_kt_mail_without_notice_fields_is_listed_apart(client, staff):
    stored("q1", headers.KT_NOTICE, "'수작업 요청' 서비스에 대한 고객문의가 접수 되었습니다.\n문의 내용을 확인해 주세요.")
    stored("k1", headers.KT_NOTICE, "ㅇ 작업내용 : 점검\nㅇ 작업일시 : 추후 공지")
    parse_pending()
    page = client.get(reverse("review")).content.decode()
    main, folded = page.split("작업 공지 형식이 아닌 KT 메일", 1)
    assert "작업 일시를 읽지 못했습니다" in main
    assert " 1건" in folded[:20]


def test_admin_has_no_groups(client, django_user_model):
    admin_user = django_user_model.objects.create_superuser("root-admin", password="pw-for-tests")
    client.force_login(admin_user)
    page = client.get("/admin/").content.decode()
    assert "/admin/auth/user/" in page and "/admin/auth/group/" not in page


def test_every_status_and_reason_has_korean_text():
    assert all(label and not label.isascii() for _, label in Notification.Status.choices)
    assert set(labels.CATEGORY) == set(headers.CATEGORIES)
