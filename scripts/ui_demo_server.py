"""Run the web UI without Qdrant, Ollama or login, to look at it or take screenshots: python scripts/ui_demo_server.py (from the repo root)."""
import os, sys, tempfile, types
d = tempfile.mkdtemp(prefix="ui-demo-")
os.environ.update({"METADATA_DB_URL": f"sqlite:///{d}/t.db", "INGEST_CONFIG_PATH": f"{d}/n.yml", "TEACHER_AUTH_DISABLED": "true",
  "TEACHER_AUTH_USERS": "", "TEACHER_AUTH_USERS_FILE": "", "TEACHER_INGEST_TOKEN": "x",
  "TEACHER_PROMPT_PATH": os.path.abspath("config/prompts/teacher_hu.md")})
sys.path[:0] = [os.path.abspath("services/common"), os.path.abspath("services/api/app")]
def stub(n, **a):
    m = types.ModuleType(n); m.__dict__.update(a); sys.modules[n] = m
nop = lambda *a, **k: None
stub("teacher_common.embeddings", embed_query=lambda t: [0.0], embed_passages=lambda t: [], warmup_embeddings=nop, embedding_dimension=lambda: 1)
stub("teacher_common.qdrant_store", ensure_collection=nop, search=lambda *a, **k: [], build_points=nop, delete_points_for_source=nop,
     delete_points_for_workspace=nop, recreate_collection=nop, upsert_points=nop, scroll_document=lambda *a, **k: [])
stub("teacher_common.documents", chunk_segments=nop, hash_file=nop, iter_library_files=lambda *a: [], parse_document=nop)
from teacher_common.db import init_db
init_db()
import uvicorn, main
uvicorn.run(main.app, host="127.0.0.1", port=8765)
