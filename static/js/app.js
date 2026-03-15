"use strict";

/* ── State ─────────────────────────────────────────────────────────── */
const S = {
  file:           null,
  uploadResult:   null,
  binResult:      null,
  activeCols:     {},
  activeTab:      null,
  charts:         {},
  selectedBins:   {},
  lastClick:      {},
  pollTimer:      null,
  currentJobId:   null,       // kept for rebin calls
  binMode:        "equal_width",
};

const API = "";

const MODE_HINTS = {
  equal_width: "Splits the value range into equal-width intervals",
  monotonic:   "Equal-frequency bins + greedy merge to enforce monotone event rate",
};

/* ── Utilities ─────────────────────────────────────────────────────── */
function $(id) { return document.getElementById(id); }
function toast(msg, ms = 2500) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), ms);
}
function fmt(n, d = 2)  { return typeof n === "number" ? n.toFixed(d) : n; }
function fmtPct(n)       { return (n * 100).toFixed(2) + "%"; }
function fmtN(n)         { return n.toLocaleString(); }

/* ── Mode toggle (sidebar) ──────────────────────────────────────────── */
document.querySelectorAll(".mode-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".mode-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    S.binMode = btn.dataset.mode;
    $("modeHint").textContent = MODE_HINTS[S.binMode] || "";
  });
});

/* ── File upload ────────────────────────────────────────────────────── */
const dropZone  = $("dropZone");
const fileInput = $("fileInput");

dropZone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => handleFile(fileInput.files[0]));
dropZone.addEventListener("dragover",  e => { e.preventDefault(); dropZone.classList.add("drag-over"); });
dropZone.addEventListener("dragleave", ()  => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", e => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});

async function handleFile(file) {
  if (!file) return;
  S.file = file;
  dropZone.innerHTML = `<div class="drop-icon"><span class="spinner"></span></div><div class="drop-label">Uploading…</div>`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res  = await fetch(`${API}/api/upload`, { method: "POST", body: fd });
    const data = await res.json();
    if (data.error) { toast("Error: " + data.error, 4000); resetDrop(); return; }
    S.uploadResult = data;
    dropZone.innerHTML = `<div class="drop-icon" style="color:var(--green)">✓</div><div class="drop-label">${file.name}</div><div class="drop-sub">${fmtN(data.rows)} rows · ${data.columns.length} columns</div>`;
    $("uploadInfo").style.display   = "block";
    $("uploadInfo").textContent     = `${fmtN(data.rows)} rows, ${data.columns.length} columns`;
    buildConfig(data);
    $("panel-config").style.display = "block";
    markStep(2);
  } catch (e) { toast("Upload failed: " + e.message, 4000); resetDrop(); }
}

function resetDrop() {
  dropZone.innerHTML = `<div class="drop-icon">↑</div><div class="drop-label">Drop CSV / Excel here</div><div class="drop-sub">or click to browse</div>`;
}

/* ── Config panel ───────────────────────────────────────────────────── */
function buildConfig(data) {
  const sel = $("outcomeSelect");
  sel.innerHTML = `<option value="">— select —</option>`;
  data.columns.forEach(c => { sel.innerHTML += `<option value="${c.name}">${c.name}</option>`; });
  const binary = data.columns.find(c => c.nunique === 2);
  if (binary) sel.value = binary.name;
  buildColAssign(data.columns);
}

function buildColAssign(columns) {
  const div = $("colAssign");
  div.innerHTML = "";
  const outcome = $("outcomeSelect").value;
  columns.forEach(col => {
    if (col.name === outcome) return;
    const row = document.createElement("div");
    row.className  = "col-row";
    row.dataset.col = col.name;
    const opts    = [
      { v: "numeric",     l: "Numeric" },
      { v: "categorical", l: "Categorical" },
      { v: "ignore",      l: "Ignore" },
    ];
    const selOpts = opts.map(o => `<option value="${o.v}" ${col.guess === o.v ? "selected" : ""}>${o.l}</option>`).join("");
    row.innerHTML = `<div class="col-name" title="${col.name}">${col.name}</div><select class="col-type-sel">${selOpts}</select>`;
    div.appendChild(row);
  });
  $("outcomeSelect").addEventListener("change", () => buildColAssign(S.uploadResult.columns));
}

/* ── Progress bar ───────────────────────────────────────────────────── */
function showProgress(pct, completed, total, backend) {
  let wrap = $("progressWrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.id = "progressWrap";
    wrap.style.cssText = [
      "position:fixed;bottom:24px;left:50%;transform:translateX(-50%)",
      "background:#1a1a18;border:1px solid rgba(255,255,255,0.1)",
      "border-radius:10px;padding:16px 24px;min-width:360px",
      "box-shadow:0 8px 32px rgba(0,0,0,0.5);z-index:1000",
      "font-family:'DM Mono',monospace",
    ].join(";");
    wrap.innerHTML = `
      <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:11px;color:#9a9a94">
        <span id="progressLabel">Binning…</span>
        <span id="progressPct" style="color:#00A3A3">0%</span>
      </div>
      <div style="height:6px;background:rgba(255,255,255,0.07);border-radius:3px;overflow:hidden">
        <div id="progressBar" style="height:100%;width:0%;background:var(--green);border-radius:3px;transition:width 0.3s ease"></div>
      </div>
      <div id="progressSub" style="margin-top:8px;font-size:10px;color:#5a5a56"></div>
    `;
    document.body.appendChild(wrap);
  }
  $("progressBar").style.width   = pct + "%";
  $("progressPct").textContent   = pct + "%";
  $("progressLabel").textContent = `Binning columns… ${completed} / ${total}`;
  $("progressSub").textContent   = `Engine: ${backend}`;
}

