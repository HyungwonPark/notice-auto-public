"""별도 발송 drafts in the partner's own wording, and mails shown as their HTML."""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from customers.models import PartnerChannel
from mailcollect import headers
from mailcollect.models import MessageBody, MessageHeader, Notice
from mailcollect.notice_store import parse_pending
from notifications import drafts
from notifications.models import Notification
from notifications.service import prepare_notifications
from tests.test_notice_store import stored
from tests.test_notifications import kt_text

pytestmark = pytest.mark.django_db
AGENCY = "agency@example.invalid"
TITLE = "VM 라이브 마이그레이션 작업"


@pytest.fixture
def channel():
    return PartnerChannel.objects.create(
        label="기관", accounts=AGENCY, sender="ops@partner.example.invalid",
        mail_to="a@agency.example.invalid;b@agency.example.invalid\nc@agency.example.invalid",
        mail_cc="협력사 운영 <ops@partner.example.invalid>", mail_bcc="copy@example.invalid",
        subject_prefix="[협력사] 기관", greeting="안녕하세요.\n협력사 클라우드 운영팀 입니다.")


def kt(uidl, category=headers.KT_NOTICE, text=None, when=None):
    stored(uidl, category, text or kt_text(AGENCY, schedule="2099. 10. 19 00:00 ~ 06:00", title=TITLE))
    if when:
        from mailcollect.models import MessageSeen
        MessageHeader.objects.create(seen=MessageSeen.objects.get(uidl=uidl), raw=b"x", subject="s", date=when)
    parse_pending()
    prepare_notifications()


def test_notice_draft_in_the_partners_wording(channel):
    kt("k1")
    n = Notification.objects.get()
    assert n.subject == f"[협력사] 기관 kt cloud {TITLE} 공지 안내"
    lines = n.body.split("\n")
    assert lines[:7] == ["안녕하세요.", "협력사 클라우드 운영팀 입니다.", "",
                         f"kt cloud로부터 2099.10.19(월) 00:00 ~ 06:00 일정 내 {TITLE}이 예정되어 안내드립니다.", "",
                         "자세한 사항은 하기 내용 참고 부탁드리겠습니다.", ""]
    assert "-" * 83 not in n.body
    # KT's own 감사합니다 stays with the KT text, the partner's follows it (its mails box the KT text)
    assert n.body.split("\n")[-3:] == ["감사합니다.", "", "감사합니다."]
    box = drafts.partner_html(n.body)
    before, _, rest = box.partition("<table")
    inside, _, after = rest.partition("</table>")
    assert "협력사 클라우드 운영팀 입니다." in before and "border:1px solid #cccccc" in inside
    assert "○ 대상계정명" in inside and after.count("감사합니다.") == 1 and "<div><br></div>" in inside
    assert drafts.partner_html("다른 형식의 본문") is None


def test_second_send_is_a_재안내_and_completion_uses_kts_quoted_name(channel):
    t0 = timezone.now() - timedelta(days=5)
    kt("k1", when=t0)
    kt("k2", when=t0 + timedelta(days=4))
    second = Notification.objects.get(round=2)
    assert second.subject.endswith("공지 재안내") and "예정되어 재안내드립니다." in second.body

    done = kt_text(AGENCY, schedule="2099. 10. 19 00:00 ~ 06:00", title=TITLE).replace(
        "안녕하세요 kt cloud 입니다.", "안녕하세요 kt cloud 입니다.\n고객님께 공지 드렸던 '공공 클라우드 개선 작업'이 완료 되었습니다.")
    kt("k3", headers.KT_COMPLETE, done)
    n = Notification.objects.get(kind="complete")
    assert n.subject == "[협력사] 기관 kt cloud 공공 클라우드 개선 작업 완료 공지 안내"
    assert "kt cloud로부터 금일 예정되어 있던 공공 클라우드 개선 작업 완료 공지가 수신되어 안내드립니다." in n.body


def test_without_a_template_the_partner_card_stays(client, django_user_model):
    PartnerChannel.objects.create(label="기관", accounts=AGENCY, sender="ops@partner.example.invalid")
    kt("k1")
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    page = client.get(reverse("notification", args=[Notification.objects.get().pk])).content.decode()
    assert "별도 발송" in page and 'id="draft-from"' not in page


def test_partner_page_shows_from_to_cc_bcc_with_checks(client, django_user_model, channel):
    kt("k1")
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    n = Notification.objects.get()
    page = client.get(reverse("notification", args=[n.pk])).content.decode()
    assert 'id="draft-from"' in page and 'value="ops@partner.example.invalid"' in page
    assert page.count("확인 필수") >= 2 and "받는 사람 (3명)" in page
    assert "a@agency.example.invalid; b@agency.example.invalid; c@agency.example.invalid" in page
    assert "copy@example.invalid" in page and "협력사 운영 &lt;ops@partner.example.invalid&gt;" in page
    assert "본문 복사 (표 포함)" in page and '<template id="draft-body-html">' in page
    assert 'class="draft-preview"' in page and "&lt;table" not in page.partition('<template id="draft-body-html">')[2][:300]
    client.post(reverse("notification_copied", args=[n.pk]), {"what": "from"})
    assert n.events.filter(note="보내는 사람 복사").exists()


def test_particle_follows_the_last_syllable():
    assert drafts._subject_particle("점검 작업") == "이" and drafts._subject_particle("서버 교체") == "가"


def test_mail_pages_show_the_html_in_a_sealed_frame(client, django_user_model):
    seen = stored("k1", headers.KT_NOTICE, kt_text(AGENCY))
    MessageBody.objects.filter(seen=seen).update(
        html='<table><tr><td>작업일시</td></tr></table><img src="https://tracker.example.invalid/p.gif"><script>x()</script>')
    parse_pending()
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    page = client.get(reverse("notice", args=[Notice.objects.get().pk])).content.decode()
    frame = page.partition("<iframe")[2].partition("</iframe>")[0]
    assert 'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"' in frame
    assert "allow-same-origin" not in frame  # its own origin: no reach into this site
    assert "default-src &#x27;none&#x27;" in frame and "img-src http: https: data:" in frame
    nonce = frame.partition("script-src &#x27;nonce-")[2].partition("&#x27;")[0]
    assert len(nonce) > 10 and frame.count(nonce) == 2  # the policy and the one fitting script
    assert "&lt;script&gt;x()" in frame  # the mail's own script: no nonce, so it never runs
    assert "&lt;table&gt;" in frame and "<table>" not in frame  # escaped into srcdoc, not into this page
    assert "텍스트로 보기" in page
