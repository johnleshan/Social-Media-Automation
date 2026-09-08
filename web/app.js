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
  groupStatus: "all",
  lastAutoVerify: null,
  logLines: 0,
  recentTimer: 0,
  activeTab: null,
  prevBusy: false,
  unseenLogCount: 0,
  toastFiredFor: null,
  media: [],           // list of {name,path,is_video} from /api/media
  mediaLoaded: false,
  mediaMode: "single", // "single" | "multiple"
  selectedMedia: [],   // array of {path,name,is_video}
  composerSynced: false,
  lastMediaCount: -1,
  previewSig: "",
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
function toast(text, level = "info") {
  const container = $("#toastContainer");
  const el = document.createElement("div");
  el.className = "toast " + level;
  el.textContent = text;
  el.addEventListener("click", () => el.remove());
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 200); }, 5500);
}
function openModal(id) { $('#' + id).removeAttribute("hidden"); }
function closeModal(id) { $('#' + id).setAttribute("hidden", ""); }

function renderTodayPosts(posts) {
  const list = $("#todayPostsList");
  const empty = $("#todayPostsEmpty");
  list.innerHTML = "";
  if (!posts || !posts.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  for (const p of posts) {
    const name = p.group_name || p.group_id || "Unknown group";
    const file = p.file_path ? p.file_path.split(/[\\/]/).pop() : "";
    const url = p.post_url || "";
    const row = document.createElement("a");
    row.className = "tp-row" + (url ? "" : " tp-row-no-link");
    if (url) { row.href = url; row.target = "_blank"; row.rel = "noopener"; }
    row.innerHTML =
      `<span class="tp-name">${esc(name)}</span>` +
      (file ? `<span class="tp-file">${esc(file)}</span>` : "") +
      (url
        ? `<span class="tp-goto">Open post ↗</span>`
        : `<span class="tp-no-url">no link (pending/inactive)</span>`) +
      `<span class="tp-time">${esc(fmtWhen(p.created_at))}</span>`;
    list.appendChild(row);
  }
}

function openTodayPosts() {
  renderTodayPosts((state && state.current && state.current.posted_today) || []);
  openModal("modalTodayPosts");
}


/* ============================================================
   CONTENT COMPOSER
   Single source of truth for the post caption + media staging.
   Content is held in the BROWSER (localStorage) only — never stored by the
   backend. The caption is passed to the posting engine at post-time via the
   request body, used for that run, then forgotten.
   Media selection is a preview/staging aid — the bot publishes the
   files present in the media folder.
   ============================================================ */
const COMPOSER_LS = "sm_composer";
function composerStorage() {
  try {
    return JSON.parse(localStorage.getItem(COMPOSER_LS)) || {};
  } catch { return {}; }
}
function composerSave() {
  try {
    localStorage.setItem(COMPOSER_LS, JSON.stringify({
      text: $("#composerText").value,
      mode: state.mediaMode,
      selected: state.selectedMedia.map((m) => ({"path": m.path, "name": m.name, "is_video": m.is_video})),
    }));
  } catch {}
}

function getCaption() {
  // single source of truth = the composer textarea
  return ($("#composerText")?.value || "").trim();
}
function setCaption(text) {
  const t = String(text ?? "");
  $("#composerText").value = t;
  // keep the advanced-settings caption in sync (same value)
  if ($("#captionInput").value !== t) $("#captionInput").value = t;
  updateCaptionHint();
  renderPostPreview();
}

async function persistCaption(text) {
  // Content is held in the browser only (localStorage). We never persist the
  // composed post body to the backend or its config — it is forgotten after
  // posting. The caption is passed to the posting engine at post-time only.
  composerSave();
}

function updateCaptionHint() {
  const el = $("#composerCharHint");
  if (el) el.textContent = `${$("#composerText").value.length} / 4000`;
}

function mediaEl(m) {
  const node = document.createElement("div");
  if (m.is_video) {
    // Live preview: an actual playable video (muted autoplay frame + play
    // overlay). Click to play — this mirrors how Facebook shows a video post
    // (a playable video with a play control), not a static image.
    node.className = "pm-video";
    const v = document.createElement("video");
    v.muted = true;
    v.playsInline = true;
    v.preload = "metadata";
    v.controls = true;
    v.src = "/api/media_file?path=" + encodeURIComponent(m.path);
    node.appendChild(v);
  } else {
    const img = document.createElement("img");
    img.className = "pm-img";
    img.src = "/api/media_file?path=" + encodeURIComponent(m.path);
    img.alt = m.name;
    node.appendChild(img);
  }
  return node;
}

function videoPlayOverlay() {
  const ov = document.createElement("div");
  ov.className = "pm-play";
  ov.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
  return ov;
}

function renderPostPreview() {
  const textEl = $("#previewText");
  const mediaEl2 = $("#previewMedia");
  if (!textEl || !mediaEl2) return;
  const text = $("#composerText").value;
  const name = state.current?.selected?.name || "Your account";
  const sig = text.length + "|" + text + "|" + name + "|" +
    state.selectedMedia.map((m) => m.path).join(",");
  if (state.previewSig === sig) return;
  state.previewSig = sig;

  if (text.trim()) {
    textEl.textContent = text;
    textEl.classList.remove("muted");
  } else {
    textEl.classList.add("muted");
    textEl.textContent = "Your post text and media will appear here as you type.";
  }
  // avatar / name from selected account
  if ($("#previewAvatar")) {
    $("#previewAvatar").textContent = (name === "Your account" ? "A" : name.charAt(0)).toUpperCase();
  }
  $("#previewName").textContent = name;

  mediaEl2.innerHTML = "";
  if (state.selectedMedia.length) {
    state.selectedMedia.forEach((m) => mediaEl2.appendChild(mediaEl(m)));
  } else {
    const empty = document.createElement("div");
    empty.className = "pm-empty";
    empty.textContent = "No media selected — pick a file from the list to preview it.";
    mediaEl2.appendChild(empty);
  }
}

function renderComposerMediaList() {
  const list = $("#composerMediaList");
  const count = $("#composerMediaCount");
  const guide = $("#composerMediaGuide");
  if (!list) return;

  if (!state.mediaLoaded) {
    list.innerHTML = '<div class="composer-empty">Loading media…</div>';
    return;
  }
  if (state.media.length === 0) {
    list.innerHTML = '<div class="composer-empty">No media files found. Drop images or videos into your content folder, then press Refresh.</div>';
    if (count) count.textContent = "0 media";
    if (guide) {
      guide.className = "inline-note warn";
      guide.textContent = "Your media folder appears to be empty. Add JPG/PNG/GIF/MP4… files to the folder, then press Refresh. You can change the folder with the “Change folder” button.";
    }
    return;
  }
  if (count) count.textContent = state.media.length + (state.media.length === 1 ? " media" : " media files");
  if (guide) guide.classList.add("hidden");

  const selPaths = new Set(state.selectedMedia.map((m) => m.path));
  const frag = document.createDocumentFragment();
  state.media.forEach((m) => {
    const tile = document.createElement("div");
    tile.className = "media-tile" + (selPaths.has(m.path) ? " selected" : "");
    tile.dataset.path = m.path;
    tile.title = m.name;
    if (m.is_video) {
      const v = document.createElement("video");
      v.className = "tile-img";
      v.muted = true;
      v.playsInline = true;
      v.preload = "metadata";
      v.src = "/api/media_file?path=" + encodeURIComponent(m.path);
      tile.appendChild(v);
      tile.appendChild(videoPlayOverlay());
      const chk = document.createElement("span");
      chk.className = "tile-check";
      chk.textContent = "✓";
      tile.appendChild(chk);
    } else {
      const img = document.createElement("img");
      img.className = "tile-img";
      img.src = "/api/media_file?path=" + encodeURIComponent(m.path);
      img.alt = m.name;
      img.loading = "lazy";
      tile.appendChild(img);
      const chk = document.createElement("span");
      chk.className = "tile-check";
      chk.textContent = "✓";
      tile.appendChild(chk);
    }
    tile.addEventListener("click", () => toggleMediaSelect(m));
    frag.appendChild(tile);
  });
  list.innerHTML = "";
  list.appendChild(frag);

  const info = $("#composerSelInfo");
  if (info) {
    info.textContent = state.selectedMedia.length
      ? `${state.selectedMedia.length} selected`
      : "";
  }
}

function toggleMediaSelect(m) {
  const i = state.selectedMedia.findIndex((x) => x.path === m.path);
  if (i >= 0) {
    state.selectedMedia.splice(i, 1);
  } else {
    if (state.mediaMode === "single") state.selectedMedia = [];
    state.selectedMedia.push({ path: m.path, name: m.name, is_video: m.is_video });
  }
  composerSave();
  renderComposerMediaList();
  renderPostPreview();
}

async function loadMedia(force) {
  // if not forced and we don't have a folder configured yet, wait
  try {
    const r = await api("/api/media");
    state.mediaLoaded = true;
    state.media = r.files || [];
    // drop any selected media no longer present
    const avail = new Set(state.media.map((m) => m.path));
    state.selectedMedia = state.selectedMedia.filter((m) => avail.has(m.path));
    composerSave();
    renderComposerMediaList();
    renderPostPreview();
    if ($("#composerSub")) {
      const folder = r.folder || "";
      $("#composerSub").textContent = folder
        ? `Post text & media from “${folder}” — then hit Post Batch`
        : "Write the post text & pick your media — then hit Post Batch";
    }
  } catch (e) {
    state.mediaLoaded = true;
    state.media = [];
    const list = $("#composerMediaList");
    if (list) list.innerHTML = '<div class="composer-empty">Could not list media: ' + esc(e.message) + '</div>';
  }
}

function clearComposerMedia() {
  state.selectedMedia = [];
  composerSave();
  renderComposerMediaList();
  renderPostPreview();
}

/* sync backend caption into the composer once (after first state load) */
function syncCaptionFromState() {
  if (state.composerSynced || !state.current?.settings) return;
  // Only initialize the composer textarea from the backend once, then let the
  // user (and localStorage) take over. If the user already has a saved draft,
  // prefer it.
  const saved = composerStorage().text;
  const backend = state.current.settings.caption || "";
  const draft = (saved !== undefined && saved !== null) ? saved : backend;
  setCaption(draft);
  state.composerSynced = true;
}

/* ---------------- teleport: switch to a tab after starting an action ---------------- */
function teleport(tabName) {
  switchTab(tabName);
}

/* ---------------- polling ---------------- */
async function poll() {
  try {
    const [st, logs] = await Promise.all([
      api("/api/state"),
      api("/api/logs?since=" + state.lastLogId),
    ]);
    const wasBusy = state.prevBusy;
    const nowBusy = st.busy;
    state.current = st;
    if (logs.lines && logs.lines.length) {
      appendLogs(logs.lines);
      state.lastLogId = logs.lines[logs.lines.length - 1].id;
    }
    // completion toast on busy -> idle transition (use last_result from backend)
    if (wasBusy && !nowBusy) {
      showCompletionToast(st);
      state.toastFiredFor = null;
    }
    state.prevBusy = nowBusy;
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
    const pill = st.job === "scan" ? "scanning" : st.job === "check" ? "checking" : "running";
    $("#statusPill").className = "pill " + pill;
    $("#statusText").textContent =
      st.status_text
      || (st.job === "scan" ? "Scanning groups…"
      : st.job === "join" ? "Auto-joining groups…"
      : st.job === "check" ? "Checking join status…"
      : `Running… ${st.posts_today} posted today`);
  } else {
    $("#statusPill").className = "pill idle";
    const lr = st.last_result;
    $("#statusText").textContent = lr ? lr.title : "Idle";
  }
  $("#postedChip").textContent = st.posts_today;

  // accounts
  renderAccountBar(st);
  renderAccountsOverview(st);
  renderBanner(st);
  renderResumeBanner(st);
  renderHardStopBanner(st);

  // stats
  $("#statPostedToday").textContent = st.posts_today;
  $("#statTotalPosted").textContent = (st.post_stats?.posted || 0) + (st.post_stats?.pending || 0) + (st.post_stats?.failed || 0);
  $("#statSafe").textContent = st.group_counts.safe;
  $("#statSkip").textContent = st.group_counts.skip;
  $("#statJoined").textContent = st.group_counts.joined;
  $("#statMedia").textContent = st.media_count;
  $("#badgeSafe").textContent = st.group_counts.safe;
  $("#badgeSkip").textContent = st.group_counts.skip;
  $("#badgeUnknown").textContent = st.group_counts.unknown;
  if ($("#badgeAll")) $("#badgeAll").textContent = st.group_counts.total;

  // join-limit indicator (Groups tab) — always visible so the user knows where they stand
  const jl = $("#joinLimitStatus");
  const jlEl = $("#joinLimitText");
  if (jl && jlEl) {
    const jt = st.join_today ?? 0;
    const cap = st.hard_stop?.cap ?? 10;
    const left = Math.max(0, cap - jt);
    const hit = left <= 0 || (st.hard_stop && (st.hard_stop.joined ?? 0) >= cap);
    jl.classList.remove("hidden");
    if (hit) {
      jl.className = "join-limit limit-hit";
      jlEl.textContent = `Today's join cap reached (${Math.max(jt, st.hard_stop?.joined ?? 0)}/${cap}) - resume tomorrow`;
    } else {
      jl.className = "join-limit";
      jlEl.textContent = `${left} of ${cap} safe joins left today`;
    }
  }

  // run controls
  const busy = st.busy;
  const noProfile = !st.selected;
  const opRunning = busy || st.login.running || st.import.running || st.verify.running;
  $("#btnStart").disabled = opRunning || noProfile;
  $("#btnScan").disabled = opRunning || noProfile;
  $("#btnJoinAll").disabled = opRunning || noProfile;
  $("#btnCheckJoin").disabled = opRunning || noProfile;
  $("#btnSyncMyGroups").disabled = opRunning || noProfile;
  $("#btnBlast").disabled = opRunning || noProfile;
  $("#btnPagePost").disabled = opRunning || noProfile;
  $("#btnStop").disabled = !busy;
  const btnPause = $("#btnPause");
  btnPause.disabled = !busy;
  if (busy && st.paused) {
    btnPause.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> Resume';
    btnPause.className = "btn success";
  } else {
    btnPause.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg> Pause';
    btnPause.className = "btn outline";
  }
  $("#btnVerify").disabled = opRunning || noProfile;
  $("#btnImport").disabled = opRunning;

  const run = $("#runStatus");
  if (busy) {
    run.className = "run-status running";
    if (st.paused) {
      run.textContent = "Paused — press Resume to continue, or Stop to end.";
    } else {
      run.textContent = st.status_text
        || (st.job === "scan" ? "Scanning groups… (stop available)"
        : st.job === "join" ? "Auto-joining groups… (stop available)"
        : st.job === "check" ? "Checking join status… (stop available)"
        : `Posting… ${st.posts_today} posted today. Press Stop when done.`);
    }  } else if (st.import.running) {
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

  // Post-to-my-groups cycle progress
  const blastEl = document.getElementById("blastProgress");
  if (blastEl) {
    const bp = st.blast_progress;
    const bd = st.blast_done || [];
    const joinedCount = (st.group_counts && st.group_counts.joined) || 0;
    const covered = (bp && typeof bp.done === "number") ? bp.done : bd.length;
    const total = (bp && typeof bp.total === "number") ? bp.total : joinedCount;
    blastEl.classList.remove("done");
    if (st.job === "blast") {
      blastEl.textContent = `Posting this batch… ${covered} of ${total} of your groups covered this cycle.`;
    } else if (covered > 0) {
      blastEl.textContent = `This cycle: ${covered} of ${total} of your groups posted. Press Post Batch to continue to the next batch.`;
      blastEl.classList.add("done");
    } else {
      blastEl.textContent = "Cycle not started. Sync My Groups once, then press Post Batch.";
    }
  }

  // Post-to-my-page progress
  const ppEl = document.getElementById("pagePostProgress");
  if (ppEl) {
    ppEl.classList.remove("done");
    if (st.job === "page_post") {
      ppEl.textContent = st.status_text || "Posting to your Page…";
    } else if (st.stage_detail && /page/i.test(st.stage_detail || "") && st.status_text && /page/i.test(st.status_text || "")) {
      ppEl.textContent = st.status_text;
      ppEl.classList.add("done");
    } else {
      ppEl.textContent = "";
    }
  }

  // activity controls bar
  const acBar = $("#activityControls");
  const grpBar = $("#groupsRunControls");
  if (busy) {
    acBar.classList.remove("hidden");
    const pill = $("#acPill");
    pill.className = "pill " + (st.paused ? "idle" : "running");
    pill.textContent = st.paused ? "Paused" : (st.job === "scan" ? "Scanning" : st.job === "join" ? "Joining" : st.job === "check" ? "Checking" : "Running");
    $("#acText").textContent = st.status_text || "Working…";
    const acPause = $("#btnAcPause");
    if (st.paused) {
      acPause.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> Resume';
      acPause.className = "btn success small";
    } else {
      acPause.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg> Pause';
      acPause.className = "btn outline small";
    }
    // Groups-tab compact controls mirror the same state
    if (grpBar) {
      grpBar.classList.remove("hidden");
      $("#grpPill").className = "pill " + (st.paused ? "idle" : "running");
      $("#grpPill").textContent = st.paused ? "Paused" : "Working";
      $("#grpText").textContent = st.status_text || "Working…";
      const grpPause = $("#btnGrpPause");
      if (st.paused) {
        grpPause.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> Resume';
        grpPause.className = "btn success small";
      } else {
        grpPause.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg> Pause';
        grpPause.className = "btn outline small";
      }
    }
  } else {
    acBar.classList.add("hidden");
    if (grpBar) grpBar.classList.add("hidden");
  }

  renderRecent();
  maybeAutoVerify(st);

  // content composer — keep the live preview and account name fresh
  syncCaptionFromState();
  renderPostPreview();

  // auto-refresh media list when the media_count changes (user drops files)
  const mc = st.media_count;
  if (state.lastMediaCount !== mc) {
    state.lastMediaCount = mc;
    loadMedia(false);
  }

  // tab rendering
  renderTabs();
  renderStepper(st);

  // identity + Pages picker
  renderIdentity(st);
  renderPagesStatus(st);
  renderPagesList(st);
  renderPagePostPicker(st);

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
    const setupTxt = ps.setup_total ? ` · Setup: ${ps.ready_count}/${ps.setup_total}${ps.ready ? " ✓" : ""}` : "";
    $("#acctDetail").textContent = `Profile folder on disk: ${ps.dir_exists ? "found" : "missing"}` + extra + setupTxt;
  }
  $("#btnVerify .lbl").textContent = st.verify.running ? "Checking…" : "Check session";
  $("#btnImport .lbl").textContent = st.import.running ? "Importing…" : "Import from Chrome";
}

function renderAccountsOverview(st) {
  const box = $("#acctOverview");
  const profiles = st.profiles || [];
  const selName = st.selected ? st.selected.name : "";
  const readyN = profiles.filter((p) => p.status && p.status.ready).length;
  $("#acctSummary").textContent = profiles.length
    ? `${readyN} of ${profiles.length} account${profiles.length === 1 ? "" : "s"} ready` +
      (readyN === profiles.length ? " — all set up" : " — see what's missing below")
    : "Setup status at a glance";
  box.innerHTML = "";
  if (!profiles.length) {
    box.innerHTML = '<div class="recent-empty">No accounts yet. Add one to get started.</div>';
    return;
  }
  profiles.forEach((p) => {
    const ps = p.status || { label: "—", code: "", setup: [], ready_count: 0, setup_total: 0 };
    const row = document.createElement("div");
    row.className = "acct-row" + (p.name === selName ? " selected" : "");
    const checks = (ps.setup || []).map((c) => `
      <span class="check ${c.ok ? "ok" : "bad"}" title="${esc(c.label)} — ${esc(c.hint)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">${c.ok ? '<path d="M20 6 9 17l-5-5"/>' : '<path d="M18 6 6 18M6 6l12 12"/>'}</svg>
        <em>${esc(c.label)}</em>
      </span>`).join("");
    const meter = ps.setup_total
      ? Math.round((100 * (ps.ready_count || 0)) / ps.setup_total)
      : 0;
    row.innerHTML = `
      <label class="acct-sel" title="Use this account">
        <input type="radio" name="acctSel" value="${esc(p.name)}" ${p.name === selName ? "checked" : ""}>
        <span class="acct-name">${esc(p.name)}</span>
      </label>
      <span class="chip account-chip ${esc(ps.code || "")}">${esc(ps.label || "—")}</span>
      <div class="acct-meter ${ps.ready ? "ready" : ""}" title="Setup ${ps.ready_count || 0}/${ps.setup_total}">
        <span class="meter-fill" style="width:${meter}%"></span>
        <b>${ps.ready_count || 0}/${ps.setup_total}</b>
      </div>
      <div class="acct-checks">${checks}</div>
      <div class="acct-actions">
        <button class="btn outline mini" data-act="check" title="Re-check the Facebook session">Check</button>
        <button class="btn outline mini" data-act="relogin" title="Open a browser to log in again">Re-login</button>
        <button class="btn danger mini" data-act="remove" title="Remove this account">Remove</button>
      </div>`;
    row.querySelector(".acct-sel").addEventListener("click", () => selectAccount(p.name));
    row.querySelector('[data-act="check"]').addEventListener("click", async (e) => {
      e.stopPropagation();
      await selectAccount(p.name, true);
      verifySession(true);
    });
    row.querySelector('[data-act="relogin"]').addEventListener("click", async (e) => {
      e.stopPropagation();
      await selectAccount(p.name, true);
      reloginSelected();
    });
    row.querySelector('[data-act="remove"]').addEventListener("click", async (e) => {
      e.stopPropagation();
      await selectAccount(p.name, true);
      removeSelected();
    });
    box.appendChild(row);
  });
}

async function selectAccount(name, silent) {
  const sel = $("#profileSelect");
  if (sel.value !== name) {
    sel.value = name;
    state.lastAutoVerify = null;
    try {
      await api("/api/prefs", { method: "POST", body: { last_profile: name } });
    } catch (e) {}
  }
  if (!silent) toast("Selected account: " + name, "info");
}

function renderResumeBanner(st) {
  const el = $("#resumeBanner");
  const ij = st.interrupted_job;
  if (!ij || !ij.type || st.busy) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const label = ij.type === "join" ? "Auto-Join"
    : ij.type === "scan" ? "Group scan"
    : ij.type === "check" ? "Join-status check"
    : "Posting run";
  const p = ij.progress || {};
  const prog = p.total ? ` — ${p.done}/${p.total} processed` : "";
  $("#resumeText").textContent =
    `A ${label} on '${ij.profile || "your account"}' was interrupted${prog}. ` +
    `Resume continues where it stopped (already-processed groups are skipped).`;
}

function renderHardStopBanner(st) {
  const hs = st.hard_stop;
  if (!hs) return;
  // Show the full-screen notice only once per hard-stop instance, so it does
  // not re-open on every state refresh. Reappears after a fresh join run or
  // the next day.
  if (state.hardStopDismissedAt === hs.triggered_at) return;
  state.hardStopDismissedAt = hs.triggered_at;
  openHardStopModal(hs);
}

function openHardStopModal(hs) {
  const cap = hs.cap || 10;
  const joined = hs.joined || 0;
  $("#hardStopLead").textContent = hs.message || (
    `You reached the safe daily limit of ${cap} group joins today (${joined} so far). ` +
    `Auto-Join stopped here automatically so Facebook cannot flag your account ` +
    `for "joining groups too fast".`
  );
  $("#hardStopCapVal").textContent = cap;
  $("#hardStopJoinedVal").textContent = joined;
  openModal("modalHardStop");
}

function dismissHardStop() {
  closeModal("modalHardStop");
}

/* ---------------- tabs ---------------- */
function renderTabs() {
  const saved = localStorage.getItem("sm_tab") || "dashboard";
  if (!state.activeTab) state.activeTab = saved;

  $$(".sidebar-nav .tab-btn").forEach((btn) => {
    const isActive = btn.dataset.tab === state.activeTab;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", isActive);
  });
  $$(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.tab === state.activeTab);
  });

  if (state.activeTab === "history") loadHistory();
  if (state.activeTab === "posts") loadPosts();

  // keep the "Post" page scrape results fresh
  renderSync();

  // activity badge: reset when Activity tab is active
  const badgeAct = $("#badgeActivity");
  if (state.activeTab === "activity") {
    state.unseenLogCount = 0;
    if (badgeAct) { badgeAct.classList.add("hidden"); badgeAct.textContent = "0"; }
  } else if (state.unseenLogCount > 0 && badgeAct) {
    badgeAct.classList.remove("hidden");
    badgeAct.textContent = state.unseenLogCount > 99 ? "99+" : state.unseenLogCount;
  }
}

