"""작업 기록: flat rows per customer notice that follow the notice and outlive the yearly purge, with a CSV."""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from customers.models import PartnerChannel
from mailcollect import headers
from mailcollect.models import Notice, Work
from mailcollect.notice_store import parse_pending
from notifications.models import Notification, Record, WorkClosure
from notifications.records import sync
from notifications.retention import purge
from notifications.service import confirm_by_staff, match_copies, prepare_notifications, record_manual_sent
from tests.test_notice_store import stored
from tests.test_notifications import TITLE, copy_text, header, kt_text, prepared

pytestmark = pytest.mark.django_db
AGENCY = "agency@example.invalid"


@pytest.fixture
def staff(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))
    return client


def test_one_flat_row_per_notice_that_follows_sending_and_the_check():
    n = prepared()
    assert sync() == 1 and sync() == 0
    r = Record.objects.get()
    assert (r.notification, r.tab, r.work_title, r.kind_label) == (n, "작업 공지", TITLE, "사전 공지 1차")
    assert (r.starts_at, r.customer, r.recipients, r.status) == (n.work.starts_at, "예시회사", "owner@example.invalid", "draft_ready")
    assert r.kt_received_at == n.notice.seen.first_seen_at and r.sent_at is None and r.staff_name == ""

    stored("c1", headers.NC24_NOTICE, copy_text(n.account), header("owner@example.invalid"))
    parse_pending()
    match_copies()
    assert sync() == 1
    r.refresh_from_db()
    assert r.status == "sent_copy_confirmed" and r.sent_at == Notice.objects.get(seen__uidl="c1").seen.first_seen_at

    n.refresh_from_db()
    assert confirm_by_staff(n, None, "홍길동")
    WorkClosure.objects.create(work=n.work, closed_at=timezone.now(), closed_by="홍길동")
    sync()
    r.refresh_from_db()
    assert (r.status, r.staff_name, r.closed_by) == ("staff_confirmed", "홍길동", "홍길동")
    assert r.staff_confirmed_at == n.staff_confirmed_at


def test_recorded_sending_counts_as_sent_and_a_partner_notice_names_its_tab():
    PartnerChannel.objects.create(label="기관", accounts=AGENCY, sender="ops@partner.example.invalid")
    n = prepared()
    record_manual_sent(n, None, "", "홍길동")
    stored("k2", headers.KT_NOTICE, kt_text(AGENCY))
    parse_pending()
    prepare_notifications()
    sync()
    mine, theirs = Record.objects.get(account=n.account), Record.objects.get(account=AGENCY)
    assert mine.status == "manual_sent_recorded" and mine.sent_at == n.events.get(status="manual_sent_recorded").at
    assert theirs.tab == "기관" and theirs.customer == "기관"


def test_rows_outlive_the_yearly_purge_with_their_last_state():
    n = prepared()
    Work.objects.filter(pk=n.work_id).update(starts_at=timezone.now() - timedelta(days=400))
    Notification.objects.filter(pk=n.pk).update(status="sent_copy_confirmed")
    n.refresh_from_db()
    confirm_by_staff(n, None, "홍길동")  # never synced before the purge: the purge syncs first
    counts = purge(365)
    assert counts["records refreshed"] == 1 and not Notification.objects.exists() and not Work.objects.exists()
    r = Record.objects.get()
    assert r.notification is None and r.work_title == TITLE and r.staff_name == "홍길동" and r.status == "staff_confirmed"


def test_page_and_csv_filter_by_year_and_tab(staff):
    PartnerChannel.objects.create(label="기관", accounts=AGENCY, sender="ops@partner.example.invalid")
    n = prepared()
    stored("k2", headers.KT_NOTICE, kt_text(AGENCY))
    parse_pending()
    prepare_notifications()
    sync()
    year = n.work.starts_at.year
    page = staff.get(reverse("records"), {"year": year}).content.decode()
    assert "작업 기록" in page and page.count(TITLE) == 2 and "예시회사" in page and f"{year}년" in page

    csv = staff.get(reverse("records_csv"), {"year": year})
    assert csv["Content-Type"].startswith("text/csv") and "%EC%9E%91%EC%97%85%EA%B8%B0%EB%A1%9D" in csv["Content-Disposition"]
    text = csv.content.decode("utf-8")
    lines = text.split("\r\n")
    assert text.startswith("﻿작업 시작,작업 종료,작업 내용,구분,공지,고객,계정,받는 사람,상태,") and len(lines) == 4
    assert lines[1].split(",")[3] == "기관" and lines[2].split(",")[3:7] == ["작업 공지", "사전 공지 1차", "예시회사", n.account]
    assert lines[1].startswith(timezone.localtime(n.work.starts_at).strftime("%Y-%m-%d %H:%M"))

    assert len(staff.get(reverse("records_csv"), {"year": year - 1}).content.decode().split("\r\n")) == 2
    month = n.work.starts_at.month
    monthly = staff.get(reverse("records_csv"), {"year": year, "month": month})
    assert len(monthly.content.decode().split("\r\n")) == 4 and f"{year}-{month:02d}.csv" in monthly["Content-Disposition"]
    assert len(staff.get(reverse("records_csv"), {"year": year, "month": month % 12 + 1}).content.decode().split("\r\n")) == 2
    assert len(staff.get(reverse("records_csv"), {"tab": "main", "year": "all"}).content.decode().split("\r\n")) == 3
    mine = staff.get(reverse("records_csv"), {"tab": PartnerChannel.objects.get().pk}).content.decode()
    assert AGENCY in mine and n.account not in mine
