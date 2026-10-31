from pathlib import Path

import pytest

from mailcollect.pop3 import ForbiddenPOP3Command, ReadOnlyPOP3


@pytest.fixture
def conn(fake):
    c = ReadOnlyPOP3(fake.host, fake.port, timeout=5)
    c.user(fake.user)
    c.pass_(fake.password)
    yield c
    c.quit()


@pytest.mark.parametrize(
    "attempt",
    [
        lambda c: c.dele(1),
        lambda c: c.rset(),
        lambda c: c._shortcmd("DELE 1"),
        lambda c: c._shortcmd("dele 1"),
        lambda c: c.stls(),
        lambda c: c.utf8(),
    ],
    ids=["dele", "rset", "raw DELE", "raw lower-case dele", "stls", "utf8"],
)
def test_forbidden_commands_never_reach_the_server(fake, conn, attempt):
    with pytest.raises(ForbiddenPOP3Command):
        attempt(conn)
    conn.stat()  # session still usable
    assert fake.verbs() == ["USER", "PASS", "STAT"]


def test_error_never_echoes_command_arguments(conn):
    with pytest.raises(ForbiddenPOP3Command) as exc:
        conn.apop("tester", "top-secret")
    assert "top-secret" not in str(exc.value)


def test_no_delete_call_in_collector_code():
    """Tripwire: collector code must not even contain a delete call."""
    for path in (Path(__file__).parent.parent / "mailcollect").rglob("*.py"):
        assert ".dele(" not in path.read_text(encoding="utf-8"), path