function switchTab(tabName) {
  state.activeTab = tabName;
  localStorage.setItem("sm_tab", tabName);
  renderTabs();
}

/* ---------------- "scrape my groups" results ---------------- */
let syncSig = "";
function renderSync() {
  const st = state.current;
  const sr = st && st.sync_result;
  const joined = (st && st.group_counts && st.group_counts.joined) || 0;
  const total = (st && st.group_counts && st.group_counts.total) || 0;
  const dev = !!(st && st.settings && st.settings.developer_mode);
  const sig = (sr && sr.found) + "|" + (sr && sr.at) + "|" + total + "|" + dev;
  const summary = $("#syncSummary");
  const body = $("#syncGroupsBody");
  const chip = $("#syncDevChip");
  if (chip) chip.hidden = !dev;
  if (!summary || !body) return;
  if (sig === syncSig && !summary.dataset.force) return;
  syncSig = sig;

  if (!sr) {
    summary.className = "sync-summary";
    summary.innerHTML = `<div class="recent-empty">No scrape yet. Press <strong>Scrape My Groups</strong> to find every group on your Facebook account.</div>`;
  } else {
    summary.className = "sync-summary has-result";
    summary.innerHTML =
      `<div class="sync-found"><span class="sync-found-num">${sr.found}</span>` +
      `<span>group(s) found in your Facebook account</span></div>` +
      `<div class="sync-meta">Scraped ${esc(sr.at || "")} &middot; ${joined} marked as your groups &middot; ${total} groups in the database${dev ? " &middot; Developer mode: full scrape" : ""}</div>`;
  }
  delete summary.dataset.force;

  if (!body) return;
  body.innerHTML = "";
  if (!sr || !sr.groups || !sr.groups.length) {
    body.innerHTML = '<tr><td colspan="2" class="empty-cell">Scrape your groups to see the full list here.</td></tr>';
    return;
  }
  // Enrich with member counts from the groups table when available.
  const byId = {};
  if (st && st.sync_result) {
    sr.groups.forEach((g) => { byId[g.id] = g; });
  }
  sr.groups.forEach((g) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.textContent = g.name || g.id;
    const numCell = document.createElement("td");
    numCell.className = "num";
    numCell.textContent = "";
    if (g.member_count != null) numCell.textContent = g.member_count;
    row.appendChild(nameCell);
    row.appendChild(numCell);
    body.appendChild(row);
  });
}

