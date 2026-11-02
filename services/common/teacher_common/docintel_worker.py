"""Runs queued Document Intelligence jobs one at a time on a background thread.

One model call at a time is the default: a second job waits in the queue, and chat requests go first (a job waits
before each model call while a chat request is in flight, see ``ChatPriority``). Progress, checkpoints and the
result are stored per step, so a restart leaves the job interrupted and resumable instead of lost.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from sqlalchemy import select

from teacher_common import citations, doc_text, docintel, docintel_store
from teacher_common.config import Settings
from teacher_common.db import session_scope
from teacher_common.models import DocumentRecord

logger = logging.getLogger("teacher.docintel")


class ChatPriority:
    """Lets interactive requests go before background jobs: a job waits for the model while one is running."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0

    @contextmanager
    def interactive(self) -> Iterator[None]:
        with self._lock:
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1

    def wait_idle(self, should_stop: Callable[[], bool] = lambda: False, poll: float = 0.5) -> None:
        while self._active and not should_stop():
            time.sleep(poll)


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        llm: Callable[[str, bool], str],
        scroll: Callable[[str, set[str]], list[dict]],
        workspace_ids_for: Callable[[str], set[str]],
        prompt_dir: Path | str,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.scroll = scroll
        self.workspace_ids_for = workspace_ids_for
        self.prompt_dir = Path(prompt_dir)
        self.current_job_id: int | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- thread ---

    def start(self) -> None:
        """Mark jobs a dead process left running as interrupted, then start the loop."""
        with session_scope() as session:
            interrupted = docintel_store.recover_after_restart(session)
        if interrupted:
            logger.warning("%d Document Intelligence job(s) were interrupted by a restart; they can be resumed.", interrupted)
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="docintel-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_next()
            except Exception:  # noqa: BLE001 - the loop must survive anything a job or the database throws
                logger.exception("Document Intelligence worker error")
                worked = False
            if not worked:
                self._wake.wait(timeout=5)
                self._wake.clear()

    # --- one job ---

    def run_pending(self) -> int:
        """Run every queued job now, in this thread (tests, and the way a CLI could drain the queue)."""
        count = 0
        while self.run_next():
            count += 1
        return count

    def run_next(self) -> bool:
        with session_scope() as session:
            job = docintel_store.claim_next(session)
        if job is None:
            return False
        self.current_job_id = job["id"]
        try:
            self._run(job)
        finally:
            self.current_job_id = None
        return True

    def _documents(self, job: dict) -> list[dict]:
        """The job's documents with their chunks in reading order; refuses what changed or is no longer allowed."""
        workspace_ids = self.workspace_ids_for(job["username"])
        documents = []
        for source in job["params"]["sources"]:
            with session_scope() as session:
                record = session.execute(
                    select(DocumentRecord).where(DocumentRecord.source_path == source["source_path"])
                ).scalar_one_or_none()
                if record is None or record.deleted or record.workspace_id not in workspace_ids:
                    raise docintel.JobFailed(f"A dokumentum már nem érhető el: {source.get('file_name')}.")
                if source.get("source_hash") and record.source_hash != source["source_hash"]:
                    raise docintel.JobFailed(
                        f"A dokumentum megváltozott a feladat indítása óta ({source.get('file_name')}); indítsd újra."
                    )
                document_workspace = record.workspace_id
            chunks = doc_text.load_chunks(source["source_path"], {document_workspace}, self.scroll)
            documents.append({**source, "chunks": chunks})
        return documents

    def _run(self, job: dict) -> None:
        job_id = job["id"]
        settings = self.settings

        def prompts(name: str) -> str:
            return docintel.load_prompt(self.prompt_dir, name)

        def progress(done: int, total: int, phase: str) -> None:
            with session_scope() as session:
                docintel_store.set_progress(session, job_id, done, total, phase)

        def cancelled() -> bool:
            with session_scope() as session:
                return docintel_store.cancel_requested(session, job_id)

        def save(key: str, value) -> None:
            with session_scope() as session:
                docintel_store.save_part(session, job_id, key, value)

        with session_scope() as session:
            parts = docintel_store.load_parts(session, job_id)
        runner = docintel.Runner(
            llm=self.llm,
            prompts=prompts,
            limits=citations.Limits(weak_score=settings.retrieval_weak_score, ocr_min_confidence=settings.ocr_low_confidence),
            window_chars=settings.docintel_window,
            translate_chars=settings.docintel_translate_window,
            max_attempts=settings.docintel_max_attempts,
            parts=parts,
            save_part=save,
            on_progress=progress,
            should_cancel=cancelled,
        )
        try:
            documents = self._documents(job)
            content = docintel.run_job(job["kind"], runner, documents, job["params"].get("options", {}))
        except docintel.JobCancelled:
            self._finish(job_id, "cancelled", "A feladatot leállították.")
        except docintel.JobFailed as exc:
            self._finish(job_id, "failed", str(exc))
        except httpx.HTTPError as exc:
            logger.warning("Document Intelligence job %s: model call failed: %r", job_id, exc)
            self._finish(job_id, "failed", "A nyelvi modell nem érhető el vagy nem válaszolt időben. Újrapróbálható.")
        except Exception:  # noqa: BLE001 - a bug in one job must not kill the worker; the job ends as failed
            logger.exception("Document Intelligence job %s failed", job_id)
            self._finish(job_id, "failed", "Váratlan hiba történt a feldolgozás közben. Újrapróbálható.")
        else:
            self._finish(job_id, "succeeded", None, content)

    def _finish(self, job_id: int, status: str, error: str | None, content: dict | None = None) -> None:
        with session_scope() as session:
            docintel_store.finish(session, job_id, status, error, content)
