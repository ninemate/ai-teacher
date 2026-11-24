"""Exercise the tutor through the authenticated application with a deterministic local model."""

import json

import pytest
from fastapi.testclient import TestClient
from test_api_auth import login, session_csrf
from test_document_isolation import _seed_private_content

CARD = {"front": "a boarding pass", "back": "beszállókártya"}
CORRECTION = {
    "type": "grammar",
    "explanation": "Az egyes szám harmadik személyben -s kell.",
    "example": "She works at the airport.",
}


@pytest.fixture
def learners(api_main):
    api_main.login_limiter.reset()
    clients = []
    for name in ("apa", "anya"):
        client = TestClient(api_main.app, follow_redirects=False)
        assert login(client, name).status_code == 303
        client.headers["X-CSRF-Token"] = session_csrf(client)
        clients.append(client)
    yield clients
    for client in clients:
        client.close()


@pytest.fixture
def model(api_main, monkeypatch):
    prompts = []
    reply = {"reply": "Where do you work?", "corrections": [], "vocabulary": [CARD]}

    def generate(prompt, json_mode=False):
        assert json_mode
        prompts.append(prompt)
        return json.dumps(reply, ensure_ascii=False)

    monkeypatch.setattr(api_main, "call_ollama", generate)
    return prompts, reply


def ok(response):
    assert response.is_success, response.text
    return response.json()


def test_private_material_practice_corrections_and_deck_export(api_main, learners, model, monkeypatch):
    owner, outsider = learners
    prompts, reply = model
    material = _seed_private_content()
    calls = []

    def retrieve(query, top_k, source_paths, workspace_ids):
        calls.append((source_paths, workspace_ids))
        if material["workspace_a"] in workspace_ids and material["path_a"] in source_paths:
            return [{"text": "The flight departs at eight.", "source_path": material["path_a"], "score": 1.0}]
        return []

    monkeypatch.setattr(api_main, "retrieve_sources", retrieve)
    other_profile = ok(outsider.get("/tutor/profile"))
    other_progress = ok(outsider.get("/tutor/progress"))
    profile = {"language": "en", "level": "B2", "goal": "Airport interviews", "auto_level": False}
    ok(owner.put("/tutor/profile", json=profile))
    # Force a manual level change even if the default happened to be B2.
    ok(owner.put("/tutor/profile", json={**profile, "level": "C1"}))
    config = {
        "mode": "roleplay",
        "scenario": "At the airport",
        "source_path": material["path_a"],
        "deck_id": material["deck_id"],
    }
    session = ok(owner.post("/tutor/sessions", json=config))
    path = f"/tutor/sessions/{session['id']}"
    assert calls and "user:anya" not in calls[-1][1]
    for marker in ("Airport interviews", "At the airport", "The flight departs", "A titkos kérdés"):
        assert marker in prompts[-1]

    reply["corrections"] = [CORRECTION]
    data = ok(owner.post(path + "/turns", json={"text": "She work at the airport."}))
    last = data["turns"][-1]
    assert last["corrections"][0]["example"] == CORRECTION["example"]
    assert last["learner"] == "She work at the airport."
    assert ok(owner.get(path))["turns"] == data["turns"]
    card = {**CARD, "deck_id": material["deck_id"]}
    ok(owner.post(path + "/cards", json=card))
    deck = ok(owner.get(f"/study/decks/{material['deck_id']}"))
    assert any(item["front"] == CARD["front"] for item in deck["cards"])
    progress = ok(owner.get("/tutor/progress"))
    assert progress["errors"]["grammar"] >= 1
    assert any(level["level"] == "C1" for level in progress["levels"])

    assert ok(outsider.get("/tutor/profile")) == other_profile
    assert ok(outsider.get("/tutor/progress")) == other_progress
    assert outsider.get(path).status_code == 404
    assert outsider.post(path + "/turns", json={"text": "guess"}).status_code == 404
    assert outsider.post(path + "/cards", json=card).status_code == 404
    assert outsider.get(f"/study/decks/{material['deck_id']}").status_code == 404
    assert material["deck_id"] not in [d["id"] for d in ok(outsider.get("/tutor/decks"))]
    for material_key in ("source_path", "deck_id"):
        assert outsider.post(
            "/tutor/sessions", json={"mode": "conversation", material_key: config[material_key]}
        ).status_code in (404, 422)


def test_automatic_level_and_failed_model_turn(learners, model):
    _, client = learners
    _, reply = model
    ok(client.put("/tutor/profile", json={"level": "A2", "language": "hu", "goal": "Utazás", "auto_level": True}))
    created = ok(client.post("/tutor/sessions", json={"mode": "writing", "scenario": "Utazás"}))
    path = f"/tutor/sessions/{created['id']}"
    for _ in range(5):
        ok(client.post(path + "/turns", json={"text": "Szeretnék egy vonatjegyet Budapestre."}))
    assert ok(client.get("/tutor/profile"))["level"] == "B1"
    last_state = ok(client.get(path))
    reply["reply"] = ""
    assert client.post(path + "/turns", json={"text": "És visszafelé?"}).status_code == 502
    assert ok(client.get(path)) == last_state


def test_listening_exercise_keeps_context(learners, model):
    client, _ = learners
    prompts, reply = model
    reply["reply"] = "The train leaves at seven. When does it leave?"
    created = ok(client.post("/tutor/sessions", json={"mode": "listening", "scenario": "Train announcements"}))
    reply["reply"] = "Correct. Where is the train going?"
    ok(client.post(f"/tutor/sessions/{created['id']}/turns", json={"text": "At seven."}))
    assert "The train leaves at seven" in prompts[-1]
    assert "At seven." in prompts[-1]
