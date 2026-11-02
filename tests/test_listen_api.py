"""Listening routes: resume across devices, on-demand audio, access control and cache cleanup."""

import re
import time
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from teacher_common.audio_cache import AudioCache
from teacher_common.db import session_scope
from teacher_common.listen_models import ListenBookmark, ListenItem
from teacher_common.models import DocumentRecord
from teacher_common.workspaces import ensure_personal_workspace

PASSWORDS = {"apa": "helyes-jelszo-123", "anya": "masik-jelszo-456"}


def login(api_main, username):
    api_main.login_limiter.reset()
    client = TestClient(api_main.app, follow_redirects=False)
    page = client.get("/login")
    form_token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login", data={"username": username, "password": PASSWORDS[username], "csrf_token": form_token}
    )
    assert response.status_code == 303
    return client, {"X-CSRF-Token": client.get("/session").json()["csrf_token"]}


def chunks_for(path):
    def chunk(position, page, text, **extra):
        return {"position": position, "text": text, "locator": f"oldal {page}", "page": page,
                "source_path": path, "ocr": False, "ocr_confidence": None, **extra}
    return [
        chunk(1, 1, "Első oldal első mondata. Első oldal második mondata."),
        chunk(2, 2, "Elmosódott oldal szövege.", ocr=True, ocr_confidence=40.0, locator="oldal 2 (OCR)"),
        chunk(3, 3, "Harmadik oldal. Itt a vége."),
    ]


@pytest.fixture
def world(api_main, monkeypatch, tmp_path):
    monkeypatch.setattr(api_main, "auth_admins", set())
    monkeypatch.setattr(api_main.listen_cache, "root", tmp_path / "audio")
    suffix = uuid.uuid4().hex
    path = f"/library/hallgat-{suffix}.pdf"
    with session_scope() as session:
        workspace = ensure_personal_workspace(session, "apa")
        document = DocumentRecord(
            workspace_id=workspace.id, source_path=path, source_hash=f"hash-{suffix}", file_type="pdf",
            size_bytes=1, modified_timestamp=1, title="Hallgatni való", status="indexed", chunk_count=3,
        )
        session.add(document)
        session.flush()
        ids = {"doc": document.id, "workspace": workspace.id}
    synthesized = []

    def fake_voice(method, path_, timeout, quiet=False, **kwargs):
        text = kwargs["json"]["text"]
        synthesized.append(text)
        return type("R", (), {"content": b"MP3:" + text.encode()})()

    monkeypatch.setattr(api_main, "scroll_document", lambda source_path: chunks_for(source_path))
    monkeypatch.setattr(api_main, "_voice_request", fake_voice)
    world = {**ids, "path": path, "synthesized": synthesized, "cache": api_main.listen_cache, "api_main": api_main}
    world["apa"] = login(api_main, "apa")
    world["anya"] = login(api_main, "anya")
    return world


def test_sections_text_and_ocr_marking(world):
    client, headers = world["apa"]
    doc = world["doc"]
    assert doc not in [item["document"]["id"] for item in client.get("/listen/library").json()]
    assert client.post("/listen/library", json={"document_id": doc}, headers=headers).status_code == 201
    overview = client.get(f"/listen/{doc}").json()
    assert [s["title"] for s in overview["sections"]] == ["1. oldal", "2. oldal", "3. oldal"]
    assert [s["low_confidence"] for s in overview["sections"]] == [False, True, False]
    assert overview["ocr_low_confidence_limit"] == 70.0
    section = client.get(f"/listen/{doc}/sections/1").json()
    assert section["segments"][0] == {
        "index": 0, "text": "Elmosódott oldal szövege.", "ocr": True, "ocr_confidence": 40.0, "low_confidence": True,
    }
    assert client.get(f"/listen/{doc}/sections/9").status_code == 404


def test_audio_is_made_per_requested_segment_once_and_cached_on_disk(world):
    client, headers = world["apa"]
    doc = world["doc"]
    first = client.get(f"/listen/{doc}/audio/0/0?voice=hu_HU-imre-medium")
    assert first.status_code == 200 and first.headers["content-type"] == "audio/mpeg"
    assert first.content.startswith("MP3:Első oldal első mondata.".encode())
    assert client.get(f"/listen/{doc}/audio/0/0?voice=hu_HU-imre-medium").content == first.content
    assert world["synthesized"] == ["Első oldal első mondata. Első oldal második mondata."]
    # a different voice is a different file; nothing else was synthesized ahead of time
    client.get(f"/listen/{doc}/audio/0/0?voice=hu_HU-anna-medium")
    assert len(world["synthesized"]) == 2
    files = list(world["cache"].root.rglob("*.mp3"))
    assert len(files) == 2 and all(f.parent.parent == world["cache"].root for f in files)
    assert client.get(f"/listen/{doc}/audio/0/0?voice=../../x").status_code == 422
    assert client.get(f"/listen/{doc}/audio/5/0").status_code == 404


