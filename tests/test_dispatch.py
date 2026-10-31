"""Sending through SENS without ever sending twice. SENS is a fake here: no mail ever leaves a test."""
import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from customers.models import PartnerChannel
from mailcollect import headers
from mailcollect.config import ConfigError
from mailcollect.headers import HeaderInfo
from mailcollect.models import MessageSeen
from mailcollect.notice_store import parse_pending
from notifications import dispatch, sens
from notifications.models import Dispatch, Notification, SendingSince
from notifications.service import match_copies, prepare_notifications
from tests.test_notice_store import stored
from tests.test_notifications import copy_text, header, kt_text, master

pytestmark = pytest.mark.django_db
ACCOUNT = "a@example.invalid"
LIVE = sens.SensConfig(mode="live", access_key="AK", secret_key="SK", service_id="ncp:mail:kr:1:p",
                       sender_address="system@example.invalid", sender_name="예시클라우드",
                       cc=("team@example.invalid",), bcc=("copies@example.invalid",))
TEST = sens.SensConfig(**{**LIVE.__dict__, "mode": "test", "test_to": ("tester@example.invalid",)})


class FakeSens:
    """Stands in for sens.Client: records what would have been sent and answers as told."""

    base = "/mail/v2/services/ncp:mail:kr:1:p/requests"

    def __init__(self):
        self.sent, self.fail, self.status, self.listed, self.bodies, self.n = [], [], {}, [], {}, 0
        self.lookups, self.lookup_fail = [], []

    def send(self, payload):
        if self.fail:
            raise self.fail.pop(0)
        self.n += 1
        rid = f"R{self.n}"
        self.sent.append(payload)
        self.bodies[rid] = payload["body"]
        return {"requestId": rid, "requestCount": 1}

    def request_status(self, request_id):
        return {"status": self.status.get(request_id, "SENDING")}

    def find_requests(self, since, until, recipient, title):
        if self.lookup_fail:
            raise self.lookup_fail.pop(0)
        self.lookups.append((recipient, title))
        return self.listed

    def _call(self, method, path, query=None, body=None):
        rid = path.split("/requests/")[1].split("/")[0]
        if path.endswith("/mails"):
            return {"mails": [{"mailId": "M1"}]}
        return {"body": self.bodies.get(rid)}


@pytest.fixture
def fake():
    return FakeSens()


def prepared(account=ACCOUNT, uidl="k1"):
    master(account)
    stored(uidl, headers.KT_NOTICE, kt_text(account, schedule="2099. 10. 24 22:00 ~ 23:00"))
    parse_pending()
    prepare_notifications()
    return Notification.objects.get(account=account)


def switch_on(mode="live", ago=timedelta(hours=1)):
    SendingSince.objects.create(mode=mode, since=timezone.now() - ago)


def later(minutes):
    return timezone.now() + timedelta(minutes=minutes)


# --- settings and the signature -----------------------------------------------------------------------------

def test_no_file_means_off_and_the_file_only_blocks_or_allows(tmp_path):
    assert sens.load_config(tmp_path / "absent.env") == sens.OFF
    env = tmp_path / "sens.env"
    env.write_text("NCP_ACCESS_KEY=a\nNCP_SECRET_KEY=s\nSENS_MAIL_SERVICE_ID=x\nMAIL_SENDER_ADDRESS=s@example.invalid\n"
                   "SEND_MODE=test\n", encoding="utf-8")
    assert sens.load_config(env).mode == "live"  # test, on or live: the page's ON/OFF decides
    env.write_text(env.read_text() + "MAIL_TEST_TO=Tester@Example.invalid\nMAIL_BCC=c@example.invalid; d@example.invalid\n"
                   "MAIL_CC=Team@example.invalid\n")
    cfg = sens.load_config(env)
    assert (cfg.mode, cfg.test_to, cfg.bcc) == ("live", ("tester@example.invalid",), ("c@example.invalid", "d@example.invalid"))
    assert cfg.cc == ("team@example.invalid",)
    assert "s\n" not in repr(cfg) and "secret" not in repr(cfg)
    env.write_text("SEND_MODE=sometimes\n")
    with pytest.raises(ConfigError, match="SEND_MODE"):
        sens.load_config(env)


def test_signature_follows_the_ncp_recipe():
    expected = base64.b64encode(hmac.new(b"SK", b"POST /mail/v2/services/x/requests\n1700000000000\nAK",
                                         hashlib.sha256).digest()).decode()
    assert sens.signature("SK", "POST", "/mail/v2/services/x/requests", "1700000000000", "AK") == expected


