import re
from types import SimpleNamespace

import httpx
import pytest
from argon2 import PasswordHasher
from conftest import INGEST_TOKEN, PASSWORDS
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import delete
from starlette.routing import Route
from teacher_common.config import Settings
from teacher_common.db import session_scope
from teacher_common.models import AuthSession

PUBLIC = {("GET", "/health"), ("GET", "/login"), ("POST", "/login")}


@pytest.fixture
def client(api_main, monkeypatch):
    api_main.login_limiter.reset()
    with session_scope() as db:
        db.execute(delete(AuthSession))
    monkeypatch.setattr(
        api_main,
        "generate_teaching_response",
        lambda task, prompt_input, level, top_k, workspace_ids: {
            "answer": f"{task}: {prompt_input}", "sources": []
        },
    )
    return TestClient(api_main.app, follow_redirects=False)


def login_form_token(client: TestClient) -> str:
    page = client.get("/login")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


def login(client: TestClient, username: str = "apa", password: str | None = None, **extra) -> httpx.Response:
    token = login_form_token(client)
    data = {"username": username, "password": PASSWORDS[username] if password is None else password,
            "csrf_token": token, **extra}
    return client.post("/login", data=data)


def session_csrf(client: TestClient) -> str:
    return client.get("/session").json()["csrf_token"]


def test_health_login_and_static_are_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_every_other_route_requires_login(api_main, client):
    checked = 0
    for route in api_main.app.routes:
        if not isinstance(route, APIRoute | Route):
            continue
        for method in route.methods or []:
            if method == "HEAD" or (method, route.path) in PUBLIC:
                continue
            response = client.request(method, route.path)
            assert response.status_code == 401, f"{method} {route.path} answered {response.status_code}"
            checked += 1
    # chat, quiz, summary, flashcards, sources, ingest, voice, logout, session, index, docs...
    assert checked >= 14


