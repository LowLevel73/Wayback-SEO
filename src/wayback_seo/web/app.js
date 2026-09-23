// Wayback SEO web interface. Every request uses a relative URL, so the page
// works at the server's own address and under a reverse-proxy subpath.

const TOOL_NAMES = { down: "Down detector", migration: "Migration check", robots: "robots.txt history" };
const CATEGORY_TONE = {
  "not redirected": "bad", "redirect to error": "bad", "error": "bad", "failed": "bad",
  "to homepage": "warn", "temporary redirect": "warn", "redirect chain": "warn",
  "redirected": "good", "still works": "good",
};
const CATEGORY_MEANING = {
  "not redirected": "now returns 404/410: it was not redirected",
  "redirect to error": "redirects, but the final page returns an error",
  "error": "now returns another error status",
  "failed": "no usable answer: timeout, connection error or redirect loop",
  "to homepage": "redirects to a homepage instead of an equivalent page",
  "temporary redirect": "reaches a working page, but through a 302/303/307",
  "redirect chain": "reaches a working page through 2 or more redirects",
  "redirected": "redirects permanently to a working page, as expected",
  "still works": "same URL still answers 200",
};

const COLLAPSE_OVER = 10;  // robots.txt versions with more rule changes start collapsed
const ROBOTS_FILTER = "robots.txt";  // the box of blocked URLs, which filters like a category

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const sameOrigin = (a, b) => { try { return new URL(a).origin === new URL(b).origin; } catch { return false; } };
const pathOf = (url) => { const u = new URL(url); return u.pathname + u.search; };
const isoDay = (d) => d.toISOString().slice(0, 10);

let currentTool = "down";
let currentAnalysis = null;
let runningJob = null;  // the analysis the Stop button would stop
let shownTool = null;  // the tool whose progress and result are on the page
let charts = [];

// ---------- tabs and forms ----------

function showTool(tool) {
  currentTool = tool;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
  $$(".tool-form").forEach((f) => (f.hidden = f.dataset.tool !== tool));
  // progress and result belong to one tool: hide them on the other tabs
  $("#result").hidden = shownTool !== tool;
  $("#progress").hidden = shownTool !== tool || !$("#progress-log").textContent;
}

function formParams(form) {
  const params = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    params[el.name] = el.type === "checkbox" ? el.checked : el.value.trim();
  }
  return params;
}

function fillForm(tool, params) {
  const form = $(`form[data-tool="${tool}"]`);
  for (const el of form.elements) {
    if (!el.name || !(el.name in params)) continue;
    if (el.type === "checkbox") el.checked = !!params[el.name];
    else el.value = params[el.name];
  }
}

// Back to the empty form of a new analysis.
function resetForm(tool) {
  const form = $(`form[data-tool="${tool}"]`);
  form.reset();
  if (tool === "down") {
    const today = new Date();
    const yearAgo = new Date(today);
    yearAgo.setDate(today.getDate() - 365);
    form.date_to.value = isoDay(today);
    form.date_from.value = isoDay(yearAgo);
  }
}

// ---------- running an analysis ----------

async function run(form) {
  const params = formParams(form);
  $$(".run").forEach((b) => (b.disabled = true));
  if (shownTool && shownTool !== form.dataset.tool) resetForm(shownTool);
  shownTool = form.dataset.tool;
  $("#progress").hidden = false;
  $("#progress-title").textContent = `Running: ${TOOL_NAMES[form.dataset.tool]}…`;
  $("#progress-log").textContent = "";
  $("#result").innerHTML = "";
  try {
    const response = await fetch("api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool: form.dataset.tool, params }),
    });
    const { job } = await response.json();
    runningJob = job;
    $("#stop").hidden = false;
    $("#stop").disabled = false;
    await follow(job);
  } catch (err) {
    showError(`Could not reach the server: ${err.message}`);
  } finally {
    $$(".run").forEach((b) => (b.disabled = false));
    runningJob = null;
    $("#stop").hidden = true;
  }
}

