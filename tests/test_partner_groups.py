"""별도 발송 받는 사람 그룹: one notice per system whose servers KT names (user, 2026-10-21)."""
import pytest
from django.urls import reverse

from customers.models import PartnerChannel, PartnerGroup, server_key
from mailcollect import headers
from mailcollect.models import MessageHeader
from mailcollect.notice_store import parse_pending
from notifications.models import Notification
from notifications.service import NOT_IN_GROUP, match_copies, prepare_notifications, record_copied
from tests.test_notice_store import stored
from tests.test_notifications import kt_text

pytestmark = pytest.mark.django_db
AGENCY = "agency@example.invalid"
PARTNER = "ops@partner.example.invalid"
FUTURE = "2099. 10. 24 22:00 ~ 23:00"


@pytest.fixture
def channel():
    ch = PartnerChannel.objects.create(
        label="기관", accounts=AGENCY, sender=PARTNER, mail_to="all1@agency.example.invalid\nall2@agency.example.invalid",
        mail_cc=f"협력사 운영 <{PARTNER}>", mail_bcc="copy@example.invalid", subject_prefix="[협력사] 기관",
        greeting="안녕하세요.\n협력사 운영팀 입니다.", server_aliases="webdb-dev = dev-web-db")
    PartnerGroup.objects.create(channel=ch, name="공통 인프라", mail_to="infra@agency.example.invalid",
                                mail_cc="infra-lead@agency.example.invalid", servers="infra-01\ninfra-02")
    PartnerGroup.objects.create(channel=ch, name="홈페이지", mail_to="web@agency.example.invalid",
                                servers="web-was\ndev-web-db\nshared-db")
    PartnerGroup.objects.create(channel=ch, name="민원", mail_to="civil@agency.example.invalid", servers="shared-db")
    return ch


def kt(uidl, servers, category=headers.KT_NOTICE, title="VM 라이브 마이그레이션 작업"):
    stored(uidl, category, kt_text(AGENCY, schedule=FUTURE, title=title).replace("web-01", servers))
    parse_pending()
    prepare_notifications()


def groups_of():
    return sorted((n.group.name if n.group else "전체", n.status) for n in Notification.objects.select_related("group"))


def test_server_names_compare_loosely():
    assert server_key(" EcoAS-was01 ") == server_key("ecoaswas01") == "ecoaswas01"


def test_one_notice_per_group_and_a_shared_server_reaches_each_of_its_groups(channel):
    kt("k1", "infra01, shared-db")  # KT writes infra-01 without its hyphen
    assert groups_of() == [("공통 인프라", "draft_ready"), ("민원", "draft_ready"), ("홈페이지", "draft_ready")]
    numbers = set(Notification.objects.values_list("number", flat=True))
    assert len(numbers) == 3  # each has its own 안내번호; the body is the same


def test_an_alias_or_no_server_or_an_unknown_one(channel):
    kt("k1", "webdb-dev")
    assert groups_of() == [("홈페이지", "draft_ready")]
    Notification.objects.all().delete()
    kt("k2", "", title="스토리지 점검 작업")  # no 대상서버명 at all: the whole list, nothing to fix
    kt("k3", "web-was, mystery-01", title="네트워크 점검 작업")  # a server outside the groups: whole list, flagged
    rows = {n.notice.seen.uidl: n for n in Notification.objects.select_related("notice__seen")}
    assert rows["k2"].group is None and rows["k2"].status == "draft_ready"
    assert rows["k3"].group is None and rows["k3"].status == "needs_review" and NOT_IN_GROUP in rows["k3"].review_reasons


