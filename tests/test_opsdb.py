"""The operations-DB view as the 일반 customers: connection file, mapping, sync and the scan hook.
No MySQL server is ever contacted: fetch_rows is replaced."""
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from customers import opsdb
from customers.models import CustomerAccount, ImportBatch, OpsDbSync
from mailcollect.config import ConfigError
from tests.test_command import write_env

pytestmark = pytest.mark.django_db
CFG = opsdb.OpsDbConfig(host="db.example.invalid", port=3306, user="reader", password="pw")


def env(tmp_path, text):
    path = tmp_path / "opsdb.env"
    path.write_text(text, encoding="utf-8")
    return path


def view_row(account, **over):
    row = {"userid": "u1", "cloud_account": account, "name": "홍길동", "company": "예시회사", "manager": "담당",
           "email": f"owner@{account.split('@')[1]}", "tel1": "02", "tel2": "123", "tel3": "4567",
           "phone1": "010", "phone2": "1234", "phone3": "5678"}
    row.update(over)
    return row


def test_file_gives_the_connection_with_defaults_for_the_rest(tmp_path):
    cfg = opsdb.load_config(env(tmp_path, "OPSDB_HOST=db.example.invalid\nOPSDB_USER=reader\nOPSDB_PASSWORD=pw\n"))
    assert (cfg.host, cfg.port, cfg.user, cfg.database, cfg.view) == ("db.example.invalid", 3306, "reader", "examplecloud", "v_customer_accounts")
    assert cfg.table == "`examplecloud`.`v_customer_accounts`" and "pw" not in repr(cfg)


def test_missing_keys_bad_port_and_odd_names_are_refused(tmp_path):
    with pytest.raises(ConfigError, match="missing OPSDB_USER, OPSDB_PASSWORD"):
        opsdb.load_config(env(tmp_path, "OPSDB_HOST=h\n"))
    with pytest.raises(ConfigError, match="OPSDB_PORT"):
        opsdb.load_config(env(tmp_path, "OPSDB_HOST=h\nOPSDB_USER=u\nOPSDB_PASSWORD=p\nOPSDB_PORT=abc\n"))
    with pytest.raises(ConfigError, match="OPSDB_VIEW"):
        opsdb.load_config(env(tmp_path, "OPSDB_HOST=h\nOPSDB_USER=u\nOPSDB_PASSWORD=p\nOPSDB_VIEW=v; drop\n"))
    with pytest.raises(ConfigError, match="cannot read"):
        opsdb.load_config(tmp_path / "none.env")


def test_command_without_a_file_says_so(settings, tmp_path):
    settings.CSP_OPSDB_ENV = str(tmp_path / "none.env")
    assert not opsdb.configured()
    with pytest.raises(CommandError, match="cannot read"):
        call_command("opsdb", "--probe")


def test_view_rows_become_csv_columns_with_the_mobile_as_the_phone():
    records = opsdb.to_records([view_row("B@Example.invalid", phone1=""), view_row("a@example.invalid")])
    assert [r["account"] for r in records] == ["a@example.invalid", "B@Example.invalid"]  # sorted, kept as given
    assert records[0]["phone"] == "010-1234-5678" and records[1]["phone"] == "02-123-4567"  # no mobile: the office line
    assert records[0] == {"account": "a@example.invalid", "userid": "u1", "name": "홍길동", "company": "예시회사",
                          "manager": "담당", "email": "owner@example.invalid", "phone": "010-1234-5678"}
    assert opsdb.as_csv(records).decode().splitlines()[0] == "account,userid,name,회사명,담당자,email,phone"