async function follow(jobId) {
  const log = $("#progress-log");
  let shown = 0;
  for (;;) {
    const job = await (await fetch(`api/jobs/${jobId}`)).json();
    // Only append new lines: rewriting the text would drop any selection.
    if (job.lines.length > shown) {
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 20;
      log.append(document.createTextNode((shown ? "\n" : "") + job.lines.slice(shown).join("\n")));
      shown = job.lines.length;
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
    if (job.status === "done") {
      $("#progress-title").textContent = "Finished";
      await loadHistory();
      await openAnalysis(job.analysis);
      await loadCacheSize();
      return;
    }
    if (job.status === "stopped") {
      $("#progress-title").textContent = "Stopped. Nothing was saved.";
      return;
    }
    if (job.status === "error") return showError(job.error);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

function showError(message) {
  $("#progress-title").innerHTML = `<span class="error">Stopped: ${esc(message)}</span>`;
}

// ---------- previous analyses ----------

function describe(item) {
  const p = item.params;
  const sites = p.sites.split(/[\s,]+/).filter(Boolean);
  let when = "";
  if (item.tool === "down") when = p.all_time ? "whole history" : `${p.date_from || "…"} → ${p.date_to || "today"}`;
  if (item.tool === "migration") when = `migration around ${p.date}`;
  if (item.tool === "robots") when = p.date_from || p.date_to ? `${p.date_from || "…"} → ${p.date_to || "today"}` : "whole history";
  return { sites: sites[0] + (sites.length > 1 ? ` +${sites.length - 1}` : ""), when };
}

async function loadCacheSize() {
  const { bytes } = await (await fetch("api/cache")).json();
  $("#cache-size").textContent = `${(bytes / 1e6).toFixed(1)} MB`;
}

async function clearCache() {
  if (!confirm("Delete all saved downloads? Saved analyses stay; new runs will download again.")) return;
  await fetch("api/cache", { method: "DELETE" });
  await loadCacheSize();
}

async function loadHistory() {
  const items = await (await fetch("api/analyses")).json();
  $("#history-empty").hidden = items.length > 0;
  $("#history-list").innerHTML = items.map((item) => {
    const d = describe(item);
    return `<li data-id="${esc(item.id)}" class="${item.id === currentAnalysis ? "active" : ""}">
      <div class="item-tool">${esc(TOOL_NAMES[item.tool])}</div>
      <div class="item-sites">${esc(d.sites)}</div>
      <div class="item-meta">${esc(d.when)}</div>
      <div class="item-meta">run ${esc(item.created.replace("T", " "))}</div>
      <button class="delete" title="Delete this analysis">×</button></li>`;
  }).join("");
}

async function openAnalysis(id) {
  const response = await fetch(`api/analyses/${id}`);
  if (!response.ok) return;
  const record = await response.json();
  currentAnalysis = id;
  history.replaceState(null, "", `#${id}`);  // the address opens this analysis again
  $$("#history-list li").forEach((li) => li.classList.toggle("active", li.dataset.id === id));
  // only the tool of the open analysis shows its values; the others start empty
  if (shownTool) resetForm(shownTool);
  resetForm(record.tool);
  shownTool = record.tool;
  $("#progress").hidden = true;
  $("#progress-log").textContent = "";
  showTool(record.tool);
  fillForm(record.tool, record.params);
  ({ down: renderDown, migration: renderMigration, robots: renderRobots })[record.tool](record);
}

// Empties every form and closes the open analysis. A run in progress keeps its log.
function newAnalysis() {
  Object.keys(TOOL_NAMES).forEach(resetForm);
  currentAnalysis = null;
  history.replaceState(null, "", location.pathname);
  $$("#history-list li").forEach((li) => li.classList.remove("active"));
  if ($(".run").disabled) return;
  shownTool = null;
  $("#result").innerHTML = "";
  $("#progress-log").textContent = "";
  showTool(currentTool);
}

async function deleteAnalysis(id) {
  if (!confirm("Delete this analysis?")) return;
  await fetch(`api/analyses/${id}`, { method: "DELETE" });
  if (id === currentAnalysis) {
    currentAnalysis = null;
    $("#result").innerHTML = "";
    resetForm(shownTool);
    shownTool = null;
    history.replaceState(null, "", location.pathname);
  }
  await loadHistory();
}

function header(record, title) {
  const d = describe(record);
  const sites = record.params.sites.split(/[\s,]+/).filter(Boolean).map(esc).join(", ");
  return `<h2>${esc(title)}</h2>
    <div class="result-meta">${sites} · ${esc(d.when)} · run ${esc(record.created.replace("T", " "))}
      · <a href="api/analyses/${esc(record.id)}.csv">Download CSV</a></div>`;
}

const stat = (value, label) => `<div class="stat"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;

// ---------- down detector ----------

function renderDown(record) {
  const data = record.result;
  const weeks = data.weeks;
  const downs = weeks.reduce((n, w) => n + w.down, 0);
  const recoveries = weeks.reduce((n, w) => n + w.recovery, 0);
  const captures = weeks.reduce((n, w) => n + w.captures, 0);
  const incomplete = data.missing.length
    ? `<p class="error">Incomplete: ${data.missing.length} pages failed to download. Run again to download only those.</p>` : "";
  $("#result").innerHTML = header(record, "Down detector") + incomplete + `
    <div class="stats">${stat(downs, "down events")}${stat(recoveries, "recovery events")}${stat(captures, "captures")}</div>
    <p class="chart-title">Down and recovery events per week</p>
    <p class="chart-note">Down: a URL went from 200 to an error. Recovery: from an error back to 200.</p>
    <div class="chart-box"><canvas id="events-chart"></canvas></div>
    <p class="chart-title">Captures per week</p>
    <p class="chart-note">An unreachable site is not archived, so an outage can show up as a dip. Lighter bars: weeks only partly inside the period.</p>
    <div class="chart-box small"><canvas id="captures-chart"></canvas></div>
    <p class="chart-title">Events (${data.events.length})</p>
    <div class="table-wrap"><table><thead><tr><th>When</th><th>Event</th><th>Status</th><th>URL</th></tr></thead><tbody>
      ${data.events.map((e) => `<tr><td>${esc(e.time.replace("T", " "))}</td>
        <td><span class="badge ${e.kind === "down" ? "bad" : "good"}">${esc(e.kind)}</span></td>
        <td>${esc(e.status)}</td><td class="url">${esc(e.url)}</td></tr>`).join("")}
    </tbody></table></div>`;

  const labels = weeks.map((w) => w.start);
  const title = (items) => {
    const w = weeks[items[0].dataIndex];
    return `Week of ${w.start}` + (w.days < 7 ? ` (${w.days} day${w.days > 1 ? "s" : ""} in the period)` : "");
  };
  const axes = { x: { ticks: { maxRotation: 90, autoSkip: true, maxTicksLimit: 20 }, grid: { display: false } },
                 y: { ticks: { precision: 0 },
                      grid: { color: (c) => (c.tick.value === 0 ? css("--ink") : css("--border")),
                              lineWidth: (c) => (c.tick.value === 0 ? 1.5 : 1) } } };
  Chart.defaults.color = css("--muted");
  charts.forEach((c) => c.destroy());
  charts = [
    new Chart($("#events-chart"), {
      type: "bar",
      data: { labels, datasets: [
        { label: "Down", data: weeks.map((w) => -w.down), backgroundColor: css("--down") },
        { label: "Recovery", data: weeks.map((w) => w.recovery), backgroundColor: css("--recovery") },
      ] },
      options: { maintainAspectRatio: false,
                 scales: { x: { ...axes.x, stacked: true },  // down events drawn below the axis
                           y: { ...axes.y, stacked: true, ticks: { ...axes.y.ticks, callback: (v) => Math.abs(v) } } },
                 plugins: { tooltip: { callbacks: { title,
                   label: (c) => `${c.dataset.label}: ${Math.abs(c.raw)}` } } } },
    }),
    new Chart($("#captures-chart"), {
      type: "bar",
      data: { labels, datasets: [{ label: "Captures", data: weeks.map((w) => w.captures),
        backgroundColor: weeks.map((w) => (w.days < 7 ? css("--accent") + "55" : css("--accent"))) }] },
      options: { maintainAspectRatio: false, scales: axes,
                 plugins: { legend: { display: false }, tooltip: { callbacks: { title } } } },
    }),
  ];
}

// ---------- migration check ----------

function renderMigration(record) {
  const data = record.result;
  const counts = {};
  data.checks.forEach((c) => (counts[c.category] = (counts[c.category] || 0) + 1));
  const total = data.checks.length;
  const categories = Object.keys(CATEGORY_MEANING).filter((c) => counts[c]);
  const note = data.old_urls > total ? ` (the first ${total} of ${data.old_urls})` : "";
  const blocked = data.checks.filter((c) => c.blocked_url).length;
  const unknown = data.robots_unknown || [];  // analyses saved before this existed have none
  const box = (key, tone, label, n, meaning) => `
      <div class="category" data-category="${esc(key)}">
        <span class="badge ${tone}">${esc(label)}</span><br>
        <b>${n}</b><span class="pct">${(100 * n / total).toFixed(1)}%</span>
        <p>${esc(meaning)}</p></div>`;
  $("#result").innerHTML = header(record, "Migration check") + `
    <p class="result-meta">${total} URLs checked${note}: they worked between ${esc(data.start)} and ${esc(data.end)}.
      Click a category to filter the table.</p>
    <div class="categories">${categories.map((c) =>
      box(c, CATEGORY_TONE[c], c, counts[c], CATEGORY_MEANING[c])).join("")}${blocked ?
      box(ROBOTS_FILTER, "bad", "blocked by robots.txt", blocked,
          "robots.txt blocks the URL or its redirect for Googlebot") : ""}</div>
    ${unknown.length ? `<p class="hint">The robots.txt of ${esc(unknown.join(", "))} never answered, so what Google may crawl there is unknown.</p>` : ""}
    <div class="table-wrap"><table><thead><tr><th>Old URL</th><th>Result</th><th>Final status</th></tr></thead>
      <tbody id="checks"></tbody></table></div>`;

  const fill = (category) => {
    const shown = (c) => !category || (category === ROBOTS_FILTER ? !!c.blocked_url : c.category === category);
    $("#checks").innerHTML = data.checks.filter(shown).map((c) => {
      const final = c.hops.length ? c.hops[c.hops.length - 1] : null;
      // one line per redirect: its status, then where it leads
      const chain = c.hops.slice(1).map(([url], i) => {
        const [from, status] = c.hops[i];
        return `<div class="chain">${esc(status)} → ${esc(sameOrigin(from, url) ? pathOf(url) : url)}</div>`;
      }).join("");
      const blockedUrl = c.blocked_url
        ? `<div class="chain blocked">Disallowed by robots.txt${c.blocked_url === c.url ? "" : `: ${esc(c.blocked_url)}`}</div>` : "";
      return `<tr><td class="url">${esc(c.url)}${chain}${c.problem ? `<div class="chain">${esc(c.problem)}</div>` : ""}${blockedUrl}</td>
        <td><span class="badge ${CATEGORY_TONE[c.category]}">${esc(c.category)}</span></td>
        <td>${final ? esc(final[1]) : ""}</td></tr>`;
    }).join("");
  };
  fill(null);
  $$(".category").forEach((el) => el.addEventListener("click", () => {
    const selected = !el.classList.contains("selected");
    $$(".category").forEach((c) => c.classList.remove("selected"));
    el.classList.toggle("selected", selected);
    fill(selected ? el.dataset.category : null);
  }));
}

// ---------- robots.txt history ----------

function renderRobots(record) {
  const rule = ([agent, directive, value], kind) =>
    `<div class="rule ${kind}">${kind === "added" ? "+" : "−"} [${esc(agent)}] ${esc(directive)}: ${esc(value)}</div>`;
  // analyses saved before notices existed have only "alerts", all warnings
  const warnings = (v) => v.warnings || v.alerts || [];
  const notices = (v) => v.notices || [];
  const rules = (v) => {
    const lines = v.added.map((r) => rule(r, "added")).join("") + v.removed.map((r) => rule(r, "removed")).join("");
    const n = v.added.length + v.removed.length;
    return n > COLLAPSE_OVER ? `<details class="rules"><summary>${n} rule changes</summary>${lines}</details>` : lines;
  };
  $("#result").innerHTML = header(record, "robots.txt history") + record.result.map((history) => {
    const count = (list) => history.versions.reduce((n, v) => n + list(v).length, 0);
    return `<h3>${esc(history.robots_url)}</h3>
      <div class="stats">${stat(history.archived, "archived versions")}${stat(history.versions.length - 1, "changes")}${stat(count(warnings), "warnings")}${stat(count(notices), "notices")}</div>
      ${history.versions.map((v, i) => `
        <div class="version ${warnings(v).length ? "has-warning" : notices(v).length ? "has-notice" : ""}">
          <div class="version-date">${esc(v.date)}${v.live ? " · live robots.txt" : i === 0 ? ` · ${history.skipped ? `oldest of the latest ${history.archived - history.skipped} versions` : "first archived version"}, ${v.rules} rules` : ""}
            <a href="${esc(v.capture)}" target="_blank" rel="noopener">${v.live ? "live file" : "archived file"}</a></div>
          ${warnings(v).map((a) => `<div class="warning">${esc(a)}</div>`).join("")}
          ${notices(v).map((a) => `<div class="notice">${esc(a)}</div>`).join("")}
          ${i === 0 ? "" : rules(v)}
        </div>`).join("")}
      ${history.live_unchanged ? `<p class="hint">The robots.txt online today is the same as the latest version above.</p>` : ""}
      ${history.failed ? `<p class="error">${history.failed} versions could not be downloaded.</p>` : ""}`;
  }).join("");
}

// ---------- theme: light by default, the choice remembered in this browser ----------

function applyTheme(theme) {
  if (theme === "dark") document.documentElement.dataset.theme = "dark";
  else delete document.documentElement.dataset.theme;
  $("#theme").textContent = theme === "dark" ? "Light" : "Dark";
  try { localStorage.setItem("theme", theme); } catch {}
  if (currentAnalysis) openAnalysis(currentAnalysis);  // charts pick up the new colours
}

// ---------- start ----------

$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTool(b.dataset.tool)));
$$(".tool-form").forEach((form) => form.addEventListener("submit", (e) => { e.preventDefault(); run(form); }));
$("#history-list").addEventListener("click", (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  if (e.target.classList.contains("delete")) deleteAnalysis(li.dataset.id);
  else openAnalysis(li.dataset.id);
});
$("#cache-clear").addEventListener("click", clearCache);
$("#stop").addEventListener("click", async () => {
  if (!runningJob) return;
  $("#stop").disabled = true;
  $("#progress-title").textContent = "Stopping after the current request…";
  await fetch(`api/jobs/${runningJob}`, { method: "DELETE" });
});
$("#new-analysis").addEventListener("click", newAnalysis);
$("#theme").addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
$("#theme").textContent = document.documentElement.dataset.theme === "dark" ? "Light" : "Dark";
resetForm("down");
loadCacheSize();
loadHistory().then(() => location.hash.length > 1 && openAnalysis(location.hash.slice(1)));
