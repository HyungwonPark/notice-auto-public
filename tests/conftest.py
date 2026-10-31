import pytest

from mailcollect import headers
from mailcollect.config import ScanConfig
from tests.fakepop3 import FakePOP3, Message
from tests.mailfactory import NOTICE, URGENT, encoded_word as ew, message

# 30 messages, oldest first (u0001 is message number 1). uidl -> expected category.
_SPECIAL = {
    "u0003": (message(ew(NOTICE)), headers.KT_NOTICE),
    "u0010": (message(ew(URGENT)), headers.KT_URGENT),
    "u0012": (message(ew("[kt cloud 작업 완료 공지]")), headers.KT_COMPLETE),
    "u0014": (message(ew("[kt Cloud 작업 취소 공지]")), headers.KT_CANCEL),
    "u0017": (message(ew(NOTICE + " (2차)")), headers.KT_NOTICE),
    "u0020": (message(ew("[예시클라우드] 작업 공지 안내"), sender="ExampleCloud <system@example.com>"), headers.NC24_NOTICE),
    "u0022": (message(ew("[예시클라우드]예시회사 님의 질문에 대한 답변입니다."), sender="ExampleCloud <system@example.com>"), headers.OTHER),
    "u0024": (message(ew("[kt cloud] 이용 요금 안내")), headers.KT_OTHER),
    "u0028": (message(ew("FW: " + NOTICE), sender="동료 <colleague@example.invalid>"), headers.SUSPECT),
    "u0029": (message(ew(NOTICE + " (3차)")), headers.KT_NOTICE),
}
MAILBOX = []
EXPECTED = {}
for n in range(1, 31):
    uidl = f"u{n:04d}"
    raw, category = _SPECIAL.get(uidl, (message(ew(f"업무 메일 {n}"), sender="팀 <team@example.invalid>"), headers.OTHER))
    MAILBOX.append(Message(uidl, raw))
    EXPECTED[uidl] = category


@pytest.fixture(autouse=True)
def no_real_mailboxes(settings, tmp_path_factory):
    """Never read this host's real mailbox files: extra mailboxes come only from a test's own directory."""
    settings.CSP_POP3_EXTRA_DIR = str(tmp_path_factory.mktemp("mailboxes"))
    settings.CSP_POP3_ENV_FILE = str(tmp_path_factory.mktemp("main") / "absent.env")
    settings.CSP_OPSDB_ENV = str(tmp_path_factory.mktemp("opsdb") / "absent.env")  # nor the company DB
    settings.CSP_SENS_ENV = str(tmp_path_factory.mktemp("sens") / "absent.env")  # nor ever send mail
    settings.CSP_SEND_LOCK = str(tmp_path_factory.mktemp("lock") / "send.lock")


@pytest.fixture
def fake():
    server = FakePOP3(MAILBOX).start()
    yield server
    server.stop()


@pytest.fixture
def make_cfg(tmp_path):
    def make(server, **overrides):
        options = dict(timeout=5, batch_size=200, max_seconds=60, lock_path=str(tmp_path / "scan.lock"))
        options.update(overrides)
        return ScanConfig(host=server.host, port=server.port, username=server.user, password=server.password, **options)

    return make
