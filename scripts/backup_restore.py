#!/usr/bin/env python3
"""Create and restore private, checksummed backups of the application state."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "manifest.json"
QDRANT_WORKER = r"""
import http.client, json, os, sys, urllib.error, urllib.parse, urllib.request, uuid
action = sys.argv[1]
base = os.environ.get('QDRANT_URL', 'http://qdrant:6333').rstrip('/')
key = os.environ.get('QDRANT_API_KEY', '')
headers = {'api-key': key} if key else {}
if action == 'api':
    method, path, payload = sys.argv[2:5]
    data = None if payload == '-' else payload.encode()
    request_headers = dict(headers)
    if data is not None: request_headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(base + path, data=data, method=method, headers=request_headers)
    with urllib.request.urlopen(req, timeout=3600) as response: body = response.read()
    sys.stdout.write(body.decode('utf-8') if body else '{}')
    raise SystemExit(0)
collection = sys.argv[2]
def request(method, path):
    req = urllib.request.Request(base + path, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=3600)
quoted = urllib.parse.quote(collection, safe='')
if action == 'snapshot':
    with request('POST', '/collections/' + quoted + '/snapshots') as response:
        name = json.load(response)['result']['name']
    try:
        with request('GET', '/collections/' + quoted + '/snapshots/' + urllib.parse.quote(name, safe='')) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk: break
                sys.stdout.buffer.write(chunk)
    finally:
        try: request('DELETE', '/collections/' + quoted + '/snapshots/' + urllib.parse.quote(name, safe=''))
        except Exception: pass
elif action == 'restore':
    boundary = 'teacher-' + uuid.uuid4().hex
    prefix = ('--' + boundary + '\r\nContent-Disposition: form-data; name="snapshot"; filename="snapshot.snapshot"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    suffix = ('\r\n--' + boundary + '--\r\n').encode()
    size = int(os.environ['BACKUP_SNAPSHOT_SIZE'])
    parsed = urllib.parse.urlsplit(base)
    connection_type = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=3600)
    path = '/collections/' + quoted + '/snapshots/upload?wait=true&priority=snapshot'
    connection.putrequest('POST', path)
    for name, value in headers.items(): connection.putheader(name, value)
    connection.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
    connection.putheader('Content-Length', str(len(prefix) + size + len(suffix)))
    connection.endheaders()
    connection.send(prefix)
    remaining = size
    while remaining:
        chunk = sys.stdin.buffer.read(min(1024 * 1024, remaining))
        if not chunk: raise RuntimeError('snapshot input ended early')
        connection.send(chunk)
        remaining -= len(chunk)
    connection.send(suffix)
    response = connection.getresponse()
    body = response.read()
    if response.status >= 300: raise RuntimeError('Qdrant restore failed: ' + body.decode('utf-8', 'replace'))
    print(body.decode('utf-8', 'replace'), file=sys.stderr)
elif action == 'presence':
    try:
        with request('GET', '/collections/' + quoted) as response: response.read()
        print('present')
    except urllib.error.HTTPError as error:
        if error.code == 404: print('missing')
        else: raise
elif action == 'delete':
    try:
        with request('DELETE', '/collections/' + quoted) as response: response.read()
    except urllib.error.HTTPError as error:
        if error.code != 404: raise
else:
    raise SystemExit('unknown worker action')