def test_voice_service_off_gives_a_clear_error_and_caches_nothing(world, monkeypatch):
    def unavailable(*args, **kwargs):
        raise HTTPException(status_code=503, detail="A hangszolgáltatás nem érhető el.")

    monkeypatch.setattr(world["api_main"], "_voice_request", unavailable)
    client, _ = world["apa"]
    response = client.get(f"/listen/{world['doc']}/audio/0/0")
    assert response.status_code == 503
    assert not list(world["cache"].root.rglob("*.mp3"))
    assert client.get(f"/listen/{world['doc']}").status_code == 200  # the text still reads without the voice


def test_position_and_bookmarks_resume_on_another_device(world):
    client, headers = world["apa"]
    doc = world["doc"]
    saved = client.put(
        f"/listen/{doc}/position", json={"section": 2, "segment": 0, "speed": 1.5, "voice": "hu_HU-anna-medium"},
        headers=headers,
    )
    assert saved.status_code == 200
    bookmark = client.post(f"/listen/{doc}/bookmarks", json={"section": 0, "segment": 0}, headers=headers)
    assert bookmark.status_code == 201 and bookmark.json()["label"].startswith("1. oldal: Első oldal első")

    phone, _ = login(world["api_main"], "apa")  # a fresh session of the same user
    overview = phone.get(f"/listen/{doc}").json()
    assert overview["position"]["section"] == 2 and overview["position"]["segment"] == 0
    assert overview["position"]["speed"] == 1.5 and overview["position"]["voice"] == "hu_HU-anna-medium"
    assert [b["id"] for b in overview["bookmarks"]] == [bookmark.json()["id"]]
    assert doc in [item["document"]["id"] for item in phone.get("/listen/library").json()]

    assert client.put(f"/listen/{doc}/position", json={"section": 9, "segment": 0}, headers=headers).status_code == 404
    assert client.put(f"/listen/{doc}/position", json={"section": 0, "segment": 0, "speed": 9}, headers=headers).status_code == 422
    assert client.delete(f"/listen/{doc}/bookmarks/{bookmark.json()['id']}", headers=headers).status_code == 204
    assert client.get(f"/listen/{doc}").json()["bookmarks"] == []


def test_position_beyond_a_shorter_reindexed_document_is_clamped(world):
    client, headers = world["apa"]
    doc = world["doc"]
    client.put(f"/listen/{doc}/position", json={"section": 2, "segment": 0}, headers=headers)
    with session_scope() as session:
        item = session.execute(select(ListenItem).where(ListenItem.document_id == doc)).scalar_one()
        item.section, item.segment = 40, 40
    position = client.get(f"/listen/{doc}").json()["position"]
    assert (position["section"], position["segment"]) == (2, 0)


def test_nobody_without_access_reaches_text_audio_position_or_bookmarks(world):
    apa, apa_headers = world["apa"]
    anya, anya_headers = world["anya"]
    doc = world["doc"]
    apa.put(f"/listen/{doc}/position", json={"section": 1, "segment": 0}, headers=apa_headers)
    bookmark = apa.post(f"/listen/{doc}/bookmarks", json={"section": 0, "segment": 0}, headers=apa_headers).json()
    apa.get(f"/listen/{doc}/audio/0/0")  # now cached on disk
    calls = len(world["synthesized"])

    assert anya.get(f"/listen/{doc}").status_code == 404
    assert anya.get(f"/listen/{doc}/sections/0").status_code == 404
    assert anya.get(f"/listen/{doc}/audio/0/0").status_code == 404
    assert anya.put(f"/listen/{doc}/position", json={"section": 0, "segment": 0}, headers=anya_headers).status_code == 404
    assert anya.post(f"/listen/{doc}/bookmarks", json={"section": 0, "segment": 0}, headers=anya_headers).status_code == 404
    assert anya.post("/listen/library", json={"document_id": doc}, headers=anya_headers).status_code == 404
    assert anya.get("/listen/library").json() == []
    assert anya.delete(f"/listen/{doc}/bookmarks/{bookmark['id']}", headers=anya_headers).status_code == 404
    assert len(world["synthesized"]) == calls

    # a bookmark id of another user is not deletable even for a document both may read
    share = apa.post(f"/workspaces/{world['workspace']}/shares", json={"username": "anya", "permission": "read"}, headers=apa_headers)
    assert share.status_code == 201
    assert anya.get(f"/listen/{doc}/audio/0/0").status_code == 200
    assert len(world["synthesized"]) == calls  # served from the shared cache of the one document
    assert anya.get(f"/listen/{doc}").json()["position"] is None  # apa's position is apa's
    assert anya.get(f"/listen/{doc}").json()["bookmarks"] == []
    assert anya.delete(f"/listen/{doc}/bookmarks/{bookmark['id']}", headers=anya_headers).status_code == 404
    anya.post("/listen/library", json={"document_id": doc}, headers=anya_headers)
    assert [i["document"]["id"] for i in anya.get("/listen/library").json()] == [doc]

    # losing the share: the list entry stays stored but nothing is reachable, cached audio included
    assert apa.delete(f"/workspaces/{world['workspace']}/shares/anya", headers=apa_headers).status_code == 204
    assert anya.get("/listen/library").json() == []
    assert anya.get(f"/listen/{doc}/audio/0/0").status_code == 404
    assert anya.get(f"/listen/{doc}/sections/0").status_code == 404
    assert list(world["cache"].root.rglob("*.mp3"))  # the file is there; the route is what guards it


