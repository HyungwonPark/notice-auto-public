import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cspnotice.settings")
application = get_wsgi_application()

from django.conf import settings  # noqa: E402  (settings are only configured after the line above)

if settings.SECRET_KEY == settings.DEV_SECRET_KEY:
    raise RuntimeError("no secret key: run ops/systemd/install.sh to create /etc/csp-notice/secret_key")
