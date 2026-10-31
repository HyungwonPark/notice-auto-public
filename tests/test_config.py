"""환경 설정: the 자동 발송 switch under sens.env's cap, and the KT 공지 양식 that replaces fixed parsing rules."""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from mailcollect import headers, ktform
from mailcollect.headers import HeaderInfo, classify
from mailcollect.models import MessageHeader, MessageSeen, Notice
from mailcollect.notices import parse_notice
from notifications import dispatch, drafts
from notifications.models import Notification, SendingSince
from tests.test_notice_store import stored
from tests.test_notifications import kt_text, master

pytestmark = pytest.mark.django_db
ENV = ("NCP_ACCESS_KEY=a\nNCP_SECRET_KEY=s\nSENS_MAIL_SERVICE_ID=x\nMAIL_SENDER_ADDRESS=s@example.invalid\n"
       "MAIL_TEST_TO=tester@example.invalid\n")


@pytest.fixture
def sens_env(settings, tmp_path):
    def write(mode, test_to=True):
        path = tmp_path / "sens.env"
        text = ENV if test_to else ENV.replace("MAIL_TEST_TO=tester@example.invalid\n", "")
        path.write_text(text + f"SEND_MODE={mode}\n", encoding="utf-8")
        settings.CSP_SENS_ENV = str(path)
    return write


