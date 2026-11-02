import importlib.metadata
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("component_inventory", ROOT / "scripts" / "component_inventory.py")
assert spec and spec.loader
component_inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(component_inventory)


def test_inventory_refuses_non_loopback_ollama_fallback(tmp_path, monkeypatch):
    settings = component_inventory.backup_restore.Settings(tmp_path / "empty-env")
    settings.transport = "direct"
    settings.env["OLLAMA_BASE_URL"] = "https://models.example.invalid"

    def fail_if_contacted(*_args, **_kwargs):
        pytest.fail("inventory must not contact a remote Ollama endpoint")

    monkeypatch.setattr(component_inventory.urllib.request, "urlopen", fail_if_contacted)
    models, status = component_inventory.ollama_models(settings)

    assert models == []
    assert "no remote endpoint was contacted" in status


def test_inventory_marks_image_status_unknown_when_compose_query_fails(tmp_path, monkeypatch):
    settings = component_inventory.backup_restore.Settings(tmp_path / "empty-env")
    settings.transport = "compose"
    monkeypatch.setattr(component_inventory, "REQUIREMENTS", ("api",))
    monkeypatch.setattr(component_inventory, "requirement_lines", lambda _service: [("example-package", "==1.2.3")])
    monkeypatch.setattr(
        component_inventory,
        "compose_python",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "service is not running"),
    )

    def missing_package(_name):
        raise importlib.metadata.PackageNotFoundError("example-package")

    monkeypatch.setattr(component_inventory.importlib.metadata, "distribution", missing_package)
    [row] = component_inventory.installed_packages(settings)

    assert row["version"] == "not found on inventory host; image status unavailable"
    assert row["location"] == "host fallback; api image metadata unavailable"
    assert row["license"] == "not checked"
