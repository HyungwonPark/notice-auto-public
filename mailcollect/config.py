"""POP3 settings, read at run time from root-owned env files.

/etc/csp-notice/pop3.env is the main mailbox. Each /etc/csp-notice/mailboxes/<name>.env adds one more; such a
mailbox is registered disabled and is read only after `manage.py mailboxes --enable <name>`."""
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ScanConfig:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    timeout: float = 30
    batch_size: int = 200
    body_batch_size: int = 50
    max_seconds: float = 60
    lock_path: str = "/data/csp-notice/var/scan.lock"
    name: str = "main"  # "main" for pop3.env, else the file name without .env
    label: str = ""  # shown in the site, e.g. 알림 메일함
    collect: str = "all"  # "all": KT notices, 예시클라우드 copies, suspects; "kt": KT work notices only
    backfill: int = 0  # first listing: read only the newest N messages, skip older ones (0: read all)

    @property
    def title(self) -> str:
        return self.label or ("기본 메일함" if self.name == "main" else self.name)


def read_env_file(path) -> dict[str, str]:
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


COLLECT = ("all", "kt")


def load_config(path, name="main", **scan_options) -> ScanConfig:
    try:
        env = read_env_file(path)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    missing = [key for key in ("POP3_USER", "POP3_PASSWORD") if not env.get(key)]
    if missing:
        raise ConfigError(f"{path}: missing {', '.join(missing)}")
    collect = env.get("COLLECT", "all" if name == "main" else "kt").lower()
    if collect not in COLLECT:
        raise ConfigError(f"{path}: COLLECT must be one of {', '.join(COLLECT)}")
    try:
        port, backfill = int(env.get("POP3_PORT", "110")), int(env.get("BACKFILL", "0" if name == "main" else "2000"))
    except ValueError:
        raise ConfigError(f"{path}: POP3_PORT and BACKFILL must be numbers") from None
    return ScanConfig(
        host=env.get("POP3_HOST", "mail.example.com"),
        port=port,
        username=env["POP3_USER"],
        password=env["POP3_PASSWORD"],
        name=name,
        label=env.get("LABEL", ""),
        collect=collect,
        backfill=backfill,
        **scan_options,
    )


def extra_env_files(directory) -> list[tuple[str, Path]]:
    """(name, path) of each extra mailbox file, sorted by name. A missing directory means none."""
    try:
        return sorted((p.stem, p) for p in Path(directory).glob("*.env") if p.is_file() and p.stem != "main")
    except OSError:
        return []