/* ---------------- journey stepper (server-side stage) ---------------- */
const STEP_DEFS = {
  join:  ["Launch Chrome", "Load targets", "Visit & Join", "Classify", "Finished"],
  scan:  ["Launch Chrome", "Search", "Collect", "Check safety", "Finished"],
  run:   ["Launch Chrome", "Scan", "Pick group", "Compose", "Publish", "Cooldown"],
  check: ["Launch Chrome", "Load targets", "Check membership", "Finished"],
};
const STEP_IDX = {
  join:  { launch: 0, prepare: 1, work: 2, classify: 3, done: 4 },
  scan:  { launch: 0, search: 1, collect: 2, check: 3, filter: 3, done: 4 },
  run:   { launch: 0, scan: 1, pick: 1, compose: 2, attach: 3, publish: 4, cooldown: 5, done: 5 },
  check: { launch: 0, prepare: 1, work: 2, done: 3 },
};

function renderStepper(st) {
  const container = $("#stepper");
  const empty = $("#stepperEmpty");
  const lastRes = $("#lastResult");
  const sub = $("#stepperSub");

  // busy: show live stepper from server-side stage
  if (st.busy && st.job) {
    empty.classList.add("hidden");
    lastRes.classList.add("hidden");
    container.innerHTML = "";

    const job = st.job;
    const baseSteps = STEP_DEFS[job] || STEP_DEFS.run;
    const baseMap = STEP_IDX[job] || STEP_IDX.run;
    const hasPage = !!st.active_page;
    // When the account operates as a Page, the very first thing every job
    // does is switch the session into that Page — reflect it as step 1.
    const steps = hasPage ? ["Switch to Page", ...baseSteps] : baseSteps;
    const map = {};
    if (hasPage) {
      map.page = 0;
      for (const k in baseMap) map[k] = (baseMap[k] ?? 0) + 1;
    } else {
      Object.assign(map, baseMap);
    }
    const current = map[st.stage] ?? 0;

    let detail = st.stage_detail || "";
    if (!detail && job === "join" && st.join_progress) {
      const jp = st.join_progress;
      if (jp.total) detail = `${jp.done}/${jp.total} processed`;
    }

    let html = '<div class="stepper-track">';
    steps.forEach((label, i) => {
      const isDone = i < current;
      const isActive = i === current;
      const cls = isDone ? "done" : isActive ? "active" : "pending";
      const icon = isDone
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
        : (isActive ? '<span class="pulse-dot"></span>' : `<span>${i + 1}</span>`);
      html += `
        <div class="step ${cls}">
          <div class="step-circle">${icon}</div>
          <div class="step-label">${esc(label)}</div>
          ${isActive && detail ? `<div class="step-detail">${esc(detail)}</div>` : ""}
        </div>
        ${i < steps.length - 1 ? '<div class="step-connector"></div>' : ""}
      `;
    });
    html += '</div>';
    container.innerHTML = html;
    sub.textContent = st.status_text || "Current run progress at a glance";
    return;
  }

  // idle: show last_result if available
  const lr = st.last_result;
  const posted = st.blast_result || [];
  const postedHtml = posted.length ? `
    <div class="lr-list-head">Groups posted in this cycle (${posted.length}):</div>
    <ul class="lr-list">
      ${posted.map(g => `
        <li>
          <span class="lr-list-name">${esc(g.name || g.id)}</span>
          ${g.member_count ? `<span class="lr-list-members">${esc(g.member_count.toLocaleString())} members</span>` : ""}
          ${g.post_url ? `<a class="lr-list-link" href="${esc(g.post_url)}" target="_blank" rel="noopener">View post ↗</a>` : ""}
          <span class="lr-list-time">${esc(fmtWhen(g.posted_at))}</span>
        </li>`).join("")}
    </ul>` : "";

  if (lr && lr.title) {
    container.innerHTML = "";
    empty.classList.add("hidden");
    lastRes.classList.remove("hidden");
    const okClass = lr.ok ? "ok" : "fail";
    const icon = lr.ok ? "✓" : "✕";
    lastRes.innerHTML = `
      <div class="lr-head">
        <div class="lr-icon ${okClass}">${icon}</div>
        <div class="lr-title">${esc(lr.title)}</div>
        <div class="lr-time">${esc(fmtWhen(lr.finished_at))}</div>
      </div>
      <div class="lr-summary">${esc(lr.summary || "")}</div>
      ${postedHtml}
    `;
    sub.textContent = "Last run result";
    return;
  }

  // persisted posted-groups list survives an app restart (last_result is in-memory)
  if (posted.length) {
    container.innerHTML = "";
    empty.classList.add("hidden");
    lastRes.classList.remove("hidden");
    lastRes.innerHTML = `<div class="lr-head">
        <div class="lr-icon ok">✓</div>
        <div class="lr-title">Posted to your groups</div>
        <div class="lr-time"></div>
      </div>${postedHtml}`;
    sub.textContent = "Last batch posting results";
    return;
  }

  // truly idle
  container.innerHTML = "";
  lastRes.classList.add("hidden");
  empty.classList.remove("hidden");
  sub.textContent = "Current run progress at a glance";
}

