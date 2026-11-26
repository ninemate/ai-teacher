"""Try the Meetingek panel without the voice service or a model: the real API with scripted STT and model answers.

Auth is off and the database is temporary. Real ffmpeg decoding is used where ffmpeg is installed; every
45-second chunk "hears" one of a few scripted lines of a staff meeting, whatever the recording contains.
Usage: python scripts/meetings_demo.py [port]   (then open http://127.0.0.1:8771/#meetings)
"""

import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8771
DATA = tempfile.mkdtemp(prefix="meetings-demo-")
os.environ.update({
    "METADATA_DB_URL": f"sqlite:///{DATA}/teacher.db", "INGEST_CONFIG_PATH": f"{DATA}/none.yml",
    "MEETINGS_UPLOAD_DIR": f"{DATA}/uploads", "LISTEN_CACHE_DIR": f"{DATA}/listen",
    "TEACHER_AUTH_DISABLED": "true", "TEACHER_AUTH_USERS": "", "TEACHER_AUTH_USERS_FILE": "",
    "TEACHER_COOKIE_SECURE": "false", "OLLAMA_BASE_URL": "http://127.0.0.1:9",
    "TEACHER_PROMPT_PATH": str(ROOT / "config" / "prompts" / "teacher_hu.md"),
})
sys.path[:0] = [str(ROOT / "services" / "common"), str(ROOT / "services" / "api" / "app")]

LINES = [
    ["Jó reggelt, kezdjük a heti egyeztetést.", "Az első napirendi pont a tavaszi vizsgaidőszak beosztása.",
     "Megállapodtunk, hogy a szóbeli vizsgák május huszadikán kezdődnek.",
     "Valakinek el kell küldenie a beosztást a diákoknak péntekig."],
    ["A második pont a laborfelszerelés cseréje.",
     "Még nem tudjuk, hogy a költségvetés fedezi-e az új mikroszkópokat.",
     "Kérjünk árajánlatot legalább két beszállítótól.", "Rendben, akkor a következő megbeszélésen visszatérünk rá."],
    ["Egyéb ügyek: a szülői értekezlet időpontja.", "Javaslom a csütörtök délutánt.",
     "Elfogadva, csütörtök öt óra.", "Köszönöm mindenkinek."],
]
NOTES = [  # (keyword in a transcript line, section, note)
    ("vizsgaidőszak", "summary", "A tavaszi vizsgaidőszak beosztását egyeztették."),
    ("labor", "summary", "Szóba került a laborfelszerelés cseréje."),
    ("május", "decisions", "A szóbeli vizsgák május 20-án kezdődnek."),
    ("csütörtök", "decisions", "A szülői értekezlet csütörtökön 17 órakor lesz."),
    ("mikroszkóp", "questions", "Fedezi-e a költségvetés az új mikroszkópokat?"),
    ("beosztást", "actions", "A vizsgabeosztás elküldése a diákoknak péntekig."),
    ("árajánlat", "actions", "Árajánlat kérése legalább két beszállítótól."),
]


def _noop(*args, **kwargs):
    return None


def _stub(name: str, **attrs) -> None:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


def transcribe(wav: bytes, calls: list = []) -> dict:  # noqa: B006 - counts chunks across calls
    calls.append(1)
    lines = LINES[(len(calls) - 1) % len(LINES)]
    return {"segments": [{"start": 1 + i * 10.5, "end": 9.5 + i * 10.5, "text": t} for i, t in enumerate(lines)]}


def llm(prompt: str, json_mode: bool) -> str:
    lines = {int(n): text for n, text in re.findall(r"^\[(\d+)\] \S+ (.*)$", prompt, re.MULTILINE)}
    notes: dict[str, list] = {"summary": [], "decisions": [], "questions": [], "actions": []}
    for keyword, section, text in NOTES:
        ids = [n for n, line in lines.items() if keyword in line][:2]
        if ids:
            notes[section].append({"text": text, "segments": ids})
    return json.dumps(notes, ensure_ascii=False)


def main() -> None:
    _stub("teacher_common.embeddings", embed_query=lambda t: [0.0], embed_passages=lambda t: [],
          warmup_embeddings=_noop, embedding_dimension=lambda: 1)
    _stub("teacher_common.qdrant_store", ensure_collection=_noop, search=lambda *a, **k: [], build_points=_noop,
          delete_points_for_source=_noop, delete_points_for_workspace=_noop, recreate_collection=_noop,
          upsert_points=_noop, scroll_document=lambda *a, **k: [])
    _stub("teacher_common.documents", chunk_segments=_noop, hash_file=_noop, iter_library_files=lambda *a: [],
          parse_document=_noop)
    import uvicorn

    api = __import__("main")
    api.meeting_worker.voice_status = lambda: {"stt": True, "max_seconds": 120}
    api.meeting_worker.transcribe = transcribe
    api.meeting_worker.llm = llm
    uvicorn.run(api.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
