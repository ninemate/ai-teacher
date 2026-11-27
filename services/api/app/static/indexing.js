// The registry loads this section only for admins. Counters become available after the run.
(() => {
  const strings = JSON.parse(document.getElementById("shell-strings").textContent);
  const status = document.getElementById("index-status");
  const details = document.getElementById("index-details");

  async function poll() {
    try {
      const response = await fetch("/ingest/status");
      if (!response.ok) throw new Error();
      const run = await response.json();
      const running = run.status === "running";
      const label = running ? strings["activity.indexing"] : strings[`index.${run.status}`];
      status.textContent = label || strings["index.unavailable"];
      const fields = [];
      if (run.started_at) {
        const started = new Date(run.started_at).toLocaleString(document.documentElement.lang);
        fields.push(`${strings["index.started"]}: ${started}`);
      }
      if (!running && run.scanned != null) {
        fields.push(`${strings["index.total"]}: ${run.scanned}`, `${strings["index.indexed"]}: ${run.indexed}`);
      }
      details.textContent = fields.join(" · ");
      window.Teacher.activity.set("indexing", running ? {label, href: "#/admin/indexing"} : null);
    } catch {
      status.textContent = strings["index.unavailable"];
      details.textContent = "";
    } finally {
      window.setTimeout(poll, 15000);
    }
  }

  poll();
})();
