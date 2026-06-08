from functools import lru_cache
from typing import List

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
    ingest_schedule_seconds: int = Field(default=0, alias="INGEST_SCHEDULE_SECONDS")
    ingest_remove_missing: bool = Field(default=True, alias="INGEST_REMOVE_MISSING")
    ingest_scan_extensions: str = Field(
        default=".pdf,.epub,.txt,.md,.docx",
        alias="INGEST_SCAN_EXTENSIONS",
    )
    ingest_chunk_size: int = Field(default=1200, alias="INGEST_CHUNK_SIZE")
    ingest_chunk_overlap: int = Field(default=200, alias="INGEST_CHUNK_OVERLAP")
    teacher_auth_username: str = Field(default="", alias="TEACHER_AUTH_USERNAME")
    teacher_auth_password: str = Field(default="", alias="TEACHER_AUTH_PASSWORD")
    ingestion_base_url: str = Field(
        default="http://ingestion:8081",
        alias="INGESTION_BASE_URL",
    )
    voice_base_url: str = Field(default="http://voice:8090", alias="VOICE_BASE_URL")
    voice_stt_backend: str = Field(default="faster-whisper", alias="VOICE_STT_BACKEND")
    voice_tts_backend: str = Field(default="disabled", alias="VOICE_TTS_BACKEND")
    voice_stt_model: str = Field(default="base", alias="VOICE_STT_MODEL")
    voice_stt_device: str = Field(default="auto", alias="VOICE_STT_DEVICE")
    ocr_enabled: bool = Field(default=True, alias="OCR_ENABLED")
    ocr_dpi: int = Field(default=300, alias="OCR_DPI")
    ocr_language: str = Field(default="hun+eng", alias="OCR_LANGUAGE")

    @property
    def scan_extensions(self) -> List[str]:
        return [item.strip().lower() for item in self.ingest_scan_extensions.split(",") if item.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.teacher_auth_username and self.teacher_auth_password)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