def test_browser_is_sent_to_login_page(client):
    response = client.get("/sources?x=1", headers={"Accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/sources%3Fx%3D1"


def test_login_flow_session_and_logout(client):
    response = login(client, next="/")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "teacher_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie

    page = client.get("/")
    assert page.status_code == 200
    assert "apa" in page.text
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    assert csrf == session_csrf(client)

    answer = client.post("/chat", json={"question": "Mi a fotoszintézis?"}, headers={"X-CSRF-Token": csrf})
    assert answer.status_code == 200
    assert answer.json()["answer"] == "answer: Mi a fotoszintézis?"
    assert client.get("/sources").status_code == 200

    old_token = client.cookies.get("teacher_session")
    assert client.post("/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/sources").status_code == 401

    # The token is revoked server-side, not just cleared in the browser.
    client.cookies.set("teacher_session", old_token)
    assert client.get("/sources").status_code == 401


def test_state_changing_requests_need_csrf_token(client):
    login(client)
    assert client.post("/chat", json={"question": "Mi a fotoszintézis?"}).status_code == 403
    bad = client.post("/chat", json={"question": "Mi a fotoszintézis?"}, headers={"X-CSRF-Token": "nope"})
    assert bad.status_code == 403
    assert client.post("/logout").status_code == 403


def test_study_routes_need_session_and_csrf(client):
    assert client.get("/study/quizzes").status_code == 401
    login(client)
    assert client.get("/study/quizzes").status_code == 200
    assert client.post("/study/decks", json={"topic": "sejt"}).status_code == 403
    assert client.delete("/study/quizzes/1").status_code == 403
    deleted = client.delete("/study/quizzes/1", headers={"X-CSRF-Token": session_csrf(client)})
    assert deleted.status_code == 404


@pytest.mark.parametrize("origin", ["https://evil.example", "https://[", "null"])
def test_cross_origin_post_is_rejected(client, origin):
    login(client)
    csrf = session_csrf(client)
    response = client.post(
        "/chat",
        json={"question": "Mi a fotoszintézis?"},
        headers={"X-CSRF-Token": csrf, "Origin": origin},
    )
    assert response.status_code == 403
    same_origin = client.post(
        "/chat",
        json={"question": "Mi a fotoszintézis?"},
        headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
    )
    assert same_origin.status_code == 200


def test_wrong_password_and_unknown_user_are_refused(client):
    for username, password in [("apa", "rossz-jelszo"), ("apa", PASSWORDS["anya"])]:
        response = login(client, username=username, password=password)
        assert response.status_code == 401
        assert "Hibás felhasználónév vagy jelszó." in response.text
        assert "teacher_session" not in client.cookies
    token = login_form_token(client)
    response = client.post("/login", data={"username": "ismeretlen", "password": "x", "csrf_token": token})
    assert response.status_code == 401


def test_login_form_needs_matching_csrf_token(client):
    login_form_token(client)
    response = client.post("/login", data={"username": "apa", "password": PASSWORDS["apa"], "csrf_token": "forged"})
    assert response.status_code == 400
    assert "teacher_session" not in client.cookies


def test_login_is_rate_limited(api_main, client):
    for _ in range(api_main.settings.teacher_login_max_attempts):
        assert login(client, password="rossz-jelszo").status_code == 401
    blocked = login(client)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert "Túl sok sikertelen próbálkozás" in blocked.text
    assert "teacher_session" not in client.cookies


def test_parallel_wrong_passwords_cannot_outrun_the_limit(api_main, client):
    from concurrent.futures import ThreadPoolExecutor

    data = {"username": "apa", "password": "rossz-jelszo", "csrf_token": "form-token"}

    def attempt(_: int) -> int:
        guesser = TestClient(api_main.app, cookies={"teacher_login_csrf": "form-token"})
        return guesser.post("/login", data=data).status_code

    attempts = api_main.settings.teacher_login_max_attempts * 3
    with ThreadPoolExecutor(attempts) as pool:
        statuses = list(pool.map(attempt, range(attempts)))
    assert statuses.count(401) == api_main.settings.teacher_login_max_attempts
    assert statuses.count(429) == attempts - api_main.settings.teacher_login_max_attempts


def test_rate_limiter_needs_at_least_one_attempt():
    with pytest.raises(ValueError):
        Settings(TEACHER_LOGIN_MAX_ATTEMPTS=0)


def test_admin_role_is_explicit_and_unknown_admins_are_rejected():
    hashed = PasswordHasher().hash("long-enough-password")
    settings = Settings(TEACHER_AUTH_USERS=f"apa:{hashed};anya:{hashed}", TEACHER_AUTH_ADMINS="apa, anya")
    assert settings.auth_admins() == {"apa", "anya"}
    with pytest.raises(ValueError, match="unknown users"):
        Settings(TEACHER_AUTH_USERS=f"apa:{hashed}", TEACHER_AUTH_ADMINS="intruder").auth_admins()


def test_host_header_cannot_make_a_route_public(client):
    for host in ["x/static/", "x/health?", "x/login#"]:
        assert client.get("/sources", headers={"host": host}).status_code == 401
        assert client.post("/chat", json={"question": "Mi a fotoszintézis?"}, headers={"host": host}).status_code == 401


def test_non_ascii_csrf_values_are_refused_not_crashed(client):
    login_form_token(client)
    response = client.post("/login", data={"username": "apa", "password": PASSWORDS["apa"], "csrf_token": "ő"})
    assert response.status_code == 400
    login(client)
    response = client.post("/chat", json={"question": "Mi a fotoszintézis?"}, headers={b"X-CSRF-Token": "é".encode()})
    assert response.status_code == 403


def test_redirect_after_login_stays_on_site(client):
    for target in ["//evil.example/", "https://evil.example/", "/\\evil.example"]:
        client.cookies.clear()
        response = login(client, next=target)
        assert response.headers["location"] == "/"
    client.cookies.clear()
    assert login(client, next="/sources").headers["location"] == "/sources"


def test_cookie_is_secure_behind_https(api_main, client, monkeypatch):
    token = login_form_token(client)
    response = client.post(
        "/login",
        data={"username": "apa", "password": PASSWORDS["apa"], "csrf_token": token},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert "Secure" in response.headers["set-cookie"]

    plain = TestClient(api_main.app, follow_redirects=False)
    assert "Secure" not in login(plain).headers["set-cookie"]

    monkeypatch.setattr(api_main.settings, "teacher_cookie_secure", "true")
    forced = TestClient(api_main.app, follow_redirects=False)
    assert "Secure" in forced.get("/login").headers["set-cookie"]


def test_password_change_or_removed_user_ends_sessions(api_main, client, monkeypatch):
    login(client)
    assert client.get("/sources").status_code == 200
    users = dict(api_main.auth_users)
    users["apa"] = PasswordHasher().hash("uj-jelszo-789")
    monkeypatch.setattr(api_main, "auth_users", users)
    assert client.get("/sources").status_code == 401

    login(client, username="anya")
    monkeypatch.setattr(api_main, "auth_users", {"apa": users["apa"]})
    assert client.get("/sources").status_code == 401


def test_security_headers(client):
    response = client.get("/login")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_reindex_forwards_ingest_token(api_main, client, monkeypatch):
    monkeypatch.setattr(api_main, "auth_admins", {"apa"})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "started"})

    fake_httpx = SimpleNamespace(Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(api_main, "httpx", fake_httpx)
    login(client)
    response = client.post("/ingest/reindex", json={}, headers={"X-CSRF-Token": session_csrf(client)})
    assert response.status_code == 200
    assert seen["authorization"] == f"Bearer {INGEST_TOKEN}"


def _fake_ingestion(api_main, monkeypatch, handler) -> None:
    fake_httpx = SimpleNamespace(
        Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw), HTTPError=httpx.HTTPError
    )
    monkeypatch.setattr(api_main, "httpx", fake_httpx)


def test_reindex_while_a_run_is_going_answers_409_not_500(api_main, client, monkeypatch):
    monkeypatch.setattr(api_main, "auth_admins", {"apa"})
    _fake_ingestion(api_main, monkeypatch, lambda request: httpx.Response(409, json={"detail": "running"}))
    login(client)
    response = client.post("/ingest/reindex", json={}, headers={"X-CSRF-Token": session_csrf(client)})
    assert response.status_code == 409


def test_reindex_with_ingestion_down_answers_502(api_main, client, monkeypatch):
    monkeypatch.setattr(api_main, "auth_admins", {"apa"})
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    _fake_ingestion(api_main, monkeypatch, down)
    login(client)
    response = client.post("/ingest/reindex", json={}, headers={"X-CSRF-Token": session_csrf(client)})
    assert response.status_code == 502


def test_schedule_is_proxied_with_the_ingest_token(api_main, client, monkeypatch):
    monkeypatch.setattr(api_main, "auth_admins", {"apa"})
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["authorization"] = request.url.path, request.headers.get("authorization")
        return httpx.Response(200, json={"schedule": {"enabled": True}, "running": False})

    _fake_ingestion(api_main, monkeypatch, handler)
    assert client.get("/ingest/schedule").status_code == 401
    login(client)
    assert client.get("/ingest/schedule").json()["schedule"]["enabled"] is True
    assert seen == {"path": "/ingest/schedule", "authorization": f"Bearer {INGEST_TOKEN}"}


def test_auth_can_be_switched_off_explicitly(api_main, client, monkeypatch):
    monkeypatch.setattr(api_main.settings, "teacher_auth_disabled", True)
    assert client.get("/sources").status_code == 200
    assert client.post("/chat", json={"question": "Mi a fotoszintézis?"}).status_code == 200


def test_users_config_parsing(tmp_path):
    hashed = PasswordHasher().hash("titok-jelszo")
    users_file = tmp_path / "users"
    users_file.write_text(f"# családi fiókok\nanya:{hashed}\n", encoding="utf-8")
    settings = Settings(TEACHER_AUTH_USERS=f"apa:{hashed}", TEACHER_AUTH_USERS_FILE=str(users_file))
    assert settings.auth_users() == {"apa": hashed, "anya": hashed}

    with pytest.raises(ValueError):
        Settings(TEACHER_AUTH_USERS="apa:plaintext-password", TEACHER_AUTH_USERS_FILE="").auth_users()
    with pytest.raises(ValueError):
        Settings(TEACHER_AUTH_USERS=f"bad name:{hashed}", TEACHER_AUTH_USERS_FILE="").auth_users()
