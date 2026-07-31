from functools import lru_cache
from pathlib import Path

from faster_whisper import WhisperModel


@lru_cache(maxsize=1)
def get_stt_model(model_name: str, device: str) -> WhisperModel:
    if device in ("auto", "cuda"):
        compute = "float16"
    else:
        compute = "int8"
    return WhisperModel(model_name, device=device, compute_type=compute)


def transcribe(audio_path: Path, model_name: str, device: str) -> str:
    model = get_stt_model(model_name, device)
    segments, _ = model.transcribe(str(audio_path), language="hu", beam_size=5)
    return "\n".join(segment.text for segment in segments)
