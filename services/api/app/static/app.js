const askButton = document.getElementById("ask-button");
const summaryButton = document.getElementById("summary-button");
const quizButton = document.getElementById("quiz-button");
const flashcardsButton = document.getElementById("flashcards-button");
const logoutButton = document.getElementById("logout-button");
const questionField = document.getElementById("question");
const levelField = document.getElementById("level");
const answerEl = document.getElementById("answer");
const sourcesEl = document.getElementById("sources");
const noticeEl = document.getElementById("grounding-notice");
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

function goToLogin() {
  window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
}

function postJson(path, payload) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(payload),
  });
}

async function runTask(path) {
  const question = questionField.value.trim();
  if (!question) {
    answerEl.textContent = "Adj meg egy kérdést vagy témát.";
    return;
  }

  answerEl.textContent = "Válasz készül...";
  sourcesEl.innerHTML = "";
  renderNotice(null);

  const payload =
    path === "/chat"
      ? { question, level: levelField.value }
      : { topic: question, level: levelField.value };

  const response = await postJson(path, payload);
  if (response.status === 401) {
    goToLogin();
    return;
  }

  const data = await response.json();
  if (!response.ok) {
    answerEl.textContent = typeof data.detail === "string" ? data.detail : "Hiba történt.";
    return;
  }

  renderAnswer(data.answer, data.citations || []);
  renderNotice(data.grounding);
  renderCitations(data.citations || []);
}

const CITE_MARKER = /\[(\d+(?:\s*[,;]\s*\d+)*)\]|\[\s*forrás\s+nélkül\s*\]/gi;
const REASONS = {
  weak_match: "gyenge egyezés",
  ocr_low_confidence: "bizonytalan OCR",
  ocr_unmeasured: "OCR, ismeretlen bizonyosság",
};

function span(className, text, title) {
  const el = document.createElement("span");
  el.className = className;
  el.textContent = text;
  if (title) el.title = title;
  return el;
}

// Text nodes only: the answer is model output and must never be parsed as HTML.
function renderAnswer(answer, citations) {
  const labels = new Map(citations.map((c) => [c.id, c.label]));
  answerEl.textContent = "";
  let last = 0;
  for (const match of answer.matchAll(CITE_MARKER)) {
    answerEl.append(answer.slice(last, match.index));
    if (match[1]) {
      const ids = match[1].split(/[,;]/).map((n) => Number(n.trim()));
      const title = ids.map((id) => labels.get(id) || "nincs ilyen forrás").join("; ");
      answerEl.append(span("cite", match[0], title));
    } else {
      answerEl.append(span("cite unsourced", match[0], "A modell saját magyarázata, nem a dokumentumokból."));
    }
    last = match.index + match[0].length;
  }
  answerEl.append(answer.slice(last));
}

function renderNotice(grounding) {
  noticeEl.hidden = !grounding || !grounding.notice;
  noticeEl.textContent = grounding && grounding.notice ? grounding.notice : "";
  noticeEl.classList.toggle("warning", Boolean(grounding && (grounding.low_confidence || grounding.status !== "grounded")));
}

function renderCitations(citations) {
  citations.forEach((citation) => {
    const li = document.createElement("li");
    li.value = citation.id;
    li.classList.toggle("uncited", !citation.cited);
    li.append(span("cite-label", citation.label));
    if (citation.title && citation.title !== citation.file_name) li.append(` – ${citation.title}`);
    if (citation.ocr) li.append(span("badge", "OCR"));
    citation.uncertain_reasons.forEach((reason) => {
      const extra = reason === "ocr_low_confidence" ? ` (${Math.round(citation.ocr_confidence)}%)` : "";
      li.append(span("badge warning", `${REASONS[reason] || reason}${extra}`));
    });
    if (!citation.cited) li.append(span("badge muted", "nem hivatkozott"));
    sourcesEl.appendChild(li);
  });
}

askButton.addEventListener("click", () => runTask("/chat"));
summaryButton.addEventListener("click", () => runTask("/summary"));
quizButton.addEventListener("click", () => runTask("/quiz"));
flashcardsButton.addEventListener("click", () => runTask("/flashcards"));

if (logoutButton) {
  logoutButton.addEventListener("click", async () => {
    await postJson("/logout", {});
    window.location.assign("/login");
  });
}
