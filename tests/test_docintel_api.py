"""Document Intelligence through the API: background jobs, stored results, isolation, restart, cancel, stale results."""

import re
import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from docintel_fakes import FakeModel, make_doc
from fastapi.testclient import TestClient
from sqlalchemy import select
from teacher_common import docintel_store
from teacher_common.config import get_settings
from teacher_common.db import session_scope
from teacher_common.docintel_models import DocIntelJob, DocIntelPart
from teacher_common.models import DocumentRecord, WorkspaceShare
from teacher_common.workspaces import ensure_personal_workspace

PASSWORDS = {"apa": "helyes-jelszo-123", "anya": "masik-jelszo-456"}
TEXT = [
    "Követelmény: A pályázónak ötéves tapasztalat szükséges. Határidő: 2026. március 1-ig.",
    "Kötelezettség: A pályázó évente jelentést készít. Teendő: A jelentést el kell küldeni.",
]


def login(api_main, username):
    api_main.login_limiter.reset()
    client = TestClient(api_main.app, follow_redirects=False)
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    answer = client.post("/login", data={"username": username, "password": PASSWORDS[username], "csrf_token": token})
    assert answer.status_code == 303
    client.headers["X-CSRF-Token"] = client.get("/session").json()["csrf_token"]
    return client


@pytest.fixture
def env(api_main, monkeypatch):
    """Two accounts with one document each, a fake model, and a scroll that serves each document's own chunks."""
    monkeypatch.setattr(api_main, "auth_admins", set())
    suffix = uuid.uuid4().hex[:8]
    docs = {}
    with session_scope() as session:
        for user in PASSWORDS:
            workspace = ensure_personal_workspace(session, user)
            doc = make_doc(f"{user}-{suffix}.pdf", TEXT)
            session.add(DocumentRecord(
                workspace_id=workspace.id, source_path=doc["source_path"], source_hash=f"h-{user}-{suffix}",
                file_type="pdf", size_bytes=1, modified_timestamp=1, title=f"{user} dokumentuma",
                status="indexed", chunk_count=len(TEXT),
            ))
            docs[user] = {**doc, "workspace_id": workspace.id}

    def scroll(source_path, workspace_ids):
        for doc in docs.values():
            if doc["source_path"] == source_path and doc["workspace_id"] in workspace_ids:
                return [{**c, "workspace_id": doc["workspace_id"]} for c in doc["chunks"]]
        return []

    model = FakeModel()
    worker = api_main.docintel_worker
    monkeypatch.setattr(worker, "scroll", scroll)
    monkeypatch.setattr(worker, "llm", model)
    worker.run_pending()  # nothing from an earlier test is left in the queue
    return SimpleNamespace(api=api_main, docs=docs, model=model, worker=worker, apa=login(api_main, "apa"),
                           anya=login(api_main, "anya"), suffix=suffix)


def start(client, doc, kind="extract", **extra):
    answer = client.post("/docintel/jobs", json={"kind": kind, "source_paths": [doc["source_path"]], **extra})
    assert answer.status_code == 202, answer.text
    return answer.json()


# --- the job lifecycle ---------------------------------------------------------------------------------------------


def test_a_job_is_queued_then_runs_in_the_background_and_its_result_is_stored(env):
    job = start(env.apa, env.docs["apa"])
    assert job["status"] == "queued" and job["kind"] == "extract" and job["resumable"] is False
    # no result yet
    assert env.apa.get(f"/docintel/jobs/{job['id']}/result").status_code == 409

    assert env.worker.run_pending() == 1
    done = env.apa.get(f"/docintel/jobs/{job['id']}").json()
    assert done["status"] == "succeeded" and done["error"] is None
    assert done["progress"]["done"] == done["progress"]["total"] >= 1
    assert done["started_at"] and done["finished_at"]

    body = env.apa.get(f"/docintel/jobs/{job['id']}/result").json()
    result = body["result"]
    assert body["job"]["id"] == job["id"] and result["kind"] == "extract"
    assert {r["type"] for r in result["rows"]} == {"requirement", "deadline", "obligation"}
    assert all(r["sources"] for r in result["rows"])
    assert result["citations"][0]["file_name"].startswith("apa-") and result["citations"][0]["page"] == 1
    # the parts of a finished job are gone; the result is all that stays
    with session_scope() as session:
        assert session.execute(select(DocIntelPart).where(DocIntelPart.job_id == job["id"])).first() is None

    csv_export = env.apa.get(f"/docintel/jobs/{job['id']}/export?format=csv")
    assert csv_export.status_code == 200 and csv_export.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_export.headers["content-disposition"]
    assert "apa-" in csv_export.text and "1. oldal" in csv_export.text
    md_export = env.apa.get(f"/docintel/jobs/{job['id']}/export?format=md")
    assert md_export.status_code == 200 and "| Típus |" in md_export.text
    assert env.apa.get(f"/docintel/jobs/{job['id']}/export?format=txt").status_code == 422  # txt is for translations
    assert any(j["id"] == job["id"] for j in env.apa.get("/docintel/jobs").json())


