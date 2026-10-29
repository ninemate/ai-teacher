import json

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

HITS = [
    {"chunk_id": "c1", "source_path": "/library/a.pdf", "title": "A", "locator": "3. oldal", "text": "alfa"},
    {"chunk_id": "c2", "source_path": "/library/a.pdf", "title": "A", "locator": "4. oldal", "text": "béta"},
]
QUIZ = json.dumps(
    {"questions": [{"question": "Mi az alfa?", "options": ["x", "y", "z"], "correct_index": 1, "explanation": "e", "sources": [1, 2]}]}
)
CARDS = json.dumps({"cards": [{"front": "Alfa?", "back": "Első", "sources": [2]}]})


class Fakes:
    def __init__(self):
        self.hits = HITS
        self.replies = []
        self.prompts = []
        self.retrievals = []

    def retrieve(self, query, top_k, source_paths):
        self.retrievals.append((query, top_k, source_paths))
        return self.hits

    def llm(self, prompt, json_mode):
        assert json_mode is True
        self.prompts.append(prompt)
        return self.replies.pop(0)


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def client(fakes):
    from study_routes import build_router
    from teacher_common.db import init_db

    init_db()
    app = FastAPI()

    def deny():
        raise HTTPException(401)

    app.include_router(build_router(fakes.retrieve, fakes.llm))
    protected = FastAPI()
    protected.include_router(build_router(fakes.retrieve, fakes.llm), dependencies=[Depends(deny)])
    c = TestClient(app)
    c.protected = TestClient(protected)
    return c


def test_router_composes_with_an_auth_dependency(client):
    assert client.protected.get("/study/quizzes").status_code == 401
    assert client.protected.get("/study/decks").status_code == 401


def test_generate_fetch_attempt_delete_quiz(client, fakes):
    fakes.replies = [QUIZ]
    resp = client.post("/study/quizzes", json={"topic": "alfa", "level": "intermediate", "count": 3})
    assert resp.status_code == 201
    quiz = resp.json()
    assert quiz["level"] == "intermediate" and quiz["title"] == "alfa"
    question = quiz["questions"][0]
    assert set(question) == {"id", "position", "question", "options"}  # no answer key before submitting
    assert fakes.retrievals == [("alfa", 10, None)]
    assert "Pontosan 3 kérdést" in fakes.prompts[0] and "[1] A | 3. oldal | alfa" in fakes.prompts[0]

    assert any(q["id"] == quiz["id"] for q in client.get("/study/quizzes").json())
    assert client.get(f"/study/quizzes/{quiz['id']}").json()["questions"][0]["id"] == question["id"]

    attempt = client.post(f"/study/quizzes/{quiz['id']}/attempts", json={"answers": {str(question["id"]): 1}})
    assert attempt.status_code == 201 and attempt.json()["score"] == 1
    released = attempt.json()["questions"][0]
    assert released["correct_index"] == 1 and released["explanation"] == "e"
    assert [s["locator"] for s in released["sources"]] == ["3. oldal", "4. oldal"]
    assert len(client.get(f"/study/quizzes/{quiz['id']}/attempts").json()) == 1
    bad = client.post(f"/study/quizzes/{quiz['id']}/attempts", json={"answers": {"999999": 0}})
    assert bad.status_code == 422

    assert client.delete(f"/study/quizzes/{quiz['id']}").status_code == 204
    assert client.get(f"/study/quizzes/{quiz['id']}").status_code == 404
    assert client.delete(f"/study/quizzes/{quiz['id']}").status_code == 404


def test_generate_deck_scoped_to_documents_and_review(client, fakes):
    fakes.replies = ["nem json", CARDS]  # first reply malformed -> retried
    resp = client.post("/study/decks", json={"topic": "alfa", "source_paths": ["/library/a.pdf"]})
    assert resp.status_code == 201
    deck = resp.json()
    assert len(fakes.prompts) == 2
    assert fakes.retrievals[0][2] == ["/library/a.pdf"]
    assert deck["scope_sources"] == ["/library/a.pdf"] and deck["due_count"] == 1
    card = deck["cards"][0]
    assert card["sources"][0]["chunk_id"] == "c2"

    due = client.get(f"/study/decks/{deck['id']}/due").json()
    assert [c["id"] for c in due] == [card["id"]]
    reviewed = client.post(f"/study/cards/{card['id']}/review", json={"grade": "good"})
    assert reviewed.json()["interval_days"] == 1
    assert client.get(f"/study/decks/{deck['id']}/due").json() == []
    assert client.post(f"/study/cards/{card['id']}/review", json={"grade": "nagyon"}).status_code == 422
    assert client.post("/study/cards/987654/review", json={"grade": "good"}).status_code == 404
    assert client.delete(f"/study/decks/{deck['id']}").status_code == 204


def test_generation_failures_are_reported_and_nothing_is_stored(client, fakes):
    before = len(client.get("/study/quizzes").json())
    fakes.replies = ["{}", "{}"]
    resp = client.post("/study/quizzes", json={"topic": "alfa"})
    assert resp.status_code == 502
    fakes.hits = []
    assert client.post("/study/quizzes", json={"topic": "alfa"}).status_code == 422
    assert len(client.get("/study/quizzes").json()) == before


def test_request_validation(client):
    assert client.post("/study/quizzes", json={}).status_code == 422
    assert client.post("/study/quizzes", json={"topic": "alfa", "count": 0}).status_code == 422
    assert client.post("/study/decks", json={"topic": "alfa", "level": "expert"}).status_code == 422
    assert client.post("/study/quizzes", json={"source_paths": ["/nincs/ilyen.pdf"]}).status_code == 404
