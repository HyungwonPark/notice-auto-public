from django.contrib import admin

from .models import Suggestion


@admin.register(Suggestion)
class SuggestionAdmin(admin.ModelAdmin):
    list_display = ("pk", "created_at", "kind", "status", "author", "page", "handled_by")
    list_filter = ("status", "kind")
    search_fields = ("body", "reply", "page", "author")