def test_deleting_a_document_removes_audio_lists_and_bookmarks(world):
    client, headers = world["apa"]
    doc = world["doc"]
    client.put(f"/listen/{doc}/position", json={"section": 0, "segment": 0}, headers=headers)
    client.post(f"/listen/{doc}/bookmarks", json={"section": 0, "segment": 0}, headers=headers)
    client.get(f"/listen/{doc}/audio/0/0")
    assert list(world["cache"].root.rglob("*.mp3"))

    assert client.delete(f"/sources/{doc}", headers=headers).status_code == 204
    assert not list(world["cache"].root.rglob("*.mp3"))
    assert client.get(f"/listen/{doc}/audio/0/0").status_code == 404
    assert client.get(f"/listen/{doc}").status_code == 404
    with session_scope() as session:
        assert session.execute(select(ListenItem).where(ListenItem.document_id == doc)).first() is None
        assert session.execute(select(ListenBookmark).where(ListenBookmark.document_id == doc)).first() is None


def test_documents_the_indexer_removed_are_unreachable_and_swept(world, ingestion_main):
    client, headers = world["apa"]
    doc = world["doc"]
    client.put(f"/listen/{doc}/position", json={"section": 0, "segment": 0}, headers=headers)
    client.get(f"/listen/{doc}/audio/0/0")
    removed = ingestion_main.remove_deleted_documents(set(), workspace_ids={world["workspace"]}, confirmed=True)
    assert removed >= 1
    assert client.get(f"/listen/{doc}/audio/0/0").status_code == 404
    assert doc not in [item["document"]["id"] for item in client.get("/listen/library").json()]
    with session_scope() as session:
        assert session.execute(select(ListenItem).where(ListenItem.document_id == doc)).first() is None
    from listen_routes import sweep_orphans

    assert list(world["cache"].root.rglob("*.mp3"))  # the indexer cannot reach the audio folder
    sweep_orphans(world["cache"])
    assert not list(world["cache"].root.rglob("*.mp3"))


def test_reindexed_documents_leave_no_audio_of_the_old_text(world):
    doc = world["doc"]
    client, _ = world["apa"]
    client.get(f"/listen/{doc}/audio/0/0")
    with session_scope() as session:
        session.get(DocumentRecord, doc).source_hash = "new-" + uuid.uuid4().hex
    from listen_routes import sweep_orphans

    sweep_orphans(world["cache"])
    assert not list(world["cache"].root.rglob("*.mp3"))
    assert isinstance(world["cache"], AudioCache)


def test_background_sweep_removes_audio_without_a_request(world, ingestion_main):
    from listen_routes import start_periodic_sweep

    doc = world["doc"]
    client, _ = world["apa"]
    client.get(f"/listen/{doc}/audio/0/0")
    ingestion_main.remove_deleted_documents(set(), workspace_ids={world["workspace"]}, confirmed=True)
    assert list(world["cache"].root.rglob("*.mp3"))
    thread, stop = start_periodic_sweep(world["cache"], interval=0.05)
    try:
        deadline = time.monotonic() + 5
        while list(world["cache"].root.rglob("*.mp3")) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not list(world["cache"].root.rglob("*.mp3"))