function hideProgress() {
  const wrap = $("progressWrap");
  if (wrap) wrap.remove();
}

/* ── Run binning ─────────────────────────────────────────────────────── */
$("runBtn").addEventListener("click", runBinning);

async function runBinning() {
  const outcome = $("outcomeSelect").value;
  if (!outcome) { toast("Select an outcome variable"); return; }
  if (!S.file)  { toast("Upload a file first"); return; }

  const numeric = [], categorical = [], col_modes = {};
  document.querySelectorAll(".col-row").forEach(row => {
    const col  = row.dataset.col;
    const type = row.querySelector(".col-type-sel").value;
    if (type === "numeric")     numeric.push(col);
    if (type === "categorical") categorical.push(col);
    const activeBtn = row.querySelector(".col-mode-btn.active");
    if (activeBtn) col_modes[col] = activeBtn.dataset.mode;
  });
  if (numeric.length + categorical.length === 0) { toast("Select at least one feature column"); return; }

  const btn = $("runBtn");
  btn.disabled  = true;
  btn.innerHTML = `<span class="spinner"></span> Starting…`;

  const config = {
    outcome, numeric, categorical,
    max_bins:  parseInt($("maxBins").value)  || 20,
    min_pct:   parseFloat($("minPct").value) || 1,
    bin_mode:  S.binMode,
    col_modes,
  };

  const fd = new FormData();
  fd.append("file",   S.file);
  fd.append("config", JSON.stringify(config));

  try {
    const res  = await fetch(`${API}/api/bin`, { method: "POST", body: fd });
    const data = await res.json();
    if (data.error) { toast("Error: " + data.error, 5000); btn.disabled = false; btn.textContent = "Run binning →"; return; }

    S.currentJobId = data.job_id;
    btn.innerHTML  = `<span class="spinner"></span> Running…`;
    showProgress(0, 0, numeric.length + categorical.length, "starting…");
    pollJob(data.job_id, btn);
  } catch (e) {
    toast("Binning failed: " + e.message, 5000);
    btn.disabled    = false;
    btn.textContent = "Run binning →";
    hideProgress();
  }
}

/* ── Job polling ────────────────────────────────────────────────────── */
function pollJob(jobId, btn) {
  if (S.pollTimer) clearInterval(S.pollTimer);

  S.pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`${API}/api/job/${jobId}`);
      const job = await res.json();

      if (!job.status) {
        clearInterval(S.pollTimer);
        toast("Job not found", 4000);
        btn.disabled = false; btn.textContent = "Run binning →";
        hideProgress();
        return;
      }

      showProgress(job.pct, job.completed, job.total, `${job.backend} + ${job.io_backend}`);

      if (job.status === "error") {
        clearInterval(S.pollTimer);
        hideProgress();
        toast("Binning error: " + (job.error || "unknown"), 6000);
        btn.disabled = false; btn.textContent = "Run binning →";
        return;
      }

      if (job.status === "done") {
        clearInterval(S.pollTimer);
        hideProgress();

        S.binResult = {
          results:        job.results,
          total_events:   job.total_events,
          total_n:        job.total_n,
          avg_event_rate: job.avg_event_rate,
          outcome_col:    job.outcome_col,
        };
        S.activeCols = JSON.parse(JSON.stringify(job.results));
        Object.keys(S.activeCols).forEach(col => {
          S.selectedBins[col] = new Set();
          S.lastClick[col]    = -1;
        });

        renderResults(S.binResult);
        $("panel-export").style.display = "block";
        $("panel-model").style.display  = "block";
        buildModelVarList(job.results);
        markStep(3);
        btn.disabled    = false;
        btn.textContent = "Run binning →";

        const elapsed = job.finished_at && job.started_at
          ? ((new Date(job.finished_at) - new Date(job.started_at)) / 1000).toFixed(1) : "?";
        toast(`Done — ${Object.keys(job.results).length} columns in ${elapsed}s`);
      }
    } catch (e) { console.warn("Poll error (will retry):", e); }
  }, 500);
}

/* ── Render results ─────────────────────────────────────────────────── */
function renderResults(data) {
  const cols = Object.keys(data.results);
  if (!cols.length) return;

  $("emptyState").style.display  = "none";
  $("resultsArea").style.display = "block";

  $("resultsMeta").innerHTML = `
    <div class="meta-item">Rows<strong>${fmtN(data.total_n)}</strong></div>
    <div class="meta-item">Events<strong>${fmtN(data.total_events)}</strong></div>
    <div class="meta-item">Avg event rate<strong>${fmtPct(data.avg_event_rate)}</strong></div>
    <div class="meta-item">Outcome<strong>${data.outcome_col}</strong></div>
    <div class="meta-item">Features<strong>${cols.length}</strong></div>
  `;

  const tabsEl = $("colTabs");
  tabsEl.innerHTML = "";
  cols.forEach((col, i) => {
    const info = data.results[col];
    const tab  = document.createElement("div");
    tab.className   = "col-tab" + (i === 0 ? " active" : "");
    tab.dataset.col = col;
    tab.innerHTML   = `${col} <span class="iv-badge">${fmt(info.total_iv, 4)}</span>`;
    tab.addEventListener("click", () => switchTab(col));
    tabsEl.appendChild(tab);
  });

  S.activeTab = cols[0];
  renderColContent(cols[0]);
}

function switchTab(col) {
  document.querySelectorAll(".col-tab").forEach(t => t.classList.toggle("active", t.dataset.col === col));
  S.activeTab = col;
  // Show binning results, hide model area
  $("resultsArea").style.display = "block";
  $("modelArea").style.display   = "none";
  renderColContent(col);
}

