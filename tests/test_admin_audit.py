"""Admin panel API and audit log: role enforcement, one audit entry per event type, no secrets logged."""

import re
import time
import uuid

import pytest
from conftest import PASSWORDS
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from teacher_common.db import session_scope
from teacher_common.models import AuditEvent, AuthSession, DocumentRecord, ExcludedDocument, Workspace


def _client(api_main):
    return TestClient(api_main.app, follow_redirects=False)


def _login(api_main, username, password=None):
    api_main.login_limiter.reset()
    client = _client(api_main)
    form = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    response = client.post("/login", data={"username": username, "password": password or PASSWORDS[username],
                                           "csrf_token": form})
    if response.status_code != 303:
        return client, None, response
    return client, {"X-CSRF-Token": client.get("/session").json()["csrf_token"]}, response


def _mark():
    with session_scope() as db:
        return db.execute(select(func.coalesce(func.max(AuditEvent.id), 0))).scalar_one()


def _events(since):
    with session_scope() as db:
        rows = db.execute(select(AuditEvent).where(AuditEvent.id > since).order_by(AuditEvent.id)).scalars().all()
        return [(r.event, r.actor, r.target, r.source_ip, r.timestamp) for r in rows]


def _one(events, name):
    found = [e for e in events if e[0] == name]
    assert found, f"no {name} event in {[e[0] for e in events]}"
    return found[-1]


@pytest.fixture
def roles(api_main, monkeypatch):
    monkeypatch.setattr(api_main, "auth_admins", {"apa"})
    with session_scope() as db:
        db.query(AuthSession).delete()


def _admin_routes(api_main):
    return [(m, r.path) for r in api_main.app.routes if isinstance(r, APIRoute)
            and (r.path.startswith("/admin/") or r.path.startswith("/ingest/")) for m in r.methods]


def test_every_admin_endpoint_refuses_non_admins(api_main, roles):
    client, headers, _ = _login(api_main, "anya")
    routes = _admin_routes(api_main)
    assert {("GET", "/admin/audit"), ("GET", "/admin/sessions"), ("DELETE", "/admin/sessions/{session_id}"),
            ("POST", "/admin/exclusions/{excluded_id}/restore"), ("GET", "/admin/users"), ("GET", "/admin/workspaces"),
            ("GET", "/admin/ingest"), ("GET", "/admin/exclusions"), ("GET", "/admin/inventory")} <= set(routes)
    start = _mark()
    for method, path in routes:
        url = path.replace("{session_id}", "abc").replace("{excluded_id}", "1")
        response = client.request(method, url, headers=headers, json={})
        assert response.status_code == 403, (method, url, response.status_code)
    assert client.get("/admin/audit", params={"format": "csv"}).status_code == 403
    denied = [e for e in _events(start) if e[0] == "admin.denied"]
    assert len(denied) >= len(routes) and all(e[1] == "anya" for e in denied)


def test_admin_endpoints_need_a_session(api_main, roles):
    anonymous = _client(api_main)
    for method, path in _admin_routes(api_main):
        assert anonymous.request(method, path).status_code in (401, 403), (method, path)


def test_admin_panel_rendered_only_for_admins(api_main, roles):
    user, _, _ = _login(api_main, "anya")
    page = user.get("/", headers={"Accept": "text/html"}).text
    assert 'id="admin-panel"' not in page and 'id="account-sessions"' in page
    admin, _, _ = _login(api_main, "apa")
    assert 'id="admin-panel"' in admin.get("/", headers={"Accept": "text/html"}).text


def test_login_logout_and_failures_are_audited_without_secrets(api_main, roles):
    start = _mark()
    wrong = "rossz-" + uuid.uuid4().hex
    _, _, failed = _login(api_main, "anya", password=wrong)
    assert failed.status_code == 401
    client = _client(api_main)
    form = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    typo = "ismeretlen-" + uuid.uuid4().hex
    assert client.post("/login", data={"username": typo, "password": wrong, "csrf_token": form}).status_code == 401
    client, headers, ok = _login(api_main, "anya")
    assert ok.status_code == 303
    cookie = client.cookies.get("teacher_session")
    assert client.post("/logout", headers=headers).status_code == 200
    events = _events(start)
    failures = [e for e in events if e[0] == "login.failure"]
    assert [e[1] for e in failures] == ["anya", None]  # unknown usernames are not recorded (could be a password)
    assert _one(events, "login.success")[1] == "anya"
    assert _one(events, "logout")[1] == "anya"
    assert all(e[3] for e in events)  # source IP recorded
    dump = repr(events)
    for secret in (wrong, typo, PASSWORDS["anya"], cookie, headers["X-CSRF-Token"], form):
        assert secret not in dump