def test_client_sends_signed_json_and_sorts_out_errors():
    seen = {}

    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self.data).encode()

    def opener(request, timeout):
        seen.update(url=request.full_url, method=request.get_method(), headers=dict(request.header_items()),
                    body=json.loads(request.data), timeout=timeout)
        return Response({"requestId": "R1"})

    client = sens.Client(LIVE, opener=opener, clock=lambda: 1700000000.0)
    assert client.send({"title": "제목"})["requestId"] == "R1"
    assert seen["url"] == "https://sens.apigw.ntruss.com/mail/v2/services/ncp:mail:kr:1:p/requests"
    assert seen["headers"]["X-ncp-apigw-timestamp"] == "1700000000000" and seen["body"] == {"title": "제목"}
    assert seen["headers"]["X-ncp-apigw-signature-v2"] == sens.signature(
        "SK", "POST", "/mail/v2/services/ncp:mail:kr:1:p/requests", "1700000000000", "AK")

    assert sens.classify(401, "Unauthorized") == "global" and sens.classify(429, "ResourceQuotaExceeded") == "global"
    assert sens.classify(429, "ThrottlingExceeded") == "retry" and sens.classify(400, "InvalidParameter") == "permanent"
    assert sens.classify(500, "InternalServerError") == "unknown" and sens.classify(503, "") == "unknown"


# --- what is sent, and to whom ------------------------------------------------------------------------------

def test_a_new_notice_is_sent_once_with_the_in_house_cc_and_bcc_and_settles_by_senss_result(fake):
    switch_on()
    n = prepared()
    counts = dispatch.run(LIVE, fake)
    assert counts["live accepted"] == 1 and len(fake.sent) == 1
    payload = fake.sent[0]
    assert payload["title"] == n.subject and payload["individual"] is False and payload["advertising"] is False
    assert payload["recipients"] == [{"address": "owner@example.invalid", "type": "TO"},
                                     {"address": "team@example.invalid", "type": "CC"},
                                     {"address": "copies@example.invalid", "type": "BCC"}]
    assert payload["senderAddress"] == "system@example.invalid" and "<br />" in payload["body"]
    assert payload["body"].startswith(dispatch.MAIL_TEMPLATE.split("__CONTENT__")[0])  # the company mail form
    n.refresh_from_db()
    assert n.status == "send_requested" and n.events.filter(note__startswith="SENS 발송 접수").exists()

    assert dispatch.run(LIVE, fake)["live accepted"] == 0 and len(fake.sent) == 1  # never twice
    fake.status["R1"] = "COMPLETED"
    dispatch.run(LIVE, fake)
    n.refresh_from_db()
    assert n.status == "sent_copy_confirmed" and n.dispatches.get().state == "done"

    with pytest.raises(IntegrityError):  # the database itself refuses a second live attempt
        Dispatch.objects.create(notification=n, mode="live", state="sending", subject="s")


def test_waiting_notices_go_once_on_but_not_old_kt_mail_partners_or_problem_notices(fake):
    n_before = prepared("before@example.invalid", "k0")
    switch_on(ago=timedelta(seconds=0))  # switched on after it was made: it still goes (user, 2026-10-21)
    n_stale = prepared("stale@example.invalid", "k1")
    MessageSeen.objects.filter(uidl="k1").update(first_seen_at=timezone.now() - timedelta(days=2))
    PartnerChannel.objects.create(label="기관", accounts="agency@example.invalid", sender="p@partner.example.invalid")
    stored("k2", headers.KT_NOTICE, kt_text("agency@example.invalid", schedule="2099. 10. 24 22:00 ~ 23:00"))
    stored("k3", headers.KT_NOTICE, kt_text("nobody@example.invalid", schedule="2099. 10. 24 22:00 ~ 23:00"))
    parse_pending()
    prepare_notifications()
    assert dispatch.run(LIVE, fake)["live accepted"] == 1 and len(fake.sent) == 1
    n_before.refresh_from_db(), n_stale.refresh_from_db()
    assert n_before.status == "send_requested" and n_stale.status == "draft_ready"
    assert Notification.objects.get(account="nobody@example.invalid").status == "needs_review"


def test_a_copy_sent_from_outlook_first_means_nothing_is_sent(fake):
    switch_on()
    n = prepared()
    stored("c1", headers.NC24_NOTICE, copy_text(ACCOUNT, schedule="2099.10.24(토) 22:00 ~ 23:00"),
           header("owner@example.invalid"))
    parse_pending()
    match_copies()  # the scan matches copies before it sends
    n.refresh_from_db()
    assert n.status == "sent_copy_confirmed"
    assert dispatch.run(LIVE, fake)["live accepted"] == 0 and not fake.sent