def test_sync_replaces_csv_db_and_web_rows_of_its_accounts_and_skips_unchanged_data(monkeypatch):
    CustomerAccount.objects.create(kind="general", source="csv", account="old@example.invalid", email="x@example.invalid")
    CustomerAccount.objects.create(kind="public", source="csv", account="gov@example.invalid", email="g@example.invalid")
    CustomerAccount.objects.create(kind="general", source="web", account="fixed@example.invalid", email="f@example.invalid")
    CustomerAccount.objects.create(kind="general", source="web", account="extra@example.invalid", email="e@example.invalid")
    rows = [view_row("a@example.invalid"), view_row("fixed@example.invalid", email="other@example.invalid")]
    monkeypatch.setattr(opsdb, "fetch_rows", lambda cfg: rows)

    run = opsdb.sync(CFG)
    assert run.ok and run.rows == 2 and run.batch.file_name == "운영 DB" and run.batch.rows == 2
    active = {c.account_key: c for c in CustomerAccount.objects.filter(active=True)}
    assert set(active) == {"a@example.invalid", "fixed@example.invalid", "extra@example.invalid", "gov@example.invalid"}
    assert active["a@example.invalid"].source == "db" and active["a@example.invalid"].phone == "010-1234-5678"
    assert active["fixed@example.invalid"].source == "db" and active["fixed@example.invalid"].email == "other@example.invalid"  # the DB is the master
    assert active["extra@example.invalid"].source == "web"  # not in the view: the person's row stays
    assert active["gov@example.invalid"].source == "csv"  # 공공 is not in the view: untouched
    assert not CustomerAccount.objects.get(account="old@example.invalid").active
    assert not CustomerAccount.objects.get(account="fixed@example.invalid", source="web").active  # kept as history

    again = opsdb.sync(CFG)
    assert again.ok and again.batch is None and ImportBatch.objects.count() == 1  # same data: nothing reloaded
    rows.append(view_row("c@example.invalid"))
    assert opsdb.sync(CFG).batch is not None and CustomerAccount.objects.filter(active=True, source="db").count() == 3
    assert OpsDbSync.objects.count() == 3


def test_a_web_only_row_is_never_replaced_by_the_db_and_edits_keep_the_mark(monkeypatch):
    pinned = CustomerAccount.objects.create(kind="general", source="web", web_only=True, account="ops.cloud@partner.example.invalid",
                                            company="협력사", email="a@example.invalid\nb@example.invalid")
    monkeypatch.setattr(opsdb, "fetch_rows", lambda cfg: [view_row("ops.cloud@partner.example.invalid", email="db@example.invalid")])
    run = opsdb.sync(CFG)
    assert run.ok and run.batch.rows == 0
    kept = CustomerAccount.objects.get(active=True, account_key="ops.cloud@partner.example.invalid")
    assert kept.pk == pinned.pk and kept.emails == ["a@example.invalid", "b@example.invalid"]
    edited = kept.edit(None, email="c@example.invalid")
    assert edited.web_only and edited.source == "web"
    assert not kept.edit(None, web_only=False).web_only


def test_an_empty_or_shrunken_view_is_refused_and_customers_stay(monkeypatch):
    rows = [view_row(f"a{i}@example.invalid") for i in range(10)]
    monkeypatch.setattr(opsdb, "fetch_rows", lambda cfg: rows)
    assert opsdb.sync(CFG).ok and CustomerAccount.objects.filter(active=True).count() == 10

    rows[:] = []  # a broken view or a lost permission answers with nothing
    run = opsdb.sync(CFG)
    assert not run.ok and run.error.startswith("Refused: the view returned no rows")
    rows[:] = [view_row(f"a{i}@example.invalid") for i in range(7)]  # 70% of last time
    run = opsdb.sync(CFG)
    assert not run.ok and "7 rows, last time 10" in run.error
    assert CustomerAccount.objects.filter(active=True).count() == 10  # nothing was wiped

    assert opsdb.sync(CFG, force=True).ok and CustomerAccount.objects.filter(active=True).count() == 7  # a real cut, on purpose
    rows[:] = [view_row(f"a{i}@example.invalid") for i in range(6)]  # 6 of 7: ordinary churn
    assert opsdb.sync(CFG).ok


def test_the_web_button_uses_timeouts_under_the_worker_limit(monkeypatch):
    seen = []
    monkeypatch.setattr(opsdb, "fetch_rows", lambda cfg: seen.append(cfg) or [view_row("a@example.invalid")])
    opsdb.sync(CFG, interactive=True)
    opsdb.sync(CFG)
    assert (seen[0].connect_timeout, seen[0].read_timeout) == (5, 20) and seen[1].read_timeout == 60


def test_csv_upload_still_keeps_web_edits():
    from customers.importer import import_csv
    CustomerAccount.objects.create(kind="general", source="web", account="fixed@example.invalid", email="f@example.invalid")
    raw = "account,userid,name,회사명,담당자,email,phone\nfixed@example.invalid,u,n,c,m,other@example.invalid,\n".encode()
    result = import_csv(raw, "a.csv", "general")
    assert result.kept_web == 1 and CustomerAccount.objects.get(active=True, account="fixed@example.invalid").source == "web"


