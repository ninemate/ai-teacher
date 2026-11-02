"""The flow of the end-to-end trial (scripts/e2e_trial.py) in one process, for `make test`, without any service.

Real here: API and ingestion routes with login and CSRF, the ingest token between them, the parsers on the committed
Hungarian fixtures, the citation and study code, SQLite, and Qdrant's filter logic (qdrant-client's local mode).
Fake here: the chat model (an httpx transport speaking Ollama's API), the embedder (hashed words), and, when Tesseract
with Hungarian is not installed, the OCR engine. Proxy, containers, network rule, restarts and the real models are only
proven by `make test-e2e`.
"""

import hashlib
import importlib.util
import json
import math
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from teacher_common import ocr
from teacher_common.config import get_settings
from teacher_common.scheduling import IngestFileConfig
from teacher_common.watch import WatchConfig, WatchFolder

QdrantClient = pytest.importorskip("qdrant_client").QdrantClient

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "services" / "common" / "teacher_common"
FIXTURES = Path(__file__).parent / "e2e" / "fixtures"
PASSWORD = "e2e-jelszo-12345"
ANNA, BELA, CILI, ADMIN = "e2e-anna", "e2e-bela", "e2e-cili", "e2e-admin"
MILL = "vizimalmok.pdf"
SCAN = "szkennelt-csillagaszat.pdf"
FISH = "halak-konyve.epub"
RECIPE = "titkos-receptek.docx"
DIMENSION = 512
# What the fake OCR engine "reads" from the scanned fixture when the real one is not installed.
SCANNED_TEXT = {
    0: "A Hold fázisai. A Hold nem saját fényével világít, hanem a Nap fényét veri vissza, újhold és telihold követi egymást.",
    1: "A gellérthegyi távcső tükrének átmérője százhúsz centiméter. A műszert Ürögdi Győző csillagász tervezte.",
}


def _embed(text: str) -> list[float]:
    vector = [0.0] * DIMENSION
    for word in re.findall(r"\w+", text.lower()):
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSION] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _load_real(name: str):
    """conftest.py stubs these modules for the unit tests; this flow needs the real ones."""
    spec = importlib.util.spec_from_file_location(f"e2e_real_{name}", COMMON / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tesseract_has_hungarian() -> bool:
    try:
        import pytesseract

        return "hun" in pytesseract.get_languages()
    except Exception:  # noqa: BLE001 - any failure means the engine cannot be used here
        return False


def _ollama_answer(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/generate"
    payload = json.loads(request.content)
    prompt = payload["prompt"]
    if payload.get("format") == "json":
        if '"questions"' in prompt:
            item = {"question": "Hány lapátja van a keréknek?", "options": ["12", "14"], "correct_index": 1,
                    "explanation": "A forrás szerint.", "sources": [1]}
            return httpx.Response(200, json={"response": json.dumps({"questions": [item]})})
        card = {"front": "Ki vezette a malmot?", "back": "Őri Ödön", "sources": [1]}
        return httpx.Response(200, json={"response": json.dumps({"cards": [card]})})
    return httpx.Response(200, json={"response": "A válasz a forrásból való [1]."})


@pytest.fixture()
def stack(api_main, ingestion_main, tmp_path, monkeypatch):
    settings = get_settings()
    # The flow uses a hashed-word fake embedder, whose scores are not calibrated against the E5 default.
    # The dedicated retrieval-floor tests cover that policy; keep this test focused on the rest of the flow.
    monkeypatch.setattr(settings, "retrieval_min_score", 0.0)
    library = tmp_path / "library"
    (library / "anna").mkdir(parents=True)
    (library / "bela").mkdir()
    for name in (MILL, SCAN, FISH):
        shutil.copy(FIXTURES / name, library / "anna" / name)
    shutil.copy(FIXTURES / RECIPE, library / "bela" / RECIPE)

    store = _load_real("qdrant_store")
    client = QdrantClient(":memory:")
    monkeypatch.setattr(store, "get_client", lambda: client)
    monkeypatch.setattr(store, "embedding_dimension", lambda: DIMENSION)
    store.ensure_collection()
    documents = _load_real("documents")
    monkeypatch.setitem(sys.modules, "teacher_common.qdrant_store", store)

    monkeypatch.setattr(settings, "library_path", str(library))
    monkeypatch.setattr(settings, "ocr_cache_dir", str(tmp_path / "ocr"))
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(ingestion_main, "ingest_config", IngestFileConfig(watch=WatchConfig(folders=[
        WatchFolder(path="anna", workspace=f"user:{ANNA}"),
        WatchFolder(path="bela", workspace=f"user:{BELA}"),
    ])))
    for name in ("parse_document", "chunk_segments", "hash_file"):
        monkeypatch.setattr(ingestion_main, name, getattr(documents, name))
    for name in ("build_points", "delete_points_for_source", "delete_points_for_workspace", "upsert_points"):
        monkeypatch.setattr(ingestion_main, name, getattr(store, name))
    monkeypatch.setattr(ingestion_main, "embed_passages", lambda texts: [_embed(text) for text in texts])
    monkeypatch.setattr(api_main, "embed_query", _embed)
    monkeypatch.setattr(api_main, "search", store.search)
    if not _tesseract_has_hungarian():
        monkeypatch.setattr(ocr.PdfOcr, "_recognise", lambda self, page, remaining: (SCANNED_TEXT[page.number], 91.0))

    # The API's one HTTP client: to the fake model, or to the real ingestion routes with the real token check.
    ingestion_client = TestClient(ingestion_main.app)
    real_client = httpx.Client

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ollama":
            return _ollama_answer(request)
        assert request.url.host == "ingestion"
        answer = ingestion_client.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(answer.status_code, content=answer.content, headers=answer.headers)

    monkeypatch.setattr(api_main, "httpx", SimpleNamespace(
        Client=lambda timeout: real_client(transport=httpx.MockTransport(route), timeout=timeout),
        HTTPError=httpx.HTTPError,
    ))

    hashed = PasswordHasher().hash(PASSWORD)
    for username in (ANNA, BELA, CILI, ADMIN):
        monkeypatch.setitem(api_main.auth_users, username, hashed)
    monkeypatch.setattr(api_main, "auth_admins", {ADMIN})
    api_main._ensure_account_workspaces()  # what the API's startup does
    api_main.login_limiter.reset()
    return SimpleNamespace(api=api_main, ingestion=ingestion_main, library=library, store=store, client=client)


def _login(api_main, username: str) -> TestClient:
    browser = TestClient(api_main.app, follow_redirects=False)
    token = re.search(r'name="csrf_token" value="([^"]+)"', browser.get("/login").text).group(1)
    answer = browser.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token})
    assert answer.status_code == 303
    browser.headers["X-CSRF-Token"] = browser.get("/session").json()["csrf_token"]
    return browser