def test_session_expiry_and_revocation(api_main, roles):
    start = _mark()
    expiring, _, _ = _login(api_main, "anya")
    with session_scope() as db:
        for row in db.execute(select(AuthSession).where(AuthSession.username == "anya")).scalars():
            row.expires_at = int(time.time()) - 1
    assert expiring.get("/session").status_code == 401
    assert _one(_events(start), "session.expired")[1] == "anya"

    phone, _, _ = _login(api_main, "anya")
    laptop, laptop_headers, _ = _login(api_main, "anya")
    own = laptop.get("/account/sessions").json()
    assert len(own["active"]) == 2 and sum(s["current"] for s in own["active"]) == 1
    assert any(r["event"] == "login.success" for r in own["recent"])
    assert laptop.post("/account/sessions/revoke-others", headers=laptop_headers).json() == {"revoked": 1}
    assert phone.get("/session").status_code == 401
    assert laptop.get("/session").status_code == 200
    revoked = _one(_events(start), "session.revoked")
    assert revoked[1] == "anya" and revoked[2] == "anya"


def test_admin_revokes_a_session(api_main, roles):
    victim, _, _ = _login(api_main, "anya")
    admin, headers, _ = _login(api_main, "apa")
    sessions = admin.get("/admin/sessions").json()
    assert {s["username"] for s in sessions} == {"anya", "apa"}
    cookie = victim.cookies.get("teacher_session")
    assert all(cookie not in str(s) for s in sessions)  # only opaque ids leave the server
    target = next(s for s in sessions if s["username"] == "anya")
    start = _mark()
    assert admin.delete(f"/admin/sessions/{target['id']}", headers=headers).status_code == 200
    assert admin.delete(f"/admin/sessions/{target['id']}", headers=headers).status_code == 404
    assert victim.get("/session").status_code == 401
    event = _one(_events(start), "admin.session_revoke")
    assert event[1:3] == ("apa", "anya")
    assert any(r["event"] == "admin.session_revoke" for r in _login(api_main, "anya")[0].get("/account/sessions").json()["recent"])


def test_workspace_and_document_events(api_main, roles, monkeypatch):
    owner, headers, _ = _login(api_main, "anya")
    admin, admin_headers, _ = _login(api_main, "apa")
    start = _mark()
    workspace = owner.post("/workspaces", json={"name": "Titkos projekt"}, headers=headers).json()["id"]
    assert owner.post(f"/workspaces/{workspace}/shares", json={"username": "apa", "permission": "read"},
                      headers=headers).status_code in (200, 201)
    assert owner.delete(f"/workspaces/{workspace}/shares/apa", headers=headers).status_code == 204

    title = "Bizalmas cím " + uuid.uuid4().hex
    path = f"/library/private-{uuid.uuid4().hex}.pdf"
    with session_scope() as db:
        doc = DocumentRecord(workspace_id=workspace, source_path=path, source_hash="x", file_type="pdf",
                             size_bytes=1, modified_timestamp=1, title=title)
        db.add(doc)
        db.flush()
        document_id = doc.id
    assert owner.delete(f"/sources/{document_id}", headers=headers).status_code == 204
    with session_scope() as db:
        excluded = db.execute(select(ExcludedDocument).where(ExcludedDocument.workspace_id == workspace)).scalar_one().id
    assert owner.post(f"/sources/excluded/{excluded}/restore", headers=headers).status_code == 204

    # Delete again and let the admin restore it from the panel.
    with session_scope() as db:
        doc = DocumentRecord(workspace_id=workspace, source_path=path, source_hash="x", file_type="pdf",
                             size_bytes=1, modified_timestamp=1, title=title)
        db.add(doc)
        db.flush()
        second_id = doc.id
    assert owner.delete(f"/sources/{second_id}", headers=headers).status_code == 204
    listed = next(x for x in admin.get("/admin/exclusions").json() if x["workspace_id"] == workspace)
    assert admin.post(f"/admin/exclusions/{listed['id']}/restore", headers=admin_headers).status_code == 200
    with session_scope() as db:
        assert db.get(ExcludedDocument, listed["id"]) is None

    monkeypatch.setattr(api_main, "_ingestion_request", lambda *a, **k: {"status": "started"})
    assert admin.post("/ingest/reindex", json={}, headers=admin_headers).status_code == 200

    events = _events(start)
    assert _one(events, "workspace.create")[1:3] == ("anya", workspace)
    assert _one(events, "workspace.share")[1:3] == ("anya", workspace)
    assert _one(events, "workspace.unshare")[1:3] == ("anya", workspace)
    assert _one(events, "document.delete")[1:3] == ("anya", str(second_id))
    assert _one(events, "document.exclude")[1] == "anya"
    assert _one(events, "document.restore")[1:3] == ("anya", str(excluded))
    assert _one(events, "admin.exclusion_restore")[1] == "apa"
    assert _one(events, "admin.reindex")[1] == "apa"
    dump = repr(events)
    assert title not in dump and path not in dump and "Titkos projekt" not in dump
    with session_scope() as db:  # other test modules expect only personal workspaces
        db.query(DocumentRecord).filter(DocumentRecord.workspace_id == workspace).delete()
        db.query(ExcludedDocument).filter(ExcludedDocument.workspace_id == workspace).delete()
        db.delete(db.get(Workspace, workspace))


