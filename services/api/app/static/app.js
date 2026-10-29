const askButton = document.getElementById("ask-button");
const summaryButton = document.getElementById("summary-button");
const quizButton = document.getElementById("quiz-button");
const flashcardsButton = document.getElementById("flashcards-button");
const logoutButton = document.getElementById("logout-button");
const questionField = document.getElementById("question");
const levelField = document.getElementById("level");
const answerEl = document.getElementById("answer");
const sourcesEl = document.getElementById("sources");
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

  answerEl.textContent = data.answer;
  (data.sources || []).forEach((source) => {
    const li = document.createElement("li");
    li.textContent = `${source.title || source.source_path} - ${source.locator || "ismeretlen hely"}`;
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
