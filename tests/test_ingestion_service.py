"""Run coordination in the ingestion service, with the heavy parsing/embedding/vector layers stubbed."""

import importlib
import os
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
    def chunk(segments, chunk_size, chunk_overlap):
        return [
            types.SimpleNamespace(text="t", locator="p1", page=1, chapter=None, ocr=False, ocr_confidence=None)
        ]

    documents.chunk_segments = chunk
    documents.parse_document = lambda path, file_hash=None: types.SimpleNamespace(
        segments=[], title=None, author=None, language="en", ocr=None
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
    ingestion_app = str(Path(__file__).resolve().parents[1] / "services" / "ingestion" / "app")
    monkeypatch.syspath_prepend(ingestion_app)  # the API service has a `main` too
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
    (svc.library / "watched" / "keep.txt").write_text("k")
    svc.run_indexing(False, True)
    (svc.library / "watched" / "a.txt").unlink()
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["watched/keep.txt"]
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
    def fail(path, file_hash=None):
        raise ValueError("boom")

    monkeypatch.setattr(svc, "parse_document", fail)
    svc.run_indexing(False, True)
    assert svc.last_run["failed"] == 1
    assert "boom" in svc.last_run["errors"][0]
    assert svc.last_run["status"] == "completed"


def test_drm_protected_ebook_is_skipped_logged_and_recorded(svc, monkeypatch, caplog):
    from teacher_common.ebooks import DrmProtectedError

    (svc.library / "watched" / "vedett.azw").write_bytes(b"x")
    svc.settings.ingest_scan_extensions = ".txt,.azw"

    def parse(path, file_hash=None):
        if path.suffix == ".azw":
            raise DrmProtectedError("vedett.azw is DRM-protected (Mobipocket/Kindle encryption); skipped.")
        return types.SimpleNamespace(segments=[], title=None, author=None, language="en", ocr=None)

    monkeypatch.setattr(svc, "parse_document", parse)
    with caplog.at_level("WARNING", logger="teacher.ingestion"):
        svc.run_indexing(False, True)
    assert svc.last_run["failed"] == 1
    assert svc.last_run["indexed"] == 1  # the plain text file is unaffected
    assert any("DRM-protected" in r.getMessage() and "vedett.azw" in r.getMessage() for r in caplog.records)
    assert "DRM-protected" in svc.last_run["errors"][0]


def test_kindle_books_that_failed_before_support_are_retried(svc):
    from teacher_common.db import session_scope

    book = svc.library / "watched" / "regi.mobi"
    book.write_bytes(b"x")
    svc.settings.ingest_scan_extensions = ".txt,.mobi"
    stat = book.stat()
    with session_scope() as session:
        session.add(svc.DocumentRecord(
            source_path=str(book), source_hash="unknown", file_type="mobi", size_bytes=stat.st_size,
            modified_timestamp=int(stat.st_mtime), title="regi", status="failed", chunk_count=0, deleted=False,
            error_message="mobi is not enabled in the MVP parser. Convert to EPUB/PDF/TXT",
        ))
    assert str(book) in svc.load_reprocess_paths()
    svc.settings.ocr_enabled = False
    assert str(book) in svc.load_reprocess_paths()
    svc.run_indexing(False, True)
    assert svc.last_run["indexed"] == 2  # a.txt and the formerly failed book (parsing is stubbed)


def test_runs_never_overlap_and_manual_trigger_shares_the_lock(svc, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = svc.index_one_document

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(svc, "index_one_document", slow)
    client = TestClient(svc.app, headers={"Authorization": "Bearer test-ingest-token"})

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
    client = TestClient(svc.app, headers={"Authorization": "Bearer test-ingest-token"})
    assert client.post("/ingest/reindex", json={}).json()["status"] == "started"
    for _ in range(100):
        if not svc.index_lock.locked():
            break
        threading.Event().wait(0.05)
    assert svc.last_run["trigger"] == "manual"
    assert svc.last_run["status"] == "completed"


def test_schedule_endpoint_reports_state(svc):
    svc.ingest_config.schedule = ScheduleConfig(enabled=True, cron="0 3 * * *")
    client = TestClient(svc.app, headers={"Authorization": "Bearer test-ingest-token"})
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


def test_full_reindex_without_watched_folders_keeps_the_index(svc):
    svc.run_indexing(False, True)
    svc.ingest_config.watch = WatchConfig()
    svc.run_indexing(True, True)
    assert live_paths(svc) == ["watched/a.txt"]
    assert svc.last_run["status"] == "failed"
    assert "untouched" in svc.last_run["message"]


def test_full_reindex_with_missing_folder_keeps_the_index(svc):
    svc.run_indexing(False, True)
    svc.ingest_config.watch = WatchConfig(folders=[WatchFolder(path="watched"), WatchFolder(path="gone")])
    svc.run_indexing(True, True)
    assert live_paths(svc) == ["watched/a.txt"]
    assert svc.last_run["status"] == "failed"


def test_full_reindex_rebuilds_when_scope_is_scannable(svc):
    svc.run_indexing(False, True)
    svc.run_indexing(True, True)
    assert live_paths(svc) == ["watched/a.txt"]
    assert svc.last_run["indexed"] == 1


def test_emptied_mount_point_does_not_wipe_the_index(svc):
    svc.ingest_config.watch = WatchConfig(folders=[WatchFolder(path=".")])
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["other/b.txt", "watched/a.txt"]
    # share unmounted: the mount point directory is still there but empty
    (svc.library / "watched" / "a.txt").unlink()
    (svc.library / "other" / "b.txt").unlink()
    (svc.library / "watched").rmdir()
    (svc.library / "other").rmdir()
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["other/b.txt", "watched/a.txt"]
    assert svc.last_run["deleted"] == 0
    assert any("unavailable" in e for e in svc.last_run["errors"])


def test_errors_survive_a_restart(svc):
    svc.run_indexing(False, True)
    (svc.library / "watched").rename(svc.library / "moved")
    svc.run_indexing(False, True)
    assert svc.last_run["errors"]
    svc.last_run.clear()  # new process: only the database is left
    body = TestClient(svc.app, headers={"Authorization": "Bearer test-ingest-token"}).get("/ingest/schedule").json()
    assert body["last_run"]["errors"]
    assert any("not found" in e for e in body["last_run"]["errors"])
    assert "\n" not in body["last_run"]["message"]


def test_full_reindex_with_emptied_mount_point_keeps_the_index(svc):
    svc.ingest_config.watch = WatchConfig(folders=[WatchFolder(path=".")])
    svc.run_indexing(False, True)
    for name in ("watched/a.txt", "other/b.txt"):
        (svc.library / name).unlink()
    (svc.library / "watched").rmdir()
    (svc.library / "other").rmdir()
    svc.run_indexing(True, True)
    assert live_paths(svc) == ["other/b.txt", "watched/a.txt"]
    assert svc.last_run["status"] == "failed"
    assert "unavailable" in svc.last_run["message"]


def test_folder_cleared_on_purpose_has_its_documents_removed(svc):
    (svc.library / "watched" / "sub").mkdir()
    (svc.library / "watched" / "notes.jpg").write_text("not indexed")
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["watched/a.txt"]
    (svc.library / "watched" / "a.txt").unlink()  # folder still has other content
    svc.run_indexing(False, True)
    assert live_paths(svc) == []
    assert svc.last_run["deleted"] == 1


def test_full_reindex_with_unreadable_nested_directory_keeps_the_index(svc):
    nested = svc.library / "watched" / "locked"
    nested.mkdir()
    (nested / "c.txt").write_text("c")
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["watched/a.txt", "watched/locked/c.txt"]
    nested.chmod(0)
    try:
        if os.access(nested, os.R_OK):
            pytest.skip("permissions are not enforced (running as root)")
        svc.run_indexing(True, True)
    finally:
        nested.chmod(0o755)
    assert live_paths(svc) == ["watched/a.txt", "watched/locked/c.txt"]
    assert svc.last_run["status"] == "failed"
    assert "could not be read" in svc.last_run["message"]


def test_completely_emptied_folder_is_kept_until_unwatched(svc):
    svc.run_indexing(False, True)
    (svc.library / "watched" / "a.txt").unlink()  # last entry gone: indistinguishable from an unmounted share
    svc.run_indexing(False, True)
    assert live_paths(svc) == ["watched/a.txt"]
    svc.ingest_config.watch = WatchConfig(folders=[WatchFolder(path="other")])  # unwatching removes it
    svc.run_indexing(False, True)
    assert "watched/a.txt" not in live_paths(svc)


def test_run_left_running_by_a_stopped_process_is_closed_at_startup(svc):
    from teacher_common.db import session_scope

    with session_scope() as session:
        session.add(svc.models.IngestRun(status="running", message="Indexing started (scheduled)"))
    assert svc.close_interrupted_runs() == 1
    body = TestClient(svc.app, headers={"Authorization": "Bearer test-ingest-token"}).get("/ingest/status").json()
    assert body["status"] == "interrupted"
    assert body["finished_at"] is not None
    assert svc.close_interrupted_runs() == 0


def test_failure_that_cannot_be_recorded_does_not_abort_the_run(svc, monkeypatch):
    (svc.library / "watched" / "z.txt").write_text("z")

    def fail(path, file_hash=None):
        if Path(path).name == "a.txt":
            raise ValueError("boom")
        return types.SimpleNamespace(segments=[], title=None, author=None, language="en", ocr=None)

    def vanished(path, message):
        raise FileNotFoundError(path)

    monkeypatch.setattr(svc, "parse_document", fail)
    monkeypatch.setattr(svc, "store_error_record", vanished)
    svc.run_indexing(False, True)
    assert svc.last_run["status"] == "completed"
    assert (svc.last_run["failed"], svc.last_run["indexed"]) == (1, 1)
