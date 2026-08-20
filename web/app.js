/* ============================================================
   Group Post Automator — frontend logic (no dependencies)
   Polls the local JSON API; renders state, log, groups, modals.
   ============================================================ */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  lastLogId: 0,
  current: null,
  groupStatus: "safe",
  lastAutoVerify: null,
  logLines: 0,
  recentTimer: 0,
};

/* ---------------- helpers ---------------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function fmtMembers(n) {
  const v = Number(n) || 0;
  if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return String(v);
}
function fmtWhen(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d)) return String(iso).slice(0, 16);
    const now = new Date();
    const diff = Math.floor((now - d) / 1000);
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    return d.toLocaleDateString();
  } catch {
    return String(iso).slice(0, 16);
  }
}
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error("HTTP " + res.status + " " + path);
  return res.json();
}
let toastTimer = 0;
function toast(text, level = "info") {
  const el = $("#toast");
  el.textContent = text;
  el.className = "toast " + level;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.classList.add("hidden"); }, 4200);
}
function openModal(id) { $('#' + id).removeAttribute("hidden"); }
function closeModal(id) { $('#' + id).setAttribute("hidden", ""); }

/* ---------------- polling ---------------- */
async function poll() {
  try {
    const [st, logs] = await Promise.all([
      api("/api/state"),
      api("/api/logs?since=" + state.lastLogId),
    ]);
    state.current = st;
    if (logs.lines && logs.lines.length) {
      appendLogs(logs.lines);
      state.lastLogId = logs.lines[logs.lines.length - 1].id;
    }
    render();
  } catch (e) {
    console.error("poll failed", e);
    $("#statusText").textContent = "Server offline — restart the app";
    $("#statusPill").className = "pill idle";
  }
  setTimeout(poll, 500);
}

/* ---------------- rendering ---------------- */
function render() {
  const st = state.current;
  if (!st) return;

  // topbar
  if (st.busy) {
    const pill = st.job === "scan" ? "scanning" : "running";
    $("#statusPill").className = "pill " + pill;
    $("#statusText").textContent =
      st.job === "scan" ? "Scanning groups…" : `Running… ${st.posts_today} posted today`;
  } else {
    $("#statusPill").className = "pill idle";
    $("#statusText").textContent = "Idle";
  }
  $("#postedChip").textContent = st.posts_today;

  // accounts
  renderAccountBar(st);
  renderBanner(st);

  // stats
  $("#statPostedToday").textContent = st.posts_today;
  $("#statTotalPosted").textContent = (st.post_stats?.posted || 0) + (st.post_stats?.pending || 0) + (st.post_stats?.failed || 0);
  $("#statSafe").textContent = st.group_counts.safe;
  $("#statSkip").textContent = st.group_counts.skip;
  $("#statReview").textContent = st.group_counts.unknown;
  $("#statMedia").textContent = st.media_count;
  $("#badgeSafe").textContent = st.group_counts.safe;
  $("#badgeSkip").textContent = st.group_counts.skip;
  $("#badgeUnknown").textContent = st.group_counts.unknown;

  // run controls
  const busy = st.busy;
  const noProfile = !st.selected;
  const opRunning = busy || st.login.running || st.import.running || st.verify.running;
  $("#btnStart").disabled = opRunning || noProfile;
  $("#btnScan").disabled = opRunning || noProfile;
  $("#btnStop").disabled = !busy;
  $("#btnVerify").disabled = opRunning || noProfile;
  $("#btnImport").disabled = opRunning;

  const run = $("#runStatus");
  if (busy) {
    run.className = "run-status running";
    run.textContent = st.job === "scan"
      ? "Scanning groups… (stop available)"
      : `Posting… ${st.posts_today} posted today. Press Stop when done.`;
  } else if (st.import.running) {
    run.className = "run-status";
    run.textContent = "Importing Chrome session…";
  } else if (st.login.running) {
    run.className = "run-status";
    run.textContent = "Waiting for you to finish logging in…";
  } else if (st.verify.running) {
    run.className = "run-status";
    run.textContent = "Checking Facebook session…";
  } else {
    run.className = "run-status";
    run.textContent = "Idle — ready when you are.";
  }

  renderRecent();
  maybeAutoVerify(st);

  // groups refresh (only if tab visible & every ~4s)
  if (!document.hidden && !state.groupTimer) {
    state.groupTimer = setTimeout(() => {
      state.groupTimer = 0;
      loadGroups(state.groupStatus, false);
    }, 4000);
  }
}

