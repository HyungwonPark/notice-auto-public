"""Customer master on synthetic CSVs that reproduce the real files' quirks (made-up values only)."""
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from customers.models import CustomerAccount, ImportBatch
from customers.rows import derive
from mailcollect.models import Mailbox, MessageSeen, Notice, NoticeTarget

pytestmark = pytest.mark.django_db

HEADER = "account,userid,name,회사명,담당자,email,phone"
PUBLIC = "\r\n".join([
    HEADER,
    'gov_a@example.invalid,,,가나기관,"담당 갑 주임\r\n담당 을 대리","a1@example.invalid\r\na2@example.invalid","010-0000-0001\r\n010-0000-0002"',
    " Lead.Space@example.invalid,,,다라기관,담당 병,b1@example.invalid,010-0000-0003",
    "",
])
GENERAL = "\r\n".join([
    HEADER,
    "c1@example.invalid,cust1,,(주)예시,담당 정,c1-owner@example.invalid>,010-0000-0004",
    "c2@example.invalid,cust2,개인 이름,,,c2-owner@example.invalid,010-0000-0005",
    "c3@example.invalid,cust3,,,,,010-0000-0006",
    "not-an-email,cust4,,회사,,d@example.invalid,",
    "c1@example.invalid,cust1,,(주)예시,담당 정,c1-owner@example.invalid,010-0000-0004",
    "",
])


