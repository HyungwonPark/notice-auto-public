import pytest
from django.core.management import call_command
from django.urls import reverse

from suggestions.models import Suggestion

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("main", password="pw-for-tests"))


def test_write_list_and_handle(client, staff):
    response = client.post(reverse("suggestions"), {"kind": "error", "page": "달력", "body": "달력에서\n작업이 안 보임",
                                                    "actor": "홍길동"}, follow=True)
    page = response.content.decode()
    assert "건의사항을 남겼습니다" in page and "달력에서<br>작업이 안 보임" in page
    s = Suggestion.objects.get()
    assert (s.kind, s.status, s.author, s.page) == ("error", "new", "홍길동", "달력")
    assert '<span class="count quiet">1</span>' in client.get(reverse("dashboard")).content.decode()

    client.post(reverse("suggestion", args=[s.pk]), {"status": "done", "reply": "고쳤습니다", "actor": ""})
    s.refresh_from_db()
    assert s.status == "new"  # a handler name is required

    client.post(reverse("suggestion", args=[s.pk]), {"status": "done", "reply": "고쳤습니다", "actor": "박관리"})
    s.refresh_from_db()
    assert (s.status, s.reply, s.handled_by) == ("done", "고쳤습니다", "박관리") and s.handled_at
    assert "처리 전인 건의사항이 없습니다" in client.get(reverse("suggestions")).content.decode()
    closed = client.get(reverse("suggestions"), {"show": "closed"}).content.decode()
    assert "고쳤습니다" in closed and "반영 완료" in closed


def test_empty_body_is_refused(client, staff):
    response = client.post(reverse("suggestions"), {"kind": "idea", "body": "  "}, follow=True)
    assert "내용을 적어 주세요" in response.content.decode() and not Suggestion.objects.exists()


def test_nav_has_suggestions_instead_of_site_admin(client, django_user_model):
    client.force_login(django_user_model.objects.create_superuser("root-admin", password="pw-for-tests"))
    page = client.get(reverse("dashboard")).content.decode()
    assert "건의사항" in page and "사이트 관리" not in page and 'href="/admin/"' not in page


def test_command_lists_open_and_records_handling(capsys):
    a = Suggestion.objects.create(kind="idea", body="첫 줄\n둘째 줄", author="홍길동")
    Suggestion.objects.create(kind="error", body="끝난 일", status="done")
    call_command("suggestions")
    out = capsys.readouterr().out
    assert f"#{a.pk} [접수] 개선" in out and "  둘째 줄" in out and "끝난 일" not in out and "1 shown, 1 open" in out

    call_command("suggestions", set=a.pk, status="doing", reply="다음 배포", by="Claude Code")
    a.refresh_from_db()
    assert (a.status, a.reply, a.handled_by) == ("doing", "다음 배포", "Claude Code")
    call_command("suggestions", all=True)
    assert "끝난 일" in capsys.readouterr().out
