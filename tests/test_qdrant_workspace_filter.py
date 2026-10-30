import importlib.util
from pathlib import Path


def _load_real_qdrant_store():
    root = Path(__file__).resolve().parents[1]
    path = root / "services" / "common" / "teacher_common" / "qdrant_store.py"
    spec = importlib.util.spec_from_file_location("teacher_qdrant_store_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeQdrant:
    def __init__(self):
        self.search_args = None
        self.delete_args = None

    def search(self, **kwargs):
        self.search_args = kwargs
        return []

    def delete(self, **kwargs):
        self.delete_args = kwargs


def test_every_vector_search_and_delete_has_workspace_filter(monkeypatch):
    store = _load_real_qdrant_store()
    client = FakeQdrant()
    monkeypatch.setattr(store, "get_client", lambda: client)

    assert store.search([0.0], limit=5, workspace_ids={"user:anya"}, source_paths=["/library/a.pdf"]) == []
    conditions = {condition.key: condition.match for condition in client.search_args["query_filter"].must}
    assert conditions["workspace_id"].any == ["user:anya"]
    assert conditions["source_path"].any == ["/library/a.pdf"]

    store.delete_points_for_source("/library/a.pdf")
    conditions = {condition.key: condition.match for condition in client.delete_args["points_selector"].filter.must}
    assert conditions["source_path"].value == "/library/a.pdf"

    previous_delete = client.delete_args
    store.delete_points_for_workspace("user:anya")
    conditions = {condition.key: condition.match for condition in client.delete_args["points_selector"].filter.must}
    assert list(conditions) == ["workspace_id"]
    assert conditions["workspace_id"].value == "user:anya"
    assert client.delete_args is not previous_delete

    previous_search = client.search_args
    assert store.search([0.0], limit=5, workspace_ids=set()) == []
    assert client.search_args is previous_search
