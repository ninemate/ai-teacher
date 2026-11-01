"""Voice service: Whisper and Piper are replaced by fakes; the real engines run only when installed (see bottom)."""

import io
import os
import time
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def voice(voice_main):
    import stt
    import tts

    class FakeWhisper:
        def __init__(self, device: str) -> None:
            self.device = device
            self.calls: list[dict] = []

        def transcribe(self, path, **kwargs):
            data = Path(path).read_bytes()
            self.calls.append({"path": path, "data": data, **kwargs})
            if data == b"garbage":
                raise ValueError("Invalid data found when processing input")
            duration = 600.0 if data == b"long" else 3.2
            segments = iter([SimpleNamespace(text=" Mi a fotoszintézis?"), SimpleNamespace(text=" Magyarázd el! ")])
            return segments, SimpleNamespace(duration=duration)

    class FakeSpeaker:
        def __init__(self, model_path: Path) -> None:
            self.model_path = model_path
            self.spoken: list[tuple[str, float]] = []

        def synthesize(self, text: str, wav_file: wave.Wave_write, length_scale: float) -> None:
            self.spoken.append((text, length_scale))
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 2205)

    settings = replace(
        voice_main.settings,
        stt_backend="faster-whisper",
        stt_device="cpu",
        stt_idle_unload_seconds=0,
        tts_backend="piper",
        tts_voices=["hu_HU-imre-medium", "hu_HU-anna-medium"],
        tts_voice_dir="/models/piper",
        tts_max_chars=200,
        max_upload_bytes=1024,
    )
    models: list[FakeWhisper] = []

    def factory(model_name, device, compute_type, cpu_threads):
        model = FakeWhisper(device)
        model.compute_type = compute_type
        models.append(model)
        return model

    speakers: dict[str, FakeSpeaker] = {}

    def load_voice(path: Path) -> FakeSpeaker:
        speakers[path.stem] = FakeSpeaker(path)
        return speakers[path.stem]

    transcriber = stt.Transcriber(settings, model_factory=factory, cuda_available=lambda: False)
    synthesizer = tts.Synthesizer(settings, voice_loader=load_voice, encode_mp3=lambda wav: b"ID3" + wav[:16])
    original = (voice_main.settings, voice_main.transcriber, voice_main.synthesizer)
    voice_main.settings, voice_main.transcriber, voice_main.synthesizer = settings, transcriber, synthesizer
    yield SimpleNamespace(client=TestClient(voice_main.app), models=models, speakers=speakers, main=voice_main)
    voice_main.settings, voice_main.transcriber, voice_main.synthesizer = original


def test_health_lists_engines_and_voices(voice):
    body = voice.client.get("/health").json()
    assert body["stt"] == {
        "enabled": True,
        "backend": "faster-whisper",
        "model": voice.main.settings.stt_model,
        "device": "cpu",
        "loaded": False,
        "max_seconds": voice.main.settings.stt_max_seconds,
    }
    assert body["tts"]["voices"] == ["hu_HU-imre-medium", "hu_HU-anna-medium"]
    assert body["tts"]["default_voice"] == "hu_HU-imre-medium"


def test_transcribe_forces_hungarian_and_joins_segments(voice):
    response = voice.client.post("/voice/transcribe", files={"audio": ("felvetel.webm", b"opus-bytes", "audio/webm")})
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "Mi a fotoszintézis? Magyarázd el!"
    assert body["language"] == "hu"
    assert body["duration"] == 3.2
    (model,) = voice.models
    assert model.compute_type == "int8"
    call = model.calls[0]
    assert call["language"] == "hu" and call["vad_filter"] is True
    assert call["data"] == b"opus-bytes"
    assert call["path"].endswith(".webm")
    assert not Path(call["path"]).exists(), "the upload is deleted after transcription"
    assert voice.client.get("/health").json()["stt"]["loaded"] is True


def test_transcribe_never_uses_the_client_file_name_as_path(voice):
    response = voice.client.post("/voice/transcribe", files={"audio": ("../../etc/passwd", b"x", "audio/wav")})
    assert response.status_code == 200
    path = voice.models[0].calls[0]["path"]
    assert Path(path).name == "upload.audio"


@pytest.mark.parametrize(
    ("data", "status", "detail"),
    [
        (b"garbage", 422, "Nem sikerült beolvasni a hangfelvételt."),
        (b"long", 413, "A felvétel legfeljebb 120 másodperces lehet."),
        (b"x" * 2048, 413, "A hangfelvétel túl nagy."),
    ],
)
def test_transcribe_rejects_bad_input(voice, data, status, detail):
    response = voice.client.post("/voice/transcribe", files={"audio": ("a.wav", data, "audio/wav")})
    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_disabled_engines_answer_503(voice):
    voice.main.settings = replace(voice.main.settings, stt_backend="disabled", tts_backend="disabled")
    assert voice.client.post("/voice/transcribe", files={"audio": ("a.wav", b"x", "audio/wav")}).status_code == 503
    assert voice.client.post("/voice/speak", json={"text": "Szia"}).status_code == 503
    health = voice.client.get("/health").json()
    assert health["stt"]["enabled"] is False and health["tts"]["voices"] == []