def test_putting_the_server_in_a_group_splits_the_untouched_notice(channel, client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    kt("k1", "web-was, mystery-01")
    whole = Notification.objects.get()
    page = client.get(reverse("notification", args=[whole.pk])).content.decode()
    assert "그룹에 없는 서버" in page and "mystery-01" in page
    page = client.get(reverse("channel_groups", args=[channel.pk])).content.decode()
    assert "mystery-01" in page and "공통 인프라" in page
    group = PartnerGroup.objects.get(name="민원")
    response = client.post(reverse("channel_groups", args=[channel.pk]), {
        "action": "save", "group": group.pk, "name": "민원", "mail_to": "civil@agency.example.invalid",
        "mail_cc": "", "servers": "shared-db\nmystery-01", "actor": "홍길동"}, follow=True)
    assert "새로 만들었습니다" in response.content.decode()
    assert groups_of() == [("민원", "draft_ready"), ("홈페이지", "draft_ready")]
    assert not Notification.objects.filter(pk=whole.pk).exists()


def test_a_notice_someone_copied_keeps_its_recipients(channel, django_user_model):
    kt("k1", "web-was, mystery-01")
    whole = Notification.objects.get()
    record_copied(whole, django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True), "본문", "홍길동")
    PartnerGroup.objects.filter(name="민원").update(servers="shared-db\nmystery-01")
    prepare_notifications()
    assert groups_of() == [("전체", "needs_review")]  # copied: it stays as it was


def test_the_page_shows_the_groups_addresses_and_the_channel_cc(channel, client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    kt("k1", "infra-01")
    n = Notification.objects.get()
    page = client.get(reverse("notification", args=[n.pk])).content.decode()
    assert "infra@agency.example.invalid" in page and "all1@agency.example.invalid" not in page
    assert "infra-lead@agency.example.invalid" in page and PARTNER in page  # group CC and the channel's own
    assert "기관 공통 인프라" in page


def partner_copy(uidl, to):
    text = "kt cloud로부터 받은 공지를 전달드립니다.\n" + kt_text(AGENCY, schedule=FUTURE)
    stored(uidl, headers.PARTNER_NOTICE, text, [f"From: 협력사 <{PARTNER}>".encode(), f"To: {to}".encode(), b"Subject: s"])
    MessageHeader.objects.filter(seen__uidl=uidl).update(from_addr=PARTNER)
    parse_pending()
    return match_copies()


def test_a_partner_mail_confirms_the_groups_it_was_sent_to(channel):
    kt("k1", "infra-01, web-was")
    partner_copy("p1", "web@agency.example.invalid")
    assert groups_of() == [("공통 인프라", "draft_ready"), ("홈페이지", "sent_copy_confirmed")]
    partner_copy("p2", "infra@agency.example.invalid, someone@agency.example.invalid")
    assert groups_of() == [("공통 인프라", "sent_copy_confirmed"), ("홈페이지", "sent_copy_confirmed")]


def test_group_form_checks_and_retiring(channel, client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    url = reverse("channel_groups", args=[channel.pk])
    page = client.post(url, {"action": "save", "name": "홈페이지", "mail_to": "not mail", "servers": "",
                             "actor": "홍길동"}).content.decode()
    assert "같은 이름의 그룹" in page and "메일 주소가 아닌 줄" in page and "서버를 한 대 이상" in page
    assert PartnerGroup.objects.count() == 3
    client.post(url, {"action": "save", "name": "새 그룹", "mail_to": "n@agency.example.invalid", "servers": "x-1",
                      "actor": ""})
    assert not PartnerGroup.objects.filter(name="새 그룹").exists()  # no name: refused
    client.post(url, {"action": "aliases", "aliases": "a-1 = b-1\n잘못된 줄", "actor": "홍길동"})
    assert PartnerChannel.objects.get().server_aliases == "webdb-dev = dev-web-db"
    client.post(url, {"action": "aliases", "aliases": "a-1 = b-1", "actor": "홍길동"})
    assert PartnerChannel.objects.get().aliases() == {"a1": "b1"}
    group = PartnerGroup.objects.get(name="민원")
    client.post(url, {"action": "retire", "group": group.pk, "actor": "홍길동"})
    group.refresh_from_db()
    assert not group.active and group.updated_by == "홍길동"
