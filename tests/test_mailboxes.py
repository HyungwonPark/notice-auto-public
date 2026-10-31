"""A second mailbox (e.g. one that gets every alarm): registered disabled, KT work notices only, limited backlog."""
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from mailcollect import headers
from mailcollect.models import Mailbox, MessageHeader, MessageSeen
from mailcollect.scanner import mailbox_for, prune_seen, scan
from tests.conftest import EXPECTED, MAILBOX
from tests.fakepop3 import FakePOP3, Message
from tests.mailfactory import encoded_word as ew
from tests.mailfactory import message
from tests.test_command import write_env

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def lock_in_tmp(settings, tmp_path):
    settings.CSP_SCAN = {**settings.CSP_SCAN, "lock_path": str(tmp_path / "scan.lock"), "timeout": 5}


@pytest.fixture
def alarm():
    server = FakePOP3(MAILBOX, user="alarm-box@example.invalid").start()
    yield server
    server.stop()


def extra(settings, server, **values):
    path = write_env(Path(settings.CSP_POP3_EXTRA_DIR) / "alarm.env", server)
    path.write_text(path.read_text() + "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def categories(mailbox):
    return dict(MessageSeen.objects.filter(mailbox=mailbox).values_list("uidl", "category"))


def test_extra_mailbox_waits_until_enabled_then_keeps_only_kt_work_notices(settings, tmp_path, fake, alarm):
    extra(settings, alarm, LABEL="알림 메일함", BACKFILL=0)
    main_env = write_env(tmp_path / "pop3.env", fake)
    out = StringIO()
    call_command("scan_mail", env_file=main_env, stdout=out)
    box = Mailbox.objects.get(username=alarm.user)
    assert (box.enabled, box.name, box.label, box.collect) == (False, "alarm", "알림 메일함", "kt")
    assert "[alarm] disabled" in out.getvalue() and not box.messages.exists()

    call_command("mailboxes", enable="alarm", env_file=main_env, stdout=StringIO())
    out = StringIO()
    call_command("scan_mail", env_file=main_env, stdout=out)
    assert "[main] status=unchanged" in out.getvalue() and "[alarm] status=ok server=30 new=30" in out.getvalue()

    # The fake alarm box holds the very mails main already kept (same Message-IDs): KT work notices are
    # recorded as duplicates, never read twice; still no 예시클라우드 copy and no suspect from this mailbox.
    want = {u: headers.DUPLICATE if c in headers.MATCHED else c if c == headers.KT_OTHER else headers.OTHER
            for u, c in EXPECTED.items()}
    assert categories(box) == want
    assert not MessageHeader.objects.filter(seen__mailbox=box).exists()


def test_first_listing_reads_only_the_newest_backfill(make_cfg, fake):
    cfg = make_cfg(fake, name="alarm", collect="kt", backfill=10)
    mailbox_for(cfg)
    run = scan(cfg)
    box = Mailbox.objects.get()
    states = dict(box.messages.values_list("uidl", "state"))
    assert run.skipped == 20 and run.headers_fetched == 10
    assert all(states[f"u{n:04d}"] == "classified" for n in range(21, 31))
    assert all(states[f"u{n:04d}"] == "skipped" for n in range(1, 21))

    fake.messages.append(Message("u0031", message(ew("[kt cloud 긴급 작업 공지]"))))
    run = scan(cfg)
    assert (run.new_count, run.skipped, run.headers_fetched, run.matched) == (1, 0, 1, 1)


def test_skipped_messages_can_be_read_later(settings, tmp_path, fake, alarm):
    extra(settings, alarm, BACKFILL=10)
    main_env = write_env(tmp_path / "pop3.env", fake)
    call_command("mailboxes", enable="alarm", env_file=main_env, stdout=StringIO())
    call_command("scan_mail", env_file=main_env, stdout=StringIO())
    box = Mailbox.objects.get(username=alarm.user)
    assert box.messages.filter(state="skipped").count() == 20

    out = StringIO()
    call_command("mailboxes", read_skipped="alarm", leave_oldest=5, env_file=main_env, stdout=out)
    assert "alarm: 15 skipped message(s) queued; about 1 scans" in out.getvalue()
    states = dict(box.messages.values_list("uidl", "state"))
    assert [u for u, s in sorted(states.items()) if s == "skipped"] == [f"u{n:04d}" for n in range(1, 6)]
    call_command("mailboxes", read_skipped="alarm", env_file=main_env, stdout=StringIO())
    call_command("scan_mail", env_file=main_env, stdout=StringIO())
    assert not box.messages.exclude(state="classified").exists()


def test_bad_extra_file_does_not_stop_the_main_mailbox(settings, tmp_path, fake):
    bad = Path(settings.CSP_POP3_EXTRA_DIR) / "broken.env"
    bad.write_text("POP3_USER=someone\n", encoding="utf-8")
    out = StringIO()
    with pytest.raises(CommandError, match="broken: .*missing POP3_PASSWORD"):
        call_command("scan_mail", env_file=write_env(tmp_path / "pop3.env", fake), stdout=out)
    assert "[main] status=ok server=30" in out.getvalue()


def test_probe_lists_and_enables(settings, tmp_path, fake, alarm):
    extra(settings, alarm, LABEL="알림 메일함")
    main_env = write_env(tmp_path / "pop3.env", fake)
    out = StringIO()
    call_command("mailboxes", probe="alarm", env_file=main_env, stdout=out)
    assert "alarm: 30 messages" in out.getvalue() and "newest 30" in out.getvalue()
    assert not MessageSeen.objects.exists()  # STAT only: nothing listed or read

    out = StringIO()
    call_command("mailboxes", env_file=main_env, stdout=out)
    assert "알림 메일함, collect=kt backfill=2000, disabled" in out.getvalue()
    assert alarm.user not in out.getvalue()  # the account name is not printed

    with pytest.raises(CommandError, match="no mailbox file named nope"):
        call_command("mailboxes", enable="nope", env_file=main_env)


def test_prune_forgets_only_old_gone_mail_that_was_never_kept(make_cfg, fake):
    scan(make_cfg(fake))
    fake.messages = []
    scan(make_cfg(fake))
    MessageSeen.objects.update(gone_at=timezone.now() - timedelta(days=31))
    kept = MessageSeen.objects.filter(category__in=headers.KEPT).count()
    assert prune_seen() == 30 - kept
    assert MessageSeen.objects.count() == kept


def test_dashboard_lists_each_enabled_mailbox(client, django_user_model, make_cfg, fake, alarm):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    scan(make_cfg(fake))
    cfg = make_cfg(alarm, name="alarm", label="알림 메일함", collect="kt")
    mailbox_for(cfg)
    Mailbox.objects.filter(username=alarm.user).update(enabled=True)
    page = client.get(reverse("dashboard")).content.decode()
    assert "기본 메일함" in page and "알림 메일함: 15분 넘게 확인하지 못했습니다" in page
    scan(cfg)
    page = client.get(reverse("dashboard")).content.decode()
    assert "알림 메일함: 15분 넘게" not in page


def test_replacing_the_mailboxes_switches_the_old_ones_off(settings, tmp_path, fake, alarm):
    """pop3.env moved to the new forwarding account and alarm.env deleted: only the new one is read."""
    alarm_file = extra(settings, alarm)
    main_env = write_env(tmp_path / "pop3.env", fake)
    call_command("scan_mail", env_file=main_env, stdout=StringIO())
    call_command("mailboxes", enable="alarm", env_file=main_env, stdout=StringIO())
    old_main = Mailbox.objects.get(username=fake.user)

    alarm_file.unlink()
    new_env = write_env(tmp_path / "pop3-new.env", alarm)  # the forwarding mailbox becomes the main one
    out = StringIO()
    call_command("scan_mail", env_file=new_env, stdout=out)
    assert "[main] no mailbox file names this login any more: disabled" in out.getvalue()
    assert "[alarm] no mailbox file names this login any more: disabled" not in out.getvalue()  # same login, now main
    old_main.refresh_from_db()
    assert not old_main.enabled
    assert list(Mailbox.objects.filter(enabled=True).values_list("username", "name")) == [(alarm.user, "main")]


def test_a_broken_extra_file_does_not_switch_its_mailbox_off(settings, tmp_path, fake, alarm):
    path = extra(settings, alarm)
    main_env = write_env(tmp_path / "pop3.env", fake)
    call_command("scan_mail", env_file=main_env, stdout=StringIO())
    call_command("mailboxes", enable="alarm", env_file=main_env, stdout=StringIO())
    path.write_text("POP3_HOST=h\n", encoding="utf-8")  # a typo while editing
    with pytest.raises(CommandError, match="alarm"):  # said, and the other mailboxes were still read
        call_command("scan_mail", env_file=main_env, stdout=StringIO())
    assert Mailbox.objects.get(username=alarm.user).enabled
