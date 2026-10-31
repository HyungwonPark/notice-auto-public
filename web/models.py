from django.conf import settings
from django.db import models


class PasswordPolicy(models.Model):
    """A sign-in that must choose a new password before using the site (set with `manage.py
    require_password_change`). The site has one shared sign-in, so this is how its password is rotated."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="password_policy")
    must_change = models.BooleanField(default=False)
    requested_at = models.DateTimeField(null=True)
    changed_at = models.DateTimeField(null=True)

    @classmethod
    def required(cls, user) -> bool:
        return user.is_authenticated and cls.objects.filter(user=user, must_change=True).exists()
