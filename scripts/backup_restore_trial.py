#!/usr/bin/env python3
"""Acceptance trial: restore one SQLite document, a saved quiz and a real Qdrant search result."""

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import backup_restore

ORM_WORKER = r"""
import json, os, sys
for name, value in list(os.environ.items()):
    if value == '': os.environ.pop(name)
from sqlalchemy import select, func
from teacher_common.db import SessionLocal, init_db
from teacher_common.models import AuthSession, DocumentRecord, Workspace
from teacher_common.study_models import QuizSet

action = sys.argv[1]
init_db()
with SessionLocal.begin() as session:
    if action == 'seed':
        session.add(Workspace(id='restore-trial', name='Restore trial', owner_username='trial'))
        session.flush()
        document = DocumentRecord(
            workspace_id='restore-trial', source_path='/library/restore-probe.txt',
            source_hash='a' * 64, file_type='txt', size_bytes=22, modified_timestamp=1,
            title='Restore probe', status='indexed', chunk_count=1,
        )
        quiz = QuizSet(workspace_id='restore-trial', title='Quiz before restore', topic='restore trial')
        session.add_all([document, quiz, AuthSession(
            token_hash='b' * 64, username='trial', csrf_token='c' * 64,
            credential_tag='d' * 64, created_at=1, expires_at=2,
        )])
        session.flush()
        print(json.dumps({'document_id': document.id, 'quiz_id': quiz.id}))
    elif action == 'mutate':
        document = session.scalar(select(DocumentRecord).where(DocumentRecord.source_path == '/library/restore-probe.txt'))
        quiz = session.scalar(select(QuizSet).where(QuizSet.workspace_id == 'restore-trial'))
        if document is None or quiz is None:
            raise RuntimeError('trial ORM rows are missing before mutation')
        session.delete(document)
        quiz.title = 'Quiz after mutation'
        session.query(AuthSession).delete(synchronize_session=False)
        print(json.dumps({'ok': True}))
    elif action == 'verify':
        document = session.scalar(select(DocumentRecord).where(DocumentRecord.source_path == '/library/restore-probe.txt'))
        quiz = session.scalar(select(QuizSet).where(QuizSet.workspace_id == 'restore-trial'))
        sessions = session.scalar(select(func.count()).select_from(AuthSession))
        print(json.dumps({
            'document': None if document is None else {
                'id': document.id, 'title': document.title, 'source_path': document.source_path,
            },
            'quiz_title': None if quiz is None else quiz.title,
            'sessions': sessions,
        }))
    else:
        raise SystemExit('unknown ORM trial action')
"""


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


def orm_action(settings: backup_restore.Settings, metadata_dir: Path, action: str) -> dict:
    if settings.transport == "compose":
        command = backup_restore._compose_command(
            settings,
            extra_env=["METADATA_DB_URL=sqlite:////backup-trial-metadata/teacher.db"],
            extra_volumes=[f"{metadata_dir.resolve()}:/backup-trial-metadata"],
            worker_code=ORM_WORKER,
        ) + [action]
        result = subprocess.run(command, cwd=settings.stack_dir, capture_output=True, text=True, timeout=120)
    else:
        environment = os.environ.copy()
        common = str(backup_restore.ROOT / "services" / "common")
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (common, environment.get("PYTHONPATH", ""))))
        result = subprocess.run(
            [sys.executable, "-c", ORM_WORKER, action],
            cwd=backup_restore.ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode:
        raise RuntimeError(f"application ORM trial action {action!r} failed: {result.stderr[-1000:]}")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"application ORM trial action {action!r} returned invalid output") from exc


def main() -> int:
    collection = "backup_restore_trial_" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="teacher-backup-restore-trial-") as temporary:
        work = Path(temporary)
        data_dir = work / "data"
        metadata_dir = data_dir / "metadata"
        metadata_dir.mkdir(parents=True)
        database = metadata_dir / "teacher.db"
        os.environ.update(
            {
                "BACKUP_DATABASE_PATH": "",
                "LIBRARY_PATH": str(work / "library"),
                "METADATA_DB_URL": f"sqlite:///{database}",
                "QDRANT_COLLECTION": collection,
                "TEACHER_BACKUP_DIR": str(work / "backups"),
                "TEACHER_DATA_DIR": str(data_dir),
                "TEACHER_STACK_DIR": str(backup_restore.ROOT),
            }
        )
        settings = backup_restore.Settings()
        if settings.db_path != database.resolve():
            raise RuntimeError(f"configured database path did not resolve to the isolated trial DB: {settings.db_path}")
        seeded = orm_action(settings, metadata_dir, "seed")
        document_id = seeded["document_id"]

        qdrant_request(settings, "PUT", f"/collections/{collection}", {"vectors": {"size": 2, "distance": "Cosine"}})
        try:
            qdrant_request(
                settings,
                "PUT",
                f"/collections/{collection}/points?wait=true",
                {
                    "points": [
                        {
                            "id": document_id,
                            "vector": [1.0, 0.0],
                            "payload": {
                                "document_id": document_id,
                                "source_path": "/library/restore-probe.txt",
                                "workspace_id": "trial",
                                "text": "A searchable restore acceptance document.",
                            },
                        }
                    ]
                },
            )

            backup = backup_restore.create_backup(settings)
            orm_action(settings, metadata_dir, "mutate")
            qdrant_request(settings, "DELETE", f"/collections/{collection}")

            backup_restore.restore_backup(settings, backup, confirm=True)
            state = orm_action(settings, metadata_dir, "verify")
            document = state["document"]
            assert document == {
                "id": document_id,
                "title": "Restore probe",
                "source_path": "/library/restore-probe.txt",
            }, document
            assert state["quiz_title"] == "Quiz before restore", state
            assert state["sessions"] == 0, f"restore should invalidate sessions, found {state['sessions']}"
            results = qdrant_request(
                settings,
                "POST",
                f"/collections/{collection}/points/search",
                {"vector": [1.0, 0.0], "limit": 3, "with_payload": True},
            )["result"]
            assert any(hit["payload"].get("source_path") == document["source_path"] for hit in results), results
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