def test_every_kind_runs_through_the_api(env):
    doc = env.docs["apa"]
    jobs = {
        "summary": start(env.apa, doc, "summary", granularity="document"),
        "tasks": start(env.apa, doc, "tasks"),
        "translate": start(env.apa, doc, "translate", direction="hu-en"),
    }
    assert env.worker.run_pending() == 3
    for kind, job in jobs.items():
        body = env.apa.get(f"/docintel/jobs/{job['id']}/result").json()
        assert body["job"]["status"] == "succeeded" and body["result"]["kind"] == kind
        assert body["result"]["citations"] and body["result"]["grounding"]["status"] in ("grounded", "partial")
    assert env.apa.get(f"/docintel/jobs/{jobs['translate']['id']}/export?format=txt").text.startswith("[EN]")


def test_compare_job_takes_two_documents_of_the_person(env):
    with session_scope() as session:
        other = make_doc(f"apa-masodik-{env.suffix}.pdf", TEXT[:1])
        session.add(DocumentRecord(
            workspace_id=env.docs["apa"]["workspace_id"], source_path=other["source_path"], source_hash="h-2",
            file_type="pdf", size_bytes=1, modified_timestamp=1, title="Második", status="indexed", chunk_count=1))
    env.docs["apa2"] = {**other, "workspace_id": env.docs["apa"]["workspace_id"]}
    answer = env.apa.post("/docintel/jobs", json={
        "kind": "compare", "mode": "gap",
        "source_paths": [env.docs["apa"]["source_path"], other["source_path"]]})
    assert answer.status_code == 202
    env.worker.run_pending()
    result = env.apa.get(f"/docintel/jobs/{answer.json()['id']}/result").json()["result"]
    assert result["mode"] == "gap" and result["documents"][0].startswith("apa-")


def test_requests_are_validated(env):
    doc = env.docs["apa"]
    post = env.apa.post
    assert post("/docintel/jobs", json={"kind": "compare", "source_paths": [doc["source_path"]]}).status_code == 422
    assert post("/docintel/jobs", json={"kind": "translate", "source_paths": [doc["source_path"]]}).status_code == 422
    assert post("/docintel/jobs", json={"kind": "extract", "source_paths": [doc["source_path"]],
                                        "types": ["titok"]}).status_code == 422
    assert post("/docintel/jobs", json={"kind": "summary", "source_paths": []}).status_code == 422
    assert post("/docintel/jobs", json={"kind": "summary", "source_paths": ["/library/nincs.pdf"]}).status_code == 404


def test_queue_is_limited_per_person(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "docintel_max_queued_jobs", 2)
    start(env.apa, env.docs["apa"])
    start(env.apa, env.docs["apa"])
    answer = env.apa.post("/docintel/jobs", json={"kind": "extract", "source_paths": [env.docs["apa"]["source_path"]]})
    assert answer.status_code == 429
    env.worker.run_pending()


# --- model trouble, cancel, restart --------------------------------------------------------------------------------


def test_unusable_model_output_ends_as_a_clear_failure_that_can_be_retried(env):
    env.model.answer_extract = lambda prompt: "ez nem JSON"
    job = start(env.apa, env.docs["apa"])
    env.worker.run_pending()
    failed = env.apa.get(f"/docintel/jobs/{job['id']}").json()
    assert failed["status"] == "failed" and "nem adott használható kimenetet" in failed["error"]
    assert failed["resumable"] is True
    assert env.apa.get(f"/docintel/jobs/{job['id']}/result").status_code == 409  # nothing broken is shown

    del env.model.answer_extract  # the model works again
    assert env.apa.post(f"/docintel/jobs/{job['id']}/resume").json()["status"] == "queued"
    env.worker.run_pending()
    assert env.apa.get(f"/docintel/jobs/{job['id']}").json()["status"] == "succeeded"