def write(tmp_path, name, text, encoding="utf-8-sig"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def load(path, kind, **kw):
    out = StringIO()
    call_command("import_customers", str(path), kind=kind, stdout=out, **kw)
    return out.getvalue()


def test_derive_rules():
    d = derive(" Gov_A@Example.invalid", "", "기관", "x@example.invalid\r\nY@example.invalid")
    assert (d.account_key, d.display_name, d.emails, d.problems) == (
        "gov_a@example.invalid", "기관", ["x@example.invalid", "y@example.invalid"], [])
    assert derive("a@example.invalid", "이름", "", "z@example.invalid>").problems == [
        "email cell has characters besides addresses"]
    assert derive("a@example.invalid", "이름", "", "z@example.invalid").display_name == "이름"
    assert derive("", "", "", "").problems == ["no account", "no email address", "no 회사명 or name"]


def test_public_file_keeps_multiline_cells_verbatim(tmp_path):
    out = load(write(tmp_path, "public.csv", PUBLIC), "public")

    assert "rows=2 ok=2 review=0" in out
    row = CustomerAccount.objects.get(account_key="gov_a@example.invalid")
    assert row.manager == "담당 갑 주임\r\n담당 을 대리"  # verbatim, never paired with emails
    assert row.emails == ["a1@example.invalid", "a2@example.invalid"]
    assert row.phone == "010-0000-0001\r\n010-0000-0002"
    lead = CustomerAccount.objects.get(account_key="lead.space@example.invalid")
    assert lead.account == " Lead.Space@example.invalid"  # source kept as written


def test_general_file_problems_are_counted_without_values(tmp_path):
    out = load(write(tmp_path, "general.csv", GENERAL), "general")

    assert "rows=5 ok=2 review=3 replaced=0 repeated_accounts=1" in out
    assert "problem: email cell has characters besides addresses x1" in out
    assert "problem: no email address x1" in out
    assert "problem: no 회사명 or name x1" in out
    assert "problem: account is not an email address x1" in out
    assert "example.invalid" not in out
    assert CustomerAccount.objects.get(account_key="c2@example.invalid").display_name == "개인 이름"


def test_reload_replaces_and_keeps_history(tmp_path):
    load(write(tmp_path, "general.csv", GENERAL), "general")
    out = load(write(tmp_path, "general2.csv", GENERAL.replace("cust4", "cust5")), "general")

    assert "replaced=5" in out
    assert CustomerAccount.objects.filter(active=True).count() == 5
    assert CustomerAccount.objects.filter(active=False).count() == 5
    assert ImportBatch.objects.count() == 2


def test_same_file_twice_is_a_no_op_unless_forced(tmp_path):
    path = write(tmp_path, "public.csv", PUBLIC)
    load(path, "public")
    assert "unchanged" in load(path, "public")
    assert "replaced=2" in load(path, "public", force=True)


def test_kinds_are_replaced_independently(tmp_path):
    load(write(tmp_path, "public.csv", PUBLIC), "public")
    load(write(tmp_path, "general.csv", GENERAL), "general")
    assert CustomerAccount.objects.filter(active=True).count() == 7


def test_cp949_file_and_spaced_header(tmp_path):
    text = PUBLIC.replace(HEADER, "account ,userid, name, 회사명, 담당자,   email, phone")
    out = load(write(tmp_path, "public.csv", text, encoding="cp949"), "public")
    assert "rows=2 ok=2" in out


def test_missing_required_column(tmp_path):
    with pytest.raises(CommandError, match="missing column"):
        load(write(tmp_path, "bad.csv", "account,name\r\na@example.invalid,x\r\n"), "public")


def test_web_edit_recomputes_derived_fields(tmp_path):
    load(write(tmp_path, "general.csv", GENERAL), "general")
    row = CustomerAccount.objects.get(account_key="c3@example.invalid")
    row.email, row.company = "fixed@example.invalid", "고친 회사"
    row.save()
    row.refresh_from_db()
    assert (row.status, row.emails, row.display_name) == ("ok", ["fixed@example.invalid"], "고친 회사")


def test_web_edit_keeps_history_and_survives_reimport(tmp_path, django_user_model):
    user = django_user_model.objects.create_user("staff")
    load(write(tmp_path, "general.csv", GENERAL), "general")
    old = CustomerAccount.objects.get(account_key="c3@example.invalid")
    new = old.edit(user, email="fixed@example.invalid", company="고친 회사")
    old.refresh_from_db()
    assert (old.active, new.active, new.source, new.replaces, new.edited_by) == (False, True, "web", old, user)
    assert (new.status, new.emails, new.userid) == ("ok", ["fixed@example.invalid"], "cust3")

    out = load(write(tmp_path, "general2.csv", GENERAL + "x9@example.invalid,,,새 회사,,x9@example.invalid,\r\n"),
               "general")
    assert "kept_web_edits=1" in out
    active = CustomerAccount.objects.filter(active=True, account_key="c3@example.invalid")
    assert list(active.values_list("source", "email")) == [("web", "fixed@example.invalid")]


def test_coverage_counts_notice_accounts(tmp_path):
    load(write(tmp_path, "public.csv", PUBLIC), "public")
    box = Mailbox.objects.create(host="h", port=110, username="u")
    seen = MessageSeen.objects.create(mailbox=box, uidl="x", first_seen_at="2026-10-18T00:00Z",
                                      last_seen_at="2026-10-18T00:00Z", category="kt_notice")
    notice = Notice.objects.create(seen=seen, category="kt_notice", status="ok", parser_version=2)
    NoticeTarget.objects.bulk_create([NoticeTarget(notice=notice, account="gov_a@example.invalid"),
                                      NoticeTarget(notice=notice, account="unknown@example.invalid")])
    out = StringIO()
    call_command("customer_coverage", stdout=out)
    assert "distinct notice accounts: 2 found=1 missing=1" in out.getvalue()
    assert "found by kind: public=1" in out.getvalue()


def test_customers_added_on_the_site_can_be_reviewed_deleted_and_put_back(client, django_user_model):
    """사이트에서 추가만: a 관리 계정 decides whether a customer added on the site stays (user, 2026-11-05)."""
    from customers.models import CustomerAccount
    from django.urls import reverse
    CustomerAccount.objects.create(kind="general", source="db", account="db@example.invalid", email="d@example.invalid")
    kept = CustomerAccount.objects.create(kind="general", source="web", account="db@example.invalid",
                                          email="w@example.invalid", web_only=True)
    gone = CustomerAccount.objects.create(kind="general", source="web", account="site@example.invalid",
                                          email="s@example.invalid")
    shared = django_user_model.objects.create_user("shared", password="pw-for-tests")
    client.force_login(shared)
    page = client.get(reverse("customers") + "?added=1").content.decode()
    assert "site@example.invalid" in page and 'name="ids"' not in page  # the shared sign-in only sees them
    client.post(reverse("customers_remove"), {"ids": [gone.pk], "actor": "홍길동"})
    assert CustomerAccount.objects.get(pk=gone.pk).active

    client.force_login(django_user_model.objects.create_user("admin", password="pw-for-tests", is_staff=True))
    page = client.get(reverse("customers") + "?added=1").content.decode()
    assert page.count('name="ids"') == 2 and '<span class="tag yellow">없음</span>' in page  # not in the DB
    assert client.post(reverse("customers_remove"), {"ids": [gone.pk]}).status_code == 302  # no name: refused
    assert CustomerAccount.objects.get(pk=gone.pk).active
    client.post(reverse("customers_remove"), {"ids": [gone.pk], "actor": "홍길동"})
    gone.refresh_from_db()
    assert not gone.active and gone.removed_by == "홍길동" and gone.removed_at
    page = client.get(reverse("customers") + "?added=1").content.decode()
    listed, _, deleted = page.partition('id="removed"')
    assert "site@example.invalid" not in listed and "site@example.invalid" in deleted and "홍길동" in deleted
    client.post(reverse("customers_remove"), {"ids": [gone.pk], "undo": "1"})
    gone.refresh_from_db()
    assert gone.active and not gone.removed_at and kept.active