function renderAccountBar(st) {
  const sel = $("#profileSelect");
  const names = (st.profiles || []).map((p) => p.name);
  sel.innerHTML = "";
  if (!names.length) {
    const o = document.createElement("option");
    o.value = "";
    o.textContent = "No account — add one";
    sel.appendChild(o);
  } else {
    names.forEach((n) => {
      const o = document.createElement("option");
      o.value = n;
      o.textContent = n;
      sel.appendChild(o);
    });
  }
  if (st.selected) sel.value = st.selected.name;

  const chip = $("#accountChip");
  const ps = st.selected ? st.selected.status : null;
  if (!ps) {
    chip.className = "chip account-chip";
    chip.textContent = "—";
    $("#acctDetail").textContent = "Add an account or import from Chrome to begin.";
  } else {
    const code = ps.code;
    chip.className = "chip account-chip " + code;
    chip.textContent = ps.label;
    const extra = ps.detail ? " · " + ps.detail : "";
    $("#acctDetail").textContent = `Profile folder on disk: ${ps.dir_exists ? "found" : "missing"}` + extra;
  }
  $("#btnVerify .lbl").textContent = st.verify.running ? "Checking…" : "Check session";
  $("#btnImport .lbl").textContent = st.import.running ? "Importing…" : "Import from Chrome";
}

function renderBanner(st) {
  const b = $("#banner");
  const sys = st.system;
  if (!sys) { b.classList.add("hidden"); return; }
  b.classList.remove("hidden");
  b.className = "banner banner-" + sys.level;
  $("#bannerIcon").textContent = sys.level === "error" ? "!" : sys.level === "success" ? "✓" : "i";
  $("#bannerTitle").textContent = sys.title;
  $("#bannerText").textContent = sys.text;

  const actions = $("#bannerActions");
  actions.innerHTML = "";
  (sys.actions || []).forEach((a) => {
    const btn = document.createElement("button");
    btn.className = "btn outline small";
    btn.textContent = a.label;
    btn.addEventListener("click", () => bannerAction(a.id));
    actions.appendChild(btn);
  });
}

function bannerAction(id) {
  switch (id) {
    case "add": openModal("modalAdd"); break;
    case "import": openImportModal(); break;
    case "relogin": reloginSelected(); break;
    case "verify": verifySession(); break;
    case "remove": removeSelected(); break;
  }
}

function maybeAutoVerify(st) {
  if (!st.selected) return;
  if (state.lastAutoVerify === st.selected.name) return;
  if (st.busy || st.login.running || st.import.running || st.verify.running) return;
  if (st.selected.status.code === "never_checked") {
    state.lastAutoVerify = st.selected.name;
    verifySession(true);
  }
}

/* ---------------- recent posts ---------------- */
async function renderRecent() {
  try {
    const data = await api("/api/posts?limit=6");
    const list = $("#recentList");
    const posts = data.posts || [];
    if (!posts.length) {
      list.innerHTML = '<div class="recent-empty">No posts yet this session.</div>';
      return;
    }
    const colors = { posted: "#22c55e", pending: "#f59e0b", failed: "#ef4444" };
    list.innerHTML = posts.map((p) => `
      <div class="recent-row" title="${esc(p.group_name)} · ${esc(p.status)}">
        <span class="dot" style="background:${colors[p.status] || "#64748b"}"></span>
        <span class="gname">${esc(p.group_name || p.group_id)}</span>
        <span class="when">${esc(fmtWhen(p.created_at))}</span>
      </div>`).join("");
  } catch { /* best effort */ }
}