/* ── Column panel ───────────────────────────────────────────────────── */
function renderColContent(col) {
  const info    = S.activeCols[col];
  const colEl   = $("colContent");
  const isNum   = info.type === "numeric";
  const mode    = info.bin_mode || "equal_width";
  const modeLabel = mode === "monotonic" ? "Monotonic" : "Equal width";
  const modeCls   = mode === "monotonic" ? "mode-mono" : "mode-eq";

  // Per-column mode switcher (numeric only)
  const modeSwitcher = isNum ? `
    <div class="mode-toggle mode-toggle-sm" id="colModeToggle-${col}">
      <button class="mode-btn ${mode === "equal_width" ? "active" : ""}"
              data-mode="equal_width" onclick="switchColMode('${col}', 'equal_width')">Equal width</button>
      <button class="mode-btn ${mode === "monotonic" ? "active" : ""}"
              data-mode="monotonic"  onclick="switchColMode('${col}', 'monotonic')">Monotonic</button>
    </div>
    <button class="btn-mono" id="rebinBtn-${col}" onclick="applyColMode('${col}')">Apply ⚡</button>
  ` : `<span class="bin-mode-badge" style="opacity:0.5">Categorical</span>`;

  colEl.innerHTML = `
    <div class="col-panel" id="cp-${col}">
      <div class="col-panel-title">${col}</div>
      <div class="col-mode-bar">
        ${modeSwitcher}
        <span class="mono-badge" id="monoBadge-${col}"></span>
      </div>
      <div class="col-panel-sub">Type: ${info.type} · Total IV: <span id="iv-${col}">${fmt(info.total_iv, 6)}</span></div>
      <div class="bin-layout">
        <div class="chart-card">
          <div class="chart-wrap" id="chartWrap-${col}">
            <canvas id="chart-${col}"></canvas>
          </div>
        </div>
        <div class="table-card">
          <div class="table-toolbar">
            <div class="table-toolbar-left">
              <span class="sel-info" id="selInfo-${col}">Click rows to select</span>
              <button class="btn-merge" id="mergeBtn-${col}" disabled onclick="mergeSelected('${col}')">Merge</button>
              <button class="btn-reset" onclick="resetCol('${col}')">Reset</button>
            </div>
          </div>
          <div id="tableWrap-${col}"></div>
        </div>
      </div>
    </div>
  `;

  refreshTable(col);
  refreshChart(col, S.binResult.avg_event_rate);
}

/* ── Per-column mode switcher ───────────────────────────────────────── */
// Track which mode is selected per column (before Apply is clicked)
const _pendingMode = {};

function switchColMode(col, mode) {
  _pendingMode[col] = mode;
  const toggle = $(`colModeToggle-${col}`);
  if (toggle) {
    toggle.querySelectorAll(".mode-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.mode === mode);
    });
  }
}

async function applyColMode(col) {
  if (!S.currentJobId) { toast("No active job — re-run binning"); return; }
  const mode = _pendingMode[col] || S.activeCols[col]?.bin_mode || "equal_width";

  const btn = $(`rebinBtn-${col}`);
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>`; }

  try {
    const res  = await fetch(`${API}/api/rebin`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        job_id:   S.currentJobId,
        col,
        bin_mode: mode,
        max_bins: parseInt($("maxBins").value)  || 20,
        min_pct:  parseFloat($("minPct").value) || 1,
      }),
    });
    const data = await res.json();
    if (data.error) {
      toast("Rebin error: " + data.error, 5000);
      if (btn) { btn.disabled = false; btn.textContent = "Apply ⚡"; }
      return;
    }

    S.activeCols[col]        = data.result;
    S.binResult.results[col] = data.result;
    S.selectedBins[col]      = new Set();
    S.lastClick[col]         = -1;

    document.querySelectorAll(".col-tab").forEach(t => {
      if (t.dataset.col === col)
        t.querySelector(".iv-badge").textContent = fmt(data.result.total_iv, 4);
    });

    renderColContent(col);
    toast(`${col} → ${mode === "monotonic" ? "monotonic" : "equal width"} (IV ${fmt(data.result.total_iv, 4)})`);
  } catch (e) {
    toast("Rebin failed: " + e.message);
    if (btn) { btn.disabled = false; btn.textContent = "Apply ⚡"; }
  }
}

/* ── Table ──────────────────────────────────────────────────────────── */
function refreshTable(col) {
  const info  = S.activeCols[col];
  const bins  = info.bins;
  const sel   = S.selectedBins[col];
  const maxN  = Math.max(...bins.map(b => b.n));
  const wrap  = $(`tableWrap-${col}`);
  const isNum = info.type === "numeric";

  const head = `<tr><th>#</th><th>${isNum ? "Range" : "Category"}</th><th>N</th><th>Events</th><th>Ev rate</th><th>WoE</th><th>IV</th></tr>`;

  const rows = bins.map((b, i) => {
    const label    = isNum ? `[${fmt(b.min, 2)}, ${fmt(b.max, 2)}]` : b.label;
    const barPct   = maxN > 0 ? Math.round(b.n / maxN * 100) : 0;
    const woeClass = b.woe >= 0 ? "woe-pos" : "woe-neg";
    const selClass = sel.has(i) ? " selected" : "";
    return `
      <tr class="selectable${selClass}" data-i="${i}" onclick="selectBinRow('${col}', ${i}, event)">
        <td>${i + 1}</td>
        <td class="bar-cell">
          <div>${label}</div>
          <div class="mini-bar-wrap"><div class="mini-bar-fill" style="width:${barPct}%"></div></div>
        </td>
        <td>${fmtN(b.n)}</td><td>${fmtN(b.events)}</td><td>${fmtPct(b.event_rate)}</td>
        <td class="${woeClass}">${fmt(b.woe, 4)}</td><td>${fmt(b.iv, 6)}</td>
      </tr>`;
  }).join("");

  wrap.innerHTML = `<table class="bin-table"><thead>${head}</thead><tbody>${rows}</tbody></table>`;

  // Monotone check badge
  const rates = bins.map(b => b.event_rate);
  const inc   = rates.every((r, i) => i === 0 || r >= rates[i-1]);
  const dec   = rates.every((r, i) => i === 0 || r <= rates[i-1]);
  const badge = $(`monoBadge-${col}`);
  if (badge) {
    badge.className   = "mono-badge " + (inc || dec ? "mono-ok" : "mono-bad");
    badge.textContent = inc || dec ? "Monotonic ✓" : "Not monotonic";
  }
  updateActionBar(col);
}