/* ---------------- completion toast ---------------- */
function showCompletionToast(st) {
  const lr = st.last_result;
  if (!lr) return;
  const title = lr.title || "Job finished";
  const body = lr.summary || "Completed";
  const container = $("#toastContainer");
  const el = document.createElement("div");
  el.className = "toast " + (lr.ok ? "success" : "error");
  el.innerHTML = `<strong>${esc(title)}</strong><br>${esc(body)}`;
  el.addEventListener("click", () => { teleport("activity"); el.remove(); });
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 200); }, 6000);
}

/* ---------------- copy logs ---------------- */
async function copyLogs() {
  const panel = $("#logPanel");
  const lines = Array.from(panel.querySelectorAll(".log-line")).map(l => l.textContent.trim());
  const text = lines.join("\n");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  const btn = $("#btnCopyLog");
  const original = btn.textContent;
  btn.textContent = "Copied!";
  btn.disabled = true;
  setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
}

async function resumeInterrupted() {
  try {
    const r = await api("/api/resume", { method: "POST", body: {} });
    if (r.ok) { toast("Resuming interrupted run…", "success"); teleport("activity"); }
    else toast(r.error || "Could not resume.", "error");
  } catch (e) { toast("Resume failed: " + e.message, "error"); }
}

async function discardInterrupted() {
  try {
    await api("/api/discard_interrupted", { method: "POST", body: {} });
    toast("Interrupted-run state cleared.", "success");
  } catch (e) { toast("Discard failed: " + e.message, "error"); }
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

/* ---------------- identity + Pages ---------------- */
function renderIdentity(st) {
  const activeName = st.active_page_name || "";
  const chip = $("#identityChip");
  if (chip) {
    if (activeName) {
      chip.textContent = activeName + " · Page";
      $("#identityChipBtn").title =
        `Operating as your Page "${activeName}" — all tasks run as it. Click to change.`;
    } else {
      chip.textContent = "profile";
      $("#identityChipBtn").title =
        "Operating as your profile — sync, posting, scanning and joining run as the account. Click to switch to a Page.";
    }
  }
  const dev = !!(st.settings && st.settings.developer_mode);
  const devChip = $("#devChipBtn");
  if (devChip) devChip.classList.toggle("hidden", !dev);
}

function renderPagesStatus(st) {
  const el = $("#pagesStatus");
  if (!el) return;
  const ps = st.pages_state || {};
  if (!st.selected) {
    el.innerHTML = "<span>Add an account to manage its Pages.</span>";
    return;
  }
  if (ps.running) {
    el.innerHTML =
      `<span class="pill running">Finding pages…</span> loading the Pages for <strong>${esc(st.selected.name)}</strong>`;
    return;
  }
  if (ps.result === "no_session") {
    el.innerHTML =
      `<span class="err">Not logged in — open the account and log in once, then find pages again.</span>`;
    return;
  }
  if (ps.result === "error") {
    el.innerHTML = `<span class="err">Could not find pages: ${esc(ps.message || "unknown error")}</span>`;
    return;
  }
  if (ps.result === "ok" && ps.message) {
    el.innerHTML = `<span class="ok">✓ ${esc(ps.message)}</span>`;
    return;
  }
  if (st.pages && st.pages.length) {
    el.innerHTML =
      `<span>${st.pages.length} Page(s) — pick one to operate as, or keep <strong>Profile</strong>.</span>`;
  } else {
    el.innerHTML =
      `<span>No Pages found yet for <strong>${esc(st.selected.name)}</strong>. Press <strong>Find my pages</strong>.</span>`;
  }
}

function renderPagesList(st) {
  const list = $("#pagesList");
  if (!list) return;
  const pages = st.pages || [];
  const active = st.active_page || "";
  const profileOpt = `
    <label class="page-opt po-profile ${active ? "" : "active"}">
      <input type="radio" name="pagePick" value="" ${active ? "" : "checked"}>
      <span class="po-name">My profile</span>
      <span class="po-url">Run tasks as the account itself</span>
      <span class="po-tag">PROFILE</span>
    </label>`;
  const pageOpts = pages.map((p) => `
    <label class="page-opt ${(p.url === active) ? "active" : ""}">
      <input type="radio" name="pagePick" value="${esc(p.url)}" ${(p.url === active) ? "checked" : ""}>
      <span class="po-name">${esc(p.name)}</span>
      <span class="po-url">${esc(p.url)}</span>
      <span class="po-tag">PAGE</span>
    </label>`).join("");
  list.innerHTML = pages.length
    ? profileOpt + pageOpts
    : `<div class="pages-empty">Press <strong>Find my pages</strong> (or re-check your session) to list the Pages this account manages.</div>`;
  $$("input[name=pagePick]", list).forEach((r) => {
    r.addEventListener("change", async () => {
      if (!r.checked) return;
      try {
        const res = await api("/api/pages/select", {
          method: "POST",
          body: { name: st.selected.name, page_url: r.value },
        });
        if (res.ok) {
          toast(r.value ? "Now operating as that Page." : "Now operating as your profile.", "success");
        } else {
          toast(res.error || "Could not switch identity.", "error");
        }
      } catch (e) { toast("Failed to switch identity: " + e.message, "error"); }
    });
  });
}

function renderPagePostPicker(st) {
  const sel = $("#pagePostSelect");
  if (!sel) return;
  const active = st.active_page || "";
  const activeName = st.active_page_name || "";
  const pages = st.pages || [];
  sel.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = activeName
    ? "Use active Page: " + activeName
    : "Paste a Page URL below…";
  sel.appendChild(blank);
  pages.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.url;
    o.textContent = p.name;
    sel.appendChild(o);
  });
  sel.onchange = () => {
    if (sel.value) $("#pagePostUrl").value = sel.value;
  };
  // When an active Page is set, prefill the URL input so "Post to Page"
  // targets it without typing.
  if (active && !$("#pagePostUrl").value.trim()) {
    const ap = pages.find((p) => p.url === active);
    if (ap) $("#pagePostUrl").value = ap.url;
  }
}