def test_speak_cleans_markdown_and_returns_mp3(voice):
    response = voice.client.post("/voice/speak", json={"text": "## Cím\n\n**Fontos:** kb. 5 db alma"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content.startswith(b"ID3")
    speaker = voice.speakers["hu_HU-imre-medium"]
    assert speaker.model_path == Path("/models/piper/hu_HU-imre-medium.onnx")
    assert speaker.spoken == [("Cím. Fontos: körülbelül 5 darab alma.", 1.0)]


def test_speak_with_another_voice_as_wav(voice):
    response = voice.client.post("/voice/speak", json={"text": "Szia!", "voice": "hu_HU-anna-medium", "format": "wav"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content)) as wav_file:
        assert wav_file.getframerate() == 22050
        assert wav_file.getnframes() == 2205
    assert list(voice.speakers) == ["hu_HU-anna-medium"]


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"text": "Szia", "voice": "../../etc/passwd"}, 422),
        ({"text": "Szia", "voice": "hu_HU-berta-medium"}, 422),  # installed voices only
        ({"text": "**   **"}, 422),
        ({"text": "a" * 201}, 413),
        ({"text": ""}, 422),
        ({"text": "Szia", "format": "ogg"}, 422),
    ],
)
def test_speak_rejects_bad_requests(voice, payload, status):
    assert voice.client.post("/voice/speak", json=payload).status_code == status
    assert voice.speakers == {}


def test_auto_device_falls_back_to_cpu_when_cuda_fails(voice_main):
    import stt

    tried = []

    def factory(model_name, device, compute_type, cpu_threads):
        tried.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("CUDA out of memory")
        return SimpleNamespace()

    settings = replace(voice_main.settings, stt_device="auto", stt_compute_type="auto")
    transcriber = stt.Transcriber(settings, model_factory=factory, cuda_available=lambda: True)
    transcriber._load()
    assert tried == [("cuda", "int8_float16"), ("cpu", "int8")]
    assert transcriber.device == "cpu"

    strict = stt.Transcriber(replace(settings, stt_device="cuda"), model_factory=factory, cuda_available=lambda: True)
    with pytest.raises(RuntimeError):
        strict._load()


def test_idle_model_is_unloaded(voice, tmp_path):
    transcriber = voice.main.transcriber
    transcriber.settings = replace(transcriber.settings, stt_idle_unload_seconds=0.05)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"pcm")
    transcriber.transcribe(audio)
    assert transcriber.loaded
    deadline = time.monotonic() + 2
    while transcriber.loaded and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not transcriber.loaded
    transcriber.transcribe(audio)
    assert len(voice.models) == 2, "reloaded on the next request"


def test_settings_from_env(voice_main, monkeypatch):
    from voice_settings import VoiceSettings

    monkeypatch.setenv("VOICE_TTS_VOICES", " hu_HU-anna-medium , hu_HU-imre-medium,")
    monkeypatch.setenv("VOICE_STT_DEVICE", "CUDA")
    monkeypatch.setenv("VOICE_MAX_UPLOAD_MB", "2.5")
    monkeypatch.setenv("VOICE_TTS_BACKEND", "")
    settings = VoiceSettings.from_env()
    assert settings.tts_voices == ["hu_HU-anna-medium", "hu_HU-imre-medium"]
    assert settings.stt_device == "cuda"
    assert settings.max_upload_bytes == int(2.5 * 1024 * 1024)
    assert settings.tts_backend == "piper", "an empty variable falls back to the default"


# --- Real engines: run where they are installed (the voice image, or a dev venv with the models) -------------


def test_mp3_encoding_with_pyav():
    pytest.importorskip("av")
    import tts

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x10\x00\xf0\xff" * 11025)
    mp3 = tts.wav_to_mp3(buffer.getvalue())
    assert mp3[:3] == b"ID3" or mp3[:2] == b"\xff\xfb"
    assert len(mp3) < len(buffer.getvalue()) / 4


@pytest.mark.skipif(not os.getenv("VOICE_TEST_PIPER_DIR"), reason="set VOICE_TEST_PIPER_DIR to a folder of Piper voices")
def test_real_piper_and_whisper_round_trip(voice_main, tmp_path):
    """Piper speaks a Hungarian sentence, Whisper (VOICE_TEST_STT_MODEL, default large-v3-turbo) hears it back."""
    pytest.importorskip("piper")
    pytest.importorskip("faster_whisper")
    import stt
    import tts

    settings = replace(
        voice_main.settings,
        tts_voice_dir=os.environ["VOICE_TEST_PIPER_DIR"],
        tts_voices=["hu_HU-imre-medium"],
        stt_model=os.getenv("VOICE_TEST_STT_MODEL", "large-v3-turbo"),
        stt_device="cpu",
    )
    audio_path = tmp_path / "answer.mp3"
    audio_path.write_bytes(tts.Synthesizer(settings).synthesize("**Mi** a fotoszintézis? Kb. 6 db molekula."))
    assert audio_path.stat().st_size > 10_000
    result = stt.Transcriber(settings).transcribe(audio_path)
    text = result.text.lower()
    # Piper samples noise, so a vowel may come out differently from run to run, and Whisper may write
    # "körülbelül" back as "kb."; match stable stems. The text clean-up itself is tested in test_speech_text.py.
    assert "szintéz" in text
    assert "6 darab" in text
    assert "molekul" in text
