"""Home screen and mode navigation: the registry drives what index.html renders."""

import re

import modes
import ui_strings
from test_api_auth import client, login  # noqa: F401  (client is a fixture)

MODE_IDS = ["chat", "study", "docs", "media", "interpreter", "meetings", "library", "admin"]  # home tiles
SIGNED_IN_VIEWS = [*MODE_IDS, "account"]  # plus the account screen, reached from the user name


def page_for(client, api_main, monkeypatch, user: str, admins: set[str]) -> str:
    monkeypatch.setattr(api_main, "auth_admins", admins)
    assert login(client, user).status_code == 303
    response = client.get("/")
    assert response.status_code == 200
    return response.text


def view_ids(html: str) -> list[str]:
    return re.findall(r'<section class="mode" id="mode-([a-z]+)"', html)


def test_user_sees_every_mode_but_admin(api_main, client, monkeypatch):
    html = page_for(client, api_main, monkeypatch, "apa", admins=set())
    assert view_ids(html) == [m for m in SIGNED_IN_VIEWS if m != "admin"]
    assert "Admin" not in re.findall(r'class="tile-title">([^<]+)', html)
    assert "#/admin" not in html
    assert 'id="indexing"' not in html and 'id="admin-panel"' not in html
    assert "/static/indexing.js" not in html


def test_admin_sees_admin_mode(api_main, client, monkeypatch):
    html = page_for(client, api_main, monkeypatch, "apa", admins={"apa"})
    assert view_ids(html) == SIGNED_IN_VIEWS
    assert 'data-role="admin"' in html
    assert 'id="admin-panel"' in html
    assert 'id="indexing"' in html
    assert "/static/indexing.js" in html


def test_existing_panels_live_in_their_modes(api_main, client, monkeypatch):
    html = page_for(client, api_main, monkeypatch, "apa", admins=set())
    by_mode = dict(zip(view_ids(html), re.split(r'<section class="mode" id="mode-[a-z]+"', html)[1:]))
    for mode, element_ids in {
        "chat": ["ask-button", "answer", "sources"],
        "study": ["tutor-title", "study-gen-quiz", "study-list", "sp-controls"],
        "docs": ["di-start", "di-jobs"],
        "media": ["media-camera", "media-process"],
        "interpreter": ["ip-talk-hu", "ip-transcript"],
        "meetings": ["mt-upload", "mt-jobs"],
        "library": ["listen-play", "excluded-documents-list"],
        "account": ["account-load", "account-revoke"],
    }.items():
        for element_id in element_ids:
            assert f'id="{element_id}"' in by_mode[mode], (mode, element_id)
    tiles = re.findall(r'class="tile[^"]*" href="#/([a-z]+)"', html)
    assert "account" not in tiles and 'href="#/account"' in html
    for mode, part in by_mode.items():
        assert "Hamarosan" not in part, mode


def test_assets_of_visible_sections_are_loaded_once(api_main, client, monkeypatch):
    html = page_for(client, api_main, monkeypatch, "apa", admins=set())
    scripts = re.findall(r'<script src="/static/([^"]+)"', html)
    assert scripts[:2] == ["shell.js", "app.js"]
    for name in ["voice.js", "tutor-voice.js", "tutor.js", "study.js", "speech.js", "docintel.js", "listen.js", "interpreter.js", "meetings.js", "media.js", "admin.js"]:
        assert scripts.count(name) == 1
    assert 'href="/static/listen.css"' in html and 'href="/static/tutor.css"' in html


def test_registry_is_consistent():
    assert [m.id for m in modes.MODES] == SIGNED_IN_VIEWS
    assert [m.id for m in modes.MODES if m.tile] == MODE_IDS
    mode_ids = {m.id for m in modes.MODES}
    assert {s.mode for s in modes.SECTIONS} <= mode_ids
    assert len({s.id for s in modes.SECTIONS}) == len(modes.SECTIONS)
    assert not mode_ids & {s.id for s in modes.SECTIONS} - {"chat", "study", "meetings", "media", "interpreter"}  # "mode-<id>" wrappers keep ids apart
    for mode in modes.MODES:
        assert f"mode.{mode.id}.title" in ui_strings.HU and f"mode.{mode.id}.summary" in ui_strings.HU


def test_unknown_language_falls_back_to_hungarian():
    assert ui_strings.t("nav.home", "de") == "Főoldal"
    assert ui_strings.t("no.such.key") == "no.such.key"


def test_account_screen_needs_a_session():
    shown = modes.visible_modes("user", signed_in=False)
    assert "account" not in [m["id"] for m in shown]
    assert "account" in [m["id"] for m in modes.visible_modes("user", signed_in=True)]


def test_mode_without_sections_shows_soon(monkeypatch):
    monkeypatch.setattr(modes, "SECTIONS", tuple(s for s in modes.SECTIONS if s.mode != "interpreter"))
    shown = {m["id"]: m for m in modes.visible_modes("user", signed_in=True)}
    assert shown["interpreter"]["soon"] and not shown["chat"]["soon"]
