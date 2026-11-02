"""Document Intelligence routes: start, watch, stop, resume, read and export whole-document jobs.

The router has no auth of its own: the session and CSRF middleware in ``main.py`` protects every route. Jobs
and results belong to the person who started them, in their own workspace; every route filters by that person and by the
workspaces the server derives for them, so a shared personal workspace does not share its jobs.
"""

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from teacher_common import docintel, docintel_store
from teacher_common.config import get_settings
from teacher_common.db import session_scope
from teacher_common.docintel_worker import Worker
from teacher_common.models import DocumentRecord
from teacher_common.workspaces import accessible_workspace_ids, ensure_personal_workspace

KIND_TITLES = {
    "summary": "Összefoglaló",
    "extract": "Kigyűjtés",
    "compare": "Összehasonlítás",
    "tasks": "Teendők és javaslatok",
    "translate": "Fordítás",
}
EXPORT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


class JobRequest(BaseModel):
    kind: Literal["summary", "extract", "compare", "tasks", "translate"]
    source_paths: list[str] = Field(min_length=1, max_length=2)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    # summary: granularity; extract and compare: types; compare: mode; translate: direction
    granularity: Literal["document", "chapter"] | None = None
    types: list[str] | None = Field(default=None, max_length=10)
    mode: Literal["diff", "gap"] | None = None
    direction: Literal["hu-en", "en-hu"] | None = None


