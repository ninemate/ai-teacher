// Study section: generate, list, and work through stored quizzes and flashcard decks.
(() => {
  const root = document.getElementById("study");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const levelField = $("level");
  const statusEl = $("study-status");
  const listEl = $("study-list");
  const sessionEl = $("study-session");
  let tab = "quizzes";

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    Object.entries(props).forEach(([key, value]) => {
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    });
    [].concat(children).forEach((child) => node.append(child));
    return node;
  }

  function setStatus(message, isError = false) {
    statusEl.textContent = message || "";
    statusEl.classList.toggle("error", isError);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (response.status === 204) return null;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
      throw new Error(detail || `Hiba (${response.status})`);
    }
    return data;
  }

  function refsList(sources) {
    return el(
      "ul",
      { class: "refs" },
      (sources || []).map((s) =>
        el("li", {}, `${s.title || (s.source_path || "").split("/").pop()} – ${s.locator || "ismeretlen hely"}`)
      )
    );
  }

  // --- source picker ---
  async function loadSources() {
    const box = $("study-docs");
    try {
      const docs = await api("/sources");
      box.replaceChildren(
        ...docs.map((d) =>
          el("label", {}, [
            el("input", { type: "checkbox", value: d.source_path }),
            el("span", {}, d.title || d.source_path.split("/").pop()),
          ])
        )
      );
      if (!docs.length) box.textContent = "Még nincs indexelt dokumentum.";
    } catch (err) {
      box.textContent = "A dokumentumok nem tölthetők be.";
    }
  }

  function selectedSources() {
    return [...$("study-docs").querySelectorAll("input:checked")].map((i) => i.value);
  }

  async function generate(kind) {
    const topic = $("study-topic").value.trim();
    const source_paths = selectedSources();
    if (topic.length < 3 && !source_paths.length) {
      setStatus("Adj meg egy témát vagy válassz dokumentumot.", true);
      return;
    }
    const body = { level: levelField.value, source_paths };
    if (topic) body.topic = topic;
    const count = parseInt($("study-count").value, 10);
    if (count) body.count = count;
    const buttons = root.querySelectorAll(".generate");
    buttons.forEach((b) => (b.disabled = true));
    setStatus("Készül, ez egy-két percig is eltarthat...");
    try {
      await api(kind === "quizzes" ? "/study/quizzes" : "/study/decks", { method: "POST", body: JSON.stringify(body) });
      setStatus("Elmentve.");
      selectTab(kind);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      buttons.forEach((b) => (b.disabled = false));
    }
  }

  // --- saved items list ---
  function selectTab(name) {
    tab = name;
    root.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    sessionEl.replaceChildren();
    loadList();
  }

  async function loadList() {
    listEl.textContent = "Betöltés...";
    try {
      const items = await api(tab === "quizzes" ? "/study/quizzes" : "/study/decks");
      if (!items.length) {
        listEl.replaceChildren(el("p", { class: "empty" }, "Még nincs mentett elem."));
        return;
      }
      listEl.replaceChildren(el("ul", { class: "items" }, items.map(tab === "quizzes" ? quizRow : deckRow)));
    } catch (err) {
      listEl.textContent = err.message;
    }
  }

  function row(title, meta, startLabel, onStart, onDelete) {
    return el("li", { class: "item" }, [
      el("div", { class: "item-title" }, title),
      el("div", { class: "item-meta" }, meta),
      el("div", { class: "item-actions" }, [
        el("button", { type: "button", class: "primary", onclick: onStart }, startLabel),
        el("button", { type: "button", class: "danger", onclick: onDelete }, "Törlés"),
      ]),
    ]);
  }

  async function remove(path, label) {
    if (!confirm(`Biztosan törlöd: ${label}?`)) return;
    try {
      await api(path, { method: "DELETE" });
      sessionEl.replaceChildren();
      loadList();
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  function quizRow(q) {
    const last = q.attempt_count ? ` · utolsó: ${q.last_score}/${q.last_total}` : "";
    return row(
      q.title,
      `${q.question_count} kérdés · ${q.attempt_count} kitöltés${last}`,
      "Kvíz indítása",
      () => startQuiz(q.id),
      () => remove(`/study/quizzes/${q.id}`, q.title)
    );
  }

  function deckRow(d) {
    return row(
      d.title,
      `${d.card_count} kártya · ${d.due_count} esedékes`,
      d.due_count ? `Tanulás (${d.due_count})` : "Nincs esedékes kártya",
      () => startDeck(d.id),
      () => remove(`/study/decks/${d.id}`, d.title)
    );
  }

  // --- flashcard session ---
  async function startDeck(id) {
    let cards;
    try {
      cards = await api(`/study/decks/${id}/due`);
    } catch (err) {
      setStatus(err.message, true);
      return;
    }
    const queue = [...cards];
    const total = queue.length;
    let done = 0;

    function finish() {
      sessionEl.replaceChildren(
        el("div", { class: "session" }, [
          el("p", { class: "result-score" }, total ? "Kész, mára ennyi volt." : "Most nincs esedékes kártya."),
          el("button", { type: "button", class: "secondary", onclick: () => selectTab("decks") }, "Vissza a paklikhoz"),
        ])
      );
    }

    function show() {
      const card = queue[0];
      if (!card) return finish();
      let flipped = false;
      const face = el("div", { class: "card-face" }, [el("div", {}, card.front)]);
      const controls = el("div");
      const draw = () => {
        face.replaceChildren(el("div", {}, card.front));
        controls.replaceChildren();
        if (!flipped) {
          controls.append(
            el("button", { type: "button", class: "primary", onclick: () => { flipped = true; draw(); } }, "Megfordítom")
          );
          return;
        }
        face.append(el("div", { class: "back" }, card.back), refsList(card.sources));
        controls.append(
          el(
            "div",
            { class: "grades" },
            [["again", "Újra"], ["hard", "Nehéz"], ["good", "Jó"], ["easy", "Könnyű"]].map(([grade, label]) =>
              el("button", { type: "button", class: grade === "again" ? "danger" : "secondary", onclick: () => grade_(card, grade) }, label)
            )
          )
        );
      };
      draw();
      sessionEl.replaceChildren(
        el("div", { class: "session" }, [el("div", { class: "progress" }, `${done + 1}. / ${total} kártya`), face, controls])
      );
    }

    async function grade_(card, grade) {
      try {
        const updated = await api(`/study/cards/${card.id}/review`, { method: "POST", body: JSON.stringify({ grade }) });
        queue.shift();
        done += 1;
        // A failed card comes back within this session.
        if (grade === "again") queue.push(updated);
        show();
      } catch (err) {
        setStatus(err.message, true);
      }
    }

    show();
  }

  // --- quiz session ---
  async function startQuiz(id) {
    let quiz;
    try {
      quiz = await api(`/study/quizzes/${id}`);
    } catch (err) {
      setStatus(err.message, true);
      return;
    }
    const chosen = {};
    let index = 0;

    function show() {
      const q = quiz.questions[index];
      const options = q.options.map((text, i) =>
        el(
          "button",
          {
            type: "button",
            class: "option" + (chosen[q.id] === i ? " selected" : ""),
            "aria-pressed": String(chosen[q.id] === i),
            onclick: () => { chosen[q.id] = i; show(); },
          },
          text
        )
      );
      const last = index === quiz.questions.length - 1;
      const next = el(
        "button",
        { type: "button", class: "primary", onclick: last ? submit : () => { index += 1; show(); } },
        last ? "Beküldés" : "Következő"
      );
      const actions = [next];
      if (index > 0) actions.push(el("button", { type: "button", class: "secondary", onclick: () => { index -= 1; show(); } }, "Előző"));
      sessionEl.replaceChildren(
        el("div", { class: "session" }, [
          el("div", { class: "progress" }, `${index + 1}. / ${quiz.questions.length} kérdés`),
          el("div", { class: "question" }, q.question),
          el("div", { class: "options" }, options),
          el("div", { class: "session-actions" }, actions),
        ])
      );
    }

    async function submit() {
      try {
        const attempt = await api(`/study/quizzes/${id}/attempts`, { method: "POST", body: JSON.stringify({ answers: chosen }) });
        showResult(attempt);
      } catch (err) {
        setStatus(err.message, true);
      }
    }

    function showResult(attempt) {
      const blocks = quiz.questions.map((q) => {
        const picked = attempt.answers[String(q.id)].chosen;
        const options = q.options.map((text, i) =>
          el("div", { class: "option" + (i === q.correct_index ? " correct" : i === picked ? " wrong" : "") }, text)
        );
        return el("div", { class: "review-block" }, [
          el("div", { class: "question" }, q.question),
          el("div", { class: "options" }, options),
          q.explanation ? el("p", { class: "explanation" }, q.explanation) : "",
          refsList(q.sources),
        ]);
      });
      sessionEl.replaceChildren(
        el("div", { class: "session" }, [
          el("p", { class: "result-score" }, `Eredmény: ${attempt.score} / ${attempt.total}`),
          el("div", { class: "session-actions" }, [
            el("button", { type: "button", class: "primary", onclick: () => startQuiz(id) }, "Újra"),
            el("button", { type: "button", class: "secondary", onclick: () => selectTab("quizzes") }, "Vissza a kvízekhez"),
          ]),
          ...blocks,
        ])
      );
    }

    show();
  }

  $("study-gen-quiz").addEventListener("click", () => generate("quizzes"));
  $("study-gen-deck").addEventListener("click", () => generate("decks"));
  root.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  loadSources();
  selectTab("quizzes");
})();
