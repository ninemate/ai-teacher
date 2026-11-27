"""Registry of the web UI's modes (home screen tiles) and the sections inside them.

A *mode* is one tile on the home screen and one screen of its own (``#/<mode id>``). A *section* is a panel
inside a mode: its own template part, JS and CSS. index.html renders everything from this registry, so a feature
adds itself here and in its own files, and does not edit index.html.

Add a section to an existing mode (e.g. Language Tutor under "study"):

1. ``templates/sections/<id>.html``: a ``<section class="panel" id="<id>">`` with its own markup. Ids inside
   it should be prefixed (``lt-...``) so they cannot collide.
2. ``static/<id>.js`` and ``static/<id>.css`` (both optional). The JS is a deferred classic script wrapped in an
   IIFE; it may use ``window.Teacher`` (see static/shell.js) and the ``csrf-token`` meta tag.
3. One ``Section(...)`` line in ``SECTIONS`` below. Deep link: ``#/<mode>/<section id>``.

Add a mode: a ``Mode(...)`` line in ``MODES`` (title and summary strings go in ui_strings.py as
``mode.<id>.title`` / ``mode.<id>.summary``). A mode with no section shows the "hamarosan" placeholder and
its tile is marked as upcoming; when its first section is registered the placeholder goes away by itself.

A mode with ``tile=False`` has no home tile and no entry in the mode bar; it is reached by a link of its own (the
account screen, from the user name in the header) and is left out while it has no visible section.
``signed_in`` sections are left out when there is no session (authentication switched off).

``admin_only`` modes (and their sections) are neither rendered nor linked for other roles; the routes behind
them must still check ``_require_admin`` themselves, hiding the UI is not access control.

Shared UI contract inside the page (shell.js): ``Teacher.activity.set(key, {label, href} | null)`` shows a
long-running job in the bar under the header on every mode, so its state stays visible and resumable.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    id: str  # url slug: #/<id>
    icon: str
    admin_only: bool = False
    tile: bool = True  # False: no home tile or mode-bar entry, see the module docstring


@dataclass(frozen=True)
class Section:
    id: str  # DOM id of the section element and deep-link anchor
    mode: str
    template: str  # relative to templates/
    title_key: str = ""  # optional ui_strings key; shown in the mode's section jump list when a mode has several
    css: tuple[str, ...] = ()  # file names under /static/
    js: tuple[str, ...] = ()
    signed_in: bool = False  # only rendered for a signed-in session


MODES: tuple[Mode, ...] = (
    Mode("chat", "💬"),
    Mode("study", "🎓"),
    Mode("docs", "📄"),
    Mode("media", "🎧"),
    Mode("interpreter", "🗣"),
    Mode("meetings", "👥"),
    Mode("library", "📚"),
    Mode("admin", "🛠", admin_only=True),
    Mode("account", "👤", tile=False),
)

# Order inside a mode is the order here. Existing panels keep their element ids and scripts unchanged.
SECTIONS: tuple[Section, ...] = (
    Section("chat", "chat", "sections/chat.html", js=("voice.js",)),
    Section("tutor", "study", "sections/tutor.html", css=("tutor.css",), js=("tutor-voice.js", "tutor.js")),
    Section("study", "study", "sections/study.html", css=("study.css",), js=("study.js",)),
    Section("speech", "study", "sections/speech.html", css=("speech.css",), js=("speech.js",)),
    Section("docintel", "docs", "sections/docintel.html", css=("docintel.css",), js=("docintel.js",)),
    Section("media", "media", "sections/media.html", css=("media.css",), js=("media.js",)),
    Section("meetings", "meetings", "sections/meetings.html", css=("meetings.css",), js=("meetings.js",)),
    Section("admin-panel", "admin", "sections/admin.html", css=("admin.css",), js=("admin.js",)),
    Section("indexing", "admin", "sections/indexing.html", js=("indexing.js",)),
    Section("excluded", "library", "sections/excluded.html"),
    Section("listen", "library", "sections/listen.html", css=("listen.css",), js=("listen.js",)),
    Section(
        "account-sessions", "account", "sections/account_sessions.html",
        css=("admin.css",), js=("admin.js",), signed_in=True,
    ),
)


def visible_modes(role: str | None, signed_in: bool = False) -> list[dict]:
    """Modes for the home screen and navigation, each with its sections, for one role."""
    is_admin = role == "admin"
    result = []
    for mode in MODES:
        if mode.admin_only and not is_admin:
            continue
        sections = [s for s in SECTIONS if s.mode == mode.id and (signed_in or not s.signed_in)]
        if not mode.tile and not sections:
            continue
        result.append({"id": mode.id, "icon": mode.icon, "tile": mode.tile, "sections": sections, "soon": not sections})
    return result


def assets(modes: list[dict]) -> tuple[list[str], list[str]]:
    """CSS and JS file names of the visible sections, in registry order, without duplicates."""
    css: list[str] = []
    js: list[str] = []
    for mode in modes:
        for section in mode["sections"]:
            css.extend(f for f in section.css if f not in css)
            js.extend(f for f in section.js if f not in js)
    return css, js
