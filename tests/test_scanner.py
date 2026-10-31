import hashlib

import pytest

from mailcollect import headers
from mailcollect.models import MessageBody, MessageHeader, MessageSeen, ScanRun
from mailcollect.scanner import AlreadyRunning, scan, scan_lock
from tests.conftest import EXPECTED, MAILBOX
from tests.fakepop3 import FakePOP3, Message
from tests.mailfactory import NOTICE, encoded_word as ew, message

pytestmark = pytest.mark.django_db
READ_ONLY_VERBS = {"USER", "PASS", "STAT", "UIDL", "TOP", "RETR", "QUIT"}
BODY_UIDLS = {u for u, c in EXPECTED.items() if c in headers.BODY}  # 6 KT originals + nc24_notice


def categories():
    return dict(MessageSeen.objects.values_list("uidl", "category"))


def test_first_scan_classifies_everything_and_keeps_only_target_headers(fake, make_cfg):
    run = scan(make_cfg(fake))

    assert run.status == ScanRun.Status.OK
    assert (run.server_count, run.new_count, run.headers_fetched, run.matched) == (30, 30, 30, 6)
    assert categories() == EXPECTED
    assert MessageHeader.objects.count() == 8  # 6 KT originals + nc24_notice + suspect
    assert not MessageSeen.objects.exclude(category__in=headers.KEPT).filter(header__isnull=False).exists()
    assert set(fake.verbs()) <= READ_ONLY_VERBS


def test_bodies_only_for_confirmed_categories_and_byte_exact(fake, make_cfg):
    run = scan(make_cfg(fake))

    assert (run.bodies_fetched, run.bodies_left) == (7, 0)
    assert fake.verbs().count("RETR") == 7
    assert set(MessageBody.objects.values_list("seen__uidl", flat=True)) == BODY_UIDLS
    originals = {m.uidl: m.raw for m in MAILBOX}
    for stored in MessageBody.objects.select_related("seen"):
        assert bytes(stored.raw) == originals[stored.seen.uidl]
        assert stored.sha256 == hashlib.sha256(originals[stored.seen.uidl]).hexdigest()
        assert stored.parse_errors == ""
        assert stored.text.startswith("본문입니다.")


def test_dot_stuffed_lines_survive_byte_exact(make_cfg):
    raw = message(ew(NOTICE)).replace(b"\r\n\r\n", b"\r\n\r\n.leading dot\r\n..two dots\r\n.\r\n", 1)
    server = FakePOP3([Message("d1", raw)]).start()
    try:
        scan(make_cfg(server))
    finally:
        server.stop()
    assert bytes(MessageBody.objects.get().raw) == raw


def test_unchanged_mailbox_sends_only_stat(fake, make_cfg):
    scan(make_cfg(fake))
    run = scan(make_cfg(fake))

    assert run.status == ScanRun.Status.UNCHANGED
    assert fake.verbs(session=1) == ["USER", "PASS", "STAT", "QUIT"]
    assert MessageSeen.objects.count() == 30


def test_body_batch_limit_and_resume(fake, make_cfg):
    run = scan(make_cfg(fake, body_batch_size=3))
    assert (run.status, run.bodies_fetched, run.bodies_left) == (ScanRun.Status.PARTIAL, 3, 4)

    run = scan(make_cfg(fake, body_batch_size=3))  # STAT unchanged, but bodies pending: no skip
    assert (run.bodies_fetched, run.bodies_left) == (3, 1)
    assert "UIDL" in fake.verbs(session=1) and "TOP" not in fake.verbs(session=1)


def test_message_removed_before_its_body_is_counted(fake, make_cfg):
    scan(make_cfg(fake, body_batch_size=0))
    fake.messages = [m for m in fake.messages if m.uidl != "u0003"]  # a KT notice leaves the server
    run = scan(make_cfg(fake))

    assert (run.body_lost, run.bodies_fetched, run.status) == (1, 6, ScanRun.Status.OK)


def test_dropped_connection_during_retr_keeps_bodies_and_resumes(fake, make_cfg):
    fake.drop_after_retrs = 2
    run = scan(make_cfg(fake))
    assert (run.status, run.bodies_fetched, run.bodies_left) == (ScanRun.Status.FAILED, 2, 5)

    fake.drop_after_retrs = None
    run = scan(make_cfg(fake))
    assert (run.status, run.bodies_fetched) == (ScanRun.Status.OK, 5)


def test_refused_retr_is_retried_next_run(fake, make_cfg):
    fake.retr_errors = {"u0010"}
    run = scan(make_cfg(fake))
    assert (run.status, run.bodies_left) == (ScanRun.Status.PARTIAL, 1)
    assert MessageSeen.objects.get(uidl="u0010").body_attempts == 1

    fake.retr_errors = set()
    run = scan(make_cfg(fake))
    assert (run.status, run.bodies_fetched) == (ScanRun.Status.OK, 1)


