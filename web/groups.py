"""별도 발송 그룹: which system's people get the notices naming its servers (customers.PartnerGroup)."""
import re
from collections import Counter
from datetime import timedelta

from django.utils import timezone

from customers.models import PartnerChannel, PartnerGroup, server_key
from mailcollect.models import Notice
from notifications.service import KIND_OF_KT, sent_at

DAYS = 90  # 그룹에 없는 서버: KT notices of this long
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MAX_NAME = 60


def unmapped(channel: PartnerChannel, days: int = DAYS) -> list[dict]:
    """Servers KT named for the channel's accounts lately that no group has, newest first."""
    since = timezone.now() - timedelta(days=days)
    notices = (Notice.objects.filter(category__in=KIND_OF_KT, targets__account__in=channel.account_keys,
                                     seen__first_seen_at__gte=since)
               .select_related("seen__header").distinct())
    count, last = Counter(), {}
    for notice in notices:
        _, unknown = channel.split(notice.servers + notice.lbs)
        for server in unknown:
            count[server] += 1
            last[server] = max(last.get(server, sent_at(notice)), sent_at(notice))
    return [{"name": s, "notices": count[s], "last": last[s]} for s in sorted(count, key=lambda s: last[s], reverse=True)]


def lines(text: str) -> list[str]:
    return [line.strip() for line in re.split(r"[;\n]", text or "") if line.strip()]


def from_post(post, channel: PartnerChannel, group: PartnerGroup | None) -> tuple[dict, list[str]]:
    """The group's fields as typed, and what is wrong with them."""
    data = {
        "name": post.get("name", "").strip(),
        "mail_to": "\n".join(lines(post.get("mail_to"))),
        "mail_cc": "\n".join(lines(post.get("mail_cc"))),
        "servers": "\n".join(dict.fromkeys(s.strip() for s in post.get("servers", "").splitlines() if s.strip())),
    }
    problems = []
    if not data["name"]:
        problems.append("그룹 이름을 입력하세요.")
    elif len(data["name"]) > MAX_NAME:
        problems.append(f"그룹 이름은 {MAX_NAME}자까지입니다.")
    elif channel.groups.filter(active=True, name=data["name"]).exclude(pk=group.pk if group else None).exists():
        problems.append("같은 이름의 그룹이 있습니다.")
    if not data["mail_to"]:
        problems.append("받는 사람을 한 명 이상 입력하세요.")
    for field, label in (("mail_to", "받는 사람"), ("mail_cc", "참조")):
        bad = [a for a in lines(data[field]) if not EMAIL.search(a)]
        if bad:
            problems.append(f"{label}에 메일 주소가 아닌 줄이 있습니다: {', '.join(bad[:3])}")
    if not data["servers"]:
        problems.append("서버를 한 대 이상 입력하세요.")
    return data, problems


def aliases_from_post(text: str) -> tuple[str, list[str]]:
    """'KT 이름 = 목록의 이름' lines, cleaned, and the lines that are not."""
    kept, bad = [], []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        kt, sep, ours = line.partition("=")
        if not sep or not server_key(kt) or not server_key(ours):
            bad.append(line.strip())
        else:
            kept.append(f"{kt.strip()} = {ours.strip()}")
    return "\n".join(dict.fromkeys(kept)), bad
