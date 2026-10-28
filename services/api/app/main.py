import math
import secrets
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
from teacher_common.config import get_settings
from teacher_common.db import init_db, session_scope
from teacher_common.embeddings import embed_query, warmup_embeddings
from teacher_common.models import DocumentRecord, IngestRun
from teacher_common.qdrant_store import ensure_collection, search

APP_DIR = Path(__file__).resolve().parent
PUBLIC_PATHS = {"/health", "/login"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

settings = get_settings()
auth_users = settings.auth_users()
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


class ReindexRequest(BaseModel):
    full_reindex: bool = False
    remove_missing: bool = True


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Default deny: every route except health, login and static files needs a session."""
    request.state.session = None
    if request.method in UNSAFE_METHODS and not auth.origin_allowed(request):
        return JSONResponse({"detail": "A kérés másik oldalról érkezett."}, status_code=403)
    if settings.auth_enabled and not _is_public(request.url.path):
        token = request.cookies.get(auth.SESSION_COOKIE)
        session = await run_in_threadpool(auth.load_session, token, auth_users)
        if session is None:
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
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
    if not request.url.path.startswith(("/docs", "/redoc")):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.on_event("startup")
def startup_event() -> None:
    Path("/data/metadata").mkdir(parents=True, exist_ok=True)
    init_db()
    ensure_collection()
    warmup_embeddings()
    _pull_ollama_model()


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
    if not settings.auth_enabled or auth.load_session(request.cookies.get(auth.SESSION_COOKIE), auth_users):
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
    if not cookie_csrf or not secrets.compare_digest(cookie_csrf, csrf_token):
        return _render_login(request, next_path, "Az űrlap lejárt. Kérlek, próbáld újra.", status_code=400)

    username = username.strip()
    client_key = f"ip:{request.client.host if request.client else 'unknown'}"
    limit_keys = [client_key] + ([f"user:{username}"] if username in auth_users else [])
    retry_after = login_limiter.retry_after(limit_keys)
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
        login_limiter.record_failure(limit_keys)
        return _render_login(request, next_path, "Hibás felhasználónév vagy jelszó.", status_code=401)

    login_limiter.reset(f"user:{username}")
    ttl_seconds = settings.teacher_session_ttl_hours * 3600
    token, _ = auth.create_session(username, auth_users[username], ttl_seconds)
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
        "csrf_token": session.csrf_token if session else None,
    }


@app.get("/health")
def health():
    with session_scope() as session:
        document_count = session.query(DocumentRecord).count()
    return {"status": "ok", "documents": document_count}


@app.get("/sources")
def list_sources():
    with session_scope() as session:
        rows = session.execute(
            select(DocumentRecord).where(DocumentRecord.deleted.is_(False)).order_by(DocumentRecord.last_indexed_at.desc())
        ).scalars()
        return [
            {
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


@app.get("/ingest/status")
def ingest_status():
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


@app.post("/ingest/reindex")
def trigger_reindex(payload: ReindexRequest):
    with httpx.Client(timeout=10.0) as client:
        response = client.post(
            f"{settings.ingestion_base_url}/ingest/reindex",
            json=payload.model_dump(),
            headers={"Authorization": f"Bearer {settings.teacher_ingest_token}"},
        )
        response.raise_for_status()
        return response.json()


@app.post("/chat")
def chat(payload: ChatRequest):
    return generate_teaching_response(
        task="answer",
        prompt_input=payload.question,
        level=payload.level,
        top_k=payload.top_k or settings.retrieval_top_k,
    )


@app.post("/quiz")
def quiz(payload: TaskRequest):
    return generate_teaching_response(
        task="quiz",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
    )


@app.post("/summary")
def summary(payload: TaskRequest):
    return generate_teaching_response(
        task="summary",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
    )


@app.post("/flashcards")
def flashcards(payload: TaskRequest):
    return generate_teaching_response(
        task="flashcards",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
    )


@app.post("/voice/transcribe")
def voice_transcribe(audio: UploadFile = File(...)):
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{settings.voice_base_url}/voice/transcribe",
            files={"audio": (audio.filename or "audio.wav", audio.file, audio.content_type or "application/octet-stream")},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/voice/speak")
def voice_speak(payload: TaskRequest):
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{settings.voice_base_url}/voice/speak",
            json={"text": payload.topic},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


def generate_teaching_response(task: str, prompt_input: str, level: str, top_k: int):
    hits = retrieve_sources(prompt_input, top_k=top_k)
    if not hits:
        return {
            "answer": "A megadott könyvtárban nem találtam elég releváns információt a kérdés megbízható megválaszolásához.",
            "sources": [],
        }

    source_lines = []
    for item in hits:
        title = item["title"] or Path(item["source_path"]).name
        locator = item["locator"] or "ismeretlen hely"
        source_lines.append(f"- {title} | {locator} | {item['text']}")
    context = "\n".join(source_lines)[: settings.max_context_chars]
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
        f"Forrásrészletek:\n{context}\n\n"
        f"Felhasználói kérés: {prompt_input}\n\n"
        "Válasz:"
    )
    answer = call_ollama(prompt)
    return {"answer": answer, "sources": hits}


def retrieve_sources(question: str, top_k: int):
    query_vector = embed_query(question)
    results = search(query_vector, limit=top_k)
    return [
        {
            "chunk_id": item.payload.get("chunk_id"),
            "source_path": item.payload.get("source_path"),
            "title": item.payload.get("title"),
            "author": item.payload.get("author"),
            "language": item.payload.get("language"),
            "locator": item.payload.get("locator"),
            "preview": item.payload.get("preview"),
            "text": item.payload.get("text"),
            "score": item.score,
        }
        for item in results
    ]


def call_ollama(prompt: str) -> str:
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": 8192,
        },
    }
    with httpx.Client(timeout=180.0) as client:
        response = client.post(f"{settings.ollama_base_url}/api/generate", json=payload)
        response.raise_for_status()
    data = response.json()
    return data.get("response", "").strip()
