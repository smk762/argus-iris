// iris dashboard: a thin client over the broker's own API. /events sends every node's state and every active lease on
// connect and again on each change, so the GPU view needs no polling. The service registry (/services, /registry) is
// re-read when a node changes (debounced) and every 20 s. The broker decides; this page only shows, and asks for a
// warm, an evict or a forget.

const api = (path) => new URL(`../${path}`, location.href);
const $ = (sel, root = document) => root.querySelector(sel);
const TOKEN_KEY = "iris-broker-token";
const SLOTS = 8; // dh categorical slots: 1-7 name a service, the last is "other", where an 8th service folds

const nodes = new Map(); // name -> NodeState (latest)
const leases = new Map(); // lease_id -> LeaseRecord, queued or granted only
const order = []; // node names in config order
const colours = new Map(); // node -> Map(service -> slot), grows only: colour follows the service, never its rank
let config = null;
let services = null; // GET /services: every service, configured and registered (null until first read)
let registry = null; // GET /registry: tenants (with placements) and nodes (with origin)
let directory = null; // GET /directory: every argus.peer-labelled container and configured peer, with health

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
  n.append(...kids.filter((k) => k != null && k !== false));
  return n;
}
const noRows = (cols, text) => [el("tr", {}, el("td", { colSpan: cols, class: "none", text }))];

// Narrow tables stack into cards; each cell carries its column's name for the label the CSS prints.
function labelCells(table) {
  const heads = [...table.tHead.rows[0].cells].map((th) => th.textContent.trim());
  for (const row of table.tBodies[0].rows) {
    [...row.cells].forEach((td, i) => { if (td.colSpan === 1 && heads[i]) td.dataset.label = heads[i]; });
  }
}
function fill(tbody, rows) {
  tbody.replaceChildren(...rows);
  labelCells(tbody.closest("table"));
}

// ---------- colour per service ----------
function slotFor(node, service) {
  let m = colours.get(node);
  if (!m) colours.set(node, (m = new Map()));
  if (!m.has(service)) m.set(service, m.size < SLOTS - 1 ? m.size + 1 : 0);
  return m.get(service);
}
const colourVar = (slot) => `var(--dh-cat-${slot || SLOTS})`; // slot 0 ("other") alone gets the last colour
const serviceList = () => services ?? config?.services ?? [];
const serviceById = (id) => serviceList().find((s) => s.id === id);
function seedColours(state) {
  // Known services first, in their order, so a service keeps its colour whether or not it is loaded.
  for (const s of serviceList()) if (s.node === state.name) slotFor(state.name, s.id);
  for (const r of state.residents) slotFor(state.name, r.service);
}

// ---------- rendering: GPUs ----------
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
  fill(tbody, rows.length ? rows.map((r) => {
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
  }) : noRows(7, "nothing resident"));
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
  const cell = el("td", { class: "actions" });
  const freed = ["unloaded", "cpu"].includes(r.detail?.model_state);
  const stop = r.source === "external" && serviceById(r.service)?.evict_by === "stop";
  if (r.source === "process" || (r.source === "external" && !stop) || freed) return cell;
  const btn = el("button", { type: "button", class: "btn ghost small", text: stop ? "Stop" : "Evict", disabled: r.busy,
    title: r.busy ? "busy: iris never interrupts work"
      : stop ? "stop its container: its clients fall back elsewhere; Warm starts it again"
      : "free its VRAM (to CPU where the service can, else unload)" });
  btn.addEventListener("click", () => runAction(`residents/${r.id.split("/").map(encodeURIComponent).join("/")}/evict`, r.id,
    btn, cell, { busyLabel: stop ? "Stopping…" : "Evicting…", summary: evictSummary }));
  cell.append(btn);
  const prev = lastResult.get(r.id);
  if (prev) cell.append(prev.cloneNode(true));
  return cell;
}
const evictSummary = (b) => `${{ evict: "moved to CPU", stop: "stopped" }[b.action] ?? "unloaded"}: ${gb(b.before_mb - b.after_mb)} freed`
  + (b.residual_mb ? `, ${gb(b.residual_mb)} still held (restart frees it)` : "") + (b.confirmed ? "" : " (not confirmed)");

