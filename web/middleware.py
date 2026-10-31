from django.contrib import messages
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from .models import PasswordPolicy

OPEN = {"password_change", "logout", "login"}  # reachable while a new password is still owed


class PasswordChangeRequired:
    """Send a sign-in that owes a new password to the change page, whatever page it asks for."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if PasswordPolicy.required(request.user) and not request.path_info.startswith("/static/"):
            try:
                name = resolve(request.path_info).url_name
            except Resolver404:
                name = None
            if name not in OPEN:
                return redirect("password_change")
        return self.get_response(request)


# Pages whose changes need the 관리 계정 (user, 2026-10-22): settings, customers, recipient groups. The shared
# sign-in sees them but cannot save. POST-only addresses go back to the page that shows them.
MANAGE = {"config": None, "customer_new": None, "customer_edit": None, "customers_sync": "customers",
          "internal_accounts": None, "internal_remove": "internal_accounts",
          "internal_company_remove": "internal_accounts", "channel_groups": None}


class ManagerOnly:
    """Refuse a change on a MANAGE page from a sign-in that is not a 관리 계정 (User.is_staff)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = request.resolver_match
        if (request.method == "POST" and request.user.is_authenticated and not request.user.is_staff
                and match is not None and match.url_name in MANAGE):
            messages.error(request, "관리 계정만 바꿀 수 있습니다.")
            back = MANAGE[match.url_name]
            return redirect(back) if back else redirect(request.path)
        return None
