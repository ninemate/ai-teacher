"""Watched-folder configuration and scanning.

Only folders listed under ``watch.folders`` are ever walked: the library is a
terabyte-scale network share and most of it is not meant to be indexed.
"""

import fnmatch
import os
import posixpath
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator


class WatchFolder(BaseModel):
    """One folder (relative to the library mount point) to index."""

    path: str
    # Existing single-user configurations keep indexing into the restricted legacy workspace.
    workspace: str = "legacy"
    enabled: bool = True
    recursive: bool = True
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    extensions: list[str] | None = None

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        normalized = posixpath.normpath(value.strip().replace("\\", "/")) if value.strip() else ""
        if not normalized:
            raise ValueError("watch folder path must not be empty (use '.' for the whole library)")
        if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise ValueError(f"watch folder path must be relative to the library root: {value!r}")
        return normalized

    @field_validator("extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [item.strip().lower() if item.strip().startswith(".") else f".{item.strip().lower()}" for item in value]

    @field_validator("workspace")
    @classmethod
    def _workspace_id(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128 or any(char.isspace() for char in value):
            raise ValueError("watch folder workspace must be a non-empty workspace id")
        return value


class WatchConfig(BaseModel):
    folders: list[WatchFolder] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_overlaps(self) -> "WatchConfig":
        folders = self.active_folders
        for index, folder in enumerate(folders):
            for other in folders[index + 1 :]:
                nested = (
                    folder.path in (".", other.path)
                    or other.path == "."
                    or folder.path.startswith(other.path.rstrip("/") + "/")
                    or other.path.startswith(folder.path.rstrip("/") + "/")
                )
                if nested and folder.workspace != other.workspace:
                    raise ValueError("overlapping watched folders must use the same workspace")
        return self

    @property
    def active_folders(self) -> list[WatchFolder]:
        return [folder for folder in self.folders if folder.enabled]


@dataclass
class ScanReport:
    """What a scan could not see; documents under these paths must not be treated as removed."""

    missing_folders: list[str] = field(default_factory=list)
    unreadable_dirs: list[str] = field(default_factory=list)
    scanned_per_folder: dict[str, int] = field(default_factory=dict)

    def is_unreliable(self, path: str) -> bool:
        return any(_is_within(path, prefix) for prefix in self._unreliable_prefixes)

    @property
    def _unreliable_prefixes(self) -> list[str]:
        return [*self.missing_folders, *self.unreadable_dirs]


def _is_within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip(os.sep) + os.sep)


def _matches(patterns: Iterable[str], relative: str) -> bool:
    """Case-insensitive glob match against the relative path or the bare name.

    ``*`` also matches ``/`` so ``**/draft/*`` and ``*draft*`` both work.
    """
    relative = relative.lower()
    name = posixpath.basename(relative)
    for pattern in patterns:
        pattern = pattern.lower()
        if fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def file_selected(folder: WatchFolder, global_exclude: list[str], relative: str, extensions: Iterable[str]) -> bool:
    """Decide whether a file (path relative to the watch folder) belongs in the index."""
    allowed = {item.lower() for item in (folder.extensions if folder.extensions is not None else extensions)}
    if posixpath.splitext(relative)[1].lower() not in allowed:
        return False
    if _matches([*global_exclude, *folder.exclude], relative):
        return False
    return not folder.include or _matches(folder.include, relative)


def scan_watched_files(
    library_root: Path,
    config: WatchConfig,
    extensions: Iterable[str],
    report: ScanReport | None = None,
) -> Iterator[Path]:
    """Yield candidate files from the active watch folders only, in stable order, without duplicates."""
    report = report if report is not None else ScanReport()
    extensions = list(extensions)
    seen: set[Path] = set()
    for folder in config.active_folders:
        base = library_root / folder.path
        if not base.is_dir():
            report.missing_folders.append(str(base))
            continue
        count = 0

        def on_error(error: OSError, base: Path = base) -> None:
            report.unreadable_dirs.append(str(error.filename or base))

        for dirpath, dirnames, filenames in os.walk(base, onerror=on_error):
            current = Path(dirpath)
            relative_dir = current.relative_to(base).as_posix()
            if not folder.recursive:
                dirnames[:] = []
            else:
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if not _matches(
                        [*config.exclude, *folder.exclude],
                        name if relative_dir == "." else f"{relative_dir}/{name}",
                    )
                )
            for filename in sorted(filenames):
                relative = filename if relative_dir == "." else f"{relative_dir}/{filename}"
                path = current / filename
                if path in seen or not file_selected(folder, config.exclude, relative, extensions):
                    continue
                seen.add(path)
                count += 1
                yield path
        report.scanned_per_folder[folder.path] = count
