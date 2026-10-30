import logging
import os
import secrets
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from teacher_common.config import get_settings
from teacher_common.db import init_db, session_scope
from teacher_common.documents import (
    chunk_segments,
    hash_file,
    parse_document,
)
from teacher_common.ebooks import DrmProtectedError
from teacher_common.embeddings import embed_passages, warmup_embeddings
from teacher_common.models import ChunkRecord, DocumentRecord, IngestRun, OcrRecord
from teacher_common.ocr import record_ocr, resumable
from teacher_common.qdrant_store import (
    build_points,
    delete_points_for_source,
    ensure_collection,
    recreate_collection,
    upsert_points,
)
from teacher_common.scheduling import IndexScheduler, load_ingest_config
from teacher_common.watch import ScanReport, scan_watched_files

logger = logging.getLogger("teacher.ingestion")
MAX_REPORTED_ERRORS = 20

settings = get_settings()
app = FastAPI(title="teacher-agent-ingestion", version="0.1.0")
# One lock for scheduled and manual runs, so they can never overlap.
index_lock = threading.Lock()
current_state: dict[str, bool | str | None] = {"running": False, "current_file": None}
ingest_config = load_ingest_config(settings.ingest_config_path, settings.ingest_schedule_seconds)
scheduler = IndexScheduler(
    ingest_config.schedule,
    lambda: run_indexing(full_reindex=False, remove_missing=settings.ingest_remove_missing, trigger="scheduled"),
)
# Details of the most recent run in this process (the DB only keeps counts).
last_run: dict = {}


class ReindexRequest(BaseModel):
    full_reindex: bool = False
    remove_missing: bool = True


def require_ingest_token(authorization: str = Header(default="")) -> None:
    """Only callers holding TEACHER_INGEST_TOKEN (the API, ops scripts) may use /ingest endpoints."""
    expected = settings.teacher_ingest_token
    if not expected:
        raise HTTPException(status_code=503, detail="TEACHER_INGEST_TOKEN is not configured")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.strip().encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing ingest token", headers={"WWW-Authenticate": "Bearer"})


@app.on_event("startup")
def startup_event() -> None:
    if not settings.teacher_ingest_token:
        logging.getLogger("uvicorn").warning("TEACHER_INGEST_TOKEN is empty: /ingest endpoints refuse every call.")
    Path("/data/metadata").mkdir(parents=True, exist_ok=True)
    init_db()
    close_interrupted_runs()
    ensure_collection()
    warmup_embeddings()
    scheduler.start()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "running": current_state["running"],
        "current_file": current_state["current_file"],
    }


@app.get("/ingest/status", dependencies=[Depends(require_ingest_token)])
def ingest_status():
    with session_scope() as session:
        row = session.execute(select(IngestRun).order_by(IngestRun.id.desc())).scalars().first()
        if row is None:
            return {"status": "idle", "running": current_state["running"]}
        return {
            "status": row.status,
            "message": row.message,
            "scanned": row.scanned,
            "indexed": row.indexed,
            "skipped": row.skipped,
            "failed": row.failed,
            "deleted": row.deleted,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "running": current_state["running"],
            "current_file": current_state["current_file"],
        }


@app.post("/ingest/reindex", dependencies=[Depends(require_ingest_token)])
def reindex(payload: ReindexRequest):
    if not index_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Indexing already running")
    thread = threading.Thread(
        target=execute_run,
        kwargs={
            "full_reindex": payload.full_reindex,
            "remove_missing": payload.remove_missing,
            "trigger": "manual",
        },
        daemon=True,
    )
    thread.start()
    return {
        "status": "started",
        "full_reindex": payload.full_reindex,
        "remove_missing": payload.remove_missing,
    }