/* ── Chart ──────────────────────────────────────────────────────────── */
function refreshChart(col, avgRate) {
  const info  = S.activeCols[col];
  const bins  = info.bins;
  const isNum = info.type === "numeric";

  const labels  = bins.map(b => isNum ? `[${fmt(b.min,1)}, ${fmt(b.max,1)}]` : b.label);
  const counts  = bins.map(b => b.n);
  const evRates = bins.map(b => +(b.event_rate * 100).toFixed(4));
  const avgLine = bins.map(() => +(avgRate * 100).toFixed(4));

  if (S.charts[col]) S.charts[col].destroy();

  const h    = Math.min(400, Math.max(220, bins.length * 28 + 60));
  const wrap = $(`chartWrap-${col}`);
  wrap.style.height = h + "px";

  S.charts[col] = new Chart($(`chart-${col}`), {
    data: { labels, datasets: [
      { type:"bar",  label:"Count",          data:counts,  backgroundColor:"rgba(200,240,96,0.20)", borderColor:"rgba(200,240,96,0.55)", borderWidth:1, borderRadius:3, yAxisID:"yCount", order:2 },
      { type:"line", label:"Event rate %",   data:evRates, borderColor:"#4d9de0", backgroundColor:"rgba(77,157,224,0.08)", borderWidth:2, pointRadius:4, pointBackgroundColor:"#4d9de0", tension:0.25, yAxisID:"yRate", order:1 },
      { type:"line", label:"Avg event rate", data:avgLine, borderColor:"#e0a84d", borderWidth:1.5, borderDash:[5,4], pointRadius:0, tension:0, yAxisID:"yRate", order:0 },
    ]},
    options: {
      responsive:true, maintainAspectRatio:false,
      interaction:{ mode:"index", intersect:false },
      plugins:{
        legend:{ display:false },
        tooltip:{
          backgroundColor:"#222220", borderColor:"rgba(255,255,255,0.1)", borderWidth:1,
          titleColor:"#e8e8e4", bodyColor:"#9a9a94",
          titleFont:{ family:"'DM Mono',monospace", size:11 },
          bodyFont: { family:"'DM Mono',monospace", size:11 },
          callbacks:{ label: ctx => {
            if (ctx.datasetIndex===0) return ` Count: ${fmtN(ctx.parsed.y)}`;
            if (ctx.datasetIndex===1) return ` Event rate: ${ctx.parsed.y.toFixed(2)}%`;
            return ` Avg rate: ${ctx.parsed.y.toFixed(2)}%`;
          }},
        },
      },
      scales:{
        x:{ ticks:{ color:"#5a5a56", font:{ family:"'DM Mono',monospace", size:10 }, maxRotation:40 }, grid:{ color:"rgba(255,255,255,0.04)" }},
        yCount:{ position:"left",  ticks:{ color:"#5a5a56", font:{ family:"'DM Mono',monospace", size:10 }}, grid:{ color:"rgba(255,255,255,0.04)" }, title:{ display:true, text:"Count",         color:"#5a5a56", font:{size:10} }},
        yRate: { position:"right", ticks:{ color:"#4d9de0", font:{ family:"'DM Mono',monospace", size:10 }, callback: v => v.toFixed(1)+"%"}, grid:{ drawOnChartArea:false }, title:{ display:true, text:"Event rate %", color:"#4d9de0", font:{size:10} }},
      },
    },
  });
}

function renderLegend(col) {
  const wrap = $(`chartWrap-${col}`);
  if (!wrap) return;
  const existing = wrap.querySelector(".chart-legend");
  if (existing) existing.remove();
  const leg = document.createElement("div");
  leg.className = "chart-legend";
  leg.style.cssText = "display:flex;gap:16px;margin-top:8px;font-size:10px;font-family:DM Mono,monospace;color:#8496AC;flex-wrap:wrap";
  leg.innerHTML = `
    <span style="display:flex;align-items:center;gap:5px"><span style="width:10px;height:10px;border-radius:2px;background:rgba(200,240,96,0.4)"></span>Count</span>
    <span style="display:flex;align-items:center;gap:5px"><span style="width:16px;height:2px;background:#4d9de0;display:inline-block"></span>Event rate</span>
    <span style="display:flex;align-items:center;gap:5px"><span style="width:16px;height:2px;border-top:2px dashed #e0a84d;display:inline-block"></span>Portfolio avg</span>
  `;
  wrap.appendChild(leg);
}

/* ── Selection ──────────────────────────────────────────────────────── */
function selectBinRow(col, i, e) {
  const sel  = S.selectedBins[col];
  const last = S.lastClick[col];
  if (e.shiftKey && last >= 0) {
    const lo = Math.min(i, last), hi = Math.max(i, last);
    sel.clear();
    for (let j = lo; j <= hi; j++) sel.add(j);
  } else if (e.ctrlKey || e.metaKey) {
    if (sel.has(i)) sel.delete(i); else sel.add(i);
  } else {
    const wasOnly = sel.has(i) && sel.size === 1;
    sel.clear();
    if (!wasOnly) sel.add(i);
  }
  S.lastClick[col] = i;
  refreshTable(col);
}

