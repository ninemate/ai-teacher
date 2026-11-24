"""Administrative and personal session APIs. All admin routes share one authorization guard."""
import csv
import hashlib
import io
import time
from importlib import metadata

import audit
import auth
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import and_, or_, select
from teacher_common.db import session_scope
from teacher_common.models import AuditEvent, AuthSession, ExcludedDocument, IngestRun, Workspace, WorkspaceShare

OWN_SESSION_EVENTS = ["login.success", "login.failure", "logout", "session.expired", "session.revoked"]


def build_router(settings, users, get_admins, require_admin):
    router = APIRouter()
    admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

    def sid(row):
        return hashlib.sha256(("session-id:" + row.token_hash).encode()).hexdigest()

    def session_data(row, current):
        return {"id": sid(row), "username": row.username, "created_at": row.created_at,
                "expires_at": row.expires_at, "current": row.token_hash == current}

    @router.get("/account/sessions")
    def my_sessions(request: Request):
        if request.state.session is None:
            raise HTTPException(403, "Bejelentkezés szükséges.")
        username = request.state.session.username
        current = auth._token_hash(request.cookies.get(auth.SESSION_COOKIE, ""))
        with session_scope() as db:
            rows = db.execute(select(AuthSession).where(AuthSession.username == username,
                              AuthSession.expires_at > int(time.time())).order_by(AuthSession.created_at.desc())).scalars()
            active = [session_data(row, current) for row in rows]
            own = and_(AuditEvent.actor == username, AuditEvent.event.in_(OWN_SESSION_EVENTS))
            by_admin = and_(AuditEvent.target == username, AuditEvent.event == "admin.session_revoke")
            recent = db.execute(select(AuditEvent).where(or_(own, by_admin))
                                .order_by(AuditEvent.id.desc()).limit(50)).scalars()
            return {"active": active, "recent": [{"event": r.event, "timestamp": r.timestamp, "source_ip": r.source_ip}
                                                 for r in recent]}

    @router.post("/account/sessions/revoke-others")
    def revoke_others(request: Request):
        if request.state.session is None:
            raise HTTPException(403, "Bejelentkezés szükséges.")
        username = request.state.session.username
        current = auth._token_hash(request.cookies.get(auth.SESSION_COOKIE, ""))
        with session_scope() as db:
            rows = db.execute(select(AuthSession).where(AuthSession.username == username,
                              AuthSession.token_hash != current)).scalars().all()
            for row in rows:
                audit.record(db, "session.revoked", username, row.username, audit.client_ip(request))
                db.delete(row)
        return {"revoked": len(rows)}

    @admin.get("/users")
    def list_users():
        admins = get_admins()
        return [{"username": u, "role": "admin" if u in admins else "user"} for u in sorted(users)]

    @admin.get("/sessions")
    def sessions(request: Request):
        current = auth._token_hash(request.cookies.get(auth.SESSION_COOKIE, ""))
        with session_scope() as db:
            rows = db.execute(select(AuthSession).where(AuthSession.expires_at > int(time.time()))
                              .order_by(AuthSession.created_at.desc())).scalars()
            return [session_data(row, current) for row in rows]

    @admin.delete("/sessions/{session_id}")
    def revoke(session_id: str, request: Request):
        with session_scope() as db:
            rows = db.execute(select(AuthSession)).scalars().all()
            row = next((r for r in rows if sid(r) == session_id), None)
            if row is None:
                raise HTTPException(404, "A munkamenet nem található.")
            audit.record(db, "admin.session_revoke", request.state.session.username, row.username, audit.client_ip(request))
            db.delete(row)
        return {"status": "revoked"}

    @admin.get("/workspaces")
    def workspaces():
        with session_scope() as db:
            rows = db.execute(select(Workspace).order_by(Workspace.name)).scalars().all()
            shares = db.execute(select(WorkspaceShare)).scalars().all()
            return [{"id": r.id, "name": r.name, "owner": r.owner_username,
                     "shares": [{"username": s.username, "permission": s.permission} for s in shares if s.workspace_id == r.id]} for r in rows]

    @admin.get("/ingest")
    def ingest():
        with session_scope() as db:
            rows = db.execute(select(IngestRun).order_by(IngestRun.id.desc()).limit(100)).scalars()
            return [{"id": r.id, "status": r.status, "scanned": r.scanned, "indexed": r.indexed,
                     "failed": r.failed, "started_at": r.started_at.isoformat() if r.started_at else None,
                     "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in rows]

    @admin.get("/exclusions")
    def exclusions():
        with session_scope() as db:
            return [{"id": r.id, "workspace_id": r.workspace_id, "source_path": r.source_path}
                    for r in db.execute(select(ExcludedDocument)).scalars()]

    @admin.post("/exclusions/{excluded_id}/restore")
    def restore(excluded_id: int, request: Request):
        with session_scope() as db:
            row = db.get(ExcludedDocument, excluded_id)
            if row is None:
                raise HTTPException(404, "A kizárás nem található.")
            audit.record(db, "admin.exclusion_restore", request.state.session.username, f"{row.workspace_id}#{row.id}", audit.client_ip(request))
            db.delete(row)
        return {"status": "restored"}

    @admin.get("/inventory")
    def inventory():
        packages = []
        for name in ("fastapi", "sqlalchemy", "httpx", "sentence-transformers"):
            try:
                version = metadata.version(name)
            except metadata.PackageNotFoundError:
                version = "unavailable"
            packages.append({"name": name, "version": version})
        return {"configured_models": {"chat": settings.ollama_model, "embedding": settings.embedding_model},
                "api_packages": packages, "note": "For the full installed inventory run make inventory on the host."}

    def query_events(db, event, actor, since, until, limit):
        query = select(AuditEvent)
        if event:
            # "admin." matches every admin action; anything else is an exact event name.
            query = query.where(AuditEvent.event.startswith(event, autoescape=True) if event.endswith(".") else AuditEvent.event == event)
        if actor:
            query = query.where(AuditEvent.actor == actor)
        if since is not None:
            query = query.where(AuditEvent.timestamp >= since)
        if until is not None:
            query = query.where(AuditEvent.timestamp <= until)
        return db.execute(query.order_by(AuditEvent.id.desc()).limit(limit)).scalars().all()

    @admin.get("/audit")
    def events(request: Request, event: str = "", actor: str = "", since: int | None = None,
               until: int | None = None, limit: int = Query(200, ge=1, le=10000), format: str = "json"):
        if format not in ("json", "csv"):
            raise HTTPException(422, "Ismeretlen formátum.")
        with session_scope() as db:
            rows = query_events(db, event, actor, since, until, limit)
            data = [{"id": r.id, "timestamp": r.timestamp, "event": r.event, "actor": r.actor,
                     "target": r.target, "source_ip": r.source_ip} for r in rows]
        if format == "csv":
            with session_scope() as db:
                audit.record(db, "admin.audit_export", request.state.session.username, f"{len(data)} rows",
                             audit.client_ip(request))
            out = io.StringIO(newline="")
            writer = csv.DictWriter(out, fieldnames=["id", "timestamp", "event", "actor", "target", "source_ip"])
            writer.writeheader()
            for item in data:
                writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in item.items()})
            return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=audit.csv"})
        return data

    router.include_router(admin)
    return router