async function pagesRefresh() {
  const name = $("#profileSelect").value;
  if (!name) return toast("Select an account first.", "warn");
  try {
    const r = await api("/api/pages/refresh", { method: "POST", body: { name } });
    if (r.ok) toast("Finding your Pages…", "info");
    else toast(r.error || "Could not start.", "error");
  } catch (e) { toast("Failed: " + e.message, "error"); }
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

  // activity badge: count new lines when not on Activity tab
  if (state.activeTab !== "activity") {
    state.unseenLogCount += lines.length;
    const badge = $("#badgeActivity");
    if (badge) {
      badge.classList.remove("hidden");
      badge.textContent = state.unseenLogCount > 99 ? "99+" : state.unseenLogCount;
    }
  }

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
    caption: getCaption(),
    media_folder: $("#mediaFolder").value.trim(),
  };
}

function openComposerForMedia(folder) {
  clearComposerMedia();
  loadMedia(true);
}

async function startRun() {
  const body = fieldSnapshot();
  if (!body.profile) return toast("Add an account first.", "warn");
  try {
    const r = await api("/api/start", { method: "POST", body });
    if (r.ok) { toast("Posting started.", "success"); teleport("activity"); }
    else toast(r.error || "Could not start.", "error");
  } catch (e) { toast("Start failed: " + e.message, "error"); }
}

