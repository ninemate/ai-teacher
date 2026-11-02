"""Storage for Document Intelligence jobs: queue, progress, checkpoints and results.

Every read takes the caller's ``workspace_ids`` (derived on the server, as for quizzes and decks) and only sees
jobs of those workspaces. The worker's own calls (``claim_next``, ``set_progress`` ...) work by job id.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from teacher_common.docintel_models import DocIntelJob, DocIntelPart, DocIntelResult
from teacher_common.models import DocumentRecord

ACTIVE = ("queued", "running")
RESUMABLE = ("failed", "interrupted")
FINISHED = ("succeeded", "failed", "cancelled", "interrupted")


class NotFoundError(Exception):
    pass


class StateError(Exception):
    """The job is in a state that does not allow this action."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize(job: DocIntelJob) -> dict:
    params = job.params or {}
    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "kind": job.kind,
        "title": job.title,
        "status": job.status,
        "cancel_requested": bool(job.cancel_requested),
        "resumable": job.status in RESUMABLE,
        "progress": {"done": job.progress_done, "total": job.progress_total, "phase": job.phase},
        "error": job.error,
        "stale": bool(job.stale),
        "stale_reason": job.stale_reason,
        "sources": [
            {"source_path": s["source_path"], "file_name": s.get("file_name"), "title": s.get("title")}
            for s in params.get("sources", [])
        ],
        "options": params.get("options", {}),
        "model": job.model,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


def create_job(
    session: Session, *, workspace_id: str, username: str, kind: str, title: str, sources: list[dict],
    options: dict, model: str | None,
) -> dict:
    job = DocIntelJob(
        workspace_id=workspace_id, username=username, kind=kind, title=title, status="queued",
        params={"sources": sources, "options": options}, model=model,
    )
    session.add(job)
    session.flush()
    return serialize(job)


def pending_count(session: Session, username: str) -> int:
    return len(session.execute(
        select(DocIntelJob.id).where(DocIntelJob.username == username, DocIntelJob.status.in_(ACTIVE))
    ).all())


def _get(session: Session, job_id: int, workspace_ids: set[str]) -> DocIntelJob:
    job = session.execute(
        select(DocIntelJob).where(DocIntelJob.id == job_id, DocIntelJob.workspace_id.in_(workspace_ids))
    ).scalar_one_or_none() if workspace_ids else None
    if job is None:
        raise NotFoundError("A feladat nem található.")
    refresh_stale(session, job)
    return job


def refresh_stale(session: Session, job: DocIntelJob) -> None:
    """Mark a finished result stale when a source document has since been deleted or changed."""
    if job.stale or job.status != "succeeded":
        return
    for source in (job.params or {}).get("sources", []):
        document = session.execute(
            select(DocumentRecord).where(DocumentRecord.source_path == source["source_path"])
        ).scalar_one_or_none()
        if document is None or document.deleted:
            job.stale, job.stale_reason = True, "A forrásdokumentumot törölték az indexből."
        elif source.get("source_hash") and document.source_hash != source["source_hash"]:
            job.stale, job.stale_reason = True, "A forrásdokumentum azóta megváltozott."
        if job.stale:
            return


def list_jobs(session: Session, workspace_ids: set[str], kind: str | None = None) -> list[dict]:
    if not workspace_ids:
        return []
    query = select(DocIntelJob).where(DocIntelJob.workspace_id.in_(workspace_ids))
    if kind:
        query = query.where(DocIntelJob.kind == kind)
    jobs = session.execute(query.order_by(DocIntelJob.id.desc()).limit(200)).scalars().all()
    for job in jobs:
        refresh_stale(session, job)
    return [serialize(job) for job in jobs]


def get_job(session: Session, job_id: int, workspace_ids: set[str]) -> dict:
    return serialize(_get(session, job_id, workspace_ids))


def get_result(session: Session, job_id: int, workspace_ids: set[str]) -> tuple[dict, dict]:
    """(job, result content); StateError while there is no result."""
    job = _get(session, job_id, workspace_ids)
    result = session.get(DocIntelResult, job.id)
    if result is None:
        raise StateError("A feladatnak még nincs eredménye.")
    return serialize(job), result.content


def request_cancel(session: Session, job_id: int, workspace_ids: set[str]) -> dict:
    job = _get(session, job_id, workspace_ids)
    if job.status == "queued":
        job.status, job.finished_at, job.error = "cancelled", _now(), "A feladatot leállították."
    elif job.status == "running":
        # The job stops after the model call it is in the middle of.
        job.cancel_requested = True
    else:
        raise StateError("Csak várakozó vagy futó feladat állítható le.")
    return serialize(job)