def test_only_new_messages_are_read(fake, make_cfg):
    scan(make_cfg(fake))
    fake.messages += [Message("u0031", message(ew(NOTICE + " (4차)"))), Message("u0032", message("Weekly"))]
    run = scan(make_cfg(fake))

    assert (run.new_count, run.headers_fetched, run.matched) == (2, 2, 1)
    assert fake.verbs(session=1).count("TOP") == 2


def test_removed_messages_are_marked_and_renumbering_is_safe(fake, make_cfg):
    run = scan(make_cfg(fake, batch_size=10))  # newest first: u0021..u0030
    assert (run.status, run.headers_fetched, run.pending_left) == (ScanRun.Status.PARTIAL, 10, 20)

    del fake.messages[:5]  # Outlook's 14-day removal takes u0001..u0005; the rest are renumbered
    while (run := scan(make_cfg(fake, batch_size=10))).status != ScanRun.Status.OK:
        pass

    gone = set(MessageSeen.objects.filter(gone_at__isnull=False).values_list("uidl", flat=True))
    assert gone == {f"u{n:04d}" for n in range(1, 6)}
    assert ScanRun.objects.order_by("pk")[1].gone_unscanned == 5
    kept = {u: c for u, c in categories().items() if u not in gone}
    assert kept == {u: c for u, c in EXPECTED.items() if u not in gone}


def test_dropped_connection_keeps_progress_and_resumes(fake, make_cfg):
    fake.drop_after_tops = 7
    run = scan(make_cfg(fake))
    assert (run.status, run.headers_fetched, run.pending_left) == (ScanRun.Status.FAILED, 7, 23)
    assert "EOF" in run.error

    fake.drop_after_tops = None
    run = scan(make_cfg(fake))
    assert (run.status, run.headers_fetched) == (ScanRun.Status.OK, 23)
    assert categories() == EXPECTED


def test_refused_top_leaves_message_pending(fake, make_cfg):
    fake.top_errors = {"u0003"}
    run = scan(make_cfg(fake))
    assert (run.status, run.headers_fetched, run.pending_left) == (ScanRun.Status.PARTIAL, 29, 1)
    assert MessageSeen.objects.get(uidl="u0003").attempts == 1

    fake.top_errors = set()
    run = scan(make_cfg(fake))
    assert (run.status, run.matched) == (ScanRun.Status.OK, 1)


def test_login_rejected_is_recorded_without_reading_anything(fake, make_cfg):
    fake.reject_login = True
    run = scan(make_cfg(fake))

    assert run.status == ScanRun.Status.LOGIN_FAILED
    assert "maildrop locked" in run.error
    assert "UIDL" not in fake.verbs() and "TOP" not in fake.verbs()


def test_concurrent_scan_is_refused_before_connecting(fake, make_cfg):
    cfg = make_cfg(fake)
    with scan_lock(cfg.lock_path), pytest.raises(AlreadyRunning):
        scan(cfg)
    assert fake.sessions == []


def test_time_budget_stops_reading(fake, make_cfg):
    run = scan(make_cfg(fake, max_seconds=0))
    assert (run.status, run.headers_fetched, run.pending_left) == (ScanRun.Status.PARTIAL, 0, 30)


def test_unreachable_server(make_cfg):
    server = FakePOP3().start()
    server.stop()  # nothing listens on this port any more
    run = scan(make_cfg(server))
    assert run.status == ScanRun.Status.FAILED
    assert "ConnectionRefusedError" in run.error


def test_uidls_are_scoped_per_mailbox(fake, make_cfg):
    other = FakePOP3(fake.messages, user="successor").start()
    try:
        scan(make_cfg(fake))
        scan(make_cfg(other))
    finally:
        other.stop()
    assert MessageSeen.objects.count() == 60
    assert MessageSeen.objects.values("mailbox").distinct().count() == 2


def test_the_same_mail_twice_is_kept_once(make_cfg):
    """A forward of both addresses into one mailbox, or a mail already collected from another mailbox."""
    raw = message(ew(NOTICE))
    server = FakePOP3([Message("a1", raw), Message("a2", raw), Message("a3", message(ew(NOTICE)))]).start()
    try:
        scan(make_cfg(server))
    finally:
        server.stop()
    assert sorted(categories().values()) == [headers.DUPLICATE, headers.KT_NOTICE, headers.KT_NOTICE]
    assert MessageHeader.objects.count() == 2 and MessageBody.objects.count() == 2