function updateActionBar(col) {
  const sel      = S.selectedBins[col];
  const info     = $(`selInfo-${col}`);
  const mergeBtn = $(`mergeBtn-${col}`);
  if (!info || !mergeBtn) return;
  if (sel.size === 0) { info.textContent = "Click rows to select"; mergeBtn.disabled = true;  return; }
  if (sel.size === 1) { info.textContent = "1 bin selected";       mergeBtn.disabled = true;  return; }
  const sorted     = [...sel].sort((a, b) => a - b);
  const contiguous = sorted.every((v, i) => i === 0 || v === sorted[i-1] + 1);
  info.textContent  = `${sel.size} selected${contiguous ? "" : " (non-contiguous)"}`;
  mergeBtn.disabled = !contiguous;
}

/* ── Merge ──────────────────────────────────────────────────────────── */
async function mergeSelected(col) {
  const sel     = S.selectedBins[col];
  const indices = [...sel].sort((a, b) => a - b);
  if (indices.length < 2) return;
  const info = S.activeCols[col];
  const te   = S.binResult.total_events;
  const tne  = S.binResult.total_n - te;
  try {
    const res  = await fetch(`${API}/api/merge`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ bins:info.bins, indices, type:info.type, total_events:te, total_non_events:tne }),
    });
    const data = await res.json();
    if (data.error) { toast("Merge error: " + data.error); return; }
    info.bins = data.bins; info.total_iv = data.total_iv;
    const ivEl = $(`iv-${col}`);
    if (ivEl) ivEl.textContent = fmt(data.total_iv, 6);
    document.querySelectorAll(".col-tab").forEach(t => {
      if (t.dataset.col === col) t.querySelector(".iv-badge").textContent = fmt(data.total_iv, 4);
    });
    S.selectedBins[col].clear(); S.lastClick[col] = -1;
    refreshTable(col); refreshChart(col, S.binResult.avg_event_rate); renderLegend(col);
  } catch (e) { toast("Merge failed: " + e.message); }
}

/* ── Reset ──────────────────────────────────────────────────────────── */
function resetCol(col) {
  S.activeCols[col]   = JSON.parse(JSON.stringify(S.binResult.results[col]));
  S.selectedBins[col] = new Set();
  S.lastClick[col]    = -1;
  const ivEl = $(`iv-${col}`);
  if (ivEl) ivEl.textContent = fmt(S.activeCols[col].total_iv, 6);
  document.querySelectorAll(".col-tab").forEach(t => {
    if (t.dataset.col === col) t.querySelector(".iv-badge").textContent = fmt(S.activeCols[col].total_iv, 4);
  });
  renderColContent(col);
}

/* ── Export ─────────────────────────────────────────────────────────── */
$("exportCodeBtn").addEventListener("click", async () => {
  if (!S.activeCols) { toast("Run binning first"); return; }
  try {
    const res  = await fetch(`${API}/api/export_code`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ results:S.activeCols, outcome_col:S.binResult.outcome_col }),
    });
    const data = await res.json();
    if (data.error) { toast("Export error: " + data.error); return; }
    $("codeOutput").textContent  = data.code;
    $("codeModal").style.display = "flex";
  } catch (e) { toast("Export failed: " + e.message); }
});

$("exportCsvBtn").addEventListener("click", () => {
  if (!S.activeCols) { toast("Run binning first"); return; }
  const rows = [["feature","type","bin_mode","bin","label_or_range","n","events","non_events","event_rate","woe","iv","total_iv"]];
  Object.entries(S.activeCols).forEach(([col, info]) => {
    info.bins.forEach((b, i) => {
      const label = info.type === "numeric" ? `[${fmt(b.min,2)}, ${fmt(b.max,2)}]` : b.label;
      rows.push([col, info.type, info.bin_mode || "", i+1, label, b.n, b.events, b.non_events,
                 fmt(b.event_rate,6), fmt(b.woe,6), fmt(b.iv,6), fmt(info.total_iv,6)]);
    });
  });
  const csv  = rows.map(r => r.map(v => `"${v}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type:"text/csv" });
  const a    = document.createElement("a");
  a.href     = URL.createObjectURL(blob);
  a.download = "woe_bins.csv";
  a.click();
  URL.revokeObjectURL(a.href);
  toast("CSV downloaded");
});

/* ── Excel export ───────────────────────────────────────────────────── */
$("exportExcelBtn").addEventListener("click", async () => {
  if (!S.activeCols) { toast("Run binning first"); return; }
  const btn = $("exportExcelBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> Building Excel…`;
  try {
    const res = await fetch(`${API}/api/export_excel`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ results: S.activeCols, outcome_col: S.binResult.outcome_col }),
    });
    if (!res.ok) {
      const err = await res.json();
      toast("Export error: " + (err.error || res.statusText), 5000);
      return;
    }
    const blob = await res.blob();
    const a    = document.createElement("a");
    a.href     = URL.createObjectURL(blob);
    a.download = "woe_binning.xlsx";
    a.click();
    URL.revokeObjectURL(a.href);
    toast("Excel downloaded");
  } catch (e) {
    toast("Excel export failed: " + e.message);
  } finally {
    btn.disabled    = false;
    btn.textContent = "Export WoE + charts (Excel)";
  }
});

