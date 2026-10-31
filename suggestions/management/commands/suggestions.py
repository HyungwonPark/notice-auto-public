from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from suggestions.models import Suggestion


class Command(BaseCommand):
    help = "List suggestions written in the site (open ones by default), or record how one was handled."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="every suggestion, not only open ones")
        parser.add_argument("--id", type=int, help="only this suggestion")
        parser.add_argument("--set", type=int, metavar="ID", help="update this suggestion (with --status, --by)")
        parser.add_argument("--status", choices=Suggestion.Status.values)
        parser.add_argument("--reply", default="", help="answer shown under the suggestion in the site")
        parser.add_argument("--by", default="", help="name shown as the handler")

    def handle(self, *args, **opts):
        if opts["set"]:
            if not opts["status"] or not opts["by"]:
                raise CommandError("--set needs --status and --by")
            s = Suggestion.objects.filter(pk=opts["set"]).first()
            if s is None:
                raise CommandError(f"no suggestion {opts['set']}")
            s.handle(opts["status"], opts["reply"] or s.reply, opts["by"])
            self.stdout.write(f"#{s.pk} -> {s.get_status_display()}")
            return
        rows = Suggestion.objects.order_by("pk")
        if opts["id"]:
            rows = rows.filter(pk=opts["id"])
        elif not opts["all"]:
            rows = rows.filter(status__in=Suggestion.OPEN)
        for s in rows:
            when = timezone.localtime(s.created_at).strftime("%Y-%m-%d %H:%M")
            self.stdout.write(f"#{s.pk} [{s.get_status_display()}] {s.get_kind_display()}, {when}, "
                              f"{s.author or '이름 없음'}" + (f", {s.page}" if s.page else ""))
            self.stdout.write("  " + s.body.replace("\n", "\n  "))
            if s.reply:
                self.stdout.write(f"  -> {s.handled_by}: " + s.reply.replace("\n", "\n     "))
        self.stdout.write(f"{rows.count()} shown, {Suggestion.objects.filter(status__in=Suggestion.OPEN).count()} open")