/* ---------------- log ---------------- */
function appendLogs(lines) {
  const panel = $("#logPanel");
  const shouldStick = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 40;
  const empty = $("#logPanel .log-empty");
  if (empty) empty.remove();
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    const d = document.createElement("div");
    d.className = "log-line " + (line.level || "info");
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = "[" + new Date().toTimeString().slice(0, 8) + "]";
    d.appendChild(ts);
    d.appendChild(document.createTextNode(line.text));
    frag.appendChild(d);
  }
  panel.appendChild(frag);
  state.logLines += lines.length;
  $("#logCount").textContent = state.logLines + " lines";
  // keep the panel bounded
  while (panel.children.length > 800) panel.removeChild(panel.firstChild);
  if (shouldStick) panel.scrollTop = panel.scrollHeight;
}

/* ---------------- actions: run ---------------- */
function fieldSnapshot() {
  const selectedName = $("#profileSelect").value;
  return {
    profile: selectedName,
    keyword: $("#keywordInput").value.trim(),
    mode: $('input[name="mode"]:checked').value,
    min_members: $("#minMembers").value,
    caption: $("#captionInput").value,
    media_folder: $("#mediaFolder").value.trim(),
  };
}

async function startRun() {
  const body = fieldSnapshot();
  if (!body.profile) return toast("Add an account first.", "warn");
  try {
    const r = await api("/api/start", { method: "POST", body });
    if (r.ok) toast("Posting started.", "success");
    else toast(r.error || "Could not start.", "error");
  } catch (e) { toast("Start failed: " + e.message, "error"); }
}

async function scanGroups() {
  const body = fieldSnapshot();
  if (!body.profile) return toast("Add an account first.", "warn");
  if (!body.keyword) toast("No keyword — will re-check unclassified groups only.", "warn");
  try {
    const r = await api("/api/scan", { method: "POST", body });
    if (r.ok) toast("Scan started.", "success");
    else toast(r.error || "Could not start scan.", "error");
  } catch (e) { toast("Scan failed: " + e.message, "error"); }
}

async function stopRun() {
  try {
    await api("/api/stop", { method: "POST", body: {} });
    toast("Stopping after the current post…", "warn");
  } catch (e) { toast("Stop failed: " + e.message, "error"); }
}

/* ---------------- actions: accounts ---------------- */
async function addAccount(name) {
  try {
    const r = await api("/api/add_account", { method: "POST", body: { name } });
    if (!r.ok) return toast(r.error || "Could not add account.", "error");
    closeModal("modalAdd");
    toast(`Account '${name}' created — a browser will open for login.`, "success");
  } catch (e) { toast("Add failed: " + e.message, "error"); }
}

async function reloginSelected() {
  const name = $("#profileSelect").value;
  if (!name) return toast("Select an account first.", "warn");
  try {
    const r = await api("/api/relogin", { method: "POST", body: { name } });
    if (!r.ok) toast(r.error || "Could not open login.", "error");
    else toast("Browser opened — complete login and close the window.", "success");
  } catch (e) { toast("Relogin failed: " + e.message, "error"); }
}

async function verifySession(silent) {
  const name = $("#profileSelect").value;
  if (!name) return;
  try {
    const r = await api("/api/verify_session", { method: "POST", body: { name } });
    if (!r.ok && !silent) toast(r.error || "Check unavailable.", "warn");
  } catch (e) { if (!silent) toast("Check failed: " + e.message, "error"); }
}

async function removeSelected() {
  const name = $("#profileSelect").value;
  if (!name) return;
  if (!confirm(`Remove account '${name}'? Profile files stay on disk.`)) return;
  try {
    await api("/api/remove_account", { method: "POST", body: { name } });
    toast(`Removed '${name}'.`, "success");
  } catch (e) { toast("Remove failed: " + e.message, "error"); }
}

async function importChrome() {
  const name = $("#importName").value.trim();
  const chromeProfile = $("#importProfile").value;
  if (!name) return toast("Enter an account name for the imported session.", "warn");
  if (!chromeProfile) return toast("Choose a Chrome profile to import.", "warn");
  try {
    const r = await api("/api/import_chrome", { method: "POST", body: { name, chrome_profile: chromeProfile } });
    if (!r.ok) return toast(r.error || "Import failed to start.", "error");
    closeModal("modalImport");
    toast("Import started — this can take a minute.", "success");
  } catch (e) { toast("Import failed: " + e.message, "error"); }
}

