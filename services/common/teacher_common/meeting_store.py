"""Durable queue and owner-scoped access to meeting results."""

from datetime import UTC, datetime

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from teacher_common.meeting_models import MeetingAssignment, MeetingJob

ACTIVE = ("queued", "running", "cancelling")


def create(session: Session, job_id: str, username: str, workspace_id: str, filename: str) -> dict:
    job = MeetingJob(
        id=job_id, username=username, workspace_id=workspace_id, filename=filename, status="queued",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    session.add(job)
    session.flush()
    return describe(job)


def describe(job: MeetingJob) -> dict:
    return {
        "id": job.id, "filename": job.filename, "created_at": job.created_at, "status": job.status,
        "progress": {"phase": job.phase, "done": job.done, "total": job.total}, "error": job.error,
    }


def list_jobs(session: Session, username: str) -> list[dict]:
    jobs = session.scalars(
        select(MeetingJob).where(MeetingJob.username == username).order_by(MeetingJob.created_at.desc(), MeetingJob.id)
    )
    return [describe(job) for job in jobs]


def get_job(session: Session, username: str, job_id: str) -> MeetingJob | None:
    return session.scalar(select(MeetingJob).where(MeetingJob.id == job_id, MeetingJob.username == username))


def result(session: Session, job: MeetingJob) -> dict:
    notes = job.notes
    if notes is not None:
        rows = session.execute(
            select(MeetingAssignment.action_id, MeetingAssignment.name).where(MeetingAssignment.meeting_id == job.id)
        ).all()
        names: dict[int, str] = {action_id: name for action_id, name in rows}
        notes = {**notes, "actions": [
            {**item, "id": index, "assignee": names.get(index)}
            for index, item in enumerate(notes["actions"], start=1)
        ]}
    return {"segments": job.transcript, "notes": notes}


def claim_next(session: Session) -> str | None:
    next_id = (
        select(MeetingJob.id).where(MeetingJob.status == "queued")
        .order_by(MeetingJob.created_at, MeetingJob.id).limit(1).scalar_subquery()
    )
    return session.scalar(
        update(MeetingJob).where(MeetingJob.id == next_id, MeetingJob.status == "queued")
        .values(status="running", phase="transcribing").returning(MeetingJob.id)
    )


def progress(session: Session, job_id: str, done: int, total: int | None, phase: str, **values) -> None:
    session.execute(update(MeetingJob).where(MeetingJob.id == job_id, MeetingJob.status == "running")
                    .values(done=done, total=total, phase=phase, **values))


def cancel(session: Session, job_id: str, username: str) -> str | None:
    return session.scalar(
        update(MeetingJob).where(MeetingJob.id == job_id, MeetingJob.username == username,
                                 MeetingJob.status.in_(("queued", "running")))
        .values(status=case((MeetingJob.status == "queued", "cancelled"), else_="cancelling"))
        .returning(MeetingJob.status)
    )


def finish(session: Session, job_id: str, status: str, error: str | None = None, notes: dict | None = None) -> None:
    finished = session.scalar(
        update(MeetingJob).where(MeetingJob.id == job_id, MeetingJob.status == "running")
        .values(status=status, error=error, notes=notes).returning(MeetingJob.status)
    )
    if finished == "succeeded" and notes is not None:
        session.add_all(MeetingAssignment(meeting_id=job_id, action_id=index)
                        for index in range(1, len(notes["actions"]) + 1))
    if finished is None:
        session.execute(update(MeetingJob).where(MeetingJob.id == job_id, MeetingJob.status == "cancelling")
                        .values(status="cancelled"))


def recover_after_restart(session: Session) -> None:
    session.execute(update(MeetingJob).where(MeetingJob.status == "running")
                    .values(status="failed", error="A feldolgozás újraindítás miatt megszakadt. Töltsd fel újra a fájlt."))
    session.execute(update(MeetingJob).where(MeetingJob.status == "cancelling").values(status="cancelled"))