function warmCell(id) {
  const c = serviceById(id);
  const cell = el("td", { class: "actions" });
  if (!c || !(c.warm_path || c.container)) return cell;
  const btn = el("button", { type: "button", class: "btn ghost small", text: "Warm", title: warmHint(c) });
  btn.addEventListener("click", () => runAction(`services/${encodeURIComponent(id)}/warm`, `warm:${id}`, btn, cell,
    { busyLabel: "Warming…", summary: warmSummary }));
  cell.append(btn);
  const prev = lastResult.get(`warm:${id}`);
  if (prev) cell.append(prev.cloneNode(true));
  return cell;
}
const warmHint = (c) => [c.container && `starts ${c.container} if it is down${c.managed === false ? " (if its agent allows it)" : ""}`,
  c.warm_path && `then calls ${c.warm_path}`].filter(Boolean).join(", ");

function renderServices(node, state) {
  const tbody = $("table.services tbody", node);
  fill(tbody, state.services.length ? state.services.map((s) => {
    const c = serviceById(s.id);
    const q = s.queue_running == null && s.queue_pending == null ? "–" : `${s.queue_running ?? 0} / ${s.queue_pending ?? 0}`;
    const status = el("td", {}, el("span", { class: s.up ? "status-up" : "status-down", text: s.up ? "up" : "down" }),
      s.error ? el("span", { class: "svc-err", text: s.error }) : null,
      s.crowded_out ? el("span", { class: "svc-err alert", title: "pins, unmanaged processes and unloaded residues: nothing iris can evict", text: `needs ${gb(s.need_mb)}, only ${gb(s.room_mb)} can ever be free` }) : null);
    return el("tr", {},
      el("td", {}, s.id, c?.origin === "tenant" ? el("span", { class: "sub", text: `registered by ${c.tenant}` }) : null),
      el("td", { class: "meta", text: s.adapter }), status,
      el("td", { class: "num", title: "running / pending", text: q }), warmCell(s.id));
  }) : noRows(5, "no services on this node"));
}

function renderNode(state) {
  let node = document.getElementById(`node-${state.name}`);
  if (!node) {
    node = $("#node-tpl").content.firstElementChild.cloneNode(true);
    node.id = `node-${state.name}`;
    for (const wrap of node.querySelectorAll(".table-wrap")) {
      wrap.setAttribute("aria-label", `${state.name} ${$("caption", wrap).textContent.toLowerCase()}`);
    }
    placeNode(node, state.name);
  }
  seedColours(state);
  node.dataset.reachable = String(state.reachable);
  $(".node-name", node).textContent = state.name;
  $(".gpu", node).textContent = state.gpu ? `${state.gpu.name} · ${gb(state.gpu.total_mb)}` : "";
  const reach = $(".reach", node);
  reach.dataset.state = state.reachable ? "ok" : "bad";
  reach.textContent = state.reachable ? "agent up" : "agent unreachable";
  $(".origin", node).hidden = nodeOrigin(state.name) !== "agent";
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
  fill($("table.leases tbody", node), rows.length ? rows.map((l) => {
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
  }) : noRows(7, "no leases"));
  renderSummary();
}

function placeNode(node, name) {
  const main = $("#nodes");
  $("#placeholder")?.remove();
  const idx = order.indexOf(name);
  const after = order.slice(idx + 1).map((n) => document.getElementById(`node-${n}`)).find(Boolean);
  main.insertBefore(node, after ?? null);
}

