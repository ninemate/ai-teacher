"""Test setup: configure the services through env vars and stub out the ML / vector store modules.

The API and ingestion services are loaded from their source files the way the containers run them
(`main.py` next to `teacher_common`), without needing Qdrant, Ollama or the embedding model.
"""

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
PASSWORDS = {"apa": "helyes-jelszo-123", "anya": "masik-jelszo-456"}
INGEST_TOKEN = "test-ingest-token"

_hasher = PasswordHasher()
# Must be set before teacher_common.config is first imported (settings are cached).
_db_dir = tempfile.mkdtemp(prefix="teacher-tests-")
os.environ.update(
    {
        "METADATA_DB_URL": f"sqlite:///{_db_dir}/teacher.db",
        "INGEST_CONFIG_PATH": f"{_db_dir}/none.yml",
        "TEACHER_AUTH_USERS": ";".join(f"{name}:{_hasher.hash(pw)}" for name, pw in PASSWORDS.items()),
        "TEACHER_AUTH_USERS_FILE": "",
        "TEACHER_AUTH_ADMINS": "",
        "TEACHER_AUTH_DISABLED": "false",
        "TEACHER_COOKIE_SECURE": "auto",
        "TEACHER_INGEST_TOKEN": INGEST_TOKEN,
        "TEACHER_PROMPT_PATH": str(ROOT / "config" / "prompts" / "teacher_hu.md"),
    }
)
sys.path[:0] = [str(ROOT / "services" / "common"), str(ROOT / "services" / "api" / "app")]


def _stub(name: str, **attrs) -> None:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


def _noop(*args, **kwargs):
    return None


_stub("teacher_common.embeddings", embed_query=lambda text: [0.0], embed_passages=lambda texts: [],
      warmup_embeddings=_noop, embedding_dimension=lambda: 1)
_stub("teacher_common.qdrant_store", ensure_collection=_noop,
      search=lambda vector, limit, workspace_ids, source_paths=None: [], build_points=_noop,
      delete_points_for_source=_noop, delete_points_for_workspace=_noop, recreate_collection=_noop, upsert_points=_noop)
_stub("teacher_common.documents", chunk_segments=_noop, hash_file=_noop, iter_library_files=lambda *a: [],
      parse_document=_noop)


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def api_main():
    from teacher_common.db import init_db

    init_db()
    return _load("api_main", ROOT / "services" / "api" / "app" / "main.py")


@pytest.fixture(scope="session")
def ingestion_main():
    from teacher_common.db import init_db

    init_db()
    return _load("ingestion_main", ROOT / "services" / "ingestion" / "app" / "main.py")


@pytest.fixture
def session():
    from teacher_common import models, study_models  # noqa: F401
    from teacher_common.db import Base

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        yield s