# --- failures -------------------------------------------------------------------------------------------------

def test_refused_attempts_are_retried_after_5_minutes_and_the_third_gives_up(fake):
    switch_on()
    n = prepared()
    fake.fail = [sens.SensError("retry", "ConnectionRefusedError") for _ in range(3)]
    dispatch.run(LIVE, fake)
    n.refresh_from_db()
    assert (n.status, n.send_failures) == ("draft_ready", 1)
    assert dispatch.run(LIVE, fake, now=later(2))["live failed"] == 0  # too soon
    dispatch.run(LIVE, fake, now=later(6))
    dispatch.run(LIVE, fake, now=later(12))
    n.refresh_from_db()
    assert (n.status, n.send_failures) == ("send_failed", 3) and "sending failed 3 times" in n.review_reasons
    assert dispatch.run(LIVE, fake, now=later(30))["live accepted"] == 0 and not fake.sent

    assert dispatch.request_send(n, "홍길동")  # 발송하기
    n.refresh_from_db()
    assert (n.status, n.send_failures, n.review_reasons) == ("draft_ready", 0, "")
    assert dispatch.run(LIVE, fake, now=later(30))["live accepted"] == 1
    assert n.dispatches.get(state="accepted").by == "홍길동"


def test_a_rejected_mail_gives_up_at_once_and_a_key_problem_stops_the_run_without_counting(fake):
    switch_on()
    a, b = prepared(), prepared("b@example.invalid", "k2")
    fake.fail = [sens.SensError("global", "HTTP 401 Unauthorized", 401)]
    assert dispatch.run(LIVE, fake)["live global"] == 1 and not fake.sent
    a.refresh_from_db(), b.refresh_from_db()
    assert a.send_failures == b.send_failures == 0 and not b.dispatches.exists()  # stopped after the first
    assert dispatch.last_global_error().error == "HTTP 401 Unauthorized"

    fake.fail = [sens.SensError("permanent", "HTTP 400 InvalidParameter", 400)]
    dispatch.run(LIVE, fake, now=later(6))
    a.refresh_from_db()
    assert a.status == "send_failed" and "SENS rejected the mail" in a.review_reasons
    assert Notification.objects.get(pk=b.pk).status == "send_requested"


def test_an_unanswered_attempt_is_looked_up_never_resent_blindly(fake):
    switch_on()
    n = prepared()
    fake.fail = [sens.SensError("unknown", "TimeoutError: timed out")]
    assert dispatch.run(LIVE, fake)["live unknown"] == 1
    assert dispatch.run(LIVE, fake, now=later(1))["live accepted"] == 0 and not fake.sent  # blocked while unknown

    # SENS did take it: a request whose body is the one sent is adopted, nothing is sent again,
    # even though the draft changed in between (the digest kept at sending is compared, not today's draft)
    fake.bodies["X9"] = dispatch.build(LIVE, n)[-1]["body"]
    n.body += "\n담당자 수정"
    n.save()
    fake.listed = [{"requestId": "X9", "status": "SENDING"}]
    counts = dispatch.run(LIVE, fake, now=later(10))
    n.refresh_from_db()
    assert counts["unknown adopted"] == 1 and n.status == "send_requested" and not fake.sent
    assert n.dispatches.get().request_id == "X9"


def test_an_unanswered_attempt_sens_never_saw_is_retried_and_another_notices_request_is_not_taken(fake):
    switch_on()
    n = prepared()
    fake.fail = [sens.SensError("unknown", "HTTP 500 InternalServerError", 500)]
    dispatch.run(LIVE, fake)
    fake.bodies["Y1"] = "<div>다른 작업의 안내</div>"  # same customer and title, another notice's mail
    fake.listed = [{"requestId": "Y1", "status": "COMPLETED"}]
    dispatch.run(LIVE, fake, now=later(3))
    n.refresh_from_db()
    assert n.send_failures == 1 and n.dispatches.get().state == "failed"  # Y1 is not taken for ours
    # but it is a mail to this customer with this subject the site did not send: rule 6 holds the retry
    assert dispatch.run(LIVE, fake, now=later(9))["live held"] == 1 and not fake.sent
    fake.listed = []
    assert dispatch.run(LIVE, fake, now=later(15))["live accepted"] == 0  # unchanged: n is now 발송 실패


