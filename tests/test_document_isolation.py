import re
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select
from teacher_common import study_store
from teacher_common.db import session_scope
from teacher_common.models import ChunkRecord, DocumentRecord, OcrRecord
from teacher_common.workspaces import ensure_personal_workspace


def _login(api_main, username):
    api_main.login_limiter.reset()
    client = TestClient(api_main.app, follow_redirects=False)
    page = client.get("/login")
    form_token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={"username": username, "password": {"apa": "helyes-jelszo-123", "anya": "masik-jelszo-456"}[username],
              "csrf_token": form_token},
    )
    assert response.status_code == 303
    return client, client.get("/session").json()["csrf_token"]


def _seed_private_content():
    suffix = uuid.uuid4().hex
    path_a = f"/library/private-a-{suffix}.pdf"
    path_b = f"/library/private-b-{suffix}.pdf"
    with session_scope() as session:
        workspace_a = ensure_personal_workspace(session, "apa")
        workspace_b = ensure_personal_workspace(session, "anya")
        doc_a = DocumentRecord(
            workspace_id=workspace_a.id, source_path=path_a, source_hash=f"ha-{suffix}", file_type="pdf",
            size_bytes=1, modified_timestamp=1, title="A titkos dokumentuma", status="indexed", chunk_count=1,
        )
        doc_b = DocumentRecord(
            workspace_id=workspace_b.id, source_path=path_b, source_hash=f"hb-{suffix}", file_type="pdf",
            size_bytes=1, modified_timestamp=1, title="B saját dokumentuma", status="indexed", chunk_count=1,
        )
        session.add_all([doc_a, doc_b])
        session.flush()
        ocr_a = OcrRecord(
            workspace_id=workspace_a.id, source_path=path_a, source_hash=f"ha-{suffix}",
            language="hun", dpi=300, pages_total=1, pages_ocr=1, pages_pending=0, status="complete",
        )
        session.add(ocr_a)
        session.flush()
        chunk_a = ChunkRecord(
            workspace_id=workspace_a.id, document_id=doc_a.id, chunk_id=f"ca-{suffix}", position=1,
            locator="1. oldal", preview="A titkos szövege",
        )
        chunk_b = ChunkRecord(
            workspace_id=workspace_b.id, document_id=doc_b.id, chunk_id=f"cb-{suffix}", position=1,
            locator="2. oldal", preview="B saját szövege",
        )
        session.add_all([chunk_a, chunk_b])
        question = {
            "question": "Kié ez a dokumentum?", "options": ["A", "B"], "correct_index": 0,
            "explanation": "A privát anyaga.", "sources": [{"chunk_id": chunk_a.chunk_id, "source_path": path_a}],
        }
        card = {"front": "A titkos kérdés?", "back": "A titkos válasza", "sources": [{"chunk_id": chunk_a.chunk_id, "source_path": path_a}]}
        quiz = study_store.create_quiz_set(
            session, title="A privát kvíze", topic="titkos", level="beginner", scope_sources=[path_a],
            model="test", questions=[question], workspace_id=workspace_a.id,
        )
        deck = study_store.create_deck(
            session, title="A privát kártyái", topic="titkos", level="beginner", scope_sources=[path_a],
            model="test", cards=[card], workspace_id=workspace_a.id,
        )
        return {
            "workspace_a": workspace_a.id,
            "path_a": path_a,
            "path_b": path_b,
            "doc_a_id": doc_a.id,
            "ocr_a_id": ocr_a.id,
            "chunk_a_id": chunk_a.id,
            "chunk_b_id": chunk_b.id,
            "quiz_id": quiz["id"],
            "question_id": quiz["questions"][0]["id"],
            "deck_id": deck["id"],
            "card_id": deck["cards"][0]["id"],
        }


