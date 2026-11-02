import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

UnexpectedResponse = pytest.importorskip("qdrant_client.http.exceptions").UnexpectedResponse


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

    store.delete_points_for_source("/library/a.pdf", workspace_id="user:anya")
    conditions = {condition.key: condition.match for condition in client.delete_args["points_selector"].filter.must}
    assert conditions["source_path"].value == "/library/a.pdf"
    assert conditions["workspace_id"].value == "user:anya"

    store.delete_points_for_source("/library/a.pdf", workspace_id="legacy")
    query_filter = client.delete_args["points_selector"].filter
    conditions = {condition.key: condition.match for condition in query_filter.must}
    assert conditions["source_path"].value == "/library/a.pdf"
    assert query_filter.should[0].match.value == "legacy"
    assert query_filter.should[1].is_empty.key == "workspace_id"

    previous_delete = client.delete_args
    store.delete_points_for_workspace("user:anya")
    conditions = {condition.key: condition.match for condition in client.delete_args["points_selector"].filter.must}
    assert list(conditions) == ["workspace_id"]
    assert conditions["workspace_id"].value == "user:anya"
    assert client.delete_args is not previous_delete

    previous_search = client.search_args
    assert store.search([0.0], limit=5, workspace_ids=set()) == []
    assert client.search_args is previous_search


class RacingQdrant:
    """Answers like Qdrant when another service created the collection between the check and the create."""

    def __init__(self, status):
        self.status = status

    def get_collections(self):
        return SimpleNamespace(collections=[])

    def create_collection(self, **kwargs):
        raise UnexpectedResponse(self.status, "conflict", b"already exists", httpx.Headers())


def test_ensure_collection_survives_losing_the_race_with_the_other_service(monkeypatch):
    store = _load_real_qdrant_store()
    monkeypatch.setattr(store, "embedding_dimension", lambda: 4)
    monkeypatch.setattr(store, "get_client", lambda: RacingQdrant(409))
    store.ensure_collection()

    monkeypatch.setattr(store, "get_client", lambda: RacingQdrant(500))
    with pytest.raises(UnexpectedResponse):
        store.ensure_collection()