@app.get("/ingest/schedule", dependencies=[Depends(require_ingest_token)])
def ingest_schedule():
    """Schedule, watched folders and the outcome of the last run, for the UI and ops."""
    schedule = ingest_config.schedule
    last = dict(last_run) if last_run else None
    if last is None:
        # After a restart only the persisted counts are known.
        with session_scope() as session:
            row = session.execute(select(IngestRun).order_by(IngestRun.id.desc())).scalars().first()
            if row is not None:
                message, stored_errors = split_message(row.message)
                last = {
                    "status": row.status,
                    "message": message,
                    "errors": stored_errors,
                    "scanned": row.scanned,
                    "indexed": row.indexed,
                    "skipped": row.skipped,
                    "failed": row.failed,
                    "deleted": row.deleted,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                }
    return {
        "schedule": {
            "enabled": schedule.enabled,
            "interval_seconds": schedule.interval_seconds,
            "cron": schedule.cron,
            "timezone": schedule.timezone,
            "next_run_at": scheduler.next_run_at.isoformat() if scheduler.next_run_at else None,
            "last_tick_at": scheduler.last_tick_at.isoformat() if scheduler.last_tick_at else None,
            "last_tick_started_run": scheduler.last_tick_started_run,
        },
        "watch": ingest_config.watch.model_dump(),
        "running": current_state["running"],
        "current_file": current_state["current_file"],
        "last_run": last,
    }


def close_interrupted_runs() -> int:
    """Runs still marked running belong to a process that stopped mid-run (deploy, restart, crash).

    Only this service writes runs and it runs as one process, so at startup none of them can be live.
    Their finished documents are kept; the next run picks up the rest, and OCR resumes from its cache.
    """
    with session_scope() as session:
        rows = session.execute(select(IngestRun).where(IngestRun.status == "running")).scalars().all()
        for row in rows:
            row.status = "interrupted"
            errors = split_message(row.message)[1]
            row.message = with_errors("Interrupted: the ingestion service stopped during this run", errors)
            row.finished_at = datetime.now(UTC)
    if rows:
        logger.warning("Marked %d unfinished indexing run(s) as interrupted", len(rows))
    return len(rows)


def run_indexing(full_reindex: bool, remove_missing: bool, trigger: str = "scheduled") -> bool:
    """Run synchronously if no other run holds the lock; returns False when it declined."""
    if not index_lock.acquire(blocking=False):
        return False
    execute_run(full_reindex=full_reindex, remove_missing=remove_missing, trigger=trigger)
    return True