def test_model_unreachable_fails_the_job_but_keeps_it_resumable(env):
    env.model.script = [httpx.ConnectError("down")]
    job = start(env.apa, env.docs["apa"])
    env.worker.run_pending()
    failed = env.apa.get(f"/docintel/jobs/{job['id']}").json()
    assert failed["status"] == "failed" and "nyelvi modell" in failed["error"] and failed["resumable"]


def test_a_queued_job_can_be_cancelled(env):
    job = start(env.apa, env.docs["apa"])
    assert env.apa.post(f"/docintel/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert env.worker.run_pending() == 0
    assert env.model.calls == []
    assert env.apa.post(f"/docintel/jobs/{job['id']}/cancel").status_code == 409


def test_a_running_job_stops_when_cancelled(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "docintel_window_chars", 250)  # one window per page: several steps
    job = start(env.apa, env.docs["apa"], "summary")
    original = env.model.answer_summary_map

    def cancel_during_the_first_call(prompt):
        assert env.apa.post(f"/docintel/jobs/{job['id']}/cancel").json()["cancel_requested"] is True
        return original(prompt)

    env.model.answer_summary_map = cancel_during_the_first_call
    env.worker.run_pending()
    stopped = env.apa.get(f"/docintel/jobs/{job['id']}").json()
    assert stopped["status"] == "cancelled"
    assert env.apa.get(f"/docintel/jobs/{job['id']}/result").status_code == 409
    assert env.apa.delete(f"/docintel/jobs/{job['id']}").status_code == 204


@pytest.mark.parametrize("kind, extra, answer", [
    ("extract", {}, "answer_extract"),
    ("translate", {"direction": "hu-en"}, "answer_translate_hu_en"),
])
def test_cancelling_during_the_last_model_call_does_not_end_as_succeeded(env, kind, extra, answer):
    """A one-window job has no later step to notice the cancel; the end of the job must check it too."""
    job = start(env.apa, env.docs["apa"], kind, **extra)
    original = getattr(env.model, answer)

    def cancel_during_the_only_call(prompt):
        assert env.apa.post(f"/docintel/jobs/{job['id']}/cancel").json()["cancel_requested"] is True
        return original(prompt)

    setattr(env.model, answer, cancel_during_the_only_call)
    env.worker.run_pending()
    assert len([c for c in env.model.calls if c[0].startswith(kind)]) == 1
    stopped = env.apa.get(f"/docintel/jobs/{job['id']}").json()
    assert stopped["status"] == "cancelled"
    assert env.apa.get(f"/docintel/jobs/{job['id']}/result").status_code == 409


def test_a_job_left_running_by_a_restart_becomes_interrupted_and_resumes(env):
    job = start(env.apa, env.docs["apa"], "summary")
    with session_scope() as session:
        claimed = docintel_store.claim_next(session)
        assert claimed["id"] == job["id"]
        docintel_store.save_part(session, job["id"], "s0:0", {"items": [], "rejected": 0})
    # the process dies here: the row says running, nobody runs it
    assert env.apa.get(f"/docintel/jobs/{job['id']}").json()["status"] == "running"
    fresh = type(env.worker)(settings=env.worker.settings, llm=env.model, scroll=env.worker.scroll,
                             workspace_ids_for=env.worker.workspace_ids_for, prompt_dir=env.worker.prompt_dir)
    fresh.start()  # what the API does at startup
    fresh.stop()
    interrupted = env.apa.get(f"/docintel/jobs/{job['id']}").json()
    assert interrupted["status"] == "interrupted" and interrupted["resumable"] and "újraindult" in interrupted["error"]
    with session_scope() as session:
        assert not session.execute(select(DocIntelJob).where(DocIntelJob.status == "running")).first()
        assert docintel_store.load_parts(session, job["id"]) == {"s0:0": {"items": [], "rejected": 0}}

    assert env.apa.post(f"/docintel/jobs/{job['id']}/resume").json()["status"] == "queued"
    env.worker.run_pending()
    assert env.apa.get(f"/docintel/jobs/{job['id']}").json()["status"] == "succeeded"


def test_only_one_model_job_runs_at_a_time(env):
    active, peak = [], []
    original = env.model.__call__

    def watched(prompt, json_mode):
        active.append(1)
        peak.append(len(active))
        try:
            return original(prompt, json_mode)
        finally:
            active.pop()

    env.worker.llm = watched
    for _ in range(3):
        start(env.apa, env.docs["apa"])
    env.worker.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        statuses = {j["status"] for j in env.apa.get("/docintel/jobs").json() if j["status"] in ("queued", "running")}
        if not statuses:
            break
        time.sleep(0.05)
    env.worker.stop()
    assert statuses == set() and max(peak) == 1


def test_background_model_calls_wait_for_chat(api_main, monkeypatch):
    calls = []

    class Client:
        def __init__(self, timeout):
            calls.append(timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"response": "kész"})

    monkeypatch.setattr(api_main, "httpx", SimpleNamespace(Client=Client, HTTPError=httpx.HTTPError))
    finished = threading.Event()

    def background():
        api_main.call_ollama("feladat", background=True)
        finished.set()

    with api_main.chat_priority.interactive():  # a chat request is in flight
        thread = threading.Thread(target=background)
        thread.start()
        assert not finished.wait(0.8)
    assert finished.wait(5)
    thread.join()
    assert calls == [get_settings().docintel_llm_timeout_seconds]


