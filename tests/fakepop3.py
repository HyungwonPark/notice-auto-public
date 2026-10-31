"""Minimal threaded POP3 server for tests. Mimics the real server's replies and records every command."""
import socketserver
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    uidl: str
    raw: bytes


class FakePOP3:
    def __init__(self, messages=(), user="tester", password="secret"):
        self.messages = list(messages)
        self.user, self.password = user, password
        self.reject_login = False  # answer PASS with -ERR (wrong password / maildrop locked)
        self.drop_after_tops = None  # close the socket instead of answering TOP number n+1
        self.drop_after_retrs = None  # same for RETR
        self.top_errors = set()  # UIDLs whose TOP is answered with -ERR
        self.retr_errors = set()  # UIDLs whose RETR is answered with -ERR
        self.sessions: list[list[str]] = []  # one command log per connection, PASS masked
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self.host, self.port = self._server.server_address
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def verbs(self, session=None):
        logs = self.sessions if session is None else [self.sessions[session]]
        return [line.split(" ", 1)[0] for log in logs for line in log]


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        fake = self.server.fake
        snapshot = list(fake.messages)  # message numbers stay fixed for the session, as in RFC 1939
        log: list[str] = []
        fake.sessions.append(log)
        user, authed, deleted = None, False, set()
        served = {"TOP": 0, "RETR": 0}
        self._send(b"+OK <1792126684> POP3 Service Ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("ascii", "replace").rstrip("\r\n")
            verb, _, arg = text.partition(" ")
            verb = verb.upper()
            log.append("PASS ***" if verb == "PASS" else text)

            if verb == "QUIT":
                fake.messages = [m for i, m in enumerate(snapshot, 1) if i not in deleted]
                self._send(b"+OK bye")
                return
            if verb == "USER":
                user = arg
                self._send(b"+OK")
            elif verb == "PASS":
                if fake.reject_login or user != fake.user or arg != fake.password:
                    self._send(b"-ERR [IN-USE] maildrop locked")
                else:
                    authed = True
                    self._send(b"+OK")
            elif not authed:
                self._send(b"-ERR not authenticated")
            elif verb == "STAT":
                self._send(f"+OK {len(snapshot)} {sum(len(m.raw) for m in snapshot)}".encode())
            elif verb == "UIDL" and not arg:
                self._multi([f"{i} {m.uidl}".encode() for i, m in enumerate(snapshot, 1)])
            elif verb in ("TOP", "RETR", "DELE"):
                args = arg.split()
                num = int(args[0]) if args and args[0].isdigit() else 0
                if not 1 <= num <= len(snapshot):
                    self._send(b"-ERR no such message")
                    continue
                msg = snapshot[num - 1]
                if verb == "DELE":
                    deleted.add(num)
                    self._send(b"+OK marked")
                    continue
                served[verb] += 1
                drop_after = fake.drop_after_tops if verb == "TOP" else fake.drop_after_retrs
                if drop_after is not None and served[verb] > drop_after:
                    return  # abrupt disconnect
                if msg.uidl in (fake.top_errors if verb == "TOP" else fake.retr_errors):
                    self._send(b"-ERR no such message")
                    continue
                lines = (msg.raw[:-2] if msg.raw.endswith(b"\r\n") else msg.raw).split(b"\r\n")
                if verb == "TOP":
                    blank = lines.index(b"")
                    lines = lines[: blank + 1 + (int(args[1]) if len(args) > 1 else 0)]
                self._multi(lines)
            else:
                self._send(b"-ERR not found command")  # CAPA and STLS answer like the real server

    def _send(self, line: bytes):
        self.wfile.write(line + b"\r\n")

    def _multi(self, lines: list[bytes]):
        out = [b"+OK"] + [b"." + ln if ln.startswith(b".") else ln for ln in lines] + [b"."]
        self.wfile.write(b"\r\n".join(out) + b"\r\n")
