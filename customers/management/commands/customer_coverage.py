from collections import Counter

from django.core.management.base import BaseCommand

from customers.models import CustomerAccount
from mailcollect.models import NoticeTarget


class Command(BaseCommand):
    help = "How many 대상계정명 in parsed notices exist in the active customer rows (counts only)."

    def handle(self, *args, **options):
        rows = CustomerAccount.objects.filter(active=True)
        kinds = dict(rows.values_list("account_key", "kind"))
        with_email = set(rows.exclude(emails=[]).values_list("account_key", flat=True))
        targets = NoticeTarget.objects.values_list("account", "notice__category")
        by_category = Counter()
        accounts = {}
        for account, category in targets:
            source = "kt" if category.startswith("kt_") else "nc24"
            found = account in kinds
            by_category[(source, found)] += 1
            accounts[account] = found
        found = sum(accounts.values())
        self.stdout.write(f"distinct notice accounts: {len(accounts)} found={found} missing={len(accounts) - found}")
        self.stdout.write(f"  found with at least one email: {sum(1 for a, f in accounts.items() if f and a in with_email)}")
        self.stdout.write("  found by kind: " + ", ".join(
            f"{k}={n}" for k, n in sorted(Counter(kinds[a] for a, f in accounts.items() if f).items())))
        for (source, ok), n in sorted(by_category.items()):
            self.stdout.write(f"  {source} notice targets {'found' if ok else 'missing'}: {n}")
