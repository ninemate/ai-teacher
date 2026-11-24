// Meetingek panel: upload a recording, follow the background job, read the grounded notes and the transcript.
// Everything from the server is model output, transcript text or file names: only ever set as text, never as HTML.
(() => {
  const root = document.getElementById("meetings");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const statusEl = $("mt-status");
  const jobsEl = $("mt-jobs");
  const resultEl = $("mt-result");
  const POLL_MS = 3000;
  const STATUS_NAMES = {
    queued: "várakozik", running: "fut", cancelling: "leállítás alatt", succeeded: "kész", failed: "sikertelen",
    cancelled: "leállítva",
  };
  const SECTIONS = [
    ["summary", "Összefoglaló"], ["decisions", "Döntések"], ["questions", "Nyitott kérdések"], ["actions", "Teendők"],
  ];
  const ACTIVE = new Set(["queued", "running", "cancelling"]);
  let timer = null;
  let openJobId = null;
  let maxBytes = 0;

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
      throw new Error(detail || `Hiba (${response.status})`);
    }
    return data;
  }

  function clock(seconds) {
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
  }

  // --- upload ---

  async function loadFormats() {
    try {
      const formats = await api("/meetings/formats");
      maxBytes = formats.max_mib * 1024 * 1024;
      $("mt-file").setAttribute("accept", formats.extensions.join(","));
      const kinds = formats.video
        ? "Hang- és videófájl is feltölthető (a videóból csak a hangot használjuk)"
        : "Ezen a telepítésen nincs ffmpeg, ezért csak WAV hangfájl tölthető fel, videó nem";
      $("mt-formats").textContent =
        `${kinds}: ${formats.extensions.join(", ")}. Legfeljebb ${formats.max_mib} MB és 4 óra. ` +
        "A beszélők nincsenek azonosítva.";
    } catch (error) {
      $("mt-formats").textContent = error.message;
    }
  }

  function upload() {
    const file = $("mt-file").files[0];
    if (!file) {
      setStatus("Válassz ki egy felvételt.", true);
      return;
    }
    if (maxBytes && file.size > maxBytes) {
      setStatus("A felvétel túl nagy.", true);
      return;
    }
    const bar = $("mt-upload-progress");
    const button = $("mt-upload");
    // XMLHttpRequest rather than fetch: it reports upload progress, which matters for large video files.
    const request = new XMLHttpRequest();
    request.open("POST", `/meetings/jobs?filename=${encodeURIComponent(file.name)}`);
    request.setRequestHeader("X-CSRF-Token", csrfToken);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) bar.value = Math.round((event.loaded / event.total) * 100);
    });
    request.addEventListener("loadend", () => {
      button.disabled = false;
      bar.hidden = true;
      let data = {};
      try { data = JSON.parse(request.responseText); } catch { /* not JSON */ }
      if (request.status === 401) {
        window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
      } else if (request.status === 202) {
        setStatus("Feltöltve. A feldolgozás a háttérben fut, ez az oldal elhagyható.");
        $("mt-file").value = "";
        refreshJobs();
      } else {
        setStatus(typeof data.detail === "string" ? data.detail : "A feltöltés nem sikerült.", true);
      }
    });
    button.disabled = true;
    bar.value = 0;
    bar.hidden = false;
    setStatus("Feltöltés…");
    request.send(file);
  }

  // --- jobs ---

  function progressText(job) {
    const { phase, done, total } = job.progress;
    if (job.status === "queued") return "Sorban áll.";
    if (phase === "transcribing") {
      return total ? `Leirat készül: ${clock(done)} / ${clock(total)}` : `Leirat készül: ${clock(done)}`;
    }
    if (phase === "summarizing") return total ? `Jegyzetek készülnek: ${done} / ${total} lépés` : "Jegyzetek készülnek…";
    return "";
  }

  function renderJob(job) {
    const created = new Date(job.created_at).toLocaleString("hu-HU");
    const item = el("li", { class: "item" }, [
      el("div", { class: "item-title" }, job.filename),
      el("div", { class: "item-meta" }, `${created} · ${STATUS_NAMES[job.status] || job.status}`),
    ]);
    if (job.status === "running") {
      const { done, total } = job.progress;
      const bar = el("progress", total ? { max: String(total), value: String(done) } : {});
      item.append(bar, el("div", { class: "item-meta" }, progressText(job)));
    } else if (job.status === "queued") {
      item.append(el("div", { class: "item-meta" }, progressText(job)));
    }
    if (job.error) item.append(el("div", { class: "item-error" }, job.error));
    const actions = el("div", { class: "item-actions" });
    if (!ACTIVE.has(job.status)) {
      actions.append(el("button", { type: "button", class: "secondary", onclick: () => openJob(job.id) }, "Megnyitás"));
      actions.append(el("button", { type: "button", class: "danger", onclick: () => deleteJob(job) }, "Törlés"));
    } else if (job.status !== "cancelling") {
      actions.append(el("button", { type: "button", class: "danger", onclick: () => cancelJob(job.id) }, "Leállítás"));
    }
    item.append(actions);
    return item;
  }

  async function refreshJobs() {
    clearTimeout(timer);
    let jobs = [];
    try {
      jobs = await api("/meetings/jobs");
    } catch (error) {
      setStatus(error.message, true);
      return;
    }
    jobsEl.replaceChildren(...(jobs.length ? jobs.map(renderJob) : [el("li", { class: "empty" }, "Még nincs felvétel.")]));
    if (jobs.some((job) => ACTIVE.has(job.status))) timer = setTimeout(refreshJobs, POLL_MS);
    const open = jobs.find((job) => job.id === openJobId);
    if (openJobId && !open) closeResult();
  }

  async function cancelJob(jobId) {
    try {
      await api(`/meetings/jobs/${jobId}/cancel`, { method: "POST" });
      setStatus("Leállítva.");
    } catch (error) {
      setStatus(error.message, true);
    }
    refreshJobs();
  }

  async function deleteJob(job) {
    if (!window.confirm(`Törlöd a(z) „${job.filename}” leiratát és jegyzeteit?`)) return;
    try {
      await api(`/meetings/jobs/${job.id}`, { method: "DELETE" });
      setStatus("Törölve.");
    } catch (error) {
      setStatus(error.message, true);
    }
    refreshJobs();
  }

  // --- result ---

  function closeResult() {
    openJobId = null;
    resultEl.hidden = true;
    resultEl.replaceChildren();
  }

  function jumpTo(segmentId) {
    const search = $("mt-search");
    if (search && search.value) {
      search.value = "";
      filterTranscript("");
    }
    const row = resultEl.querySelector(`[data-segment="${segmentId}"]`);
    if (!row) return;
    resultEl.querySelectorAll(".transcript li.target").forEach((node) => node.classList.remove("target"));
    row.classList.add("target");
    row.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function sourceChips(ids, times) {
    return ids.map((id) => el("button", {
      type: "button", class: "ts", title: "Ugrás a leirat pontjára", onclick: () => jumpTo(id),
    }, times.get(id) || "?"));
  }

  function assignForm(jobId, action) {
    const input = el("input", {
      type: "text", maxlength: "100", placeholder: "Felelős neve (nem kötelező)", "aria-label": "Felelős neve",
    });
    input.value = action.assignee || "";
    const save = el("button", { type: "button", class: "secondary" }, "Mentés");
    save.addEventListener("click", async () => {
      try {
        const answer = await api(`/meetings/jobs/${jobId}/actions/${action.id}`, {
          method: "PATCH", body: JSON.stringify({ name: input.value }),
        });
        input.value = answer.assignee || "";
        setStatus(answer.assignee ? `Felelős mentve: ${answer.assignee}` : "Felelős törölve.");
      } catch (error) {
        setStatus(error.message, true);
      }
    });
    return el("div", { class: "assign" }, [input, save]);
  }

  function renderNotes(job, times) {
    const box = el("div");
    if (!job.notes) {
      box.append(el("p", { class: "empty" }, "Jegyzetek nem készültek; a leirat elérhető."));
      return box;
    }
    (job.notes.gaps || []).forEach((gap) => {
      box.append(el("p", { class: "item-error" },
        `Ehhez a részhez nem készült jegyzet: ${clock(gap.start)} – ${clock(gap.end)}. A leiratban megtalálod.`));
    });
    SECTIONS.forEach(([key, label]) => {
      box.append(el("h4", {}, label));
      const items = job.notes[key];
      if (!items.length) {
        box.append(el("p", { class: "empty" }, "Nincs ilyen bejegyzés."));
        return;
      }
      box.append(el("ul", { class: "notes" }, items.map((item) => {
        const li = el("li", {}, [el("span", {}, `${item.text} `), ...sourceChips(item.segments, times)]);
        if (key === "actions") li.append(assignForm(job.id, item));
        return li;
      })));
    });
    return box;
  }

  function highlight(text, query) {
    if (!query) return [text];
    const parts = [];
    const lower = text.toLocaleLowerCase("hu");
    let from = 0;
    let at = lower.indexOf(query, from);
    while (at !== -1) {
      parts.push(text.slice(from, at), el("mark", {}, text.slice(at, at + query.length)));
      from = at + query.length;
      at = lower.indexOf(query, from);
    }
    parts.push(text.slice(from));
    return parts;
  }

  function filterTranscript(raw) {
    const query = raw.trim().toLocaleLowerCase("hu");
    let hits = 0;
    resultEl.querySelectorAll(".transcript li").forEach((row) => {
      const text = row.dataset.text;
      const match = !query || text.toLocaleLowerCase("hu").includes(query);
      row.hidden = !match;
      if (match && query) hits += 1;
      row.querySelector(".text").replaceChildren(...highlight(text, query));
    });
    $("mt-search-count").textContent = query ? `${hits} találat` : "";
  }

  function renderTranscript(segments) {
    const box = el("div");
    const search = el("input", { id: "mt-search", type: "search", placeholder: "Keresés a leiratban" });
    search.addEventListener("input", () => filterTranscript(search.value));
    box.append(
      el("label", { for: "mt-search" }, "Keresés"), search,
      el("p", { id: "mt-search-count", class: "hint", role: "status" }),
    );
    const list = el("ol", { class: "transcript" }, segments.map((segment) => {
      const row = el("li", { "data-segment": String(segment.id) }, [
        el("span", { class: "time" }, clock(segment.start)),
        el("span", {}, [el("span", { class: "speaker" }, "Ismeretlen beszélő · "), el("span", { class: "text" }, segment.text)]),
      ]);
      row.dataset.text = segment.text;
      return row;
    }));
    if (!segments.length) box.append(el("p", { class: "empty" }, "A felvételen nem hallható beszéd."));
    else box.append(list);
    return box;
  }

  async function openJob(jobId) {
    let job;
    try {
      job = await api(`/meetings/jobs/${jobId}`);
    } catch (error) {
      setStatus(error.message, true);
      return;
    }
    openJobId = jobId;
    const segments = job.segments || [];
    const times = new Map(segments.map((segment) => [segment.id, clock(segment.start)]));
    const exports = el("div", { class: "exports" });
    if (job.notes) exports.append(el("a", { href: `/meetings/jobs/${jobId}/export?format=md` }, "Jegyzetek (Markdown)"));
    if (job.segments) {
      exports.append(
        el("a", { href: `/meetings/jobs/${jobId}/export?format=txt` }, "Leirat (TXT)"),
        el("a", { href: `/meetings/jobs/${jobId}/export?format=srt` }, "Felirat (SRT)"),
      );
    }
    const children = [el("h3", {}, job.filename)];
    if (job.error) children.push(el("p", { class: "item-error" }, job.error));
    children.push(exports, renderNotes(job, times));
    children.push(
      el("p", { class: "hint" }, "Felelőst csak te rendelhetsz egy teendőhöz; a rendszer nem találgat."),
      el("h4", {}, "Leirat"),
    );
    children.push(job.segments ? renderTranscript(segments) : el("p", { class: "empty" }, "Leirat nem készült."));
    resultEl.replaceChildren(...children);
    resultEl.hidden = false;
    resultEl.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  $("mt-upload").addEventListener("click", upload);
  loadFormats();
  refreshJobs();
})();