// One card per node above the detail: how full it is, its leases, and anything that needs a look. Links to the node.
function renderSummary() {
  const cards = order.filter((n) => nodes.has(n)).map((name) => {
    const s = nodes.get(name);
    const L = s.ledger;
    const mine = [...leases.values()].filter((l) => l.node === name);
    const granted = mine.filter((l) => l.state === "granted").length;
    const queued = mine.length - granted;
    const crowded = s.services.filter((x) => x.crowded_out).map((x) => x.id);
    const down = s.services.filter((x) => !x.up).map((x) => x.id);
    const mini = el("div", { class: "mini", "aria-hidden": "true" });
    if (L?.total_mb) {
      for (const p of segments(s)) {
        if (p.cls === "free" || p.cls === "held") continue;
        mini.append(el("span", { class: p.cls, style: { "--c": p.c ?? "", "flex-basis": `${(p.mb / L.total_mb) * 100}%` } }));
      }
    }
    const used = L ? L.total_mb - L.free_mb : null;
    const notes = [
      granted || queued ? `${granted} granted${queued ? `, ${queued} queued` : ""}` : "no leases",
      down.length ? el("span", { class: "alert", text: `down: ${down.join(", ")}` }) : null,
      crowded.length ? el("span", { class: "alert", text: `crowded out: ${crowded.join(", ")}` }) : null,
    ].filter(Boolean).flatMap((n, i) => (i ? [" · ", n] : [n]));
    return el("a", { class: "sum", href: `#node-${name}`, "data-reachable": String(s.reachable),
      "aria-label": `${name}: ${s.reachable ? (L ? `${gb(used)} of ${gb(L.total_mb)} used` : "up") : "agent unreachable"}` },
      el("div", { class: "top" }, el("span", { class: "name", text: name }),
        s.reachable ? null : el("span", { class: "pill", "data-state": "bad", text: "unreachable" }),
        el("span", { class: "meta", text: L ? `${gb(used)} / ${gb(L.total_mb)}` : "–" })),
      mini, el("div", { class: "notes" }, ...notes));
  });
  $("#summary").replaceChildren(...cards);
  const alerts = [...nodes.values()].some((s) => !s.reachable || s.services.some((x) => x.crowded_out));
  const count = $("#count-gpus");
  count.textContent = nodes.size ? String(nodes.size) : "";
  count.toggleAttribute("data-alert", alerts);
}

function renderAgents() {
  const all = [...nodes.values()];
  const up = all.filter((n) => n.reachable).length;
  const pill = $("#agents");
  pill.textContent = `${up}/${all.length || order.length} agents`;
  pill.dataset.state = !all.length ? "unknown" : up === all.length ? "ok" : up ? "warn" : "bad";
}

// ---------- rendering: services, tenants, nodes (the registry) ----------
const nodeOrigin = (name) => registry?.nodes?.find((n) => n.name === name)?.origin;
const serviceState = (s) => nodes.get(s.node)?.services.find((x) => x.id === s.id);

function renderRegistry() {
  if (!services || !registry) return;
  const cnt = $("#count-services");
  cnt.textContent = String(services.length);
  cnt.toggleAttribute("data-alert", registry.tenants.some((t) => t.placements.some((p) => p.reason)));

  fill($("table.services-all tbody"), services.length ? services.map((s) => {
    const st = serviceState(s);
    const origin = s.origin === "tenant"
      ? el("td", {}, el("span", { class: "chip tenant", text: "registered" }),
        el("span", { class: "sub", text: s.tenant }), s.lapsed ? el("span", { class: "svc-err alert", text: "lapsed: kept while its container exists" }) : null)
      : el("td", {}, el("span", { class: "chip", text: "config" }));
    const status = st
      ? el("td", {}, el("span", { class: st.up ? "status-up" : "status-down", text: st.up ? "up" : "down" }),
        st.crowded_out ? el("span", { class: "svc-err alert", text: `crowded out: needs ${gb(st.need_mb)}` }) : null)
      : el("td", { class: "meta", text: "not polled yet" });
    const control = [
      s.managed === true ? "agent can start it" : s.managed === false ? "agent won't start it" : null,
      s.admin_token === true ? "admin token" : s.admin_token === false ? "no admin token" : null,
    ].filter(Boolean);
    return el("tr", {},
      el("td", {}, s.id, s.serves?.length ? el("span", { class: "sub", text: `also answers ${s.serves.join(", ")}` }) : null),
      el("td", { text: s.node }),
      origin,
      el("td", {}, el("code", { text: s.base_url }), s.container ? el("span", { class: "sub", text: `container ${s.container}` }) : null),
      status,
      el("td", { class: "meta" }, s.adapter, ...control.map((t) => el("span", { class: "sub", text: t }))),
      warmCell(s.id));
  }) : noRows(7, "no services yet"));

  fill($("table.tenants tbody"), registry.tenants.length ? registry.tenants.map((t) => {
    const cell = el("td", { class: "actions" });
    const btn = el("button", { type: "button", class: "btn ghost small", text: "Forget",
      title: "drop its registration and services now; a running client registers again on its next renewal" });
    btn.addEventListener("click", () => forget(`tenants/${encodeURIComponent(t.tenant)}?purge=true`, `tenant:${t.tenant}`, btn, cell,
      `Forget ${t.tenant}?`, `Its registration and the ${t.placements.filter((p) => p.service).length} service(s) it brought go now. If it is still running it registers again within five minutes.`));
    cell.append(btn);
    return el("tr", {},
      el("td", {}, t.tenant, t.repo ? el("span", { class: "sub", text: t.repo }) : null),
      el("td", {}, t.placements.length
        ? el("ul", { class: "placements" }, ...t.placements.map((p) => el("li", {},
          el("strong", { text: p.resident }), " → ",
          p.service ? `${p.node}/${p.service}` : p.covered_by ? `${p.node}: read by config's ${p.covered_by}` : el("span", { class: "why", text: p.reason }))))
        : el("span", { class: "meta", text: "no residents (leases only)" })),
      el("td", { class: "meta", text: t.kinds.join(", ") || "–" }),
      el("td", { class: "num" }, `${dur(since(t.seen_at))} ago`,
        t.lapsed ? el("span", { class: "sub", text: "lapsed" }) : null,
        t.needs_admin_token && !t.admin_token ? el("span", { class: "sub alert", text: "no admin token: its admin routes will refuse iris" }) : null),
      cell);
  }) : noRows(5, "No tenant has registered yet. Services register when their cortex client starts, or with their first lease."));

  fill($("table.node-list tbody"), registry.nodes.map((n) => {
    const cell = el("td", { class: "actions" });
    if (n.origin === "agent") {
      const btn = el("button", { type: "button", class: "btn ghost small", text: "Forget" });
      btn.addEventListener("click", () => forget(`nodes/${encodeURIComponent(n.name)}`, `node:${n.name}`, btn, cell,
        `Forget node ${n.name}?`, "iris stops polling it and its services. If its agent is still running it announces itself again within a minute."));
      cell.append(btn);
    }
    const st = nodes.get(n.name);
    return el("tr", {},
      el("td", {}, n.name, st?.gpu ? el("span", { class: "sub", text: st.gpu.name }) : null),
      el("td", {}, el("span", { class: n.origin === "agent" ? "chip tenant" : "chip", text: n.origin === "agent" ? "announced" : "config" })),
      el("td", {}, el("code", { text: n.agent_url }),
        st ? el("span", { class: `sub ${st.reachable ? "status-up" : "status-down"}`, text: st.reachable ? "up" : "unreachable" }) : null),
      el("td", { class: "num", text: n.containers ?? "–", title: n.containers == null ? "the agent does not list containers (older than this broker?)" : null }),
      cell);
  }));
}

