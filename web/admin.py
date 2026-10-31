from django.contrib import admin
from django.contrib.auth.models import Group

from customers.models import InternalAccount, PartnerChannel
from mailcollect.models import ScanRun

from .models import PasswordPolicy

# One shared main login runs the site, so groups are not used.
admin.site.unregister(Group)
admin.site.site_header = admin.site.site_title = "작업 공지 안내 관리"
admin.site.index_title = "관리"


@admin.register(ScanRun)
class ScanRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "status", "server_count", "new_count", "headers_fetched", "bodies_fetched", "error")
    list_filter = ("status",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InternalAccount)
class InternalAccountAdmin(admin.ModelAdmin):
    list_display = ("account_key", "note", "active", "added_by", "added_at", "removed_by", "removed_at")
    list_filter = ("active",)


@admin.register(PartnerChannel)
class PartnerChannelAdmin(admin.ModelAdmin):
    """별도 발송: a tab per partner, its customers' accounts (one per line) and the address it sends from."""
    list_display = ("label", "sender", "active", "created_at")


@admin.register(PasswordPolicy)
class PasswordPolicyAdmin(admin.ModelAdmin):
    """Tick 'must change' to make a sign-in choose a new password at the next page it opens."""
    list_display = ("user", "must_change", "requested_at", "changed_at")
