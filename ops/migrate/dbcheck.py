"""Integrity check and row counts of a CSP notice DB copy, to compare both ends of a server move.

Prints counts only, never row contents. Usage: dbcheck.py DB_FILE (exit 1 if the copy is damaged).
"""
import sys

import pysqlite3 as sqlite3

TABLES = ("auth_user", "customers_customeraccount", "customers_internalaccount", "customers_partnerchannel",
          "mailcollect_mailbox", "mailcollect_messageseen", "mailcollect_notice", "mailcollect_work",
          "notifications_notification", "notifications_notificationevent")

# immutable: a copy that nothing writes to; reading it then creates no -wal/-shm files beside it.
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro&immutable=1", uri=True)
ok = con.execute("PRAGMA integrity_check").fetchone()[0]
print(f"integrity {ok}")
for table in TABLES:
    print(f"{table} {con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]}")
sys.exit(0 if ok == "ok" else 1)