async function scanGroups() {
  const body = fieldSnapshot();
  if (!body.profile) return toast("Add an account first.", "warn");
  if (!body.keyword) toast("No keyword — will re-check unclassified groups only.", "warn");
  try {
    const r = await api("/api/scan", { method: "POST", body });
    if (r.ok) { toast("Scan started.", "success"); teleport("activity"); }
    else toast(r.error || "Could not start scan.", "error");
  } catch (e) { toast("Scan failed: " + e.message, "error"); }
}

async function joinAllGroups() {
  const profile = $("#profileSelect").value;
  if (!profile) return toast("Add an account first.", "warn");
  try {
    const r = await api("/api/join_all", { method: "POST", body: { profile } });
    if (r.ok) { toast("Auto-join started.", "success"); teleport("activity"); }
    else toast(r.error || "Could not start auto-join.", "error");
  } catch (e) { toast("Auto-join failed: " + e.message, "error"); }
}

async function checkJoinStatus() {
  const profile = $("#profileSelect").value;
  if (!profile) return toast("Add an account first.", "warn");
  try {
    const r = await api("/api/check_join", { method: "POST", body: { profile } });
    if (r.ok) { toast("Join-status check started.", "success"); teleport("activity"); }
    else toast(r.error || "Could not start check.", "error");
  } catch (e) { toast("Check failed: " + e.message, "error"); }
}

async function syncMyGroups() {
  const profile = $("#profileSelect").value;
  if (!profile) return toast("Add an account first.", "warn");
  try {
    const r = await api("/api/sync", { method: "POST", body: { profile } });
    if (r.ok) { toast("Syncing your full Facebook groups list…", "info"); teleport("activity"); }
    else toast(r.error || "Could not start sync.", "error");
  } catch (e) { toast("Sync failed: " + e.message, "error"); }
}

async function blastRun() {
  const profile = $("#profileSelect").value;
  if (!profile) return toast("Add an account first.", "warn");
  let batch = parseInt($("#blastBatch").value, 10);
  if (!batch || batch < 1) batch = 10;
  try {
    // Send the composed caption with this batch request (browser-held, not
    // stored on the backend). It is used for this run and then forgotten.
    const r = await api("/api/blast", { method: "POST", body: { profile, batch, caption: getCaption() } });
    if (r.ok) { toast(`Batch posting started (${batch} per press).`, "success"); teleport("activity"); }
    else toast(r.error || "Could not start batch posting.", "error");
  } catch (e) { toast("Batch failed: " + e.message, "error"); }
}

async function stopRun() {
  try {
    await api("/api/stop", { method: "POST", body: {} });
    toast("Stopping after the current post…", "warn");
  } catch (e) { toast("Stop failed: " + e.message, "error"); }
}

async function pagePostRun() {
  const profile = $("#profileSelect").value;
  const pageUrl = $("#pagePostUrl").value.trim();
  if (!profile) return toast("Add an account first.", "warn");
  // Empty URL is fine — the backend falls back to the account's active Page.
  try {
    const r = await api("/api/page_post", { method: "POST", body: { profile, page_url: pageUrl, caption: getCaption() } });
    if (r.ok) { toast("Page posting started.", "success"); teleport("activity"); }
    else toast(r.error || "Could not start page posting.", "error");
  } catch (e) { toast("Page post failed: " + e.message, "error"); }
}

