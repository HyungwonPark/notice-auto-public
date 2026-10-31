"""Read-only POP3 client. The collector must never delete, move or alter mail on the server."""
import poplib
from contextlib import contextmanager


class ForbiddenPOP3Command(RuntimeError):
    """Raised before a command outside the read-only allowlist reaches the server."""


class LoginRejected(Exception):
    """USER/PASS refused: wrong credentials, or the maildrop is locked by another client (Outlook)."""


class ReadOnlyPOP3(poplib.POP3):
    # RETR reads a message without changing it (POP3 has no "seen" flag). DELE, RSET, APOP, STLS
    # and UTF8 never pass.
    ALLOWED = frozenset({"USER", "PASS", "STAT", "UIDL", "TOP", "RETR", "QUIT"})

    def dele(self, which):
        raise ForbiddenPOP3Command("DELE")

    def _putcmd(self, line):
        verb = line.split(" ", 1)[0].upper()
        if verb not in self.ALLOWED:
            raise ForbiddenPOP3Command(verb)  # never echo the line: PASS carries the password
        super()._putcmd(line)


def server_text(exc: Exception) -> str:
    """Short, printable error text. poplib puts the server's -ERR line in args[0] as bytes."""
    msg = exc.args[0] if exc.args else ""
    text = msg.decode("ascii", "replace") if isinstance(msg, bytes) else str(exc)
    return text[:200]


@contextmanager
def open_mailbox(cfg):
    conn = ReadOnlyPOP3(cfg.host, cfg.port, timeout=cfg.timeout)
    try:
        try:
            conn.user(cfg.username)
            conn.pass_(cfg.password)
        except poplib.error_proto as exc:
            raise LoginRejected(server_text(exc)) from None
        yield conn
    finally:
        try:
            conn.quit()  # ends the session; with no DELE sent, nothing is removed
        except (poplib.error_proto, OSError):
            conn.close()
