from django.db import models
from django.utils import timezone


class Suggestion(models.Model):
    """An error report or improvement idea written in the site, then handled and answered one by one.

    Read on the server with `manage.py suggestions` when the next round of improvements is planned."""

    class Kind(models.TextChoices):
        ERROR = "error", "오류"
        IDEA = "idea", "개선"
        OTHER = "other", "기타"

    class Status(models.TextChoices):
        NEW = "new", "접수"
        DOING = "doing", "진행 중"
        DONE = "done", "반영 완료"
        HOLD = "hold", "보류"

    OPEN = (Status.NEW, Status.DOING)

    kind = models.CharField(max_length=10, choices=Kind.choices)
    page = models.CharField(max_length=200, blank=True)  # where in the site, in the writer's words
    body = models.TextField()
    author = models.CharField(max_length=50, blank=True)  # name typed in the page (shared login)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    reply = models.TextField(blank=True)
    handled_by = models.CharField(max_length=50, blank=True)
    handled_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    def handle(self, status: str, reply: str, name: str) -> None:
        self.status, self.reply, self.handled_by, self.handled_at = status, reply, name, timezone.now()
        self.save()