/* ── Code modal ─────────────────────────────────────────────────────── */
function closeModal() { $("codeModal").style.display = "none"; }
$("codeModal").addEventListener("click", e => { if (e.target === $("codeModal")) closeModal(); });

async function copyCode() {
  try { await navigator.clipboard.writeText($("codeOutput").textContent); toast("Copied to clipboard"); }
  catch { toast("Copy failed — use Ctrl+A in the box"); }
}

function downloadCode() {
  const blob = new Blob([$("codeOutput").textContent], { type:"text/x-python" });
  const a    = document.createElement("a");
  a.href     = URL.createObjectURL(blob);
  a.download = "woe_transformer.py";
  a.click();
  URL.revokeObjectURL(a.href);
  toast("Downloaded woe_transformer.py");
}

/* ── Step indicators ────────────────────────────────────────────────── */
function markStep(n) {
  document.querySelectorAll(".step-dot").forEach(el => {
    const s = parseInt(el.dataset.step);
    if (s < n)  { el.classList.add("done");   el.classList.remove("active"); el.textContent = "✓"; }
    if (s === n) { el.classList.add("active"); el.classList.remove("done"); }
    if (s > n)  { el.classList.remove("active", "done"); }
  });
}

document.addEventListener("click", e => {
  const tab = e.target.closest(".col-tab");
  if (tab) setTimeout(() => renderLegend(tab.dataset.col), 50);
});

/* ── MODEL TAB ───────────────────────────────────────────────────── */

// ── Mode toggle (manual / auto-tune) ─────────────────────────────
document.querySelectorAll("#modelTuneToggle .mode-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#modelTuneToggle .mode-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    $("modelTuneHint").textContent = btn.dataset.tune === "auto"
      ? "Alpha will be found by cross-validation (slower)"
      : "Use alpha and L1 ratio as set above";
    $("modelAlpha").closest(".config-field").style.opacity = btn.dataset.tune === "auto" ? "0.35" : "1";
  });
});

// ── Build variable selector after binning ─────────────────────────
function buildModelVarList(results) {
  const div = $("modelVarList");
  div.innerHTML = "";
  Object.keys(results).forEach(col => {
    const info = results[col];
    const row  = document.createElement("div");
    row.className = "model-var-row";
    row.innerHTML = `
      <input type="checkbox" checked id="mvar_${col}" data-col="${col}">
      <label for="mvar_${col}" style="flex:1;cursor:pointer">${col}</label>
      <span style="font-size:10px;color:var(--text-3)">IV ${fmt(info.total_iv,4)}</span>
    `;
    div.appendChild(row);
  });
  updateModelVarCount();
}

function selectAllModelVars(checked) {
  document.querySelectorAll("#modelVarList input[type=checkbox]").forEach(cb => cb.checked = checked);
  updateModelVarCount();
}

function updateModelVarCount() {
  const total   = document.querySelectorAll("#modelVarList input[type=checkbox]").length;
  const checked = document.querySelectorAll("#modelVarList input[type=checkbox]:checked").length;
  $("modelVarCount").textContent = `(${checked}/${total})`;
}

document.addEventListener("change", e => {
  if (e.target.closest("#modelVarList")) updateModelVarCount();
});

// ── Fit button ────────────────────────────────────────────────────
$("modelFitBtn").addEventListener("click", fitModel);

async function fitModel() {
  if (!S.currentJobId) { toast("Run binning first"); return; }

  const selected = [...document.querySelectorAll("#modelVarList input[type=checkbox]:checked")]
    .map(cb => cb.dataset.col);
  if (selected.length === 0) { toast("Select at least one variable"); return; }

  const autoTune = document.querySelector("#modelTuneToggle .mode-btn.active")?.dataset.tune === "auto";

  const config = {
    selected_cols: selected,
    alpha:      parseFloat($("modelAlpha").value)    || 1.0,
    l1_ratio:   parseFloat($("modelL1Ratio").value)  || 0.5,
    cv_folds:   parseInt($("modelCvFolds").value)    || 5,
    auto_tune:  autoTune,
    base_score: parseInt($("modelBaseScore").value)  || 1500,
    pdo:        parseInt($("modelPdo").value)        || 20,
    score_min:  parseInt($("modelScoreMin").value)   || 1001,
    score_max:  parseInt($("modelScoreMax").value)   || 1999,
  };

  const btn = $("modelFitBtn");
  btn.disabled  = true;
  btn.innerHTML = `<span class="spinner"></span> Fitting…`;

  try {
    const res  = await fetch(`${API}/api/model/fit`, {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ bin_job_id: S.currentJobId, config }),
    });
    const data = await res.json();
    if (data.error) { toast("Model error: " + data.error, 5000); btn.disabled=false; btn.textContent="Fit model →"; return; }

    pollModelJob(data.model_job_id, btn);
  } catch(e) {
    toast("Fit failed: " + e.message);
    btn.disabled = false; btn.textContent = "Fit model →";
  }
}

function pollModelJob(modelJobId, btn) {
  const timer = setInterval(async () => {
    try {
      const res = await fetch(`${API}/api/model/status/${modelJobId}`);
      const job = await res.json();

      if (btn) {
        btn.innerHTML = `<span class="spinner"></span> ${job.message || "Running…"} ${job.progress||0}%`;
      }

      if (job.status === "error") {
        clearInterval(timer);
        toast("Model failed: " + (job.error||"").split("\n")[0], 6000);
        if(btn){ btn.disabled=false; btn.textContent="Fit model →"; }
        return;
      }

      if (job.status === "done") {
        clearInterval(timer);
        if(btn){ btn.disabled=false; btn.textContent="Fit model →"; }
        renderModelResults(job.result);
        toast(`Model fitted — Gini ${job.result.gini.toFixed(1)}%  KS ${job.result.ks.toFixed(1)}%`);
      }
    } catch(e) { console.warn("Model poll error:", e); }
  }, 800);
}