/* ---------------- settings ---------------- */
function openSettings() {
  const s = state.current.settings;
  $("#setDelayMin").value = s.delay_min ?? 180;
  $("#setDelayMax").value = s.delay_max ?? 480;
  $("#setSoftCap").value = s.soft_cap ?? 150;
  $("#setMaxCycle").value = s.max_cycle_posts ?? 0;
  $("#setHeadless").checked = !!s.headless;
  $("#setScanOnStart").checked = !!s.scan_on_start;
  openModal("modalSettings");
}

async function saveSettings() {
  const body = {
    delay_min: $("#setDelayMin").value,
    delay_max: $("#setDelayMax").value,
    soft_cap: $("#setSoftCap").value,
    max_cycle_posts: $("#setMaxCycle").value,
    headless: $("#setHeadless").checked,
    scan_on_start: $("#setScanOnStart").checked,
  };
  try {
    const r = await api("/api/settings", { method: "POST", body });
    if (!r.ok) return toast(r.error || "Could not save.", "error");
    closeModal("modalSettings");
    toast("Settings saved.", "success");
  } catch (e) { toast("Save failed: " + e.message, "error"); }
}

/* ---------------- manage accounts ---------------- */
async function refreshAccountsModal() {
  const st = state.current;
  const list = $("#accountsList");
  list.innerHTML = "";
  if (!st.profiles.length) {
    list.innerHTML = '<div class="recent-empty">No accounts yet.</div>';
    return;
  }
  st.profiles.forEach((p) => {
    const ps = st.selected && st.selected.name === p.name ? st.selected.status : { label: "—", code: "" };
    const row = document.createElement("div");
    row.className = "account-row";
    row.innerHTML = `
      <div class="aname">${esc(p.name)}</div>
      <div class="adetail">${esc(p.user_data_dir)}</div>
      <div class="abtns">
        <button class="btn outline mini" data-act="relogin">Re-login</button>
        <button class="btn outline mini" data-act="import">Import Chrome</button>
        <button class="btn danger mini" data-act="remove">Remove</button>
      </div>`;
    row.querySelector('[data-act="relogin"]').addEventListener("click", () => {
      api("/api/relogin", { method: "POST", body: { name: p.name } })
        .then((r) => toast(r.ok ? "Browser opened for login." : (r.error || "Error"), r.ok ? "success" : "error"));
    });
    row.querySelector('[data-act="import"]').addEventListener("click", () => {
      closeModal("modalAccounts");
      setTimeout(() => { $("#importName").value = p.name; openImportModal(); }, 60);
    });
    row.querySelector('[data-act="remove"]').addEventListener("click", async () => {
      if (!confirm(`Remove account '${p.name}'? Profile files stay on disk.`)) return;
      await api("/api/remove_account", { method: "POST", body: { name: p.name } });
      refreshAccountsModal();
      toast(`Removed '${p.name}'.`, "success");
    });
    list.appendChild(row);
  });
}

/* ---------------- import modal ---------------- */
async function openImportModal() {
  const el = $("#chromeStatus");
  const sel = $("#importProfile");
  sel.innerHTML = "";
  try {
    const c = await api("/api/chrome_profiles");
    if (!c.available) {
      el.className = "inline-note error";
      el.textContent = "No Chrome/Edge user-data folder found. Install Chrome, use it once, then import here.";
      openModal("modalImport");
      return;
    }
    if (!c.profiles.length) {
      el.className = "inline-note warn";
      el.textContent = "Chrome found, but no profiles with login data were detected.";
    } else {
      el.className = "inline-note ok";
      el.textContent = "Found " + c.profiles.length + " Chrome profile(s) — close Chrome fully before importing for a clean copy.";
    }
    c.profiles.forEach((p) => {
      const o = document.createElement("option");
      o.value = p; o.textContent = p;
      sel.appendChild(o);
    });
  } catch (e) {
    el.className = "inline-note error";
    el.textContent = "Could not list Chrome profiles: " + e.message;
  }
  openModal("modalImport");
}

