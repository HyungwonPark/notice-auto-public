"""Requiring a new password for the shared sign-in, and changing it."""
import pytest
from django.core.management import call_command
from django.urls import reverse

from web.models import PasswordPolicy

pytestmark = pytest.mark.django_db
OLD = "old-pass-for-tests-1"
NEW = "brand-new-pass-for-tests-2"


@pytest.fixture
def signed_in(client, django_user_model):
    user = django_user_model.objects.create_user("examplecloud", password=OLD)
    client.force_login(user)
    return user


def test_required_change_sends_every_page_to_the_form_until_done(client, signed_in, capsys):
    call_command("require_password_change", "examplecloud")
    assert "must choose a new password" in capsys.readouterr().out
    for url in (reverse("dashboard"), reverse("review"), "/admin/"):
        response = client.get(url)
        assert response.status_code == 302 and response.url == reverse("password_change")
    page = client.get(reverse("password_change")).content.decode()
    assert "관리자가 비밀번호 변경을 요청했습니다" in page

    weak = client.post(reverse("password_change"), {"old_password": OLD, "new_password1": "12345678",
                                                    "new_password2": "12345678"})
    assert weak.status_code == 200 and 'aria-invalid="true"' in weak.content.decode()
    assert PasswordPolicy.objects.get().must_change

    done = client.post(reverse("password_change"), {"old_password": OLD, "new_password1": NEW,
                                                    "new_password2": NEW}, follow=True)
    assert "비밀번호를 바꿨습니다" in done.content.decode()
    signed_in.refresh_from_db()
    assert signed_in.check_password(NEW) and not PasswordPolicy.objects.get().must_change
    assert client.get(reverse("dashboard")).status_code == 200  # still signed in, nothing owed


def test_change_page_is_reachable_but_not_in_the_menu_and_can_be_lifted(client, signed_in, capsys):
    page = client.get(reverse("dashboard")).content.decode()
    assert f'href="{reverse("password_change")}"' not in page  # the user had the top-bar link removed
    assert "관리자가 비밀번호 변경을 요청했습니다" not in client.get(reverse("password_change")).content.decode()
    call_command("require_password_change", "examplecloud")
    call_command("require_password_change", "examplecloud", "--off")
    assert client.get(reverse("dashboard")).status_code == 200
