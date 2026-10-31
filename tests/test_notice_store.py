from datetime import datetime

import pytest
from django.utils import timezone

from mailcollect import headers
from mailcollect.models import Mailbox, MessageBody, MessageHeader, MessageSeen, Notice, NoticeTarget, Work
from mailcollect.notice_store import PARSER_VERSION, parse_pending
from mailcollect.notices import KST
from tests.test_notices import FAMILY_A, NC24_COPY

pytestmark = pytest.mark.django_db


def stored(uidl, category, text, header_lines=None):
    box, _ = Mailbox.objects.get_or_create(host="h", port=110, username="u")
    now = timezone.now()
    seen = MessageSeen.objects.create(
        mailbox=box, uidl=uidl, first_seen_at=now, last_seen_at=now,
        state=MessageSeen.State.CLASSIFIED, category=category,
    )
    if header_lines:
        MessageHeader.objects.create(seen=seen, raw=b"\r\n".join(header_lines), subject="s")
    raw = text.encode()
    MessageBody.objects.create(seen=seen, raw=raw, size=len(raw), sha256="0" * 64, text=text)
    return seen


def test_same_work_for_two_accounts_is_one_work():
    stored("a", headers.KT_NOTICE, FAMILY_A)
    stored("b", headers.KT_NOTICE, FAMILY_A.replace("26j3_test@", "other@"))
    counts = parse_pending()

    assert counts == {(headers.KT_NOTICE, Notice.Status.OK): 2}
    work = Work.objects.get()
    assert (work.title, work.starts_at) == ("KT Cloud 네트워크 개선 작업", datetime(2026, 10, 24, 22, tzinfo=KST))
    assert set(NoticeTarget.objects.values_list("account", flat=True)) == {"26j3_test@example.invalid", "other@example.invalid"}
    assert work.notices.count() == 2


def test_nc24_copy_gets_recipients_and_no_work():
    stored("c", headers.NC24_NOTICE, NC24_COPY, header_lines=[
        b"From: ExampleCloud <system@example.com>",
        b"To: =?utf-8?B?6rOg6rCd?= <Cust1@example.invalid>, cust2@example.invalid",
        b"Cc: staff@example.invalid",
        b"Subject: x",
    ])
    parse_pending()

    notice = Notice.objects.get()
    assert notice.work is None
    assert notice.recipients == ["cust1@example.invalid", "cust2@example.invalid", "staff@example.invalid"]
    assert list(notice.targets.values_list("account", flat=True)) == ["c1@example.invalid", "c2@example.invalid"]


def test_parse_is_idempotent_and_reparse_does_not_duplicate():
    stored("a", headers.KT_NOTICE, FAMILY_A)
    parse_pending()
    assert parse_pending() == {}
    assert sum(parse_pending(reparse=True).values()) == 1
    assert (Notice.objects.count(), NoticeTarget.objects.count(), Work.objects.count()) == (1, 1, 1)


def test_older_parser_version_is_parsed_again():
    stored("a", headers.KT_NOTICE, FAMILY_A)
    parse_pending()
    Notice.objects.update(parser_version=PARSER_VERSION - 1)
    assert sum(parse_pending().values()) == 1


def test_problems_mark_review_and_hold_no_values():
    stored("x", headers.KT_COMPLETE, "ㅇ 작업일시 : 비공개-서버-77 점검 후 안내")
    parse_pending()
    notice = Notice.objects.get()
    assert notice.status == Notice.Status.REVIEW
    assert "비공개-서버-77" not in notice.problems
    assert notice.schedule == "비공개-서버-77 점검 후 안내"
