from collections import Counter

from django.core.management.base import BaseCommand

from mailcollect.models import Notice
from mailcollect.notice_store import parse_pending


class Command(BaseCommand):
    help = "Parse stored notice bodies into fields (local only). Prints counts and problem types, never values."

    def add_arguments(self, parser):
        parser.add_argument("--reparse", action="store_true", help="parse every stored body again")

    def handle(self, *args, reparse, **options):
        counts = parse_pending(reparse=reparse)
        self.stdout.write(f"parsed: {sum(counts.values())}")
        for (category, status), n in sorted(counts.items()):
            self.stdout.write(f"  {category:<13} {status:<6} {n}")
        problems = Counter(p for text in Notice.objects.exclude(problems="").values_list("problems", flat=True)
                           for p in text.split("\n"))
        for problem, n in problems.most_common():
            self.stdout.write(f"  problem: {problem} x{n}")
