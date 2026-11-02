import importlib.util
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
