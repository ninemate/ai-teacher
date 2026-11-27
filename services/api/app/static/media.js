// Média: photo → local OCR → HU↔EN translation → explanation → optional flashcards. Mobile first.
(() => {
  const root = document.getElementById("media");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const camera = $("media-camera");
  const fileInput = $("media-file");
  const preview = $("media-preview");
  const direction = $("media-direction");
  const processButton = $("media-process");
  const statusEl = $("media-status");
  const resultEl = $("media-result");
  const historyEl = $("media-history");
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const LANG = { "hu-en": "magyar → angol", "en-hu": "angol → magyar" };
  const TRIM = /^[\s.,;:!?"'()\[\]{}„”«»…–—-]+|[\s.,;:!?"'()\[\]{}„”«»…–—-]+$/g;
  let picked = null;

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    Object.entries(props).forEach(([key, value]) => {
      if (value === undefined || value === null || value === false) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    });
    [].concat(children).forEach((child) => child != null && node.append(child));
    return node;
  }

  function setStatus(target, message, isError = false) {
    target.textContent = message || "";
    target.classList.toggle("error", isError);
  }

  async function api(path, options = {}) {
    const headers = { "X-CSRF-Token": csrfToken, ...(options.headers || {}) };
    if (options.json !== undefined) {
      headers["Content-Type"] = "application/json";
      options = { ...options, body: JSON.stringify(options.json) };
    }
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      throw new Error("Lejárt a bejelentkezés.");
    }
    if (response.status === 204) return null;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? "Hibás kérés." : data.detail;
      throw new Error(detail || "A művelet nem sikerült.");
    }
    return data;
  }

  // --- picking and uploading -------------------------------------------------------------------------------------

  function onPick(event) {
    picked = event.target.files[0] || null;
    preview.hidden = true;
    preview.removeAttribute("src");
    // A data: URL, since the page's content security policy allows data: images but not blob: ones.
    if (picked && picked.size <= 10 * 1024 * 1024) {
      const reader = new FileReader();
      const file = picked;
      reader.onload = () => { if (picked === file) { preview.src = reader.result; preview.hidden = false; } };
      reader.readAsDataURL(file);
    }
    processButton.disabled = !picked;
    setStatus(statusEl, picked ? `Kiválasztva: ${picked.name || "fotó"}` : "");
  }
  camera.addEventListener("change", onPick);
  fileInput.addEventListener("change", onPick);

  processButton.addEventListener("click", async () => {
    if (!picked) return;
    if (picked.size > 10 * 1024 * 1024) {
      setStatus(statusEl, "A kép legfeljebb 10 MB lehet.", true);
      return;
    }
    processButton.disabled = true;
    setStatus(statusEl, "Szövegfelismerés a gépen… (néhány másodperc)");
    try {
      const body = new FormData();
      body.append("image", picked);
      body.append("direction", direction.value);
      const item = await api("/media/items", { method: "POST", body });
      setStatus(statusEl, "Felismerve. Fordítás a helyi modellel…");
      show(item);
      loadHistory();
      await translate(item, item.direction);
      setStatus(statusEl, "Kész.");
    } catch (error) {
      setStatus(statusEl, error.message, true);
    } finally {
      processButton.disabled = !picked;
    }
  });

  // --- one item ---------------------------------------------------------------------------------------------------

  let current = null;
  let view = "original";
  let selecting = false;
  const selected = new Set(); // "line:word" keys of words chosen for flashcards
  const extraPhrases = [];

  function show(item) {
    current = item;
    view = "original";
    selecting = false;
    selected.clear();
    extraPhrases.length = 0;
    render();
    resultEl.hidden = false;
    resultEl.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function translate(item, dir) {
    const box = resultEl.querySelector(".media-translate-status");
    if (box) setStatus(box, "Fordítás folyamatban…");
    try {
      const updated = await api(`/media/items/${item.id}/translate`, { method: "POST", json: { direction: dir } });
      if (current && current.id === updated.id) {
        current = { ...current, ...updated };
        render();
      }
    } catch (error) {
      if (current && current.id === item.id) {
        const target = resultEl.querySelector(".media-translate-status");
        if (target) setStatus(target, error.message, true);
      }
      throw error;
    }
  }

  function phrases() {
    const out = [];
    (current.lines || []).forEach((line, li) => {
      let run = [];
      line.forEach((word, wi) => {
        if (selected.has(`${li}:${wi}`)) run.push(word.text);
        else if (run.length) { out.push(run.join(" ")); run = []; }
      });
      if (run.length) out.push(run.join(" "));
    });
    return [...out, ...extraPhrases].map((p) => p.replace(TRIM, "")).filter((p, i, all) => p && all.indexOf(p) === i);
  }

  function renderText() {
    const box = el("div", { class: `media-copy${selecting ? " selecting" : ""}`, lang: current.direction.slice(0, 2) });
    const low = current.low_confidence ?? 70;
    (current.lines || []).forEach((line, li) => {
      const row = el("p", { class: line.length ? "media-line" : "media-gap" });
      line.forEach((word, wi) => {
        if (wi) row.append(" ");
        const key = `${li}:${wi}`;
        const uncertain = word.confidence != null && word.confidence < low;
        const span = el(selecting ? "button" : "span", {
          class: [uncertain && "low-confidence", selected.has(key) && "chosen", selecting && "media-word"].filter(Boolean).join(" ") || undefined,
          title: uncertain ? `Bizonytalan felismerés (${Math.round(word.confidence)}%)` : undefined,
          type: selecting ? "button" : undefined,
          "aria-pressed": selecting ? String(selected.has(key)) : undefined,
          text: word.text,
        });
        if (selecting) span.addEventListener("click", () => {
          selected.has(key) ? selected.delete(key) : selected.add(key);
          render();
        });
        row.append(span);
      });
      box.append(row);
    });
    return box;
  }

  function render() {
    const item = current;
    const uncertain = (item.lines || []).flat().filter((w) => w.confidence != null && w.confidence < (item.low_confidence ?? 70)).length;
    const meta = [
      item.engine === "vision" ? "Helyi képfelismerő modell" : "Tesseract (helyi)",
      item.mean_confidence != null ? `átlagos bizonyosság ${Math.round(item.mean_confidence)}%` : null,
      uncertain ? `${uncertain} bizonytalan szó aláhúzva` : null,
    ].filter(Boolean).join(" · ");

    const tabs = el("div", { class: "media-tabs", role: "tablist", "aria-label": "Nézet" }, [
      el("button", { type: "button", role: "tab", class: "secondary", "aria-selected": String(view === "original"), text: "Eredeti", onclick: () => { view = "original"; render(); } }),
      el("button", { type: "button", role: "tab", class: "secondary", "aria-selected": String(view === "translation"), text: "Fordítás", onclick: () => { view = "translation"; selecting = false; render(); } }),
    ]);

    let body;
    if (view === "original") {
      body = renderText();
    } else if (item.translation) {
      body = el("div", { class: "media-copy", lang: item.direction.slice(3) }, item.translation.split("\n").map((line) => el("p", { class: line.trim() ? "media-line" : "media-gap", text: line })));
    } else {
      body = el("p", { class: "muted-text", text: "Még nincs fordítás." });
    }

    const dirSelect = el("select", { "aria-label": "Fordítás iránya", class: "media-dir" },
      Object.entries(LANG).map(([value, label]) => el("option", { value, text: label, selected: value === item.direction })));
    const translateButton = el("button", { type: "button", class: "secondary", text: item.translation ? "Újrafordítás" : "Fordítás", onclick: async () => {
      translateButton.disabled = true;
      try { await translate(item, dirSelect.value); view = "translation"; render(); } catch (_) { translateButton.disabled = false; }
    } });

    const explainStatus = el("p", { class: "status", role: "status" });
    const explainButton = el("button", { type: "button", class: "secondary", text: "💡 Fontos részletek magyarázata", onclick: async () => {
      explainButton.disabled = true;
      setStatus(explainStatus, "Magyarázat készül a helyi modellel…");
      try {
        const data = await api(`/media/items/${item.id}/explain`, { method: "POST" });
        current = { ...current, explanation: data.explanation };
        render();
      } catch (error) {
        setStatus(explainStatus, error.message, true);
        explainButton.disabled = false;
      }
    } });

    const deleteButton = el("button", { type: "button", class: "danger", text: "🗑 Kép és eredmény törlése", onclick: async () => {
      if (!window.confirm("Törlöd ezt a képet, a felismert szöveget és a fordítást? A már elmentett kártyapakli megmarad.")) return;
      try {
        await api(`/media/items/${item.id}`, { method: "DELETE" });
        current = null;
        resultEl.hidden = true;
        resultEl.replaceChildren();
        setStatus(statusEl, "Törölve.");
        loadHistory();
      } catch (error) {
        setStatus(statusEl, error.message, true);
      }
    } });

    resultEl.replaceChildren(...[
      el("div", { class: "media-head" }, [
        el("img", { class: "media-thumb", src: `/media/items/${item.id}/image`, alt: "A feldolgozott kép" }),
        el("div", {}, [el("h3", { text: item.title }), el("p", { class: "muted-text", text: meta })]),
      ]),
      tabs,
      body,
      el("div", { class: "media-row" }, [dirSelect, translateButton]),
      el("p", { class: "status media-translate-status", role: "status" }),
      explainButton,
      explainStatus,
      item.explanation ? el("div", { class: "media-explanation" }, [el("h4", { text: "Magyarázat" }), ...item.explanation.split("\n").map((l) => el("p", { text: l }))]) : null,
      vocabulary(item),
      deleteButton,
    ].filter(Boolean));
  }

  // --- saving chosen phrases as flashcards (only on the user's explicit choice) ------------------------------------

  function vocabulary(item) {
    const box = el("details", { class: "media-vocab", open: selecting || undefined });
    box.append(el("summary", { text: "📇 Szavak mentése tanuláshoz" }));
    box.addEventListener("toggle", () => {
      if (box.open !== selecting) { selecting = box.open; if (selecting) view = "original"; render(); }
    });
    if (!selecting) return box;

    const list = phrases();
    const manual = el("input", { type: "text", placeholder: "Vagy írj be egy kifejezést", "aria-label": "Kifejezés hozzáadása", maxlength: "120" });
    const pairsBox = el("div", { class: "media-pairs" });
    const status = el("p", { class: "status", role: "status" });
    const suggest = el("button", { type: "button", class: "primary", text: "Fordítás javaslata a kártyákhoz", disabled: !list.length || undefined, onclick: async () => {
      suggest.disabled = true;
      setStatus(status, "Fordítások a helyi modellel…");
      try {
        const data = await api(`/media/items/${item.id}/glossary`, { method: "POST", json: { phrases: list.slice(0, 30) } });
        renderPairs(item, pairsBox, data.pairs, status);
      } catch (error) {
        setStatus(status, error.message, true);
      } finally {
        suggest.disabled = false;
      }
    } });

    box.append(
      el("p", { class: "hint", text: "Koppints a szavakra fent a szövegben: az egymás melletti szavak egy kifejezést adnak. Semmi nem mentődik, amíg a „Pakli mentése” gombot nem nyomod meg." }),
      el("ul", { class: "media-chips" }, list.length ? list.map((p) => el("li", { text: p })) : el("li", { class: "muted-text", text: "Még nincs kiválasztott szó." })),
      el("div", { class: "media-row" }, [manual, el("button", { type: "button", class: "secondary", text: "Hozzáadás", onclick: () => {
        const value = manual.value.trim();
        if (value) { extraPhrases.push(value); render(); }
      } })]),
      suggest,
      status,
      pairsBox,
    );
    return box;
  }

  function renderPairs(item, box, pairs, status) {
    const rows = pairs.map((pair) => {
      const front = el("input", { type: "text", value: pair.front, "aria-label": "Kártya eleje", maxlength: "300" });
      const back = el("input", { type: "text", value: pair.back, "aria-label": "Kártya hátoldala", placeholder: "Jelentés", maxlength: "300" });
      const row = el("li", { class: "media-pair" }, [front, back]);
      row.append(el("button", { type: "button", class: "secondary", "aria-label": "Kártya elhagyása", text: "✕", onclick: () => row.remove() }));
      return row;
    });
    const list = el("ol", { class: "media-pair-list" }, rows);
    const title = el("input", { type: "text", value: `Fotó: ${item.title}`.slice(0, 200), "aria-label": "Pakli neve", maxlength: "200" });
    const saveStatus = el("p", { class: "status", role: "status" });
    const save = el("button", { type: "button", class: "primary", text: "Pakli mentése", onclick: async () => {
      const chosen = [...list.querySelectorAll(".media-pair")].map((row) => {
        const [front, back] = row.querySelectorAll("input");
        return { front: front.value.trim(), back: back.value.trim() };
      }).filter((p) => p.front && p.back);
      if (!chosen.length) { setStatus(saveStatus, "Minden kártyának legyen eleje és hátoldala.", true); return; }
      save.disabled = true;
      try {
        const data = await api(`/media/items/${item.id}/deck`, { method: "POST", json: { pairs: chosen, title: title.value } });
        setStatus(saveStatus, `Elmentve: „${data.title}” (${data.card_count} kártya). A Tanulás részben gyakorolhatod.`);
      } catch (error) {
        setStatus(saveStatus, error.message, true);
        save.disabled = false;
      }
    } });
    box.replaceChildren(el("label", { text: "Pakli neve" }), title, list, save, saveStatus);
    setStatus(status, "Nézd át és javítsd a hátoldalakat, aztán mentsd a paklit.");
  }

  // --- history ----------------------------------------------------------------------------------------------------

  async function loadHistory() {
    try {
      const items = await api("/media/items");
      historyEl.replaceChildren(...(items.length ? items.map((item) => el("li", {}, el("button", {
        type: "button", class: "media-history-item", onclick: () => show(item),
      }, [
        el("img", { src: `/media/items/${item.id}/image`, alt: "", loading: "lazy" }),
        el("span", {}, [el("strong", { text: item.title }), el("small", { text: `${LANG[item.direction]}${item.created_at ? " · " + new Date(item.created_at).toLocaleString("hu-HU") : ""}` })]),
      ]))) : [el("li", { class: "muted-text", text: "Még nincs feldolgozott kép." })]));
    } catch (_) {
      historyEl.replaceChildren(el("li", { class: "muted-text", text: "A korábbi képek most nem tölthetők be." }));
    }
  }

  loadHistory();
})();
