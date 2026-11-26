"""Hungarian speech-to-text with faster-whisper (CTranslate2).

The model is loaded on first use and kept; with `stt_idle_unload_seconds` it is dropped again after a quiet
period, which hands GPU memory back to Ollama when Whisper runs on the shared GPU.
"""

import gc
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voice_settings import VoiceSettings

logger = logging.getLogger("uvicorn")

ModelFactory = Callable[[str, str, str, int], Any]


class AudioTooLong(ValueError):
    pass


class AudioUnreadable(ValueError):
    pass


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    duration: float
    elapsed: float
    device: str
    segments: tuple[TranscriptSegment, ...] = ()
    acoustics: dict | None = None


def _whisper_model(model_name: str, device: str, compute_type: str, cpu_threads: int) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(model_name, device=device, compute_type=compute_type, cpu_threads=cpu_threads)


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - no CUDA runtime at all means no GPU
        return False


class Transcriber:
    def __init__(
        self,
        settings: VoiceSettings,
        model_factory: ModelFactory = _whisper_model,
        cuda_available: Callable[[], bool] = _cuda_available,
    ) -> None:
        self.settings = settings
        self._model_factory = model_factory
        self._cuda_available = cuda_available
        self._model: Any = None
        self._device = ""
        # One transcription at a time: a second parallel run would double RAM/VRAM for little gain.
        self._lock = threading.Lock()
        self._unload_timer: threading.Timer | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def device(self) -> str:
        return self._device

    def _compute_type(self, device: str) -> str:
        if self.settings.stt_compute_type != "auto":
            return self.settings.stt_compute_type
        return "int8_float16" if device == "cuda" else "int8"

    def _load(self) -> None:
        wanted = self.settings.stt_device
        if wanted == "auto":
            devices = ["cuda", "cpu"] if self._cuda_available() else ["cpu"]
        else:
            devices = [wanted]
        for index, device in enumerate(devices):
            try:
                started = time.perf_counter()
                self._model = self._model_factory(
                    self.settings.stt_model, device, self._compute_type(device), self.settings.stt_cpu_threads
                )
                self._device = device
                logger.info(
                    "Loaded STT model %s on %s (%s) in %.1fs",
                    self.settings.stt_model,
                    device,
                    self._compute_type(device),
                    time.perf_counter() - started,
                )
                return
            except Exception as exc:
                if index == len(devices) - 1:
                    raise
                logger.warning("Loading STT model on %s failed, falling back to CPU: %s", device, exc)

    def transcribe(self, audio_path: Path, language: str = "hu", measure: bool = False) -> Transcript:
        with self._lock:
            if self._model is None:
                self._load()
            started = time.perf_counter()
            try:
                segments, info = self._model.transcribe(
                    str(audio_path),
                    language=language,
                    word_timestamps=measure,
                    task="transcribe",
                    beam_size=self.settings.stt_beam_size,
                    vad_filter=True,
                    condition_on_previous_text=False,
                )
            except ValueError as exc:
                # PyAV's InvalidDataError (a ValueError) for files it cannot decode, empty uploads included.
                raise AudioUnreadable(str(exc)) from exc
            if info.duration > self.settings.stt_max_seconds:
                raise AudioTooLong(f"{info.duration:.0f}s")
            # Segments are produced lazily: the actual decoding happens here, once for every response shape.
            decoded = list(segments)
            timed = tuple(
                TranscriptSegment(float(segment.start), float(segment.end), segment.text.strip()) for segment in decoded
            )
            text = " ".join(segment.text for segment in timed).strip()
            acoustics = None
            if measure:
                words = [word for segment in decoded for word in (segment.words or [])]
                acoustics = {"duration": round(info.duration, 2)}
                # Pace over the speech itself: leading waits and the trailing end-of-speech silence don't count.
                span = words[-1].end - words[0].start if words else 0
                if span > 0:
                    acoustics.update({
                        "words_per_minute": round(60 * len(words) / span),
                        "pause_count": sum(b.start - a.end >= 0.5 for a, b in zip(words, words[1:], strict=False)),
                        "mean_word_probability": round(sum(word.probability for word in words) / len(words), 3),
                    })
            elapsed = time.perf_counter() - started
        self._schedule_unload()
        return Transcript(
            text=text, duration=float(info.duration), elapsed=elapsed, device=self._device,
            segments=timed, acoustics=acoustics,
        )

    def _schedule_unload(self) -> None:
        if self.settings.stt_idle_unload_seconds <= 0:
            return
        if self._unload_timer is not None:
            self._unload_timer.cancel()
        self._unload_timer = threading.Timer(self.settings.stt_idle_unload_seconds, self.unload)
        self._unload_timer.daemon = True
        self._unload_timer.start()

    def unload(self) -> None:
        with self._lock:
            if self._model is None:
                return
            self._model = None
            gc.collect()
            logger.info("Unloaded idle STT model %s", self.settings.stt_model)
