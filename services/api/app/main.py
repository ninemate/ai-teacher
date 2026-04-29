import secrets
from pathlib import Path
from typing import Literal, Optional

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select

from teacher_common.config import get_settings
from teacher_common.db import init_db, session_scope
from teacher_common.embeddings import embed_query
from teacher_common.models import DocumentRecord, IngestRun
from teacher_common.qdrant_store import ensure_collection, search


settings = get_settings()
security = HTTPBasic(auto_error=False)
app = FastAPI(title="teacher-agent-api", version="0.1.0")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")
templates = Jinja2Templates(directory="/app/templates")


class ChatRequest(BaseModel):
    question: str = Field(min_length=3)
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    top_k: Optional[int] = None


class TaskRequest(BaseModel):
    topic: str = Field(min_length=3)
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"


class ReindexRequest(BaseModel):
    full_reindex: bool = False
    remove_missing: bool = True


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not settings.auth_enabled:
        return
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    username_ok = secrets.compare_digest(credentials.username, settings.teacher_auth_username)
    password_ok = secrets.compare_digest(credentials.password, settings.teacher_auth_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.on_event("startup")
def startup_event() -> None:
    Path("/data/metadata").mkdir(parents=True, exist_ok=True)
    init_db()
    ensure_collection()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _: None = Depends(require_auth)):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "auth_enabled": settings.auth_enabled,
            "default_level": "beginner",
        },
    )


@app.get("/health")
def health():
    with session_scope() as session:
        document_count = session.query(DocumentRecord).count()
    return {"status": "ok", "documents": document_count}


@app.get("/sources")
def list_sources(_: None = Depends(require_auth)):
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
def ingest_status(_: None = Depends(require_auth)):
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
def trigger_reindex(payload: ReindexRequest, _: None = Depends(require_auth)):
    with httpx.Client(timeout=10.0) as client:
        response = client.post(
            f"{settings.ingestion_base_url}/ingest/reindex",
            json=payload.model_dump(),
        )
        response.raise_for_status()
        return response.json()


@app.post("/chat")
def chat(payload: ChatRequest, _: None = Depends(require_auth)):
    return generate_teaching_response(
        task="answer",
        prompt_input=payload.question,
        level=payload.level,
        top_k=payload.top_k or settings.retrieval_top_k,
    )


@app.post("/quiz")
def quiz(payload: TaskRequest, _: None = Depends(require_auth)):
    return generate_teaching_response(
        task="quiz",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
    )


@app.post("/summary")
def summary(payload: TaskRequest, _: None = Depends(require_auth)):
    return generate_teaching_response(
        task="summary",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
    )


@app.post("/flashcards")
def flashcards(payload: TaskRequest, _: None = Depends(require_auth)):
    return generate_teaching_response(
        task="flashcards",
        prompt_input=payload.topic,
        level=payload.level,
        top_k=settings.retrieval_top_k,
    )


@app.post("/voice/transcribe")
def voice_transcribe(_: None = Depends(require_auth), audio: UploadFile = File(...)):
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{settings.voice_base_url}/voice/transcribe",
            files={"audio": (audio.filename or "audio.wav", audio.file, audio.content_type or "application/octet-stream")},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.post("/voice/speak")
def voice_speak(payload: TaskRequest, _: None = Depends(require_auth)):
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