def test_user_data_and_vector_retrieval_are_workspace_scoped(api_main, monkeypatch):
    monkeypatch.setattr(api_main, "auth_admins", set())
    data = _seed_private_content()
    vector_scopes = []
    deleted_vectors = []
    vectors = {
        "user:apa": [{"chunk_id": "A", "source_path": data["path_a"], "title": "A titkos dokumentuma",
                      "locator": "1. oldal", "text": "A titkos szövege", "preview": "A titkos szövege"}],
        "user:anya": [{"chunk_id": "B", "source_path": data["path_b"], "title": "B saját dokumentuma",
                       "locator": "2. oldal", "text": "B saját szövege", "preview": "B saját szövege"}],
    }

    def scoped_search(query_vector, limit, workspace_ids, source_paths=None):
        vector_scopes.append(set(workspace_ids))
        return [SimpleNamespace(payload=item, score=0.9) for workspace_id in workspace_ids
                for item in vectors.get(workspace_id, [])
                if not source_paths or item["source_path"] in source_paths]

    monkeypatch.setattr(api_main, "search", scoped_search)
    monkeypatch.setattr(api_main, "embed_query", lambda text: [0.0])
    monkeypatch.setattr(api_main, "call_ollama", lambda prompt: "Rövid válasz")
    import teacher_common.qdrant_store as qdrant_store
    monkeypatch.setattr(qdrant_store, "delete_points_for_source", lambda path: deleted_vectors.append(path))

    client_a, csrf_a = _login(api_main, "apa")
    client_b, csrf_b = _login(api_main, "anya")
    headers_a, headers_b = {"X-CSRF-Token": csrf_a}, {"X-CSRF-Token": csrf_b}

    sources_a = client_a.get("/sources").json()
    sources_b = client_b.get("/sources").json()
    assert {item["source_path"] for item in sources_a} >= {data["path_a"]}
    assert data["path_a"] not in {item["source_path"] for item in sources_b}
    assert data["path_b"] in {item["source_path"] for item in sources_b}

    answer_b = client_b.post("/chat", json={"question": "Mit tudok erről?"}, headers=headers_b).json()
    assert [source["source_path"] for source in answer_b["sources"]] == [data["path_b"]]
    assert "A titkos szövege" not in str(answer_b)
    assert vector_scopes[-1] == {"user:anya"}
    answer_a = client_a.post("/chat", json={"question": "Mit tudok erről?"}, headers=headers_a).json()
    assert [source["source_path"] for source in answer_a["sources"]] == [data["path_a"]]
    assert vector_scopes[-1] == {"user:apa"}

    assert data["quiz_id"] not in {quiz["id"] for quiz in client_b.get("/study/quizzes").json()}
    assert client_b.get(f"/study/quizzes/{data['quiz_id']}").status_code == 404
    assert client_b.get(f"/study/quizzes/{data['quiz_id']}/attempts").status_code == 404
    assert client_b.post(
        f"/study/quizzes/{data['quiz_id']}/attempts", json={"answers": {str(data['question_id']): 1}},
        headers=headers_b,
    ).status_code == 404
    assert client_b.delete(f"/study/quizzes/{data['quiz_id']}", headers=headers_b).status_code == 404

    assert data["deck_id"] not in {deck["id"] for deck in client_b.get("/study/decks").json()}
    assert client_b.get(f"/study/decks/{data['deck_id']}").status_code == 404
    assert client_b.get(f"/study/decks/{data['deck_id']}/due").status_code == 404
    assert client_b.post(f"/study/cards/{data['card_id']}/review", json={"grade": "good"}, headers=headers_b).status_code == 404
    assert client_b.delete(f"/study/decks/{data['deck_id']}", headers=headers_b).status_code == 404
    assert client_b.post(
        "/study/decks", json={"source_paths": [data["path_a"]]}, headers=headers_b,
    ).status_code == 404

    assert client_b.delete(f"/sources/{data['doc_a_id']}", headers=headers_b).status_code == 404
    with session_scope() as session:
        assert session.get(DocumentRecord, data["doc_a_id"]) is not None
        assert session.get(ChunkRecord, data["chunk_a_id"]) is not None
        assert session.get(OcrRecord, data["ocr_a_id"]) is not None
    assert deleted_vectors == []

    share = client_a.post(
        f"/workspaces/{data['workspace_a']}/shares", json={"username": "anya", "permission": "read"},
        headers=headers_a,
    )
    assert share.status_code == 201
    assert data["path_a"] in {item["source_path"] for item in client_b.get("/sources").json()}
    assert data["quiz_id"] in {item["id"] for item in client_b.get("/study/quizzes").json()}
    assert client_b.get(f"/study/quizzes/{data['quiz_id']}").status_code == 200
    assert client_b.post(
        f"/study/quizzes/{data['quiz_id']}/attempts", json={"answers": {}}, headers=headers_b,
    ).status_code == 404
    assert client_b.delete(f"/study/quizzes/{data['quiz_id']}", headers=headers_b).status_code == 404
    assert data["deck_id"] in {item["id"] for item in client_b.get("/study/decks").json()}
    assert client_b.get(f"/study/decks/{data['deck_id']}").status_code == 200
    assert client_b.post(
        f"/study/cards/{data['card_id']}/review", json={"grade": "good"}, headers=headers_b,
    ).status_code == 404
    assert client_b.delete(f"/study/decks/{data['deck_id']}", headers=headers_b).status_code == 404
    assert client_b.delete(f"/sources/{data['doc_a_id']}", headers=headers_b).status_code == 404
    with session_scope() as session:
        anya_workspace = ensure_personal_workspace(session, "anya")
        other_quiz = study_store.create_quiz_set(
            session, title="B saját kvíze", topic="közös forrás", level="beginner", scope_sources=[data["path_a"]],
            model="test", questions=[{"question": "Saját kérdés?", "options": ["igen"], "correct_index": 0,
                                      "explanation": "Saját magyarázat.",
                                      "sources": [{"source_path": data["path_a"]}]}],
            workspace_id=anya_workspace.id,
        )
        other_deck = study_store.create_deck(
            session, title="B saját kártyái", topic="közös forrás", level="beginner", scope_sources=[data["path_a"]],
            model="test", cards=[{"front": "Saját kérdés?", "back": "Saját válasz.",
                                  "sources": [{"source_path": data["path_a"]}]}],
            workspace_id=anya_workspace.id,
        )
    assert client_a.delete(
        f"/workspaces/{data['workspace_a']}/shares/anya", headers=headers_a,
    ).status_code == 204
    assert data["path_a"] not in {item["source_path"] for item in client_b.get("/sources").json()}
    assert data["quiz_id"] not in {item["id"] for item in client_b.get("/study/quizzes").json()}

    assert client_a.delete(f"/sources/{data['doc_a_id']}", headers=headers_a).status_code == 204
    assert deleted_vectors == [data["path_a"]]
    with session_scope() as session:
        assert session.get(DocumentRecord, data["doc_a_id"]) is None
        assert session.get(ChunkRecord, data["chunk_a_id"]) is None
        assert session.get(OcrRecord, data["ocr_a_id"]) is None
        assert session.execute(select(study_store.QuizSet).where(study_store.QuizSet.id == data["quiz_id"])).first() is None
        assert session.execute(select(study_store.FlashcardDeck).where(
            study_store.FlashcardDeck.id == data["deck_id"]
        )).first() is None
        assert session.get(study_store.QuizSet, other_quiz["id"]) is not None
        assert session.get(study_store.FlashcardDeck, other_deck["id"]) is not None


def test_regular_users_cannot_read_admin_ingestion_status_but_admins_can(api_main, monkeypatch):
    monkeypatch.setattr(api_main, "auth_admins", set())
    data = _seed_private_content()
    regular, _ = _login(api_main, "anya")
    assert regular.get("/ingest/status").status_code == 403
    assert data["path_a"] not in {item["source_path"] for item in regular.get("/sources").json()}

    monkeypatch.setattr(api_main, "auth_admins", {"apa"})
    admin, _ = _login(api_main, "apa")
    assert admin.get("/session").json()["role"] == "admin"
    assert admin.get("/ingest/status").status_code == 200
    assert {data["path_a"], data["path_b"]} <= {
        item["source_path"] for item in admin.get("/sources").json()
    }
