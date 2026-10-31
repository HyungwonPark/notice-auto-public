"""Load one customer CSV (account,userid,name,회사명,담당자,email,phone) as the current rows of one kind.
Shared by the import_customers command, the upload page and the operations-DB sync (opsdb.py), which hands
in the same columns."""
import csv
import hashlib
import io
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from .models import CustomerAccount, ImportBatch
from .rows import COLUMNS, REQUIRED


class CsvError(Exception):
    pass


@dataclass
class ImportResult:
    batch: ImportBatch | None  # None when the same file was already loaded
    rows: int = 0
    review: int = 0
    replaced: int = 0
    repeated: int = 0
    emails: int = 0
    kept_web: int = 0
    problems: Counter = field(default_factory=Counter)
    ignored: list[str] = field(default_factory=list)
    unchanged_batch: ImportBatch | None = None
    changed: list[str] = field(default_factory=list)  # accounts whose row is new or differs from the current one

    @property
    def ok(self) -> int:
        return self.rows - self.review


def import_csv(raw: bytes, file_name: str, kind: str, force: bool = False) -> ImportResult:
    digest = hashlib.sha256(raw).hexdigest()
    last = ImportBatch.objects.filter(kind=kind).order_by("-pk").first()
    if last and last.sha256 == digest and not force:
        return ImportResult(batch=None, unchanged_batch=last)

    reader = csv.DictReader(io.StringIO(_decode(raw), newline=""))
    header = {(name or "").strip(): name for name in reader.fieldnames or []}
    missing = [col for col in REQUIRED if col not in header]
    if missing:
        raise CsvError(f"missing column(s): {', '.join(missing)}; found: {', '.join(header) or 'none'}")
    records = [{fld: (record.get(header[col]) or "") for col, fld in COLUMNS.items() if col in header}
               for record in reader]
    result = load_rows(records, kind, CustomerAccount.Source.CSV, file_name, digest, raw)
    result.ignored = [name for name in header if name not in COLUMNS]
    return result


def load_rows(records: list[dict], kind: str, source: str, file_name: str, digest: str, raw: bytes | None,
              override_web: bool = False) -> ImportResult:
    """Make `records` (model field -> value, as the source gives them) the current rows of `kind`.

    Every earlier CSV or DB row of the kind becomes inactive history. Accounts edited on the web are kept
    (a file never overwrites a person's correction) unless `override_web`: the operations DB is the master
    of its accounts, so its row replaces the web row of the same account — but never a row marked 웹에서만
    관리 (web_only). Web rows for accounts the DB does not list are kept either way."""
    web_edited = dict(
        CustomerAccount.objects.filter(kind=kind, source=CustomerAccount.Source.WEB, active=True)
        .values_list("account_key", "web_only")
    )
    fields = list(COLUMNS.values())
    current = defaultdict(set)  # account -> its source columns as they stand before this load
    for values in CustomerAccount.objects.filter(kind=kind, active=True).values_list("account_key", *fields):
        current[values[0]].add(values[1:])
    rows, kept_web, overridden = [], 0, []
    for values in records:
        if not any(v.strip() for v in values.values()):
            continue  # blank line
        row = CustomerAccount(kind=kind, source=source, **values)
        row.refresh_derived()  # bulk_create skips save()
        if row.account_key in web_edited:
            if not override_web or web_edited[row.account_key]:
                kept_web += 1
                continue
            overridden.append(row.account_key)
        rows.append(row)

    with transaction.atomic():
        batch = ImportBatch.objects.create(
            kind=kind, file_name=file_name[:255], sha256=digest, rows=len(rows), raw=raw,
            review_rows=sum(r.status == CustomerAccount.Status.REVIEW for r in rows),
        )
        replaced = CustomerAccount.objects.filter(
            kind=kind, source__in=[CustomerAccount.Source.CSV, CustomerAccount.Source.DB], active=True
        ).update(active=False)
        if overridden:
            replaced += CustomerAccount.objects.filter(
                kind=kind, source=CustomerAccount.Source.WEB, active=True, account_key__in=overridden
            ).update(active=False)
        for row in rows:
            row.batch = batch
        CustomerAccount.objects.bulk_create(rows)

    return ImportResult(
        batch=batch,
        rows=len(rows),
        review=batch.review_rows,
        replaced=replaced,
        repeated=sum(n - 1 for n in Counter(r.account_key for r in rows if r.account_key).values() if n > 1),
        emails=sum(len(r.emails) for r in rows),
        kept_web=kept_web,
        problems=Counter(p for r in rows for p in r.problems.split("\n") if p),
        changed=list(dict.fromkeys(r.account_key for r in rows if r.account_key
                                   and tuple(getattr(r, f) for f in fields) not in current[r.account_key])),
    )


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp949")  # Excel on Korean Windows saves CSV as CP949
