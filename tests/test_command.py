from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from tests.mailfactory import NOTICE

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def lock_in_tmp(settings, tmp_path):
    settings.CSP_SCAN = {**settings.CSP_SCAN, "lock_path": str(tmp_path / "scan.lock"), "timeout": 5}


def write_env(path, fake, password=None):
    path.write_text(
        f"# test\nPOP3_HOST={fake.host}\nPOP3_PORT={fake.port}\nPOP3_USER={fake.user}\n"
        f"POP3_PASSWORD={fake.password if password is None else password}\n",
        encoding="utf-8",
    )
    return path


def test_prints_counts_and_only_kept_subjects(fake, tmp_path):
    out = StringIO()
    call_command("scan_mail", env_file=write_env(tmp_path / "pop3.env", fake), stdout=out)
    text = out.getvalue()

    assert "status=ok server=30 new=30 headers=30 matched=6 bodies=7" in text
    assert (
        "kt_notice=3 kt_urgent=1 kt_complete=1 kt_cancel=1 kt_incident=0 kt_recovery=0 kt_check=0 kt_postpone=0 kt_other=1 "
        "nc24_notice=1 nc24_complete=0 nc24_postpone=0 partner_notice=0 partner_complete=0 suspect=1 other=21"
    ) in text
    assert NOTICE in text
    assert "업무 메일" not in text  # subjects of "other" mail are never printed
    assert "이용 요금" not in text  # nor of kt_other mail


def test_login_failure_exits_with_error(fake, tmp_path):
    with pytest.raises(CommandError, match="login_failed"):
        call_command("scan_mail", env_file=write_env(tmp_path / "pop3.env", fake, password="wrong"), stdout=StringIO())


def test_missing_env_file(tmp_path):
    with pytest.raises(CommandError, match="cannot read"):
        call_command("scan_mail", env_file=tmp_path / "absent.env")


def test_missing_password_is_named_not_shown(tmp_path):
    env = tmp_path / "pop3.env"
    env.write_text("POP3_USER=tester\n", encoding="utf-8")
    with pytest.raises(CommandError, match="missing POP3_PASSWORD"):
        call_command("scan_mail", env_file=env)