def test_senss_own_failure_after_acceptance_is_not_retried(fake):
    switch_on()
    a, b = prepared(), prepared("b@example.invalid", "k2")
    dispatch.run(LIVE, fake)
    fake.status.update({"R1": "FAILED", "R2": "PARTIAL_FAILED"})
    dispatch.run(LIVE, fake, now=later(6))
    a.refresh_from_db(), b.refresh_from_db()
    assert a.status == "send_failed" and "SENS could not deliver" in a.review_reasons
    assert b.status == "sent_copy_candidate" and "some recipients failed (SENS)" in b.review_reasons
    assert dispatch.run(LIVE, fake, now=later(30))["live accepted"] == 0 and len(fake.sent) == 2


def test_the_draft_fills_the_company_form_line_by_line_escaped():
    body = dispatch.html("안녕하세요.\n<점검> & 안내\n")
    head, tail = dispatch.MAIL_TEMPLATE.split("__CONTENT__")
    assert body == head + "안녕하세요.<br />\n&lt;점검&gt; &amp; 안내<br />\n" + tail
    assert "<body" in head and "</body>" in tail


def test_a_mail_sent_by_hand_through_sens_holds_the_notice_until_a_person_sends_it(fake):
    switch_on()
    n = prepared()
    fake.listed = [{"requestId": "H1", "status": "COMPLETED"}, {"requestId": "H0", "status": "FAILED"}]
    assert dispatch.run(LIVE, fake)["live held"] == 1 and not fake.sent
    assert fake.lookups == [("owner@example.invalid", n.subject)]  # the real recipient and subject
    n.refresh_from_db()
    assert n.status == "send_failed" and "SENS has the same mail" in n.review_reasons
    assert n.dispatches.get().state == "held" and n.dispatches.get().request_id == "H1"  # a failed one is no send
    assert dispatch.run(LIVE, fake, now=later(10))["live held"] == 0  # waits for a person

    assert dispatch.request_send(n, "홍길동")  # it was another notice's mail: the person sends it
    assert dispatch.run(LIVE, fake, now=later(11))["live accepted"] == 1 and len(fake.sent) == 1
    n.refresh_from_db()
    assert n.status == "send_requested" and "SENS has the same mail" not in n.review_reasons


def test_a_newer_hand_sent_mail_holds_again_after_send_and_a_failed_lookup_sends_nothing(fake):
    switch_on()
    n = prepared()
    fake.listed = [{"requestId": "H1", "status": "COMPLETED"}]
    dispatch.run(LIVE, fake)
    dispatch.request_send(Notification.objects.get(pk=n.pk), "홍길동")
    fake.listed = [{"requestId": "H1", "status": "COMPLETED"}, {"requestId": "H2", "status": "READY"}]
    assert dispatch.run(LIVE, fake, now=later(1))["live held"] == 1 and not fake.sent

    dispatch.request_send(Notification.objects.get(pk=n.pk), "홍길동")
    fake.lookup_fail = [sens.SensError("unknown", "TimeoutError: timed out")]
    assert dispatch.run(LIVE, fake, now=later(2))["live failed"] == 1 and not fake.sent
    d = n.dispatches.order_by("-pk").first()
    assert d.state == "failed" and d.error_kind == "retry" and d.error.startswith("발송 전 확인 실패")
    assert dispatch.run(LIVE, fake, now=later(8))["live accepted"] == 1  # 5 minutes later, H1 and H2 passed over


# --- test mode ------------------------------------------------------------------------------------------------

def test_test_mode_mails_the_test_address_only_once_and_leaves_the_notice_alone(fake):
    switch_on("test")
    n = prepared()
    dispatch.run(TEST, fake)
    payload = fake.sent[0]
    assert payload["recipients"] == [{"address": "tester@example.invalid", "type": "TO"}]  # no customer, no CC or BCC
    assert payload["title"] == "[테스트] " + n.subject and "실제 받는 사람: owner@example.invalid" in payload["body"]
    n.refresh_from_db()
    assert n.status == "draft_ready"
    dispatch.run(TEST, fake, now=later(10))
    assert len(fake.sent) == 1

    # its copy in the collected mailbox is never taken for a customer send
    info = HeaderInfo(subject=payload["title"], subject_ok=True, from_addr="system@example.com", from_display="",
                      date_raw="", date=None, message_id="", errors=())
    assert headers.classify(info) == headers.OTHER


def test_the_page_lists_attempts_and_has_no_test_button(client, django_user_model, monkeypatch, fake):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    n = prepared()
    monkeypatch.setattr(sens, "load_config", lambda path=None: TEST)
    dispatch.run(TEST, fake)
    page = client.get(reverse("notification", args=[n.pk])).content.decode()
    assert "자동 발송" in page and "테스트 접수됨" in page and "테스트 발송" not in page