// Document Intelligence panel: start whole-document jobs, watch their progress, read and export the results.
// Everything from the server is model output or file names: it is only ever set as text, never as HTML.
(() => {
  const root = document.getElementById("docintel");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const statusEl = $("di-status");
  const jobsEl = $("di-jobs");
  const resultEl = $("di-result");
  const POLL_MS = 3000;
  const KIND_NAMES = {
    summary: "Összefoglaló", extract: "Kigyűjtés", compare: "Összehasonlítás", tasks: "Teendők", translate: "Fordítás",
  };
  const STATUS_NAMES = {
    queued: "várakozik", running: "fut", succeeded: "kész", failed: "sikertelen", cancelled: "leállítva",
    interrupted: "megszakadt",
  };
  let timer = null;
  let openJobId = null;

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

  // --- the form ---

  function syncForm() {
    const kind = $("di-kind").value;
    root.querySelectorAll("[data-for]").forEach((node) => {
      node.hidden = node.dataset.for !== kind;
    });
    $("di-doc-b-row").hidden = kind !== "compare";
    $("di-doc-a-label").textContent = kind === "compare" ? "Első dokumentum (A, a hivatkozási alap)" : "Dokumentum";
  }

  async function loadDocuments() {
    const sources = await api("/sources");
    ["di-doc-a", "di-doc-b"].forEach((id) => {
      const select = $(id);
      select.textContent = "";
      sources.forEach((source) => {
        select.append(el("option", { value: source.source_path }, source.title || source.source_path.split("/").pop()));
      });
    });
    if (sources.length > 1) $("di-doc-b").selectedIndex = 1;
  }

  async function startJob() {
    const kind = $("di-kind").value;
    const body = { kind, source_paths: [$("di-doc-a").value] };
    if (!body.source_paths[0]) return setStatus("Válassz dokumentumot.", true);
    if (kind === "summary") body.granularity = $("di-granularity").value;
    if (kind === "extract") {
      body.types = [...root.querySelectorAll('input[name="di-type"]:checked')].map((box) => box.value);
      if (!body.types.length) return setStatus("Válassz legalább egy típust.", true);
    }
    if (kind === "compare") {
      body.source_paths.push($("di-doc-b").value);
      body.mode = $("di-mode").value;
    }
    if (kind === "translate") body.direction = $("di-direction").value;
    $("di-start").disabled = true;
    try {
      await api("/docintel/jobs", { method: "POST", body: JSON.stringify(body) });
      setStatus("A feladat sorba került, a háttérben fut.");
      await refresh();
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      $("di-start").disabled = false;
    }
  }

  // --- the job list ---

  function sourceNames(job) {
    return job.sources.map((s) => s.title || s.file_name).join(" ↔ ");
  }

  function jobItem(job) {
    const { done, total, phase } = job.progress;
    const active = job.status === "queued" || job.status === "running";
    const item = el("li", { class: "item" }, [
      el("div", { class: "item-title" }, job.title),
      el("div", { class: "item-meta" }, [
        `${KIND_NAMES[job.kind] || job.kind} · ${sourceNames(job)} · `,
        el("span", { class: `badge ${job.status === "succeeded" ? "ok" : job.status === "failed" ? "bad" : ""}` },
          STATUS_NAMES[job.status] || job.status),
        job.stale ? el("span", { class: "badge warning", title: job.stale_reason || "" }, "elavult") : "",
      ]),
    ]);
    if (job.status === "running" || (active && total)) {
      item.append(el("progress", { max: String(Math.max(total, 1)), value: String(done) }));
      item.append(el("div", { class: "item-meta" },
        `${done} / ${total} lépés${phase ? ` · ${phase}` : ""}${job.cancel_requested ? " · leállítás folyamatban" : ""}`));
    } else if (job.status === "queued") {
      item.append(el("div", { class: "item-meta" }, "Sorban áll; egyszerre egy feladat fut, a kérdezés elsőbbséget élvez."));
    }
    if (job.stale) item.append(el("div", { class: "item-error" }, job.stale_reason));
    if (job.error) item.append(el("div", { class: "item-error" }, job.error));
    const actions = el("div", { class: "item-actions" });
    if (job.status === "succeeded") actions.append(el("button", { type: "button", class: "primary", onclick: () => openResult(job.id) }, "Megnyitás"));
    if (active && !job.cancel_requested) actions.append(el("button", { type: "button", class: "secondary", onclick: () => act(job, "cancel") }, "Leállítás"));
    if (job.resumable) actions.append(el("button", { type: "button", class: "primary", onclick: () => act(job, "resume") }, "Folytatás"));
    if (job.status !== "running") actions.append(el("button", { type: "button", class: "danger", onclick: () => remove(job) }, "Törlés"));
    item.append(actions);
    return item;
  }

  async function act(job, action) {
    try {
      await api(`/docintel/jobs/${job.id}/${action}`, { method: "POST", body: "{}" });
      await refresh();
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function remove(job) {
    if (!window.confirm("Biztosan törlöd ezt a feladatot és az eredményét?")) return;
    try {
      await api(`/docintel/jobs/${job.id}`, { method: "DELETE" });
      if (openJobId === job.id) closeResult();
      await refresh();
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function refresh() {
    let jobs;
    try {
      jobs = await api("/docintel/jobs");
    } catch (error) {
      return setStatus(error.message, true);
    }
    jobsEl.textContent = "";
    if (!jobs.length) jobsEl.append(el("li", { class: "empty" }, "Még nincs feladat."));
    jobs.forEach((job) => jobsEl.append(jobItem(job)));
    const anyActive = jobs.some((job) => job.status === "queued" || job.status === "running");
    if (anyActive && !timer) timer = window.setInterval(refresh, POLL_MS);
    if (!anyActive && timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  // --- results ---

  function closeResult() {
    openJobId = null;
    resultEl.hidden = true;
    resultEl.textContent = "";
  }

  // The places a statement stands on, as chips; a doubtful scan or match is marked.
  function sources(ids, citations) {
    const byId = new Map(citations.map((c) => [c.id, c]));
    const box = el("span", { class: "srcs" });
    (ids || []).forEach((id) => {
      const c = byId.get(id);
      if (!c) return;
      box.append(el("span", { class: `src${c.uncertain ? " uncertain" : ""}`, title: c.uncertain ? "bizonytalan forrás" : "" }, c.label));
    });
    if (!ids || !ids.length) box.append(el("span", { class: "src uncertain" }, "forrás nélkül"));
    return box;
  }

  function table(headers, rows) {
    return el("div", { class: "table-scroll" }, el("table", {}, [
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.map((row) => el("tr", {}, row.map((cell) => el("td", {}, cell))))),
    ]));
  }

  function renderPoints(points, citations) {
    return el("ul", { class: "points" }, points.map((p) => el("li", {}, [p.text, " ", sources(p.sources, citations)])));
  }

  function renderSummary(result, parts) {
    parts.append(el("h4", {}, "Összefoglaló"), renderPoints(result.overall, result.citations));
    result.sections.forEach((section) => {
      parts.append(el("h4", {}, section.label), renderPoints(section.points, result.citations));
    });
  }

  function renderRows(result, parts) {
    parts.append(table(
      ["Típus", "Tartalom", "Részlet", "Forrás"],
      result.rows.map((row) => {
        const content = [row.text];
        if (row.table) {
          content.push(table(row.table.columns, row.table.rows));
        }
        return [row.type_label, content, row.detail || "", sources(row.sources, result.citations)];
      }),
    ));
  }

  function renderTasks(result, parts) {
    parts.append(el("h4", {}, "Forrásadatok (a dokumentumból)"));
    renderRows(result, parts);
    const box = el("div", { class: "conclusions" }, [
      el("h4", {}, "Következtetések (a modell saját javaslatai, nem a dokumentum állításai)"),
    ]);
    if (!result.conclusions.length) box.append(el("p", {}, "Nincs javaslat."));
    result.conclusions.forEach((c) => {
      box.append(el("p", {}, [
        c.text,
        c.grounded_in.length ? el("span", { class: "hint" }, ` — a forrásadat ${c.grounded_in.join(", ")}. sora alapján`) : " ",
        sources(c.sources, result.citations),
      ]));
    });
    parts.append(box);
  }

  function renderCompare(result, parts) {
    const kinds = { changed: "Eltérés", missing_in_b: "Hiányzik a B-ből", only_in_b: "Csak a B-ben van" };
    if (!result.findings.length) parts.append(el("p", {}, "Nem találtam eltérést."));
    result.findings.forEach((f) => {
      const box = el("div", { class: "finding" }, [
        el("strong", {}, kinds[f.kind] || f.kind),
        el("span", { class: `badge ${f.status === "evidenced" ? "ok" : "warning"}`, title: f.reason }, f.status === "evidenced" ? "bizonyított" : "bizonytalan"),
        el("div", {}, f.description),
      ]);
      if (f.a) box.append(el("div", { class: "side" }, ["A: ", f.a.text, " ", sources(f.a.sources, result.citations)]));
      if (f.b) box.append(el("div", { class: "side" }, ["B: ", f.b.text, " ", sources(f.b.sources, result.citations)]));
      if (f.closest) box.append(el("div", { class: "side hint" }, ["Legközelebbi rész a másik dokumentumban: ", sources(f.closest.sources, result.citations)]));
      box.append(el("div", { class: "hint" }, f.reason));
      parts.append(box);
    });
  }

  function renderTranslation(result, parts) {
    const box = el("div", { class: "translation" });
    result.segments.forEach((segment) => {
      box.append(el("div", { class: "place" }, segment.label), el("p", {}, segment.text));
    });
    parts.append(box);
  }

  async function openResult(jobId) {
    let body;
    try {
      body = await api(`/docintel/jobs/${jobId}/result`);
    } catch (error) {
      return setStatus(error.message, true);
    }
    openJobId = jobId;
    const { job, result } = body;
    resultEl.textContent = "";
    resultEl.hidden = false;
    resultEl.append(el("h3", {}, job.title));
    if (job.stale) resultEl.append(el("p", { class: "notice warning" }, `${job.stale_reason} Az eredmény már nem ellenőrizhető az eredeti forrással.`));
    if (result.notice) resultEl.append(el("p", { class: `notice${result.grounding.low_confidence || result.coverage.failed ? " warning" : ""}` }, result.notice));
    const exports = el("div", { class: "exports" });
    const formats = result.kind === "translate" ? [["txt", "Szöveg (.txt)"], ["md", "Markdown"]] : [["csv", "CSV"], ["md", "Markdown"]];
    formats.forEach(([format, label]) => {
      exports.append(el("a", { href: `/docintel/jobs/${jobId}/export?format=${format}`, download: "" }, `Letöltés: ${label}`));
    });
    resultEl.append(exports);
    const renderers = { summary: renderSummary, extract: renderRows, tasks: renderTasks, compare: renderCompare, translate: renderTranslation };
    renderers[result.kind](result, resultEl);
    resultEl.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  $("di-kind").addEventListener("change", syncForm);
  $("di-start").addEventListener("click", startJob);
  syncForm();
  loadDocuments().catch((error) => setStatus(error.message, true));
  refresh();
  // A link like /#di-job=7 opens that result straight away.
  const linked = /^#di-job=(\d+)$/.exec(window.location.hash);
  if (linked) openResult(Number(linked[1]));
})();
