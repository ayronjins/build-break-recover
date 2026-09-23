/* BBR Console frontend.
   No framework, no build step, no third-party requests. The browser only
   ever receives JSON from the local API - never credentials, never a
   command surface. */

const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let VIEW = "resolved";
let DETAIL = null;
let TIMER = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

function ago(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}

function dur(sec) {
  if (sec == null) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function healthClass(h) {
  return { healthy: "ok", degraded: "warn", critical: "crit", unreachable: "crit" }[h] || "";
}

/* ------------------------------------------------------------ status bar */
async function updateStatus() {
  try {
    const h = await api("/api/health");
    const dot = $("#conn-dot"), txt = $("#conn-text");
    if (!h.target_reachable) {
      dot.className = "dot dot-crit";
      txt.textContent = "TARGET UNREACHABLE";
    } else if (h.stale) {
      dot.className = "dot dot-warn";
      txt.textContent = `STALE · ${ago(h.age_seconds)}`;
    } else {
      dot.className = "dot dot-" + (healthClass(h.health) || "ok");
      txt.textContent = `${(h.health || "ok").toUpperCase()} · ${ago(h.age_seconds)}`;
    }
    $("#last-updated").textContent = h.collected_at ? `snapshot ${h.collected_at}` : "";
  } catch {
    $("#conn-dot").className = "dot dot-crit";
    $("#conn-text").textContent = "CONSOLE OFFLINE";
  }
}

/* ---------------------------------------------------------------- charts */
function sparkline(series, key, color, max) {
  const pts = series.filter(p => p.reachable && p[key] != null);
  if (pts.length < 2) return `<div class="empty">Collecting samples. The chart fills in as
    snapshots accumulate (one every 30s).</div>`;
  const W = 600, H = 110, P = 8, LP = 34;
  const hi = max ?? Math.max(...pts.map(p => p[key]), 1);
  const x = i => LP + i * (W - LP - P) / (pts.length - 1);
  const y = v => H - P - (v / hi) * (H - 2 * P);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  const area = `M${x(0)},${H - P} ` +
    pts.map((p, i) => `L${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ") +
    ` L${x(pts.length - 1)},${H - P} Z`;

  const gridVals = [0, hi / 2, hi];
  const grid = gridVals.map(v => `
    <line x1="${LP}" y1="${y(v)}" x2="${W - P}" y2="${y(v)}"
          stroke="var(--line)" stroke-width="1" opacity=".6"/>
    <text x="${LP - 6}" y="${y(v) + 3}" text-anchor="end"
          font-size="11" font-family="ui-monospace,monospace"
          fill="var(--text-faint)">${v % 1 ? v.toFixed(1) : v}</text>`).join("");

  const gaps = series.map((p, i) => p.reachable ? null : i).filter(v => v !== null)
    .map(i => {
      const gx = LP + i * (W - LP - P) / Math.max(series.length - 1, 1);
      return `<line x1="${gx}" y1="${P}" x2="${gx}" y2="${H - P}"
                    stroke="var(--crit)" stroke-width="1.5" opacity=".4"/>`;
    }).join("");

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
               role="img" aria-label="${esc(key)} over time">
    ${grid}${gaps}
    <path d="${area}" fill="${color}" opacity=".10"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.8"
          stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(pts.length - 1)}" cy="${y(pts[pts.length - 1][key])}" r="2.6" fill="${color}"/>
  </svg>`;
}

/* -------------------------------------------------------------- overview */
async function viewOverview() {
  const [d, hist] = await Promise.all([api("/api/overview"), api("/api/history?points=60")]);
  const m = d.meta, s = d.sli, b = d.baseline;

  let banner = "";
  if (!m.reachable) {
    banner = `<div class="banner b-crit"><div><div class="bt">Target VM unreachable</div>
      <div class="bd">${esc(m.error || "no response from collector")}<br>
      Showing the last snapshot from ${esc(m.collected_at)} (${ago(m.age_seconds)}).
      The console keeps serving the last snapshot instead of failing outright,
      because the target being down is exactly when you need to know what
      its final state was.</div></div></div>`;
  } else if (d.health === "critical") {
    banner = `<div class="banner b-crit"><div><div class="bt">Incident in progress</div>
      <div class="bd">${d.findings.filter(f => f.severity === "critical").length} critical
      finding(s). The evidence below is collected, not inferred.</div></div></div>`;
  } else if (d.health === "degraded") {
    banner = `<div class="banner b-warn"><div><div class="bt">Degraded</div>
      <div class="bd">Non-critical findings present.</div></div></div>`;
  } else {
    banner = `<div class="banner b-ok"><div><div class="bt">All systems nominal</div>
      <div class="bd">Error rate at baseline, all Services have endpoints,
      no policy denials.</div></div></div>`;
  }

  const errCls = s.error_rate > 50 ? "v-crit" : s.error_rate > 0 ? "v-warn" : "v-ok";
  const p50Cls = s.p50_ms > 1000 ? "v-crit" : s.p50_ms > b.p95_ms * 3 ? "v-warn" : "v-ok";

  const stats = `
  <div class="grid g4">
    <div class="card stat"><span class="l">Error rate</span>
      <span class="v ${errCls}">${s.error_rate ?? "—"}%</span>
      <span class="sub">baseline ${b.error_rate}%</span></div>
    <div class="card stat"><span class="l">p50 latency</span>
      <span class="v ${p50Cls}">${s.p50_ms ?? "—"}<span style="font-size:14px">ms</span></span>
      <span class="sub">baseline ${b.p50_ms}ms</span></div>
    <div class="card stat"><span class="l">p95 latency</span>
      <span class="v">${s.p95_ms ?? "—"}<span style="font-size:14px">ms</span></span>
      <span class="sub">baseline ${b.p95_ms}ms</span></div>
    <div class="card stat"><span class="l">Pods running</span>
      <span class="v v-accent">${d.pods.running}/${d.pods.total}</span>
      <span class="sub">${d.pods.restarts} total restarts</span></div>
  </div>`;

  const findings = d.findings.length ? d.findings.map(f => `
    <div class="finding">
      <span class="sev sev-${f.severity}">${f.severity}</span>
      <div><div class="ft">${esc(f.title)}<span class="plane">${esc(f.plane)}</span></div>
      <div class="fd">${esc(f.detail)}</div></div>
    </div>`).join("")
    : `<div class="empty">No findings. System matches the healthy baseline.</div>`;

  const deps = (d.deployments || []).map(x => `
    <tr><td class="mono">${esc(x.name)}</td>
    <td>${x.ready}/${x.desired}</td>
    <td>${x.desired === 0 ? '<span class="tag tag-crit">SCALED TO ZERO</span>'
        : x.ready === x.desired ? '<span class="tag tag-ok">ready</span>'
        : '<span class="tag tag-warn">degraded</span>'}</td>
    <td class="mono">${x.generation}${x.generation > 1 ? ' <span class="tag tag-warn">changed</span>' : ''}</td>
    </tr>`).join("");

  const eps = Object.entries(d.endpoints || {}).map(([k, v]) => `
    <tr><td class="mono">${esc(k)}</td><td>${v}</td>
    <td>${v === 0 ? '<span class="tag tag-crit">ZERO ENDPOINTS</span>'
        : '<span class="tag tag-ok">routable</span>'}</td></tr>`).join("");

  return `${banner}${stats}
  <h2 class="section">Service level indicators, last ${hist.series.length} samples</h2>
  <div class="grid g2">
    <div class="card"><h3>Error rate %</h3>
      ${sparkline(hist.series, "error_rate", "var(--crit)", 100)}
      <div class="chart-legend"><span><i style="background:var(--crit)"></i>error rate</span>
        <span><i style="background:var(--crit);opacity:.4"></i>target unreachable</span></div></div>
    <div class="card"><h3>p50 latency (ms)</h3>
      ${sparkline(hist.series, "p50_ms", "var(--accent)")}
      <div class="chart-legend"><span><i style="background:var(--accent)"></i>p50 ms</span>
        <span>baseline ${b.p50_ms}ms</span></div></div>
  </div>

  <h2 class="section">Findings, drawn from evidence</h2>
  <div class="card">${findings}</div>

  <div class="grid g2" style="margin-top:14px">
    <div class="card"><h3>Deployments</h3>
      <table><thead><tr><th>Name</th><th>Ready</th><th>State</th><th>Gen</th></tr></thead>
      <tbody>${deps || '<tr><td colspan="4" class="empty">none</td></tr>'}</tbody></table></div>
    <div class="card"><h3>Service endpoints</h3>
      <table><thead><tr><th>Service</th><th>Endpoints</th><th>State</th></tr></thead>
      <tbody>${eps || '<tr><td colspan="3" class="empty">none</td></tr>'}</tbody></table>
      <div class="note">A Service with zero endpoints is one of the great silent failures:
        every pod can look <em>Running</em> while nothing is reachable.</div></div>
  </div>`;
}

/* ------------------------------------------------------------- incidents */
async function viewIncidents() {
  const d = await api("/api/incidents");
  if (!d.incidents.length)
    return `<div class="empty">No experiments recorded yet.</div>`;

  const st = d.stats;
  const byId = {};
  (d.scorecard || []).forEach(s => byId[s.experiment_id] = s);

  const rows = d.incidents.map(i => {
    const diag = i.diagnosis || {};
    let rc = diag.root_cause;
    if (rc && typeof rc === "object") rc = rc.root_cause;
    const conf = i.confidence ?? (typeof diag.root_cause === "object" ? diag.root_cause?.confidence : null);
    const s = byId[i.experiment_id];
    const verdict = i.correct === 1 ? '<span class="tag tag-ok">CORRECT</span>'
      : i.correct === 0 ? '<span class="tag tag-crit">MISSED</span>'
      : '<span class="tag">unscored</span>';
    return `<tr class="clickable" data-eid="${esc(i.experiment_id)}">
      <td class="mono">${esc(i.experiment_id)}</td>
      <td class="mono" style="font-size:12px">${esc(i.opened_at || "")}</td>
      <td>${verdict}</td>
      <td class="mono">${s ? esc(s.fault_actual) : "—"}</td>
      <td style="max-width:340px">${esc((rc || "—").slice(0, 120))}${(rc || "").length > 120 ? "…" : ""}</td>
      <td class="mono">${conf != null ? (conf * 100).toFixed(0) + "%" : "—"}</td>
    </tr>`;
  }).join("");

  const acc = st.accuracy_pct;
  const accCls = acc === 100 ? "v-ok" : acc >= 70 ? "v-warn" : acc == null ? "" : "v-crit";

  return `
  <div class="grid g4">
    <div class="card stat"><span class="l">Experiments</span><span class="v v-accent">${st.total}</span></div>
    <div class="card stat"><span class="l">Root-cause accuracy</span>
      <span class="v ${accCls}">${acc != null ? acc + "%" : "—"}</span>
      <span class="sub">${st.correct}/${st.scored} scored blind</span></div>
    <div class="card stat"><span class="l">Active</span><span class="v">${st.active}</span></div>
    <div class="card stat"><span class="l">Tamper checks</span>
      <span class="v ${st.commitment_failures ? "v-crit" : "v-ok"}">${st.commitment_failures ? "FAIL" : "PASS"}</span>
      <span class="sub">sealed-truth SHA-256</span></div>
  </div>

  <h2 class="section">Experiment log — click a row for full evidence</h2>
  <div class="card"><table>
    <thead><tr><th>Experiment</th><th>Injected</th><th>Verdict</th><th>Actual fault</th>
      <th>Diagnosed root cause</th><th>Conf.</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
  <div class="note">Every row is a real injected fault. The analyst diagnosed each one
    without access to the sealed ground truth — that separation is enforced by unix
    ownership and Kubernetes RBAC, not by instructions. The "actual fault" column is
    filled in only <em>after</em> the diagnosis was recorded, and the SHA-256 commitment
    proves the answer was not edited to match.</div>`;
}

async function viewIncidentDetail(eid) {
  const i = await api(`/api/incidents/${encodeURIComponent(eid)}`);
  const d = i.diagnosis || {};
  const rcObj = typeof d.root_cause === "object" ? d.root_cause : null;
  const rc = rcObj ? rcObj.root_cause : d.root_cause;
  const conf = i.confidence ?? rcObj?.confidence;

  const ev = d.evidence || {};
  const evHtml = Object.keys(ev).length
    ? Object.entries(ev).map(([plane, items]) => `
      <div style="margin-bottom:14px">
        <div class="mono" style="color:var(--text-faint);font-size:12px;letter-spacing:.1em;
             text-transform:uppercase;margin-bottom:6px">${esc(plane.replace(/_/g, " "))}</div>
        <ul style="list-style:none;display:flex;flex-direction:column;gap:5px">
        ${(Array.isArray(items) ? items : [items]).map(x =>
          `<li class="mono" style="font-size:12px;color:var(--text-dim);
             padding-left:14px;position:relative">
             <span style="position:absolute;left:0;color:var(--accent-dim)">›</span>
             ${esc(x)}</li>`).join("")}
        </ul></div>`).join("")
    : "";

  const hyps = (d.ranked_hypotheses || []).map(h => `
    <div class="finding">
      <span class="sev ${h.confidence >= .5 ? "sev-critical" : "sev-info"}">
        ${(h.confidence * 100).toFixed(0)}%</span>
      <div><div class="ft">#${h.rank} ${esc(h.hypothesis)}</div>
      ${(h.supporting || []).length ? `<div class="fd"><strong style="color:var(--ok)">Supports:</strong>
        ${h.supporting.map(esc).join(" · ")}</div>` : ""}
      ${(h.falsified_by || []).length ? `<div class="fd"><strong style="color:var(--crit)">Falsified by:</strong>
        ${h.falsified_by.map(esc).join(" · ")}</div>` : ""}
      </div></div>`).join("");

  const disc = (d.discriminators || []).map(x =>
    `<li class="mono" style="font-size:12px;color:var(--text-dim);margin-bottom:5px">› ${esc(x)}</li>`).join("");

  const rep = d.proposed_repair || {};

  return `<button class="back" id="back-btn">← back to incidents</button>
  <h2 class="section">${esc(i.experiment_id)}</h2>

  <div class="grid g2">
    <div class="card"><h3>Diagnosis</h3>
      <dl class="kv">
        <dt>STATUS</dt><dd><span class="tag ${i.status === "DIAGNOSED" ? "tag-ok" : "tag-warn"}">${esc(i.status)}</span></dd>
        <dt>INJECTED</dt><dd class="mono">${esc(i.opened_at || "—")}</dd>
        <dt>CONFIDENCE</dt><dd class="mono">${conf != null ? (conf * 100).toFixed(0) + "%" : "—"}</dd>
      </dl>
      ${conf != null ? `<div class="bar"><i style="width:${conf * 100}%"></i></div>` : ""}
      ${d.symptom ? `<div class="note"><strong>Symptom:</strong> ${esc(d.symptom)}</div>` : ""}
    </div>
    <div class="card"><h3>Root cause</h3>
      <div style="font-size:13.5px;line-height:1.6">${esc(rc || "not yet determined")}</div>
      ${rcObj?.proximate_cause ? `<div class="note"><strong>Proximate:</strong>
        ${esc(rcObj.proximate_cause)}</div>` : ""}
      ${(rcObj?.contributing_factors || []).map(f =>
        `<div class="note">${esc(f)}</div>`).join("")}
    </div>
  </div>

  ${disc ? `<h2 class="section">Discriminating evidence</h2>
    <div class="card"><ul style="list-style:none">${disc}</ul></div>` : ""}

  ${evHtml ? `<h2 class="section">Evidence by plane</h2><div class="card">${evHtml}</div>` : ""}

  ${hyps ? `<h2 class="section">Ranked hypotheses — including the ones ruled out</h2>
    <div class="card">${hyps}</div>
    <div class="note">Recording falsified hypotheses matters as much as the answer:
      it shows the conclusion survived competing explanations rather than being the
      first guess.</div>` : ""}

  ${rep.change ? `<h2 class="section">Proposed repair</h2>
    <div class="card">
      <dl class="kv">
        <dt>CHANGE</dt><dd>${esc(rep.change)}</dd>
        <dt>SCOPE</dt><dd class="mono">${rep.files_touched ?? "?"} file(s), ${rep.lines_changed ?? "?"} line(s)</dd>
        <dt>SECURITY</dt><dd>${esc(rep.security_impact || "—")}</dd>
        <dt>ROLLBACK</dt><dd>${esc(rep.rollback || "—")}</dd>
      </dl>
      ${rep.why_minimal ? `<div class="note">${esc(rep.why_minimal)}</div>` : ""}
      ${(rep.verification_plan || []).length ? `
        <h3 style="margin-top:14px">Verification plan — stated BEFORE applying</h3>
        <ul style="list-style:none">${rep.verification_plan.map(v =>
          `<li class="mono" style="font-size:12px;color:var(--text-dim);margin-bottom:4px">
           ☐ ${esc(v)}</li>`).join("")}</ul>` : ""}
    </div>` : ""}

  ${d.security_note ? `<div class="banner b-info" style="margin-top:16px"><div>
    <div class="bt">Security note</div><div class="bd">${esc(d.security_note)}</div></div></div>` : ""}`;
}

/* ------------------------------------------------------------------ live */
async function viewLive() {
  const d = await api("/api/live");
  const h = d.host || {};
  const byNs = {};
  (d.pods || []).forEach(p => (byNs[p.ns] = byNs[p.ns] || []).push(p));

  const nsCards = Object.entries(byNs).sort().map(([ns, pods]) => `
    <div class="card"><h3>${esc(ns)} · ${pods.length}</h3>
      <table><tbody>${pods.map(p => `
        <tr><td class="mono" style="font-size:12px">${esc(p.name)}</td>
        <td style="width:70px">${p.ready}/${p.total}</td>
        <td style="width:90px">${p.phase === "Running"
          ? '<span class="tag tag-ok">Running</span>'
          : `<span class="tag tag-warn">${esc(p.phase)}</span>`}</td>
        <td style="width:80px" class="mono">${p.restarts ? `<span class="tag tag-warn">${p.restarts}×</span>` : "0"}</td>
        </tr>`).join("")}</tbody></table></div>`).join("");

  return `
  <div class="grid g4">
    <div class="card stat"><span class="l">Uptime</span><span class="v">${dur(h.uptime_seconds)}</span></div>
    <div class="card stat"><span class="l">Load</span><span class="v">${h.load1 ?? "—"}</span></div>
    <div class="card stat"><span class="l">Memory</span>
      <span class="v">${h.mem_used_mb ?? "—"}<span style="font-size:13px">MB</span></span>
      <span class="sub">of ${h.mem_total_mb ?? "—"}MB · ${h.mem_avail_mb ?? "—"} available</span></div>
    <div class="card stat"><span class="l">Disk</span>
      <span class="v ${parseInt(h.disk_pct) > 85 ? "v-warn" : ""}">${h.disk_pct ?? "—"}</span>
      <span class="sub">${h.disk_used ?? "?"} used · ${h.disk_avail ?? "?"} free</span></div>
  </div>
  <h2 class="section">Cluster nodes</h2>
  <div class="card"><table><thead><tr><th>Node</th><th>Ready</th><th>Version</th></tr></thead>
  <tbody>${(d.nodes || []).map(n => `<tr><td class="mono">${esc(n.name)}</td>
    <td>${n.ready === "True" ? '<span class="tag tag-ok">Ready</span>'
      : `<span class="tag tag-crit">${esc(n.ready)}</span>`}</td>
    <td class="mono">${esc(n.version)}</td></tr>`).join("") ||
    '<tr><td colspan="3" class="empty">no nodes</td></tr>'}</tbody></table></div>
  <h2 class="section">Workloads by namespace</h2>
  <div class="grid g2">${nsCards}</div>`;
}

/* --------------------------------------------------------------- network */
async function viewNetwork() {
  const d = await api("/api/network");
  const v = d.verdicts || {}, drops = d.drops || {};
  const total = Object.values(v).reduce((a, b) => a + b, 0) || 1;
  const denied = drops.POLICY_DENIED || 0;

  return `
  ${denied ? `<div class="banner b-crit"><div><div class="bt">${denied} POLICY_DENIED drops</div>
    <div class="bd">A NetworkPolicy is actively blocking traffic. This is the signature that
    separates "cannot reach" from "is broken" — pods can be perfectly healthy while
    unable to communicate.</div></div></div>` : ""}

  <div class="grid g3">
    <div class="card stat"><span class="l">Forwarded</span>
      <span class="v v-ok">${(v.FORWARDED || 0).toLocaleString()}</span>
      <span class="sub">${((v.FORWARDED || 0) / total * 100).toFixed(2)}% of flows</span></div>
    <div class="card stat"><span class="l">Dropped</span>
      <span class="v ${(v.DROPPED || 0) > 0 ? "v-warn" : "v-ok"}">${(v.DROPPED || 0).toLocaleString()}</span></div>
    <div class="card stat"><span class="l">Policy denied (10m)</span>
      <span class="v ${denied ? "v-crit" : "v-ok"}">${denied}</span></div>
  </div>

  <h2 class="section">Drop reasons — last 10 minutes</h2>
  <div class="card"><table><thead><tr><th>Reason</th><th>Count</th><th>Interpretation</th></tr></thead>
  <tbody>${Object.entries(drops).map(([k, n]) => `<tr>
    <td class="mono">${esc(k)}</td><td>${n}</td>
    <td style="color:var(--text-dim);font-size:12px">${
      k === "POLICY_DENIED" ? "NetworkPolicy is blocking this traffic — investigate policy objects"
      : k === "UNSUPPORTED_L3_PROTOCOL" ? "Benign. IPv6/other traffic the datapath ignores."
      : "—"}</td></tr>`).join("") ||
    '<tr><td colspan="3" class="empty">no drops</td></tr>'}</tbody></table></div>

  <h2 class="section">Network policy objects</h2>
  <div class="card"><table><thead><tr><th>Name</th><th>Created</th></tr></thead>
  <tbody>${(d.netpol || []).map(p => `<tr><td class="mono">${esc(p.name)}</td>
    <td class="mono" style="font-size:12px">${esc(p.created || "")}</td></tr>`).join("") ||
    '<tr><td colspan="2" class="empty">no policies in bbr-demo</td></tr>'}</tbody></table>
  <div class="note">A policy object created shortly before an incident is one of the
    strongest leads available — "what changed" beats "what is broken".</div></div>

  <h2 class="section">Service endpoints</h2>
  <div class="card"><table><thead><tr><th>Service</th><th>Ready endpoints</th></tr></thead>
  <tbody>${Object.entries(d.endpoints || {}).map(([k, n]) => `<tr>
    <td class="mono">${esc(k)}</td>
    <td>${n === 0 ? '<span class="tag tag-crit">ZERO</span>' : n}</td></tr>`).join("")}
  </tbody></table></div>`;
}

/* ---------------------------------------------------------------- kernel */
async function viewKernel() {
  const d = await api("/api/kernel");
  const ev = d.kernel_events || [];
  return `
  <div class="grid g4">
    <div class="card stat"><span class="l">Kernel</span>
      <span class="v" style="font-size:16px">${esc(d.kernel || "—")}</span></div>
    <div class="card stat"><span class="l">OOM kills (24h)</span>
      <span class="v ${d.oom_events ? "v-crit" : "v-ok"}">${d.oom_events ?? 0}</span></div>
    <div class="card stat"><span class="l">Boots recorded</span>
      <span class="v">${d.boot_count ?? "—"}</span>
      <span class="sub">unexpected reboots appear here</span></div>
    <div class="card stat"><span class="l">Failed units</span>
      <span class="v ${(d.failed_units || []).length ? "v-warn" : "v-ok"}">${(d.failed_units || []).length}</span></div>
  </div>

  ${(d.failed_units || []).length ? `<h2 class="section">Failed systemd units</h2>
  <div class="card"><table><tbody>${d.failed_units.map(u =>
    `<tr><td class="mono">${esc(u)}</td></tr>`).join("")}</tbody></table></div>` : ""}

  <h2 class="section">Kernel ring buffer — warnings and above, last 2h</h2>
  <div class="card">
  ${ev.length ? `<div class="code">${ev.map(e => esc(e.msg)).join("\n")}</div>`
    : '<div class="empty">No kernel warnings in the window. This is the healthy case.</div>'}
  <div class="note">Tetragon's eBPF probe warning is expected — it is the kernel noting
    that an observability tool attached a program. Kernel <em>observability</em> is
    deliberately separate from kernel <em>fault injection</em>: nothing here grants
    the ability to break the kernel.</div></div>`;
}

/* ---------------------------------------------------------------- safety */
async function viewSafety() {
  const d = await api("/api/safety");
  const g = d.guarantees.map(x => `
    <div class="finding">
      <span class="sev ${x.verified ? "sev-info" : "sev-warning"}"
            style="${x.verified ? "background:rgba(94,220,154,.13);color:var(--ok)" : ""}">
        ${x.verified ? "VERIFIED" : "UNTESTED"}</span>
      <div><div class="ft">${esc(x.claim)}</div>
      <div class="fd"><span class="mono">mechanism:</span> ${esc(x.mechanism)}</div></div>
    </div>`).join("");

  const pol = (d.policies || []).map(p => `<tr>
    <td class="mono">${esc(p.name)}</td>
    <td>${p.action === "Enforce" ? '<span class="tag tag-accent">Enforce</span>'
      : `<span class="tag tag-warn">${esc(p.action)}</span>`}</td>
    <td>${p.ready === "True" ? '<span class="tag tag-ok">ready</span>'
      : `<span class="tag tag-warn">${esc(p.ready)}</span>`}</td></tr>`).join("");

  const snaps = (d.snapshots || []).map(s => {
    const pct = parseFloat(s.used_pct) || 0;
    return `<tr><td class="mono">${esc(s.name)}</td><td class="mono">${esc(s.size)}</td>
    <td style="width:180px"><div class="bar"><i style="width:${Math.min(pct, 100)}%;
      background:${pct > 80 ? "var(--crit)" : pct > 50 ? "var(--warn)" : "var(--accent)"}"></i></div>
      <span class="mono" style="font-size:12px;color:var(--text-faint)">${pct.toFixed(1)}% used</span></td></tr>`;
  }).join("");

  return `
  <div class="banner b-ok"><div><div class="bt">Autonomy Level ${d.autonomy_level} — ${esc(d.autonomy_label)}</div>
    <div class="bd">${esc(d.autonomy_note)}</div></div></div>

  <div class="grid g3">
    <div class="card stat"><span class="l">Boundary tests</span>
      <span class="v v-ok">${d.boundary_tests.passed}/${d.boundary_tests.passed + d.boundary_tests.failed}</span>
      <span class="sub">analyst isolation</span></div>
    <div class="card stat"><span class="l">Policy tests</span>
      <span class="v v-ok">${d.policy_tests.passed}/${d.policy_tests.passed + d.policy_tests.failed}</span>
      <span class="sub">admission control</span></div>
    <div class="card stat"><span class="l">VG free</span>
      <span class="v v-accent">${esc(d.vg_free || "—")}</span>
      <span class="sub">rollback headroom</span></div>
  </div>

  <h2 class="section">Enforced guarantees — each one adversarially tested</h2>
  <div class="card">${g}</div>

  <h2 class="section">Admission policies</h2>
  <div class="card"><table><thead><tr><th>Policy</th><th>Mode</th><th>State</th></tr></thead>
  <tbody>${pol || '<tr><td colspan="3" class="empty">none</td></tr>'}</tbody></table>
  <div class="note">Enforce, not Audit. An audit-mode policy logs the violation while
    admitting the workload — the appearance of a control without the substance.</div></div>

  <h2 class="section">Rollback snapshots</h2>
  <div class="card"><table><thead><tr><th>Snapshot</th><th>Size</th><th>Fill</th></tr></thead>
  <tbody>${snaps || '<tr><td colspan="3" class="empty">none</td></tr>'}</tbody></table>
  <div class="note">An LVM snapshot that fills to 100% becomes invalid and can no longer
    restore. Fill level is a rollback-capability metric, not just disk trivia.</div></div>`;
}

/* ---------------------------------------------------------------- repair */
async function viewRepair() {
  const d = await api("/api/incidents");
  const withRepair = d.incidents.filter(i => i.diagnosis?.proposed_repair?.change);

  const stages = ["DETECT", "DIAGNOSE", "PROPOSE", "CI", "HUMAN", "POLICY", "DEPLOY", "VERIFY"];
  const pipeline = `<div class="pipeline">${stages.map((s, i) => `
    <div class="stage ${i < 3 ? "done" : i === 4 ? "blocked" : ""}">
      <div class="sn">${String(i + 1).padStart(2, "0")}</div>
      <div class="sl">${s}</div></div>`).join("")}</div>`;

  return `
  <div class="banner b-info"><div><div class="bt">Repair execution is not wired</div>
    <div class="bd">This console can display a proposed repair; it has no code path that
    applies one. The analyst identity is additionally denied every mutating verb by RBAC —
    verified by test, not asserted. Repairs in this lab were applied by a human through a
    separate privileged path.</div></div></div>

  <h2 class="section">Intended change pipeline</h2>
  <div class="card">${pipeline}
    <div class="note">There is deliberately no shortcut from AI directly to Kubernetes.
      Stage 05 (human approval) is a hard stop, not a formality.</div></div>

  <h2 class="section">Proposed repairs on record</h2>
  ${withRepair.length ? withRepair.map(i => {
    const r = i.diagnosis.proposed_repair;
    return `<div class="card" style="margin-bottom:12px">
      <h3>${esc(i.experiment_id)}</h3>
      <dl class="kv">
        <dt>CHANGE</dt><dd>${esc(r.change)}</dd>
        <dt>SCOPE</dt><dd class="mono">${r.files_touched ?? "?"} file(s) · ${r.lines_changed ?? "?"} line(s)</dd>
        <dt>SECURITY</dt><dd>${esc(r.security_impact || "—")}</dd>
      </dl>
      ${r.why_minimal ? `<div class="note">${esc(r.why_minimal)}</div>` : ""}
    </div>`;
  }).join("") : '<div class="card"><div class="empty">No repair proposals recorded yet.</div></div>'}`;
}

/* ------------------------------------------------------- problems fixed */
function resolutionCard(r, kind) {
  const sevCls = { critical: "sev-critical", high: "sev-critical",
                   medium: "sev-warning", low: "sev-info", info: "sev-info" }[r.severity] || "sev-info";

  const list = (arr, cls = "") => (arr || []).map(x =>
    `<li class="res-li ${cls}">${esc(x)}</li>`).join("");

  return `<article class="res">
    <header class="res-head">
      <div class="res-titles">
        <h3 class="res-title">${esc(r.title)}</h3>
        <div class="res-meta">
          <span class="sev ${sevCls}">${esc(r.severity)}</span>
          <span class="tag">${esc(r.category)}</span>
          ${r.id ? `<span class="mono res-id">${esc(r.id)}</span>` : ""}
          ${kind === "injected" && r.diagnosis_correct
            ? '<span class="tag tag-ok">DIAGNOSED CORRECTLY</span>' : ""}
        </div>
      </div>
      <span class="res-status">${esc(r.status)}</span>
    </header>

    ${r.impact ? `<p class="res-impact"><strong>Impact:</strong> ${esc(r.impact)}</p>` : ""}

    <div class="res-flow">
      <section class="res-step">
        <div class="res-step-h"><span class="res-n">1</span> Problem found</div>
        <p class="res-body">${esc(r.symptom)}</p>
        ${r.how_found || r.how_detected
          ? `<p class="res-sub">Detected by: ${esc(r.how_found || r.how_detected)}</p>` : ""}
      </section>

      <section class="res-step">
        <div class="res-step-h"><span class="res-n">2</span> Evidence</div>
        <ul class="res-list">${list(r.key_evidence)}</ul>
        ${r.trap_avoided
          ? `<p class="res-trap"><strong>Trap avoided:</strong> ${esc(r.trap_avoided)}</p>` : ""}
      </section>

      <section class="res-step">
        <div class="res-step-h"><span class="res-n">3</span> Root cause</div>
        <p class="res-body">${esc(r.root_cause)}</p>
        ${r.confidence != null
          ? `<p class="res-sub">Stated confidence: ${(r.confidence * 100).toFixed(0)}%
             ${r.actual_fault ? `· actual fault was <span class="mono">${esc(r.actual_fault)}</span>` : ""}</p>` : ""}
      </section>

      <section class="res-step">
        <div class="res-step-h"><span class="res-n">4</span> Fix applied</div>
        <p class="res-body">${esc(r.fix_applied)}</p>
        ${r.fix_scope ? `<p class="res-sub">Scope: ${esc(r.fix_scope)}</p>` : ""}
        ${r.previous_workaround
          ? `<p class="res-sub">${esc(r.previous_workaround)}</p>` : ""}
        ${r.security_impact
          ? `<p class="res-sec"><strong>Security:</strong> ${esc(r.security_impact)}</p>` : ""}
      </section>

      <section class="res-step res-verified">
        <div class="res-step-h"><span class="res-n">5</span> Verified fixed</div>
        <ul class="res-list">${list(r.verified_by, "ok")}</ul>
      </section>
    </div>

    ${r.ongoing_note ? `<p class="res-ongoing">${esc(r.ongoing_note)}</p>` : ""}
  </article>`;
}

async function viewResolved() {
  const [d, auto] = await Promise.all([
    api("/api/resolved"),
    api("/api/automation").catch(() => ({})),
  ]);
  const s = d.summary;
  const w = auto.watcher;

  const autoPanel = w ? `
  <div class="card auto-panel">
    <h3>Continuity automation — always on</h3>
    <div class="auto-row">
      <span class="dot ${w.alive ? (w.outage_active ? "dot-warn" : "dot-ok") : "dot-crit"}"></span>
      <strong>${w.outage_active ? "Capacity outage — waiting to resume"
                 : w.alive ? "Monitoring · capacity available" : "Watcher not reporting"}</strong>
    </div>
    <dl class="kv" style="margin-top:10px">
      <dt>CHECK EVERY</dt><dd>${Math.round((w.interval_seconds || 0) / 60)} min (backs off automatically)</dd>
      <dt>LAST CHECK</dt><dd>${ago(w.last_check_age_seconds)}</dd>
      <dt>OUTAGES RECOVERED</dt><dd>${w.outages_recovered}</dd>
      <dt>PROBES SAVED</dt><dd>${w.probes_saved} (free local pre-check, no request spent)</dd>
    </dl>
    <div class="note">When the model subscription runs dry, work stops. This watcher notices the
      moment capacity comes back and picks up from the saved task context, so
      nobody has to restart anything by hand. It distinguishes "out of credits" (wait) from a genuine fault (alert), so a
      network or login failure can never trigger a false resume.</div>
  </div>` : "";

  const openItems = (d.open_items || []).map(o => `
    <div class="finding">
      <span class="sev ${o.severity === "warning" ? "sev-warning" : "sev-info"}">
        ${o.severity === "warning" ? "limitation" : "not yet"}</span>
      <div><div class="ft">${esc(o.title)}</div>
      <div class="fd">${esc(o.detail)}</div></div>
    </div>`).join("");

  return `
  <div class="banner b-ok"><div>
    <div class="bt">${s.total_resolved} problems found, diagnosed and fixed on the test system — every fix verified by measurement</div>
    <div class="bd">${s.real_resolved} were real defects discovered while building the lab.
      ${s.injected_resolved} were deliberate faults injected to test diagnosis, all
      ${s.injected_diagnosed_correctly} identified correctly without being told what had been
      broken. No fix weakened a security control.</div></div></div>

  <div class="grid g4">
    <div class="card stat"><span class="l">Problems fixed</span>
      <span class="v v-ok">${s.total_resolved}</span>
      <span class="sub">all verified by measurement</span></div>
    <div class="card stat"><span class="l">Critical resolved</span>
      <span class="v v-accent">${s.critical_fixed}</span>
      <span class="sub">highest severity class</span></div>
    <div class="card stat"><span class="l">Diagnosed blind</span>
      <span class="v v-ok">${s.injected_diagnosed_correctly}/${s.injected_resolved}</span>
      <span class="sub">without seeing the answer</span></div>
    <div class="card stat"><span class="l">Security weakened</span>
      <span class="v v-ok">${s.security_weakened_count}</span>
      <span class="sub">never traded for uptime</span></div>
  </div>

  <div class="scope-note">Scope: everything here happened on the Build-Break-Recover test system, the machine that exists to be broken. It is a Kubernetes cluster on its own isolated virtual machine.</div>

  ${autoPanel}

  <h2 class="section">Real problems found and fixed</h2>
  <p class="section-lede">These are real defects in the lab, not exercises. Each one lists what broke and the evidence that found it, then the fix and the measurement taken afterwards to confirm it worked.</p>
  ${d.real_problems.map(r => resolutionCard(r, "real")).join("")}

  <h2 class="section">Injected faults — diagnosed without being told the answer</h2>
  <p class="section-lede">These faults were injected on purpose. The investigator had no way to read
      the sealed record of what was broken, because separate system accounts
      enforce that rather than a written rule telling it not to peek. A checksum
      on the sealed file shows the answer was not quietly edited afterwards to
      match whatever the diagnosis happened to say.</p>
  ${d.injected_faults.map(r => resolutionCard(r, "injected")).join("")}

  <h2 class="section">Known limitations — stated plainly</h2>
  <div class="card">${openItems}</div>
  <div class="note">Listed because a system that only reports its successes cannot be
    trusted when it reports a success.</div>`;
}
async function viewAbout() {
  return `
  <h2 class="section">What this lab does</h2>
  <div class="card">
    <p style="line-height:1.7">A fault is injected into a Kubernetes cluster by a process the
    analyst cannot see. The analyst observes the resulting behaviour through application,
    Kubernetes, network and kernel telemetry, states a root cause with a confidence score,
    and proposes the smallest safe repair. Only then is the ground truth revealed and the
    diagnosis scored against what was actually broken.</p>
    <div class="note">The loop is <strong>Build → Break → Understand → Repair → Verify</strong>.
      "Understand" sits between break and repair on purpose: a system that detects a failure
      and immediately applies a fix is an autoremediation script. Requiring a stated root
      cause first, and a verification step that proves recovery rather than assuming it,
      is what makes it diagnostic.</div>
  </div>

  <h2 class="section">Why the analyst is blind</h2>
  <div class="card">
    <p style="line-height:1.7">If the same process both injects the fault and investigates it,
    "don't look at the answer" is an instruction, not a boundary. Here the sealed ground truth
    is owned by a separate unix user with directory mode 0700, and a SHA-256 commitment is
    published at injection time so the answer cannot be edited afterwards to match a wrong
    diagnosis.</p>
    <div class="note">Verified by attempting the forbidden action and confirming it fails —
      23 boundary tests, 11 admission-policy tests. A control that has never rejected
      anything is an untested assumption.</div>
  </div>

  <h2 class="section">Evidence planes</h2>
  <div class="card"><table>
    <thead><tr><th>Plane</th><th>Source</th><th>Question it answers</th></tr></thead>
    <tbody>
      <tr><td class="mono">application</td><td class="mono">app logs, SLI probe</td><td>Is the service actually broken?</td></tr>
      <tr><td class="mono">workload</td><td class="mono">Kubernetes API</td><td>What did the orchestrator do?</td></tr>
      <tr><td class="mono">routing</td><td class="mono">EndpointSlices</td><td>Can traffic find a destination?</td></tr>
      <tr><td class="mono">network</td><td class="mono">Cilium / Hubble</td><td>Is traffic being delivered or denied?</td></tr>
      <tr><td class="mono">process</td><td class="mono">Tetragon / eBPF</td><td>What actually executed?</td></tr>
      <tr><td class="mono">change</td><td class="mono">generations, object ages</td><td>What changed just before this?</td></tr>
    </tbody></table>
    <div class="note">No single plane is sufficient. "Pod Running but requests failing" is
      indistinguishable from "pod broken" until you look at flow verdicts.</div>
  </div>

  <h2 class="section">Rules</h2>
  <div class="card"><div class="code">Evidence before action.
Root cause before repair.
Minimal change before broad change.
Security before convenience.
Human approval before deployment.
Verification before claiming success.

Never fabricate telemetry.
Never fabricate tests.
Never weaken security just to make the error disappear.
Never disable the sensor to fix the alarm.</div></div>`;
}

/* ------------------------------------------------------------------ boot */
const VIEWS = {
  resolved: viewResolved, overview: viewOverview, incidents: viewIncidents,
  live: viewLive, network: viewNetwork, kernel: viewKernel, safety: viewSafety,
  repair: viewRepair, about: viewAbout,
};

let VIEW_INIT = "resolved";

async function render() {
  const main = $("#main");
  try {
    main.innerHTML = DETAIL
      ? await viewIncidentDetail(DETAIL)
      : await VIEWS[VIEW]();
  } catch (e) {
    main.innerHTML = `<div class="banner b-crit"><div><div class="bt">Could not load view</div>
      <div class="bd">${esc(e.message)}</div></div></div>`;
  }

  main.querySelectorAll("tr[data-eid]").forEach(tr =>
    tr.addEventListener("click", () => { DETAIL = tr.dataset.eid; render(); }));
  const back = $("#back-btn");
  if (back) back.addEventListener("click", () => { DETAIL = null; render(); });
}

$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-view]");
  if (!b) return;
  $("#tabs .active")?.classList.remove("active");
  b.classList.add("active");
  VIEW = b.dataset.view;
  DETAIL = null;
  render();
});

$("#refresh-btn").addEventListener("click", async e => {
  const b = e.target;
  b.disabled = true; b.textContent = "Collecting…";
  // A bare catch{} here hid a real server error from the person who clicked
  // the button - the counter stayed stale and nothing told them why.
  let failed = false;
  try {
    const r = await fetch("/api/refresh", { method: "POST" });
    if (!r.ok) failed = true;
  } catch { failed = true; }
  await updateStatus(); await render();
  b.disabled = false;
  b.textContent = failed ? "Refresh failed - retry" : "Refresh";
});

function start() {
  updateStatus(); render();
  clearInterval(TIMER);
  TIMER = setInterval(async () => {
    await updateStatus();
    // "resolved" is a static record, not live telemetry - re-rendering it on
    // a timer would scroll the reader's position for no benefit.
    if (!DETAIL && VIEW !== "resolved" && VIEW !== "about") await render();
  }, 15000);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearInterval(TIMER); else start();
});

start();