// The Suite view: one row per labelled service, healthiest problems first so a failing one is at the top.
const HEALTH_RANK = { down: 0, starting: 1, degraded: 2, ok: 3 };
function renderSuite() {
  const tbody = $("table.suite tbody");
  const cnt = $("#count-suite");
  if (!directory) { fill(tbody, noRows(6, "This broker has no service directory.")); cnt.textContent = ""; return; }
  const status = (e) => (!e.running ? "stopped" : !e.url ? "unreachable" : e.health?.status ?? "unchecked");
  const rank = (e) => HEALTH_RANK[status(e)] ?? -1;
  const rows = [...directory].sort((a, b) => rank(a) - rank(b) || a.peer.localeCompare(b.peer) || (a.node ?? "").localeCompare(b.node ?? ""));
  const bad = rows.filter((e) => e.running && ["down", "degraded", "unreachable"].includes(status(e))).length;
  cnt.textContent = rows.length ? String(rows.length) : "";
  cnt.toggleAttribute("data-alert", bad > 0);
  fill(tbody, rows.length ? rows.map((e) => {
    const h = e.health;
    const st = status(e);
    const cls = st === "ok" ? "status-up" : ["down", "unreachable"].includes(st) ? "status-down" : "";
    const failing = Object.entries(h?.checks ?? {}).filter(([, c]) => !c.ok);
    return el("tr", {},
      el("td", {}, e.peer, e.origin === "config" ? el("span", { class: "sub", text: "from the broker config" }) : null),
      el("td", {}, e.service ?? el("span", { class: "meta", text: "–" }),
        h?.version ? el("span", { class: "sub", text: `v${h.version}${h.contract ? "" : " (off contract)"}` })
          : h && !h.contract ? el("span", { class: "sub", text: "no ServiceHealth body" }) : null),
      el("td", { text: e.node ?? "–" }),
      el("td", {}, e.url ? el("code", { text: e.url }) : el("span", { class: "svc-err alert", text: e.reason ?? "not reachable" }),
        e.container ? el("span", { class: "sub", text: `container ${e.container}` }) : null),
      el("td", {}, el("span", { class: cls, text: st }),
        h?.error ? el("span", { class: "svc-err", text: h.error }) : null,
        ...failing.map(([name, c]) => el("span", { class: "svc-err alert", text: `${name}: ${c.detail ?? "failing"}` })),
        h?.capabilities?.length ? el("span", { class: "sub", text: h.capabilities.join(", ") }) : null),
      el("td", { class: "num", "data-idle": h ? since(h.checked_at) : "", "data-at": Date.now(), text: h ? dur(since(h.checked_at)) : "–" }));
  }) : noRows(6, "Nothing labelled yet. Add labels: {argus.peer: <name>} to a service's compose file."));
}

