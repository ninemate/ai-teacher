(() => {
  const $ = id => document.getElementById("sp-" + id);
  const root = document.getElementById("speech");
  if (!root) return;
  const modes = [...root.querySelectorAll("[data-mode]")];
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let mode = "talks", options, current, recorder, recording, timer, busy = false;
  let voice = {};
  const labels = {
    question_turn_ratio: "Kérdést tartalmazó megszólalások", longest_turn_words: "Leghosszabb megszólalás (szó)",
    monologues: "Hosszú monológok", repetition: "Ismétlődő szókapcsolatok", listening_ratio: "Partner szavait átvevő válaszok",
    time: "Időkeret", pace: "Tempó", fluency: "Folyamatosság", concision: "Tömörség", structure: "Felépítés",
    key_messages: "Kulcsüzenetek", overall: "Összesített pontszám", opening: "Nyitás", value: "Értékajánlat",
    evidence: "Bizonyíték", close: "Zárás"
  };
  const ratios = ["question_turn_ratio", "repetition", "listening_ratio"];
  const value = (key, n) => n == null ? "—" : ratios.includes(key) ? Math.round(n * 100) + "%" : String(n);
  const node = (tag, text) => Object.assign(document.createElement(tag), {textContent: text});
  const line = (text, tag = "p") => $("result").append(node(tag, text));
  function sheet(entries) {
    const dl = document.createElement("dl");
    for (const [label, number] of entries) dl.append(node("dt", label), node("dd", number));
    $("result").append(dl);
  }
  async function api(path, body, method) {
    const init = {method: method || (body ? "POST" : "GET"), headers: {"X-CSRF-Token": csrf}};
    if (body instanceof FormData) init.body = body;
    else if (body) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch("/speech/" + path, init);
    if (res.status === 401) location.assign("/login");
    const data = await res.json();
    if (!res.ok) throw Error(typeof data.detail === "string" ? data.detail : "Ellenőrizd a megadott adatokat.");
    return data;
  }
  function lock(on) {
    $("controls").disabled = on;
    modes.forEach(b => b.disabled = on || !!recorder);
  }
  async function run(fn) {
    if (busy) return;
    busy = true;
    lock(true);
    $("status").textContent = "Feldolgozás…";
    try {
      await fn();
      $("status").textContent = "";
    } catch (err) {
      $("status").textContent = err.message;
    } finally {
      busy = false;
      lock(false);
    }
  }
  function select(id, entries) {
    $(id).replaceChildren(...entries.map(([key, label]) => new Option(label, key)));
  }
  function hint() {
    $("hint").textContent = options[mode].find(s => s.id === $("scenario").value)?.hint || "";
  }
  function clearInput() {
    $("text").value = "";
    $("text").disabled = false;
    $("file").value = "";
    $("clock").textContent = "";
    $("clear").hidden = true;
    recording = null;
  }
  function render() {
    const talk = mode === "talks", active = talk && current?.status === "active";
    $("result").replaceChildren();
    $("start").hidden = !talk || active;
    $("entry").hidden = talk && !active;
    $("finish").hidden = !active;
    $("finish").disabled = !current?.user_turns;
    $("retry").hidden = !talk || current?.status !== "finished";
    $("send").textContent = talk ? "Válasz küldése" : "Próba értékelése";
    $("prompt").textContent = talk ? "A válaszod" : "A bemutatkozó beszéded";
    $("text").maxLength = talk ? 4000 : 6000;
    for (const id of ["scenario", "lang", "style"]) $(id).disabled = active;
    if (!current) return;
    line(options[mode].find(s => s.id === current.scenario).label, "h3");
    if (talk) {
      if (current.focus) line("A próba fókusza: " + options.skills[current.focus]);
      for (const t of current.turns) {
        const p = node("p", (t.role === "user" ? "Te: " : "Partner: ") + t.text);
        p.className = "sp-turn";
        $("result").append(p);
        if (t.metrics?.words_per_minute != null) line(t.metrics.words_per_minute + " szó/perc · " + t.metrics.filler_count + " töltelékszó");
      }
      if (!current.metrics) return;
      line("Beszélgetés szerkezete", "h4");
      const keys = ["question_turn_ratio", "monologues", "longest_turn_words", "repetition", "listening_ratio"];
      sheet(keys.map(k => [labels[k], value(k, current.metrics[k])]));
      for (const item of current.feedback.observations) line(options.skills[item.skill] + " · " + item.note + (item.quote ? " „" + item.quote + "”" : ""));
      line(current.feedback.tip);
      line(current.feedback.retry_instruction);
    } else {
      const m = current.metrics, f = current.feedback;
      line(current.transcript);
      if (current.has_audio) {
        const player = document.createElement("audio");
        player.controls = true;
        player.preload = "none";
        player.src = "/speech/takes/" + current.id + "/audio";
        $("result").append(player);
      }
      line("Mérések", "h4");
      sheet([[m.duration_source === "estimated" ? "Becsült idő" : "Felvétel ideje", m.duration_seconds + " / " + current.target_seconds + " mp"],
        ["Túllépés (mp)", value("", m.overrun_seconds)], ["Szó/perc", value("", m.words_per_minute)], ["Szavak", m.word_count],
        ["Szünetek (időbélyegek alapján)", value("", m.pauses?.count)], ["Töltelékszavak", m.filler_count]]);
      line("Értékelőlap · 0–100 pont", "h4");
      sheet(Object.entries(current.scores).map(([k, n]) => [labels[k], value(k, n)]));
      for (const [k, part] of Object.entries(f.structure)) line(labels[k] + " · " + (part.present ? "„" + part.quote + "”" : "Hiányzik") + " " + part.note);
      for (const item of current.coverage) line((item.covered ? "Elhangzott: " : "Nem talált kulcsüzenet: ") + item.message);
      line(f.clarity);
      line(f.tip);
      for (const r of f.rewordings) line("„" + r.original + "” → „" + r.suggestion + "” " + r.why);
      line("A hallgató kérdései", "h4");
      line("Válaszolj rájuk gépelve, vagy készíts felvételt fent, és nyomd meg a kérdés Válasz gombját.");
      current.followups.questions.forEach((q, i) => {
        line(q.question, "h5");
        if (q.answer != null) {
          line("A válaszod: " + q.answer);
          if (q.metrics?.words_per_minute != null) line(q.metrics.words_per_minute + " szó/perc · " + q.metrics.filler_count + " töltelékszó");
          line((q.feedback.answered ? "Megválaszolta a kérdést. " : "Nem válaszolta meg a kérdést. ") + q.feedback.note + " " + q.feedback.tip);
          return;
        }
        const box = node("textarea", "");
        box.rows = 2;
        box.maxLength = 4000;
        box.setAttribute("aria-label", "Válasz erre: " + q.question);
        const send = node("button", "Válasz");
        send.onclick = () => run(async () => {
          const form = new FormData();
          form.set("index", String(i));
          if (recording) form.set("audio", recording, "answer");
          else if (box.value.trim()) form.set("text", box.value.trim());
          else throw Error("Írd be a válaszodat, vagy készíts felvételt.");
          openAttempt(await api("takes/" + current.id + "/answer", form));
        });
        $("result").append(box, send);
      });
    }
    const changes = Object.entries(current.comparison || {});
    if (changes.length) {
      line("Összehasonlítás · előző → most", "h4");
      sheet(changes.map(([k, d]) => [labels[k], value(k, d.before) + " → " + value(k, d.now)]));
    }
  }
  function setup(next) {
    mode = next;
    current = null;
    clearInput();
    modes.forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    $("style-label").hidden = mode !== "talks";
    for (const id of ["interest-label", "target-label", "keys-label"]) $(id).hidden = mode === "talks";
    select("scenario", options[mode].map(s => [s.id, (s.category ? s.category + " · " : "") + s.label]));
    $("history").open = false;
    hint();
    render();
  }
  function openAttempt(data) {
    current = data;
    $("scenario").value = data.scenario;
    $("lang").value = data.language;
    if (mode === "talks") $("style").value = data.style;
    else {
      $("target").value = data.target_seconds;
      $("interest").value = data.followups.interest;
      $("keys").value = data.key_messages.join("\n");
    }
    clearInput();
    $("history").open = false;
    hint();
    render();
  }
  $("scenario").onchange = hint;
  modes.forEach(b => b.onclick = () => setup(b.dataset.mode));
  $("start").onclick = () => run(async () => openAttempt(await api("talks", {
    scenario: $("scenario").value, style: $("style").value, language: $("lang").value
  })));
  $("finish").onclick = () => run(async () => openAttempt(await api("talks/" + current.id + "/finish", null, "POST")));
  $("retry").onclick = () => run(async () => openAttempt(await api("talks/" + current.id + "/retry", null, "POST")));
  $("send").onclick = () => run(async () => {
    const form = new FormData();
    if (recording) form.set("audio", recording, "take");
    else if ($("text").value.trim()) form.set("text", $("text").value.trim());
    else throw Error("Írj be szöveget vagy adj meg egy hangfelvételt.");
    if (mode === "takes") {
      for (const [key, id] of [["scenario","scenario"],["target","target"],["language","lang"],["interest","interest"],["messages","keys"]]) form.set(key, $(id).value);
    }
    openAttempt(await api(mode === "talks" ? "talks/" + current.id + "/turn" : "takes", form));
  });
  $("history").ontoggle = () => {
    if (!$("history").open) return;
    run(async () => {
      const rows = (await api("history"))[mode];
      $("list").replaceChildren();
      if (!rows.length) $("list").textContent = "Még nincs mentett próba.";
      for (const r of rows) {
        const title = options[mode].find(s => s.id === r.scenario).label;
        const detail = mode === "takes" ? r.target_seconds + " mp · " + r.scores.overall + " pont" : r.status === "active" ? "folyamatban" : "lezárva";
        const b = node("button", title + " · " + r.language.toUpperCase() + " · " + detail + " · #" + r.id);
        b.onclick = () => run(async () => openAttempt(await api(mode + "/" + r.id)));
        $("list").append(b);
      }
    });
  };
  function useAudio(blob, label) {
    recording = blob;
    $("clear").hidden = !blob;
    $("text").disabled = !!blob;
    $("clock").textContent = label;
  }
  $("clear").onclick = () => {
    $("file").value = "";
    useAudio(null, "");
  };
  $("file").onchange = () => useAudio($("file").files[0], $("file").files[0]?.name || "");
  function recordingControls(active) {
    for (const id of ["send","finish","file","clear","target","scenario","lang","interest","keys"]) $(id).disabled = active;
    $("history").hidden = active;
    modes.forEach(b => b.disabled = active);
    $("text").disabled = active || !!recording;
    if (!active) render();
  }
  $("record").onclick = async () => {
    if (recorder) {
      if (recorder.state === "recording") recorder.stop();
      return;
    }
    let stream;
    lock(true);
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio: true});
      recorder = new MediaRecorder(stream);
      const chunks = [], start = performance.now();
      const target = mode === "takes" ? Number($("target").value) : null;
      recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = () => {
        clearInterval(timer);
        stream.getTracks().forEach(track => track.stop());
        useAudio(new Blob(chunks, {type: recorder.mimeType}), "Felvétel kész · " + Math.round((performance.now()-start)/1000) + " mp");
        recorder = null;
        $("record").textContent = "Új felvétel";
        recordingControls(false);
      };
      recorder.start();
      lock(false);
      recordingControls(true);
      $("record").textContent = "Felvétel leállítása";
      const tick = () => {
        const sec = Math.floor((performance.now()-start)/1000);
        $("clock").textContent = sec + (target ? " / " + target : "") + " mp" + (target && sec > target ? " · időkeret túllépve" : "");
        if (sec >= (voice.max_seconds || 120) && recorder?.state === "recording") recorder.stop();
      };
      timer = setInterval(tick, 250);
      tick();
    } catch {
      stream?.getTracks().forEach(track => track.stop());
      recorder = null;
      lock(false);
      $("status").textContent = "A mikrofon nem indítható. Válassz hangfájlt vagy gépelj.";
    }
  };
  window.addEventListener("pagehide", () => {
    if (recorder?.state === "recording") recorder.stop();
  });
  run(async () => {
    options = await api("options");
    select("style", Object.entries(options.styles));
    select("interest", Object.entries(options.interest));
    $("interest").value = "medium";
    setup("talks");
  });
  fetch("/voice/status").then(r => r.json()).then(data => {
    voice = data;
    $("voice").hidden = !voice.stt;
    $("record").hidden = !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder;
  }).catch(() => {});
})();
