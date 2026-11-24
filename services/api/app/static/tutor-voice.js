// Offline turn-taking. Keep one microphone stream for a hands-free session,
// mute it during playback, and discard unfinished audio when the session ends.
window.createTutorVoice = function ({language, onTurn, status}) {
  const hold = document.getElementById("tutor-ptt");
  const handsfree = document.getElementById("tutor-handsfree");
  const feedback = document.getElementById("tutor-acoustics");
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const idleLabel = handsfree.textContent;
  let operation = null;
  let context = null;
  let source = null;
  let playbackRequest = null;
  let held = false;

  function audioContext() {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) throw new Error("A böngésző nem támogatja a hanglejátszást.");
    context ||= new Context();
    return context;
  }

  function stopPlayback() {
    playbackRequest?.abort();
    playbackRequest = null;
    if (source) {
      source.stop();
      source = null;
    }
  }

  function stop() {
    const current = operation;
    operation = null;
    held = false;
    current?.abort.abort();
    if (current?.recorder?.state === "recording") current.recorder.stop();
    current?.stream?.getTracks().forEach(track => track.stop());
    stopPlayback();
    hold.setAttribute("aria-pressed", "false");
    handsfree.setAttribute("aria-pressed", "false");
    handsfree.textContent = idleLabel;
  }

  async function checked(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      throw new Error(typeof problem.detail === "string" ? problem.detail : "A hangszolgáltatás nem érhető el.");
    }
    return response;
  }

  async function speak(text) {
    if (!text) return;
    stopPlayback();
    const request = new AbortController();
    playbackRequest = request;
    const audio = audioContext();
    await audio.resume();
    const {voices, tts} = await (await checked("/voice/status", {signal: request.signal})).json();
    const voice = voices?.find(name => name.startsWith(language() + "_"));
    if (!tts || !voice) throw new Error("A célnyelvhez nincs telepített felolvasóhang.");
    const response = await checked("/voice/speak", {
      method: "POST", signal: request.signal,
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify({text, voice}),
    });
    const buffer = await audio.decodeAudioData(await response.arrayBuffer());
    if (request.signal.aborted) return;
    const player = audio.createBufferSource();
    player.buffer = buffer;
    player.connect(audio.destination);
    source = player;
    await new Promise(resolve => {
      player.onended = resolve;
      player.start();
    });
    player.disconnect();
    if (source === player) source = null;
  }

  function record(current, limit) {
    const stream = current.stream;
    stream.getAudioTracks().forEach(track => { track.enabled = true; });
    const recorder = new MediaRecorder(stream);
    current.recorder = recorder;
    const chunks = [];
    const audio = audioContext();
    const analyser = audio.createAnalyser();
    const mic = audio.createMediaStreamSource(stream);
    mic.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const started = performance.now();
    let lastSpeech = 0;
    let timedOut = false;
    status(current.auto ? "Figyelek. Befejezéshez mondd: „gyakorlás vége” vagy „end practice”." : "Felvétel…");
    return new Promise((resolve, reject) => {
      recorder.ondataavailable = event => chunks.push(event.data);
      recorder.onerror = () => {
        clearInterval(timer);
        mic.disconnect();
        reject(new Error("A hangfelvétel megszakadt."));
      };
      recorder.onstop = () => {
        clearInterval(timer);
        mic.disconnect();
        stream.getAudioTracks().forEach(track => { track.enabled = false; });
        current.recorder = null;
        resolve(timedOut ? null : new Blob(chunks, {type: recorder.mimeType}));
      };
      recorder.start();
      const timer = setInterval(() => {
        if (recorder.state !== "recording") return;
        const now = performance.now();
        analyser.getFloatTimeDomainData(samples);
        const rms = Math.sqrt(samples.reduce((sum, x) => sum + x * x, 0) / samples.length);
        if (rms > 0.018) lastSpeech = now;
        const elapsed = now - started;
        timedOut = current.auto && !lastSpeech && elapsed >= Math.min(15000, limit);
        const finished = current.auto && lastSpeech && now - lastSpeech >= 1300;
        if (timedOut || finished || elapsed >= limit) recorder.stop();
      }, 100);
    });
  }

  function showMeasurements(metrics = {}) {
    const facts = [];
    if (Number.isFinite(metrics.duration)) facts.push('Felvétel: ' + metrics.duration + ' mp');
    if (Number.isFinite(metrics.words_per_minute)) facts.push('Tempó: ' + metrics.words_per_minute + ' szó/perc');
    if (Number.isFinite(metrics.pause_count)) facts.push('Legalább fél másodperces szóközi szünet: ' + metrics.pause_count);
    if (Number.isFinite(metrics.mean_word_probability)) {
      facts.push('Átlagos szófelismerési bizonyosság: ' + Math.round(metrics.mean_word_probability * 100) + '%');
    }
    feedback.textContent = facts.join(' · ');
  }

  async function begin(auto) {
    if (operation) return;
    const current = {auto, abort: new AbortController()};
    operation = current;
    const {signal} = current.abort;
    const button = auto ? handsfree : hold;
    button.setAttribute("aria-pressed", "true");
    if (auto) handsfree.textContent = "Kihangosított mód leállítása";
    try {
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        throw new Error("A mikrofonhoz HTTPS és hangrögzítést támogató böngésző szükséges.");
      }
      stopPlayback();
      await audioContext().resume();
      const info = await (await checked("/voice/status", {signal})).json();
      if (!info.stt || !info.tts || !info.voices?.some(name => name.startsWith(language() + "_"))) {
        throw new Error("A hangos gyakorláshoz beszédfelismerés és célnyelvi felolvasóhang szükséges.");
      }
      if (!auto && !held) return;
      const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true}});
      current.stream = stream;
      if (signal.aborted || (!auto && !held)) {
        stream.getTracks().forEach(track => track.stop());
        return;
      }
      do {
        const blob = await record(current, Math.max(1000, ((info.max_seconds || 120) - 1) * 1000));
        if (signal.aborted) return;
        if (!blob) break;
        status("Beszéd felismerése…");
        const form = new FormData();
        const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
        form.append("audio", blob, "gyakorlas." + ext);
        form.append("language", language());
        form.append("measure", "true");
        const result = await (await checked("/voice/transcribe", {
          method: "POST", body: form, signal, headers: {"X-CSRF-Token": csrf},
        })).json();
        if (signal.aborted) return;
        const text = result.text?.trim();
        if (!text) {
          status("Nem hallottam beszédet.");
          break;
        }
        if (auto && /^(gyakorlás vége|end practice)[.!?]?$/i.test(text)) break;
        showMeasurements(result.acoustics);
        const response = await onTurn(text);
        if (signal.aborted) return;
        await speak(response);
      } while (auto && !signal.aborted);
      if (auto && !signal.aborted) status("A kihangosított gyakorlás véget ért.");
    } catch (error) {
      if (!signal.aborted) status(error.name === "NotAllowedError" ? "A mikrofon használatát engedélyezni kell." : error.message);
    } finally {
      if (current === operation) stop();
    }
  }

  function press() {
    held = true;
    begin(false);
  }
  function release() {
    held = false;
    const recorder = operation?.recorder;
    if (!operation?.auto && recorder?.state === "recording") recorder.stop();
  }
  hold.addEventListener("pointerdown", event => {
    if (!event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    hold.setPointerCapture(event.pointerId);
    press();
  });
  hold.addEventListener("pointerup", release);
  hold.addEventListener("lostpointercapture", release);
  hold.addEventListener("keydown", event => {
    if ([" ", "Enter"].includes(event.key)) {
      event.preventDefault();
      if (!event.repeat) press();
    }
  });
  hold.addEventListener("keyup", release);
  handsfree.addEventListener("click", () => {
    if (operation) {
      stop();
      status("A hangos gyakorlás leállt.");
    } else begin(true);
  });
  window.addEventListener("pagehide", stop);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
  return {stop, speak};
};
