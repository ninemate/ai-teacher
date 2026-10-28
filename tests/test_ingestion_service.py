"""Run coordination in the ingestion service, with the heavy parsing/embedding/vector layers stubbed."""

import importlib
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from teacher_common.scheduling import ScheduleConfig
from teacher_common.watch import WatchConfig, WatchFolder


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    hashed: list[str] = []
    documents = types.ModuleType("teacher_common.documents")
    documents.chunk_segments = lambda segments, chunk_size, chunk_overlap: [types.SimpleNamespace(text="t", locator="p1")]
    documents.parse_document = lambda path: types.SimpleNamespace(
        segments=[], title=None, author=None, language="en"
    )

    def hash_file(path):
        hashed.append(Path(path).name)
        return "h" + Path(path).stem

    documents.hash_file = hash_file
    embeddings = types.ModuleType("teacher_common.embeddings")
    embeddings.embed_passages = lambda texts: [[0.0] for _ in texts]
    embeddings.warmup_embeddings = lambda: None
    store = types.ModuleType("teacher_common.qdrant_store")
    store.deleted_sources = []
    store.build_points = lambda **kw: []
    store.delete_points_for_source = lambda source: store.deleted_sources.append(source)
    store.ensure_collection = lambda: None
    store.recreate_collection = lambda: None
    store.upsert_points = lambda points: None
    for name, module in {
        "teacher_common.documents": documents,
        "teacher_common.embeddings": embeddings,
        "teacher_common.qdrant_store": store,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    main.init_db()
    from teacher_common import models
    from teacher_common.db import Base, engine

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    library = tmp_path / "lib"
    (library / "watched").mkdir(parents=True)
    (library / "other").mkdir()
    (library / "watched" / "a.txt").write_text("a")
    (library / "other" / "b.txt").write_text("b")
    main.settings.library_path = str(library)
    main.settings.ingest_scan_extensions = ".txt"
    main.ingest_config.watch = WatchConfig(folders=[WatchFolder(path="watched")])
    main.last_run.clear()
    main.hashed = hashed
    main.store = store
    main.models = models
    main.library = library
    yield main
    sys.modules.pop("main", None)


def live_paths(svc):
    from sqlalchemy import select
    from teacher_common.db import session_scope

    with session_scope() as session:
        rows = session.execute(
            select(svc.DocumentRecord.source_path).where(svc.DocumentRecord.deleted.is_(False))
        ).scalars()
        return sorted(Path(p).relative_to(svc.library).as_posix() for p in rows)


def test_only_watched_folders_are_indexed(svc):
    assert svc.run_indexing(False, True) is True
    assert live_paths(svc) == ["watched/a.txt"]
    assert svc.last_run["status"] == "completed"
    assert svc.last_run["scanned"] == 1 and svc.last_run["indexed"] == 1
    assert svc.last_run["trigger"] == "scheduled"


def test_unchanged_files_are_not_rehashed(svc):
    svc.run_indexing(False, True)
    assert svc.hashed == ["a.txt"]
    svc.run_indexing(False, True)
    assert svc.hashed == ["a.txt"]
    assert svc.last_run["skipped"] == 1 and svc.last_run["indexed"] == 0


def test_changed_file_is_reindexed(svc):
    svc.run_indexing(False, True)
    path = svc.library / "watched" / "a.txt"
    path.write_text("longer content")
    svc.run_indexing(False, True)
    assert svc.hashed == ["a.txt", "a.txt"]


def test_vanished_documents_are_removed(svc):
    svc.run_indexing(False, True)
    (svc.library / "watched" / "a.txt").unlink()
    svc.run_indexing(False, True)
    assert live_paths(svc) == []
    assert svc.last_run["deleted"] == 1


def test_remove_missing_false_keeps_documents(svc):
    svc.run_indexing(False, True)
    (svc.library / "watched" / "a.txt").unlink()
    svc.run_indexing(False, False)
    assert live_paths(svc) == ["watched/a.txt"]


def test_unreachable_watched_folder_does_not_delete_its_documents(svc):
    svc.run_indexing(False, True)
    (svc.library / "watched" / "a.txt").unlink()
    (svc.library / "watched").rmdir()  # e.g. share went away
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["watched/a.txt"]
    assert any("not found" in e for e in svc.last_run["errors"])


def test_no_watched_folders_indexes_and_removes_nothing(svc):
    svc.run_indexing(False, True)
    svc.ingest_config.watch = WatchConfig()
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["watched/a.txt"]
    assert svc.last_run["scanned"] == 0


def test_unwatching_a_folder_removes_its_documents(svc):
    svc.run_indexing(False, True)
    svc.ingest_config.watch = WatchConfig(folders=[WatchFolder(path="other")])
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["other/b.txt"]


def test_failed_file_is_counted_and_reported(svc, monkeypatch):
    def fail(path):
        raise ValueError("boom")

    monkeypatch.setattr(svc, "parse_document", fail)
    svc.run_indexing(False, True)
    assert svc.last_run["failed"] == 1
    assert "boom" in svc.last_run["errors"][0]
    assert svc.last_run["status"] == "completed"


def test_runs_never_overlap_and_manual_trigger_shares_the_lock(svc, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = svc.index_one_document

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(svc, "index_one_document", slow)
    client = TestClient(svc.app)

    first = threading.Thread(target=svc.run_indexing, args=(False, True, "scheduled"))
    first.start()
    try:
        assert entered.wait(timeout=10)
        assert svc.run_indexing(False, True) is False  # scheduler tick while running: declined
        assert client.post("/ingest/reindex", json={}).status_code == 409  # manual: same lock
        assert client.get("/ingest/schedule").json()["running"] is True
    finally:
        release.set()
        first.join(timeout=10)
    assert not svc.index_lock.locked()


def test_manual_trigger_runs_and_releases_lock(svc):
    client = TestClient(svc.app)
    assert client.post("/ingest/reindex", json={}).json()["status"] == "started"
    for _ in range(100):
        if not svc.index_lock.locked():
            break
        threading.Event().wait(0.05)
    assert svc.last_run["trigger"] == "manual"
    assert svc.last_run["status"] == "completed"


def test_schedule_endpoint_reports_state(svc):
    svc.ingest_config.schedule = ScheduleConfig(enabled=True, cron="0 3 * * *")
    client = TestClient(svc.app)
    body = client.get("/ingest/schedule").json()
    assert body["schedule"]["enabled"] is True
    assert body["schedule"]["cron"] == "0 3 * * *"
    assert body["watch"]["folders"][0]["path"] == "watched"
    assert body["last_run"] is None
    svc.run_indexing(False, True)
    body = client.get("/ingest/schedule").json()
    assert body["last_run"]["indexed"] == 1
    assert body["last_run"]["folders"] == {"watched": 1}
    assert body["running"] is False
