import secrets

from django import template
from django.utils.safestring import mark_safe

from web import labels

register = template.Library()

# Simple 2px-stroke glyphs drawn for this site (no icon library, no emoji).
_ICONS = {
    "refresh": '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 7l8.5 6 8.5-6"/>',
    "alert": '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.2v.1"/>',
    "list": '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 20a6.5 6.5 0 0 0-2-4.7"/>',
    "upload": '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "arrow-left": '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "message": '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-4.5 4v-4h0A1.5 1.5 0 0 1 4 14.5z"/>',
    "building": '<rect x="5" y="3.5" width="14" height="17" rx="1.5"/><path d="M9 8h2M13 8h2M9 12h2M13 12h2M10.5 20.5v-4h3v4"/>',
    "archive": '<rect x="3.5" y="4" width="17" height="4.5" rx="1"/><path d="M5 8.5v10a1.5 1.5 0 0 0 1.5 1.5h11a1.5 1.5 0 0 0 1.5-1.5v-10M10 13h4"/>',
    "download": '<path d="M12 4v11M7 10.5l5 5 5-5"/><path d="M4.5 19.5h15"/>',
    "settings": '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
}


@register.simple_tag
def icon(name):
    # width/height on the element itself: the icon stays 16px even when a stale stylesheet is cached.
    return mark_safe(
        '<svg class="icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
        f"{_ICONS[name]}</svg>"
    )


@register.filter
def reasons(text):
    return labels.translate(text, labels.REASON)


@register.filter
def problems(text):
    return labels.translate(text, labels.PROBLEM)


@register.filter
def customer_problems(text):
    return labels.translate(text, labels.CUSTOMER_PROBLEM)


@register.filter
def category(code):
    return labels.CATEGORY.get(code, code)


@register.filter
def who(n):
    """Name shown for a customer notification."""
    if n.display_name:
        return f"{n.display_name} {n.group.name}" if n.group_id else n.display_name
    return {"internal": "사내 계정", "unused": "안 쓰는 계정"}.get(n.status, "고객 정보 없음")


# Attention first when a status column is sorted ascending.
STATUS_ORDER = ["send_failed", "needs_review", "sent_copy_candidate", "draft_ready", "copied", "send_requested",
                "manual_sent_recorded", "sent_copy_confirmed", "staff_confirmed", "internal", "unused"]


@register.filter
def status_order(code):
    return STATUS_ORDER.index(code) if code in STATUS_ORDER else len(STATUS_ORDER)


@register.filter
def work_state(code):
    return labels.WORK_STATE.get(code, code)


@register.filter
def get(mapping, key):
    return mapping.get(key, "") if mapping else ""


# A mail's own HTML in a sandboxed frame (unique origin: no access to this site, its cookies or forms). Images
# load as in Outlook (user, 2026-10-19: they were missing; the sender can then tell the mail was opened). The
# mail's own scripts never run: only the one fitting script below carries the per-render nonce.
FIT = ("(function(){function fit(){var d=document.documentElement;d.style.zoom='';"
       "var w=Math.max(d.scrollWidth,document.body?document.body.scrollWidth:0),v=window.innerWidth;"
       "if(w>v+2){d.style.zoom=((v-6)/w).toFixed(3);}}"
       "addEventListener('load',fit);addEventListener('resize',fit);fit();})();")


@register.filter
def mail_doc(html):
    """srcdoc for web/_mail_body.html: a mail wider than the frame is scaled down to fit. Left unescaped here;
    the template escapes it into the attribute."""
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; style-src 'unsafe-inline'; img-src http: https: data:; font-src data:; "
           f"script-src 'nonce-{nonce}'")
    return ('<!doctype html><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
            '<base target="_blank"><style>body{margin:16px;font-family:"Malgun Gothic",sans-serif;'
            'font-size:14px;color:#222;background:#fff}img{max-width:100%}</style>'
            + (html or "") + f'<script nonce="{nonce}">{FIT}</script>')
