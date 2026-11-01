#!/usr/bin/env python3
"""The end-to-end trial's steps, run against a live stack through its HTTPS proxy. Started by scripts/e2e.sh.

Standard library only, so it needs nothing on the host but python3. It logs in the way scripts/smoke-chat.sh does.
A small CPU model answers badly, so nothing here reads the model's wording: the assertions are about structure,
retrieval, citations and storage. Every step runs even after an earlier one failed, and the result is a table of
PASS, FAIL and SKIP with the reason; the exit status is non-zero when anything failed.
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

ANNA, BELA, CILI, ADMIN = "e2e-anna", "e2e-bela", "e2e-cili", "e2e-admin"
PASSWORD_VARS = {ANNA: "ANNA_PASSWORD", BELA: "BELA_PASSWORD", CILI: "CILI_PASSWORD", ADMIN: "ADMIN_PASSWORD"}

MILL_PDF = "vizimalmok.pdf"
SCAN_PDF = "szkennelt-csillagaszat.pdf"
FISH_EPUB = "halak-konyve.epub"
RECIPE_DOCX = "titkos-receptek.docx"
ANNA_FILES = {MILL_PDF, SCAN_PDF, FISH_EPUB}
BELA_FILES = {RECIPE_DOCX}

# Each fact is on one known page or chapter of one fixture (tests/e2e/make_fixtures.py).
MILL_QUESTION = "Hány lapátja van a kőröshegyi malom kerekének, és ki volt a molnármester?"
SCAN_QUESTION = "Mekkora a gellérthegyi távcső tükrének átmérője, és ki tervezte a műszert?"
FISH_QUESTION = "Hol ívik tavasszal a tiszai tokhal?"
RECIPE_QUESTION = "Mi a bodzás őszibarack lekvár titkos receptje?"
UNRELATED_QUESTION = "Ki nyerte a labdarúgó-világbajnokságot 2018-ban?"
RECIPE_SECRET = "BODZALEKVAR-7731"
SCAN_WORDS = ("gellérthegyi", "távcső", "százhúsz", "Ürögdi", "Győző")
CITATION_FIELDS = {"id", "file_name", "source_path", "page", "chapter", "locator", "label", "score", "ocr", "uncertain"}


class Skip(Exception):
    """The step cannot run here; the message says why."""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """One browser: its own cookies, no automatic redirects, trusts the proxy's internal CA like curl -k."""

    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.cookies: dict[str, str] = {}
        self.csrf = ""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))

    def request(self, method: str, path: str, *, body=None, json_body=None, headers=None, timeout=None) -> Response:
        headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers.setdefault("Content-Type", "application/json")
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        if self.csrf:
            headers.setdefault("X-CSRF-Token", self.csrf)
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as raw:
                response = Response(raw.status, dict(raw.headers.items()), raw.read())
                cookies = raw.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as error:
            response = Response(error.code, dict(error.headers.items()), error.read())
            cookies = error.headers.get_all("Set-Cookie") or []
        for cookie in cookies:
            name, _, value = cookie.split(";", 1)[0].partition("=")
            if value.strip('"'):
                self.cookies[name] = value
            else:
                self.cookies.pop(name, None)
        return response

    def get(self, path: str, **kwargs) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json_body=None, **kwargs) -> Response:
        return self.request("POST", path, json_body=json_body, **kwargs)

    def login(self, username: str, password: str) -> Response:
        """Form token from the login page, form post, then the session's CSRF token (scripts/smoke-chat.sh)."""
        page = self.get("/login").text
        token = re.search(r'name="csrf_token" value="([^"]*)"', page)
        assert token, "the login page has no csrf_token field"
        from urllib.parse import urlencode

        form = urlencode({"username": username, "password": password, "csrf_token": token.group(1)}).encode()
        response = self.request(
            "POST", "/login", body=form, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        if response.status == 303:
            self.csrf = self.get("/session").json()["csrf_token"]
        return response


def expect(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_status(response: Response, status: int, what: str) -> None:
    expect(response.status == status, f"{what}: expected HTTP {status}, got {response.status}: {response.text[:300]}")


def file_name(path: str) -> str:
    return Path(path).name


class Trial:
    def __init__(self, args) -> None:
        self.args = args
        self.results: list[tuple[str, str, str]] = []
        self.library = Path(args.library)
        self.clients: dict[str, Client] = {}
        self.first_run: dict = {}
        self.sources_before: dict[str, list[dict]] = {}
        self.stored: dict = {}

    # --- plumbing ---

    def step(self, title: str, function) -> None:
        print(f"\n--- {title}", flush=True)
        started = time.monotonic()
        try:
            detail = function() or ""
            status = "PASS"
        except Skip as skip:
            status, detail = "SKIP", str(skip)
        except AssertionError as failure:
            status, detail = "FAIL", str(failure)
        except Exception as failure:  # noqa: BLE001 - one broken step must not hide the others
            status, detail = "FAIL", f"{type(failure).__name__}: {failure}"
            traceback.print_exc()
        print(f"{status} ({time.monotonic() - started:.1f}s) {detail}", flush=True)
        self.results.append((title, status, detail))

    def client(self, username: str) -> Client:
        if username not in self.clients:
            client = Client(self.args.base_url, timeout=self.args.model_timeout)
            expect_status(client.login(username, os.environ[PASSWORD_VARS[username]]), 303, f"login of {username}")
            self.clients[username] = client
        return self.clients[username]

    def sources(self, username: str) -> list[dict]:
        response = self.client(username).get("/sources")
        expect_status(response, 200, f"/sources of {username}")
        return response.json()

    def source_names(self, username: str) -> set[str]:
        return {file_name(row["source_path"]) for row in self.sources(username)}

    def chat(self, username: str, question: str) -> dict:
        response = self.client(username).post("/chat", {"question": question, "level": "beginner"})
        expect_status(response, 200, f"/chat of {username}")
        answer = response.json()
        expect({"answer", "sources", "citations", "grounding"} <= answer.keys(), f"chat answer fields: {list(answer)}")
        expect(isinstance(answer["answer"], str) and answer["answer"].strip(), "empty answer")
        for citation in answer["citations"]:
            missing = CITATION_FIELDS - citation.keys()
            expect(not missing, f"citation without {sorted(missing)}")
        return answer

    def index_run(self, **options) -> dict:
        """Trigger a run as admin through the proxy and wait for its counters."""
        admin = self.client(ADMIN)
        deadline = time.monotonic() + self.args.index_timeout
        while True:
            response = admin.post("/ingest/reindex", {"full_reindex": False, "remove_missing": True, **options})
            if response.status != 409:
                break
            expect(time.monotonic() < deadline, "indexing stayed busy")
            time.sleep(2)
        expect_status(response, 200, "POST /ingest/reindex")
        while True:
            state = admin.get("/ingest/schedule").json()
            last = state.get("last_run") or {}
            if not state["running"] and last.get("status") != "running":
                print(f"run: {json.dumps(last, ensure_ascii=False)}")
                return last
            expect(time.monotonic() < deadline, f"indexing did not finish in {self.args.index_timeout}s")
            time.sleep(2)

    def run_command(self, command: str, timeout: float) -> subprocess.CompletedProcess:
        print(f"$ {command}", flush=True)
        return subprocess.run(command, shell=True, text=True, capture_output=True, timeout=timeout, check=False)

    def point_count(self) -> int | None:
        if not self.args.point_count_cmd:
            return None
        result = self.run_command(self.args.point_count_cmd, 120)
        expect(result.returncode == 0, f"point count command failed: {result.stderr[-300:]}")
        return int(result.stdout.strip().splitlines()[-1])

    # --- steps ---

    def proxy_https(self):
        plain = Client(self.args.http_url).get("/health")
        expect(plain.status in (301, 302, 307, 308), f"plain HTTP is not redirected: HTTP {plain.status}")
        location = plain.headers.get("Location", "")
        expect(location.startswith("https://"), f"plain HTTP redirects to {location!r}")
        secure = Client(self.args.base_url).get("/health")
        expect_status(secure, 200, "/health over HTTPS")
        expect(secure.headers.get("X-Content-Type-Options") == "nosniff", "the proxy's security headers are missing")
        root_ca = Client(self.args.http_url).get("/ca.crt")
        expect_status(root_ca, 200, "the internal CA's root over plain HTTP (docs/https.md)")
        expect(b"BEGIN CERTIFICATE" in root_ca.body, "/ca.crt is not a certificate")
        return f"HTTP {plain.status} -> {location}; HTTPS /health is 200; /ca.crt serves the root"

    def login_required(self):
        anonymous = Client(self.args.base_url)
        for method, path, body in (
            ("GET", "/sources", None),
            ("GET", "/workspaces", None),
            ("GET", "/ingest/status", None),
            ("GET", "/study/quizzes", None),
            ("GET", "/study/decks", None),
            ("GET", "/voice/status", None),
            ("POST", "/chat", {"question": "Mi a teszt kérdés?"}),
            ("POST", "/study/quizzes", {"topic": "próba"}),
            ("POST", "/ingest/reindex", {}),
            ("DELETE", "/sources/1", None),
        ):
            response = anonymous.request(method, path, json_body=body)
            expect_status(response, 401, f"anonymous {method} {path}")
        page = anonymous.get("/", headers={"Accept": "text/html"})
        expect_status(page, 303, "anonymous page load")
        expect(page.headers.get("Location", "").startswith("/login"), "page load does not lead to the login")
        expect_status(Client(self.args.base_url).login(ANNA, "wrong-password-123"), 401, "login with a wrong password")
        # A session without its CSRF token may not change anything, and neither may another site.
        anna = self.client(ANNA)
        no_csrf = Client(self.args.base_url)
        no_csrf.cookies = dict(anna.cookies)
        expect_status(no_csrf.post("/chat", {"question": "Mi a teszt kérdés?"}), 403, "POST without a CSRF token")
        cross_site = anna.post("/chat", {"question": "Mi a teszt kérdés?"}, headers={"Origin": "https://evil.example"})
        expect_status(cross_site, 403, "POST from another origin")
        # A regular user may not start or inspect indexing.
        expect_status(anna.post("/ingest/reindex", {}), 403, "reindex by a regular user")
        return "anonymous API 401, pages redirect to login, wrong password 401, no CSRF 403, other origin 403"

    def index_watched_folders(self):
        for username in (ANNA, BELA, CILI):
            self.client(username)  # the first login creates the personal workspace the watched folders point at
        run = self.index_run()
        self.first_run = run
        expect(run.get("status") == "completed", f"run ended as {run.get('status')}: {run.get('message')}")
        expected = len(ANNA_FILES | BELA_FILES)
        expect(run["scanned"] == expected, f"scanned {run['scanned']} of {expected}")
        expect(run["failed"] == 0, f"{run['failed']} failed: {run.get('errors')}")
        expect(run["indexed"] == expected, f"indexed {run['indexed']} of {expected}")
        expect(self.source_names(ANNA) == ANNA_FILES, f"anna has {self.source_names(ANNA)}")
        expect(self.source_names(BELA) == BELA_FILES, f"bela has {self.source_names(BELA)}")
        for username in (ANNA, BELA):
            self.sources_before[username] = self.sources(username)
            for row in self.sources_before[username]:
                expect(row["chunk_count"] > 0, f"{row['source_path']} has no chunks")
        return f"scanned={run['scanned']} indexed={run['indexed']} failed={run['failed']}"

    def ocr_scanned_pdf(self):
        answer = self.chat(ANNA, SCAN_QUESTION)
        expect(answer["citations"], "no citations for a question about the scanned PDF")
        top = answer["citations"][0]
        expect(file_name(top["source_path"]) == SCAN_PDF, f"top citation is {top['label']}")
        expect(top["page"] == 2, f"top citation is on page {top['page']}, the fact is on page 2")
        expect(top["ocr"] is True, "the citation is not marked as OCR")
        expect(isinstance(top["ocr_confidence"], (int, float)), f"OCR confidence is {top['ocr_confidence']!r}")
        # Only OCR can have produced these: the fixture has no text layer. The accents must survive too.
        found = [word for word in SCAN_WORDS if word.lower() in (top["preview"] or "").lower()]
        expect(len(found) >= 3, f"OCR text does not hold the page's words, found {found}: {top['preview']!r}")
        return f"{top['label']}, confidence {top['ocr_confidence']}, recognised {found}"

    def rerun_no_duplicates(self):
        points_before = self.point_count()
        run = self.index_run()
        expect(run.get("status") == "completed", f"run ended as {run.get('status')}: {run.get('message')}")
        expect(run["indexed"] == 0, f"second run indexed {run['indexed']} documents again")
        expect(run["skipped"] == run["scanned"] == len(ANNA_FILES | BELA_FILES), f"second run: {run}")
        for username, before in self.sources_before.items():
            after = self.sources(username)
            expect(
                sorted((r["id"], r["chunk_count"]) for r in after) == sorted((r["id"], r["chunk_count"]) for r in before),
                f"{username}'s documents changed: {before} -> {after}",
            )
        chunks = sum(r["chunk_count"] for rows in self.sources_before.values() for r in rows)
        detail = f"indexed=0 skipped={run['skipped']}, {chunks} chunks"
        points_after = self.point_count()
        if points_after is not None:
            expect(points_after == points_before == chunks, f"points {points_before} -> {points_after}, chunks {chunks}")
            detail += f" = {points_after} Qdrant points"
        answer = self.chat(ANNA, MILL_QUESTION)
        ids = [c["chunk_id"] for c in answer["citations"]]
        expect(len(ids) == len(set(ids)), f"the same chunk is cited twice: {ids}")
        return detail

    def chat_cites_source(self):
        pdf = self.chat(ANNA, MILL_QUESTION)
        expect(pdf["citations"], "no citations")
        top = pdf["citations"][0]
        expect(file_name(top["source_path"]) == MILL_PDF, f"top citation is {top['label']}")
        expect(top["page"] == 2, f"the fact is on page 2, top citation says page {top['page']}")
        expect(top["ocr"] is False, "a text PDF citation is marked as OCR")
        expect("tizennégy" in (top["preview"] or ""), f"cited text is not the page: {top['preview']!r}")
        expect(MILL_PDF in top["label"] and "2. oldal" in top["label"], f"label is {top['label']!r}")
        expect(pdf["grounding"]["status"] in ("grounded", "partial", "no_source"), f"{pdf['grounding']}")
        expect(top["file_name"] == pdf["sources"][0]["file_name"], "sources and citations disagree")
        print(f"model answered: {pdf['answer'][:200]!r} grounding={pdf['grounding']['status']}")

        epub = self.chat(ANNA, FISH_QUESTION)
        expect(epub["citations"], "no citations")
        top_epub = epub["citations"][0]
        expect(file_name(top_epub["source_path"]) == FISH_EPUB, f"top citation is {top_epub['label']}")
        expect("fejezet2" in (top_epub["chapter"] or ""), f"the fact is in chapter 2, citation: {top_epub['chapter']!r}")
        expect("Ikrás-zátony" in (top_epub["preview"] or ""), f"cited text: {top_epub['preview']!r}")
        return f"{top['label']} (score {top['score']:.3f}); {top_epub['label']} (score {top_epub['score']:.3f})"

    def no_source_says_so(self):
        # Acceptance test 5, part one: nothing retrievable at all. The refusal is fixed text, not the model's.
        nothing = self.chat(CILI, MILL_QUESTION)
        expect(nothing["citations"] == [] and nothing["sources"] == [], f"cili got sources: {nothing['citations']}")
        expect(nothing["grounding"]["status"] == "no_source", f"grounding: {nothing['grounding']}")
        expect(nothing["grounding"]["notice"], "no notice to the user")
        expect("nem találtam" in nothing["answer"], f"answer: {nothing['answer']!r}")
        # Part two: documents exist, but none is about the question.
        unrelated = self.chat(ANNA, UNRELATED_QUESTION)
        scores = [round(c["score"], 3) for c in unrelated["citations"]]
        print(f"unrelated question: scores={scores} grounding={unrelated['grounding']}")
        print(f"model answered: {unrelated['answer'][:200]!r}")
        grounding = unrelated["grounding"]
        if unrelated["citations"]:
            expect(grounding["notice"], f"a question with no source got an unqualified answer: {grounding}")
            expect(
                grounding["status"] == "no_source" or grounding["low_confidence"],
                f"a question with no source is presented as grounded: {grounding}, scores {scores}",
            )
        detail = f"empty workspace: fixed refusal. Unrelated question: {grounding['status']}, scores {scores}"
        # Reported, not failed: whether weak matches alone should bring the fixed refusal is undecided.
        if unrelated["citations"]:
            detail += ". OPEN: retrieval has no minimum score, so only the low-confidence notice says it"
        return detail

    def isolation(self):
        anna, bela = self.client(ANNA), self.client(BELA)
        expect(self.source_names(BELA) == BELA_FILES, f"bela lists {self.source_names(BELA)}")
        expect(self.source_names(ANNA) == ANNA_FILES, f"anna lists {self.source_names(ANNA)}")
        for username, question, foreign in (
            (BELA, MILL_QUESTION, ANNA_FILES),
            (BELA, SCAN_QUESTION, ANNA_FILES),
            (ANNA, RECIPE_QUESTION, BELA_FILES),
        ):
            answer = self.chat(username, question)
            cited = {file_name(c["source_path"]) for c in answer["citations"]}
            leaked = cited & foreign
            expect(not leaked, f"{username} got {leaked} from another account")
            sources = {file_name(s["source_path"]) for s in answer["sources"]}
            expect(not sources & foreign, f"{username} got {sources & foreign} in sources")
        recipe = self.chat(ANNA, RECIPE_QUESTION)
        expect(RECIPE_SECRET not in json.dumps(recipe, ensure_ascii=False), "anna's answer holds bela's private text")
        own = self.chat(BELA, RECIPE_QUESTION)
        expect(own["citations"] and file_name(own["citations"][0]["source_path"]) == RECIPE_DOCX, "bela loses his document")

        # Knowing the id or the path of a document gives nothing.
        mill = next(r for r in self.sources(ANNA) if file_name(r["source_path"]) == MILL_PDF)
        expect_status(bela.request("DELETE", f"/sources/{mill['id']}"), 404, "bela deleting anna's document")
        stolen = bela.post("/study/quizzes", {"source_paths": [mill["source_path"]], "count": 1})
        expect_status(stolen, 404, "a quiz by bela from anna's path")
        expect(MILL_PDF in self.source_names(ANNA), "anna's document is gone")
        expect(anna.get("/workspaces").status == 200, "anna's workspaces")
        ids = {row["id"] for row in bela.get("/workspaces").json()}
        expect(f"user:{ANNA}" not in ids, f"bela sees anna's workspace: {ids}")
        return "search, chat, source list, delete by id and study by path all stay inside the account"

    def _generate(self, client: Client, path: str, payload: dict) -> tuple[dict, int]:
        """The model may fail to produce valid items; that is its weakness, so it gets more than one go."""
        failures = []
        for attempt in range(1, self.args.study_tries + 1):
            response = client.post(path, payload)
            if response.status == 201:
                return response.json(), attempt
            expect(response.status == 502, f"POST {path}: HTTP {response.status}: {response.text[:300]}")
            failures.append(response.text[:200])
        raise AssertionError(f"POST {path}: model gave no usable items in {self.args.study_tries} requests: {failures}")

    def quiz_and_flashcards(self):
        anna = self.client(ANNA)
        scope = {"topic": "A kőröshegyi malom története", "level": "beginner", "count": 2}
        quiz, quiz_tries = self._generate(anna, "/study/quizzes", {**scope, "title": "E2E kvíz"})
        expect(quiz["id"] and quiz["questions"], f"quiz: {quiz}")
        for question in quiz["questions"]:
            expect(question["question"] and len(question["options"]) >= 2, f"question: {question}")
        deck, deck_tries = self._generate(anna, "/study/decks", {**scope, "title": "E2E kártyák"})
        expect(deck["id"] and deck["cards"], f"deck: {deck}")
        for card in deck["cards"]:
            expect(card["front"] and card["back"] and card["sources"], f"card: {card}")

        # Stored means readable again by id and in the lists, with what a user did to it.
        expect_status(anna.get(f"/study/quizzes/{quiz['id']}"), 200, "stored quiz")
        expect(any(q["id"] == quiz["id"] for q in anna.get("/study/quizzes").json()), "quiz not listed")
        expect(any(d["id"] == deck["id"] for d in anna.get("/study/decks").json()), "deck not listed")
        # The answer key and sources are released with the first attempt's result.
        answers = {str(q["id"]): 0 for q in quiz["questions"]}
        attempt = anna.post(f"/study/quizzes/{quiz['id']}/attempts", {"answers": answers})
        expect_status(attempt, 201, "quiz attempt")
        graded = attempt.json()
        expect(graded["total"] == len(quiz["questions"]) and 0 <= graded["score"] <= graded["total"], f"{graded}")
        for question in graded["questions"]:
            expect(0 <= question["correct_index"] < len(question["options"]), f"correct_index: {question}")
            expect(question["sources"], f"a question without sources: {question}")
            for source in question["sources"]:
                expect(file_name(source["source_path"]) in ANNA_FILES, f"foreign source: {source}")
        review = anna.post(f"/study/cards/{deck['cards'][0]['id']}/review", {"grade": "good"})
        expect_status(review, 200, "card review")

        # Another account cannot reach them.
        bela = self.client(BELA)
        expect_status(bela.get(f"/study/quizzes/{quiz['id']}"), 404, "bela reading anna's quiz")
        expect_status(bela.get(f"/study/decks/{deck['id']}"), 404, "bela reading anna's deck")

        self.stored = {
            "quiz": anna.get(f"/study/quizzes/{quiz['id']}").json(),
            "deck": anna.get(f"/study/decks/{deck['id']}").json(),
            "attempts": anna.get(f"/study/quizzes/{quiz['id']}/attempts").json(),
        }
        return (
            f"quiz {quiz['id']} with {len(quiz['questions'])} questions ({quiz_tries} requests), "
            f"deck {deck['id']} with {len(deck['cards'])} cards ({deck_tries} requests)"
        )

    def survives_restart(self):
        result = self.run_command(self.args.restart_cmd, self.args.restart_timeout)
        print(result.stdout[-800:], result.stderr[-800:])
        expect(result.returncode == 0, f"restart failed: {result.stderr[-500:]}")
        deadline = time.monotonic() + self.args.restart_timeout
        probe = Client(self.args.base_url)
        while True:
            try:
                if probe.get("/health").status == 200:
                    break
            except OSError:
                pass
            expect(time.monotonic() < deadline, "the stack did not come back")
            time.sleep(2)
        # The browser keeps its session across the restart, so the clients are not logged in again.
        anna = self.client(ANNA)
        expect_status(anna.get("/session"), 200, "session after the restart")
        expect(anna.get("/session").json()["username"] == ANNA, "the session did not survive")
        if not self.stored:
            raise AssertionError("nothing to compare: the quiz and flashcards step stored nothing")
        quiz, deck = self.stored["quiz"], self.stored["deck"]
        after_quiz = anna.get(f"/study/quizzes/{quiz['id']}")
        expect_status(after_quiz, 200, "quiz after the restart")
        expect(after_quiz.json() == quiz, "the quiz changed in the restart")
        after_deck = anna.get(f"/study/decks/{deck['id']}")
        expect_status(after_deck, 200, "deck after the restart")
        expect(after_deck.json() == deck, "the deck changed in the restart")
        attempts = anna.get(f"/study/quizzes/{quiz['id']}/attempts").json()
        expect(attempts == self.stored["attempts"] and attempts, "the quiz attempt did not survive")
        answer = self.chat(ANNA, MILL_QUESTION)
        expect(answer["citations"] and answer["citations"][0]["page"] == 2, "the index did not survive")
        expect(self.source_names(ANNA) == ANNA_FILES, "the documents did not survive")
        return "session, quiz, deck, attempt, documents and vectors unchanged"

    def delete_document(self):
        anna = self.client(ANNA)
        points_before = self.point_count()
        mill = next(r for r in self.sources(ANNA) if file_name(r["source_path"]) == MILL_PDF)
        expect_status(anna.request("DELETE", f"/sources/{mill['id']}"), 204, "delete through the API")
        expect(MILL_PDF not in self.source_names(ANNA), "still listed after DELETE")
        answer = self.chat(ANNA, MILL_QUESTION)
        cited = {file_name(c["source_path"]) for c in answer["citations"]}
        expect(MILL_PDF not in cited, f"still cited after DELETE: {answer['citations']}")
        expect("tizennégy" not in json.dumps(answer["citations"], ensure_ascii=False), "deleted text still in citations")
        detail = "DELETE /sources removes it from the list and from answers"
        if points_before is not None:
            points_after = self.point_count()
            expect(points_after == points_before - mill["chunk_count"], f"points {points_before} -> {points_after}")
            detail += f" ({points_before} -> {points_after} Qdrant points)"
        if self.stored:
            # A stored quiz built on a deleted document goes with it (docs/security.md).
            gone = anna.get(f"/study/quizzes/{self.stored['quiz']['id']}")
            expect_status(gone, 404, "quiz built on the deleted document")

        # A file removed from the watched folder leaves the index on the next run.
        scan = next(r for r in self.sources(ANNA) if file_name(r["source_path"]) == SCAN_PDF)
        (self.library / "anna" / SCAN_PDF).unlink()
        run = self.index_run()
        expect(run.get("status") == "completed", f"run ended as {run.get('status')}: {run.get('message')}")
        expect(run["deleted"] == 1, f"run after removing a file: {run}")
        expect(SCAN_PDF not in self.source_names(ANNA), "removed file still listed")
        answer = self.chat(ANNA, SCAN_QUESTION)
        cited = {file_name(c["source_path"]) for c in answer["citations"]}
        expect(SCAN_PDF not in cited, f"removed file still cited: {answer['citations']}")
        detail += f"; removed file (id {scan['id']}) leaves on the next run"
        # Reported, not failed: what a delete should mean while the file stays in a watched folder is undecided.
        if MILL_PDF in self.source_names(ANNA):
            detail += f". OPEN: {MILL_PDF} came back in that run, its file is still in the watched folder"
        return detail

    def egress(self):
        if not self.args.egress_cmd:
            raise Skip("no container network on this backend, so there is nothing to probe; run make test-e2e with Docker")
        result = self.run_command(self.args.egress_cmd, 300)
        print(result.stdout[-1500:], result.stderr[-500:])
        expect(result.returncode == 0, f"egress check failed: {result.stderr[-500:] or result.stdout[-500:]}")
        expect("part 3" in result.stdout, "the live probe did not run")
        expect("runtime audit skipped" not in result.stdout, "the egress check did not see the running stack")
        return "config audit, running-container audit and live probe all passed"

    def voice(self):
        if not self.args.voice:
            raise Skip("the voice overlay is not part of this run; add --voice (its image downloads large models)")
        anna = self.client(ANNA)
        status = anna.get("/voice/status").json()
        expect(status["available"] and status["tts"] and status["stt"], f"/voice/status: {status}")
        sentence = "A kőröshegyi malom kerekének tizennégy lapátja van."
        speech = anna.post("/voice/speak", {"text": sentence}, timeout=self.args.model_timeout)
        expect_status(speech, 200, "/voice/speak")
        expect(speech.headers.get("Content-Type", "").startswith("audio/"), f"speak returned {speech.headers}")
        expect(len(speech.body) > 2000, f"only {len(speech.body)} bytes of audio")
        boundary = uuid.uuid4().hex
        form = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="round.mp3"\r\n'
            "Content-Type: audio/mpeg\r\n\r\n"
        ).encode() + speech.body + f"\r\n--{boundary}--\r\n".encode()
        heard = anna.request(
            "POST", "/voice/transcribe", body=form,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=self.args.model_timeout,
        )
        expect_status(heard, 200, "/voice/transcribe")
        text = heard.json()["text"]
        print(f"round trip: {sentence!r} -> {text!r}")
        words = [word for word in ("malom", "lapát", "tizennégy", "14") if word in text.lower()]
        expect(len(words) >= 2, f"transcript holds {words} of the spoken words: {text!r}")
        return f"{len(speech.body)} bytes of {speech.headers['Content-Type']} -> {text!r}"

    # --- the whole run ---

    def run(self) -> int:
        started = time.monotonic()
        for title, function in (
            ("HTTPS proxy in front, plain HTTP redirected", self.proxy_https),
            ("Login required through the proxy", self.login_required),
            ("Triggered run indexes the watched folders", self.index_watched_folders),
            ("Scanned PDF read through OCR", self.ocr_scanned_pdf),
            ("Second run duplicates nothing", self.rerun_no_duplicates),
            ("Chat answer cites the right file and page or chapter", self.chat_cites_source),
            ("Question with no source says so (acceptance test 5)", self.no_source_says_so),
            ("User B cannot reach user A's documents (acceptance test 6)", self.isolation),
            ("Quiz and flashcards generated and stored", self.quiz_and_flashcards),
            ("Stored data survives a stack restart", self.survives_restart),
            ("Deleting a document removes it from index and answers", self.delete_document),
            ("Egress check with the stack running", self.egress),
            ("Voice round trip in Hungarian", self.voice),
        ):
            self.step(title, function)
        return self.report(time.monotonic() - started)

    def report(self, elapsed: float) -> int:
        lines = [
            f"# End-to-end trial: stack `{self.args.stack}`, model `{self.args.llm_model}`, {elapsed:.0f}s",
            "",
            "| Step | Result | Detail |",
            "| --- | --- | --- |",
        ]
        for title, status, detail in self.results:
            lines.append(f"| {title} | {status} | {detail.replace('|', '/')[:600]} |")
        counts = {status: sum(1 for _, s, _ in self.results if s == status) for status in ("PASS", "FAIL", "SKIP")}
        lines += ["", f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped"]
        text = "\n".join(lines) + "\n"
        print("\n" + text)
        if self.args.report:
            Path(self.args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(self.args.report).write_text(text, encoding="utf-8")
        return 1 if counts["FAIL"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--http-url", required=True)
    parser.add_argument("--library", required=True, help="the trial's library folder on this host")
    parser.add_argument("--stack", default="compose")
    parser.add_argument("--llm-model", default="?")
    parser.add_argument("--restart-cmd", required=True)
    parser.add_argument("--egress-cmd", default="")
    parser.add_argument("--point-count-cmd", default="")
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--report", default="")
    parser.add_argument("--model-timeout", type=float, default=600.0)
    parser.add_argument("--index-timeout", type=float, default=1200.0)
    parser.add_argument("--restart-timeout", type=float, default=600.0)
    parser.add_argument("--study-tries", type=int, default=3)
    return Trial(parser.parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