def build_router(worker: Worker) -> APIRouter:
    settings = get_settings()
    router = APIRouter(prefix="/docintel", tags=["docintel"])

    def _principal(request: Request, session, *, write: bool = False) -> tuple[str, set[str]]:
        principal = getattr(request.state, "session", None)
        if principal is None and settings.auth_enabled:
            raise HTTPException(status_code=401, detail="Bejelentkezés szükséges.")
        username = principal.username if principal else "local"
        role = principal.role if principal else "user"
        return username, accessible_workspace_ids(session, username, admin=role == "admin", write=write)

    def _options(payload: JobRequest) -> dict:
        kind = payload.kind
        if kind == "compare" and len(set(payload.source_paths)) != 2:
            raise HTTPException(status_code=422, detail="Az összehasonlításhoz pontosan két különböző dokumentum kell.")
        if kind != "compare" and len(payload.source_paths) != 1:
            raise HTTPException(status_code=422, detail="Ehhez a feladathoz egy dokumentumot válassz.")
        if kind == "summary":
            return {"granularity": payload.granularity or "document"}
        if kind == "extract":
            allowed = payload.types or list(docintel.EXTRACT_TYPES)
            if not set(allowed) <= set(docintel.EXTRACT_TYPES):
                raise HTTPException(status_code=422, detail="Ismeretlen kigyűjtési típus.")
            return {"types": allowed}
        if kind == "compare":
            allowed = payload.types or list(docintel.COMPARE_TYPES)
            if not set(allowed) <= set(docintel.EXTRACT_TYPES) - {"table"}:
                raise HTTPException(status_code=422, detail="Ismeretlen összehasonlítási típus.")
            return {"mode": payload.mode or "diff", "types": allowed}
        if kind == "translate":
            if payload.direction is None:
                raise HTTPException(status_code=422, detail="Add meg a fordítás irányát (hu-en vagy en-hu).")
            return {"direction": payload.direction}
        return {}

    def _job_error(exc: Exception) -> HTTPException:
        if isinstance(exc, docintel_store.NotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=409, detail=str(exc))

    @router.post("/jobs", status_code=202)
    def create_job(payload: JobRequest, request: Request):
        options = _options(payload)
        with session_scope() as session:
            username, workspace_ids = _principal(request, session)
            wanted = list(dict.fromkeys(payload.source_paths))
            rows = session.execute(
                select(DocumentRecord).where(
                    DocumentRecord.source_path.in_(wanted),
                    DocumentRecord.workspace_id.in_(workspace_ids),
                    DocumentRecord.deleted.is_(False),
                )
            ).scalars().all() if workspace_ids else []
            by_path = {row.source_path: row for row in rows}
            if set(wanted) - set(by_path):
                raise HTTPException(status_code=404, detail="A dokumentum nem található az indexben.")
            if docintel_store.pending_count(session, username) >= settings.docintel_max_queued_jobs:
                raise HTTPException(status_code=429, detail="Túl sok várakozó feladat. Várd meg, amíg lefutnak.")
            sources = [
                {
                    "source_path": path,
                    "source_hash": by_path[path].source_hash,
                    "workspace_id": by_path[path].workspace_id,
                    "file_name": path.rsplit("/", 1)[-1],
                    "title": by_path[path].title,
                }
                for path in wanted
            ]
            names = ", ".join(s["title"] or s["file_name"] for s in sources)
            title = payload.title or f"{KIND_TITLES[payload.kind]}: {names}"[:200]
            job = docintel_store.create_job(
                session,
                workspace_id=ensure_personal_workspace(session, username).id,
                username=username,
                kind=payload.kind,
                title=title,
                sources=sources,
                options=options,
                model=settings.ollama_model,
            )
        worker.wake()
        return job

    @router.get("/jobs")
    def list_jobs(request: Request, kind: str | None = None):
        with session_scope() as session:
            username, workspace_ids = _principal(request, session)
            return docintel_store.list_jobs(session, username, workspace_ids, kind)

    @router.get("/jobs/{job_id}")
    def get_job(job_id: int, request: Request):
        with session_scope() as session:
            username, workspace_ids = _principal(request, session)
            try:
                return docintel_store.get_job(session, job_id, username, workspace_ids)
            except docintel_store.NotFoundError as exc:
                raise _job_error(exc) from exc

    @router.get("/jobs/{job_id}/result")
    def get_result(job_id: int, request: Request):
        with session_scope() as session:
            username, workspace_ids = _principal(request, session)
            try:
                job, result = docintel_store.get_result(session, job_id, username, workspace_ids)
            except (docintel_store.NotFoundError, docintel_store.StateError) as exc:
                raise _job_error(exc) from exc
            return {"job": job, "result": result}

    @router.get("/jobs/{job_id}/export")
    def export_result(job_id: int, request: Request, format: Literal["csv", "md", "txt"] = "md"):
        with session_scope() as session:
            username, workspace_ids = _principal(request, session)
            try:
                job, result = docintel_store.get_result(session, job_id, username, workspace_ids)
            except (docintel_store.NotFoundError, docintel_store.StateError) as exc:
                raise _job_error(exc) from exc
        kind = result["kind"]
        if format == "csv" and kind != "translate":
            body = docintel.export_csv(result)
        elif format == "md":
            body = docintel.export_markdown(result, job["title"])
        elif format == "txt" and kind == "translate":
            body = docintel.export_text(result)
        else:
            raise HTTPException(status_code=422, detail="Ez a formátum ehhez az eredményhez nem érhető el.")
        filename = f"docintel-{kind}-{job_id}.{format}"
        return Response(
            body,
            media_type=EXPORT_TYPES[format],
            headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"},
        )

    @router.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: int, request: Request):
        with session_scope() as session:
            username, workspace_ids = _principal(request, session, write=True)
            try:
                return docintel_store.request_cancel(session, job_id, username, workspace_ids)
            except (docintel_store.NotFoundError, docintel_store.StateError) as exc:
                raise _job_error(exc) from exc

    @router.post("/jobs/{job_id}/resume")
    def resume_job(job_id: int, request: Request):
        with session_scope() as session:
            username, workspace_ids = _principal(request, session, write=True)
            if docintel_store.pending_count(session, username) >= settings.docintel_max_queued_jobs:
                raise HTTPException(status_code=429, detail="Túl sok várakozó feladat. Várd meg, amíg lefutnak.")
            try:
                job = docintel_store.resume(session, job_id, username, workspace_ids)
            except (docintel_store.NotFoundError, docintel_store.StateError) as exc:
                raise _job_error(exc) from exc
        worker.wake()
        return job

    @router.delete("/jobs/{job_id}", status_code=204)
    def delete_job(job_id: int, request: Request):
        with session_scope() as session:
            username, workspace_ids = _principal(request, session, write=True)
            try:
                docintel_store.delete_job(session, job_id, username, workspace_ids)
            except (docintel_store.NotFoundError, docintel_store.StateError) as exc:
                raise _job_error(exc) from exc
        return Response(status_code=204)

    return router