let registryTimer = null;
async function loadRegistry() {
  try {
    const [s, r, d] = await Promise.all([fetch(api("services")), fetch(api("registry")), fetch(api("directory"))]);
    if (!s.ok || !r.ok) throw new Error(`HTTP ${s.ok ? r.status : s.status}`);
    [services, registry] = [await s.json(), await r.json()];
    directory = d.ok ? await d.json() : null; // an older broker has no directory: the Suite tab says so
  } catch (e) {
    if (!services) fill($("table.services-all tbody"), noRows(7, `Could not read the registry: ${e.message}`));
    return;
  }
  // A forgotten node: drop its card and summary.
  const known = new Set(registry.nodes.map((n) => n.name));
  for (const name of [...nodes.keys()]) {
    if (known.has(name)) continue;
    nodes.delete(name);
    order.splice(order.indexOf(name), 1);
    document.getElementById(`node-${name}`)?.remove();
  }
  renderRegistry();
  renderSuite();
  renderAgents();
  for (const s of nodes.values()) renderNode(s); // origins, warm buttons and colours follow the registry
}
function scheduleRegistry(ms = 1500) {
  clearTimeout(registryTimer);
  registryTimer = setTimeout(loadRegistry, ms);
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
setInterval(loadRegistry, 20000);

// ---------- actions ----------
const lastResult = new Map(); // action key -> result element (kept across re-renders)
const warmSummary = (b) => (b.up ? "" : "still down: ")
  + (b.detail ?? [b.started_container && "container started", b.warmed && "warmed", `up after ${b.waited_s.toFixed(1)}s`].filter(Boolean).join(", "));
async function runAction(path, key, btn, cell, { method = "POST", busyLabel, summary }) {
  let token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) token = await askToken();
  if (!token) return null;
  const label = btn.textContent;
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  btn.textContent = busyLabel;
  let msg, ok = false;
  try {
    const res = await fetch(api(path), { method, headers: { Authorization: `Bearer ${token}` } });
    const body = await res.json().catch(() => ({}));
    if (res.status === 401) { sessionStorage.removeItem(TOKEN_KEY); showTokenState(); }
    ok = res.ok && body.up !== false && body.confirmed !== false;
    msg = el("span", { class: `result ${ok ? "ok" : "err"}`, role: "status",
      text: res.ok ? summary(body) : `${res.status}: ${body.detail ?? res.statusText}` });
  } catch (e) {
    msg = el("span", { class: "result err", role: "status", text: String(e) });
  }
  lastResult.set(key, msg);
  btn.disabled = false;
  btn.removeAttribute("aria-busy");
  btn.textContent = label;
  cell.querySelector(".result")?.remove();
  cell.append(msg.cloneNode(true));
  return ok;
}

const confirmDlg = $("#confirm-dialog");
function confirmForget(title, body) {
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  confirmDlg.returnValue = "";
  confirmDlg.showModal();
  return new Promise((resolve) => confirmDlg.addEventListener("close", () => {
    resolve(confirmDlg.returnValue === "confirm");
    confirmDlg.returnValue = "";
  }, { once: true }));
}
async function forget(path, key, btn, cell, title, body) {
  if (!(await confirmForget(title, body))) return;
  const ok = await runAction(path, key, btn, cell, { method: "DELETE", busyLabel: "Forgetting…", summary: () => "forgotten" });
  if (ok) loadRegistry();
}