// ── Render model results ──────────────────────────────────────────
let _modelCharts = {};

function renderModelResults(r) {
  $("resultsArea").style.display   = "none";
  $("modelArea").style.display     = "block";

  // Meta bar
  $("modelMeta").innerHTML = `
    <div class="meta-item">Gini (in-sample)<strong style="color:var(--accent)">${r.gini.toFixed(1)}%</strong></div>
    <div class="meta-item">CV Gini<strong>${r.cv_gini_mean.toFixed(1)}% ±${r.cv_gini_std.toFixed(1)}</strong></div>
    <div class="meta-item">KS<strong>${r.ks.toFixed(1)}%</strong></div>
    <div class="meta-item">Variables in model<strong>${r.n_in_model}/${r.selected_cols.length}</strong></div>
    <div class="meta-item">Records<strong>${fmtN(r.n_records)}</strong></div>
    <div class="meta-item">Score range<strong>${r.score_min}–${r.score_max}</strong></div>
    <div class="meta-item">PDO<strong>${r.pdo}</strong></div>
  `;

  // Wire model sub-tabs
  document.querySelectorAll("#modelTabs .col-tab").forEach(t => {
    t.onclick = () => {
      document.querySelectorAll("#modelTabs .col-tab").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      renderModelTab(t.dataset.mtab, r);
    };
  });

  renderModelTab("overview", r);
}

function renderModelTab(tab, r) {
  // Destroy old charts
  Object.values(_modelCharts).forEach(c => c && c.destroy && c.destroy());
  _modelCharts = {};

  const el = $("modelContent");

  if (tab === "overview")     renderModelOverview(el, r);
  if (tab === "performance")  renderModelPerf(el, r);
  if (tab === "calibration")  renderModelCalib(el, r);
  if (tab === "coefficients") renderModelCoef(el, r);
}

// ── Overview: metric cards + ROC + KS ────────────────────────────
function renderModelOverview(el, r) {
  el.innerHTML = `
    <div class="model-cards">
      <div class="model-card">
        <div class="model-card-label">Gini (in-sample)</div>
        <div class="model-card-value">${r.gini.toFixed(1)}%</div>
        <div class="model-card-sub">AUC = ${((r.gini/100+1)/2).toFixed(4)}</div>
      </div>
      <div class="model-card">
        <div class="model-card-label">CV Gini (${r.cv_folds}-fold)</div>
        <div class="model-card-value">${r.cv_gini_mean.toFixed(1)}%</div>
        <div class="model-card-sub">±${r.cv_gini_std.toFixed(1)}%
          <div class="fold-badges">${r.cv_gini_folds.map(g=>`<span class="fold-badge">${g}%</span>`).join("")}</div>
        </div>
      </div>
      <div class="model-card">
        <div class="model-card-label">KS Statistic</div>
        <div class="model-card-value">${r.ks.toFixed(1)}%</div>
        <div class="model-card-sub">CV KS ${r.cv_ks_mean.toFixed(1)}%</div>
      </div>
      <div class="model-card">
        <div class="model-card-label">Regularisation</div>
        <div class="model-card-value" style="font-size:14px">${r.cv_results.method}</div>
        <div class="model-card-sub">α=${r.cv_results.best_alpha.toFixed(4)}  L1=${r.cv_results.l1_ratio}</div>
      </div>
    </div>
    <div class="model-chart-grid">
      <div class="model-chart-card">
        <div class="model-chart-title">ROC Curve (Gini = ${r.gini.toFixed(1)}%)</div>
        <div class="model-chart-wrap"><canvas id="chartRoc"></canvas></div>
      </div>
      <div class="model-chart-card">
        <div class="model-chart-title">Cumulative Event Capture (KS = ${r.ks.toFixed(1)}%)</div>
        <div class="model-chart-wrap"><canvas id="chartKs"></canvas></div>
      </div>
    </div>
  `;

  // ROC chart
  const fpr = r.roc_curve.fpr;
  const tpr = r.roc_curve.tpr;
  _modelCharts.roc = new Chart($("chartRoc"), {
    type: "line",
    data: {
      labels: fpr.map(v => v.toFixed(3)),
      datasets: [
        { label: "ROC", data: tpr, borderColor: "#FFFFFF", borderWidth: 2,
          pointRadius: 0, fill: false, tension: 0 },
        { label: "Random", data: fpr, borderColor: "rgba(255,255,255,0.35)", borderWidth: 1,
          borderDash: [4,4], pointRadius: 0, fill: false, tension: 0 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: true, labels: { color: "#FFFFFF", font: { size: 11 } } } },
      scales: {
        x: { title: { display: true, text: "False Positive Rate", color: "#FFFFFF" },
             ticks: { color: "#FFFFFF", maxTicksLimit: 6 }, grid: { color: "rgba(255,255,255,0.12)" } },
        y: { title: { display: true, text: "True Positive Rate", color: "#FFFFFF" },
             ticks: { color: "#FFFFFF" }, grid: { color: "rgba(255,255,255,0.12)" } },
      }
    }
  });

  // KS / Cumulative capture chart
  const bands = r.perf_table;
  const pctPop    = bands.map(b => b.pct_of_total * 100);
  const pctEvents = bands.map(b => b.pct_events_captured * 100);
  const random    = bands.map(b => b.pct_of_total * 100);
  _modelCharts.ks = new Chart($("chartKs"), {
    type: "line",
    data: {
      labels: pctPop.map(v => v.toFixed(0) + "%"),
      datasets: [
        { label: "Model", data: pctEvents, borderColor: "#FFFFFF", borderWidth: 2,
          pointRadius: 0, fill: false, tension: 0 },
        { label: "Random", data: random, borderColor: "rgba(255,255,255,0.35)", borderWidth: 1,
          borderDash: [4,4], pointRadius: 0, fill: false, tension: 0 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: true, labels: { color: "#FFFFFF", font: { size: 11 } } } },
      scales: {
        x: { title: { display: true, text: "% Population (high score first)", color: "#FFFFFF" },
             ticks: { color: "#FFFFFF", maxTicksLimit: 6 }, grid: { color: "rgba(255,255,255,0.12)" } },
        y: { title: { display: true, text: "% Events Captured", color: "#FFFFFF" },
             ticks: { color: "#FFFFFF" }, grid: { color: "rgba(255,255,255,0.12)" } },
      }
    }
  });
}

