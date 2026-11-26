"""Upload, view and export only the authenticated user's own meeting jobs."""

import os
import uuid
from pathlib import Path
from typing import Literal

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from teacher_common import meeting_notes, meeting_store
from teacher_common.db import session_scope
from teacher_common.meeting_models import MeetingAssignment, MeetingJob
from teacher_common.meeting_worker import MeetingWorker
from teacher_common.workspaces import ensure_personal_workspace

MAX_BYTES = 512 * 1024 * 1024
MAX_ACTIVE = 3
EXPORT_TYPES = {"md": "text/markdown", "txt": "text/plain", "srt": "application/x-subrip; charset=utf-8"}


class AssignmentRequest(BaseModel):
    name: str | None = Field(max_length=100)


def _username(request: Request) -> str:
    principal = request.state.session
    return principal.username if principal else "local"


def _owned_job(session: Session, request: Request, job_id: str) -> MeetingJob:
    job = meeting_store.get_job(session, _username(request), job_id)
    if job is None:
        raise HTTPException(404, "A meeting nem található.")
    return job


def _check_queue(session: Session, username: str) -> None:
    active = session.scalar(
        select(func.count()).select_from(MeetingJob)
        .where(MeetingJob.username == username, MeetingJob.status.in_(meeting_store.ACTIVE))
    )
    if active is not None and active >= MAX_ACTIVE:
        raise HTTPException(429, "Túl sok aktív feldolgozás. Várd meg, amíg valamelyik befejeződik.")


def build_router(worker: MeetingWorker) -> APIRouter:
    router = APIRouter(prefix="/meetings", tags=["meetings"])

    @router.get("/formats")
    def formats():
        return {"extensions": worker.decoder.extensions, "video": worker.decoder.ffmpeg is not None,
                "max_mib": MAX_BYTES // (1024 * 1024)}

    @router.get("/jobs")
    def list_jobs(request: Request):
        with session_scope() as session:
            return meeting_store.list_jobs(session, _username(request))

    @router.post("/jobs", status_code=202)
    async def create_job(request: Request, filename: str):
        # Stream the binary body directly to a private file, so the size limit applies during upload.
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(char for char in filename if char.isprintable()).strip()[-255:]
        if Path(filename).suffix.lower() not in worker.decoder.extensions:
            raise HTTPException(415, "Ez a fájlformátum nem támogatott.")
        length = request.headers.get("content-length", "")
        if length.isdecimal() and int(length) > MAX_BYTES:
            raise HTTPException(413, "A felvétel túl nagy.")
        username = _username(request)

        def check_queue() -> None:
            with session_scope() as session:
                _check_queue(session, username)

        def enqueue(job_id: str) -> dict:
            with session_scope() as session:
                # Take the SQLite write lock before counting to serialize simultaneous submissions.
                workspace = ensure_personal_workspace(session, username)
                session.execute(update(MeetingJob).where(MeetingJob.id == job_id).values(done=0))
                _check_queue(session, username)
                return meeting_store.create(session, job_id, username, workspace.id, filename)

        await run_in_threadpool(check_queue)
        job_id = uuid.uuid4().hex
        target = worker.source_path(job_id)
        await run_in_threadpool(lambda: target.parent.mkdir(mode=0o700, parents=True, exist_ok=True))
        complete = False
        try:
            size = 0
            # O_EXCL via "xb" ensures an upload never overwrites another job.
            async with await anyio.open_file(target, "xb", opener=_private_file) as output:
                async for block in request.stream():
                    size += len(block)
                    if size > MAX_BYTES:
                        raise HTTPException(413, "A felvétel túl nagy.")
                    await output.write(block)
            if not size:
                raise HTTPException(422, "Üres felvétel.")
            with anyio.CancelScope(shield=True):
                job = await run_in_threadpool(enqueue, job_id)
                complete = True
                worker.wake()
            return job
        finally:
            if not complete:
                worker.discard_source(job_id)

    @router.get("/jobs/{job_id}")
    def detail(request: Request, job_id: str):
        with session_scope() as session:
            job = _owned_job(session, request, job_id)
            return {**meeting_store.describe(job), **meeting_store.result(session, job)}

    @router.post("/jobs/{job_id}/cancel")
    def cancel(request: Request, job_id: str):
        with session_scope() as session:
            status = meeting_store.cancel(session, job_id, _username(request))
            if status is None:
                job = _owned_job(session, request, job_id)
                if job.status not in {"cancelled", "cancelling"}:
                    raise HTTPException(409, "A feldolgozás már befejeződött.")
                status = job.status
        if status == "cancelled":
            worker.discard_source(job_id)
        return {"status": status}

    @router.delete("/jobs/{job_id}", status_code=204)
    def delete(request: Request, job_id: str):
        with session_scope() as session:
            job = _owned_job(session, request, job_id)
            if job.status in meeting_store.ACTIVE:
                raise HTTPException(409, "Előbb állítsd le a feldolgozást.")
            session.execute(sql_delete(MeetingAssignment).where(MeetingAssignment.meeting_id == job_id))
            session.delete(job)
        worker.discard_source(job_id)
        return Response(status_code=204)

    @router.patch("/jobs/{job_id}/actions/{action_id}")
    def assign(request: Request, job_id: str, action_id: int, payload: AssignmentRequest):
        name = " ".join((payload.name or "").split()) or None
        with session_scope() as session:
            _owned_job(session, request, job_id)
            changed = session.execute(
                update(MeetingAssignment).where(
                    MeetingAssignment.meeting_id == job_id, MeetingAssignment.action_id == action_id,
                ).values(name=name)
            )
            if not changed.rowcount:
                raise HTTPException(404, "A teendő nem található.")
        return {"assignee": name}

    @router.get("/jobs/{job_id}/export")
    def export(request: Request, job_id: str, format: Literal["md", "txt", "srt"] = "md"):
        with session_scope() as session:
            job = _owned_job(session, request, job_id)
            if job.transcript is None or (format == "md" and job.notes is None):
                raise HTTPException(409, "Ez az eredmény még nem érhető el.")
            if format == "md":
                notes = meeting_store.result(session, job)["notes"]
                body = meeting_notes.export_notes(job.filename, job.transcript, notes)
            else:
                body = meeting_notes.export_transcript(job.transcript, srt=format == "srt")
        return Response(body, media_type=EXPORT_TYPES[format],
                        headers={"Content-Disposition": f'attachment; filename="meeting-{job_id}.{format}"'})

    return router


def _private_file(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)
