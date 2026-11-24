// Application shell: hash routing between the home screen and the modes, and the cross-mode activity bar.
//
// Routes:  #/ (home)   #/<mode>   #/<mode>/<section id> (deep link, scrolls to the section)
// Every mode stays in the DOM and is only hidden, so a running job, an audio element or a half-typed form in a
// mode keeps going while another mode is open. Sections may rely on that.
//
// Public API for section scripts (window.Teacher):
//   Teacher.activity.set(key, { label, href } | null)  show/clear a long-running job in the bar on every mode;
//                                                       href is a hash route that resumes it, e.g. "#/docs/docintel"
//   Teacher.go(route)                                  navigate, e.g. Teacher.go("#/library/listen")
//   Teacher.onShow(modeId, fn)                         call fn when that mode becomes visible
(() => {
  const strings = JSON.parse(document.getElementById("shell-strings").textContent);
  const appTitle = document.body.dataset.appTitle || document.title;
  const isAdmin = document.body.dataset.role === "admin";
  const views = new Map([...document.querySelectorAll("[data-view]")].map((node) => [node.dataset.view, node]));
  const showHooks = new Map();
  const activityEl = document.getElementById("activity");
  const activities = new Map();
  let current = null;

  function parse(hash) {
    const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
    const mode = parts[0] || "home";
    return views.has(mode) ? { mode, section: parts[1] || "" } : { mode: "home", section: "", invalid: Boolean(parts[0]) };
  }

  function route() {
    const { mode, section, invalid } = parse(window.location.hash);
    if (invalid) {
      // Unknown or not permitted (e.g. admin for a normal user): go home without leaving a dead history entry.
      history.replaceState(null, "", "#/");
    }
    const changed = mode !== current;
    views.forEach((node, id) => { node.hidden = id !== mode; });
    document.body.dataset.view = mode;
    const view = views.get(mode);
    const title = mode === "home" ? "" : view.dataset.title;
    document.getElementById("appbar-title").textContent = title;
    document.title = title ? `${title} – ${appTitle}` : appTitle;
    document.querySelectorAll("[data-mode-link]").forEach((link) => {
      if (link.closest("#modebar")) {
        const active = link.dataset.modeLink === mode;
        link.classList.toggle("active", active);
        if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
        if (active && changed) link.scrollIntoView({ inline: "center", block: "nearest" });
      }
    });
    const target = section ? document.getElementById(section) : null;
    if (target && view.contains(target)) {
      target.scrollIntoView({ block: "start" });
    } else if (changed) {
      window.scrollTo(0, 0);
      const heading = view.querySelector(".mode-title") || view.querySelector("h1");
      if (heading) heading.focus({ preventScroll: true });
    }
    current = mode;
    if (changed && showHooks.has(mode)) showHooks.get(mode).forEach((fn) => fn());
  }

  function renderActivity() {
    activityEl.textContent = "";
    activities.forEach((entry, key) => {
      const li = document.createElement("li");
      li.dataset.key = key;
      const link = document.createElement("a");
      link.href = entry.href || "#/";
      const label = document.createElement("span");
      label.className = "activity-label";
      label.textContent = entry.label;
      const resume = document.createElement("span");
      resume.className = "activity-resume";
      resume.textContent = strings["activity.resume"];
      link.append(label, resume);
      li.append(link);
      activityEl.append(li);
    });
    activityEl.hidden = activities.size === 0;
  }

  window.Teacher = {
    activity: {
      set(key, entry) {
        const before = activities.get(key);
        if (!entry) activities.delete(key);
        else activities.set(key, entry);
        const after = activities.get(key);
        if (!before !== !after || (before && (before.label !== after.label || before.href !== after.href))) renderActivity();
      },
    },
    go(target) { window.location.hash = target; },
    onShow(modeId, fn) {
      if (!showHooks.has(modeId)) showHooks.set(modeId, []);
      showHooks.get(modeId).push(fn);
      if (current === modeId) fn();
    },
  };

  // Indexing runs in another service for minutes to hours; only the admin may read its state.
  if (isAdmin) {
    const poll = async () => {
      try {
        const response = await fetch("/ingest/status");
        const data = response.ok ? await response.json() : null;
        const running = data && data.status === "running";
        window.Teacher.activity.set("indexing", running
          ? { label: `${strings["activity.indexing"]}: ${data.indexed || 0}/${data.scanned || 0}`, href: "#/library" }
          : null);
      } catch { /* the next poll tries again */ }
    };
    poll();
    window.setInterval(poll, 15000);
  }

  window.addEventListener("hashchange", route);
  route();
})();
