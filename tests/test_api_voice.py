"""API side of voice: /voice/* is proxied to the voice service (mocked here) behind login and CSRF."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from teacher_common.db import session_scope
from teacher_common.models import AuthSession
from test_api_auth import login, session_csrf

HEALTH = {
    "status": "ok",
    "stt": {"enabled": True, "max_seconds": 120.0},
    "tts": {
        "enabled": True,
        "voices": ["hu_HU-imre-medium", "hu_HU-anna-medium"],
        "default_voice": "hu_HU-imre-medium",
        "max_chars": 6000,
    },
}


@pytest.fixture
def client(api_main):
    api_main.login_limiter.reset()
    with session_scope() as db:
        db.execute(delete(AuthSession))
    client = TestClient(api_main.app, follow_redirects=False)
    login(client)
    client.headers["X-CSRF-Token"] = session_csrf(client)
    return client


@pytest.fixture
def voice_service(api_main, monkeypatch):
    """Route the API's httpx calls to a handler; `state.handler` can be replaced per test."""
    seen: list[httpx.Request] = []

    def default(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json=HEALTH)
        if request.url.path == "/voice/transcribe":
            return httpx.Response(200, json={"text": "Mi a fotoszintézis?", "language": "hu", "duration": 2.1})
        if request.url.path == "/voice/speak":
            return httpx.Response(200, content=b"ID3-mp3", headers={"content-type": "audio/mpeg"})
        return httpx.Response(404)

    state = SimpleNamespace(handler=default, seen=seen)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return state.handler(request)

    fake_httpx = SimpleNamespace(
        Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
        HTTPError=httpx.HTTPError,
    )
    monkeypatch.setattr(api_main, "httpx", fake_httpx)
    return state


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Name or service not known", request=request)


def test_voice_routes_need_login(api_main):
    anonymous = TestClient(api_main.app, follow_redirects=False)
    assert anonymous.get("/voice/status").status_code == 401
    assert anonymous.post("/voice/speak", json={"text": "Szia"}).status_code == 401
    assert anonymous.post("/voice/transcribe", files={"audio": ("a.webm", b"x")}).status_code == 401


def test_voice_posts_need_csrf(client, voice_service):
    del client.headers["X-CSRF-Token"]
    assert client.post("/voice/speak", json={"text": "Szia"}).status_code == 403
    assert client.post("/voice/transcribe", files={"audio": ("a.webm", b"x")}).status_code == 403
    assert voice_service.seen == []


def test_status_reports_engines_and_voices(client, voice_service):
    assert client.get("/voice/status").json() == {
        "available": True,
        "stt": True,
        "tts": True,
        "voices": ["hu_HU-imre-medium", "hu_HU-anna-medium"],
        "default_voice": "hu_HU-imre-medium",
        "max_seconds": 120.0,
        "max_chars": 6000,
    }


def test_status_without_voice_service_is_not_an_error(client, voice_service):
    voice_service.handler = unreachable
    response = client.get("/voice/status")
    assert response.status_code == 200
    assert response.json()["available"] is False


def test_transcribe_is_forwarded(client, voice_service):
    response = client.post("/voice/transcribe", files={"audio": ("../felvetel.webm", b"opus-bytes", "audio/webm")})
    assert response.status_code == 200
    assert response.json()["text"] == "Mi a fotoszintézis?"
    (request,) = voice_service.seen
    body = request.read()
    assert b"opus-bytes" in body
    assert b'filename="audio.webm"' in body, "only the extension of the client's file name is passed on"


def test_transcribe_limits(client, voice_service, api_main, monkeypatch):
    assert client.post("/voice/transcribe", files={"audio": ("a.webm", b"")}).status_code == 422
    monkeypatch.setattr(api_main.settings, "voice_max_upload_mb", 0.001)
    response = client.post("/voice/transcribe", files={"audio": ("a.webm", b"x" * 2000)})
    assert response.status_code == 413
    assert voice_service.seen == []


def test_voice_service_errors_keep_status_and_message(client, voice_service):
    voice_service.handler = lambda request: httpx.Response(422, json={"detail": "Nem sikerült beolvasni a hangfelvételt."})
    response = client.post("/voice/transcribe", files={"audio": ("a.webm", b"x")})
    assert response.status_code == 422
    assert response.json()["detail"] == "Nem sikerült beolvasni a hangfelvételt."

    voice_service.handler = lambda request: httpx.Response(500, text="Internal Server Error")
    response = client.post("/voice/speak", json={"text": "Szia"})
    assert response.status_code == 500
    assert response.json()["detail"] == "A hangszolgáltatás hibát jelzett."


def test_voice_service_down_is_503(client, voice_service):
    voice_service.handler = unreachable
    response = client.post("/voice/speak", json={"text": "Szia"})
    assert response.status_code == 503
    assert response.json()["detail"] == "A hangszolgáltatás nem érhető el."


def test_speak_returns_audio(client, voice_service):
    response = client.post("/voice/speak", json={"text": "A fotoszintézis...", "voice": "hu_HU-anna-medium"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"ID3-mp3"
    (request,) = voice_service.seen
    assert request.url.path == "/voice/speak"
    assert json.loads(request.read()) == {"text": "A fotoszintézis...", "voice": "hu_HU-anna-medium", "format": "mp3"}


def test_page_allows_blob_audio(client):
    csp = client.get("/").headers["content-security-policy"]
    assert "media-src 'self' blob:" in csp
