"""Hungarian text-to-speech with Piper (VITS voices exported to ONNX, run on the CPU).

Voices are downloaded into `tts_voice_dir` when the image is built; only names listed in `tts_voices` can be
asked for, so a request can never point the loader at another path.
"""

import io
import logging
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from speech_text import prepare_for_speech
from voice_settings import VoiceSettings

logger = logging.getLogger("uvicorn")

AudioFormat = Literal["mp3", "wav"]
MEDIA_TYPES: dict[str, str] = {"mp3": "audio/mpeg", "wav": "audio/wav"}


class UnknownVoice(ValueError):
    pass


class NothingToSay(ValueError):
    pass


class PiperSpeaker:
    """Adapter so tests can swap in a fake voice without importing piper."""

    def __init__(self, model_path: Path) -> None:
        from piper import PiperVoice

        self._voice = PiperVoice.load(model_path)

    def synthesize(self, text: str, wav_file: wave.Wave_write, length_scale: float) -> None:
        from piper import SynthesisConfig

        self._voice.synthesize_wav(text, wav_file, syn_config=SynthesisConfig(length_scale=length_scale))


def wav_to_mp3(wav_bytes: bytes, bitrate: int = 64_000) -> bytes:
    """Re-encode Piper's 16-bit WAV as mono MP3 (about a tenth of the size, plays in every browser)."""
    import av

    out = io.BytesIO()
    with av.open(io.BytesIO(wav_bytes)) as source, av.open(out, "w", format="mp3") as target:
        sample_rate = source.streams.audio[0].rate
        stream = target.add_stream("libmp3lame", rate=sample_rate)
        stream.bit_rate = bitrate
        stream.layout = "mono"
        resampler = av.AudioResampler(format=stream.format.name, layout="mono", rate=sample_rate)
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                target.mux(stream.encode(converted))
        for converted in resampler.resample(None):
            target.mux(stream.encode(converted))
        target.mux(stream.encode(None))
    return out.getvalue()


class Synthesizer:
    def __init__(
        self,
        settings: VoiceSettings,
        voice_loader: Callable[[Path], Any] = PiperSpeaker,
        encode_mp3: Callable[[bytes], bytes] = wav_to_mp3,
    ) -> None:
        self.settings = settings
        self._voice_loader = voice_loader
        self._encode_mp3 = encode_mp3
        self._voices: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def default_voice(self) -> str:
        return self.settings.tts_voices[0]

    @property
    def loaded_voices(self) -> list[str]:
        return list(self._voices)

    def _voice(self, name: str) -> Any:
        if name not in self.settings.tts_voices:
            raise UnknownVoice(name)
        if name not in self._voices:
            started = time.perf_counter()
            self._voices[name] = self._voice_loader(Path(self.settings.tts_voice_dir) / f"{name}.onnx")
            logger.info("Loaded TTS voice %s in %.1fs", name, time.perf_counter() - started)
        return self._voices[name]

    def synthesize(self, text: str, voice: str | None = None, audio_format: AudioFormat = "mp3") -> bytes:
        spoken = prepare_for_speech(text)
        if not spoken:
            raise NothingToSay()
        buffer = io.BytesIO()
        with self._lock:
            speaker = self._voice(voice or self.default_voice)
            with wave.open(buffer, "wb") as wav_file:
                speaker.synthesize(spoken, wav_file, self.settings.tts_length_scale)
        wav_bytes = buffer.getvalue()
        return wav_bytes if audio_format == "wav" else self._encode_mp3(wav_bytes)
