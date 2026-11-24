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
  let busy = false;

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

  function playClip(turn) {
    if (!turn.audio) return;
    const audio = $("ip-audio");
    audio.src = `data:${turn.audio_type || "audio/mpeg"};base64,${turn.audio}`;
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
    $("ip-export").disabled = !sessionId;
    $("ip-delete").disabled = !sessionId;
  }

  // --- One utterance ---------------------------------------------------------

  async function interpret(fields, audio) {
    busy = true;
    updateButtons();
    setStatus("Tolmácsolás...");
    const form = new FormData();
    form.append("mode", mode);
    form.append("glossary", $("ip-glossary").value);
    Object.entries(fields).forEach(([key, value]) => form.append(key, value));
    if (audio) form.append("audio", audio.blob, audio.filename);
    try {
      const turn = await api("/interpreter/turn", { method: "POST", body: form });
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
      button.disabled = !voice.stt || (busy && !active) || Boolean(recorder && !active);
    });
    ["ip-type-hu", "ip-type-en"].forEach((id) => ($(id).disabled = busy));
  }

  function stopRecording() {
    if (recorder && recorder.state === "recording") recorder.stop();
  }

  async function startRecording(side) {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setStatus("A mikrofon nem érhető el. Engedélyezd a böngészőben.", true);
      return false;
    }
    const mimeType = RECORDING_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
    const chunks = [];
    const startedAt = Date.now();
    const current = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    recorder = current;
    recordingSide = side;
    current.addEventListener("dataavailable", (event) => event.data.size && chunks.push(event.data));
    current.addEventListener("stop", () => {
      clearInterval(recordTimer);
      stream.getTracks().forEach((track) => track.stop());
      const type = current.mimeType || "audio/webm";
      const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      recorder = null;
      const spokenSide = recordingSide;
      recordingSide = null;
      updateButtons();
      if (chunks.length) interpret({ side: spokenSide }, { blob: new Blob(chunks, { type }), filename: `felvetel.${extension}` });
    });
    current.start();
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
    let pressedAt = 0;
    let wasRecording = false;
    button.addEventListener("pointerdown", async (event) => {
      if (button.disabled && !(recorder && recordingSide === button.dataset.side)) return;
      event.preventDefault();
      pressedAt = Date.now();
      wasRecording = Boolean(recorder);
      if (wasRecording) {
        stopRecording();
        return;
      }
      await startRecording(button.dataset.side);
    });
    const release = () => {
      if (!pressedAt) return;
      const held = Date.now() - pressedAt;
      pressedAt = 0;
      if (!wasRecording && held > HOLD_MS) stopRecording();
    };
    button.addEventListener("pointerup", release);
    button.addEventListener("pointercancel", release);
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
    }
  }

  async function open(id) {
    if (!id) return;
    try {
      const data = await api(`/interpreter/sessions/${id}`);
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
