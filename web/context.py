from django.conf import settings

from customers.models import PartnerChannel
from notifications.models import Notification
from suggestions.models import Suggestion

from .scope import needs_attention_all

S = Notification.Status


def _revision() -> str:
    """Deployed git revision (ops/deploy.sh writes REVISION); changes the asset URLs on every deploy."""
    try:
        return (settings.BASE_DIR / "REVISION").read_text(encoding="ascii").strip()[:12]
    except OSError:
        return "dev"


ASSET_VERSION = _revision()


def nav(request):
    """Asset version for cache busting, and the numbers on the 확인 필요 and 건의사항 tabs."""
    context = {"asset_version": ASSET_VERSION}
    if getattr(request, "user", None) and request.user.is_authenticated:
        review, late, unchecked = needs_attention_all()  # every tab, as the 확인 필요 page lists
        context["nav_review"] = review.count() + late.count() + unchecked.count()
        context["nav_suggestions"] = Suggestion.objects.filter(status=Suggestion.Status.NEW).count()
        context["nav_channels"] = PartnerChannel.objects.filter(active=True).order_by("pk")
        context["can_manage"] = request.user.is_staff  # 관리 계정: settings, customers, groups (web.middleware)
    return context
