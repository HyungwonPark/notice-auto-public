"""NAVER Cloud SENS mail (v2): the only way this site itself sends mail (user decision, 2026-10-21).

Settings live in /etc/csp-notice/sens.env (root:cspnotice 0640), never in code or the repo:

    NCP_ACCESS_KEY / NCP_SECRET_KEY   API key (a sub-account limited to SENS mail)
    SENS_MAIL_SERVICE_ID              ncp:mail:kr:<number>:<project>
    MAIL_SENDER_ADDRESS               system@example.com, as the notices are sent now: its copies match as before
    MAIL_SENDER_NAME                  optional (the notices now show "ExampleCloud")
    MAIL_CC                           in-house address(es) shown as 참조 on every customer mail
    MAIL_BCC                          in-house address(es) that get a copy (the collected mailbox)
    SEND_MODE                         off blocks sending on this server (rehearsal, restored backup); else on
    MAIL_TEST_TO                      optional: test mode's only recipient (no button since 2026-10-22)

API: https://api.ncloud-docs.com/docs/sens-mail-request-create and common-ncpapi (signature)."""
import base64
import hashlib
import hmac
import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode

from django.conf import settings

from mailcollect.config import ConfigError, read_env_file

MODES = ("off", "on", "test", "live")  # SEND_MODE: off blocks sending on this server; any other allows ON
DEFAULT_HOST = "sens.apigw.ntruss.com"
TIMEOUT = 20  # seconds; the scan service allows minutes, the web button 30 s
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass(frozen=True)
class SensConfig:
    mode: str
    access_key: str = ""
    secret_key: str = field(default="", repr=False)
    service_id: str = ""
    sender_address: str = ""
    sender_name: str = ""
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    test_to: tuple[str, ...] = ()
    host: str = DEFAULT_HOST

    @property
    def on(self) -> bool:
        return self.mode in ("test", "live")


OFF = SensConfig(mode="off")


def _addresses(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(a.lower() for a in _EMAIL.findall(value or "")))


def load_config(path=None) -> SensConfig:
    """The sending settings. No file means sending is off; a broken file is an error, never a guess."""
    path = Path(path or settings.CSP_SENS_ENV)
    if not path.is_file():
        return OFF
    try:
        env = read_env_file(path)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    mode = env.get("SEND_MODE", "off").strip().lower() or "off"
    if mode not in MODES:
        raise ConfigError(f"{path}: SEND_MODE must be one of {', '.join(MODES)}")
    missing = [k for k in ("NCP_ACCESS_KEY", "NCP_SECRET_KEY", "SENS_MAIL_SERVICE_ID", "MAIL_SENDER_ADDRESS")
               if not env.get(k)]
    if missing:
        if mode == "off":
            return OFF
        raise ConfigError(f"{path}: missing {', '.join(missing)}")
    # With keys, "off" still lets mail already accepted get SENS's result; it only stops new sending. Whether
    # this server sends at all is chosen on 환경 설정 (자동 발송 ON/OFF, dispatch.config()); test and on are the
    # same as live here since the test mode left that page (user, 2026-10-21).
    cfg = SensConfig(
        mode="off" if mode == "off" else "live", access_key=env["NCP_ACCESS_KEY"], secret_key=env["NCP_SECRET_KEY"],
        service_id=env["SENS_MAIL_SERVICE_ID"], sender_address=env["MAIL_SENDER_ADDRESS"].strip(),
        sender_name=env.get("MAIL_SENDER_NAME", "").strip(), cc=_addresses(env.get("MAIL_CC", "")),
        bcc=_addresses(env.get("MAIL_BCC", "")),
        test_to=_addresses(env.get("MAIL_TEST_TO", "")), host=env.get("SENS_HOST", "").strip() or DEFAULT_HOST,
    )
    if not _EMAIL.fullmatch(cfg.sender_address):
        raise ConfigError(f"{path}: MAIL_SENDER_ADDRESS is not an email address")
    return cfg


class SensError(Exception):
    """kind: retry (nothing was accepted: try again later), unknown (it may have been accepted: look it up before
    any new attempt), permanent (this mail will never be accepted as it is), global (keys, permission, service id
    or the monthly quota: nothing can be sent until a person fixes it)."""

    def __init__(self, kind: str, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.kind, self.status, self.code = kind, status, code


def classify(status: int, code: str) -> str:
    if status in (401, 403, 404) or code == "ResourceQuotaExceeded":
        return "global"
    if status == 429:
        return "retry"  # ThrottlingExceeded: refused, nothing queued
    if 400 <= status < 500:
        return "permanent"  # InvalidParameter, ContentTooLarge
    return "unknown"  # 5xx: the server may have queued it before failing


def signature(secret_key: str, method: str, url: str, timestamp: str, access_key: str) -> str:
    """HMAC-SHA256 of "METHOD url\\ntimestamp\\naccessKey", url being the path with its query string."""
    message = f"{method} {url}\n{timestamp}\n{access_key}"
    digest = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class Client:
    def __init__(self, cfg: SensConfig, opener=urllib.request.urlopen, clock=time.time):
        self.cfg, self.opener, self.clock = cfg, opener, clock

    @property
    def base(self) -> str:
        return f"/mail/v2/services/{self.cfg.service_id}/requests"

    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = path + ("?" + urlencode(query, doseq=True, quote_via=quote) if query else "")
        timestamp = str(int(self.clock() * 1000))
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "x-ncp-apigw-timestamp": timestamp,
            "x-ncp-iam-access-key": self.cfg.access_key,
            "x-ncp-apigw-signature-v2": signature(self.cfg.secret_key, method, url, timestamp, self.cfg.access_key),
        }
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = urllib.request.Request(f"https://{self.cfg.host}{url}", data=data, method=method, headers=headers)
        try:
            with self.opener(request, timeout=TIMEOUT) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read() or b"{}").get("error") or {}
            except ValueError:
                error = {}
            code = str(error.get("errorCode", ""))
            raise SensError(classify(exc.code, code), f"HTTP {exc.code} {code} {error.get('message', '')}".strip(),
                            exc.code, code) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                raise SensError("retry", f"{type(reason).__name__}: {reason}") from None  # never reached SENS
            raise SensError("unknown", f"{type(reason).__name__}: {reason}") from None  # timeout: may have arrived
        except (TimeoutError, http.client.HTTPException, ConnectionError) as exc:
            raise SensError("unknown", f"{type(exc).__name__}: {exc}") from None

    def send(self, payload: dict) -> dict:
        return self._call("POST", self.base, body=payload)

    def request_status(self, request_id: str) -> dict:
        return self._call("GET", f"{self.base}/{quote(request_id, safe='')}")

    def find_requests(self, since, until, recipient: str, title: str) -> list[dict]:
        """Requests SENS accepted for this recipient and title between two aware datetimes."""
        query = {"fromDateTime": since.isoformat(timespec="seconds"), "toDateTime": until.isoformat(timespec="seconds"),
                 "recipientAddress": recipient, "title": title, "pageSize": 100}
        return self._call("GET", self.base, query=query).get("requests") or []