def execute_run(full_reindex: bool, remove_missing: bool, trigger: str) -> None:
    """Run indexing. The caller must hold ``index_lock``; it is released here."""
    current_state["running"] = True
    current_state["current_file"] = None
    run_id = None
    errors: list[str] = []
    started_at = datetime.now(UTC)
    last_run.clear()
    last_run.update({"trigger": trigger, "status": "running", "started_at": started_at.isoformat(), "errors": errors})

    try:
        with session_scope() as session:
            run = IngestRun(status="running", message=f"Indexing started ({trigger})")
            session.add(run)
            session.flush()
            run_id = run.id

        library_root = Path(settings.library_path)
        if not library_root.exists():
            raise RuntimeError(f"Library path not found: {library_root}")

        if full_reindex:
            # A full reindex wipes the index first, so make sure there is something to rebuild it from.
            problem = full_reindex_blocker(library_root)
            if problem:
                raise RuntimeError(f"Full reindex refused, existing index left untouched: {problem}")
            with session_scope() as session:
                session.query(ChunkRecord).delete()
                session.query(DocumentRecord).delete()
            recreate_collection()

        scanned = indexed = skipped = failed = deleted = 0
        seen_paths: set[str] = set()
        report = ScanReport()
        message = "Indexing completed"
        watch = ingest_config.watch

        if not watch.active_folders:
            # Never fall back to walking the whole share, and never treat "nothing watched" as "all deleted".
            message = "No watched folders configured; nothing to index"
            remove_missing = False
        known = load_known_documents()
        reprocess = load_reprocess_paths()

        for file_path in scan_watched_files(library_root, watch, settings.scan_extensions, report):
            scanned += 1
            current_state["current_file"] = str(file_path)
            seen_paths.add(str(file_path))
            try:
                outcome = index_one_document(file_path, full_reindex=full_reindex, known=known, reprocess=reprocess)
                if outcome == "indexed":
                    indexed += 1
                elif outcome == "skipped":
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 - one broken file must not abort the run
                failed += 1
                if isinstance(exc, DrmProtectedError):
                    logger.warning("Skipping DRM-protected ebook %s: %s", file_path, exc)
                add_error(errors, f"{file_path}: {exc}")
                try:
                    store_error_record(file_path, str(exc))
                except Exception as record_exc:  # noqa: BLE001 - e.g. the file vanished meanwhile
                    logger.warning("Could not record the failure of %s: %s", file_path, record_exc)

        for missing in report.missing_folders:
            add_error(errors, f"Watched folder not found or not a directory: {missing}")
        for unreadable in report.unreadable_dirs:
            add_error(errors, f"Could not read directory: {unreadable}")

        mark_vanished_folders(library_root, watch, known, report, errors)

        if remove_missing:
            deleted = remove_deleted_documents(seen_paths, report)

        update_run(
            run_id=run_id,
            status="completed",
            message=with_errors(message, errors),
            scanned=scanned,
            indexed=indexed,
            skipped=skipped,
            failed=failed,
            deleted=deleted,
        )
        last_run.update(
            status="completed", message=message, scanned=scanned, indexed=indexed, skipped=skipped,
            failed=failed, deleted=deleted, folders=report.scanned_per_folder,
            finished_at=datetime.now(UTC).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - ensure the run is marked failed instead of dying silently
        logger.exception("Indexing run failed")
        stored = with_errors(str(exc), errors)
        add_error(errors, str(exc))
        last_run.update(status="failed", message=str(exc), finished_at=datetime.now(UTC).isoformat())
        if run_id is not None:
            update_run(
                run_id=run_id,
                status="failed",
                message=stored,
            )
    finally:
        current_state["running"] = False
        current_state["current_file"] = None
        index_lock.release()


def with_errors(message: str, errors: list[str]) -> str:
    """Runs only persist a message, so the bounded error list is kept below it, one per line."""
    return "\n".join([message.splitlines()[0] if message else "", *[e.replace("\n", " ") for e in errors]]).rstrip()


def split_message(stored: str | None) -> tuple[str | None, list[str]]:
    if not stored:
        return stored, []
    head, *errors = stored.split("\n")
    return head, errors


def full_reindex_blocker(library_root: Path) -> str | None:
    """Why a full reindex could not rebuild anything, or None when the watched scope looks scannable."""
    watch = ingest_config.watch
    if not watch.active_folders:
        return "no watched folders configured"
    missing = [folder.path for folder in watch.active_folders if not (library_root / folder.path).is_dir()]
    if missing:
        return "watched folder(s) not found: " + ", ".join(missing)
    # Preview the scan (names only, nothing is read) and compare it with what is indexed now.
    report = ScanReport()
    for _ in scan_watched_files(library_root, watch, settings.scan_extensions, report):
        pass
    unreadable = [*report.missing_folders, *report.unreadable_dirs]
    if unreadable:
        return "watched path(s) could not be read: " + ", ".join(unreadable)
    vanished = vanished_folders(library_root, watch, load_known_documents(), report)
    if vanished:
        return "watched folder(s) look unavailable (completely empty but indexed before): " + ", ".join(vanished)
    return None


def vanished_folders(
    library_root: Path, watch, known: dict[str, tuple[int, int]], report: ScanReport
) -> list[str]:
    """Watched folders that look like an unmounted share rather than a clean-up.

    An unmounted share leaves an empty mount point behind: no entries at all. A folder that still holds
    something (subfolders, other file types, files excluded by the filters) but no matching files is a
    real change, and its documents are removed as usual.
    """
    found = []
    for folder in watch.active_folders:
        if report.scanned_per_folder.get(folder.path) != 0:
            continue
        base = library_root / folder.path
        prefix = str(base).rstrip("/") + "/"
        if not any(path.startswith(prefix) for path in known):
            continue
        try:
            has_entries = next(os.scandir(base), None) is not None
        except OSError:
            has_entries = False
        if not has_entries:
            found.append(folder.path)
    return found


def mark_vanished_folders(
    library_root: Path, watch, known: dict[str, tuple[int, int]], report: ScanReport, errors: list[str]
) -> None:
    for path in vanished_folders(library_root, watch, known, report):
        report.unreadable_dirs.append(str(library_root / path))
        add_error(
            errors,
            f"Watched folder {path!r} is completely empty but had indexed documents; "
            "assuming it is unavailable and keeping them (unwatch the folder to remove them)",
        )


def add_error(errors: list[str], message: str) -> None:
    if len(errors) < MAX_REPORTED_ERRORS:
        errors.append(message)


def load_known_documents() -> dict[str, tuple[int, int]]:
    """Size and mtime of every live document, in one query, so unchanged files cost no DB round trip."""
    with session_scope() as session:
        rows = session.execute(
            select(DocumentRecord.source_path, DocumentRecord.size_bytes, DocumentRecord.modified_timestamp).where(
                DocumentRecord.deleted.is_(False)
            )
        ).all()
    return {path: (size, mtime) for path, size, mtime in rows}


def load_reprocess_paths() -> set[str]:
    """Unchanged PDFs that still need work: failed ones (the OCR cache makes a retry cheap), ones OCR'd with
    another language or DPI, ones indexed while OCR was off, and ones whose OCR stopped on the time budget
    (each run OCRs the next pages, the cached ones are not repeated). Also Kindle books that failed before
    MOBI support existed.

    A failed PDF whose OCR stopped for good (stalled, page limit, failing page) is left alone: a retry would
    stop at the same place, and a stalled scan would spend OCR_TIMEOUT_SECONDS on every run."""
    with session_scope() as session:
        # Kindle books that failed before MOBI support existed ("not enabled in the MVP parser") keep their size/mtime,
        # so without this they would be skipped forever after the upgrade.
        legacy_ebooks = set(
            session.execute(
                select(DocumentRecord.source_path).where(
                    DocumentRecord.deleted.is_(False),
                    DocumentRecord.status == "failed",
                    DocumentRecord.file_type.in_(["mobi", "azw", "azw3"]),
                    DocumentRecord.error_message.like("%not enabled in the MVP parser%"),
                )
            ).scalars()
        )
        if not settings.ocr_enabled:
            return legacy_ebooks
        failed = session.execute(
            select(DocumentRecord.source_path).where(
                DocumentRecord.deleted.is_(False),
                DocumentRecord.status == "failed",
                DocumentRecord.file_type == "pdf",
            )
        ).scalars()
        paths = legacy_ebooks | set(failed)
        rows = session.execute(
            select(OcrRecord.source_path, OcrRecord.status, OcrRecord.message, OcrRecord.language, OcrRecord.dpi)
        ).all()
    wanted = (settings.ocr_language, settings.ocr_dpi)
    for path, status, message, language, dpi in rows:
        if status == "disabled" or (language, dpi) != wanted or resumable(status, message):
            paths.add(path)
        elif status == "partial":
            paths.discard(path)
    return paths


def index_one_document(
    file_path: Path,
    full_reindex: bool,
    known: dict[str, tuple[int, int]] | None = None,
    reprocess: set[str] | None = None,
) -> str:
    stat = file_path.stat()
    # size + mtime first: only changed or new files get hashed (a read of the whole file over the network).
    if known is None:
        known = load_known_documents()
    if reprocess is None:
        reprocess = load_reprocess_paths()
    if (
        not full_reindex
        and known.get(str(file_path)) == (stat.st_size, int(stat.st_mtime))
        and str(file_path) not in reprocess
    ):
        return "skipped"

    file_hash = hash_file(file_path)
    parsed = parse_document(file_path, file_hash=file_hash)
    chunks = chunk_segments(
        parsed.segments,
        chunk_size=settings.ingest_chunk_size,
        chunk_overlap=settings.ingest_chunk_overlap,
    )
    if not chunks:
        if parsed.ocr is not None:
            with session_scope() as session:
                record_ocr(session, str(file_path), file_hash, parsed.ocr, settings)
        raise ValueError("No extractable text found")

    vectors = embed_passages(chunk.text for chunk in chunks)
    chunk_rows = []
    for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=False), start=1):
        chunk_id = f"{file_hash}:{index}"
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))
        chunk_rows.append(
            {
                "chunk_id": chunk_id,
                "point_id": point_id,
                "position": index,
                "locator": chunk.locator,
                "text": chunk.text,
                "preview": chunk.text[:240],
                "vector": vector,
            }
        )

    delete_points_for_source(str(file_path))
    points = build_points(
        source_path=str(file_path),
        title=parsed.title or file_path.stem,
        author=parsed.author,
        language=parsed.language,
        chunk_rows=chunk_rows,
    )
    upsert_points(points)

    with session_scope() as session:
        existing = session.execute(
            select(DocumentRecord).where(DocumentRecord.source_path == str(file_path))
        ).scalars().first()
        if existing is None:
            existing = DocumentRecord(
                source_path=str(file_path),
                source_hash=file_hash,
                file_type=file_path.suffix.lower().lstrip("."),
                size_bytes=stat.st_size,
                modified_timestamp=int(stat.st_mtime),
                title=parsed.title or file_path.stem,
                author=parsed.author,
                language=parsed.language,
                status="indexed",
                chunk_count=len(chunk_rows),
                deleted=False,
                error_message=None,
            )
            session.add(existing)
            session.flush()
        else:
            session.query(ChunkRecord).filter(ChunkRecord.document_id == existing.id).delete()
            existing.source_hash = file_hash
            existing.file_type = file_path.suffix.lower().lstrip(".")
            existing.size_bytes = stat.st_size
            existing.modified_timestamp = int(stat.st_mtime)
            existing.title = parsed.title or file_path.stem
            existing.author = parsed.author
            existing.language = parsed.language
            existing.status = "indexed"
            existing.chunk_count = len(chunk_rows)
            existing.deleted = False
            existing.error_message = None
            existing.last_indexed_at = datetime.now(UTC)

        session.flush()
        for row in chunk_rows:
            session.add(
                ChunkRecord(
                    document_id=existing.id,
                    chunk_id=row["chunk_id"],
                    position=row["position"],
                    locator=row["locator"],
                    preview=row["preview"],
                )
            )
        if parsed.ocr is not None:
            record_ocr(session, str(file_path), file_hash, parsed.ocr, settings)

    return "indexed"


