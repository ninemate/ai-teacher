"""scripts/check-library.sh: a share that is absent, empty or lacks the watched folders must fail."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-library.sh"


@pytest.fixture()
def library(tmp_path):
    root = tmp_path / "lib"
    (root / "books").mkdir(parents=True)
    (root / "books" / "a.txt").write_text("a")
    config = tmp_path / "ingestion.yml"
    config.write_text("watch:\n  folders:\n    - path: books\n      workspace: legacy  # comment\n")
    return root, config


def check(root, config, tmp_path, **env):
    full = {"PATH": "/usr/bin:/bin", "REQUIRE_MOUNTPOINT": "0", "INGEST_CONFIG_PATH": str(config), **env}
    return subprocess.run(
        ["sh", str(SCRIPT), str(root)], cwd=tmp_path, env=full, capture_output=True, text=True, check=False
    )


def test_healthy_library_passes(library, tmp_path):
    root, config = library
    result = check(root, config, tmp_path)
    assert result.returncode == 0, result.stdout
    assert "watched folder books" in result.stdout


def test_missing_mount_point_fails(library, tmp_path):
    _, config = library
    assert check(tmp_path / "nope", config, tmp_path).returncode == 1


def test_empty_mount_point_fails(library, tmp_path):
    _, config = library
    empty = tmp_path / "empty"
    empty.mkdir()
    result = check(empty, config, tmp_path)
    assert result.returncode == 1
    assert "is empty" in result.stdout and "watched folder books missing" in result.stdout


def test_missing_watched_folder_fails(library, tmp_path):
    root, config = library
    (root / "books" / "a.txt").unlink()
    (root / "books").rmdir()
    (root / "other").mkdir()
    (root / "other" / "x").write_text("x")
    result = check(root, config, tmp_path)
    assert result.returncode == 1 and "watched folder books missing" in result.stdout


def test_sentinel_is_required_when_configured(library, tmp_path):
    root, config = library
    assert check(root, config, tmp_path, LIBRARY_SENTINEL=".mounted").returncode == 1
    (root / ".mounted").write_text("")
    assert check(root, config, tmp_path, LIBRARY_SENTINEL=".mounted").returncode == 0


def test_plain_folder_is_not_a_mount_point(library, tmp_path):
    root, config = library
    result = check(root, config, tmp_path, REQUIRE_MOUNTPOINT="1")
    assert result.returncode == 1 and "not a mount point" in result.stdout
