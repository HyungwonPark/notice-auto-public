"""A customer upload or edit re-judges notifications that are not settled yet."""
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from customers.models import CustomerAccount
from mailcollect import headers
from mailcollect.notice_store import parse_pending
from notifications.models import Notification
from notifications.service import match_copies, prepare_notifications, refresh_from_master
from tests.test_customers import HEADER
from tests.test_notice_store import stored
from tests.test_notifications import copy_text, header, kt_text, master

pytestmark = pytest.mark.django_db
ACCOUNT = "a@example.invalid"
CSV = f"{HEADER}\r\n{ACCOUNT},u1,,예시회사,담당,owner@example.invalid,010-0000-0001\r\n".encode()


@pytest.fixture
def staff(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))


def drafted(account=ACCOUNT, uidl="k1", text=None):
    stored(uidl, headers.KT_NOTICE, text or kt_text(account))
    parse_pending()
    prepare_notifications()
    return Notification.objects.get(account=account)


def test_adding_a_missing_customer_moves_its_notice_to_ready(client, staff):
    n = drafted()
    assert (n.status, n.review_reasons) == ("needs_review", "account not in customer master")
    page = client.post(reverse("customer_new"), {"kind": "general", "account": ACCOUNT, "company": "예시회사",
                                                 "email": "owner@example.invalid"}, follow=True).content.decode()
    assert "확인 필요 → 발송 대기 1건" in page
    n.refresh_from_db()
    assert (n.status, n.review_reasons, n.recipients, n.display_name) == (
        "draft_ready", "", ["owner@example.invalid"], "예시회사")
    assert n.events.order_by("-pk").first().note == "고객 정보 반영"
    assert ACCOUNT not in client.get(reverse("review")).content.decode()


def test_edit_that_removes_the_email_sends_it_back_to_review(client, staff):
    row = master(ACCOUNT)
    n = drafted()
    assert n.status == "draft_ready"
    client.post(reverse("customer_edit", args=[row.pk]), {"account": ACCOUNT, "company": "예시회사", "email": ""})
    n.refresh_from_db()
    assert (n.status, n.review_reasons, n.recipients) == ("needs_review", "no email address in customer master", [])


def test_other_reasons_keep_it_in_review():
    text = kt_text(ACCOUNT).replace(f"○ 대상계정명 : {ACCOUNT}", f"○ 대상계정명 : {ACCOUNT}, b@example.invalid")
    n = drafted(text=text)
    master(ACCOUNT)
    refresh_from_master()
    n.refresh_from_db()
    assert n.status == "needs_review"
    assert n.review_reasons == "notice lists several accounts; check the quote"


def test_candidate_is_confirmed_once_the_master_has_the_address_the_mail_went_to():
    master(ACCOUNT, email="old@example.invalid")
    n = drafted()
    stored("c1", headers.NC24_NOTICE, copy_text(ACCOUNT), header("new@example.invalid"))
    parse_pending()
    match_copies()
    n.refresh_from_db()
    assert n.status == "sent_copy_candidate"

    CustomerAccount.objects.get(account=ACCOUNT).edit(None, email="new@example.invalid")
    assert refresh_from_master()["sent_copy_candidate->sent_copy_confirmed"] == 1
    n.refresh_from_db()
    assert (n.status, n.review_reasons) == ("sent_copy_confirmed", "")


def test_settled_notifications_are_left_alone():
    n = drafted()
    Notification.objects.filter(pk=n.pk).update(status="staff_confirmed")
    master(ACCOUNT)
    assert refresh_from_master() == {}


def test_import_command_reports_what_moved(tmp_path):
    drafted()
    path = tmp_path / "general.csv"
    path.write_bytes(CSV)
    out = StringIO()
    call_command("import_customers", str(path), "--kind", "general", stdout=out)
    assert "notifications: needs_review->draft_ready=1" in out.getvalue()