def resume(session: Session, job_id: int, workspace_ids: set[str]) -> dict:
    job = _get(session, job_id, workspace_ids)
    if job.status not in RESUMABLE:
        raise StateError("Csak megszakadt vagy sikertelen feladat folytatható.")
    job.status, job.error, job.cancel_requested, job.finished_at = "queued", None, False, None
    return serialize(job)


def delete_job(session: Session, job_id: int, workspace_ids: set[str]) -> None:
    job = _get(session, job_id, workspace_ids)
    if job.status == "running":
        raise StateError("Futó feladat nem törölhető; előbb állítsd le.")
    session.query(DocIntelPart).filter(DocIntelPart.job_id == job.id).delete()
    session.query(DocIntelResult).filter(DocIntelResult.job_id == job.id).delete()
    session.delete(job)


def mark_stale_for_source(session: Session, source_path: str) -> int:
    """A document was deleted: results made from it are kept but marked stale; unfinished jobs on it stop.

    Across every workspace, because a result in someone's personal workspace may be made from a shared document.
    """
    count = 0
    for job in session.execute(select(DocIntelJob)).scalars().all():
        if source_path not in [s["source_path"] for s in (job.params or {}).get("sources", [])]:
            continue
        if job.status == "succeeded" and not job.stale:
            job.stale, job.stale_reason = True, "A forrásdokumentumot törölték az indexből."
        elif job.status == "running":
            job.cancel_requested = True
        elif job.status in ("queued", "failed", "interrupted"):
            job.status, job.error, job.finished_at = "cancelled", "A forrásdokumentumot törölték az indexből.", _now()
        count += 1
    return count


# --- the worker's side -------------------------------------------------------------------------------------------


def claim_next(session: Session) -> dict | None:
    """Take the oldest queued job and mark it running. One worker only, so one LLM job runs at a time."""
    job = session.execute(
        select(DocIntelJob).where(DocIntelJob.status == "queued").order_by(DocIntelJob.id).limit(1)
    ).scalar_one_or_none()
    if job is None:
        return None
    result = session.execute(
        update(DocIntelJob).where(DocIntelJob.id == job.id, DocIntelJob.status == "queued")
        .values(status="running", started_at=_now(), error=None, cancel_requested=False,
                progress_done=0, progress_total=0, phase="Indul")
    )
    if not getattr(result, "rowcount", 0):
        return None
    session.refresh(job)
    return {**serialize(job), "username": job.username, "params": job.params}


def recover_after_restart(session: Session) -> int:
    """Jobs left running by a process that died are interrupted (resumable), never left looking alive."""
    jobs = session.execute(select(DocIntelJob).where(DocIntelJob.status == "running")).scalars().all()
    for job in jobs:
        job.status, job.finished_at = "interrupted", _now()
        job.error = "A szolgáltatás újraindult a feldolgozás közben. Folytatható: a kész lépések megmaradtak."
    return len(jobs)


def set_progress(session: Session, job_id: int, done: int, total: int, phase: str) -> None:
    session.execute(
        update(DocIntelJob).where(DocIntelJob.id == job_id)
        .values(progress_done=done, progress_total=total, phase=phase[:255])
    )


def cancel_requested(session: Session, job_id: int) -> bool:
    return bool(session.execute(select(DocIntelJob.cancel_requested).where(DocIntelJob.id == job_id)).scalar())


def load_parts(session: Session, job_id: int) -> dict[str, Any]:
    rows = session.execute(select(DocIntelPart).where(DocIntelPart.job_id == job_id)).scalars()
    return {row.key: row.payload for row in rows}


def save_part(session: Session, job_id: int, key: str, payload: Any) -> None:
    part = session.execute(
        select(DocIntelPart).where(DocIntelPart.job_id == job_id, DocIntelPart.key == key)
    ).scalar_one_or_none()
    if part is None:
        session.add(DocIntelPart(job_id=job_id, key=key, payload=payload))
    else:
        part.payload = payload


def finish(session: Session, job_id: int, status: str, error: str | None = None, content: dict | None = None) -> None:
    job = session.get(DocIntelJob, job_id)
    if job is None:  # deleted while it ran
        return
    job.status, job.error, job.finished_at = status, error, _now()
    if content is not None:
        session.merge(DocIntelResult(job_id=job_id, content=content))
    if status in ("succeeded", "cancelled"):
        # Nothing left to resume from; the result (if any) is all that stays.
        session.query(DocIntelPart).filter(DocIntelPart.job_id == job_id).delete()
