import logging
import math
import secrets
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import auth
import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from study_routes import build_router as build_study_router
from teacher_common import citations, study_store
from teacher_common.config import get_settings
from teacher_common.db import init_db, session_scope
from teacher_common.embeddings import embed_query, warmup_embeddings
from teacher_common.models import DocumentRecord, IngestRun, OcrRecord, Workspace, WorkspaceShare
from teacher_common.qdrant_store import ensure_collection, search
from teacher_common.workspaces import accessible_workspace_ids, ensure_personal_workspace

APP_DIR = Path(__file__).resolve().parent
PUBLIC_PATHS = {"/health", "/login"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

settings = get_settings()
auth_users = settings.auth_users()
auth_admins = settings.auth_admins()
login_limiter = auth.LoginRateLimiter(settings.teacher_login_max_attempts, settings.teacher_login_window_seconds)
app = FastAPI(title="teacher-agent-api", version="0.1.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


class ChatRequest(BaseModel):
    question: str = Field(min_length=3)
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    top_k: int | None = None


class TaskRequest(BaseModel):
    topic: str = Field(min_length=3)
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    voice: str | None = Field(default=None, max_length=100)


class ReindexRequest(BaseModel):
    full_reindex: bool = False
    remove_missing: bool = True


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class WorkspaceShareRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    permission: Literal["read", "write"] = "read"


def _route_path(request: Request) -> str:
    # The path the router matches on. `request.url.path` is rebuilt from the Host header and can be spoofed.
    return request.scope["path"]


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


def _identity(request: Request) -> tuple[str, str]:
    session = request.state.session
    return (session.username, session.role) if session else ("local", "user")


def _workspace_ids(request: Request, db, *, write: bool = False) -> set[str]:
    username, role = _identity(request)
    return accessible_workspace_ids(db, username, admin=role == "admin", write=write)


def _require_admin(request: Request) -> None:
    if _identity(request)[1] != "admin":
        raise HTTPException(status_code=403, detail="Rendszergazdai jogosultság szükséges.")


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Default deny: every route except health, login and static files needs a session."""
    request.state.session = None
    if request.method in UNSAFE_METHODS and not auth.origin_allowed(request):
        return JSONResponse({"detail": "A kérés másik oldalról érkezett."}, status_code=403)
    if settings.auth_enabled and not _is_public(_route_path(request)):
        token = request.cookies.get(auth.SESSION_COOKIE)
        session = await run_in_threadpool(auth.load_session, token, auth_users, auth_admins)
        if session is None:
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                target = _route_path(request) + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
            return JSONResponse({"detail": "Bejelentkezés szükséges."}, status_code=401)
        if request.method in UNSAFE_METHODS and not auth.csrf_valid(request, session):
            return JSONResponse({"detail": "Hiányzó vagy érvénytelen CSRF token."}, status_code=403)
        request.state.session = session
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    path = _route_path(request)
    if not path.startswith(("/docs", "/redoc")):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if not path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.on_event("startup")
def startup_event() -> None:
    if not settings.auth_enabled:
        logging.getLogger("uvicorn").warning("TEACHER_AUTH_DISABLED is set: the UI and API are open to anyone.")
    elif not auth_users:
        logging.getLogger("uvicorn").warning("TEACHER_AUTH_USERS is empty: nobody can log in.")
    Path("/data/metadata").mkdir(parents=True, exist_ok=True)
    init_db()
    _claim_legacy_workspace()
    ensure_collection()
    warmup_embeddings()
    _pull_ollama_model()


def _claim_legacy_workspace() -> None:
    """Keep a single-account installation working while multi-account legacy data stays admin-only."""
    admins = auth_admins
    owner = next(iter(admins)) if len(admins) == 1 else (next(iter(auth_users)) if len(auth_users) == 1 else None)
    if owner is None:
        return
    with session_scope() as session:
        workspace = session.get(Workspace, "legacy")
        if workspace is not None and workspace.owner_username == "legacy":
            workspace.owner_username = owner


def _pull_ollama_model() -> None:
    import httpx
    try:
        with httpx.Client(timeout=600.0) as client:
            resp = client.post(
                f"{settings.ollama_base_url}/api/pull",
                json={"name": settings.ollama_model},
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        import logging
        logging.getLogger("uvicorn").warning("Ollama model pull failed (will retry on first request): %s", exc)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    session = request.state.session
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "auth_enabled": settings.auth_enabled,
            "username": session.username if session else None,
            "csrf_token": session.csrf_token if session else "",
            "default_level": "beginner",
        },
    )


def _render_login(request: Request, next_path: str, error: str | None = None, status_code: int = 200) -> Response:
    login_csrf = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": login_csrf, "next": next_path, "error": error},
        status_code=status_code,
    )
    auth.set_cookie(response, request, settings, auth.LOGIN_CSRF_COOKIE, login_csrf, max_age=3600)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    next_path = auth.safe_next(next)
    if not settings.auth_enabled or auth.load_session(request.cookies.get(auth.SESSION_COOKIE), auth_users, auth_admins):
        return RedirectResponse(next_path, status_code=303)
    return _render_login(request, next_path)


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    next: str = Form("/"),
):
    next_path = auth.safe_next(next)
    if not settings.auth_enabled:
        return RedirectResponse(next_path, status_code=303)

    cookie_csrf = request.cookies.get(auth.LOGIN_CSRF_COOKIE, "")
    if not auth.same_secret(cookie_csrf, csrf_token):
        return _render_login(request, next_path, "Az űrlap lejárt. Kérlek, próbáld újra.", status_code=400)

    username = username.strip()
    client_key = f"ip:{request.client.host if request.client else 'unknown'}"
    limit_keys = [client_key] + ([f"user:{username}"] if username in auth_users else [])
    retry_after = login_limiter.acquire(limit_keys)
    if retry_after:
        minutes = math.ceil(retry_after / 60)
        response = _render_login(
            request,
            next_path,
            f"Túl sok sikertelen próbálkozás. Próbáld újra {minutes} perc múlva.",
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    if len(password) > 1024 or not auth.verify_credentials(auth_users, username, password):
        return _render_login(request, next_path, "Hibás felhasználónév vagy jelszó.", status_code=401)

    login_limiter.release(limit_keys)
    login_limiter.reset(f"user:{username}")
    ttl_seconds = settings.teacher_session_ttl_hours * 3600
    role = "admin" if username in auth_admins else "user"
    token, _ = auth.create_session(username, auth_users[username], ttl_seconds, role=role)
    response = RedirectResponse(next_path, status_code=303)
    auth.set_cookie(response, request, settings, auth.SESSION_COOKIE, token, max_age=ttl_seconds)
    response.delete_cookie(auth.LOGIN_CSRF_COOKIE, path="/")
    return response


@app.post("/logout")
def logout(request: Request):
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/session")
def current_session(request: Request):
    session = request.state.session
    return {
        "auth_enabled": settings.auth_enabled,
        "username": session.username if session else None,
        "role": session.role if session else None,
        "csrf_token": session.csrf_token if session else None,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/sources")
def list_sources(request: Request):
    with session_scope() as session:
        workspace_ids = _workspace_ids(request, session)
        if not workspace_ids:
            return []
        rows = session.execute(
            select(DocumentRecord)
            .where(DocumentRecord.workspace_id.in_(workspace_ids), DocumentRecord.deleted.is_(False))
            .order_by(DocumentRecord.last_indexed_at.desc())
        ).scalars()
        return [
            {
                "id": row.id,
                "workspace_id": row.workspace_id,
                "source_path": row.source_path,
                "title": row.title,
                "author": row.author,
                "language": row.language,
                "file_type": row.file_type,
                "chunk_count": row.chunk_count,
                "last_indexed_at": row.last_indexed_at.isoformat() if row.last_indexed_at else None,
            }
            for row in rows
        ]


@app.delete("/sources/{document_id}", status_code=204)
def delete_source(document_id: int, request: Request):
    from teacher_common.qdrant_store import delete_points_for_source

    with session_scope() as session:
        workspace_ids = _workspace_ids(request, session, write=True)
        row = session.execute(
            select(DocumentRecord).where(
                DocumentRecord.id == document_id,
                DocumentRecord.workspace_id.in_(workspace_ids),
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="A dokumentum nem található.")
        delete_points_for_source(row.source_path)
        study_store.delete_derived_items_for_source(session, row.source_path, row.workspace_id)
        session.query(OcrRecord).filter(
            OcrRecord.source_path == row.source_path,
            OcrRecord.workspace_id == row.workspace_id,
        ).delete()
        session.delete(row)
    return Response(status_code=204)


@app.get("/workspaces")
def list_workspaces(request: Request):
    with session_scope() as session:
        username, role = _identity(request)
        workspace_ids = _workspace_ids(request, session)
        if not workspace_ids:
            return []
        rows = session.execute(select(Workspace).where(Workspace.id.in_(workspace_ids)).order_by(Workspace.name)).scalars()
        result = []
        for row in rows:
            share = session.execute(
                select(WorkspaceShare).where(
                    WorkspaceShare.workspace_id == row.id,
                    WorkspaceShare.username == username,
                )
            ).scalar_one_or_none()
            result.append({
                "id": row.id,
                "name": row.name,
                "owner": row.owner_username,
                "permission": "admin" if role == "admin" else ("owner" if row.owner_username == username else share.permission),
            })
        return result


@app.post("/workspaces", status_code=201)
def create_workspace(payload: WorkspaceCreate, request: Request):
    username, _ = _identity(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A tudástár neve nem lehet üres.")
    with session_scope() as session:
        ensure_personal_workspace(session, username)
        workspace = Workspace(id=f"kb:{uuid.uuid4().hex}", name=name, owner_username=username)
        session.add(workspace)
        session.flush()
        return {"id": workspace.id, "name": workspace.name, "owner": username, "permission": "owner"}


@app.post("/workspaces/{workspace_id}/shares", status_code=201)
def share_workspace(workspace_id: str, payload: WorkspaceShareRequest, request: Request):
    username, role = _identity(request)
    if payload.username not in auth_users:
        raise HTTPException(status_code=422, detail="A megosztott felhasználó nem létezik.")
    with session_scope() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or (workspace.owner_username != username and role != "admin"):
            raise HTTPException(status_code=404, detail="A tudástár nem található.")
        if payload.username == workspace.owner_username:
            raise HTTPException(status_code=422, detail="A tulajdonos már hozzáfér a tudástárhoz.")
        share = session.execute(
            select(WorkspaceShare).where(
                WorkspaceShare.workspace_id == workspace_id,
                WorkspaceShare.username == payload.username,
            )
        ).scalar_one_or_none()
        if share is None:
            share = WorkspaceShare(workspace_id=workspace_id, username=payload.username)
            session.add(share)
        share.permission = payload.permission
        return {"workspace_id": workspace_id, "username": payload.username, "permission": share.permission}


@app.delete("/workspaces/{workspace_id}/shares/{share_username}", status_code=204)
def unshare_workspace(workspace_id: str, share_username: str, request: Request):
    username, role = _identity(request)
    with session_scope() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None or (workspace.owner_username != username and role != "admin"):
            raise HTTPException(status_code=404, detail="A tudástár nem található.")
        share = session.execute(
            select(WorkspaceShare).where(
                WorkspaceShare.workspace_id == workspace_id,
                WorkspaceShare.username == share_username,
            )
        ).scalar_one_or_none()
        if share is None:
            raise HTTPException(status_code=404, detail="A megosztás nem található.")
        session.delete(share)
    return Response(status_code=204)


@app.get("/ingest/status")
def ingest_status(request: Request):
    _require_admin(request)
    with session_scope() as session:
        row = session.execute(select(IngestRun).order_by(IngestRun.started_at.desc())).scalars().first()
        if row is None:
            return {"status": "idle"}
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
        }


def _ingestion_request(method: str, path: str, json: dict | None = None):
    """Call the ingestion service with the ingest token; pass its refusals (409 while a run is going) through."""
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.request(
                method,
                f"{settings.ingestion_base_url}{path}",
                json=json,
                headers={"Authorization": f"Bearer {settings.teacher_ingest_token}"},
            )
    except httpx.HTTPError as exc:
        logging.getLogger("uvicorn").warning("Ingestion service unreachable: %s", exc)
        raise HTTPException(status_code=502, detail="Az indexelő szolgáltatás nem érhető el.") from exc
    if response.status_code == 409:
        raise HTTPException(status_code=409, detail="Az indexelés már fut.")
    if response.status_code >= 400:
        # 401/503 here mean the token is missing or differs between the services: a server problem.
        logging.getLogger("uvicorn").warning("Ingestion service answered %s for %s", response.status_code, path)
        raise HTTPException(status_code=502, detail="Az indexelő szolgáltatás hibát jelzett.")
    return response.json()


@app.post("/ingest/reindex")
def trigger_reindex(payload: ReindexRequest, request: Request):
    _require_admin(request)
    return _ingestion_request("POST", "/ingest/reindex", json=payload.model_dump())


@app.get("/ingest/schedule")
def ingest_schedule(request: Request):
    _require_admin(request)
    return _ingestion_request("GET", "/ingest/schedule")


@app.post("/chat")
def chat(payload: ChatRequest, request: Request):
    with session_scope() as session:
        workspace_ids = _workspace_ids(request, session)
    return generate_teaching_response(
        task="answer",
        prompt_input=payload.question,
        level=payload.level,
        top_k=payload.top_k or settings.retrieval_top_k,
        workspace_ids=workspace_ids,
    )


@app.post("/quiz")
def quiz(payload: TaskRequest, request: Request):
    with session_scope() as session:
        workspace_ids = _workspace_ids(request, session)
    return generate_teaching_response(
        task="quiz",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
        workspace_ids=workspace_ids,
    )


@app.post("/summary")
def summary(payload: TaskRequest, request: Request):
    with session_scope() as session:
        workspace_ids = _workspace_ids(request, session)
    return generate_teaching_response(
        task="summary",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
        workspace_ids=workspace_ids,
    )


@app.post("/flashcards")
def flashcards(payload: TaskRequest, request: Request):
    with session_scope() as session:
        workspace_ids = _workspace_ids(request, session)
    return generate_teaching_response(
        task="flashcards",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
        workspace_ids=workspace_ids,
    )


VOICE_UNAVAILABLE = "A hangszolgáltatás nem érhető el."


def _voice_request(method: str, path: str, timeout: float, quiet: bool = False, **kwargs) -> httpx.Response:
    """Call the internal voice service; connection problems become 503, its errors keep status and detail."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, f"{settings.voice_base_url}{path}", **kwargs)
    except httpx.HTTPError as exc:
        if not quiet:
            logging.getLogger("uvicorn").warning("Voice service call %s failed: %s", path, exc)
        raise HTTPException(status_code=503, detail=VOICE_UNAVAILABLE) from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        raise HTTPException(
            status_code=response.status_code,
            detail=detail if isinstance(detail, str) else "A hangszolgáltatás hibát jelzett.",
        )
    return response


@app.get("/voice/status")
def voice_status():
    """What the UI may offer: the voice overlay is optional, so its absence is a normal answer, not an error."""
    unavailable = {"available": False, "stt": False, "tts": False, "voices": [], "default_voice": None}
    if not settings.voice_base_url:
        return unavailable
    try:
        # Quiet: without the voice overlay this fails on every page load, which is expected.
        health = _voice_request("GET", "/health", timeout=3.0, quiet=True).json()
    except HTTPException:
        return unavailable
    stt, tts = health.get("stt") or {}, health.get("tts") or {}
    return {
        "available": True,
        "stt": bool(stt.get("enabled")),
        "tts": bool(tts.get("enabled")),
        "voices": tts.get("voices") or [],
        "default_voice": tts.get("default_voice"),
        "max_seconds": stt.get("max_seconds"),
        "max_chars": tts.get("max_chars"),
    }


@app.post("/voice/transcribe")
def voice_transcribe(audio: UploadFile = File(...)):
    limit = int(settings.voice_max_upload_mb * 1024 * 1024)
    content = audio.file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail="A hangfelvétel túl nagy.")
    if not content:
        raise HTTPException(status_code=422, detail="Üres hangfelvétel.")
    # Only the extension is passed on, as a hint for the decoder.
    suffix = Path(audio.filename or "").suffix[:8]
    response = _voice_request(
        "POST",
        "/voice/transcribe",
        timeout=settings.voice_timeout_seconds,
        files={"audio": (f"audio{suffix}", content, audio.content_type or "application/octet-stream")},
    )
    return response.json()


@app.post("/voice/speak")
def voice_speak(payload: SpeakRequest):
    response = _voice_request(
        "POST",
        "/voice/speak",
        timeout=settings.voice_timeout_seconds,
        json={"text": payload.text, "voice": payload.voice, "format": "mp3"},
    )
    return Response(response.content, media_type=response.headers.get("content-type", "audio/mpeg"))


def generate_teaching_response(task: str, prompt_input: str, level: str, top_k: int, workspace_ids: set[str]):
    hits = retrieve_sources(prompt_input, top_k=top_k, workspace_ids=workspace_ids)
    if not hits:
        answer = "A megadott könyvtárban nem találtam elég releváns információt a kérdés megbízható megválaszolásához."
        return {"answer": answer, "sources": [], "citations": [], "grounding": citations.assess(answer, [])}

    limits = citations.Limits(weak_score=settings.retrieval_weak_score, ocr_min_confidence=settings.ocr_low_confidence)
    built = citations.build_citations(hits, limits)
    context, used = citations.build_context(built, hits, settings.max_context_chars)
    base_prompt = Path(settings.teacher_prompt_path).read_text(encoding="utf-8").strip()
    task_instruction = {
        "answer": "Válaszold meg a kérdést tanári stílusban.",
        "quiz": "Készíts rövid, ellenőrző mini kvízt a témáról.",
        "summary": "Készíts tömör összefoglalót a témáról.",
        "flashcards": "Készíts kérdés-válasz alapú villámkártyákat a témáról.",
    }[task]
    prompt = (
        f"{base_prompt}\n\n"
        f"Feladat: {task_instruction}\n"
        f"Nehézségi szint: {level}\n\n"
        f"{citations.CITATION_RULES}\n\n"
        f"Forrásrészletek:\n{context}\n\n"
        f"Felhasználói kérés: {prompt_input}\n\n"
        "Válasz:"
    )
    answer = call_ollama(prompt)
    grounding = citations.assess(answer, used)
    return {"answer": answer, "sources": hits[: len(used)], "citations": used, "grounding": grounding}


def retrieve_sources(
    question: str,
    top_k: int,
    source_paths: list[str] | None = None,
    workspace_ids: set[str] | None = None,
):
    if not workspace_ids:
        return []
    query_vector = embed_query(question)
    results = search(query_vector, limit=top_k, workspace_ids=workspace_ids, source_paths=source_paths)
    return [
        {
            "chunk_id": item.payload.get("chunk_id"),
            "file_id": item.payload.get("file_id"),
            "file_name": item.payload.get("file_name"),
            "source_path": item.payload.get("source_path"),
            "title": item.payload.get("title"),
            "author": item.payload.get("author"),
            "language": item.payload.get("language"),
            "locator": item.payload.get("locator"),
            "page": item.payload.get("page"),
            "chapter": item.payload.get("chapter"),
            "ocr": item.payload.get("ocr", False),
            "ocr_confidence": item.payload.get("ocr_confidence"),
            "preview": item.payload.get("preview"),
            "text": item.payload.get("text"),
            "score": item.score,
        }
        for item in results
    ]


def call_ollama(prompt: str, json_mode: bool = False) -> str:
    payload: dict = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": 8192,
        },
    }
    if json_mode:
        payload["format"] = "json"
    with httpx.Client(timeout=180.0) as client:
        response = client.post(f"{settings.ollama_base_url}/api/generate", json=payload)
        response.raise_for_status()
    data = response.json()
    return data.get("response", "").strip()


# Stored quizzes and flashcard decks (/study/...). Defined at the bottom so the
# helpers above exist; the session and CSRF middleware protects them like every other route.
app.include_router(
    build_study_router(
        retrieve=lambda query, top_k, source_paths, workspace_ids: retrieve_sources(
            query, top_k, source_paths, workspace_ids
        ),
        llm=lambda prompt, json_mode: call_ollama(prompt, json_mode=json_mode),
    ),
)
