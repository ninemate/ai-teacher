import pytest
from conftest import INGEST_TOKEN
from fastapi.testclient import TestClient


@pytest.fixture
def client(ingestion_main, monkeypatch):
    monkeypatch.setattr(ingestion_main, "run_indexing", lambda **kwargs: None)
    return TestClient(ingestion_main.app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_is_open(client):
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "headers", [{}, bearer("wrong"), {"Authorization": INGEST_TOKEN}, {b"Authorization": "Bearer ő".encode()}]
)
def test_ingest_endpoints_refuse_without_token(client, headers):
    assert client.get("/ingest/status", headers=headers).status_code == 401
    assert client.post("/ingest/reindex", json={}, headers=headers).status_code == 401


def test_ingest_endpoints_accept_token(client):
    assert client.get("/ingest/status", headers=bearer(INGEST_TOKEN)).status_code == 200
    response = client.post("/ingest/reindex", json={}, headers=bearer(INGEST_TOKEN))
    assert response.status_code == 200
    assert response.json()["status"] == "started"


def test_ingest_endpoints_closed_when_token_unset(ingestion_main, client, monkeypatch):
    monkeypatch.setattr(ingestion_main.settings, "teacher_ingest_token", "")
    assert client.get("/ingest/status", headers=bearer("")).status_code == 503
