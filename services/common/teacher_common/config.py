import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_role: str = Field(default="api", alias="APP_ROLE")
    library_path: str = Field(default="/library", alias="LIBRARY_PATH")
    metadata_db_url: str = Field(
        default="sqlite:////data/metadata/teacher.db",
        alias="METADATA_DB_URL",
    )
    qdrant_url: str = Field(default="http://qdrant:6333", alias="QDRANT_URL")
    qdrant_collection: str = Field(
        default="teacher_documents",
        alias="QDRANT_COLLECTION",
    )
    embedding_model: str = Field(
        default="intfloat/multilingual-e5-base",
        alias="EMBEDDING_MODEL",
    )
    embedding_device: str = Field(default="cpu", alias="EMBEDDING_DEVICE")
    ollama_base_url: str = Field(
        default="http://ollama:11434",
        alias="OLLAMA_BASE_URL",
    )
    ollama_model: str = Field(
        default="qwen2.5:7b-instruct-q4_K_M",
        alias="OLLAMA_MODEL",
    )
    teacher_prompt_path: str = Field(
        default="/app/config/prompts/teacher_hu.md",
        alias="TEACHER_PROMPT_PATH",
    )
    retrieval_top_k: int = Field(default=6, alias="RETRIEVAL_TOP_K")
    max_context_chars: int = Field(default=18000, alias="MAX_CONTEXT_CHARS")
    study_top_k: int = Field(default=10, alias="STUDY_TOP_K")
    study_max_attempts: int = Field(default=2, alias="STUDY_MAX_ATTEMPTS")
    ingest_schedule_seconds: int = Field(default=0, alias="INGEST_SCHEDULE_SECONDS")
    ingest_config_path: str = Field(
        default="/app/config/ingestion/ingestion.yml",
        alias="INGEST_CONFIG_PATH",
    )
    ingest_remove_missing: bool = Field(default=True, alias="INGEST_REMOVE_MISSING")
    ingest_scan_extensions: str = Field(
        default=".pdf,.epub,.txt,.md,.docx",
        alias="INGEST_SCAN_EXTENSIONS",
    )
    ingest_chunk_size: int = Field(default=1200, alias="INGEST_CHUNK_SIZE")
    ingest_chunk_overlap: int = Field(default=200, alias="INGEST_CHUNK_OVERLAP")
    teacher_auth_users: str = Field(default="", alias="TEACHER_AUTH_USERS")
    teacher_auth_users_file: str = Field(default="", alias="TEACHER_AUTH_USERS_FILE")
    teacher_auth_disabled: bool = Field(default=False, alias="TEACHER_AUTH_DISABLED")
    teacher_session_ttl_hours: int = Field(default=720, ge=1, alias="TEACHER_SESSION_TTL_HOURS")
    teacher_cookie_secure: Literal["auto", "true", "false"] = Field(default="auto", alias="TEACHER_COOKIE_SECURE")
    teacher_login_max_attempts: int = Field(default=10, ge=1, alias="TEACHER_LOGIN_MAX_ATTEMPTS")
    teacher_login_window_seconds: int = Field(default=900, ge=1, alias="TEACHER_LOGIN_WINDOW_SECONDS")
    teacher_ingest_token: str = Field(default="", alias="TEACHER_INGEST_TOKEN")
    ingestion_base_url: str = Field(
        default="http://ingestion:8081",
        alias="INGESTION_BASE_URL",
    )
    voice_base_url: str = Field(default="http://voice:8090", alias="VOICE_BASE_URL")
    voice_max_upload_mb: float = Field(default=15, gt=0, alias="VOICE_MAX_UPLOAD_MB")
    voice_timeout_seconds: float = Field(default=300, gt=0, alias="VOICE_TIMEOUT_SECONDS")
    ocr_enabled: bool = Field(default=True, alias="OCR_ENABLED")
    ocr_dpi: int = Field(default=300, alias="OCR_DPI")
    ocr_language: str = Field(default="hun+eng", alias="OCR_LANGUAGE")
    ocr_min_chars: int = Field(default=50, alias="OCR_MIN_CHARS")
    ocr_timeout_seconds: int = Field(default=900, alias="OCR_TIMEOUT_SECONDS")
    ocr_max_pages: int = Field(default=0, alias="OCR_MAX_PAGES")
    ocr_cache_dir: str = Field(default="/data/metadata/ocr_cache", alias="OCR_CACHE_DIR")

    @property
    def scan_extensions(self) -> list[str]:
        return [item.strip().lower() for item in self.ingest_scan_extensions.split(",") if item.strip()]

    @property
    def auth_enabled(self) -> bool:
        return not self.teacher_auth_disabled

    def auth_users(self) -> dict[str, str]:
        """Return username -> password hash from TEACHER_AUTH_USERS and TEACHER_AUTH_USERS_FILE.

        Entries are `username:hash`, separated by newlines or `;`. Lines starting with `#` are ignored.
        """
        raw = self.teacher_auth_users
        if self.teacher_auth_users_file:
            raw += "\n" + Path(self.teacher_auth_users_file).read_text(encoding="utf-8")
        users: dict[str, str] = {}
        for entry in re.split(r"[;\n]", raw):
            entry = entry.strip()
            if not entry or entry.startswith("#"):
                continue
            username, sep, password_hash = entry.partition(":")
            username, password_hash = username.strip(), password_hash.strip()
            if not sep or not USERNAME_PATTERN.fullmatch(username) or not password_hash.startswith("$argon2"):
                raise ValueError(f"Invalid TEACHER_AUTH_USERS entry for user {username!r}: expected username:$argon2id$...")
            users[username] = password_hash
        return users


USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

