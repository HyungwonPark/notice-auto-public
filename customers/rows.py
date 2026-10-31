"""Derived fields of a customer row. The source columns are never rewritten; everything here sits beside them.
Pure functions, no I/O."""
import re
from dataclasses import dataclass

# Source column -> model field. The operations DB query is expected to return the same columns.
COLUMNS = {
    "account": "account",
    "userid": "userid",
    "name": "name",
    "회사명": "company",
    "담당자": "manager",
    "email": "email",
    "phone": "phone",
}
REQUIRED = ("account", "email")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SEPARATORS = re.compile(r"[\s,;/]+")


@dataclass(frozen=True)
class Derived:
    account_key: str
    display_name: str
    emails: list[str]
    problems: list[str]  # generic descriptions only, never values


def derive(account: str, name: str, company: str, email: str) -> Derived:
    problems = []
    account_key = account.strip().lower()
    if not account_key:
        problems.append("no account")
    elif not _EMAIL.fullmatch(account_key):
        problems.append("account is not an email address")

    # One cell may hold several addresses on separate lines. Each line is kept as written in `email`;
    # only the addresses are lifted out. Names and phones are never paired with them.
    emails = []
    for addr in _EMAIL.findall(email):
        if addr.lower() not in emails:
            emails.append(addr.lower())
    if not emails:
        problems.append("no email address")
    elif _SEPARATORS.sub("", _EMAIL.sub("", email)):
        problems.append("email cell has characters besides addresses")

    display_name = company.strip() or name.strip()
    if not display_name:
        problems.append("no 회사명 or name")
    return Derived(account_key, display_name, emails, problems)