def remove_deleted_documents(seen_paths: set[str], report: ScanReport | None = None) -> int:
    """Mark documents that were not seen as deleted, except under paths the scan could not read."""
    deleted = 0
    with session_scope() as session:
        rows = session.execute(
            select(DocumentRecord).where(DocumentRecord.deleted.is_(False))
        ).scalars()
        for row in rows:
            if row.source_path not in seen_paths and not (report and report.is_unreliable(row.source_path)):
                delete_points_for_source(row.source_path)
                row.deleted = True
                row.status = "deleted"
                row.last_indexed_at = datetime.now(UTC)
                deleted += 1
    return deleted


def store_error_record(file_path: Path, error_message: str) -> None:
    stat = file_path.stat()
    with session_scope() as session:
        existing = session.execute(
            select(DocumentRecord).where(DocumentRecord.source_path == str(file_path))
        ).scalars().first()
        if existing is None:
            existing = DocumentRecord(
                source_path=str(file_path),
                source_hash="unknown",
                file_type=file_path.suffix.lower().lstrip("."),
                size_bytes=stat.st_size,
                modified_timestamp=int(stat.st_mtime),
                title=file_path.stem,
                author=None,
                language=None,
                status="failed",
                chunk_count=0,
                deleted=False,
                error_message=error_message,
            )
            session.add(existing)
        else:
            existing.status = "failed"
            existing.error_message = error_message
            existing.deleted = False


def update_run(run_id: int, status: str, message: str, scanned: int = 0, indexed: int = 0, skipped: int = 0, failed: int = 0, deleted: int = 0) -> None:
    with session_scope() as session:
        run = session.execute(select(IngestRun).where(IngestRun.id == run_id)).scalars().first()
        if run is None:
            return
        run.status = status
        run.message = message
        run.scanned = scanned
        run.indexed = indexed
        run.skipped = skipped
        run.failed = failed
        run.deleted = deleted
        run.finished_at = datetime.now(UTC)

