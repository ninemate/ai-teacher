"""Size-bounded cache of synthesized segments, one folder per document (and its content hash).

Audio is only ever made for a segment someone asked for, so the cache holds what has been listened to. Files are
named by a hash of voice and text, never by anything a client sent. A document's folder is named after its id and
source hash: deleting the document removes the folder, and a re-indexed file (new hash) leaves an orphan that
``sweep`` removes. Access is not decided here; callers check the document before they touch the cache.
"""

import hashlib
import os
import shutil
import threading
from collections.abc import Iterable
from pathlib import Path


class AudioCache:
    def __init__(self, root: Path | str, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    @staticmethod
    def document_key(document_id: int, source_hash: str) -> str:
        return f"{int(document_id)}-{hashlib.sha256(source_hash.encode()).hexdigest()[:12]}"

    @staticmethod
    def _file_name(voice: str, text: str) -> str:
        return hashlib.sha256(f"{voice}\0{text}".encode()).hexdigest()[:40] + ".mp3"

    def _path(self, document_key: str, voice: str, text: str) -> Path:
        return self.root / document_key / self._file_name(voice, text)

    def key_lock(self, document_key: str, voice: str, text: str) -> threading.Lock:
        """One lock per segment, so two requests for it synthesize once."""
        name = f"{document_key}/{self._file_name(voice, text)}"
        with self._lock:
            return self._key_locks.setdefault(name, threading.Lock())

    def release_key_lock(self, document_key: str, voice: str, text: str) -> None:
        with self._lock:
            self._key_locks.pop(f"{document_key}/{self._file_name(voice, text)}", None)

    def get(self, document_key: str, voice: str, text: str) -> bytes | None:
        path = self._path(document_key, voice, text)
        try:
            data = path.read_bytes()
            os.utime(path)  # most recently used survives eviction
        except OSError:
            return None
        return data

    def put(self, document_key: str, voice: str, text: str, data: bytes) -> None:
        path = self._path(document_key, voice, text)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
        self.evict()

    def _files(self) -> list[tuple[float, int, Path]]:
        found: list[tuple[float, int, Path]] = []
        if not self.root.is_dir():
            return found
        for path in self.root.glob("*/*.mp3"):
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, stat.st_size, path))
        return found

    def total_bytes(self) -> int:
        return sum(size for _, size, _ in self._files())

    def evict(self) -> int:
        """Delete least recently used files until the cache fits; returns how many were removed."""
        with self._lock:
            files = sorted(self._files())
            total = sum(size for _, size, _ in files)
            removed = 0
            for _, size, path in files:
                if total <= self.max_bytes:
                    break
                path.unlink(missing_ok=True)
                total -= size
                removed += 1
            return removed

    def remove_document(self, document_id: int) -> None:
        """Remove the audio of every version of a document."""
        if not self.root.is_dir():
            return
        for folder in self.root.glob(f"{int(document_id)}-*"):
            shutil.rmtree(folder, ignore_errors=True)

    def sweep(self, live_keys: Iterable[str]) -> int:
        """Remove folders of documents that no longer exist or were re-indexed."""
        if not self.root.is_dir():
            return 0
        live = set(live_keys)
        removed = 0
        for folder in self.root.iterdir():
            if folder.is_dir() and folder.name not in live:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed
