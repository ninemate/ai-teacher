import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from teacher_common.config import get_settings
from teacher_common.db import init_db, session_scope
from teacher_common.documents import chunk_segments, hash_file, iter_library_files, parse_document
from teacher_common.embeddings import embed_passages
from teacher_common.models import ChunkRecord, DocumentRecord, IngestRun
from teacher_common.qdrant_store import (
    build_points,
    delete_points_for_source,
    ensure_collection,
    recreate_collection,
    upsert_points,
)


settings = get_settings()
app = FastAPI(title="teacher-agent-ingestion", version="0.1.0")
index_lock = threading.Lock()
current_state = {"running": False, "current_file": None}


class ReindexRequest(BaseModel):
    full_reindex: bool = False
    remove_missing: bool = True


@app.on_event("startup")
def startup_event() -> None:
    Path("/data/metadata").mkdir(parents=True, exist_ok=True)
    init_db()
    ensure_collection()
    if settings.ingest_schedule_seconds > 0:
        thread = threading.Thread(target=scheduled_loop, daemon=True)
        thread.start()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "running": current_state["running"],
        "current_file": current_state["current_file"],
    }


@app.get("/ingest/status")
def ingest_status():
    with session_scope() as session:
        row = session.execute(select(IngestRun).order_by(IngestRun.started_at.desc())).scalars().first()
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


@app.post("/ingest/reindex")
def reindex(payload: ReindexRequest):
    if current_state["running"]:
        raise HTTPException(status_code=409, detail="Indexing already running")
    thread = threading.Thread(
        target=run_indexing,
        kwargs={
            "full_reindex": payload.full_reindex,
            "remove_missing": payload.remove_missing,
        },
        daemon=True,
    )
    thread.start()
    return {
        "status": "started",
        "full_reindex": payload.full_reindex,
        "remove_missing": payload.remove_missing,
    }


def scheduled_loop() -> None:
    while True:
        if not current_state["running"]:
            run_indexing(full_reindex=False, remove_missing=settings.ingest_remove_missing)
        threading.Event().wait(settings.ingest_schedule_seconds)


def run_indexing(full_reindex: bool, remove_missing: bool) -> None:
    if not index_lock.acquire(blocking=False):
        return
    current_state["running"] = True
    current_state["current_file"] = None
    run_id = None

    try:
        with session_scope() as session:
            run = IngestRun(status="running", message="Indexing started")
            session.add(run)
            session.flush()
            run_id = run.id

        if full_reindex:
            with session_scope() as session:
                session.query(ChunkRecord).delete()
                session.query(DocumentRecord).delete()
            recreate_collection()

        library_root = Path(settings.library_path)
        if not library_root.exists():
            raise RuntimeError(f"Library path not found: {library_root}")

        scanned = indexed = skipped = failed = deleted = 0
        seen_paths: set[str] = set()

        for file_path in iter_library_files(library_root, settings.scan_extensions):
            scanned += 1
            current_state["current_file"] = str(file_path)
            seen_paths.add(str(file_path))
            try:
                outcome = index_one_document(file_path, full_reindex=full_reindex)
                if outcome == "indexed":
                    indexed += 1
                elif outcome == "skipped":
                    skipped += 1
            except Exception as exc:
                failed += 1
                store_error_record(file_path, str(exc))

        if remove_missing:
            deleted = remove_deleted_documents(seen_paths)

        update_run(
            run_id=run_id,
            status="completed",
            message="Indexing completed",
            scanned=scanned,
            indexed=indexed,
            skipped=skipped,
            failed=failed,
            deleted=deleted,
        )
    except Exception as exc:
        if run_id is not None:
            update_run(
                run_id=run_id,
                status="failed",
                message=str(exc),
            )
    finally:
        current_state["running"] = False
        current_state["current_file"] = None
        index_lock.release()


def index_one_document(file_path: Path, full_reindex: bool) -> str:
    stat = file_path.stat()
    with session_scope() as session:
        existing = session.execute(
            select(DocumentRecord).where(DocumentRecord.source_path == str(file_path))
        ).scalars().first()
        if (
            existing
            and not full_reindex
            and existing.size_bytes == stat.st_size
            and existing.modified_timestamp == int(stat.st_mtime)
            and not existing.deleted
        ):
            return "skipped"

    file_hash = hash_file(file_path)
    parsed = parse_document(file_path)
    chunks = chunk_segments(
        parsed.segments,
        chunk_size=settings.ingest_chunk_size,
        chunk_overlap=settings.ingest_chunk_overlap,
    )
    if not chunks:
        raise ValueError("No extractable text found")

    vectors = embed_passages(chunk.text for chunk in chunks)
    chunk_rows = []
    for index, (chunk, vector) in enumerate(zip(chunks, vectors), start=1):
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
            existing.last_indexed_at = datetime.now(timezone.utc)

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

    return "indexed"


def remove_deleted_documents(seen_paths: set[str]) -> int:
    deleted = 0
    with session_scope() as session:
        rows = session.execute(
            select(DocumentRecord).where(DocumentRecord.deleted.is_(False))
        ).scalars()
        for row in rows:
            if row.source_path not in seen_paths:
                delete_points_for_source(row.source_path)
                row.deleted = True
                row.status = "deleted"
                row.last_indexed_at = datetime.now(timezone.utc)
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
        run.finished_at = datetime.now(timezone.utc)