// ---------- token dialog ----------
const dlg = $("#token-dialog");
let tokenResolve = null;
function askToken() {
  $("#token-input").value = sessionStorage.getItem(TOKEN_KEY) ?? "";
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((r) => (tokenResolve = r));
}
function showTokenState() {
  const set = Boolean(sessionStorage.getItem(TOKEN_KEY));
  $("#token-btn").textContent = set ? "Broker token ✓" : "Broker token";
  $("#token-state").textContent = set ? "set for this tab" : "not set";
}
$("#token-btn").addEventListener("click", () => askToken());
$("#token-clear").addEventListener("click", () => { sessionStorage.removeItem(TOKEN_KEY); $("#token-input").value = ""; dlg.close(""); });
$("#token-form").addEventListener("submit", (e) => {
  if (e.submitter?.value !== "save") return;
  const v = $("#token-input").value.trim();
  if (v) sessionStorage.setItem(TOKEN_KEY, v);
});
dlg.addEventListener("close", () => {
  const v = dlg.returnValue === "save" ? sessionStorage.getItem(TOKEN_KEY) : null;
  dlg.returnValue = "";
  tokenResolve?.(v);
  tokenResolve = null;
  showTokenState();
});
showTokenState();

// ---------- tabs ----------
// Selection follows focus (arrow keys, Home, End). Hidden panels stay findable with find-in-page where supported.
const tabs = [...document.querySelectorAll('[role="tab"]')];
const panelOf = (tab) => document.getElementById(tab.id.replace("tab-", "panel-"));
function select(tab, focus = false) {
  for (const t of tabs) {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    const panel = panelOf(t);
    if (on) panel.removeAttribute("hidden");
    else panel.hidden = "until-found";
  }
  if (focus) tab.focus();
  tab.scrollIntoView({ block: "nearest", inline: "nearest" });
  try { sessionStorage.setItem("iris-tab", tab.id); } catch { /* storage blocked: the tab is not remembered */ }
  if (tab.id === "tab-gpus") requestAnimationFrame(() => { for (const b of document.querySelectorAll(".seg-bar")) fitLabels(b); });
}
for (const tab of tabs) {
  tab.addEventListener("click", () => select(tab));
  tab.addEventListener("keydown", (e) => {
    const i = tabs.indexOf(tab);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (next == null) return;
    e.preventDefault();
    select(tabs[(next + tabs.length) % tabs.length], true);
  });
  panelOf(tab).addEventListener("beforematch", () => select(tab));
}
for (const t of tabs) if (t.getAttribute("aria-selected") !== "true") panelOf(t).hidden = "until-found";
// A summary card links to its node: show the GPUs view first.
document.addEventListener("click", (e) => { if (e.target.closest?.('a[href^="#node-"]')) select(tabs[0]); });
{
  let saved = null;
  try { saved = sessionStorage.getItem("iris-tab"); } catch { /* ignore */ }
  // A link to /ui/#services or #connect opens that view; otherwise the one this tab last showed.
  const linked = tabs.find((t) => `#${t.id.replace("tab-", "")}` === location.hash);
  const tab = linked ?? tabs.find((t) => t.id === saved);
  if (tab) select(tab);
}

// ---------- copy buttons ----------
const copyTimers = new WeakMap();
let lastCopyMessage = "";
function legacyCopy(text) {
  // The dashboard is usually plain http on the LAN, where navigator.clipboard does not exist.
  const field = Object.assign(document.createElement("textarea"), { value: text });
  field.setAttribute("readonly", "");
  field.style.cssText = "position:fixed;inset-block-start:0;opacity:0";
  document.body.append(field);
  field.select();
  try { return document.execCommand("copy"); } finally { field.remove(); }
}
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return legacyCopy(text);
  }
}
document.addEventListener("click", async (e) => {
  const btn = e.target.closest?.("button[data-copy]");
  if (!btn) return;
  const ok = await copyText(document.getElementById(btn.dataset.copy).innerText.trim());
  const tip = btn.nextElementSibling;
  clearTimeout(copyTimers.get(btn));
  btn.removeAttribute("data-copied");
  btn.removeAttribute("data-failed");
  btn.setAttribute(ok ? "data-copied" : "data-failed", "");
  let message = ok ? "Copied" : "Copy failed: select the text instead";
  if (message === lastCopyMessage) message += " "; // VoiceOver skips a repeat of the same message
  lastCopyMessage = message;
  tip.textContent = "";
  requestAnimationFrame(() => (tip.textContent = message));
  copyTimers.set(btn, setTimeout(() => {
    btn.removeAttribute("data-copied");
    btn.removeAttribute("data-failed");
    tip.textContent = "";
  }, 2000));
});
function renderSnippets() {
  const origin = new URL(api("")).origin;
  $("#snippet-env").textContent = [
    `ARGUS_GPU_BROKER_URL=${origin}`,
    "ARGUS_GPU_BROKER_TOKEN=<the broker's ARGUS_GPU_BROKER_TOKEN>",
    "# only if its manifest has admin_auth residents (usually already set):",
    "ARGUS_ADMIN_TOKEN=<the service's own admin token>",
  ].join("\n");
  $("#snippet-agent").textContent = [
    "IRIS_AGENT_NODE=<a short name for this GPU, e.g. jarrah>",
    "IRIS_AGENT_TOKEN=<the broker's IRIS_AGENT_TOKEN>",
    `IRIS_BROKER_URL=${origin}`,
    "IRIS_AGENT_MANAGE_LABEL=1   # optional: allow containers labelled argus.iris.manage=true",
  ].join("\n");
}
renderSnippets();

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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") tip.hidden = true; });

