import json
import sqlite3

import pytest

from scripts import backup_restore


def settings(tmp_path, monkeypatch):
    env_file = tmp_path / "empty-env"
    result = backup_restore.Settings(env_file)
    result.data_dir = tmp_path / "data"
    result.db_path = tmp_path / "data" / "metadata" / "teacher.db"
    result.destination = tmp_path / "backups"
    result.library = tmp_path / "library"
    result.stack_dir = tmp_path / "stack"
    result.collection = "unit_test"
    result.transport = "direct"
    result.qdrant_url = "http://127.0.0.1:6333"
    result.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(result.db_path) as db:
        db.executescript(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, title TEXT);"
            "CREATE TABLE quizzes (id INTEGER PRIMARY KEY, title TEXT);"
            "CREATE TABLE auth_sessions (token_hash TEXT PRIMARY KEY);"
            "INSERT INTO documents VALUES (1, 'Original document');"
            "INSERT INTO quizzes VALUES (1, 'Original quiz');"
            "INSERT INTO auth_sessions VALUES ('old-session');"
        )
    return result


def fake_qdrant(monkeypatch, present):
    state = {"present": present}

    def snapshot(_settings, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"qdrant snapshot")

    def restore(_settings, _collection, path):
        assert path.read_bytes() == b"qdrant snapshot"
        state["present"] = True

    def delete(_settings, _collection):
        state["present"] = False

    monkeypatch.setattr(backup_restore, "qdrant_present", lambda *_: state["present"])
    monkeypatch.setattr(backup_restore, "qdrant_snapshot", snapshot)
    monkeypatch.setattr(backup_restore, "qdrant_restore", restore)
    monkeypatch.setattr(backup_restore, "qdrant_delete", delete)
    return state


def test_restore_recovers_database_and_keeps_undo_point(tmp_path, monkeypatch):
    config = settings(tmp_path, monkeypatch)
    config_file = config.stack_dir / "config" / "prompts" / "teacher.md"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("original prompt", encoding="utf-8")
    fake_qdrant(monkeypatch, True)
    backup = backup_restore.create_backup(config)
    assert (backup / "manifest.json").is_file()
    assert (backup / "config/prompts/teacher.md").read_text() == "original prompt"
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / "database/teacher.db").stat().st_mode & 0o777 == 0o600
    assert (backup / "qdrant/collection.snapshot").stat().st_mode & 0o777 == 0o600

    config_file.write_text("changed prompt", encoding="utf-8")
    stale_config = config.stack_dir / "config" / "models" / "after-backup.yml"
    stale_config.parent.mkdir(parents=True)
    stale_config.write_text("model: unapproved", encoding="utf-8")
    with sqlite3.connect(config.db_path) as db:
        db.execute("DELETE FROM documents")
        db.execute("UPDATE quizzes SET title = 'Changed quiz'")
    backup_restore.restore_backup(config, backup, confirm=True)

    with sqlite3.connect(config.db_path) as db:
        assert db.execute("SELECT title FROM documents").fetchone() == ("Original document",)
        assert db.execute("SELECT title FROM quizzes").fetchone() == ("Original quiz",)
        assert db.execute("SELECT count(*) FROM auth_sessions").fetchone() == (0,)
    assert config_file.read_text() == "original prompt"
    assert not stale_config.exists()
    assert len(list(config.destination.glob("backup-*"))) == 2


def test_restore_checks_every_checksum_before_live_changes(tmp_path, monkeypatch):
    config = settings(tmp_path, monkeypatch)
    fake_qdrant(monkeypatch, True)
    backup = backup_restore.create_backup(config)
    (backup / "database/teacher.db").write_bytes(b"tampered")
    with pytest.raises(backup_restore.BackupError, match="checksum or size mismatch"):
        backup_restore.restore_backup(config, backup, confirm=True)
    with sqlite3.connect(config.db_path) as db:
        assert db.execute("SELECT title FROM documents").fetchone() == ("Original document",)
    assert len(list(config.destination.glob("backup-*"))) == 1


def test_restore_requires_explicit_confirmation(tmp_path, monkeypatch):
    config = settings(tmp_path, monkeypatch)
    fake_qdrant(monkeypatch, True)
    backup = backup_restore.create_backup(config)
    with pytest.raises(backup_restore.BackupError, match="pass --confirm-live-data"):
        backup_restore.restore_backup(config, backup, confirm=False)


@pytest.mark.parametrize("live_state", ["missing", "corrupt"])
def test_restore_recovers_when_live_database_cannot_be_backed_up(tmp_path, monkeypatch, live_state):
    config = settings(tmp_path, monkeypatch)
    fake_qdrant(monkeypatch, True)
    backup = backup_restore.create_backup(config)
    if live_state == "missing":
        config.db_path.unlink()
    else:
        config.db_path.write_bytes(b"not a sqlite database")

    restored, undo = backup_restore.restore_backup(config, backup, confirm=True)

    assert restored == backup
    assert undo is None
    with sqlite3.connect(config.db_path) as db:
        assert db.execute("SELECT title FROM documents").fetchone() == ("Original document",)
        assert db.execute("SELECT title FROM quizzes").fetchone() == ("Original quiz",)
        assert db.execute("SELECT count(*) FROM auth_sessions").fetchone() == (0,)


def test_failed_restore_reapplies_the_undo_point(tmp_path, monkeypatch):
    config = settings(tmp_path, monkeypatch)
    fake_qdrant(monkeypatch, True)
    backup = backup_restore.create_backup(config)
    with sqlite3.connect(config.db_path) as db:
        db.execute("UPDATE documents SET title = 'Live state to preserve'")

    real_restore = backup_restore.qdrant_restore
    calls = 0

    def fail_once(settings, collection, path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise backup_restore.BackupError("simulated snapshot import failure")
        real_restore(settings, collection, path)

    monkeypatch.setattr(backup_restore, "qdrant_restore", fail_once)
    with pytest.raises(backup_restore.BackupError, match="live state was rolled back"):
        backup_restore.restore_backup(config, backup, confirm=True)

    with sqlite3.connect(config.db_path) as db:
        assert db.execute("SELECT title FROM documents").fetchone() == ("Live state to preserve",)
    assert calls == 2


def test_destination_cannot_be_inside_read_only_library(tmp_path, monkeypatch):
    config = settings(tmp_path, monkeypatch)
    config.destination = config.library / "backups"
    with pytest.raises(backup_restore.BackupError, match="outside the read-only library"):
        config.validate_destination()


def test_backup_manifest_checksums_cover_payloads(tmp_path, monkeypatch):
    config = settings(tmp_path, monkeypatch)
    fake_qdrant(monkeypatch, True)
    backup = backup_restore.create_backup(config)
    manifest, files = backup_restore.verify_backup(backup)
    assert "database/teacher.db" in files
    assert manifest["qdrant_collection_present"] is True
    assert json.loads((backup / "manifest.json").read_text())["files"] == manifest["files"]


def test_backup_retention_keeps_only_the_newest_n(tmp_path, monkeypatch):
    config = settings(tmp_path, monkeypatch)
    config.keep = 2
    fake_qdrant(monkeypatch, True)
    created = [backup_restore.create_backup(config) for _ in range(3)]
    remaining = sorted(config.destination.glob("backup-*"))
    assert len(remaining) == 2
    assert created[0] not in remaining
    assert remaining == created[1:]
