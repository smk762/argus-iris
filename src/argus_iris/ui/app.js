// iris dashboard: a thin client over the broker's own API. /events sends every node's state and every active lease on
// connect and again on each change, so the page needs no polling. The broker decides; this page only shows (and asks for a warm).

const api = (path) => new URL(`../${path}`, location.href);
const $ = (sel, root = document) => root.querySelector(sel);
const TOKEN_KEY = "iris-broker-token";
const SLOTS = 8; // dh categorical slots: 1-7 name a service, the last is "other", where an 8th service folds

const nodes = new Map(); // name -> NodeState (latest)
const leases = new Map(); // lease_id -> LeaseRecord, queued or granted only
const order = []; // node names in config order
const colours = new Map(); // node -> Map(service -> slot), grows only: colour follows the service, never its rank
let config = null;

// ---------- formatting ----------
const gb = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GB` : `${Math.round(mb)} MB`);
const pct = (part, whole) => (whole ? `${Math.round((part / whole) * 100)}%` : "–");
function dur(s) {
  if (s == null || !Number.isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}
const since = (iso) => (iso ? (Date.now() - Date.parse(iso)) / 1000 : null);
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "style") for (const [p, val] of Object.entries(v)) n.style.setProperty(p, val);
    else if (k.startsWith("data-") || k.startsWith("aria-") || k === "role" || k === "tabindex") n.setAttribute(k, v);
    else n[k] = v;
  }
  n.append(...kids.filter((k) => k != null));
  return n;
}

// ---------- colour per service ----------
function slotFor(node, service) {
  let m = colours.get(node);
  if (!m) colours.set(node, (m = new Map()));
  if (!m.has(service)) m.set(service, m.size < SLOTS - 1 ? m.size + 1 : 0);
  return m.get(service);
}
const colourVar = (slot) => `var(--dh-cat-${slot || SLOTS})`; // slot 0 ("other") alone gets the last colour
function seedColours(state) {
  // Configured services first, in config order, so a service keeps its colour whether or not it is loaded.
  for (const s of config?.services ?? []) if (s.node === state.name) slotFor(state.name, s.id);
  for (const r of state.residents) slotFor(state.name, r.service);
}

// ---------- rendering ----------
function tile(label, value, sub) {
  return el("div", { class: "tile" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: value }),
    sub ? el("div", { class: "sub", text: sub }) : null);
}

function segments(state) {
  const L = state.ledger;
  const parts = [];
  // Residents grouped by service, largest service first so the big blocks lead.
  const bySvc = new Map();
  for (const r of state.residents) {
    if (r.vram_mb <= 0) continue;
    const g = bySvc.get(r.service) ?? { mb: 0, models: [] };
    g.mb += r.vram_mb;
    if (r.model) g.models.push(r.model);
    bySvc.set(r.service, g);
  }
  [...bySvc.entries()].sort((a, b) => b[1].mb - a[1].mb).forEach(([svc, g]) => {
    const slot = slotFor(state.name, svc);
    parts.push({ key: svc, name: svc, mb: g.mb, cls: "", c: colourVar(slot), note: g.models.join(", ") });
  });
  const unattributed = Math.max(0, L.unattributed_mb);
  if (unattributed) parts.push({ key: "~unattributed", name: "unattributed", mb: unattributed, cls: "unattributed",
    note: "used VRAM no resident accounts for (CUDA contexts, display, other users)" });
  const admissible = Math.max(0, Math.min(L.free_mb, L.admissible_mb));
  const held = Math.max(0, L.free_mb - admissible);
  if (held) parts.push({ key: "~held", name: "held back", mb: held, cls: "held",
    note: `free VRAM kept back for the reserve (${gb(L.reserve_mb)}) and headroom (${gb(L.headroom_mb)}), less what used VRAM already covers` });
  if (admissible) parts.push({ key: "~free", name: "admissible", mb: admissible, cls: "free",
    note: "what a new lease could get without evicting" });
  return parts;
}

function renderBar(node, state) {
  const wrap = $(".vram", node);
  const L = state.ledger;
  if (!L || !L.total_mb) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const bar = $(".seg-bar", wrap);
  const legend = $(".legend", wrap);
  const parts = segments(state);
  bar.replaceChildren();
  legend.replaceChildren();
  for (const p of parts) {
    const share = p.mb / L.total_mb;
    const label = `${p.name}: ${gb(p.mb)} (${pct(p.mb, L.total_mb)})${p.note ? `. ${p.note}` : ""}`;
    const seg = el("div", { class: `seg ${p.cls}`, tabindex: "0", "aria-label": label, style: { "--c": p.c ?? "", "flex-basis": `calc(${(share * 100).toFixed(3)}% - 2px)` } });
    seg.dataset.tipTitle = p.name;
    seg.dataset.tipBody = `${gb(p.mb)} · ${pct(p.mb, L.total_mb)} of ${gb(L.total_mb)}${p.note ? `\n${p.note}` : ""}`;
    seg.append(el("span", { class: "seg-label", text: gb(p.mb) }));
    bar.append(seg);
    legend.append(el("li", {}, el("span", { class: `sw ${p.cls}`, style: { "--c": p.c ?? "" } }), p.name));
  }
  bar.setAttribute("aria-label", `${state.name} VRAM, ${gb(L.total_mb)} total: ` + parts.map((p) => `${p.name} ${gb(p.mb)}`).join(", "));
  requestAnimationFrame(() => fitLabels(bar));
}

// A block too narrow for its label prints none (hover, focus and the table still carry the value).
function fitLabels(bar) {
  for (const seg of bar.children) {
    const label = seg.firstElementChild;
    label.hidden = false;
    if (label.offsetWidth + 12 > seg.clientWidth) label.hidden = true;
  }
  inkFor(bar);
}

// White or near-black text inside a fill, by the fill's luminance (re-run when the theme flips).
function inkFor(root) {
  for (const seg of root.querySelectorAll(".seg:not(.held):not(.free)")) {
    const m = getComputedStyle(seg).backgroundColor.match(/[\d.]+/g);
    if (!m) continue;
    const [r, g, b] = m.slice(0, 3).map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    });
    seg.dataset.ink = 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? "dark" : "light";
  }
}

function renderResidents(node, state) {
  const tbody = $("table.residents tbody", node);
  const rows = [...state.residents].sort((a, b) => b.vram_mb - a.vram_mb);
  const age = since(state.last_seen) ?? 0;
  tbody.replaceChildren(...(rows.length ? rows.map((r) => {
    const slot = slotFor(state.name, r.service);
    const idle = r.idle_s == null ? null : r.idle_s + age;
    const modelState = r.detail?.model_state;
    let st = r.busy ? "busy" : "idle";
    if (modelState && modelState !== "loaded") st = modelState;
    const left = r.expires_at ? -since(r.expires_at) : null;
    const exp = left == null ? "" : left > 365 * 86400 ? " · pinned" : ` · expires ${dur(left)}`;
    const pol = policyText(r.policy);
    return el("tr", {},
      el("td", {}, el("span", { class: "sw", style: { "--c": colourVar(slot) }, "aria-hidden": "true" }), r.service),
      el("td", { text: r.model ?? "–" }),
      el("td", { class: "meta", title: r.tenant ? `tenant ${r.tenant}: its own leases count this memory as theirs` : null },
        r.tenant && r.tenant !== r.service ? `${r.source} · ${r.tenant}` : r.source),
      el("td", { class: "num", text: gb(r.vram_mb) }),
      el("td", { class: "num", "data-idle": idle ?? "", "data-at": Date.now(), text: dur(idle) }),
      el("td", { class: `state ${r.busy ? "busy" : ""}`, title: r.policy?.note ?? null }, st + exp,
        pol ? el("span", { class: `svc-err ${r.policy.enforce ? "" : "would"}`, text: pol }) : null),
      evictCell(r));
  }) : [el("tr", {}, el("td", { colSpan: 7, class: "none", text: "nothing resident" }))]));
}

// What the residency policy says: pinned, when idle-kill is due, what iris does next ("would" while not enforced).
function policyText(p) {
  if (!p) return null;
  if (p.pinned) return p.enforce || !p.next ? "policy: pinned" : "policy: pinned (would set keep_alive -1)";
  const verb = { evict: "idle-kill", unload: "unload from CPU", restart: "restart", extend: "extend keep_alive", pin: "pin" }[p.next];
  if (verb) return p.enforce ? `policy: ${verb} now` : `policy: would ${verb}`;
  if (p.due_in_s != null) return `policy: idle-kill in ${dur(p.due_in_s)}${p.enforce ? "" : " (report-only)"}`;
  return p.note ? `policy: ${p.note}` : null;
}

// Residents iris can evict: those read through an adapter, while they hold VRAM. Observed processes have none, and
// an external service only when it is configured to be stopped (evict_by: stop).
function evictCell(r) {
  const cell = el("td", {});
  const freed = ["unloaded", "cpu"].includes(r.detail?.model_state);
  const stop = r.source === "external" && config?.services?.find((s) => s.id === r.service)?.evict_by === "stop";
  if (r.source === "process" || (r.source === "external" && !stop) || freed) return cell;
  const btn = el("button", { type: "button", class: "btn ghost", text: stop ? "Stop" : "Evict", disabled: r.busy,
    title: r.busy ? "busy: iris never interrupts work"
      : stop ? "stop its container: its clients fall back elsewhere; Warm starts it again"
      : "free its VRAM (to CPU where the service can, else unload)" });
  btn.addEventListener("click", () => runAction(`residents/${r.id.split("/").map(encodeURIComponent).join("/")}/evict`, r.id,
    btn, cell, btn.textContent, stop ? "Stopping…" : "Evicting…", evictSummary));
  cell.append(btn);
  const prev = lastResult.get(r.id);
  if (prev) cell.append(prev.cloneNode(true));
  return cell;
}
const evictSummary = (b) => `${{ evict: "moved to CPU", stop: "stopped" }[b.action] ?? "unloaded"}: ${gb(b.before_mb - b.after_mb)} freed`
  + (b.residual_mb ? `, ${gb(b.residual_mb)} still held (restart frees it)` : "") + (b.confirmed ? "" : " (not confirmed)");

function renderServices(node, state) {
  const tbody = $("table.services tbody", node);
  const cfg = new Map((config?.services ?? []).map((s) => [s.id, s]));
  tbody.replaceChildren(...(state.services.length ? state.services.map((s) => {
    const c = cfg.get(s.id);
    const q = s.queue_running == null && s.queue_pending == null ? "–" : `${s.queue_running ?? 0} / ${s.queue_pending ?? 0}`;
    const canWarm = c && (c.warm_path || c.container);
    const status = el("td", {}, el("span", { class: s.up ? "status-up" : "status-down", text: s.up ? "up" : "down" }),
      s.error ? el("span", { class: "svc-err", text: s.error }) : null,
      s.crowded_out ? el("span", { class: "svc-err", title: "pins, unmanaged processes and unloaded residues: nothing iris can evict", text: `needs ${gb(s.need_mb)}, only ${gb(s.room_mb)} can ever be free` }) : null);
    const act = el("td", {});
    if (canWarm) {
      const btn = el("button", { type: "button", class: "btn ghost", text: "Warm", title: warmHint(c) });
      btn.addEventListener("click", () => runAction(`services/${encodeURIComponent(s.id)}/warm`, s.id, btn, act, "Warm", "Warming…", warmSummary));
      act.append(btn);
      const prev = lastResult.get(s.id);
      if (prev) act.append(prev.cloneNode(true));
    }
    return el("tr", {}, el("td", { text: s.id }), el("td", { class: "meta", text: s.adapter }), status,
      el("td", { class: "num", title: "running / pending", text: q }), act);
  }) : [el("tr", {}, el("td", { colSpan: 5, class: "none", text: "no services configured" }))]));
}
const warmHint = (c) => [c.container && `starts ${c.container} if it is down`, c.warm_path && `then calls ${c.warm_path}`].filter(Boolean).join(", ");

function renderNode(state) {
  let node = document.getElementById(`node-${state.name}`);
  if (!node) {
    node = $("#node-tpl").content.firstElementChild.cloneNode(true);
    node.id = `node-${state.name}`;
    placeNode(node, state.name);
  }
  seedColours(state);
  node.dataset.reachable = String(state.reachable);
  $(".node-name", node).textContent = state.name;
  $(".gpu", node).textContent = state.gpu ? `${state.gpu.name} · ${gb(state.gpu.total_mb)}` : "";
  const reach = $(".reach", node);
  reach.dataset.state = state.reachable ? "ok" : "bad";
  reach.textContent = state.reachable ? "agent up" : "agent unreachable";
  const seen = $(".seen", node);
  seen.dataset.since = state.last_seen ?? "";
  seen.textContent = state.last_seen ? `seen ${dur(since(state.last_seen))} ago` : "never seen";
  const err = $(".node-error", node);
  err.hidden = !state.error;
  err.textContent = state.error ? `${state.agent_url}: ${state.error}` : "";

  const L = state.ledger;
  const tiles = [];
  if (L) {
    const used = L.total_mb - L.free_mb;
    tiles.push(tile("VRAM used", gb(used), `${pct(used, L.total_mb)} of ${gb(L.total_mb)}`));
    tiles.push(tile("Admissible", gb(L.admissible_mb), "without evicting"));
    tiles.push(tile("Residents", gb(L.resident_mb), `${state.residents.length} attributed`));
  } else {
    const held = state.residents.reduce((a, r) => a + r.vram_mb, 0);
    if (held) tiles.push(tile("Residents", gb(held), "read through adapters"));
  }
  if (state.gpu?.util_pct != null) tiles.push(tile("GPU util", `${state.gpu.util_pct}%`));
  if (state.host) tiles.push(tile("Host RAM free", gb(state.host.available_mb), `of ${gb(state.host.total_mb)}`));
  $(".tiles", node).replaceChildren(...tiles);

  renderBar(node, state);
  renderResidents(node, state);
  renderServices(node, state);
  renderLeases(state.name);
}

// Progress is what the caller reports with its heartbeat (cortex GpuLease.report_progress); the broker only relays it.
function progressCell(l) {
  if (l.state !== "granted" || (l.progress == null && !l.stage)) return el("td", { class: "meta", text: "–" });
  const p = l.progress;
  const elapsed = since(l.granted_at);
  // Linear ETA from the grant: rough, and only once there is enough done to extrapolate from.
  const left = p != null && p >= 0.05 && p < 1 && elapsed != null ? (elapsed * (1 - p)) / p : null;
  const label = [p != null ? `${Math.round(p * 100)}%` : null, left != null ? `~${dur(left)} left` : null]
    .filter(Boolean).join(" · ");
  return el("td", { class: "progress-cell" },
    p != null ? el("div", { class: "progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100,
      "aria-valuenow": Math.round(p * 100), "aria-label": `${l.tenant} progress` },
      el("div", { class: "fill", style: { width: `${(p * 100).toFixed(1)}%` } })) : null,
    label ? el("span", { class: "sub", text: label }) : null,
    l.stage ? el("span", { class: "svc-err", text: l.stage, title: l.stage }) : null);
}

function renderLeases(name) {
  const node = document.getElementById(`node-${name}`);
  if (!node) return;
  const rows = [...leases.values()].filter((l) => l.node === name)
    .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  $("table.leases tbody", node).replaceChildren(...(rows.length ? rows.map((l) => {
    const from = l.granted_at ?? l.created_at;
    // fits=false: admission would have refused it. Observe-only grants it anyway; the row says so.
    const over = l.fits === false;
    // stalled_at: heartbeating, yet no GPU work under the lease for lease_stall_s (a keepalive that outlived its job).
    const stalled = l.state === "granted" && l.stalled_at;
    const st = l.state === "queued" ? "queued" : stalled ? "granted · stalled" : over ? "granted · over" : "granted";
    const stall = stalled
      ? [`no GPU activity for ${dur(since(l.active_at ?? l.granted_at))}`, l.yield_requested && "asked to yield"]
        .filter(Boolean).join(" · ")
      : null;
    const why = [l.reason, l.caller && `caller ${l.caller}`, l.admissible_mb != null && `${gb(l.admissible_mb)} admissible at grant`]
      .filter(Boolean).join(" · ");
    // Size: weights the service keeps after the lease vs the working set held only while it runs, and what the
    // caller says it really peaked at (heartbeats), so a manifest that over- or under-asks shows.
    const split = l.model_mb ? `${gb(l.model_mb)} model · ${gb(l.vram_mb - l.model_mb)} work` : null;
    // peak_source "broker": the caller could not say (a container cannot see its host PID), so iris filled in what its
    // tenant's own processes held on the node during the lease: the process total, shared by concurrent leases.
    const peak = l.peak_vram_mb != null ? `peak ${gb(l.peak_vram_mb)}${l.peak_source === "broker" ? " (seen by iris)" : ""}` : null;
    return el("tr", {},
      el("td", {}, `${l.tenant} · `, el("span", { class: "meta", text: l.kind }),
        l.caller ? el("span", { class: "svc-err", text: l.caller, title: l.caller }) : null),
      el("td", { text: l.target }),
      el("td", { class: "meta", text: l.priority + (l.preemptible ? " · preemptible" : "") }),
      el("td", { class: "num" }, l.vram_mb ? gb(l.vram_mb) : "–",
        split || peak ? el("span", { class: "sub", text: [split, peak].filter(Boolean).join(" · ") }) : null),
      el("td", { class: "num", "data-idle": since(from) ?? "", "data-at": Date.now(), text: dur(since(from)) }),
      progressCell(l),
      el("td", { class: `state ${over || stalled ? "busy" : ""}`, title: why || null }, st,
        stall ? el("span", { class: "svc-err", text: stall }) : null,
        // A queued lease says what it waits for (evicting X, waiting for N MB): the point of looking at the queue.
        l.state === "queued" && l.reason ? el("span", { class: "svc-err", text: l.reason }) : null));
  }) : [el("tr", {}, el("td", { colSpan: 7, class: "none", text: "no leases" }))]));
}

function placeNode(node, name) {
  const main = $("#nodes");
  $("#placeholder")?.remove();
  const idx = order.indexOf(name);
  const after = order.slice(idx + 1).map((n) => document.getElementById(`node-${n}`)).find(Boolean);
  main.insertBefore(node, after ?? null);
}

function renderAgents() {
  const all = [...nodes.values()];
  const up = all.filter((n) => n.reachable).length;
  const pill = $("#agents");
  pill.textContent = `${up}/${all.length || order.length} agents`;
  pill.dataset.state = !all.length ? "unknown" : up === all.length ? "ok" : up ? "warn" : "bad";
}

// Ages tick locally between events.
setInterval(() => {
  for (const s of document.querySelectorAll(".seen[data-since]")) {
    if (s.dataset.since) s.textContent = `seen ${dur(since(s.dataset.since))} ago`;
  }
  for (const td of document.querySelectorAll("td[data-idle]")) {
    if (td.dataset.idle === "") continue;
    td.textContent = dur(Number(td.dataset.idle) + (Date.now() - Number(td.dataset.at)) / 1000);
  }
}, 1000);

// ---------- warm ----------
const lastResult = new Map(); // service or resident id -> result element (kept across re-renders)
const warmSummary = (b) => (b.up ? "" : "still down: ")
  + (b.detail ?? [b.started_container && "container started", b.warmed && "warmed", `up after ${b.waited_s.toFixed(1)}s`].filter(Boolean).join(", "));
async function runAction(path, key, btn, cell, label, busyLabel, summary) {
  let token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) token = await askToken();
  if (!token) return;
  btn.disabled = true;
  btn.textContent = busyLabel;
  let msg;
  try {
    const res = await fetch(api(path), { method: "POST", headers: { Authorization: `Bearer ${token}` } });
    const body = await res.json().catch(() => ({}));
    if (res.status === 401) sessionStorage.removeItem(TOKEN_KEY);
    const ok = res.ok && body.up !== false && body.confirmed !== false;
    msg = el("span", { class: `result ${ok ? "ok" : "err"}`, text: res.ok ? summary(body) : `${res.status}: ${body.detail ?? res.statusText}` });
  } catch (e) {
    msg = el("span", { class: "result err", text: String(e) });
  }
  lastResult.set(key, msg);
  btn.disabled = false;
  btn.textContent = label;
  cell.querySelector(".result")?.remove();
  cell.append(msg.cloneNode(true));
}

// ---------- token dialog ----------
const dlg = $("#token-dialog");
let tokenResolve = null;
function askToken() {
  $("#token-input").value = sessionStorage.getItem(TOKEN_KEY) ?? "";
  dlg.showModal();
  return new Promise((r) => (tokenResolve = r));
}
$("#token-btn").addEventListener("click", () => askToken());
$("#token-cancel").addEventListener("click", () => dlg.close("cancel"));
$("#token-clear").addEventListener("click", () => { sessionStorage.removeItem(TOKEN_KEY); $("#token-input").value = ""; dlg.close("cancel"); });
$("#token-form").addEventListener("submit", () => {
  const v = $("#token-input").value.trim();
  if (v) sessionStorage.setItem(TOKEN_KEY, v);
});
dlg.addEventListener("close", () => {
  const v = dlg.returnValue === "save" ? sessionStorage.getItem(TOKEN_KEY) : null;
  tokenResolve?.(v);
  tokenResolve = null;
  $("#token-btn").textContent = sessionStorage.getItem(TOKEN_KEY) ? "Broker token ✓" : "Broker token";
});
$("#token-btn").textContent = sessionStorage.getItem(TOKEN_KEY) ? "Broker token ✓" : "Broker token";

// ---------- tooltip (hover and keyboard focus) ----------
const tip = $("#tip");
function showTip(target, x, y) {
  tip.replaceChildren(el("strong", { text: target.dataset.tipTitle }), ...target.dataset.tipBody.split("\n").flatMap((l, i) => (i ? [el("br"), l] : [l])));
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = `${Math.min(window.innerWidth - r.width - 8, Math.max(8, x - r.width / 2))}px`;
  tip.style.top = `${Math.max(8, y - r.height - 10)}px`;
}
document.addEventListener("pointerover", (e) => { const t = e.target.closest?.("[data-tip-title]"); if (t) showTip(t, e.clientX, t.getBoundingClientRect().top); });
document.addEventListener("pointermove", (e) => { const t = e.target.closest?.("[data-tip-title]"); if (t) showTip(t, e.clientX, t.getBoundingClientRect().top); });
document.addEventListener("pointerout", (e) => { if (e.target.closest?.("[data-tip-title]")) tip.hidden = true; });
document.addEventListener("focusin", (e) => { const t = e.target.closest?.("[data-tip-title]"); if (t) { const r = t.getBoundingClientRect(); showTip(t, r.left + r.width / 2, r.top); } });
document.addEventListener("focusout", () => (tip.hidden = true));

// ---------- data ----------
window.addEventListener("dh-theme-change", () => requestAnimationFrame(() => inkFor(document)));
window.addEventListener("resize", () => { for (const b of document.querySelectorAll(".seg-bar")) fitLabels(b); });

async function boot() {
  try {
    const [h, c] = await Promise.all([fetch(api("health")).then((r) => r.json()), fetch(api("config")).then((r) => r.json())]);
    config = c;
    order.push(...Object.keys(c.nodes));
    $("#version").textContent = `v${h.version}`;
    $("#mode").textContent = h.mode ?? "observe-only";
  } catch (e) {
    $("#placeholder").textContent = `Could not reach the broker: ${e}`;
  }
  const live = $("#live");
  const setLive = (state, text) => { live.dataset.state = state; live.lastElementChild.textContent = text; };
  const es = new EventSource(api("events"));
  es.addEventListener("open", () => {
    setLive("ok", "live");
    // The stream resends every active lease on connect; drop what we had so leases that ended meanwhile go.
    const had = new Set([...leases.values()].map((l) => l.node));
    leases.clear();
    for (const name of had) if (name) renderLeases(name);
  });
  es.addEventListener("lease", (e) => {
    const l = JSON.parse(e.data);
    const was = leases.get(l.lease_id);
    if (l.state === "queued" || l.state === "granted") leases.set(l.lease_id, l);
    else leases.delete(l.lease_id);
    if (was?.node && was.node !== l.node) renderLeases(was.node);
    if (l.node) renderLeases(l.node);
  });
  es.addEventListener("error", () => setLive(es.readyState === EventSource.CLOSED ? "bad" : "warn", "reconnecting"));
  es.addEventListener("node", (e) => {
    const state = JSON.parse(e.data);
    if (!order.includes(state.name)) order.push(state.name);
    nodes.set(state.name, state);
    renderNode(state);
    renderAgents();
  });
}
boot();