# --- who may see what ----------------------------------------------------------------------------------------------


def test_every_route_needs_a_login_and_a_csrf_token(env):
    job = start(env.apa, env.docs["apa"])
    env.worker.run_pending()
    anonymous = TestClient(env.api.app, follow_redirects=False)
    base = f"/docintel/jobs/{job['id']}"
    for method, path in (("GET", "/docintel/jobs"), ("POST", "/docintel/jobs"), ("GET", base), ("GET", base + "/result"),
                         ("GET", base + "/export"), ("POST", base + "/cancel"), ("POST", base + "/resume"),
                         ("DELETE", base)):
        assert anonymous.request(method, path, json={}).status_code == 401, (method, path)
    no_csrf = TestClient(env.api.app, follow_redirects=False)
    no_csrf.cookies.update(env.apa.cookies)
    assert no_csrf.post(base + "/cancel").status_code == 403
    assert no_csrf.delete(base).status_code == 403


def test_jobs_and_results_are_private_to_their_owner(env):
    job = start(env.apa, env.docs["apa"], "translate", direction="hu-en")
    env.worker.run_pending()
    base = f"/docintel/jobs/{job['id']}"
    assert env.apa.get(base + "/result").status_code == 200

    anya = env.anya
    assert job["id"] not in {j["id"] for j in anya.get("/docintel/jobs").json()}
    for method, path in (("GET", base), ("GET", base + "/result"), ("GET", base + "/export?format=md"),
                         ("GET", base + "/export?format=txt"), ("POST", base + "/cancel"),
                         ("POST", base + "/resume"), ("DELETE", base)):
        assert anya.request(method, path).status_code == 404, (method, path)
    # nor can she start a job on his document, by path
    stolen = anya.post("/docintel/jobs", json={"kind": "extract", "source_paths": [env.docs["apa"]["source_path"]]})
    assert stolen.status_code == 404
    assert env.apa.get(base).json()["status"] == "succeeded"  # her attempts changed nothing


def test_sharing_a_personal_workspace_does_not_share_its_jobs(env):
    job = start(env.apa, env.docs["apa"], "translate", direction="hu-en")
    env.worker.run_pending()
    base = f"/docintel/jobs/{job['id']}"
    with session_scope() as session:
        session.add(WorkspaceShare(workspace_id=env.docs["apa"]["workspace_id"], username="anya", permission="write"))
    try:
        assert env.anya.get("/docintel/jobs").json() == []
        for method, path in (("GET", base), ("GET", base + "/result"), ("GET", base + "/export?format=txt"),
                             ("POST", base + "/cancel"), ("POST", base + "/resume"), ("DELETE", base)):
            assert env.anya.request(method, path).status_code == 404, (method, path)
        # the shared document itself is readable, so she can make a job of her own from it
        own = start(env.anya, env.docs["apa"])
        assert [j["id"] for j in env.anya.get("/docintel/jobs").json()] == [own["id"]]
        assert own["id"] not in {j["id"] for j in env.apa.get("/docintel/jobs").json()}
    finally:
        with session_scope() as session:
            session.query(WorkspaceShare).filter(WorkspaceShare.workspace_id == env.docs["apa"]["workspace_id"],
                                                 WorkspaceShare.username == "anya").delete()
    assert env.apa.get(base).json()["status"] == "succeeded"


