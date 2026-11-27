# Web UI: home screen and modes

The UI is one server-rendered page (`templates/index.html`) with vanilla JS. It opens on a home screen of
tiles; each tile is a *mode* with its own screen. Routing is by URL hash, so deep links and the browser back
button work:

- `#/` home
- `#/<mode>` a mode, for example `#/docs`
- `#/<mode>/<section id>` a mode scrolled to one section, for example `#/library/listen`
- Existing `#di-job=<id>`, `#listen-<id>` and `#meeting=<id>` links open the result or document in its mode.
  The sign-in form carries the requested fragment through authentication.

Modes: `chat` (Beszélgetés), `study` (Tanulás és beszéd), `docs` (Dokumentumok), `media` (Média), `interpreter`
(Tolmács), `meetings` (Meetingek), `library` (Könyvtár), `admin` (Admin, admin role only). A mode without a section
shows a "Hamarosan" placeholder.

Where the existing panels went: chat to Beszélgetés; study and the Language Tutor to Tanulás és beszéd; photo text recognition and translation to Média; meeting transcripts to Meetingek; the admin panel (with the indexing status) to Admin; own sessions to the Fiók screen (`#/account`, opened from the user name in the header, no home tile); Document Intelligence to
Dokumentumok; read-aloud (listen) and the excluded-documents list to Könyvtár, because both work on the indexed
library itself, while Média is kept for audio and image material that is not a library document.

## Long jobs stay visible

Modes are hidden, not unloaded, so a running job, an audio player or a half-filled form carries on when you open
another mode. The bar under the header lists running work on every screen with a one-tap link back
(`Teacher.activity.set(key, {label, href})`): Document Intelligence jobs, the open listening document (playing or
paused, resumes at the saved position), and indexing (admin only). The indexing link opens `#/admin/indexing`, where an admin can see
the start time and status of the latest run. File counts are available after the run finishes.

## Adding a feature

Everything is registered in `services/api/app/modes.py`; index.html needs no edit.

1. Section: `templates/sections/<id>.html` (a `<section class="panel" id="<id>">`; prefix element ids),
   optional `static/<id>.js` (deferred, IIFE) and `static/<id>.css`.
2. One `Section("<id>", "<mode id>", "sections/<id>.html", css=(...), js=(...))` line in `SECTIONS`.
3. A new mode: a `Mode(...)` line in `MODES` plus `mode.<id>.title` / `mode.<id>.summary` in `ui_strings.py`.
4. Admin-only modes set `admin_only=True`; the routes must still call `_require_admin`.
   A mode with `tile=False` has no home tile (it needs a link of its own); a section with `signed_in=True` is
   left out when there is no session.

Scripts may use `window.Teacher` (`activity.set`, `go`, `onShow`), documented at the top of `static/shell.js`.

## Strings

Shell strings live in `ui_strings.py` (Hungarian, looked up with `t("key")` in templates). A UI-language pack would
add another dictionary there; the existing panel text is still inline Hungarian.

## Looking at it

`python scripts/ui_demo_server.py` serves the UI at http://127.0.0.1:8765 without Qdrant, Ollama or login.
