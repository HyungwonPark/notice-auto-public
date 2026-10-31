import os
import sqlite3
import time
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from mailcollect.models import Mailbox, ScanRun
from mailcollect.scanner import request_scan


@pytest.mark.django_db
def test_request_writes_trigger_file(tmp_path):
    path = tmp_path / "scan.request"
    requested, last = request_scan(path, min_interval=10)
    assert (requested, last) == (True, None)
    assert path.exists()


@pytest.mark.django_db
def test_request_right_after_a_scan_is_skipped(tmp_path):
    box = Mailbox.objects.create(host="h", port=110, username="u")
    run = ScanRun.objects.create(mailbox=box, status=ScanRun.Status.OK, finished_at=timezone.now())
    path = tmp_path / "scan.request"

    assert request_scan(path, min_interval=10) == (False, run)
    assert not path.exists()

    ScanRun.objects.filter(pk=run.pk).update(finished_at=timezone.now() - timedelta(seconds=11))
    assert request_scan(path, min_interval=10)[0] is True


@pytest.mark.django_db
def test_request_scan_command_reports_skip(settings, tmp_path):
    settings.CSP_SCAN = {**settings.CSP_SCAN, "request_path": str(tmp_path / "scan.request")}
    box = Mailbox.objects.create(host="h", port=110, username="u")
    ScanRun.objects.create(mailbox=box, status=ScanRun.Status.UNCHANGED, finished_at=timezone.now())
    out = StringIO()
    call_command("request_scan", stdout=out)
    assert "skipped" in out.getvalue() and "status=unchanged" in out.getvalue()


@pytest.mark.django_db(transaction=True)  # VACUUM cannot run inside a transaction
def test_backup_writes_a_readable_copy_and_prunes_old_ones(tmp_path):
    Mailbox.objects.create(host="h", port=110, username="u")
    old = tmp_path / "db-20260101-000000.sqlite3"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 15 * 86400,) * 2)
    out = StringIO()
    call_command("backup_db", dir=tmp_path, keep_days=14, stdout=out)

    copies = list(tmp_path.glob("db-*.sqlite3"))
    assert len(copies) == 1 and copies[0] != old
    with sqlite3.connect(copies[0]) as db:
        assert db.execute("select count(*) from mailcollect_mailbox").fetchone() == (1,)
    assert "removed 1 old" in out.getvalue()
