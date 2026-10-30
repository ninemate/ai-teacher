// Optional voice: dictate the question (offline Whisper) and read the answer aloud (offline Piper).
// Everything stays hidden unless /voice/status reports the voice service.
(() => {
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const questionField = document.getElementById("question");
  const dictation = document.getElementById("dictation");
  const micButton = document.getElementById("mic-button");
  const fileLabel = document.getElementById("audio-file-label");
  const fileInput = document.getElementById("audio-file");
  const dictationStatus = document.getElementById("dictation-status");
  const readAloud = document.getElementById("read-aloud");
  const speakButton = document.getElementById("speak-button");
  const voiceSelect = document.getElementById("voice-select");
  const speakStatus = document.getElementById("speak-status");
  const audioEl = document.getElementById("answer-audio");

  const VOICE_NAMES = {
    "hu_HU-imre-medium": "Imre (férfihang)",
    "hu_HU-anna-medium": "Anna (női hang)",
    "hu_HU-berta-medium": "Berta (női hang)",
  };
  const VOICE_KEY = "teacher.voice";
  const RECORDING_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];

  let currentAnswer = "";
  let audioUrl = null;
  let recorder = null;
  let recordTimer = null;
  let maxSeconds = 120;

  function goToLogin() {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
  }

  async function errorText(response, fallback) {
    try {
      const data = await response.json();
      return typeof data.detail === "string" ? data.detail : fallback;
    } catch {
      return fallback;
    }
  }

  // --- Dictation ---------------------------------------------------------

  async function sendAudio(blob, filename) {
    dictationStatus.textContent = "Szöveg felismerése...";
    micButton.disabled = true;
    const form = new FormData();
    form.append("audio", blob, filename);
    try {
      const response = await fetch("/voice/transcribe", {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
        body: form,
      });
      if (response.status === 401) {
        goToLogin();
        return;
      }
      if (!response.ok) {
        dictationStatus.textContent = await errorText(response, "A felismerés nem sikerült.");
        return;
      }
      const data = await response.json();
      const text = (data.text || "").trim();
      if (!text) {
        dictationStatus.textContent = "Nem hallottam beszédet. Próbáld újra, közelebb a mikrofonhoz.";
        return;
      }
      const existing = questionField.value.trim();
      questionField.value = existing ? `${existing} ${text}` : text;
      questionField.focus();
      dictationStatus.textContent = "Kész. Nézd át a szöveget, aztán kérdezz.";
    } catch {
      dictationStatus.textContent = "A hangszolgáltatás nem érhető el.";
    } finally {
      micButton.disabled = false;
    }
  }

  function stopRecording() {
    if (recorder && recorder.state === "recording") {
      recorder.stop();
    }
  }

  async function startRecording() {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      dictationStatus.textContent = "A mikrofon nem érhető el. Engedélyezd a böngészőben.";
      return;
    }
    const mimeType = RECORDING_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const chunks = [];
    const startedAt = Date.now();
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size) chunks.push(event.data);
    });
    recorder.addEventListener("stop", () => {
      clearInterval(recordTimer);
      stream.getTracks().forEach((track) => track.stop());
      micButton.textContent = "🎙 Diktálás";
      micButton.classList.remove("recording");
      micButton.setAttribute("aria-pressed", "false");
      const type = recorder.mimeType || "audio/webm";
      const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      recorder = null;
      sendAudio(new Blob(chunks, { type }), `felvetel.${extension}`);
    });
    recorder.start();
    micButton.textContent = "⏹ Leállítás";
    micButton.classList.add("recording");
    micButton.setAttribute("aria-pressed", "true");
    dictationStatus.textContent = "Felvétel... Beszélj, majd nyomd meg a Leállítás gombot.";
    recordTimer = setInterval(() => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      dictationStatus.textContent = `Felvétel... ${seconds} mp`;
      if (seconds >= maxSeconds) stopRecording();
    }, 500);
  }

  micButton.addEventListener("click", () => (recorder ? stopRecording() : startRecording()));

  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) sendAudio(file, file.name);
    fileInput.value = "";
  });

  // --- Read aloud --------------------------------------------------------

  function resetAudio() {
    audioEl.pause();
    audioEl.hidden = true;
    audioEl.removeAttribute("src");
    if (audioUrl) {
      URL.revokeObjectURL(audioUrl);
      audioUrl = null;
    }
  }

  document.addEventListener("teacher:answer", (event) => {
    currentAnswer = (event.detail && event.detail.answer) || "";
    resetAudio();
    speakStatus.textContent = "";
    speakButton.disabled = !currentAnswer;
  });

  voiceSelect.addEventListener("change", () => {
    localStorage.setItem(VOICE_KEY, voiceSelect.value);
    resetAudio();
  });

  speakButton.addEventListener("click", async () => {
    if (!currentAnswer) return;
    resetAudio();
    speakButton.disabled = true;
    speakStatus.textContent = "Hang készül...";
    try {
      const response = await fetch("/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ text: currentAnswer, voice: voiceSelect.value || null }),
      });
      if (response.status === 401) {
        goToLogin();
        return;
      }
      if (!response.ok) {
        speakStatus.textContent = await errorText(response, "A felolvasás nem sikerült.");
        return;
      }
      audioUrl = URL.createObjectURL(await response.blob());
      audioEl.src = audioUrl;
      audioEl.hidden = false;
      speakStatus.textContent = "";
      // Some phones refuse autoplay after an await; the visible player still works with one tap.
      audioEl.play().catch(() => {
        speakStatus.textContent = "Indítsd el a lejátszást a lejátszón.";
      });
    } catch {
      speakStatus.textContent = "A hangszolgáltatás nem érhető el.";
    } finally {
      speakButton.disabled = !currentAnswer;
    }
  });

  // --- Setup -------------------------------------------------------------

  async function init() {
    let status;
    try {
      const response = await fetch("/voice/status");
      if (!response.ok) return;
      status = await response.json();
    } catch {
      return;
    }
    if (!status.available) return;

    if (status.stt) {
      maxSeconds = status.max_seconds || maxSeconds;
      dictation.hidden = false;
      const canRecord = window.isSecureContext && navigator.mediaDevices && window.MediaRecorder;
      if (canRecord) {
        micButton.hidden = false;
      } else {
        // Browsers only allow the microphone over HTTPS (or on localhost); a recorded file still works.
        fileLabel.hidden = false;
        dictationStatus.textContent = "Élő diktáláshoz HTTPS kapcsolat kell. Addig felvett hangfájlt küldhetsz.";
      }
    }

    if (status.tts && status.voices.length) {
      const saved = localStorage.getItem(VOICE_KEY);
      status.voices.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = VOICE_NAMES[name] || name;
        voiceSelect.appendChild(option);
      });
      voiceSelect.value = status.voices.includes(saved) ? saved : status.default_voice || status.voices[0];
      voiceSelect.hidden = status.voices.length < 2;
      readAloud.hidden = false;
    }
  }

  init();
})();
