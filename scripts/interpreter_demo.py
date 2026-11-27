#!/usr/bin/env python3
"""Run the web app with fake speech and translation stages, to look at the Tolmács UI without models.

    python3 scripts/interpreter_demo.py [port]

Needs no Qdrant, model runtime or voice service; login is switched off and data goes to a temporary folder.
The "translations" are canned and the latency figures are made up: this is for the layout, not for measuring.
"""

import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
tmp = tempfile.mkdtemp(prefix="tolmacs-demo-")
os.environ.update(
    METADATA_DB_URL=f"sqlite:///{tmp}/teacher.db",
    INTERPRETER_LATENCY_LOG=f"{tmp}/lat.jsonl",
    INTERPRETER_CONFIG_PATH=str(ROOT / "config/models/interpreter.json"),
    TEACHER_AUTH_DISABLED="true",
    TEACHER_PROMPT_PATH=str(ROOT / "config/prompts/teacher_hu.md"),
    INGEST_CONFIG_PATH=f"{tmp}/none.yml",
    LISTEN_CACHE_DIR=f"{tmp}/listen",
    VOICE_BASE_URL="",
)
sys.path[:0] = [str(ROOT / "services/common"), str(ROOT / "services/api/app")]


def stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


noop = lambda *a, **k: None  # noqa: E731
stub("teacher_common.embeddings", embed_query=lambda t: [0.0], embed_passages=lambda t: [], warmup_embeddings=noop,
     embedding_dimension=lambda: 1)
stub("teacher_common.qdrant_store", ensure_collection=noop, search=lambda *a, **k: [], build_points=noop,
     delete_points_for_source=noop, delete_points_for_workspace=noop, recreate_collection=noop, upsert_points=noop,
     scroll_document=lambda *a, **k: [])
stub("teacher_common.documents", chunk_segments=noop, hash_file=noop, iter_library_files=lambda *a: [],
     parse_document=noop)

from teacher_common.db import init_db  # noqa: E402

init_db()
import main  # noqa: E402
import uvicorn  # noqa: E402

PAIRS = {"hu": ("Jó napot kívánok, hol találom a recepciót?", "Good afternoon, where can I find reception?"),
         "en": ("The meeting starts at nine.", "A megbeszélés kilenckor kezdődik.")}


def transcribe(content, filename, content_type, language, beam):
    time.sleep(0.2)
    return {"text": PAIRS["hu" if language in ("hu", "auto") else "en"][0], "language": "hu" if language != "en" else "en"}


def translate(prompt, model, options):
    time.sleep(0.15)
    return PAIRS["hu"][1] if "magyar szöveget angolra" in prompt or "magyar mondatot angolra" in prompt else PAIRS["en"][1]


main.interpreter_engine._transcribe = transcribe
main.interpreter_engine._translate = translate
main.interpreter_engine._speak = lambda text, voice: (time.sleep(0.1), b"ID3")[1]
main.interpreter_engine._available_voices = lambda: ["hu_HU-imre-medium", "en_US-lessac-medium"]
main.voice_status = main.voice_status  # the page asks /voice/status; with no voice service it says unavailable

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
