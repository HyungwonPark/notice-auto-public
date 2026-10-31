from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from web.models import PasswordPolicy


class Command(BaseCommand):
    help = "Make a sign-in choose a new password before it can use the site again (or lift that with --off)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--off", action="store_true", help="no longer require it")

    def handle(self, *args, username, off, **options):
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"no sign-in named {username}")
        policy, _ = PasswordPolicy.objects.get_or_create(user=user)
        policy.must_change = not off
        if not off:
            policy.requested_at = timezone.now()
        policy.save()
        self.stdout.write(f"{username}: " + ("no change required" if off else
                                             "must choose a new password at the next page it opens"))
