from io import StringIO

import pytest
from django.core.management import call_command

from mailcollect import headers
from mailcollect.models import MessageHeader, MessageSeen
from mailcollect.scanner import scan
from tests.conftest import EXPECTED

pytestmark = pytest.mark.django_db


def test_stored_headers_follow_new_rules(fake, make_cfg):
    scan(make_cfg(fake))
    MessageSeen.objects.filter(uidl="u0012").update(category=headers.SUSPECT)  # as if an older rule had misfiled it
    out = StringIO()
    call_command("reclassify", stdout=out)

    assert "suspect->kt_complete=1" in out.getvalue()
    assert MessageSeen.objects.get(uidl="u0012").category == headers.KT_COMPLETE
    assert len(fake.sessions) == 1  # reclassify never contacts the mail server


def test_parsed_fields_are_refreshed_from_raw_header(fake, make_cfg):
    scan(make_cfg(fake))
    MessageHeader.objects.filter(seen__uidl="u0020").update(from_addr="", parse_errors="stale")
    out = StringIO()
    call_command("reclassify", stdout=out)

    assert "refreshed headers: 1" in out.getvalue()
    header = MessageHeader.objects.get(seen__uidl="u0020")
    assert (header.from_addr, header.parse_errors) == ("system@example.com", "")


def test_recheck_one_category_reads_only_that_category_again(fake, make_cfg):
    scan(make_cfg(fake))
    out = StringIO()
    call_command("reclassify", recheck=headers.KT_OTHER, stdout=out)
    assert "requeued without header: 1" in out.getvalue()
    run = scan(make_cfg(fake))
    assert run.headers_fetched == 1


def test_recheck_other_reads_only_mail_without_header_again(fake, make_cfg):
    scan(make_cfg(fake))
    call_command("reclassify", recheck_other=True, stdout=StringIO())
    run = scan(make_cfg(fake))

    without_header = sum(1 for c in EXPECTED.values() if c not in headers.KEPT)
    assert run.headers_fetched == without_header
    assert fake.verbs(session=1).count("TOP") == without_header
    assert dict(MessageSeen.objects.values_list("uidl", "category")) == EXPECTED
