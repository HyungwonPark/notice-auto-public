"""The company's operations DB (MySQL), read through one view that lists every 일반 KT Cloud account with its
customer contacts. Its columns (userid, cloud_account, name, company, manager, email, tel1-3, phone1-3) are
mapped onto the customer CSV's (account, userid, name, 회사명, 담당자, email, phone) and loaded through the
same path as a CSV, so the site treats both alike. 공공 customers are not in the view and stay as they are.
The DB is the master of the accounts it lists (user decision, 2026-10-20): its row replaces a web edit of
the same account, and such rows are shown read-only (fix them in the operations DB).

Read-only: this module only ever runs SELECT. Connection details live in /etc/csp-notice/opsdb.env
(root:cspnotice 0640), never in code or the repo. sync() runs before every scan and from the customers page."""
import csv
import dataclasses
import hashlib
import io
import time
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from mailcollect.config import ConfigError, read_env_file

from .importer import ImportResult, load_rows
from .models import CustomerAccount, ImportBatch, InternalAccount, OpsDbSync

DEFAULT_PORT = 3306
DEFAULT_DATABASE = "examplecloud"
DEFAULT_VIEW = "v_customer_accounts"
SOURCE_NAME = "운영 DB"  # ImportBatch.file_name of a sync
KIND = CustomerAccount.Kind.GENERAL
VIEW_COLUMNS = {"cloud_account": "account", "userid": "userid", "name": "name", "company": "company",
                "manager": "manager", "email": "email"}


@dataclass(frozen=True)
class OpsDbConfig:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str = DEFAULT_DATABASE
    view: str = DEFAULT_VIEW
    connect_timeout: int = 10
    read_timeout: int = 60

    @property
    def table(self) -> str:
        return f"`{self.database}`.`{self.view}`"


def load_config(path=None) -> OpsDbConfig:
    path = Path(path or settings.CSP_OPSDB_ENV)
    try:
        env = read_env_file(path)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    missing = [key for key in ("OPSDB_HOST", "OPSDB_USER", "OPSDB_PASSWORD") if not env.get(key)]
    if missing:
        raise ConfigError(f"{path}: missing {', '.join(missing)}")
    try:
        port = int(env.get("OPSDB_PORT", str(DEFAULT_PORT)))
    except ValueError:
        raise ConfigError(f"{path}: OPSDB_PORT must be a number") from None
    for key in ("OPSDB_DATABASE", "OPSDB_VIEW"):
        value = env.get(key, "")
        if value and not value.replace("_", "").isalnum():
            raise ConfigError(f"{path}: {key} may hold only letters, digits and _")
    return OpsDbConfig(
        host=env["OPSDB_HOST"], port=port, user=env["OPSDB_USER"], password=env["OPSDB_PASSWORD"],
        database=env.get("OPSDB_DATABASE") or DEFAULT_DATABASE, view=env.get("OPSDB_VIEW") or DEFAULT_VIEW,
    )


def configured() -> bool:
    return Path(settings.CSP_OPSDB_ENV).is_file()


def connect(cfg: OpsDbConfig):
    import pymysql  # imported here so the rest of the site never needs the driver

    return pymysql.connect(
        host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password, database=cfg.database,
        charset="utf8mb4", connect_timeout=cfg.connect_timeout, read_timeout=cfg.read_timeout,
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
    )


def probe(cfg: OpsDbConfig) -> dict:
    """Column names, row count and timing of the view — never a value from it."""
    started = time.monotonic()
    with connect(cfg) as con, con.cursor() as cur:
        connected = time.monotonic() - started
        cur.execute(f"SELECT * FROM {cfg.table} LIMIT 0")
        columns = [d[0] for d in cur.description]
        cur.execute(f"SELECT COUNT(*) AS n FROM {cfg.table}")
        count = cur.fetchone()["n"]
    return {"columns": columns, "rows": count, "connect_seconds": round(connected, 3),
            "total_seconds": round(time.monotonic() - started, 3)}