"""


class BackupError(RuntimeError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


class Settings:
    def __init__(self, env_file: Path | None = None, args=None):
        self.env_file = env_file or Path(os.environ.get("TEACHER_ENV_FILE", ROOT / ".env"))
        if not self.env_file.is_absolute():
            self.env_file = (Path.cwd() / self.env_file).resolve()
        self.env = read_env_file(self.env_file)

        def value(name: str, default: str = "") -> str:
            return os.environ.get(name, self.env.get(name, default))

        self.stack_dir = Path(value("TEACHER_STACK_DIR", str(ROOT))).expanduser().resolve()
        self.data_dir = Path(value("TEACHER_DATA_DIR", "/srv/teacher-agent")).expanduser().resolve()
        self.library = Path(value("LIBRARY_PATH", value("TEACHER_MOUNT_POINT", "/mnt/library"))).expanduser()
        self.library = self.library.resolve()
        self.destination = Path(value("TEACHER_BACKUP_DIR", str(self.data_dir / "backups"))).expanduser()
        self.destination = self.destination.resolve()
        self.db_path = Path(value("BACKUP_DATABASE_PATH", "")).expanduser() if value("BACKUP_DATABASE_PATH") else None
        if self.db_path is None:
            url = value("METADATA_DB_URL", "sqlite:////data/metadata/teacher.db")
            if not url.startswith("sqlite:///"):
                raise BackupError("backup currently supports SQLite metadata databases only")
            raw = urllib.parse.unquote(url[len("sqlite:///"):])
            db_path = Path(raw)
            if str(db_path).startswith("/data/"):
                db_path = self.data_dir / str(db_path).removeprefix("/data/")
            elif not db_path.is_absolute():
                db_path = Path.cwd() / db_path
            self.db_path = db_path.resolve()
        self.qdrant_url = value("QDRANT_URL", "http://qdrant:6333").rstrip("/")
        self.collection = value("QDRANT_COLLECTION", "teacher_documents")
        self.transport = value("BACKUP_QDRANT_TRANSPORT", "compose")
        self.keep = int(value("BACKUP_KEEP_LAST", "7"))
        self.args = args
        if args:
            if args.destination:
                self.destination = Path(args.destination).expanduser().resolve()
            if args.transport:
                self.transport = args.transport

    def validate_destination(self) -> None:
        if self.keep < 1:
            raise BackupError("BACKUP_KEEP_LAST must be at least 1")
        try:
            self.destination.relative_to(self.library)
        except ValueError:
            return
        raise BackupError(f"backup destination must be outside the read-only library ({self.library})")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def db_backup(source_path: Path, target_path: Path) -> None:
    if not source_path.is_file():
        raise BackupError(f"SQLite database does not exist: {source_path}")
    source = sqlite3.connect(source_path, timeout=30)
    target = sqlite3.connect(target_path, timeout=30)
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"SQLite integrity check failed: {result}")
    finally:
        target.close()
        source.close()


def _headers(settings: Settings) -> dict[str, str]:
    key = settings.env.get("QDRANT_API_KEY", os.environ.get("QDRANT_API_KEY", ""))
    return {"api-key": key} if key else {}


def _compose_command(
    settings: Settings,
    extra_env: list[str] | None = None,
    extra_volumes: list[str] | None = None,
    worker_code: str = QDRANT_WORKER,
) -> list[str]:
    args = ["docker", "compose"]
    if settings.env_file.is_file():
        args += ["--env-file", str(settings.env_file)]
    for name in ("docker-compose.yml", "docker-compose.dev.yml", "docker-compose.proxy.yml"):
        args += ["-f", str(settings.stack_dir / "compose" / name)]
    args += ["run", "--rm", "-T", "--no-deps"]
    for item in extra_env or []:
        args += ["-e", item]
    for volume in extra_volumes or []:
        args += ["-v", volume]
    args += ["--entrypoint", "python", "api", "-c", worker_code]
    return args


def qdrant_snapshot(settings: Settings, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if settings.transport == "compose":
        command = _compose_command(settings) + ["snapshot", settings.collection]
        try:
            with target.open("wb") as output:
                process = subprocess.run(command, stdout=output, check=False, cwd=settings.stack_dir)
        except OSError as exc:
            raise BackupError(f"could not run Qdrant snapshot through Docker Compose: {exc}") from exc
        if process.returncode:
            raise BackupError("Qdrant snapshot through the private API container failed")
        return
    if settings.transport != "direct":
        raise BackupError("BACKUP_QDRANT_TRANSPORT must be 'compose' or 'direct'")
    collection = urllib.parse.quote(settings.collection, safe="")
    request = urllib.request.Request(f"{settings.qdrant_url}/collections/{collection}/snapshots", method="POST", headers=_headers(settings))
    name = ""
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            name = json.load(response)["result"]["name"]
        download = urllib.request.Request(
            f"{settings.qdrant_url}/collections/{collection}/snapshots/{urllib.parse.quote(name, safe='')}",
            headers=_headers(settings),
        )
        with urllib.request.urlopen(download, timeout=3600) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, KeyError, urllib.error.URLError) as exc:
        raise BackupError(f"Qdrant snapshot failed: {exc}") from exc
    finally:
        if name:
            cleanup = urllib.request.Request(
                f"{settings.qdrant_url}/collections/{collection}/snapshots/{urllib.parse.quote(name, safe='')}",
                method="DELETE",
                headers=_headers(settings),
            )
            try:
                urllib.request.urlopen(cleanup, timeout=60).close()
            except (OSError, urllib.error.URLError):
                pass


def qdrant_present(settings: Settings, collection: str | None = None) -> bool:
    collection = collection or settings.collection
    if settings.transport == "compose":
        result = subprocess.run(
            _compose_command(settings) + ["presence", collection],
            check=False,
            cwd=settings.stack_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise BackupError("could not check Qdrant collection through the private API container")
        return result.stdout.strip() == "present"
    if settings.transport != "direct":
        raise BackupError("BACKUP_QDRANT_TRANSPORT must be 'compose' or 'direct'")
    request = urllib.request.Request(
        f"{settings.qdrant_url}/collections/{urllib.parse.quote(collection, safe='')}", headers=_headers(settings)
    )
    try:
        urllib.request.urlopen(request, timeout=30).close()
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise BackupError(f"could not inspect Qdrant collection: HTTP {exc.code}") from exc
    except OSError as exc:
        raise BackupError(f"could not inspect Qdrant collection: {exc}") from exc


def qdrant_delete(settings: Settings, collection: str) -> None:
    if settings.transport == "compose":
        result = subprocess.run(
            _compose_command(settings) + ["delete", collection],
            check=False,
            cwd=settings.stack_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise BackupError("could not remove Qdrant collection through the private API container")
        return
    request = urllib.request.Request(
        f"{settings.qdrant_url}/collections/{urllib.parse.quote(collection, safe='')}",
        method="DELETE",
        headers=_headers(settings),
    )
    try:
        urllib.request.urlopen(request, timeout=60).close()
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise BackupError(f"could not remove Qdrant collection: HTTP {exc.code}") from exc
    except OSError as exc:
        raise BackupError(f"could not remove Qdrant collection: {exc}") from exc


def qdrant_restore(settings: Settings, collection: str, source: Path) -> None:
    if settings.transport == "compose":
        command = _compose_command(settings, [f"BACKUP_SNAPSHOT_SIZE={source.stat().st_size}"]) + ["restore", collection]
        try:
            with source.open("rb") as snapshot:
                result = subprocess.run(command, stdin=snapshot, check=False, cwd=settings.stack_dir)
        except OSError as exc:
            raise BackupError(f"could not restore Qdrant through Docker Compose: {exc}") from exc
        if result.returncode:
            raise BackupError("Qdrant restore through the private API container failed")
        return
    if settings.transport != "direct":
        raise BackupError("BACKUP_QDRANT_TRANSPORT must be 'compose' or 'direct'")

    parsed = urllib.parse.urlsplit(settings.qdrant_url)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=3600)
    boundary = "teacher-" + os.urandom(16).hex()
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"snapshot\"; filename=\"snapshot.snapshot\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    collection_path = urllib.parse.quote(collection, safe="")
    path = f"/collections/{collection_path}/snapshots/upload?wait=true&priority=snapshot"
    try:
        connection.putrequest("POST", path)
        for name, value in _headers(settings).items():
            connection.putheader(name, value)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(len(prefix) + source.stat().st_size + len(suffix)))
        connection.endheaders()
        connection.send(prefix)
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        body = response.read()
        if response.status >= 300:
            raise BackupError(f"Qdrant restore failed ({response.status}): {body[:500].decode('utf-8', 'replace')}")
    except OSError as exc:
        raise BackupError(f"Qdrant restore failed: {exc}") from exc
    finally:
        connection.close()


def config_files(settings: Settings) -> list[tuple[Path, str]]:
    selected: list[tuple[Path, str]] = []
    for directory in ("config/ingestion", "config/prompts", "config/models"):
        source_root = settings.stack_dir / directory
        if not source_root.is_dir():
            continue
        for source in sorted(source_root.rglob("*")):
            if source.is_file() and not source.is_symlink():
                relative = source.relative_to(settings.stack_dir).as_posix()
                selected.append((source, relative))
    cache = Path(os.environ.get("OCR_CACHE_HOST_PATH", settings.data_dir / "metadata" / "ocr_cache")).expanduser()
    if cache.is_dir():
        for source in sorted(cache.rglob("*")):
            if source.is_file() and not source.is_symlink():
                selected.append((source, "generated/ocr_cache/" + source.relative_to(cache).as_posix()))
    update_record = settings.data_dir / "components" / "model-update-records.json"
    if update_record.is_file() and not update_record.is_symlink():
        selected.append((update_record, "components/model-update-records.json"))
    return selected


def create_backup(settings: Settings, *, retention: bool = True, allow_missing_collection: bool = False) -> Path:
    settings.validate_destination()
    settings.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.destination.chmod(0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    final = settings.destination / f"backup-{stamp}"
    stage = Path(tempfile.mkdtemp(prefix=".backup-stage-", dir=settings.destination))
    stage.chmod(0o700)
    try:
        database = stage / "database" / "teacher.db"
        database.parent.mkdir(mode=0o700)
        db_backup(settings.db_path, database)
        database.chmod(0o600)

        collection_present = qdrant_present(settings)
        if collection_present:
            snapshot = stage / "qdrant" / "collection.snapshot"
            qdrant_snapshot(settings, snapshot)
            snapshot.chmod(0o600)
        elif not allow_missing_collection:
            raise BackupError(f"configured Qdrant collection does not exist: {settings.collection}")

        for source, relative in config_files(settings):
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, destination)
            destination.chmod(0o600)

        files = []
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            files.append({"path": path.relative_to(stage).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256(path)})
        manifest = {
            "format_version": 1,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "qdrant_collection": settings.collection,
            "qdrant_collection_present": collection_present,
            "files": files,
            "restore_policy": {"sessions": "invalidated after restore", "library_files": "stored on NAS; not included"},
        }
        manifest_path = stage / MANIFEST
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        os.replace(stage, final)
        final.chmod(0o700)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if retention:
        prune_backups(settings.destination, settings.keep, exclude={final})
    return final


def prune_backups(destination: Path, keep: int, exclude: set[Path] | None = None) -> None:
    exclude = exclude or set()
    folders = []
    for path in destination.glob("backup-*"):
        if path.is_dir() and not path.is_symlink() and (path / MANIFEST).is_file() and path.resolve() not in exclude:
            folders.append(path)
    for path in sorted(folders, key=lambda item: item.name, reverse=True)[max(0, keep - len(exclude)):]:
        shutil.rmtree(path)


def verify_backup(folder: Path) -> tuple[dict, dict[str, Path]]:
    if folder.is_symlink() or not folder.is_dir():
        raise BackupError(f"backup folder does not exist or is a symlink: {folder}")
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"backup manifest is missing or invalid: {exc}") from exc
    if manifest.get("format_version") != 1 or not isinstance(manifest.get("files"), list):
        raise BackupError("unsupported or invalid backup manifest")
    verified: dict[str, Path] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise BackupError("backup manifest contains an invalid file entry")
        relative = item.get("path", "")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative == MANIFEST:
            raise BackupError(f"unsafe path in backup manifest: {relative!r}")
        if relative in verified:
            raise BackupError(f"duplicate path in backup manifest: {relative}")
        path = folder.joinpath(*pure.parts)
        parents = path.parents
        if path.is_symlink() or any(parent.is_symlink() for parent in parents if parent != folder.parent) or not path.is_file():
            raise BackupError(f"backup file is missing or a symlink: {relative}")
        if path.stat().st_size != item.get("size_bytes") or sha256(path) != item.get("sha256"):
            raise BackupError(f"checksum or size mismatch: {relative}")
        verified[relative] = path
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and p.name != MANIFEST}
    if actual != set(verified):
        raise BackupError("backup contents do not match its manifest")
    if "database/teacher.db" not in verified:
        raise BackupError("backup has no SQLite database")
    if not isinstance(manifest.get("qdrant_collection"), str) or not manifest["qdrant_collection"]:
        raise BackupError("backup manifest has no Qdrant collection name")
    snapshots = [name for name in verified if name.startswith("qdrant/") and name.endswith(".snapshot")]
    expected = 1 if manifest.get("qdrant_collection_present", True) else 0
    if len(snapshots) != expected:
        raise BackupError("backup Qdrant snapshot does not match its collection presence marker")
    if expected and snapshots != ["qdrant/collection.snapshot"]:
        raise BackupError("backup Qdrant snapshot has an unexpected path")
    allowed = (
        "database/teacher.db",
        "qdrant/collection.snapshot",
        "components/model-update-records.json",
    )
    if any(
        name not in allowed
        and not name.startswith(("config/ingestion/", "config/prompts/", "config/models/", "generated/ocr_cache/"))
        for name in verified
    ):
        raise BackupError("backup contains an unexpected payload path")
    database = sqlite3.connect(verified["database/teacher.db"])
    try:
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        database.close()
    if integrity != "ok":
        raise BackupError(f"backup SQLite database failed integrity check: {integrity}")
    return manifest, verified


def live_database_is_backupable(path: Path) -> bool:
    """Return whether a live SQLite database can safely serve as a restore undo point."""
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"refusing to replace a non-file or symlink SQLite database: {path}")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) as database:
            result = database.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        message = str(exc).lower()
        if any(marker in message for marker in ("not a database", "malformed", "disk image")):
            return False
        raise BackupError(f"could not inspect live SQLite database for an undo point: {exc}") from exc
    except sqlite3.Error as exc:
        raise BackupError(f"could not inspect live SQLite database for an undo point: {exc}") from exc
    return bool(result and result[0] == "ok")


def install_database(source_path: Path, target_path: Path) -> None:
    """Build the restored database beside the target, then atomically replace it."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".teacher-db-restore-", dir=target_path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source = sqlite3.connect(source_path, timeout=30)
        target = sqlite3.connect(temporary, timeout=30)
        try:
            source.backup(target)
            tables = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "auth_sessions" in tables:
                target.execute("DELETE FROM auth_sessions")
            target.commit()
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise BackupError(f"restored SQLite database failed integrity check: {result}")
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
            source.close()
        temporary.chmod(0o600)
        # Writers must be stopped during restore. Discard sidecars for the old database so
        # they cannot be replayed against the newly installed database after restart.
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(target_path) + suffix).unlink(missing_ok=True)
        os.replace(temporary, target_path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_config_restore_targets(settings: Settings) -> None:
    config_root = settings.stack_dir / "config"
    if config_root.is_symlink():
        raise BackupError(f"refusing to restore config through a symlink: {config_root}")
    for name in ("ingestion", "prompts", "models"):
        target = config_root / name
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise BackupError(f"refusing to restore config into a non-directory or symlink: {target}")


def clear_config_restore_targets(settings: Settings) -> None:
    config_root = settings.stack_dir / "config"
    for name in ("ingestion", "prompts", "models"):
        target = config_root / name
        if not target.exists():
            continue
        for child in target.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)