/* ---------------- folder browser ---------------- */
let browsePath = "";
async function openBrowse() {
  browsePath = $("#mediaFolder").value.trim() || "";
  openModal("modalBrowse");
  await loadBrowse();
}
async function loadBrowse() {
  const dirs = $("#browseDirs");
  const pathEl = $("#browsePath");
  try {
    const r = await api("/api/browse?path=" + encodeURIComponent(browsePath));
    if (!r.ok) {
      pathEl.textContent = r.error || "Error";
      dirs.innerHTML = '<div class="browse-empty">Could not open that folder.</div>';
      $("#btnBrowseSelect").disabled = true;
      return;
    }
    browsePath = r.path || "";
    pathEl.textContent = r.path || "My Computer (drives)";
    $("#btnBrowseUp").disabled = !r.parent;
    $("#btnBrowseSelect").disabled = !r.selectable;
    dirs.innerHTML = "";
    if (!r.dirs.length) {
      dirs.innerHTML = '<div class="browse-empty">No subfolders here.</div>';
    }
    r.dirs.forEach((d) => {
      const b = document.createElement("button");
      b.className = "browse-dir";
      b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span>${esc(d)}</span>`;
      b.addEventListener("click", () => { browsePath = r.path ? r.path + (r.path.endsWith("\\") || r.path.endsWith("/") ? "" : "\\") + d : d; loadBrowse(); });
      dirs.appendChild(b);
    });
  } catch (e) {
    pathEl.textContent = "Error: " + e.message;
  }
}
function browseUp() {
  const st = state.current;
  const cur = browsePath;
  if (!cur) return;
  const parts = cur.replace(/\//g, "\\").split("\\").filter(Boolean);
  parts.pop();
  browsePath = parts.length ? parts.join("\\") + "\\" : "";
  loadBrowse();
}
function selectBrowse() {
  if (browsePath) {
    $("#mediaFolder").value = browsePath;
    toast("Media folder set.", "success");
  }
  closeModal("modalBrowse");
}

/* ---------------- groups ---------------- */
function switchTab(status) {
  state.groupStatus = status;
  $$("#groupTabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.status === status));
  loadGroups(status, true);
}

let groupsCache = {};
async function loadGroups(status, force) {
  if (!force && groupsCache[status] && Date.now() - groupsCache[status].t < 5000) {
    renderGroups(groupsCache[status].rows);
    return;
  }
  try {
    const r = await api("/api/groups?status=" + encodeURIComponent(status));
    groupsCache[status] = { rows: r.groups, t: Date.now() };
    renderGroups(r.groups);
  } catch (e) {
    console.error("groups fetch failed", e);
  }
}

function renderGroups(rows) {
  const body = $("#groupsBody");
  if (!rows.length) {
    const msg = state.groupStatus === "unknown"
      ? "No groups in review. Run a scan to discover and classify groups."
      : `No ${state.groupStatus === "safe" ? "safe" : "skipped"} groups yet. Classify groups from the Review tab.`;
    body.innerHTML = `<tr><td colspan="6" class="empty-cell" id="groupsEmpty">${msg}</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((g) => {
    const name = g.name || g.id;
    return `<tr data-id="${esc(g.id)}">
      <td><div class="gname" title="${esc(name)}">${esc(name)}</div><div class="gid">${esc(g.id)}</div></td>
      <td class="num mono">${fmtMembers(g.member_count)}</td>
      <td><span class="signal" title="${esc(g.approval_signal || "")}">${esc(g.approval_signal || "—")}</span></td>
      <td class="num mono">${g.times_posted ?? 0}</td>
      <td class="mono">${esc(fmtWhen(g.last_posted_at))}</td>
      <td class="actions">
        <span class="row-actions">
          ${state.groupStatus !== "safe" ? `<button class="row-btn safe" data-act="mark-safe" title="Post here immediately">✓ Safe</button>` : ""}
          ${state.groupStatus !== "skip" ? `<button class="row-btn skip" data-act="mark-skip" title="Skip — posts go to approval">✕ Skip</button>` : ""}
          <button class="row-btn del" data-act="delete" title="Delete this group">Delete</button>
        </span>
      </td>
    </tr>`;
  }).join("");

  $$("#groupsBody tr").forEach((tr) => {
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest(".row-btn")) return;
      $$("#groupsBody tr").forEach((o) => o.classList.remove("selected"));
      tr.classList.add("selected");
    });
    const gid = tr.dataset.id;
    tr.querySelector('[data-act="mark-safe"]')?.addEventListener("click", () => markGroup(gid, "safe"));
    tr.querySelector('[data-act="mark-skip"]')?.addEventListener("click", () => markGroup(gid, "skip"));
    tr.querySelector('[data-act="delete"]')?.addEventListener("click", () => deleteGroup(gid));
  });
}

