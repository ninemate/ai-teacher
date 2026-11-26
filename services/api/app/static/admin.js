// Admin panel and "own sessions" section. Every value is inserted as text, never as HTML.
(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const headers = {"X-CSRF-Token": csrf};

  async function request(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = "";
      try { detail = (await response.json()).detail || ""; } catch { /* not JSON */ }
      throw new Error(detail || `Hiba (${response.status})`);
    }
    return response.json();
  }

  const when = (value) => {
    if (value === null || value === undefined || value === "") return "–";
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return isNaN(date) ? String(value) : date.toLocaleString("hu-HU");
  };

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function actionButton(label, confirmText, run, onError) {
    const button = el("button", label, "admin-action");
    button.type = "button";
    button.addEventListener("click", async () => {
      if (confirmText && !confirm(confirmText)) return;
      button.disabled = true;
      try { await run(); } catch (e) { onError(e.message); button.disabled = false; }
    });
    return button;
  }

  // columns: [{label, value: row => text | Node}]
  function table(rows, columns, empty) {
    if (!rows.length) return el("p", empty || "Nincs adat.", "admin-empty");
    const wrap = el("div", null, "admin-table-wrap");
    const t = el("table", null, "admin-table");
    const head = t.createTHead().insertRow();
    for (const c of columns) head.append(el("th", c.label));
    const body = t.createTBody();
    for (const row of rows) {
      const tr = body.insertRow();
      for (const c of columns) {
        const td = tr.insertCell();
        td.dataset.label = c.label;
        const v = c.value(row);
        if (v instanceof Node) td.append(v); else td.textContent = v ?? "–";
      }
    }
    wrap.append(t);
    return wrap;
  }

  // ---- Own sessions (every signed-in user) ----
  const account = document.getElementById("account-content");
  const accountStatus = document.getElementById("account-status");
  const EVENT_NAMES = {
    "login.success": "Bejelentkezés", "login.failure": "Sikertelen bejelentkezés", "logout": "Kijelentkezés",
    "session.expired": "Lejárt munkamenet", "session.revoked": "Visszavont munkamenet",
    "admin.session_revoke": "Rendszergazda által visszavonva",
  };
  async function loadAccount() {
    try {
      const data = await request("/account/sessions");
      account.replaceChildren(
        el("h3", "Aktív munkamenetek"),
        table(data.active, [
          {label: "Létrehozva", value: s => when(s.created_at)},
          {label: "Lejár", value: s => when(s.expires_at)},
          {label: "", value: s => s.current ? el("strong", "ez az eszköz") : ""},
        ]),
        el("h3", "Legutóbbi tevékenység"),
        table(data.recent.slice(0, 20), [
          {label: "Időpont", value: r => when(r.timestamp)},
          {label: "Esemény", value: r => EVENT_NAMES[r.event] || r.event},
          {label: "IP-cím", value: r => r.source_ip},
        ]),
      );
      accountStatus.textContent = "";
    } catch (e) { accountStatus.textContent = e.message; }
  }
  document.getElementById("account-load")?.addEventListener("click", loadAccount);
  document.getElementById("account-revoke")?.addEventListener("click", async () => {
    if (!confirm("Kijelentkezel minden más eszközön?")) return;
    try {
      const result = await request("/account/sessions/revoke-others", {method: "POST", headers});
      await loadAccount();
      accountStatus.textContent = `${result.revoked} munkamenet visszavonva.`;
    } catch (e) { accountStatus.textContent = e.message; }
  });

  // ---- Admin panel (rendered by the server only for admins; the API checks the role again) ----
  const content = document.getElementById("admin-content");
  if (!content) return;
  const status = document.getElementById("admin-status");
  const filter = document.getElementById("audit-filter");
  const tabs = document.querySelectorAll("[data-admin-view]");
  const fail = (message) => { status.textContent = message; };
  let view = "users";

  const VIEWS = {
    users: (data) => [
      el("p", "A fiókok és szerepkörök a kiszolgáló beállításaiból jönnek (TEACHER_AUTH_USERS, TEACHER_AUTH_USERS_FILE, TEACHER_AUTH_ADMINS); módosítás után az API-t újra kell indítani.", "admin-note"),
      table(data, [
        {label: "Felhasználó", value: u => u.username},
        {label: "Szerepkör", value: u => u.role === "admin" ? "rendszergazda" : "felhasználó"},
      ]),
    ],
    sessions: (data) => [table(data, [
      {label: "Felhasználó", value: s => s.username},
      {label: "Létrehozva", value: s => when(s.created_at)},
      {label: "Lejár", value: s => when(s.expires_at)},
      {label: "", value: s => s.current ? el("strong", "saját munkamenet") : actionButton(
        "Visszavonás", `Visszavonod ${s.username} munkamenetét?`,
        async () => { await request(`/admin/sessions/${encodeURIComponent(s.id)}`, {method: "DELETE", headers}); await load(); },
        fail)},
    ], "Nincs aktív munkamenet.")],
    workspaces: (data) => [table(data, [
      {label: "Tudástár", value: w => w.name},
      {label: "Tulajdonos", value: w => w.owner || "–"},
      {label: "Megosztva", value: w => w.shares.length
        ? w.shares.map(s => `${s.username} (${s.permission === "write" ? "írás" : "olvasás"})`).join(", ") : "–"},
    ])],
    ingest: (data) => [table(data, [
      {label: "Futás", value: r => `#${r.id}`},
      {label: "Állapot", value: r => r.status},
      {label: "Kezdés", value: r => when(r.started_at)},
      {label: "Vége", value: r => when(r.finished_at)},
      {label: "Átnézve", value: r => r.scanned},
      {label: "Indexelve", value: r => r.indexed},
      {label: "Hiba", value: r => r.failed},
    ], "Még nem futott indexelés.")],
    exclusions: (data) => [table(data, [
      {label: "Tudástár", value: x => x.workspace_id},
      {label: "Forrás", value: x => x.source_path},
      {label: "", value: x => actionButton("Visszaállítás", "A dokumentum a következő indexeléskor újra bekerül. Folytatod?",
        async () => { await request(`/admin/exclusions/${x.id}/restore`, {method: "POST", headers}); await load(); }, fail)},
    ], "Nincs kizárt dokumentum.")],
    inventory: (data) => [
      table(Object.entries(data.configured_models).map(([role, name]) => ({role, name})), [
        {label: "Modell szerepe", value: m => m.role}, {label: "Beállított modell", value: m => m.name},
      ]),
      table(data.api_packages, [{label: "Csomag", value: p => p.name}, {label: "Verzió", value: p => p.version}]),
      el("p", "A teljes komponens- és modelljegyzékhez futtasd a gépen: make inventory.", "admin-note"),
    ],
    audit: (data) => [table(data, [
      {label: "Időpont", value: r => when(r.timestamp)},
      {label: "Esemény", value: r => r.event},
      {label: "Ki", value: r => r.actor},
      {label: "Cél", value: r => r.target},
      {label: "IP-cím", value: r => r.source_ip},
    ], "Nincs a szűrésnek megfelelő esemény.")],
  };

  function auditQuery() {
    const params = new URLSearchParams();
    for (const [key, value] of new FormData(filter)) {
      if (!value) continue;
      if (key === "since" || key === "until") {
        const date = new Date(value + (key === "until" ? "T23:59:59" : "T00:00:00"));
        params.set(key, String(Math.floor(date.getTime() / 1000)));
      } else params.set(key, value);
    }
    return params;
  }

  async function load() {
    filter.hidden = view !== "audit";
    tabs.forEach(t => t.setAttribute("aria-pressed", String(t.dataset.adminView === view)));
    let url = `/admin/${view}`;
    if (view === "audit") {
      const params = auditQuery();
      url += `?${params}`;
      params.set("format", "csv");
      params.set("limit", "10000");
      document.getElementById("audit-csv").href = `/admin/audit?${params}`;
    }
    status.textContent = "Betöltés…";
    try {
      const data = await request(url);
      content.replaceChildren(...VIEWS[view](data));
      status.textContent = "";
    } catch (e) { content.replaceChildren(); status.textContent = e.message; }
  }

  tabs.forEach(button => button.addEventListener("click", () => { view = button.dataset.adminView; load(); }));
  filter.addEventListener("submit", event => { event.preventDefault(); load(); });
  load();
})();
