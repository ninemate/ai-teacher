// Workspace-scoped practice. Only the server stores learner data.
(() => {
  const root = document.getElementById("tutor");
  if (!root) return;
  const $ = (name) => root.querySelector("#tutor-" + name);
  const languages = { en: "Angol", hu: "Magyar" };
  const modes = { conversation: "Beszélgetés", roleplay: "Szerepjáték", listening: "Hallásértés", writing: "Írásbeli javítás" };
  const errorNames = { grammar: "Nyelvtan", vocabulary: "Szókincs", spelling: "Helyesírás", usage: "Szóhasználat", word_order: "Szórend", comprehension: "Szövegértés", other: "Egyéb" };
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let session = null;
  let profile;
  let decks = [];
  let busy = false;
  const savedCards = new Set();

  function status(message, error = false) {
    $("status").textContent = message;
    $("status").classList.toggle("error", error);
  }
  async function api(path, method = "GET", body) {
    const res = await fetch(path, {
      method, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      location.assign("/login?next=" + encodeURIComponent(location.pathname + location.hash));
      throw Error("Jelentkezz be újra.");
    }
    const data = await res.json();
    if (!res.ok) throw Error(typeof data.detail === "string" ? data.detail : "A kérés sikertelen. Ellenőrizd a megadott adatokat.");
    return data;
  }
  function node(tag, text, cls) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }
  function button(label, action) {
    const b = node("button", label, "secondary");
    b.type = "button";
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await action(); } catch (err) { status(err.message, true); }
      finally { b.disabled = false; }
    });
    return b;
  }
  function showProfile(p) {
    profile = p;
    $("language").value = p.language;
    $("level").value = p.level;
    $("goal").value = p.goal;
    $("auto").checked = p.auto_level;
    $("summary").textContent = languages[p.language] + " · " + p.level;
  }
  function selectItems(select, items, option) {
    const previous = select.value;
    select.replaceChildren(select.options[0], ...items.map(option));
    select.value = previous;
    if (select.selectedIndex < 0) select.selectedIndex = 0;
  }
  async function loadDecks() {
    decks = await api("/tutor/decks");
    selectItems($("deck"), decks, d => new Option(d.title, d.id));
  }
  $("material").addEventListener("toggle", async () => {
    if (!$("material").open) return;
    try {
      const [docs] = await Promise.all([api("/sources"), loadDecks()]);
      selectItems($("source"), docs, d => new Option(d.title || d.source_path.split("/").pop(), d.source_path));
    } catch (err) { status(err.message, true); }
  });
  $("profile-form").addEventListener("submit", async e => {
    e.preventDefault();
    const submit = e.submitter;
    submit.disabled = true;
    try {
      showProfile(await api("/tutor/profile", "PUT", {
        language: $("language").value, level: $("level").value, goal: $("goal").value.trim(), auto_level: $("auto").checked,
      }));
      $("profile").open = false;
      status("A profil mentve.");
    } catch (err) { status(err.message, true); }
    finally { submit.disabled = false; }
  });

  const voice = window.createTutorVoice({
    language: () => session?.language || profile?.language || "en",
    status, onTurn: submitTurn,
  });
  function cardPicker(turn, turnIndex) {
    const details = node("details");
    details.append(node("summary", "Szókincs mentése (" + turn.vocabulary.length + ")"));
    const label = node("label", "Célpakli");
    const select = node("select");
    select.add(new Option("Tutor szókincspakli", ""));
    decks.forEach(d => select.add(new Option(d.title, d.id)));
    label.append(select);
    details.append(label);
    for (const [index, card] of turn.vocabulary.entries()) {
      const key = turnIndex + ":" + index;
      const row = node("p", card.front + " — " + card.back + " ");
      const save = button("Mentés", async () => {
        await api("/tutor/sessions/" + session.id + "/cards", "POST", {
          deck_id: select.value ? Number(select.value) : null, front: card.front, back: card.back,
        });
        savedCards.add(key);
        save.replaceWith(node("strong", "Mentve"));
        status("A szókincs bekerült a kártyapakliba.");
      });
      row.append(savedCards.has(key) ? node("strong", "Mentve") : save);
      details.append(row);
    }
    return details;
  }
  function renderTurn(turn, index) {
    const box = node("article", "", "tutor-turn");
    if (turn.learner) {
      const learner = node("p", "", "tutor-learner");
      learner.append(node("strong", "Te · "), turn.learner);
      box.append(learner);
    }
    box.append(node("strong", "Tutor"));
    if (session.mode === "listening" && !index) {
      box.append(node("p", "Hallgasd meg a szöveget és a kérdést, majd válaszolj."));
      const details = node("details");
      details.append(node("summary", "Átirat megjelenítése"), node("p", turn.reply));
      box.append(button("Meghallgatás", () => voice.speak(turn.reply)), details);
    } else {
      box.append(node("p", turn.reply));
    }
    if (turn.corrections.length) {
      box.append(node("strong", "Javítás"));
      const list = node("ul");
      for (const c of turn.corrections) {
        const li = node("li", (errorNames[c.type] || c.type) + " · " + c.explanation);
        li.append(node("p", c.example));
        list.append(li);
      }
      box.append(list);
    }
    if (turn.vocabulary.length) box.append(cardPicker(turn, index));
    return box;
  }
  function renderSession(data) {
    session = data;
    $("setup").hidden = true;
    $("session").hidden = false;
    $("mode-title").textContent = modes[data.mode];
    $("thread").replaceChildren(...data.turns.map(renderTurn));
    $("thread").scrollTop = $("thread").scrollHeight;
  }
  $("start").addEventListener("submit", async e => {
    e.preventDefault();
    if (busy) return;
    busy = true;
    $("start-button").disabled = true;
    status("A tutor készíti a feladatot…");
    try {
      await loadDecks();
      const data = await api("/tutor/sessions", "POST", {
        mode: root.querySelector('input[name="tutor-mode"]:checked').value,
        scenario: $("scenario").value.trim(), source_path: $("source").value || null,
        deck_id: $("deck").value ? Number($("deck").value) : null,
      });
      savedCards.clear();
      $("reply").value = "";
      $("acoustics").textContent = "";
      renderSession(data);
      status("");
      if (session.mode === "listening") await voice.speak(data.turns[0].reply);
    } catch (err) { status(err.message, true); }
    finally { busy = false; $("start-button").disabled = false; }
  });
  async function submitTurn(text) {
    if (busy || !session) throw Error("Várd meg a tutor válaszát.");
    const current = session.id;
    busy = true;
    $("reply-form").querySelector("button").disabled = true;
    status("A tutor válaszol…");
    try {
      const data = await api("/tutor/sessions/" + current + "/turns", "POST", { text });
      if (session?.id !== current) return "";
      renderSession(data);
      if ($("progress").open) await loadProgress();
      status("");
      return data.turns.at(-1).reply;
    } finally { busy = false; $("reply-form").querySelector("button").disabled = false; }
  }
  $("reply-form").addEventListener("submit", async e => {
    e.preventDefault();
    const input = $("reply").value.trim();
    if (!input) return;
    try { await submitTurn(input); if ($("reply").value.trim() === input) $("reply").value = ""; }
    catch (err) { status(err.message, true); }
  });
  $("end").addEventListener("click", async () => {
    voice.stop();
    session = null;
    $("session").hidden = true;
    $("setup").hidden = false;
    status("A gyakorlás mentve.");
    try { showProfile(await api("/tutor/profile")); await loadProgress(); }
    catch (err) { status(err.message, true); }
  });
  async function loadProgress() {
    const p = await api("/tutor/progress");
    const box = $("stats");
    box.replaceChildren(node("p", p.sessions + " gyakorlás · " + p.turns + " válasz"));
    const list = node("ul");
    for (const [type, count] of Object.entries(p.errors)) list.append(node("li", (errorNames[type] || type) + ": " + count));
    if (list.childElementCount) box.append(node("h4", "Hibák típus szerint"), list);
    if (p.levels.length) {
      const changes = node("ul");
      for (const l of p.levels) changes.append(node("li", languages[l.language] + " · " + l.previous + " → " + l.level));
      box.append(node("h4", "Szintváltozások"), changes);
    }
  }
  $("progress").addEventListener("toggle", () => {
    if ($("progress").open) loadProgress().catch(err => status(err.message, true));
  });
  api("/tutor/profile").then(p => {
    showProfile(p);
    $("start-button").disabled = false;
  }).catch(err => status(err.message, true));
})();