async function pauseRun() {
  try {
    const st = state.current;
    if (st && st.paused) {
      const r = await api("/api/unpause", { method: "POST", body: {} });
      if (r.ok) toast("Resumed.", "success");
      else toast(r.error || "Could not resume.", "error");
    } else {
      const r = await api("/api/pause", { method: "POST", body: {} });
      if (r.ok) toast("Paused — will stop after current item.", "info");
      else toast(r.error || "Could not pause.", "error");
    }
  } catch (e) { toast("Pause failed: " + e.message, "error"); }
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
  $("#setDevMode").checked = !!s.developer_mode;
  $("#setPostableOnly").checked = s.postable_only ?? true;
  $("#setNicheOnly").checked = s.niche_only ?? true;
  $("#setNicheMaxMembers").value = s.niche_max_members ?? 150000;
  $("#setMaxIdle").value = s.max_group_idle_days ?? 21;
  $("#setJoinDelayMin").value = s.join_delay_min ?? 30;
  $("#setJoinDelayMax").value = s.join_delay_max ?? 90;
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
    developer_mode: $("#setDevMode").checked,
    postable_only: $("#setPostableOnly").checked,
    niche_only: $("#setNicheOnly").checked,
    niche_max_members: $("#setNicheMaxMembers").value,
    max_group_idle_days: $("#setMaxIdle").value,
    join_delay_min: $("#setJoinDelayMin").value,
    join_delay_max: $("#setJoinDelayMax").value,
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
  // Start from "This PC" (the drive list), not the C:\ root — let the user
  // pick any drive/folder from there.
  browsePath = "";
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
    $("#browseQuery").value = r.path || "";
    $("#btnBrowseUp").disabled = r.parent === null;
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
function browseGo() {
  const raw = ($("#browseQuery").value || "").trim();
  if (!raw) return;
  // Accept both slash styles; keep any trailing slash normalized.
  let p = raw.replace(/\//g, "\\");
  // Strip surrounding quotes if the user pasted a quoted path.
  p = p.replace(/^["']+|["']+$/g, "");
  if (!p) return;
  // If it's just a drive letter like "C" or "C:", make it a drive root.
  if (/^[a-zA-Z]:?$/.test(p)) p = p.charAt(0).toUpperCase() + ":\\";
  browsePath = p;
  loadBrowse();
}
function browseUp() {
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
    // persist the media folder to the backend, then refresh the composer media
    api("/api/settings", { method: "POST", body: { media_folder: browsePath } })
      .then(() => loadMedia(true))
      .catch(() => {});
  }
  closeModal("modalBrowse");
}

/* ---------------- groups ---------------- */
function switchGroupTab(status) {
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

const JOIN_STATUS_LABELS = {
  not_joined: "Not checked",
  pending: "Pending",
  joined: "Joined",
  declined: "Declined",
  unviewable: "Unviewable",
  unknown: "Unknown",
};
const JOIN_STATUS_COLORS = {
  not_joined: "muted",
  pending: "warn",
  joined: "ok",
  declined: "bad",
  unviewable: "muted",
  unknown: "muted",
};

function renderGroups(rows) {
  const body = $("#groupsBody");
  if (!rows.length) {
    const msg = state.groupStatus === "unknown"
      ? "No groups in review yet. Run a scan or Auto-Join — the app classifies each group automatically."
      : `No ${state.groupStatus === "safe" ? "safe" : "skipped"} groups yet. Groups are classified automatically when you scan or join.`;
    body.innerHTML = `<tr><td colspan="7" class="empty-cell" id="groupsEmpty">${msg}</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((g) => {
    const name = g.name || g.id;
    const js = g.join_status || "not_joined";
    const jsLabel = JOIN_STATUS_LABELS[js] || js;
    const jsColor = JOIN_STATUS_COLORS[js] || "muted";
    return `<tr data-id="${esc(g.id)}">
      <td><div class="gname" title="${esc(name)}">${esc(name)}</div><div class="gid">${esc(g.id)}</div></td>
      <td class="num mono">${fmtMembers(g.member_count)}</td>
      <td><span class="signal" title="${esc(g.approval_signal || "")}">${esc(g.approval_signal || "—")}</span></td>
      <td><span class="join-badge ${jsColor}" title="Last checked: ${esc(fmtWhen(g.join_checked_at))}">${jsLabel}</span></td>
      <td class="num mono">${g.times_posted ?? 0}</td>
      <td class="mono">${esc(fmtWhen(g.last_posted_at))}</td>
      <td class="actions">
        <span class="row-actions">
          ${state.groupStatus === "safe" ? `<button class="row-btn skip" data-act="mark-skip" title="Mark this group unsafe — never auto-post to it">Mark Unsafe</button>` : ""}
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
  if (!confirm("Delete this group from the database?")) return;
  try {
    await api("/api/group/delete", { method: "POST", body: { id } });
    groupsCache = {};
    loadGroups(state.groupStatus, true);
    toast("Group deleted.", "success");
  } catch (e) { toast("Delete failed: " + e.message, "error"); }
}

/* ---------------- history ---------------- */
const HISTORY_TYPE_LABELS = {
  join: "Auto-Join",
  check: "Join-status check",
  scan: "Group scan",
  run: "Posting run",
};
const HISTORY_STATUS_LABELS = {
  finished: "Finished",
  cancelled: "Cancelled",
  failed: "Failed",
  interrupted: "Left hanging",
};

async function loadHistory() {
  const body = $("#historyBody");
  try {
    const r = await api("/api/history");
    const rows = (r && r.history) || [];
    const sub = $("#historySub");
    if (sub) sub.textContent = rows.length
      ? `${rows.length} job(s) recorded — newest first.`
      : "Every job the app has run - finished, cancelled, or left hanging.";
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty-cell">No runs recorded yet. Run a scan, Auto-Join, or a join-status check.</td></tr>`;
      return;
    }
    body.innerHTML = rows.map((h) => {
      const type = HISTORY_TYPE_LABELS[h.type] || h.type;
      const stLabel = HISTORY_STATUS_LABELS[h.status] || h.status;
      const stClass = h.status === "finished" ? "ok"
        : h.status === "cancelled" ? "warn"
        : h.status === "failed" ? "bad"
        : h.status === "interrupted" ? "warn" : "muted";
      const prog = (typeof h.progress_done === "number" && typeof h.progress_total === "number")
        ? `${h.progress_done}/${h.progress_total}`
        : (typeof h.progress_done === "number" ? `${h.progress_done} done` : "—");
      const when = h.finished_at || h.started_at || "";
      const summary = h.summary || "";
      return `<tr>
        <td class="mono">${esc(fmtWhen(when))}</td>
        <td>${esc(type)}${h.profile ? ` <span class="gid">on ${esc(h.profile)}</span>` : ""}</td>
        <td><span class="join-badge ${stClass}" title="Status: ${esc(h.status)}">${esc(stLabel)}</span></td>
        <td class="num mono">${esc(prog)}</td>
        <td title="${esc(summary)}">${esc(summary)}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="5" class="empty-cell">Could not load history: ${esc(e.message)}</td></tr>`;
  }
}

/* ---------------- posts management ---------------- */
const POST_STATUS_LABELS = {
  posted: "Posted",
  pending: "Pending",
  failed: "Failed",
  error: "Error",
  draft: "Un-posted",
};
let lastPostPosts = [];

async function loadPosts() {
  const body = $("#postsBody");
  try {
    const r = await api("/api/posts?limit=200");
    lastPostPosts = (r && r.posts) || [];
    const rows = lastPostPosts;
    const sub = $("#postsSub");
    if (sub) sub.textContent = rows.length
      ? `${rows.length} post record(s) — newest first.`
      : "Every local record of a post the app made.";
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty-cell">No posts recorded yet. Post to groups to see them here.</td></tr>`;
      return;
    }
    body.innerHTML = rows.map((p) => {
      const priv = esc((p.file_path || "").split(/[\\/]/).pop() || "—");
      const gname = esc(p.group_name || p.group_id || "Unknown group");
      const status = p.status || "unknown";
      const stLabel = POST_STATUS_LABELS[status] || status;
      const stClass = status === "posted" ? "ok"
        : status === "failed" || status === "error" ? "bad"
        : status === "pending" ? "warn" : "muted";
      const url = p.post_url || "";
      const link = url
        ? `<a class="lr-list-link" href="${esc(url)}" target="_blank" rel="noopener">Open ↗</a>`
        : `<span class="gid">no link</span>`;
      const canToggle = (status === "posted") ? "mark un-posted" : "mark posted";
      return `<tr>
        <td class="mono">#${p.id}</td>
        <td class="mono">${esc(fmtWhen(p.created_at))}</td>
        <td title="${esc(p.group_id || "")}">${gname}</td>
        <td>${priv}</td>
        <td><span class="join-badge ${stClass}">${esc(stLabel)}</span></td>
        <td>${link}</td>
        <td>
          <button class="btn ghost small" data-post-set="${p.id}" title="Toggle posted/un-posted record used by today's counter">${canToggle}</button>
          <button class="btn ghost small danger" data-post-del="${p.id}" title="Remove this post record">Remove</button>
        </td>
      </tr>`;
    }).join("");
    $$("#postsBody [data-post-del]").forEach((b) =>
      b.addEventListener("click", () => deletePost(Number(b.dataset.postDel))));
    $$("#postsBody [data-post-set]").forEach((b) =>
      b.addEventListener("click", () => setPostStatus(Number(b.dataset.postSet), b)));
  } catch (e) {
    body.innerHTML = `<tr><td colspan="7" class="empty-cell">Could not load posts: ${esc(e.message)}</td></tr>`;
  }
}

async function deletePost(postId) {
  if (!confirm(`Remove post record #${postId}? This only edits local history — it will not delete the post from Facebook.`)) return;
  const r = await api("/api/post/delete", { method: "POST", body: { id: postId } });
  if (r && r.ok) { await loadPosts(); await loadState(); }
  else alert((r && r.error) || "Could not remove post.");
}

async function setPostStatus(postId, btn) {
  const p = lastPostPosts.find((x) => x.id === postId);
  if (!p) return;
  const to = (p.status === "posted") ? "draft" : "posted";
  const r = await api("/api/post/update", { method: "POST", body: { id: postId, status: to } });
  if (r && r.ok) { await loadPosts(); await loadState(); }
  else alert((r && r.error) || "Could not update post.");
}

/* ---------------- sidebar ---------------- */
function toggleSidebar() {
  const sb = $("#sidebar");
  const collapsed = sb.classList.toggle("collapsed");
  localStorage.setItem("sm_sidebar", collapsed ? "1" : "");
}

/* ---------------- wiring ---------------- */
function bind() {
  // sidebar
  const savedSidebar = localStorage.getItem("sm_sidebar");
  if (savedSidebar) $("#sidebar").classList.add("collapsed");
  $("#btnCollapse").addEventListener("click", toggleSidebar);

  // settings (both sidebar and old topbar button if present)
  $("#btnSettingsSidebar")?.addEventListener("click", openSettings);
  $("#btnSettings")?.addEventListener("click", openSettings);
  $("#btnSaveSettings")?.addEventListener("click", saveSettings);

  $("#btnStart").addEventListener("click", startRun);
  $("#btnStop").addEventListener("click", stopRun);
  $("#btnPause").addEventListener("click", pauseRun);
  $("#btnAcStop").addEventListener("click", stopRun);
  $("#btnAcPause").addEventListener("click", pauseRun);
  $("#btnGrpStop").addEventListener("click", stopRun);
  $("#btnGrpPause").addEventListener("click", pauseRun);
  $("#btnHardStopGotIt").addEventListener("click", dismissHardStop);
  $("#btnScan").addEventListener("click", scanGroups);
  $("#btnJoinAll").addEventListener("click", joinAllGroups);
  $("#btnCheckJoin").addEventListener("click", checkJoinStatus);
  $("#btnSyncMyGroups").addEventListener("click", syncMyGroups);
  $("#btnRefreshSync").addEventListener("click", () => { if ($("#syncSummary")) $("#syncSummary").dataset.force = "1"; renderSync(); });
  $("#btnBlast").addEventListener("click", blastRun);
  $("#blastBatch").addEventListener("keydown", (e) => { if (e.key === "Enter") blastRun(); });
  $("#btnPagePost").addEventListener("click", pagePostRun);
  $("#pagePostUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") pagePostRun(); });
  $("#btnResume").addEventListener("click", resumeInterrupted);
  $("#btnDiscard").addEventListener("click", discardInterrupted);

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
  $("#btnAddAccount2")?.addEventListener("click", () => { $("#addName").value = ""; openModal("modalAdd"); });
  $("#btnImport2")?.addEventListener("click", openImportModal);
  $("#btnManage2")?.addEventListener("click", () => { refreshAccountsModal(); openModal("modalAccounts"); });
  $("#btnAccountsAdd").addEventListener("click", () => {
    closeModal("modalAccounts");
    setTimeout(() => { $("#addName").value = ""; openModal("modalAdd"); }, 60);
  });

  $("#btnBrowseFolder").addEventListener("click", openBrowse);
  $("#btnBrowseUp").addEventListener("click", browseUp);
  $("#btnBrowseSelect").addEventListener("click", selectBrowse);
  $("#btnBrowseGo").addEventListener("click", browseGo);
  $("#browseQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") browseGo(); });

  // ---- content composer ----
  $("#btnRefreshMedia")?.addEventListener("click", () => loadMedia(true));
  $("#btnChangeFolder")?.addEventListener("click", openBrowse);
  $("#btnClearMedia")?.addEventListener("click", clearComposerMedia);

  $("#composerText")?.addEventListener("input", () => {
    persistCaption(getCaption());
    updateCaptionHint();
    composerSave();
    if ($("#captionInput").value !== $("#composerText").value) {
      $("#captionInput").value = $("#composerText").value;
    }
    renderPostPreview();
  });

  // Advanced settings caption stays in sync with the composer (single source)
  $("#captionInput")?.addEventListener("input", () => {
    if ($("#composerText").value !== $("#captionInput").value) {
      $("#composerText").value = $("#captionInput").value;
    }
    persistCaption(getCaption());
    updateCaptionHint();
    composerSave();
    renderPostPreview();
  });

  $$('input[name="composerMode"]').forEach((r) => {
    r.addEventListener("change", () => {
      if (!r.checked) return;
      state.mediaMode = r.value;
      if (state.mediaMode === "single" && state.selectedMedia.length > 1) {
        state.selectedMedia = state.selectedMedia.slice(0, 1);
      }
      composerSave();
      renderComposerMediaList();
      renderPostPreview();
    });
  });

  // restore the last draft + selection after binding
  if ($("#composerText")) {
    const saved = composerStorage();
    if (saved.text !== undefined) $("#composerText").value = saved.text;
    if (saved.mode === "multiple" || saved.mode === "single") state.mediaMode = saved.mode;
    const single = $("#composerMode" + (state.mediaMode === "multiple" ? "Multi" : "Single"));
    if (single) single.checked = true;
    if (Array.isArray(saved.selected)) {
      state.selectedMedia = saved.selected.filter((m) => m && m.path);
    }
    updateCaptionHint();
    renderPostPreview();
  }

  $("#btnRefreshGroups").addEventListener("click", () => { groupsCache = {}; loadGroups(state.groupStatus, true); });
  $("#btnRefreshHistory")?.addEventListener("click", loadHistory);
  $("#btnRefreshPosts")?.addEventListener("click", loadPosts);
  $$("#groupTabs .tab").forEach((t) => t.addEventListener("click", () => switchGroupTab(t.dataset.status)));

  // main tab navigation (sidebar nav)
  $$(".sidebar-nav .tab-btn").forEach((btn) => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

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

  $("#btnCopyLog").addEventListener("click", copyLogs);

  // "posted today" chip -> open today's posted posts list
  $("#postedChipBtn").addEventListener("click", openTodayPosts);

  // Pages tab picker + identity chips
  $("#btnRefreshPages")?.addEventListener("click", pagesRefresh);
  $("#identityChipBtn")?.addEventListener("click", () => switchTab("pages"));
  $("#devChipBtn")?.addEventListener("click", openSettings);

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
  loadGroups("all", true);
  loadMedia(false);   // populate the content composer's media picker
});
