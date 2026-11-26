"""Process meeting jobs serially, yielding the language model to interactive requests."""

import logging
import math
import threading
import wave
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import httpx
from sqlalchemy import select

from teacher_common import meeting_notes, meeting_store
from teacher_common.citations import Limits
from teacher_common.config import Settings
from teacher_common.db import session_scope
from teacher_common.docintel import JobCancelled, JobFailed, OutputError, Runner
from teacher_common.meeting_audio import MAX_SECONDS, UNREADABLE, AudioChunk, AudioDecoder
from teacher_common.meeting_models import MeetingJob

logger = logging.getLogger("teacher.meetings")


def voice_failure(detail: object) -> JobFailed:
    """A voice-service error as the job's reason, so the person sees why instead of a generic failure."""
    reason = detail if isinstance(detail, str) and detail else "A hangszolgáltatás hibát jelzett."
    return JobFailed(f"Beszédfelismerés: {reason}")


class MeetingWorker:
    def __init__(
        self, settings: Settings, *, voice_status: Callable[[], dict],
        transcribe: Callable[[bytes], dict], llm: Callable[[str, bool], str],
    ) -> None:
        self.settings = settings
        self.voice_status = voice_status
        self.transcribe = transcribe
        self.llm = llm
        self.decoder = AudioDecoder()
        self.directory = Path(settings.meetings_upload_dir)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def source_path(self, job_id: str) -> Path:
        return self.directory / job_id

    def discard_source(self, job_id: str) -> None:
        try:
            self.source_path(job_id).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove meeting upload %s", job_id)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with session_scope() as session:
            meeting_store.recover_after_restart(session)
            queued = set(session.scalars(select(MeetingJob.id).where(MeetingJob.status == "queued")))
        # The API has one worker process. Requests cannot stage uploads until startup completes.
        if self.directory.exists():
            for source in self.directory.iterdir():
                if len(source.name) == 32 and all(char in "0123456789abcdef" for char in source.name):
                    if source.name not in queued:
                        self.discard_source(source.name)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="meeting-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_next()
            except Exception:  # noqa: BLE001 - one database error must not kill the queue
                logger.exception("Meeting worker error")
                worked = False
            if not worked:
                self._wake.wait(timeout=5)
                self._wake.clear()

    def run_next(self) -> bool:
        with session_scope() as session:
            job_id = meeting_store.claim_next(session)
        if job_id is None:
            return False

        def check_cancel() -> None:
            with session_scope() as session:
                status = session.scalar(select(MeetingJob.status).where(MeetingJob.id == job_id))
            if status != "running":
                raise JobCancelled

        def progress(done: int, total: int | None, phase: str, **values) -> None:
            with session_scope() as session:
                meeting_store.progress(session, job_id, done, total, phase, **values)

        def call_model(prompt: str, json_mode: bool) -> str:
            check_cancel()
            return self.llm(prompt, json_mode)

        notes = None
        error = None
        status = "failed"
        try:
            source = self.source_path(job_id)
            if not source.is_file():
                raise JobFailed("A feltöltött fájl már nem érhető el. Töltsd fel újra.")
            voice = self.voice_status()
            if not voice.get("stt"):
                raise JobFailed("A beszédfelismerés nem érhető el.")
            duration = self.decoder.duration(source)
            if duration and duration > MAX_SECONDS + 0.1:
                raise JobFailed("A felvétel legfeljebb 4 órás lehet.")
            total = round(duration) if duration else None
            progress(0, total, "transcribing")
            segments: list[dict] = []
            seconds = min(45.0, float(voice.get("max_seconds") or 45.0))
            with closing(self.decoder.chunks(source, seconds, check_cancel)) as chunks:
                for chunk in chunks:
                    check_cancel()
                    self._append_segments(segments, chunk)
                    done = round(chunk.offset + chunk.duration)
                    progress(done, max(total, done) if total else None, "transcribing")
            check_cancel()
            progress(0, None, "summarizing", transcript=segments)
            prompt_dir = Path(self.settings.teacher_prompt_path).parent
            runner = Runner(
                llm=call_model,
                prompts=lambda name: (prompt_dir / f"meeting_{name}_hu.md").read_text(encoding="utf-8"),
                limits=Limits(0, 0), window_chars=self.settings.docintel_window, translate_chars=0,
                max_attempts=self.settings.docintel_max_attempts, on_progress=progress,
            )
            notes = meeting_notes.summarize(segments, runner)
            check_cancel()
            status = "succeeded"
        except JobCancelled:
            status = "cancelled"
        except JobFailed as exc:
            error = str(exc)
        except (EOFError, wave.Error):
            error = UNREADABLE
        except (httpx.HTTPError, OutputError):
            error = "A nyelvi modell nem tudta elkészíteni a jegyzeteket. A leirat letölthető."
        except Exception:  # noqa: BLE001 - no uploaded text or model output in logs
            logger.error("Unexpected error processing meeting %s", job_id)
            error = "Váratlan hiba történt a feldolgozás közben."
        finally:
            with session_scope() as session:
                meeting_store.finish(session, job_id, status, error, notes)
            self.discard_source(job_id)
        return True

    def _append_segments(self, segments: list[dict], chunk: AudioChunk) -> None:
        result = self.transcribe(chunk.wav)
        timed = result.get("segments")
        if not isinstance(timed, list):
            raise JobFailed("A hangszolgáltatás nem ad időbélyegeket. Frissítsd a hangszolgáltatást.")
        items = []
        for entry in timed:
            try:
                start, end = float(entry["start"]), float(entry["end"])
                text = " ".join(entry["text"].split())
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                raise JobFailed("Hibás időbélyeg érkezett a hangszolgáltatástól.") from exc
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                raise JobFailed("Hibás időbélyeg érkezett a hangszolgáltatástól.")
            start = round(chunk.offset + start, 3)
            end = round(chunk.offset + min(end, chunk.duration), 3)
            if text and end > start:
                items.append({"start": start, "end": end, "text": text, "speaker": None})
        for item in sorted(items, key=lambda item: item["start"]):
            segments.append({"id": len(segments) + 1, **item})
