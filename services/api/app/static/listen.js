// Listening section: pick a library document, read it aloud one segment at a time (offline Piper voice),
// resume where you stopped on any device. Audio is made on demand by the server, never for the whole book.
(() => {
  const root = document.getElementById("listen");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const audio = $("listen-audio");
  const statusEl = $("listen-status");
  const VOICE_KEY = "teacher.voice";
  const SKIP_KEY = "teacher.listen.skipLow";
  const READ_AHEAD = 2;
  const SAVE_EVERY_MS = 4000;
  const VOICE_NAMES = {
    "hu_HU-imre-medium": "Imre (férfihang)",
    "hu_HU-anna-medium": "Anna (női hang)",
    "hu_HU-berta-medium": "Berta (női hang)",
  };

  let voice = { tts: false, voices: [], default_voice: null };
  let doc = null; // {id, title, sections, bookmarks, limit}
  let sectionIndex = 0;
  let segmentIndex = 0;
  let playing = false;
  let token = 0; // bumped on every jump, so a slow answer for an old segment is ignored
  const texts = new Map(); // section -> Promise<section text>
  const clips = new Map(); // "section/segment" -> Promise<object URL>
  let saveTimer = null;
  let lastSave = 0;

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
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      ...options,
    });
    if (response.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      throw new Error("Lejárt a bejelentkezés.");
    }
    if (response.status === 204) return null;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
      const error = new Error(detail || `Hiba (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  // --- voice availability (the overlay is optional) ---------------------------------------

  async function loadVoice() {
    try {
      voice = await api("/voice/status");
    } catch {
      voice = { tts: false, voices: [], default_voice: null };
    }
    $("listen-voice-off").hidden = Boolean(voice.tts);
    const select = $("listen-voice");
    select.replaceChildren(
      ...(voice.voices || []).map((name) => el("option", { value: name }, VOICE_NAMES[name] || name))
    );
    select.disabled = !voice.tts;
    $("listen-play").disabled = !voice.tts;
    const saved = localStorage.getItem(VOICE_KEY);
    select.value = (voice.voices || []).includes(saved) ? saved : voice.default_voice || "";
  }

  // --- library list ----------------------------------------------------------------------

  async function loadLibrary() {
    const list = $("listen-library");
    let items = [];
    try {
      items = await api("/listen/library");
    } catch (error) {
      list.replaceChildren(el("li", { class: "empty" }, error.message));
      return;
    }
    if (!items.length) {
      list.replaceChildren(el("li", { class: "empty" }, "Még nincs dokumentum a hallgatási listádon. Adj hozzá egyet lent."));
    } else {
      list.replaceChildren(
        ...items.map((item) => {
          const doc_ = item.document;
          const meta = [doc_.author, item.position.section || item.position.segment
            ? `Folytatás: ${item.position.section + 1}. szakasz` : "Még nem kezdted el"].filter(Boolean).join(" · ");
          return el("li", {}, [
            el("strong", {}, doc_.title || `Dokumentum ${doc_.id}`),
            el("span", { class: "meta" }, meta),
            el("div", { class: "row" }, [
              el("button", { type: "button", class: "primary", onclick: () => openDocument(doc_.id) },
                item.position.section || item.position.segment ? "▶ Folytatás" : "▶ Megnyitás"),
              el("button", { type: "button", class: "secondary", "aria-label": `Eltávolítás: ${doc_.title || doc_.id}`,
                onclick: () => removeFromLibrary(doc_.id) }, "✕"),
            ]),
          ]);
        })
      );
    }
    await loadPicker(new Set(items.map((item) => item.document.id)));
  }

  async function loadPicker(already) {
    const select = $("listen-doc-select");
    try {
      const sources = await api("/sources");
      const options = sources.filter((s) => !already.has(s.id));
      select.replaceChildren(
        ...(options.length
          ? options.map((s) => el("option", { value: String(s.id) }, s.title || s.source_path.split("/").pop()))
          : [el("option", { value: "" }, "Nincs több hozzáadható dokumentum")])
      );
      $("listen-add").disabled = !options.length;
    } catch {
      select.replaceChildren(el("option", { value: "" }, "A dokumentumok nem tölthetők be"));
      $("listen-add").disabled = true;
    }
  }

  async function addToLibrary() {
    const id = Number($("listen-doc-select").value);
    if (!id) return;
    try {
      await api("/listen/library", { method: "POST", body: JSON.stringify({ document_id: id }) });
      await loadLibrary();
    } catch (error) {
      $("listen-library").prepend(el("li", { class: "empty" }, error.message));
    }
  }

  async function removeFromLibrary(id) {
    if (!window.confirm("Eltávolítod a listáról? A helyed és a könyvjelzőid is törlődnek.")) return;
    try {
      await api(`/listen/library/${id}`, { method: "DELETE" });
    } catch {
      /* already gone */
    }
    await loadLibrary();
  }

  // --- opening a document ----------------------------------------------------------------

  async function openDocument(id) {
    try {
      const data = await api(`/listen/${id}`);
      doc = {
        id,
        title: data.document.title || `Dokumentum ${id}`,
        sections: data.sections,
        bookmarks: data.bookmarks,
        limit: data.ocr_low_confidence_limit,
      };
      const position = data.position || { section: 0, segment: 0 };
      $("listen-library-view").hidden = true;
      $("listen-player").hidden = false;
      $("listen-title").textContent = doc.title;
      $("listen-skip-low").checked = localStorage.getItem(SKIP_KEY) === "1";
      if (data.position && data.position.speed) $("listen-speed").value = String(data.position.speed);
      if (data.position && data.position.voice && (voice.voices || []).includes(data.position.voice)) {
        $("listen-voice").value = data.position.voice;
      }
      delete $("listen-text").dataset.section;
      renderSectionList();
      renderOcrNote();
      renderBookmarks();
      applySpeed();
      setStatus("");
      await goTo(position.section, position.segment, false);
    } catch (error) {
      await loadLibrary();
      $("listen-library").prepend(el("li", { class: "empty" }, error.message));
    }
  }

  function closeDocument() {
    pause();
    flushSave();
    doc = null;
    texts.clear();
    clips.forEach((promise) => promise.then((url) => url && URL.revokeObjectURL(url)).catch(() => {}));
    clips.clear();
    $("listen-player").hidden = true;
    $("listen-library-view").hidden = false;
    loadLibrary();
  }

  function renderSectionList() {
    $("listen-section").replaceChildren(
      ...doc.sections.map((s) =>
        el("option", { value: String(s.index) }, `${s.title}${s.low_confidence ? " (bizonytalan OCR)" : s.ocr ? " (OCR)" : ""}`)
      )
    );
  }

  function renderOcrNote() {
    const low = doc.sections.filter((s) => s.low_confidence).length;
    const ocr = doc.sections.filter((s) => s.ocr).length;
    const note = $("listen-ocr-note");
    note.hidden = !ocr;
    if (!ocr) return;
    note.textContent =
      `Ebben a dokumentumban ${ocr} szakasz beolvasott (OCR) szöveg, ebből ${low} bizonytalan ` +
      `(${Math.round(doc.limit)}% alatti az OCR megbízhatósága). A beolvasott oldalakat nem biztos, hogy hibátlanul olvassa fel; ` +
      "a bizonytalan részeket narancssárga sáv jelöli.";
  }

  // --- section text and clips ------------------------------------------------------------

  function sectionText(index) {
    if (!texts.has(index)) {
      const promise = api(`/listen/${doc.id}/sections/${index}`);
      promise.catch(() => texts.delete(index));
      texts.set(index, promise);
    }
    return texts.get(index);
  }

  function clipUrl(section, segment) {
    const key = `${section}/${segment}`;
    if (!clips.has(key)) {
      const voiceName = $("listen-voice").value;
      const query = voiceName ? `?voice=${encodeURIComponent(voiceName)}` : "";
      const documentId = doc.id;
      const promise = fetch(`/listen/${documentId}/audio/${section}/${segment}${query}`, {
        headers: { "X-CSRF-Token": csrfToken },
      }).then(async (response) => {
        if (response.status === 401) {
          window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
        }
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          const error = new Error(typeof data.detail === "string" ? data.detail : `Hiba (${response.status})`);
          error.status = response.status;
          throw error;
        }
        return URL.createObjectURL(await response.blob());
      });
      promise.catch(() => clips.delete(key));
      clips.set(key, promise);
      // Keep a handful of clips in memory, not the whole listening session.
      while (clips.size > READ_AHEAD + 4) {
        const [oldKey, oldPromise] = clips.entries().next().value;
        clips.delete(oldKey);
        oldPromise.then((url) => URL.revokeObjectURL(url)).catch(() => {});
      }
    }
    return clips.get(key);
  }

  async function neighbour(section, segment, step) {
    const current = await sectionText(section);
    const next = segment + step;
    if (next >= 0 && next < current.segments.length) return [section, next];
    const other = section + step;
    if (other < 0 || other >= doc.sections.length) return null;
    const text = await sectionText(other);
    return [other, step > 0 ? 0 : text.segments.length - 1];
  }

  async function prefetch(section, segment, myToken) {
    if (!voice.tts) return;
    let ref = [section, segment];
    for (let i = 0; i < READ_AHEAD; i += 1) {
      ref = await neighbour(ref[0], ref[1], 1).catch(() => null);
      if (!ref || myToken !== token) return;
      clipUrl(ref[0], ref[1]).catch(() => {});
    }
  }

  // --- showing the text ------------------------------------------------------------------

  function renderText(text) {
    const box = $("listen-text");
    box.replaceChildren(
      ...text.segments.map((segment) => {
        const classes = segment.low_confidence ? "low" : "";
        const tag = segment.low_confidence
          ? el("span", { class: "tag" }, `Bizonytalan OCR${segment.ocr_confidence != null ? ` (${Math.round(segment.ocr_confidence)}%)` : ""}`)
          : segment.ocr
            ? el("span", { class: "tag ok" }, `OCR${segment.ocr_confidence != null ? ` (${Math.round(segment.ocr_confidence)}%)` : ""}`)
            : null;
        return el("p", { class: classes, "data-index": String(segment.index), onclick: () => goTo(sectionIndex, segment.index, playing) },
          [tag, segment.text].filter(Boolean));
      })
    );
  }

  function markCurrent(scroll) {
    const box = $("listen-text");
    box.querySelectorAll("p.current").forEach((node) => node.classList.remove("current"));
    const node = box.querySelector(`p[data-index="${segmentIndex}"]`);
    if (node) {
      node.classList.add("current");
      if (scroll) node.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    const count = doc.sections[sectionIndex].segments;
    $("listen-progress").max = Math.max(count, 1);
    $("listen-progress").value = segmentIndex + 1;
    $("listen-section").value = String(sectionIndex);
    updateMediaSession();
  }

  // --- playing ---------------------------------------------------------------------------

  async function goTo(section, segment, play) {
    const myToken = ++token;
    audio.pause();
    sectionIndex = Math.min(Math.max(section, 0), doc.sections.length - 1);
    const text = await sectionText(sectionIndex).catch((error) => {
      setStatus(error.message, true);
      return null;
    });
    if (!text || myToken !== token) return;
    segmentIndex = Math.min(Math.max(segment, 0), text.segments.length - 1);
    if (!$("listen-text").dataset.section || Number($("listen-text").dataset.section) !== sectionIndex) {
      renderText(text);
      $("listen-text").dataset.section = String(sectionIndex);
    }
    markCurrent(true);
    scheduleSave();
    if (play) await playCurrent(myToken);
    else setPlayingState(false);
  }

  async function playCurrent(myToken) {
    if (!voice.tts) {
      setPlayingState(false);
      return;
    }
    const text = await sectionText(sectionIndex);
    const segment = text.segments[segmentIndex];
    if (segment.low_confidence && $("listen-skip-low").checked) {
      const next = await neighbour(sectionIndex, segmentIndex, 1);
      if (myToken !== token) return;
      if (next) await goTo(next[0], next[1], true);
      else setPlayingState(false);
      return;
    }
    setStatus("Hang készítése...");
    setPlayingState(true);
    let url;
    try {
      url = await clipUrl(sectionIndex, segmentIndex);
    } catch (error) {
      if (myToken !== token) return;
      setStatus(error.status === 503 ? "A felolvasás most nem elérhető (a hangszolgáltatás nem válaszol)." : error.message, true);
      setPlayingState(false);
      return;
    }
    if (myToken !== token) return;
    audio.src = url;
    audio.playbackRate = Number($("listen-speed").value);
    try {
      await audio.play();
      setStatus("");
    } catch {
      setStatus("A böngésző nem engedte elindítani a hangot. Koppints a lejátszásra.", true);
      setPlayingState(false);
      return;
    }
    prefetch(sectionIndex, segmentIndex, myToken);
  }

  function setPlayingState(value) {
    playing = value;
    const button = $("listen-play");
    button.textContent = value ? "⏸" : "▶";
    button.setAttribute("aria-label", value ? "Szünet" : "Lejátszás");
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = value ? "playing" : "paused";
  }

  function pause() {
    token += 1;
    audio.pause();
    setPlayingState(false);
    setStatus("");
    scheduleSave(true);
  }

  async function togglePlay() {
    if (playing) {
      pause();
      return;
    }
    if (audio.src && audio.paused && audio.currentTime > 0 && !audio.ended && audio.dataset.ref === `${sectionIndex}/${segmentIndex}`) {
      setPlayingState(true);
      await audio.play().catch(() => setPlayingState(false));
      return;
    }
    await playCurrent(++token);
  }

  async function step(delta) {
    const next = await neighbour(sectionIndex, segmentIndex, delta).catch(() => null);
    if (next) await goTo(next[0], next[1], playing);
  }

  async function jumpSection(delta) {
    const target = sectionIndex + delta;
    if (target < 0 || target >= doc.sections.length) return;
    await goTo(target, 0, playing);
  }

  audio.addEventListener("ended", () => {
    if (!playing || !doc) return;
    const myToken = token;
    neighbour(sectionIndex, segmentIndex, 1).then((next) => {
      if (myToken !== token || !playing) return; // paused or moved while the lookup ran
      if (next) goTo(next[0], next[1], true);
      else {
        setPlayingState(false);
        setStatus("A dokumentum végére értél.");
      }
    });
  });
  audio.addEventListener("play", () => audio.setAttribute("data-ref", `${sectionIndex}/${segmentIndex}`));
  audio.addEventListener("error", () => {
    if (playing) setStatus("A hang lejátszása nem sikerült.", true);
  });

  function applySpeed() {
    audio.playbackRate = Number($("listen-speed").value);
  }

  // --- resume position and bookmarks (stored on the server, per user) --------------------

  function scheduleSave(now = false) {
    if (!doc) return;
    clearTimeout(saveTimer);
    const wait = now ? 0 : Math.max(0, SAVE_EVERY_MS - (Date.now() - lastSave));
    saveTimer = setTimeout(flushSave, wait);
  }

  function flushSave() {
    if (!doc) return;
    clearTimeout(saveTimer);
    lastSave = Date.now();
    const body = {
      section: sectionIndex,
      segment: segmentIndex,
      speed: Number($("listen-speed").value),
      voice: $("listen-voice").value || null,
    };
    // keepalive lets the request finish while the page is closing.
    fetch(`/listen/${doc.id}/position`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => {});
  }

  function renderBookmarks() {
    const list = $("listen-bookmarks");
    list.replaceChildren(
      ...doc.bookmarks.map((b) =>
        el("li", {}, [
          el("button", { type: "button", class: "secondary go", onclick: () => goTo(b.section, b.segment, playing) }, `🔖 ${b.label}`),
          el("button", { type: "button", class: "danger", "aria-label": "Könyvjelző törlése", onclick: () => removeBookmark(b.id) }, "✕"),
        ])
      )
    );
  }

  async function addBookmark() {
    try {
      const created = await api(`/listen/${doc.id}/bookmarks`, {
        method: "POST",
        body: JSON.stringify({ section: sectionIndex, segment: segmentIndex }),
      });
      doc.bookmarks.push(created);
      doc.bookmarks.sort((a, b) => a.section - b.section || a.segment - b.segment);
      renderBookmarks();
      setStatus("Könyvjelző mentve.");
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function removeBookmark(id) {
    try {
      await api(`/listen/${doc.id}/bookmarks/${id}`, { method: "DELETE" });
    } catch {
      /* already gone */
    }
    doc.bookmarks = doc.bookmarks.filter((b) => b.id !== id);
    renderBookmarks();
  }

  // --- lock screen and headset controls --------------------------------------------------

  function updateMediaSession() {
    if (!("mediaSession" in navigator) || !doc) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: doc.sections[sectionIndex].title,
      artist: doc.title,
      album: "Hallgatás",
    });
  }

  if ("mediaSession" in navigator) {
    const handlers = {
      play: () => !playing && togglePlay(),
      pause: () => playing && pause(),
      previoustrack: () => jumpSection(-1),
      nexttrack: () => jumpSection(1),
      seekbackward: () => step(-1),
      seekforward: () => step(1),
    };
    Object.entries(handlers).forEach(([action, handler]) => {
      try {
        navigator.mediaSession.setActionHandler(action, handler);
      } catch {
        /* this browser lacks the action */
      }
    });
  }

  // --- wiring ----------------------------------------------------------------------------

  $("listen-add").addEventListener("click", addToLibrary);
  $("listen-back").addEventListener("click", closeDocument);
  $("listen-play").addEventListener("click", togglePlay);
  $("listen-prev").addEventListener("click", () => step(-1));
  $("listen-next").addEventListener("click", () => step(1));
  $("listen-prev-section").addEventListener("click", () => jumpSection(-1));
  $("listen-next-section").addEventListener("click", () => jumpSection(1));
  $("listen-section").addEventListener("change", (event) => goTo(Number(event.target.value), 0, playing));
  $("listen-speed").addEventListener("change", () => {
    applySpeed();
    scheduleSave();
  });
  $("listen-voice").addEventListener("change", (event) => {
    localStorage.setItem(VOICE_KEY, event.target.value);
    clips.forEach((promise) => promise.then((url) => URL.revokeObjectURL(url)).catch(() => {}));
    clips.clear();
    scheduleSave();
    if (playing) goTo(sectionIndex, segmentIndex, true);
  });
  $("listen-skip-low").addEventListener("change", (event) =>
    localStorage.setItem(SKIP_KEY, event.target.checked ? "1" : "0")
  );
  $("listen-bookmark").addEventListener("click", addBookmark);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushSave();
  });
  window.addEventListener("pagehide", flushSave);

  // A link such as /#listen-12 opens that document straight away (for the navigation to come).
  loadVoice()
    .then(loadLibrary)
    .then(() => {
      const match = /^#listen-(\d+)$/.exec(window.location.hash);
      if (match) openDocument(Number(match[1]));
    });
})();