def test_admin_views_list_state(api_main, roles):
    admin, _, _ = _login(api_main, "apa")
    users = admin.get("/admin/users").json()
    assert {"username": "apa", "role": "admin"} in users and {"username": "anya", "role": "user"} in users
    assert all(set(u) == {"username", "role"} for u in users)  # never password hashes
    with session_scope() as db:
        names = {w.id for w in db.execute(select(Workspace)).scalars()}
    assert {w["id"] for w in admin.get("/admin/workspaces").json()} == names
    assert isinstance(admin.get("/admin/ingest").json(), list)
    inventory = admin.get("/admin/inventory").json()
    assert inventory["configured_models"]["chat"] and inventory["api_packages"]


def test_audit_filters_and_csv_export(api_main, roles):
    admin, headers, _ = _login(api_main, "apa")
    _login(api_main, "anya")
    rows = admin.get("/admin/audit", params={"event": "login.success", "actor": "anya"}).json()
    assert rows and all(r["event"] == "login.success" and r["actor"] == "anya" for r in rows)
    newest = rows[0]["timestamp"]
    assert admin.get("/admin/audit", params={"since": newest + 10}).json() == []
    assert admin.get("/admin/audit", params={"limit": 1}).json().__len__() == 1
    assert admin.get("/admin/audit", params={"format": "xml"}).status_code == 422
    # Prefix filter: "admin." lists every admin action.
    admin.post("/admin/exclusions/999999/restore", headers=headers)
    admin.get("/admin/audit", params={"format": "csv"})
    prefixed = admin.get("/admin/audit", params={"event": "admin."}).json()
    assert prefixed and all(r["event"].startswith("admin.") for r in prefixed)

    with session_scope() as db:
        db.add(AuditEvent(timestamp=int(time.time()), event="test.formula", actor="x", target="=HYPERLINK(1)"))
    csv = admin.get("/admin/audit", params={"event": "test.formula", "format": "csv"})
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv.headers["content-disposition"]
    assert csv.text.splitlines()[0] == "id,timestamp,event,actor,target,source_ip"
    assert "'=HYPERLINK(1)" in csv.text  # spreadsheet formula injection neutralised
    assert _one(_events(0), "admin.audit_export")[1] == "apa"


def test_retention_prunes_old_events(api_main, monkeypatch):
    import audit

    old = int(time.time()) - 400 * 86400
    with session_scope() as db:
        db.add(AuditEvent(timestamp=old, event="test.old"))
        db.add(AuditEvent(timestamp=int(time.time()), event="test.new"))
    with session_scope() as db:
        audit.prune(db)
    with session_scope() as db:
        left = {e.event for e in db.execute(select(AuditEvent).where(AuditEvent.event.like("test.%"))).scalars()}
    assert "test.old" not in left and "test.new" in left

    monkeypatch.setattr(audit, "get_settings", lambda: type("S", (), {"audit_retention_days": 0})())
    with session_scope() as db:
        db.add(AuditEvent(timestamp=old, event="test.kept"))
    with session_scope() as db:
        audit.prune(db)
    with session_scope() as db:
        assert db.execute(select(AuditEvent).where(AuditEvent.event == "test.kept")).scalar_one_or_none()
