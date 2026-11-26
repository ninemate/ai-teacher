"""Decode uploaded media into one bounded audio chunk at a time."""

import io
import math
import os
import selectors
import shutil
import subprocess
import time
import wave
from collections.abc import Callable, Generator, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from teacher_common.docintel import JobFailed

AUDIO_EXTENSIONS = (".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm")
VIDEO_EXTENSIONS = (".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg")
MAX_SECONDS = 4 * 60 * 60
SAMPLE_RATE = 16000
PROBE_OPTIONS = [
    "-protocol_whitelist", "file,pipe",
    "-format_whitelist", "aac,avi,flac,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mp3,mpeg,ogg,wav",
    "-max_alloc", "67108864",
]
UNREADABLE = "A fájl hangja nem olvasható."
# Below the voice service's default 15 MB upload limit.
WAV_CHUNK_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class AudioChunk:
    offset: float
    duration: float
    wav: bytes


class AudioDecoder:
    def __init__(self) -> None:
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")

    @property
    def extensions(self) -> tuple[str, ...]:
        return AUDIO_EXTENSIONS + VIDEO_EXTENSIONS if self.ffmpeg else (".wav",)

    def duration(self, source: Path) -> float | None:
        if not self.ffmpeg:
            with closing(wave.open(str(source), "rb")) as audio:
                return audio.getnframes() / audio.getframerate()
        if not self.ffprobe:
            return None
        try:
            result = subprocess.run(
                [self.ffprobe, "-v", "error", *PROBE_OPTIONS, "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True,
            )
            value = float(result.stdout)
            return value if math.isfinite(value) and value > 0 else None
        except (ValueError, subprocess.SubprocessError):
            return None

    def chunks(self, source: Path, seconds: float, check_cancel: Callable[[], None]) -> Iterator[AudioChunk]:
        if self.ffmpeg:
            with closing(self._pcm(source, max(1, int(seconds * SAMPLE_RATE)) * 2, check_cancel)) as pcm:
                yield from self._wav_chunks(pcm, SAMPLE_RATE, 1, 2)
        else:
            with closing(wave.open(str(source), "rb")) as audio:
                rate, channels, width = audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
                # Even uncommon multichannel or high-rate WAVs stay within the voice service's upload limit.
                frames = max(1, min(int(seconds * rate), WAV_CHUNK_BYTES // (channels * width)))
                blocks = iter(lambda: audio.readframes(frames), b"")
                yield from self._wav_chunks(blocks, rate, channels, width)

    @staticmethod
    def _wav_chunks(pcm: Iterator[bytes], rate: int, channels: int, width: int) -> Iterator[AudioChunk]:
        offset = 0.0
        for data in pcm:
            duration = len(data) / (rate * channels * width)
            if offset + duration > MAX_SECONDS + 1e-6:
                raise JobFailed("A felvétel legfeljebb 4 órás lehet.")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setparams((channels, width, rate, 0, "NONE", ""))
                wav.writeframes(data)
            yield AudioChunk(offset, duration, output.getvalue())
            offset += duration

    def _pcm(self, source: Path, chunk_bytes: int, check_cancel: Callable[[], None]) -> Generator[bytes, None, None]:
        assert self.ffmpeg is not None
        command = [
            self.ffmpeg, "-nostdin", "-v", "error", "-xerror", *PROBE_OPTIONS,
            "-threads", "1", "-copyts", "-start_at_zero", "-i", str(source),
            "-map", "0:a:0", "-af", "aresample=16000:async=1:first_pts=0", "-ac", "1", "-f", "s16le", "pipe:1",
        ]
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process, selectors.DefaultSelector() as selector:
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    data = bytearray()
                    deadline = time.monotonic() + 60
                    next_check = 0.0
                    while len(data) < chunk_bytes:
                        now = time.monotonic()
                        if now >= next_check:
                            check_cancel()
                            next_check = now + 0.25
                        if now >= deadline:
                            raise JobFailed("A hang dekódolása túl sokáig tart.")
                        if not selector.select(timeout=0.25):
                            continue
                        block = os.read(process.stdout.fileno(), min(65536, chunk_bytes - len(data)))
                        if not block:
                            if process.wait(timeout=5) != 0:
                                raise JobFailed(UNREADABLE)
                            if data:
                                yield bytes(data)
                            return
                        data.extend(block)
                    yield bytes(data)
            finally:
                if process.poll() is None:
                    process.kill()
