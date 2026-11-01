"""Voice service settings, read from the environment (see docs/voice-hungarian.md)."""

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


@dataclass(frozen=True)
class VoiceSettings:
    stt_backend: str = "faster-whisper"  # faster-whisper | disabled
    stt_model: str = "large-v3-turbo"
    stt_device: str = "cpu"  # cpu | cuda | auto (cuda when a GPU is visible, else cpu)
    stt_compute_type: str = "auto"  # auto: int8 on cpu, int8_float16 on cuda
    stt_cpu_threads: int = 0  # 0: CTranslate2 default
    stt_beam_size: int = 5
    stt_max_seconds: float = 120.0
    stt_idle_unload_seconds: int = 0  # 0: keep the model loaded
    tts_backend: str = "piper"  # piper | disabled
    tts_voices: list[str] = field(default_factory=lambda: ["hu_HU-imre-medium"])  # first one is the default
    tts_voice_dir: str = "/models/piper"
    tts_length_scale: float = 1.0  # above 1 speaks slower
    tts_max_chars: int = 6000
    max_upload_bytes: int = 15 * 1024 * 1024

    @property
    def stt_enabled(self) -> bool:
        return self.stt_backend == "faster-whisper"

    @property
    def tts_enabled(self) -> bool:
        return self.tts_backend == "piper" and bool(self.tts_voices)

    @classmethod
    def from_env(cls) -> "VoiceSettings":
        voices = [item.strip() for item in _env("VOICE_TTS_VOICES", "hu_HU-imre-medium").split(",") if item.strip()]
        return cls(
            stt_backend=_env("VOICE_STT_BACKEND", "faster-whisper").lower(),
            stt_model=_env("VOICE_STT_MODEL", "large-v3-turbo"),
            stt_device=_env("VOICE_STT_DEVICE", "cpu").lower(),
            stt_compute_type=_env("VOICE_STT_COMPUTE_TYPE", "auto"),
            stt_cpu_threads=int(_env("VOICE_STT_CPU_THREADS", "0")),
            stt_beam_size=int(_env("VOICE_STT_BEAM_SIZE", "5")),
            stt_max_seconds=float(_env("VOICE_STT_MAX_SECONDS", "120")),
            stt_idle_unload_seconds=int(_env("VOICE_STT_IDLE_UNLOAD_SECONDS", "0")),
            tts_backend=_env("VOICE_TTS_BACKEND", "piper").lower(),
            tts_voices=voices,
            tts_voice_dir=_env("VOICE_TTS_VOICE_DIR", "/models/piper"),
            tts_length_scale=float(_env("VOICE_TTS_LENGTH_SCALE", "1.0")),
            tts_max_chars=int(_env("VOICE_TTS_MAX_CHARS", "6000")),
            max_upload_bytes=int(float(_env("VOICE_MAX_UPLOAD_MB", "15")) * 1024 * 1024),
        )
