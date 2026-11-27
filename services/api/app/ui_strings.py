"""UI strings of the application shell (home screen, navigation, placeholders).

Hungarian is the only language today. Strings are looked up by key through ``t()`` (a Jinja global) and the
``shell_strings()`` dict that is handed to shell.js, so a later UI-language pack only has to add another
dictionary here and pick it per request. Panel text inside the section templates is not routed through
this table yet; new sections should use ``{{ t("key") }}`` for what they add.
"""

HU: dict[str, str] = {
    "app.title": "Teacher Agent",
    "app.eyebrow": "Saját tanulósegéd",
    "home.title": "Magyar tanulósegéd a saját könyvtáradból",
    "home.lead": "Válassz, mit szeretnél csinálni.",
    "nav.home": "Főoldal",
    "nav.back_home": "← Főoldal",
    "nav.modes": "Módok",
    "nav.account": "Fiók",
    "account.signed_in": "Bejelentkezve:",
    "account.logout": "Kijelentkezés",
    "soon.badge": "hamarosan",
    "soon.title": "Hamarosan",
    "soon.body": "Ez a rész még készül. Itt fog megjelenni, amint elkészült.",
    "activity.label": "Folyamatban lévő feladatok",
    "activity.resume": "Folytatás",
    "activity.indexing": "Indexelés fut",
    "activity.docintel": "Dokumentumelemzés fut",
    "activity.listen": "Hallgatás",
    "mode.chat.title": "Beszélgetés",
    "mode.chat.summary": "Kérdezz a könyvtárad dokumentumairól, forrásokkal.",
    "mode.study.title": "Tanulás és beszéd",
    "mode.study.summary": "Kvízek, villámkártyák, ismétlés.",
    "mode.docs.title": "Dokumentumok",
    "mode.docs.summary": "Összefoglaló, kigyűjtés, összehasonlítás, fordítás.",
    "mode.media.title": "Média",
    "mode.media.summary": "Fénykép szövege és fordítása.",
    "mode.interpreter.title": "Tolmács",
    "mode.interpreter.summary": "Magyar–angol tolmácsolás.",
    "mode.meetings.title": "Meetingek",
    "mode.meetings.summary": "Jegyzőkönyvek és teendők hangfelvételből.",
    "mode.library.title": "Könyvtár",
    "mode.library.summary": "Hallgatás, kizárt dokumentumok.",
    "mode.admin.title": "Admin",
    "mode.admin.summary": "Felhasználók, munkamenetek, indexelés, napló.",
    "mode.account.title": "Fiók",
    "mode.account.summary": "Saját munkamenetek.",
    "index.title": "Könyvtár indexelése",
    "index.loading": "Állapot lekérése…",
    "index.unavailable": "Az állapot most nem érhető el.",
    "index.idle": "Még nem indult indexelés.",
    "index.completed": "Az indexelés befejeződött.",
    "index.failed": "Az indexelés sikertelen.",
    "index.interrupted": "Az indexelés megszakadt.",
    "index.skipped": "Az indexelés kimaradt.",
    "index.started": "Kezdés",
    "index.total": "Átvizsgált fájlok",
    "index.indexed": "Indexelt fájlok",
}

LANGUAGES: dict[str, dict[str, str]] = {"hu": HU}
DEFAULT_LANGUAGE = "hu"


def t(key: str, lang: str = DEFAULT_LANGUAGE) -> str:
    table = LANGUAGES.get(lang, HU)
    return table.get(key) or HU.get(key) or key


# The subset shell.js needs in the browser (it never builds markup from anything else).
SHELL_KEYS = ("nav.home", "activity.resume", "activity.indexing", *(key for key in HU if key.startswith("index.")))


def shell_strings(lang: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    return {key: t(key, lang) for key in SHELL_KEYS}