async function markGroup(id, status) {
  try {
    const r = await api("/api/group/mark", { method: "POST", body: { id, status } });
    if (!r.ok) return toast(r.error || "Mark failed.", "error");
    groupsCache = {};
    loadGroups(state.groupStatus, true);
    toast("Group marked " + status + ".", "success");
  } catch (e) { toast("Mark failed: " + e.message, "error"); }
}
async function deleteGroup(id) {
  const g = state.current.group_counts;
  if (!confirm("Delete this group from the database?")) return;
  try {
    await api("/api/group/delete", { method: "POST", body: { id } });
    groupsCache = {};
    loadGroups(state.groupStatus, true);
    toast("Group deleted.", "success");
  } catch (e) { toast("Delete failed: " + e.message, "error"); }
}

/* ---------------- wiring ---------------- */
function bind() {
  $("#btnStart").addEventListener("click", startRun);
  $("#btnStop").addEventListener("click", stopRun);
  $("#btnScan").addEventListener("click", scanGroups);

  $("#btnAddAccount").addEventListener("click", () => {
    $("#addName").value = "";
    openModal("modalAdd");
  });
  $("#btnConfirmAdd").addEventListener("click", () => addAccount($("#addName").value));
  $("#addName").addEventListener("keydown", (e) => { if (e.key === "Enter") addAccount($("#addName").value); });

  $("#btnImport").addEventListener("click", openImportModal);
  $("#btnConfirmImport").addEventListener("click", importChrome);

  $("#btnVerify").addEventListener("click", () => verifySession(false));
  $("#btnManage").addEventListener("click", () => { refreshAccountsModal(); openModal("modalAccounts"); });
  $("#btnAccountsAdd").addEventListener("click", () => {
    closeModal("modalAccounts");
    setTimeout(() => { $("#addName").value = ""; openModal("modalAdd"); }, 60);
  });

  $("#btnSettings").addEventListener("click", openSettings);
  $("#btnSaveSettings").addEventListener("click", saveSettings);

  $("#btnBrowseFolder").addEventListener("click", openBrowse);
  $("#btnBrowseUp").addEventListener("click", browseUp);
  $("#btnBrowseSelect").addEventListener("click", selectBrowse);

  $("#btnRefreshGroups").addEventListener("click", () => { groupsCache = {}; loadGroups(state.groupStatus, true); });
  $$("#groupTabs .tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.status)));

  $("#profileSelect").addEventListener("change", async () => {
    const name = $("#profileSelect").value;
    if (!name) return;
    state.lastAutoVerify = null;
    await api("/api/prefs", { method: "POST", body: { last_profile: name } });
  });

  $("#btnClearLog").addEventListener("click", async () => {
    try {
      await api("/api/logs/clear", { method: "POST", body: {} });
      $("#logPanel").innerHTML = '<div class="log-empty">Log cleared.</div>';
      state.logLines = 0;
      $("#logCount").textContent = "0 lines";
    } catch (e) { toast("Clear failed: " + e.message, "error"); }
  });

  // modal close wiring
  $$(".modal").forEach((m) => {
    const id = m.id;
    m.querySelector(".modal-backdrop").addEventListener("click", () => closeModal(id));
    m.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => closeModal(id)));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $$(".modal").forEach((m) => m.setAttribute("hidden", ""));
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bind();
  poll();
  loadGroups("safe", true);
});
