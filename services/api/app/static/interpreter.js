// Tolmács: push-to-talk interpreting HU<->EN. One request per utterance (speech -> text -> translation -> speech);
// the server measures each stage and the page shows the figures under every row. Texts are only ever set with
// textContent. Audio is not stored; a saved session keeps the transcript and the latency figures.
(() => {
  const root = document.getElementById("interpreter");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const MODE_KEY = "teacher.interpreter.mode";
  const GLOSSARY_KEY = "teacher.interpreter.glossary";
  const RECORDING_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  const HOLD_MS = 450; // held longer than this: release ends the recording; a shorter tap keeps it running
  const MAX_SECONDS = 60;

  let config = null;
  let voice = { available: false, stt: false, tts: false };
  let mode = localStorage.getItem(MODE_KEY) || "fast";
  let turns = []; // {source_lang, target_lang, source_text, translation, latency, audio?}
  let sessionId = null;
  let recorder = null;
  let recordingSide = null;
  let recordTimer = null;
  let busy = false; // an utterance is being interpreted
  let starting = false; // the microphone is being opened (one capture is reserved before the permission prompt)
  let saving = false; // a save or open is in flight
  let epoch = 0; // bumped whenever the active session changes, so late answers cannot land in another one

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
    $("ip-status").textContent = message || "";
    $("ip-status").classList.toggle("error", isError);
  }
  function setSessionStatus(message, isError = false) {
    $("ip-session-status").textContent = message || "";
    $("ip-session-status").classList.toggle("error", isError);
  }

  async function api(path, options = {}) {
    const isForm = options.body instanceof FormData;
    const response = await fetch(path, {
      ...options,
      headers: { ...(isForm ? {} : { "Content-Type": "application/json" }), "X-CSRF-Token": csrfToken },
    });
    if (response.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      throw new Error("login");
    }
    if (!response.ok) {
      let detail = "Hiba történt.";
      try {
        const data = await response.json();
        if (typeof data.detail === "string") detail = data.detail;
      } catch {}
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  const seconds = (ms) => (ms == null ? "–" : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} mp`);

  // --- Transcript ----------------------------------------------------------

  // The page's security policy allows blob: media but not data: URLs, so the base64 clip becomes a Blob.
  let clipUrl = null;
  function playClip(turn) {
    if (!turn.audio) return;
    const audio = $("ip-audio");
    const bytes = Uint8Array.from(atob(turn.audio), (char) => char.charCodeAt(0));
    if (clipUrl) URL.revokeObjectURL(clipUrl);
    clipUrl = URL.createObjectURL(new Blob([bytes], { type: turn.audio_type || "audio/mpeg" }));
    audio.src = clipUrl;
    audio.play().catch(() => setStatus("Indítsd el a hangot a 🔊 gombbal.", true));
  }

  function renderTurn(turn) {
    const spoken = el("div", { class: "ip-cell spoken" }, turn.source_text);
    const translated = el("div", { class: "ip-cell translated" }, turn.translation);
    const [left, right] = turn.source_lang === "hu" ? [spoken, translated] : [translated, spoken];
    const lat = turn.latency || {};
    const meta = el("div", { class: "ip-meta" }, [
      el("span", {}, `Felismerés ${seconds(lat.stt_ms)}`),
      el("span", {}, `Fordítás ${seconds(lat.translate_ms)}`),
      el("span", {}, `Hang ${seconds(lat.tts_ms)}`),
      el("strong", {}, `Összesen ${seconds(lat.total_ms)}`),
    ]);
    if (turn.audio) {
      meta.append(el("button", { type: "button", class: "secondary", "aria-label": "Újra lejátszás", onclick: () => playClip(turn) }, "🔊"));
    } else if (turn.tts_error) {
      meta.append(el("span", { class: "warn" }, turn.tts_error));
    }
    return el("div", { class: "ip-row" }, [left, right, meta]);
  }

  function renderTranscript() {
    const box = $("ip-transcript");
    box.replaceChildren();
    if (!turns.length) box.append(el("p", { class: "empty" }, "Még nincs mondat."));
    turns.forEach((turn) => box.append(renderTurn(turn)));
    box.scrollTop = box.scrollHeight;
    // Export links name one session id: never leave them standing after the active session changed.
    document.querySelector(".ip-export-links")?.remove();
    updateButtons();
  }

  // --- One utterance ---------------------------------------------------------

  async function interpret(fields, audio) {
    busy = true;
    const startedIn = epoch;
    updateButtons();
    setStatus("Tolmácsolás...");
    const form = new FormData();
    form.append("mode", mode);
    form.append("glossary", $("ip-glossary").value);
    Object.entries(fields).forEach(([key, value]) => form.append(key, value));
    if (audio) form.append("audio", audio.blob, audio.filename);
    try {
      const turn = await api("/interpreter/turn", { method: "POST", body: form });
      if (startedIn !== epoch) return; // the session changed meanwhile; the answer belongs to the old one
      turns.push(turn);
      renderTranscript();
      setStatus(turn.tts_error ? turn.tts_error : "");
      playClip(turn);
      if ($("ip-latency-box").open) showLatency();
    } catch (error) {
      if (error.message !== "login") setStatus(error.message || "A tolmácsolás nem sikerült.", true);
    } finally {
      busy = false;
      updateButtons();
    }
  }

  // --- Push to talk ----------------------------------------------------------

  function updateButtons() {
    document.querySelectorAll(".ip-talk-button").forEach((button) => {
      const active = recorder && recordingSide === button.dataset.side;
      button.classList.toggle("recording", Boolean(active));
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.disabled = !voice.stt || (busy && !active) || saving || Boolean(recorder && !active);
    });
    ["ip-type-hu", "ip-type-en"].forEach((id) => ($(id).disabled = busy || saving));
    // Moving between sessions while a recording, an utterance or a save is pending would mix transcripts.
    const locked = busy || starting || saving || Boolean(recorder);
    ["ip-new", "ip-saved", "ip-save"].forEach((id) => ($(id).disabled = locked));
    $("ip-export").disabled = !sessionId || locked;
    $("ip-delete").disabled = !sessionId || locked;
  }

  function stopRecording() {
    if (recorder && recorder.state === "recording") recorder.stop();
  }

  // `press` is shared with the pointer handlers: if the person lets go of a hold before the microphone is open,
  // nothing is recorded (and the stream is closed at once).
  async function startRecording(side, press = { abandoned: false }) {
    if (recorder || starting || busy || saving) return false;
    starting = true;
    updateButtons();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      starting = false;
      updateButtons();
      setStatus("A mikrofon nem érhető el. Engedélyezd a böngészőben.", true);
      return false;
    }
    starting = false;
    const closeStream = () => stream.getTracks().forEach((track) => track.stop());
    if (press.abandoned) {
      closeStream();
      updateButtons();
      setStatus("");
      return false;
    }
    const chunks = [];
    const startedAt = Date.now();
    let current;
    try {
      const mimeType = RECORDING_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
      current = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      current.start();
    } catch {
      closeStream();
      updateButtons();
      setStatus("A felvétel ebben a böngészőben nem indítható.", true);
      return false;
    }
    recorder = current;
    recordingSide = side;
    current.addEventListener("dataavailable", (event) => event.data.size && chunks.push(event.data));
    current.addEventListener("stop", () => {
      clearInterval(recordTimer);
      closeStream();
      const type = current.mimeType || "audio/webm";
      const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      recorder = null;
      const spokenSide = recordingSide;
      recordingSide = null;
      updateButtons();
      if (chunks.length) interpret({ side: spokenSide }, { blob: new Blob(chunks, { type }), filename: `felvetel.${extension}` });
    });
    updateButtons();
    recordTimer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);
      setStatus(`Felvétel... ${elapsed} mp. Koppints újra a befejezéshez.`);
      if (elapsed >= MAX_SECONDS) stopRecording();
    }, 250);
    return true;
  }

  // Hold the button and release to send, or tap once to start and tap again to stop.
  function wireTalkButton(button) {
    let press = null; // {pointerId, at, abandoned, wasRecording}
    button.addEventListener("pointerdown", (event) => {
      if (press) return; // one pointer at a time
      const mine = recorder && recordingSide === button.dataset.side;
      if (button.disabled && !mine) return;
      event.preventDefault();
      // Capture the pointer so a release outside the button still reaches this button.
      try {
        button.setPointerCapture(event.pointerId);
      } catch {}
      press = { pointerId: event.pointerId, at: Date.now(), abandoned: false, wasRecording: Boolean(recorder) };
      if (press.wasRecording) return stopRecording();
      startRecording(button.dataset.side, press);
    });
    const release = (event) => {
      if (!press || event.pointerId !== press.pointerId) return;
      const finished = press;
      press = null;
      if (finished.wasRecording) return;
      const held = Date.now() - finished.at;
      if (event.type === "pointercancel" || held > HOLD_MS) {
        finished.abandoned = true; // still opening the microphone: record nothing
        stopRecording();
      }
    };
    button.addEventListener("pointerup", release);
    button.addEventListener("pointercancel", release);
    button.addEventListener("lostpointercapture", release);
    button.addEventListener("contextmenu", (event) => event.preventDefault());
    // Keyboard: Enter or Space toggles.
    button.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      if (event.repeat) return;
      recorder ? stopRecording() : startRecording(button.dataset.side);
    });
  }

  // --- Modes, glossary --------------------------------------------------------

  function renderModes() {
    const box = $("ip-modes");
    box.replaceChildren();
    Object.entries(config.modes).forEach(([name, info]) => {
      box.append(
        el(
          "button",
          {
            type: "button",
            role: "radio",
            class: "secondary",
            "aria-checked": String(name === mode),
            onclick: () => {
              mode = name;
              localStorage.setItem(MODE_KEY, mode);
              renderModes();
            },
          },
          info.label,
        ),
      );
    });
    $("ip-glossary-box").hidden = mode !== "professional";
  }

  // --- Sessions ---------------------------------------------------------------

  const stripped = (turn) => ({
    source_lang: turn.source_lang,
    target_lang: turn.target_lang,
    source_text: turn.source_text,
    translation: turn.translation,
    latency: turn.latency,
  });

  async function refreshSaved(selectId) {
    const select = $("ip-saved");
    const list = await api("/interpreter/sessions");
    select.replaceChildren(el("option", { value: "" }, list.length ? "Válassz..." : "Még nincs mentett munkamenet"));
    list.forEach((item) => select.append(el("option", { value: item.id }, `${item.title} (${item.turns} mondat)`)));
    select.value = selectId || "";
  }

  async function save() {
    if (!turns.length) return setSessionStatus("Még nincs mit menteni.", true);
    const body = JSON.stringify({ title: $("ip-title").value, mode, turns: turns.map(stripped) });
    saving = true;
    updateButtons();
    try {
      const saved = sessionId
        ? await api(`/interpreter/sessions/${sessionId}`, { method: "PUT", body })
        : await api("/interpreter/sessions", { method: "POST", body });
      sessionId = saved.id;
      $("ip-title").value = saved.title;
      await refreshSaved(sessionId);
      renderTranscript();
      setSessionStatus("Mentve.");
    } catch (error) {
      setSessionStatus(error.message, true);
    } finally {
      saving = false;
      updateButtons();
    }
  }

  async function open(id) {
    if (!id || busy || starting || saving || recorder) return;
    saving = true;
    updateButtons();
    try {
      const data = await api(`/interpreter/sessions/${id}`);
      epoch += 1;
      turns = data.turns;
      sessionId = data.id;
      $("ip-title").value = data.title;
      if (config.modes[data.mode]) {
        mode = data.mode;
        renderModes();
      }
      renderTranscript();
      setSessionStatus("");
    } catch (error) {
      setSessionStatus(error.message, true);
      $("ip-saved").value = sessionId || "";
    } finally {
      saving = false;
      updateButtons();
    }
  }

  function showExportLinks() {
    if (!sessionId) return;
    document.querySelector(".ip-export-links")?.remove();
    const links = el(
      "div",
      { class: "ip-export-links" },
      ["md", "txt", "csv", "json"].map((format) =>
        el("a", { href: `/interpreter/sessions/${sessionId}/export?format=${format}`, download: "" }, format.toUpperCase()),
      ),
    );
    document.querySelector(".ip-actions").after(links);
  }

  // --- Latency ----------------------------------------------------------------

  async function showLatency() {
    const box = $("ip-latency");
    try {
      const data = await api("/interpreter/latency");
      box.replaceChildren();
      const modes = Object.entries(data);
      if (!modes.length) return box.append(el("p", { class: "empty" }, "Még nincs mért adat."));
      const names = { stt_ms: "Felismerés", translate_ms: "Fordítás", tts_ms: "Hang", total_ms: "Összesen" };
      modes.forEach(([name, info]) => {
        const head = el("tr", {}, ["", "n", "p50", "p95"].map((text) => el("th", {}, text)));
        const rows = Object.entries(info.stages).map(([stage, v]) =>
          el("tr", {}, [names[stage], v.n, seconds(v.p50), seconds(v.p95)].map((text) => el("td", {}, String(text)))),
        );
        box.append(el("h3", {}, `${config.modes[name]?.label || name} (${info.count} mondat)`), el("table", {}, [head, ...rows]));
      });
    } catch (error) {
      box.textContent = error.message;
    }
  }

  // --- Setup --------------------------------------------------------------------

  async function init() {
    try {
      config = await api("/interpreter/config");
    } catch {
      root.hidden = true;
      return;
    }
    try {
      const response = await fetch("/voice/status");
      if (response.ok) voice = await response.json();
    } catch {}
    $("ip-voice-off").hidden = voice.stt && voice.tts;
    if (!config.modes[mode]) mode = Object.keys(config.modes)[0];
    $("ip-glossary").value = localStorage.getItem(GLOSSARY_KEY) || "";
    $("ip-glossary").addEventListener("input", () => localStorage.setItem(GLOSSARY_KEY, $("ip-glossary").value));
    renderModes();
    document.querySelectorAll(".ip-talk-button").forEach(wireTalkButton);
    ["hu", "en"].forEach((side) =>
      $(`ip-type-${side}`).addEventListener("click", () => {
        const text = $("ip-text").value.trim();
        if (!text || busy) return;
        $("ip-text").value = "";
        interpret({ side, text }, null);
      }),
    );
    $("ip-type").addEventListener("submit", (event) => event.preventDefault());
    $("ip-save").addEventListener("click", save);
    $("ip-new").addEventListener("click", () => {
      epoch += 1;
      turns = [];
      sessionId = null;
      $("ip-title").value = "";
      $("ip-saved").value = "";
      document.querySelector(".ip-export-links")?.remove();
      renderTranscript();
      setSessionStatus("");
    });
    $("ip-saved").addEventListener("change", () => open($("ip-saved").value));
    $("ip-export").addEventListener("click", showExportLinks);
    $("ip-delete").addEventListener("click", async () => {
      if (!sessionId || !window.confirm("Biztosan törlöd ezt a munkamenetet?")) return;
      try {
        await api(`/interpreter/sessions/${sessionId}`, { method: "DELETE" });
        $("ip-new").click();
        await refreshSaved();
      } catch (error) {
        setSessionStatus(error.message, true);
      }
    });
    $("ip-latency-box").addEventListener("toggle", () => $("ip-latency-box").open && showLatency());
    updateButtons();
    renderTranscript();
    refreshSaved().catch(() => {});
    if (!voice.stt) setStatus("A beszédfelismerés nem érhető el, gépelj a mező alatt.");
  }

  init();
})();