def test_a_shared_document_can_be_read_but_the_result_stays_with_the_reader(env):
    with session_scope() as session:
        session.add(WorkspaceShare(workspace_id=env.docs["apa"]["workspace_id"], username="anya", permission="read"))
    try:
        job = start(env.anya, env.docs["apa"])
        env.worker.run_pending()
        assert env.anya.get(f"/docintel/jobs/{job['id']}/result").status_code == 200
        assert env.apa.get(f"/docintel/jobs/{job['id']}").status_code == 404  # her result is hers, not the owner's
        # when the share ends, a resumed job cannot read the document any more
        failed_job = start(env.anya, env.docs["apa"])
    finally:
        with session_scope() as session:
            session.query(WorkspaceShare).filter(WorkspaceShare.workspace_id == env.docs["apa"]["workspace_id"],
                                                 WorkspaceShare.username == "anya").delete()
    env.worker.run_pending()
    failed = env.anya.get(f"/docintel/jobs/{failed_job['id']}").json()
    assert failed["status"] == "failed" and "már nem érhető el" in failed["error"]


# --- source documents going away, and the index staying clean ------------------------------------------------------


def test_deleting_a_source_marks_its_results_stale_and_stops_unfinished_jobs(env, monkeypatch):
    import teacher_common.qdrant_store as qdrant_store

    monkeypatch.setattr(qdrant_store, "delete_points_for_source", lambda path: None)
    done = start(env.apa, env.docs["apa"], "translate", direction="hu-en")
    env.worker.run_pending()
    waiting = start(env.apa, env.docs["apa"])
    document_id = next(s["id"] for s in env.apa.get("/sources").json() if s["source_path"] == env.docs["apa"]["source_path"])

    assert env.apa.get(f"/docintel/jobs/{done['id']}").json()["stale"] is False
    assert env.apa.delete(f"/sources/{document_id}").status_code == 204

    stale = env.apa.get(f"/docintel/jobs/{done['id']}").json()
    assert stale["stale"] is True and "törölték" in stale["stale_reason"]
    # kept and still downloadable: it is the person's own result, deleted only on their say-so
    assert env.apa.get(f"/docintel/jobs/{done['id']}/export?format=txt").status_code == 200
    cancelled = env.apa.get(f"/docintel/jobs/{waiting['id']}").json()
    assert cancelled["status"] == "cancelled" and "törölték" in cancelled["error"]
    assert env.worker.run_pending() == 0


def test_a_changed_source_makes_the_result_stale(env):
    job = start(env.apa, env.docs["apa"])
    env.worker.run_pending()
    with session_scope() as session:
        session.query(DocumentRecord).filter(DocumentRecord.source_path == env.docs["apa"]["source_path"]).update(
            {"source_hash": "megváltozott"})
    changed = env.apa.get(f"/docintel/jobs/{job['id']}").json()
    assert changed["stale"] and "megváltozott" in changed["stale_reason"]


def test_a_translation_is_not_added_to_the_index(env, monkeypatch):
    import teacher_common.qdrant_store as qdrant_store

    upserts = []
    monkeypatch.setattr(qdrant_store, "upsert_points", lambda points: upserts.append(points))
    sources_before = env.apa.get("/sources").json()
    job = start(env.apa, env.docs["apa"], "translate", direction="hu-en")
    env.worker.run_pending()
    assert env.apa.get(f"/docintel/jobs/{job['id']}").json()["status"] == "succeeded"
    assert env.apa.get("/sources").json() == sources_before
    assert upserts == []


def test_deleting_a_job_removes_it_and_its_result(env):
    job = start(env.apa, env.docs["apa"])
    env.worker.run_pending()
    assert env.apa.delete(f"/docintel/jobs/{job['id']}").status_code == 204
    assert env.apa.get(f"/docintel/jobs/{job['id']}").status_code == 404
    with session_scope() as session:
        from teacher_common.docintel_models import DocIntelResult

        assert session.get(DocIntelResult, job["id"]) is None