def _run_index(admin: TestClient) -> dict:
    assert admin.post("/ingest/reindex", json={}).status_code == 200
    deadline = time.monotonic() + 120
    while True:
        state = admin.get("/ingest/schedule").json()
        if not state["running"] and state["last_run"]["status"] != "running":
            return state["last_run"]
        assert time.monotonic() < deadline, "indexing did not finish"
        time.sleep(0.05)


def _names(browser: TestClient) -> set[str]:
    return {Path(row["source_path"]).name for row in browser.get("/sources").json()}


def _chat(browser: TestClient, question: str) -> dict:
    answer = browser.post("/chat", json={"question": question})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_whole_flow(stack):
    api = stack.api

    # Login is required.
    anonymous = TestClient(api.app, follow_redirects=False)
    for method, path in (("GET", "/sources"), ("POST", "/chat"), ("GET", "/study/quizzes"), ("POST", "/ingest/reindex")):
        assert anonymous.request(method, path, json={"question": "Mi ez a kérdés?"}).status_code == 401
    # The ingestion service refuses a caller without the token, also directly.
    assert TestClient(stack.ingestion.app).post("/ingest/reindex", json={}).status_code == 401

    anna, bela, cili, admin = (_login(api, name) for name in (ANNA, BELA, CILI, ADMIN))
    assert anna.post("/ingest/reindex", json={}).status_code == 403

    # A triggered run indexes the watched folders, the scanned PDF through OCR.
    first = _run_index(admin)
    assert (first["status"], first["scanned"], first["indexed"], first["failed"]) == ("completed", 4, 4, 0), first
    assert _names(anna) == {MILL, SCAN, FISH}
    assert _names(bela) == {RECIPE}
    documents_before = sorted((row["id"], row["chunk_count"]) for row in anna.get("/sources").json())
    points_before = stack.client.count("teacher_documents", exact=True).count

    # A second run duplicates nothing.
    second = _run_index(admin)
    assert (second["indexed"], second["skipped"]) == (0, 4), second
    assert sorted((row["id"], row["chunk_count"]) for row in anna.get("/sources").json()) == documents_before
    assert stack.client.count("teacher_documents", exact=True).count == points_before

    # The answer cites the right file and page or chapter.
    top = _chat(anna, "Hány lapátja van a kőröshegyi malom kerekének?")["citations"][0]
    assert (Path(top["source_path"]).name, top["page"], top["ocr"]) == (MILL, 2, False), top
    assert "tizennégy" in top["preview"]
    assert top["label"] == f"{MILL}, 2. oldal"
    scanned = _chat(anna, "Mekkora a gellérthegyi távcső tükrének átmérője?")["citations"][0]
    assert (Path(scanned["source_path"]).name, scanned["page"], scanned["ocr"]) == (SCAN, 2, True), scanned
    assert "távcső" in scanned["preview"]
    chapter = _chat(anna, "Hol ívik tavasszal a tiszai tokhal?")["citations"][0]
    assert Path(chapter["source_path"]).name == FISH and "fejezet2" in chapter["chapter"], chapter

    # No source: says so, with fixed text rather than the model's.
    nothing = _chat(cili, "Hány lapátja van a kőröshegyi malom kerekének?")
    assert nothing["citations"] == [] and nothing["grounding"]["status"] == "no_source"
    assert nothing["grounding"]["notice"] and "nem találtam" in nothing["answer"]

    # User B reaches nothing of user A's, by search, id or path.
    assert not {Path(c["source_path"]).name for c in _chat(bela, "A kőröshegyi malom kereke?")["citations"]} & {MILL, SCAN, FISH}
    recipe = _chat(anna, "Mi a bodzás őszibarack lekvár titkos receptje?")
    assert "BODZALEKVAR-7731" not in json.dumps(recipe, ensure_ascii=False)
    mill = next(row for row in anna.get("/sources").json() if row["source_path"].endswith(MILL))
    assert bela.delete(f"/sources/{mill['id']}").status_code == 404
    assert bela.post("/study/quizzes", json={"source_paths": [mill["source_path"]]}).status_code == 404

    # A quiz and flashcards get generated and stored; the other account cannot read them.
    scope = {"topic": "A kőröshegyi malom története", "count": 1}
    quiz = anna.post("/study/quizzes", json=scope)
    deck = anna.post("/study/decks", json=scope)
    assert quiz.status_code == 201 and deck.status_code == 201, (quiz.text, deck.text)
    quiz, deck = quiz.json(), deck.json()
    assert anna.get(f"/study/quizzes/{quiz['id']}").json() == quiz
    answers = {str(question["id"]): 1 for question in quiz["questions"]}
    attempt = anna.post(f"/study/quizzes/{quiz['id']}/attempts", json={"answers": answers})
    assert attempt.status_code == 201 and attempt.json()["score"] == 1
    assert attempt.json()["questions"][0]["sources"][0]["page"] == 2
    assert deck["cards"][0]["sources"][0]["page"] == 2
    assert any(row["id"] == deck["id"] for row in anna.get("/study/decks").json())
    assert bela.get(f"/study/quizzes/{quiz['id']}").status_code == 404
    assert bela.get(f"/study/decks/{deck['id']}").status_code == 404

    # Deleting a document removes it from the list, the index, the answers and what was built on it.
    points = stack.client.count("teacher_documents", exact=True).count
    assert anna.delete(f"/sources/{mill['id']}").status_code == 204
    assert MILL not in _names(anna)
    assert stack.client.count("teacher_documents", exact=True).count == points - mill["chunk_count"]
    assert all(not c["source_path"].endswith(MILL) for c in _chat(anna, "A kőröshegyi malom kereke?")["citations"])
    assert anna.get(f"/study/quizzes/{quiz['id']}").status_code == 404

    # A file removed from the watched folder leaves on the next run.
    (stack.library / "anna" / SCAN).unlink()
    third = _run_index(admin)
    assert third["deleted"] == 1, third
    assert SCAN not in _names(anna)
    assert all(not c["source_path"].endswith(SCAN) for c in _chat(anna, "A gellérthegyi távcső tükre?")["citations"])


def test_a_failing_model_is_reported_and_not_a_server_crash(stack, monkeypatch):
    """A 500 here also closes the proxy's connection, so the next user's request got a 502 too."""
    anna = _login(stack.api, ANNA)
    monkeypatch.setattr(
        stack.api,
        "retrieve_sources",
        lambda *args, **kwargs: [{"source_path": "/l/a.pdf", "text": "x", "score": 0.9}],
    )

    def timed_out(prompt, json_mode=False):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(stack.api, "call_ollama", timed_out)
    answer = anna.post("/chat", json={"question": "Mi a teszt kérdés?"})
    assert answer.status_code == 502
    assert "nyelvi modell" in answer.json()["detail"]
