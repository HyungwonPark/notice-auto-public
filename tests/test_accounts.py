"""관리 계정 (User.is_staff) and the shared sign-in (user, 2026-10-22): only the 관리 계정 changes settings, customers
and recipient groups; the shared sign-in sees those pages read-only and keeps the daily work."""
import pytest
from django.urls import reverse

from customers.models import CustomerAccount, PartnerChannel, PartnerGroup
from notifications import dispatch
from notifications.models import SendSetting

pytestmark = pytest.mark.django_db


@pytest.fixture
def shared(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("examplecloud", password="pw-for-tests"))
    return client


def test_the_shared_sign_in_sees_but_cannot_change(shared):
    page = shared.get(reverse("config")).content.decode()
    assert "보기 전용" in page and 'value="send_mode"' not in page and 'value="kt_save"' not in page
    assert "KT 공지 양식" not in page  # the form itself is the 관리 계정's (user, 2026-10-22)
    response = shared.post(reverse("config"), {"action": "send_mode", "mode": "live", "actor": "홍길동"}, follow=True)
    assert "관리 계정만 바꿀 수 있습니다." in response.content.decode() and not SendSetting.objects.exists()
    assert dispatch.chosen_mode() == "off"

    ch = PartnerChannel.objects.create(label="기관", accounts="a@example.invalid", sender="p@partner.example.invalid")
    group = PartnerGroup.objects.create(channel=ch, name="홈페이지", mail_to="w@example.invalid", servers="web-01")
    page = shared.get(reverse("channel_groups", args=[ch.pk])).content.decode()
    assert "홈페이지" in page and "빼기" not in page and "그룹 추가" not in page
    shared.post(reverse("channel_groups", args=[ch.pk]), {"action": "retire", "group": group.pk, "actor": "홍길동"})
    group.refresh_from_db()
    assert group.active

    row = CustomerAccount.objects.create(kind="general", account="c@example.invalid", email="o@example.invalid")
    page = shared.get(reverse("customer_edit", args=[row.pk])).content.decode()
    assert "o@example.invalid" in page and "고객 정보 저장" not in page
    shared.post(reverse("customer_edit", args=[row.pk]), {"account": "c@example.invalid", "email": "x@example.invalid"})
    assert CustomerAccount.objects.get(active=True, account="c@example.invalid").email == "o@example.invalid"
    assert shared.get(reverse("customer_new")).status_code == 302
    assert "고객 추가" not in shared.get(reverse("customers")).content.decode()
    response = shared.post(reverse("internal_accounts"), {"account": "c@example.invalid", "kind": "internal",
                                                          "actor": "홍길동"})
    assert response.status_code == 302 and "해제" not in shared.get(reverse("internal_accounts")).content.decode()


def test_a_removed_group_comes_back(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("admin", password="pw-for-tests", is_staff=True))
    ch = PartnerChannel.objects.create(label="기관", accounts="a@example.invalid", sender="p@partner.example.invalid")
    group = PartnerGroup.objects.create(channel=ch, name="홈페이지", mail_to="w@example.invalid", servers="web-01")
    url = reverse("channel_groups", args=[ch.pk])
    client.post(url, {"action": "retire", "group": group.pk, "actor": "홍길동"})
    assert "뺀 그룹" in client.get(url).content.decode()
    client.post(url, {"action": "restore", "group": group.pk, "actor": "홍길동"})
    group.refresh_from_db()
    assert group.active
