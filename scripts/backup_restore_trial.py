#!/usr/bin/env python3
"""Acceptance trial: restore one SQLite document, a saved quiz and a real Qdrant search result."""

import json
import os
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import backup_restore


def qdrant_request(settings: backup_restore.Settings, method: str, path: str, body: dict | None = None) -> dict:
    if settings.transport == "compose":
        payload = json.dumps(body) if body is not None else "-"
        command = backup_restore._compose_command(settings) + ["api", method, path, payload]
        result = subprocess.run(command, cwd=settings.stack_dir, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"Qdrant {method} {path} failed in the private API container: {result.stderr[-500:]}")
        return json.loads(result.stdout)
    data = json.dumps(body).encode() if body is not None else None
    headers = backup_restore._headers(settings)
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        settings.qdrant_url + path,
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def main() -> int:
    collection = "backup_restore_trial_" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="teacher-backup-restore-trial-") as temporary:
        work = Path(temporary)
        database = work / "metadata" / "teacher.db"
        database.parent.mkdir()
        with sqlite3.connect(database) as db:
            db.executescript(
                """
                CREATE TABLE documents (id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, title TEXT NOT NULL);
                CREATE TABLE quiz_sets (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
                CREATE TABLE auth_sessions (token_hash TEXT PRIMARY KEY);
                INSERT INTO documents VALUES (1, '/library/restore-probe.txt', 'Restore probe');
                INSERT INTO quiz_sets VALUES (1, 'Quiz before restore');
                INSERT INTO auth_sessions VALUES ('session-from-before-backup');
                """
            )

        settings = backup_restore.Settings()
        settings.data_dir = work / "data"
        settings.db_path = database
        settings.destination = work / "backups"
        settings.library = work / "library"
        settings.collection = collection
        settings.transport = os.environ.get("BACKUP_QDRANT_TRANSPORT", "direct")
        settings.qdrant_url = os.environ.get("QDRANT_URL", settings.qdrant_url).rstrip("/")

        qdrant_request(settings, "PUT", f"/collections/{collection}", {"vectors": {"size": 2, "distance": "Cosine"}})
        try:
            qdrant_request(
                settings,
                "PUT",
                f"/collections/{collection}/points?wait=true",
                {
                    "points": [
                        {
                            "id": 1,
                            "vector": [1.0, 0.0],
                            "payload": {
                                "document_id": 1,
                                "source_path": "/library/restore-probe.txt",
                                "workspace_id": "trial",
                                "text": "A searchable restore acceptance document.",
                            },
                        }
                    ]
                },
            )

            backup = backup_restore.create_backup(settings)
            with sqlite3.connect(database) as db:
                db.execute("DELETE FROM documents")
                db.execute("UPDATE quiz_sets SET title = 'Quiz after mutation'")
                db.execute("DELETE FROM auth_sessions")
            qdrant_request(settings, "DELETE", f"/collections/{collection}")

            backup_restore.restore_backup(settings, backup, confirm=True)
            with sqlite3.connect(database) as db:
                document = db.execute("SELECT source_path, title FROM documents WHERE id = 1").fetchone()
                quiz = db.execute("SELECT title FROM quiz_sets WHERE id = 1").fetchone()
                sessions = db.execute("SELECT count(*) FROM auth_sessions").fetchone()[0]
            assert document == ("/library/restore-probe.txt", "Restore probe"), document
            assert quiz == ("Quiz before restore",), quiz
            assert sessions == 0, f"restore should invalidate sessions, found {sessions}"
            results = qdrant_request(
                settings,
                "POST",
                f"/collections/{collection}/points/search",
                {"vector": [1.0, 0.0], "limit": 3, "with_payload": True},
            )["result"]
            assert any(hit["payload"].get("source_path") == document[0] for hit in results), results
            print("PASS: SQLite document and quiz restored; old sessions invalidated; Qdrant search found the document")
            print(f"backup: {backup}")
        finally:
            try:
                qdrant_request(settings, "DELETE", f"/collections/{collection}")
            except (OSError, urllib.error.URLError):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