// ── Performance table ─────────────────────────────────────────────
function renderModelPerf(el, r) {
  const rows = r.perf_table.map((b, i) => `
    <tr>
      <td>${b.band}</td>
      <td>${b.score_from}</td>
      <td>${b.score_to}</td>
      <td>${fmtN(b.n)}</td>
      <td>${fmtN(b.events)}</td>
      <td>${fmtPct(b.event_rate)}</td>
      <td>${fmtN(b.cum_n)}</td>
      <td>${fmtN(b.cum_events)}</td>
      <td>${fmtPct(b.cum_event_rate)}</td>
      <td>${fmtPct(b.pct_of_total)}</td>
      <td>${fmtPct(b.pct_events_captured)}</td>
    </tr>`).join("");

  el.innerHTML = `
    <div style="overflow-x:auto">
      <table class="perf-table">
        <thead><tr>
          <th>Band</th><th>Score from</th><th>Score to</th>
          <th>N</th><th>Events</th><th>Event rate</th>
          <th>Cum. N</th><th>Cum. events</th><th>Cum. event rate</th>
          <th>% Population</th><th>% Events captured</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ── Calibration ───────────────────────────────────────────────────
function renderModelCalib(el, r) {
  el.innerHTML = `
    <div class="model-chart-grid">
      <div class="model-chart-card" style="grid-column:1/-1">
        <div class="model-chart-title">Calibration — Predicted vs Actual bad rate</div>
        <div class="model-chart-wrap" style="height:320px"><canvas id="chartCalib"></canvas></div>
      </div>
    </div>`;

  const c = r.calibration;
  _modelCharts.calib = new Chart($("chartCalib"), {
    type: "scatter",
    data: {
      datasets: [
        { label: "Bins", data: c.map(b => ({ x: b.pred_rate, y: b.actual_rate, r: Math.sqrt(b.n)/4 })),
          backgroundColor: "rgba(255,255,255,0.7)", borderColor: "#FFFFFF", borderWidth: 1,
          pointRadius: c.map(b => Math.max(4, Math.sqrt(b.n)/3)) },
        { label: "Perfect calibration",
          data: [{ x: 0, y: 0 }, { x: 1, y: 1 }],
          type: "line", borderColor: "rgba(255,255,255,0.35)", borderWidth: 1, borderDash: [4,4],
          pointRadius: 0, fill: false },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: "#FFFFFF", font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => {
          if (ctx.datasetIndex === 0) {
            const b = c[ctx.dataIndex];
            return ` Predicted: ${fmtPct(b.pred_rate)}  Actual: ${fmtPct(b.actual_rate)}  N: ${fmtN(b.n)}`;
          }
        }}}
      },
      scales: {
        x: { title: { display: true, text: "Predicted bad rate", color: "#FFFFFF" },
             ticks: { color: "#FFFFFF", callback: v => fmtPct(v) },
             grid: { color: "rgba(255,255,255,0.12)" } },
        y: { title: { display: true, text: "Actual bad rate", color: "#FFFFFF" },
             ticks: { color: "#FFFFFF", callback: v => fmtPct(v) },
             grid: { color: "rgba(255,255,255,0.12)" } },
      }
    }
  });
}

// ── Coefficients ──────────────────────────────────────────────────
function renderModelCoef(el, r) {
  const maxAbs = Math.max(...r.coefficients.map(c => Math.abs(c.coefficient)));
  const rows = r.coefficients.map(c => {
    const barW   = maxAbs > 0 ? Math.round(Math.abs(c.coefficient) / maxAbs * 80) : 0;
    const cls    = !c.in_model ? "coef-zero" : c.coefficient > 0 ? "coef-pos" : "coef-neg";
    const barCol = c.coefficient > 0 ? "var(--blue)" : "var(--red)";
    return `<tr>
      <td style="font-family:var(--mono)">${c.variable}</td>
      <td class="${cls}">${c.coefficient > 0 ? "+" : ""}${c.coefficient.toFixed(5)}</td>
      <td>
        <div class="coef-bar-wrap">
          <div class="coef-bar-fill" style="width:${barW}%;background:${barCol}"></div>
        </div>
      </td>
      <td style="color:${c.in_model ? 'var(--accent)' : 'var(--text-3)'}">
        ${c.in_model ? "✓ In model" : "✗ Zeroed out"}
      </td>
    </tr>`;
  }).join("");

  el.innerHTML = `
    <p style="font-size:11px;color:var(--text-3);margin-bottom:12px">
      ${r.n_in_model} of ${r.selected_cols.length} variables have non-zero coefficients.
      Positive coefficient = higher WoE → higher predicted probability.
    </p>
    <div style="overflow-x:auto">
      <table class="coef-table">
        <thead><tr><th>Variable</th><th>Coefficient</th><th>Magnitude</th><th>Status</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}