def fetch_rows(cfg: OpsDbConfig) -> list[dict]:
    """Every row of the view, as dicts keyed by the view's column names."""
    with connect(cfg) as con, con.cursor() as cur:
        cur.execute(f"SELECT * FROM {cfg.table}")
        return list(cur.fetchall())


def _phone(row: dict) -> str:
    """phone1-2-3 is the mobile number, tel1-2-3 the office line; the CSV holds one phone column."""
    for prefix in ("phone", "tel"):
        parts = [str(row.get(f"{prefix}{i}") or "").strip() for i in (1, 2, 3)]
        if all(parts):
            return "-".join(parts)
    return ""


def to_records(view_rows: list[dict]) -> list[dict]:
    """View rows -> the CSV's columns (model field names), in a fixed order so equal data hashes equal."""
    records = []
    for row in view_rows:
        values = {fld: str(row.get(col) or "").strip() for col, fld in VIEW_COLUMNS.items()}
        values["phone"] = _phone(row)
        records.append(values)
    records.sort(key=lambda r: (r["account"].lower(), r["email"].lower(), r["userid"]))
    return records


def as_csv(records: list[dict]) -> bytes:
    """The records as the CSV the site would have been given: hashed to skip unchanged syncs, kept as history."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["account", "userid", "name", "회사명", "담당자", "email", "phone"])
    for r in records:
        writer.writerow([r[f] for f in CustomerAccount.SOURCE_COLUMNS])
    return out.getvalue().encode("utf-8")


class Refused(Exception):
    """The view answered, but with so few rows that loading them would wipe customers: kept the current ones."""


MIN_SHARE = 0.8  # a read with fewer rows than this share of the last good read is a broken view, not churn
INTERACTIVE_TIMEOUTS = {"connect_timeout": 5, "read_timeout": 20}  # under the web worker's 30 s limit


def sync(cfg: OpsDbConfig | None = None, force: bool = False, interactive: bool = False) -> OpsDbSync:
    """Read the view and make its rows the current 일반 customers, unless nothing changed since the last load.

    Every outcome is recorded as an OpsDbSync row (the customers page shows the latest); a failure is
    recorded, not raised, so a DB outage never stops mail collection. An empty view, or one that suddenly
    lists far fewer accounts, is refused the same way unless `force`. `interactive` (the web button) uses
    shorter timeouts so a hung DB cannot outlast the web worker."""
    run = OpsDbSync.objects.create(started_at=timezone.now())
    try:
        cfg = cfg or load_config()
        if interactive:
            cfg = dataclasses.replace(cfg, **INTERACTIVE_TIMEOUTS)
        records = to_records(fetch_rows(cfg))
        raw = as_csv(records)
        digest = hashlib.sha256(raw).hexdigest()
        run.rows = len(records)
        previous = OpsDbSync.objects.filter(ok=True, rows__gt=0).exclude(pk=run.pk).order_by("-pk").first()
        if not force and not records:
            raise Refused("the view returned no rows; kept the current customers")
        if not force and previous and len(records) < previous.rows * MIN_SHARE:
            raise Refused(f"the view returned {len(records)} rows, last time {previous.rows}; kept the current customers")
        latest = ImportBatch.objects.filter(kind=KIND).order_by("-pk").first()
        if latest is None or latest.sha256 != digest or force:
            result: ImportResult = load_rows(records, KIND, CustomerAccount.Source.DB, SOURCE_NAME, digest, raw,
                                             override_web=True)  # the DB is the master of the accounts it lists
            run.batch = result.batch
            run.lifted = InternalAccount.lift_unused(result.changed, timezone.now())
        run.ok = True
    except Exception as exc:  # driver, network or config errors: the words only
        run.error = f"{type(exc).__name__}: {exc}"[:1000]
    run.finished_at = timezone.now()
    run.save()
    old = OpsDbSync.objects.order_by("-pk").values_list("pk", flat=True)[OpsDbSync.KEEP:]
    if old:
        OpsDbSync.objects.filter(pk__in=list(old)).delete()
    return run