def apply_backup(settings: Settings, folder: Path, manifest: dict, files: dict[str, Path]) -> None:
    collection = manifest["qdrant_collection"]
    if collection != settings.collection:
        raise BackupError(
            f"backup collection {collection!r} differs from configured collection {settings.collection!r}; "
            "restore with the matching configuration"
        )
    validate_config_restore_targets(settings)
    source_db = files["database/teacher.db"]
    install_database(source_db, settings.db_path)

    clear_config_restore_targets(settings)
    for relative, source_path in files.items():
        if relative.startswith("config/"):
            parts = PurePosixPath(relative).parts
            if len(parts) < 2 or parts[0] != "config" or parts[1] not in {"ingestion", "prompts", "models"}:
                raise BackupError(f"unexpected config path in backup: {relative}")
            destination = settings.stack_dir.joinpath(*parts)
        elif relative.startswith("generated/ocr_cache/"):
            cache = Path(os.environ.get("OCR_CACHE_HOST_PATH", settings.data_dir / "metadata" / "ocr_cache")).expanduser()
            destination = cache / Path(*PurePosixPath(relative).parts[2:])
        elif relative == "components/model-update-records.json":
            destination = settings.data_dir / relative
        else:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source_path, destination)

    if manifest.get("qdrant_collection_present", True):
        snapshot = files["qdrant/collection.snapshot"]
        qdrant_restore(settings, collection, snapshot)
    else:
        qdrant_delete(settings, collection)


