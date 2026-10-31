"""예시클라우드's own accounts: listed, never sent or checked."""
import pytest
from django.urls import reverse
from django.utils import timezone

from customers.models import InternalAccount
from mailcollect import headers
from mailcollect.notice_store import parse_pending
from notifications.models import Notification
from notifications.service import apply_internal_accounts, match_copies, prepare_notifications
from tests.test_notice_store import stored
from tests.test_notifications import copy_text, header, kt_text, master

pytestmark = pytest.mark.django_db
OWN = "ops@example.invalid"


def internal(account=OWN):
    return InternalAccount.objects.create(account_key=account, added_at=timezone.now())


def notice_for(account=OWN, uidl="k1"):
    stored(uidl, headers.KT_URGENT, kt_text(account))
    parse_pending()
    prepare_notifications()
    return Notification.objects.get(account=account)


def test_new_notice_for_an_internal_account_needs_no_customer_data():
    internal()
    n = notice_for()
    assert (n.status, n.review_reasons, n.get_status_display()) == ("internal", "", "사내 계정")


def test_marking_and_unmarking_moves_only_unsent_notices():
    n = notice_for()
    assert n.status == "needs_review"  # not in the customer master
    row = internal()
    assert apply_internal_accounts() == {"internal": 1}
    n.refresh_from_db()
    assert (n.status, n.review_reasons) == ("internal", "")

    row.active = False
    row.save()
    master(OWN)
    assert apply_internal_accounts() == {"internal removed": 1}
    n.refresh_from_db()
    assert (n.status, n.recipients) == ("draft_ready", ["owner@example.invalid"])


def test_sent_notice_keeps_its_evidence_when_the_account_becomes_internal():
    master(OWN)
    n = notice_for()
    stored("c1", headers.NC24_NOTICE, copy_text(OWN, number=n.number), header("owner@example.invalid"))
    parse_pending()
    match_copies()
    internal()
    assert apply_internal_accounts() == {}
    n.refresh_from_db()
    assert n.status == "sent_copy_confirmed"


def test_copy_for_an_internal_notice_is_logged_without_changing_it():
    internal()
    n = notice_for()
    stored("c1", headers.NC24_NOTICE, copy_text(OWN, number=n.number), header("owner@example.invalid"))
    parse_pending()
    match_copies()
    n.refresh_from_db()
    assert n.status == "internal" and n.copies.count() == 1


def test_pages(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    n = notice_for()
    review = client.get(reverse("review")).content.decode()
    assert OWN in review and "사내 계정</button>" in review and "안 쓰는 계정</button>" in review

    response = client.post(reverse("internal_accounts"), {"account": " OPS@example.invalid ", "note": "운영",
                                                          "actor": "홍길동", "next": "/review/"}, follow=True)
    assert "발송 전이던 안내 1건을 발송 대상에서 뺐습니다" in response.content.decode()
    n.refresh_from_db()
    assert n.status == "internal"
    row = InternalAccount.objects.get()
    assert (row.account_key, row.note, row.added_by) == (OWN, "운영", "홍길동")
    assert OWN not in client.get(reverse("review")).content.decode()  # no longer a missing customer

    page = client.get(reverse("internal_accounts")).content.decode()
    assert OWN in page and "KT Cloud 네트워크 개선 작업" in page
    work = client.get(reverse("work", args=[n.work_id])).content.decode()
    assert "사내 계정" in work
    assert "고객 안내를 보내지 않고" in client.get(reverse("notification", args=[n.pk])).content.decode()

    client.post(reverse("internal_remove", args=[row.pk]), {"actor": "홍길동"})
    row.refresh_from_db()
    n.refresh_from_db()
    assert (row.active, row.removed_by, n.status) == (False, "홍길동", "needs_review")
    assert "해제한 계정" in client.get(reverse("internal_accounts")).content.decode()


def test_rejects_a_value_that_is_not_an_account(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    response = client.post(reverse("internal_accounts"), {"account": "nope"}, follow=True)
    assert "이메일 형식" in response.content.decode() and not InternalAccount.objects.exists()


def test_unused_account_is_left_without_customer_data_and_not_sent(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    n = notice_for("gone@example.invalid")
    assert n.status == "needs_review"  # not in the customer master
    response = client.post(reverse("internal_accounts"), {"account": "gone@example.invalid", "kind": "unused",
                                                          "actor": "홍길동", "next": "/review/"}, follow=True)
    page = response.content.decode()
    assert "안 쓰는 계정으로 지정했습니다. 발송 전이던 안내 1건" in page and "gone@example.invalid" not in page
    n.refresh_from_db()
    assert (n.status, n.get_status_display(), n.review_reasons) == ("unused", "안 쓰는 계정", "")
    detail = client.get(reverse("notification", args=[n.pk])).content.decode()
    assert "안 쓰는 계정으로 지정돼" in detail

    # Switching kinds moves it along; a new KT notice for it is born unused.
    row = InternalAccount.objects.get()
    assert row.kind == "unused"
    client.post(reverse("internal_accounts"), {"account": "gone@example.invalid", "kind": "internal", "actor": "홍길동"})
    n.refresh_from_db()
    assert n.status == "internal"
    client.post(reverse("internal_accounts"), {"account": "gone@example.invalid", "kind": "unused", "actor": "홍길동"})
    stored("k2", headers.KT_COMPLETE, kt_text("gone@example.invalid"))
    parse_pending()
    prepare_notifications()
    assert Notification.objects.get(account="gone@example.invalid", kind="complete").status == "unused"
    page = client.get(reverse("internal_accounts")).content.decode()
    assert "안 쓰는 계정" in page and "gone@example.invalid" in page
    assert "gone@example.invalid" not in client.get(reverse("review")).content.decode()


# --- 사내 고객명: every account under a customer name (user, 2026-10-21) --------------------------------------

def company(name="(주)예시데이타", by="홍길동"):
    from customers.models import InternalCompany
    return InternalCompany.objects.create(name=name, key=InternalCompany.key_of(name), added_by=by,
                                          added_at=timezone.now())


def test_accounts_under_a_customer_name_become_internal_now_and_when_added_later():
    from notifications.service import apply_internal_lists
    master("a@example.invalid", company="(주)예시 데이타")  # spacing differs: still the name
    master("b@example.invalid", company="")
    n = notice_for("a@example.invalid")
    assert n.status == "draft_ready"
    company()
    counts = apply_internal_lists()
    assert counts["accounts marked"] == 1 and counts["internal"] == 1
    n.refresh_from_db()
    row = InternalAccount.objects.get(account_key="a@example.invalid")
    assert n.status == "internal" and row.note == "고객명 (주)예시데이타" and row.added_by == "홍길동"

    row.active = False  # a person takes one account off: the name never puts it back
    row.save()
    master("c@example.invalid", company="(주)예시데이타")  # the company DB adds another account later
    assert apply_internal_lists()["accounts marked"] == 1
    assert set(InternalAccount.keys()) == {"c@example.invalid"}


def test_the_page_adds_and_removes_a_customer_name(client, django_user_model):
    from customers.models import InternalCompany
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    master("a@example.invalid", company="예시데이타")
    page = client.post(reverse("internal_accounts"), {"company": " 예시데이타 ", "actor": "홍길동"}, follow=True)
    assert "계정 1개를 사내 계정으로" in page.content.decode() and "a@example.invalid" in InternalAccount.keys()
    row = InternalCompany.objects.get()
    client.post(reverse("internal_company_remove", args=[row.pk]), {"actor": "홍길동"})
    row.refresh_from_db()
    assert not row.active and "a@example.invalid" in InternalAccount.keys()  # marked accounts stay