// ---------- data ----------
// dh-theme dispatches its change event on document, and it does not bubble to window.
document.addEventListener("dh-theme-change", () => requestAnimationFrame(() => inkFor(document)));
window.addEventListener("resize", () => { for (const b of document.querySelectorAll(".seg-bar")) fitLabels(b); });

// The browser retries a dropped stream by itself, but gives up for good (CLOSED) on an answer that is not an event
// stream, such as a proxy's 502 while the broker restarts: then open a new one, backing off up to 30 s.
function connectEvents(setLive, handlers, delay = 1000) {
  const es = new EventSource(api("events"));
  for (const [name, fn] of Object.entries(handlers)) es.addEventListener(name, fn);
  es.addEventListener("open", () => { delay = 1000; });
  es.addEventListener("error", () => {
    if (es.readyState !== EventSource.CLOSED) return setLive("warn", "reconnecting");
    es.close();
    setLive("bad", `disconnected: retrying in ${Math.round(delay / 1000)}s`);
    setTimeout(() => connectEvents(setLive, handlers, Math.min(delay * 2, 30000)), delay);
  });
}

async function boot() {
  try {
    const [h, c] = await Promise.all([fetch(api("health")).then((r) => r.json()), fetch(api("config")).then((r) => r.json())]);
    config = c;
    order.push(...Object.keys(c.nodes));
    $("#version").textContent = `v${h.version}`;
    const mode = $("#mode");
    mode.textContent = c.admission === "enforce" ? "admission · enforce" : "observe-only";
    mode.dataset.mode = c.admission;
    mode.title = c.admission === "enforce" ? "admission enforced: leases queue, spill and evict" : "observe-only: every lease is granted, and whether it would have fit is recorded";
  } catch (e) {
    $("#placeholder").textContent = `Could not reach the broker: ${e}`;
  }
  loadRegistry();
  const live = $("#live");
  const setLive = (state, text) => { live.dataset.state = state; live.lastElementChild.textContent = text; };
  connectEvents(setLive, {
    open: () => {
      setLive("ok", "live");
      // The stream resends every active lease on connect; drop what we had so leases that ended meanwhile go.
      const had = new Set([...leases.values()].map((l) => l.node));
      leases.clear();
      for (const name of had) if (name) renderLeases(name);
    },
    lease: (e) => {
      const l = JSON.parse(e.data);
      const was = leases.get(l.lease_id);
      if (l.state === "queued" || l.state === "granted") leases.set(l.lease_id, l);
      else leases.delete(l.lease_id);
      if (was?.node && was.node !== l.node) renderLeases(was.node);
      if (l.node) renderLeases(l.node);
    },
    node: (e) => {
      const state = JSON.parse(e.data);
      if (!order.includes(state.name)) { order.push(state.name); scheduleRegistry(0); }
      const was = nodes.get(state.name);
      nodes.set(state.name, state);
      renderNode(state);
      renderAgents();
      renderSummary();
      // A service appearing or going on a node is a registry change: re-read it soon.
      const ids = (s) => s?.services.map((x) => x.id).join() ?? "";
      if (ids(was) !== ids(state)) scheduleRegistry();
      else if (services) renderRegistry();
    },
  });
}
boot();