def restore_backup(settings: Settings, folder: Path, *, confirm: bool) -> tuple[Path, Path | None]:
    if not confirm:
        raise BackupError("restore overwrites live data; pass --confirm-live-data after stopping api and ingestion")
    manifest, files = verify_backup(folder)
    settings.validate_destination()
    rollback = None
    rollback_manifest = None
    rollback_files = None
    if live_database_is_backupable(settings.db_path):
        rollback = create_backup(settings, retention=False, allow_missing_collection=True)
        rollback_manifest, rollback_files = verify_backup(rollback)
    try:
        apply_backup(settings, folder, manifest, files)
    except Exception as restore_error:
        if rollback is None:
            raise BackupError(
                f"restore failed and no undo point was available because the live SQLite database was missing or corrupt: "
                f"{restore_error}"
            ) from restore_error
        try:
            apply_backup(settings, rollback, rollback_manifest, rollback_files)
        except Exception as rollback_error:  # noqa: BLE001 - any restore failure must trigger the saved undo point
            raise BackupError(
                f"restore failed ({restore_error}); automatic rollback also failed ({rollback_error}); "
                f"manual rollback backup: {rollback}"
            ) from restore_error
        raise BackupError(f"restore failed and live state was rolled back: {restore_error}; rollback backup: {rollback}") from restore_error
    prune_backups(settings.destination, settings.keep)
    return folder, rollback


def latest_backup(destination: Path) -> Path:
    backups = [path for path in destination.glob("backup-*") if path.is_dir() and not path.is_symlink()]
    if not backups:
        raise BackupError(f"no backups found in {destination}")
    return max(backups, key=lambda item: item.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--destination", default="")
    parser.add_argument("--transport", choices=("compose", "direct"), default="")
    parser.add_argument("--backup", type=Path, default=None, help="backup folder; restore uses the latest by default")
    parser.add_argument("--confirm-live-data", action="store_true", help="acknowledge that restore replaces current state")
    args = parser.parse_args()
    try:
        settings = Settings(args.env_file, args)
        if args.action == "backup":
            path = create_backup(settings)
            print(f"Backup created: {path}")
        else:
            folder = args.backup or latest_backup(settings.destination)
            if not folder.is_absolute():
                folder = settings.destination / folder
            restored, rollback = restore_backup(settings, folder.resolve(), confirm=args.confirm_live_data)
            print(f"Restored: {restored}")
            if rollback:
                print(f"Undo point: {rollback}")
            else:
                print("Undo point: unavailable (live SQLite database was missing or corrupt)")
    except (BackupError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