@pytest.fixture
def staff(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    return client


# --- 자동 발송 ---------------------------------------------------------------------------------------------------

def test_sending_is_on_only_when_chosen_on_and_sens_env_allows_it(sens_env):
    sens_env("test")  # any SEND_MODE but off lets the page decide (the test mode left the page)
    assert dispatch.config().mode == "off"  # nobody turned it on yet
    dispatch.choose("live", "홍길동")
    assert dispatch.config().mode == "live"
    sens_env("off")  # e.g. a rehearsal server or a restored backup: the file blocks it
    assert dispatch.config().mode == "off"
    sens_env("live")
    dispatch.choose("off", "홍길동")
    assert dispatch.config().mode == "off"


def test_switching_on_starts_afresh(sens_env):
    sens_env("live")
    SendingSince.objects.create(mode="live", since=timezone.now() - timedelta(days=3))
    dispatch.choose("off", "홍길동")
    dispatch.choose("live", "홍길동")
    assert SendingSince.objects.get(mode="live").since > timezone.now() - timedelta(minutes=1)


def test_the_page_turns_sending_on_and_off_with_a_name(staff, sens_env):
    sens_env("off")
    page = staff.get(reverse("config")).content.decode()
    assert "환경 설정" in page and 'value="live" disabled' in page and "테스트" not in page
    staff.post(reverse("config"), {"action": "send_mode", "mode": "live", "actor": "홍길동"})
    assert dispatch.chosen_mode() == "off"  # blocked by sens.env: refused
    sens_env("live")
    staff.post(reverse("config"), {"action": "send_mode", "mode": "live", "actor": ""})
    assert dispatch.chosen_mode() == "off"  # no name: refused
    staff.post(reverse("config"), {"action": "send_mode", "mode": "test", "actor": "홍길동"})
    assert dispatch.chosen_mode() == "off"  # no test mode any more
    staff.post(reverse("config"), {"action": "send_mode", "mode": "live", "actor": "홍길동"})
    assert dispatch.chosen_mode() == "live" and dispatch.config().mode == "live"
    page = staff.get(reverse("config")).content.decode()
    assert "홍길동" in page and ">ON<" in page  # the change is listed with its name


def test_send_now_is_offered_only_where_automatic_sending_will_not_send(staff, sens_env):
    sens_env("live")
    dispatch.choose("live", "홍길동")
    master("a@example.invalid")
    seen = stored("k1", headers.KT_NOTICE, kt_text("a@example.invalid", schedule="2099. 10. 24 22:00 ~ 23:00"))
    from mailcollect.notice_store import parse_pending
    from notifications.service import prepare_notifications
    parse_pending()
    prepare_notifications()
    n = Notification.objects.get(notice__seen=seen)
    send_form = f'action="{reverse("notification_send", args=[n.pk])}"'
    page = staff.get(reverse("notification", args=[n.pk])).content.decode()
    assert n.status == "draft_ready" and send_form not in page and "다음 수집 때 자동 발송" in page
    assert "테스트 발송" not in page  # the test button left the pages (user, 2026-10-22)
    MessageSeen.objects.filter(pk=seen.pk).update(first_seen_at=timezone.now() - timedelta(days=2))  # KT mail too old
    page = staff.get(reverse("notification", args=[n.pk])).content.decode()
    assert send_form in page and "다음 수집 때 자동 발송" not in page
    assert dispatch.request_send(n, "홍길동") and n.send_requested_by == "홍길동"


# --- KT 공지 양식: the rules ---------------------------------------------------------------------------------------

def test_the_default_form_is_the_old_fixed_rules():
    form = ktform.DEFAULT
    assert ktform.from_values(form.values()) == form
    info = HeaderInfo("[kt cloud 작업 공지]", True, "cloudmaster@ktcloud.com", "", "", None, "", ())
    assert classify(info) == headers.KT_NOTICE == classify(info, form=form)


def test_a_changed_sender_and_subject_words_classify_kt_mail():
    form = ktform.KtForm(sender="notice@kt.example.invalid", work=("작업", "점검"), complete=("완료", "종료"))
    def kind(subject, sender="notice@kt.example.invalid"):
        return classify(HeaderInfo(subject, True, sender, "", "", None, "", ()), form=form)
    assert kind("[kt cloud] 정기 점검 안내") == headers.KT_NOTICE
    assert kind("[kt cloud] 정기 점검 종료") == headers.KT_COMPLETE
    # the old sender is no longer KT's: its work mail is kept for a person to look at, as any look-alike
    assert kind("[kt cloud] 작업 공지", sender="cloudmaster@ktcloud.com") == headers.SUSPECT


def test_a_renamed_item_label_is_read_once_added():
    text = "○ 대상 계정 : s@example.invalid\nㅇ 작업내용 : 점검\nㅇ 작업 기간 : 2026.10.24 01:00 ~ 02:00"
    assert parse_notice(text).windows == [] and "작업기간" in parse_notice(text).extra
    labels = dict(ktform.DEFAULT.labels)
    labels["schedule"] = ("작업일시", "작업 기간")
    form = ktform.KtForm(labels=tuple(labels.items()))
    info = parse_notice(text, form)
    assert info.windows and info.accounts == ["s@example.invalid"] and not info.problems


def test_footer_lines_are_the_forms():
    text = "본문\n☎고객센터 080\n문의: 1588\n* 본 메일은 발신전용"
    assert drafts.kt_quote(text) == "본문\n문의: 1588"
    assert drafts.kt_quote(text, ("문의:",)) == "본문\n☎고객센터 080\n* 본 메일은 발신전용"


def test_the_posted_form_is_checked():
    post = {"sender": "not mail", "work": "", "label_accounts": "", "label_title": "작업내용",
            "label_schedule": "작업일시, 작업 내용", "label_detail": "이 이름은 공백을 빼도 스무 자가 넘는 아주 긴 항목 이름"}
    _, problems = ktform.from_post(post)
    text = " ".join(problems)
    assert "메일 주소" in text and "제목 단어" in text and "대상 계정" in text and "함께" in text and "20자" in text
    form, problems = ktform.from_post({**_fields(ktform.DEFAULT), "complete": "완료, 종료,완료"})
    assert not problems and form.complete == ("완료", "종료")


def _fields(form):
    data = {"sender": form.sender, "footer": "\n".join(form.footer)}
    data.update({key: ", ".join(getattr(form, key)) for key in ktform.WORD_LISTS})
    data.update({f"label_{name}": ", ".join(texts) for name, texts in form.labels})
    return data


# --- KT 공지 양식: the page -----------------------------------------------------------------------------------------

def _kt_mail(uidl, text):
    seen = stored(uidl, headers.KT_NOTICE, text, [b"From: kt cloud <cloudmaster@ktcloud.com>", b"Subject: [kt cloud] x"])
    MessageHeader.objects.filter(seen=seen).update(date=timezone.now())
    return seen


def test_preview_save_and_reread_bring_in_a_notice_kt_relabelled(staff):
    from mailcollect.notice_store import parse_pending
    from notifications.service import prepare_notifications
    master("b@example.invalid", email="other@example.invalid")
    master("a@example.invalid")
    text = kt_text("a@example.invalid", schedule="2099. 10. 24 22:00 ~ 23:00").replace("작업일시", "작업 기간")
    seen = _kt_mail("k1", text)
    twin = _kt_mail("k2", kt_text("b@example.invalid", schedule="2099. 10. 25 22:00 ~ 23:00"))  # read well
    parse_pending()
    prepare_notifications()
    assert Notice.objects.get(seen=seen).status == "review"
    first = Notification.objects.get(notice__seen=seen)  # made without a work, waiting for a person
    assert first.work is None and first.status == "needs_review"

    fields = {**_fields(ktform.DEFAULT), "label_schedule": "작업일시, 작업 기간"}
    page = staff.post(reverse("config"), {**fields, "action": "kt_preview"}).content.decode()
    assert "미리보기" in page and "작업 기간" in page  # what was typed stays in the form
    assert ktform.current() == ktform.DEFAULT  # a preview saves nothing

    page = staff.post(reverse("config"), {**fields, "action": "kt_save", "actor": ""}).content.decode()
    assert "저장하는 사람 이름" in page and ktform.current() == ktform.DEFAULT
    staff.post(reverse("config"), {**fields, "action": "kt_save", "actor": "홍길동"})
    assert ktform.current().labels_of("schedule") == ("작업일시", "작업 기간")

    staff.post(reverse("config"), {"action": "kt_reread"})
    notice = Notice.objects.get(seen=seen)
    assert notice.status == "ok" and notice.work is not None
    rows = Notification.objects.filter(account="a@example.invalid")
    assert rows.count() == 1 and rows.get().pk == first.pk  # the same customer notice, now under its work
    assert rows.get().work == notice.work and rows.get().status == "draft_ready"
    assert Notice.objects.get(seen=twin).notifications.count() == 1  # a well-read notice is left as it was

    staff.post(reverse("config"), {**fields, "action": "kt_default", "actor": "홍길동"})
    assert ktform.current() == ktform.DEFAULT


def test_kt_other_mail_of_the_last_day_is_queued_for_a_fresh_look(staff):
    seen = stored("o1", headers.KT_OTHER, "x")
    old = stored("o2", headers.KT_OTHER, "x")
    MessageSeen.objects.filter(pk=old.pk).update(first_seen_at=timezone.now() - timedelta(days=3))
    staff.post(reverse("config"), {"action": "kt_recheck"})
    seen.refresh_from_db(), old.refresh_from_db()
    assert seen.state == "pending" and seen.category == "" and old.category == headers.KT_OTHER