def test_a_failed_read_is_recorded_not_raised_and_the_scan_goes_on(monkeypatch, settings, tmp_path, fake):
    def boom(cfg):
        raise OSError("connection refused")
    monkeypatch.setattr(opsdb, "fetch_rows", boom)
    run = opsdb.sync(CFG)
    assert not run.ok and run.error == "OSError: connection refused" and run.finished_at

    settings.CSP_OPSDB_ENV = str(env(tmp_path, "OPSDB_HOST=db.example.invalid\nOPSDB_USER=u\nOPSDB_PASSWORD=p\n"))
    settings.CSP_SCAN = {**settings.CSP_SCAN, "lock_path": str(tmp_path / "scan.lock"), "timeout": 5}
    out = StringIO()
    call_command("scan_mail", env_file=write_env(tmp_path / "pop3.env", fake), stdout=out)
    assert "opsdb: failed: OSError: connection refused" in out.getvalue() and "status=ok" in out.getvalue()
    assert OpsDbSync.objects.count() == 2


def test_customers_page_shows_the_last_read_and_can_read_now(monkeypatch, settings, tmp_path, client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests", is_staff=True))
    page = client.get(reverse("customers")).content.decode()
    assert "운영 DB" not in page  # not configured: nothing about it

    settings.CSP_OPSDB_ENV = str(env(tmp_path, "OPSDB_HOST=db.example.invalid\nOPSDB_USER=u\nOPSDB_PASSWORD=p\n"))
    monkeypatch.setattr(opsdb, "fetch_rows", lambda cfg: [view_row("a@example.invalid")])
    page = client.get(reverse("customers")).content.decode()
    assert "운영 DB 지금 조회" in page and "아직 조회 전" in page and "15분 넘게 조회하지 못함" in page
    response = client.post(reverse("customers_sync"), follow=True)
    text = response.content.decode()
    assert "운영 DB 1건 조회, 고객 정보 갱신 (1건)" in text and "1건, 고객 정보 갱신" in text and "15분" not in text

    row = CustomerAccount.objects.get(active=True, account="a@example.invalid")
    page = client.get(reverse("customer_edit", args=[row.pk])).content.decode()
    card = page.partition('class="grid-2"')[2].partition("<aside")[0]
    assert "운영 DB 값" in page and 'name="email"' not in card and "웹에서만 관리로 전환" in card
    client.post(reverse("customer_edit", args=[row.pk]), {"account": row.account, "email": "x@example.invalid"})
    assert CustomerAccount.objects.filter(active=True, account="a@example.invalid").count() == 1
    assert CustomerAccount.objects.get(active=True, account="a@example.invalid").source == "db"  # the edit was refused

    response = client.post(reverse("customer_edit", args=[row.pk]), {"take_over": "1"}, follow=True)
    taken = CustomerAccount.objects.get(active=True, account="a@example.invalid")
    assert taken.source == "web" and taken.web_only and taken.email == row.email
    page = response.content.decode()
    assert "웹에서만 관리" in page and 'name="web_only" value="1" checked' in page
    client.post(reverse("customer_edit", args=[taken.pk]), {"account": taken.account, "email": taken.email, "web_only_shown": "1"})
    assert not CustomerAccount.objects.get(active=True, account="a@example.invalid").web_only  # unticked: the DB may take it back


def test_an_unused_account_the_db_updates_is_a_customer_again(monkeypatch):
    from django.utils import timezone
    from customers.models import InternalAccount
    from notifications.service import apply_internal_lists
    from tests.test_internal import notice_for
    rows = [view_row("a@example.invalid"), view_row("b@example.invalid")]
    monkeypatch.setattr(opsdb, "fetch_rows", lambda cfg: rows)
    opsdb.sync(CFG)
    for account in ("a@example.invalid", "b@example.invalid", "c@example.invalid"):
        InternalAccount.objects.create(account_key=account, kind="unused", added_at=timezone.now())
    n = notice_for("a@example.invalid")
    assert n.status == "unused"

    rows[0] = view_row("a@example.invalid", phone3="0000")  # the DB updates a
    rows.append(view_row("c@example.invalid"))  # and gives c, unknown to it so far, its data
    run = opsdb.sync(CFG)
    assert run.lifted == 2 and InternalAccount.kinds() == {"b@example.invalid": "unused"}  # b is as it was
    assert InternalAccount.objects.get(account_key="a@example.invalid").removed_by == "운영 DB 갱신"
    apply_internal_lists()
    n.refresh_from_db()
    assert n.status == "draft_ready" and n.recipients == ["owner@example.invalid"]


def test_a_csv_upload_never_lifts_an_unused_account():
    from django.utils import timezone
    from customers.importer import import_csv
    from customers.models import InternalAccount
    InternalAccount.objects.create(account_key="a@example.invalid", kind="unused", added_at=timezone.now())
    assert import_csv("account,email\na@example.invalid,x@example.invalid\n".encode(), "f.csv", "general").changed == ["a@example.invalid"]
    assert InternalAccount.kinds() == {"a@example.invalid": "unused"}
