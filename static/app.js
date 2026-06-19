/* spoolman-cfs-sync — UI behavior (redesigned shell, same backend API)
 *
 * All fetch endpoints, payload shapes and state fields are unchanged.
 * Endpoints used:
 *   GET  /api/ui/state
 *   GET  /api/ui/spoolman/spools
 *   POST /api/ui/spool/set_start            { slot, start_g }
 *   POST /api/ui/spool/set_remaining        { slot, remaining_g }
 *   POST /api/ui/spoolman/mapping           { slot, spool_id|null }
 *   POST /api/ui/spoolman/config            { url, enabled, dry_run, sync_mode, live_min_delta_mm }
 *   POST /api/ui/spoolman/test              {}
 *   POST /api/ui/spoolman/retry             { record_key }
 *   POST /api/ui/printer/config             { moonraker_url, poll_interval_sec, filament_diameter_mm, cfs_autosync }
 *   POST /api/ui/accounting/clear           {}
 */

"use strict";

const $ = (id) => document.getElementById(id);
const DEBUG_MODE_KEY = "spoolmanCfsSyncDebugMode";

let latestState = null;
let spoolmanSpools = [];
let spoolmanSpoolsLoadingPromise = null;
let spoolmanSpoolsLoadedAt = 0;

let spoolmanPickerSlot = null;
let spoolmanPickerFilter = "";
let spoolmanPickerOpen = false;

let spoolModalOpen = false;
let spoolSlotId = null;
let spoolPrevPaused = null;

let settingsModalOpen = false;

/* ---------- helpers ---------- */

function isDebugMode() {
  try { return localStorage.getItem(DEBUG_MODE_KEY) === "1"; }
  catch { return false; }
}
function setDebugMode(enabled) {
  try { localStorage.setItem(DEBUG_MODE_KEY, enabled ? "1" : "0"); }
  catch { /* ignore */ }
}

function fmtTs(ts) {
  if (!ts) return "—";
  try { return new Date(Number(ts) * 1000).toLocaleString(); }
  catch { return "—"; }
}
function fmtAgo(value) {
  if (!value) return "—";
  const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  const t = d.getTime();
  if (!Number.isFinite(t)) return "—";
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 48) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
function fmtMm(mm) {
  const m = Number(mm || 0) / 1000.0;
  return (m >= 10 ? m.toFixed(1) : m.toFixed(2)) + " m";
}
function fmtG(g) {
  if (g == null) return "0 g";
  const gg = Number(g);
  if (!Number.isFinite(gg)) return "0 g";
  if (gg >= 100) return gg.toFixed(0) + " g";
  if (gg >= 10) return gg.toFixed(1) + " g";
  return gg.toFixed(2) + " g";
}
function normalizeColor(value, fallback = "#3f3f46") {
  let c = String(value || "").trim();
  if (!c) return fallback;
  if (!c.startsWith("#")) c = "#" + c;
  return /^#[0-9a-fA-F]{6}$/.test(c) ? c.toUpperCase() : fallback;
}
function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = opts.text;
  if (opts.html != null) node.innerHTML = opts.html;
  if (opts.title) node.title = opts.title;
  if (opts.attrs) for (const k in opts.attrs) node.setAttribute(k, opts.attrs[k]);
  if (opts.style) Object.assign(node.style, opts.style);
  if (opts.on) for (const k in opts.on) node.addEventListener(k, opts.on[k]);
  for (const c of [].concat(children)) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

async function postJson(url, payload) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!r.ok) throw new Error(await r.text().catch(() => `HTTP ${r.status}`));
  return r.json();
}
async function getJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(await r.text().catch(() => `HTTP ${r.status}`));
  return r.json();
}

/* ---------- slot / spool helpers ---------- */

function buildSlotIds(connectedBoxes) {
  const ids = [];
  for (const b of connectedBoxes || []) for (const l of ["A","B","C","D"]) ids.push(`${b}${l}`);
  return ids;
}
function connectedBoxesFor(slots) {
  const boxesInfo = (slots && slots._boxes) ? slots._boxes : {};
  const boxes = [];
  for (const n of ["1","2","3","4"]) if (boxesInfo[n] && boxesInfo[n].connected === true) boxes.push(n);
  if (!boxes.length) boxes.push("1");
  return boxes;
}
function slotMeta(state, slots, sid) {
  const remote = (slots && slots[sid]) ? slots[sid] : {};
  const local  = (state.slots && state.slots[sid]) ? state.slots[sid] : {};
  return {
    present: remote.present ?? local.present ?? true,
    material: String((remote.material ?? local.material) || "").toUpperCase(),
    color: normalizeColor(remote.color ?? remote.color_hex ?? local.color ?? local.color_hex, "#3f3f46"),
    name: String((remote.name ?? local.name) || ""),
    vendor: String((remote.vendor ?? local.manufacturer) || ""),
    remaining_g: local.remaining_g ?? null,
    spool_remaining_g: local.spool_remaining_g ?? null,
    spool_used_g: local.spool_used_g ?? null,
    spool_consumed_g: local.spool_consumed_g ?? null,
    spool_epoch: local.spool_epoch ?? 0,
  };
}

function spoolColor(spool, fallback = "#6b7280") {
  const fil = spool && typeof spool.filament === "object" ? spool.filament : {};
  return normalizeColor(spool?.color_hex ?? spool?.color ?? fil.color_hex ?? fil.color, fallback);
}
function spoolVendor(spool) {
  const fil = spool && typeof spool.filament === "object" ? spool.filament : {};
  const vendor = fil.vendor;
  if (typeof vendor === "string") return vendor;
  if (vendor && typeof vendor === "object") return vendor.name || vendor.title || "";
  return spool?.vendor || "";
}
function spoolFilamentName(spool) {
  const fil = spool && typeof spool.filament === "object" ? spool.filament : {};
  return spool?.filament_name || spool?.name || fil.name || fil.label || "Filament";
}
function spoolMaterial(spool) {
  const fil = spool && typeof spool.filament === "object" ? spool.filament : {};
  return String(spool?.material || fil.material || "—").toUpperCase();
}
function spoolDisplayName(spool) {
  if (!spool) return "No spool selected";
  return [spoolVendor(spool), spoolFilamentName(spool)].filter(Boolean).join(" ") || `Spool #${spool.id}`;
}
function spoolWeightText(spool) {
  if (!spool) return "—";
  const remaining = spool.remaining_weight ?? spool.remaining_weight_g ?? spool.weight_remaining;
  const initial   = spool.initial_weight   ?? spool.initial_weight_g   ?? spool.weight;
  const left = remaining != null ? fmtG(remaining) : "—";
  return initial != null ? `${left} / ${fmtG(initial)}` : left;
}
function spoolRemainingWeight(spool) {
  if (!spool) return null;
  const value = spool.remaining_weight ?? spool.remaining_weight_g ?? spool.weight_remaining;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}
function findSpool(id) {
  const sid = Number(id || 0);
  if (!sid) return null;
  return spoolmanSpools.find((s) => Number(s.id) === sid) || null;
}

async function loadSpoolmanSpools(force = false) {
  if (spoolmanSpools.length && !force) return spoolmanSpools;
  if (spoolmanSpoolsLoadingPromise) return spoolmanSpoolsLoadingPromise;
  const status = $("spoolmanPickerStatus");
  if (status) status.textContent = "Loading spools…";
  spoolmanSpoolsLoadingPromise = (async () => {
    const j = await getJson("/api/ui/spoolman/spools");
    const result = j.result || j;
    spoolmanSpools = Array.isArray(result.spools) ? result.spools : [];
    spoolmanSpoolsLoadedAt = Date.now();
    if (status) status.textContent = `${spoolmanSpools.length} spool${spoolmanSpools.length === 1 ? "" : "s"} available`;
    return spoolmanSpools;
  })();
  try { return await spoolmanSpoolsLoadingPromise; }
  finally { spoolmanSpoolsLoadingPromise = null; }
}
function ensureSpoolmanSpoolsLoadedForPanel(cfg) {
  if (!cfg || !cfg.url) return;
  const mappings = cfg.slot_mappings || {};
  const hasMappedSlot = Object.values(mappings).some((v) => Number(v || 0) > 0);
  if (!hasMappedSlot) return;
  const freshEnough = spoolmanSpools.length && (Date.now() - spoolmanSpoolsLoadedAt) < 60000;
  if (freshEnough || spoolmanSpoolsLoadingPromise) return;
  loadSpoolmanSpools(true)
    .then(() => { if (latestState) render(latestState); })
    .catch(() => { /* ignore */ });
}

/* ---------- connection strip ---------- */

function printerErrorLabel(err) {
  const s = String(err || "").trim().toLowerCase();
  if (!s) return "";
  if (s.includes("timed out") || s.includes("timeout")) return "timed out";
  if (s.includes("connection refused")) return "refused";
  if (s.includes("no route to host") || s.includes("network is unreachable")) return "unreachable";
  if (s.includes("name or service not known") || s.includes("getaddrinfo")) return "host not found";
  return "error";
}
function setCStat(id, state, value) {
  const node = $(id);
  if (!node) return;
  node.dataset.state = state;
  const v = node.querySelector(".cvalue");
  if (v) v.textContent = value;
}

/* ---------- CFS slot rendering ---------- */

function slotStatusChip(meta, isActive) {
  if (meta.present === false) return el("span", { class: "chip chip-muted", text: "empty" });
  if (isActive) return el("span", { class: "chip chip-accent", text: "active" });
  return el("span", { class: "chip chip-muted", text: "ready" });
}

function buildSlotCard(state, slots, sid, isActive) {
  const meta = slotMeta(state, slots, sid);
  const cfg = state.spoolman_config || {};
  const mappings = cfg.slot_mappings || {};
  const mapped = mappings[sid];
  const spool = findSpool(mapped);
  const swatch = el("div", { class: "slot-swatch" + (meta.present === false ? " absent" : "") });
  if (meta.present !== false) swatch.style.background = meta.color;

  const main = el("div", { class: "slot-main" });
  const idRow = el("div", { class: "slot-id-row" }, [
    el("span", { class: "slot-id", text: sid }),
    slotStatusChip(meta, isActive),
  ]);
  main.appendChild(idRow);

  if (meta.present === false) {
    main.appendChild(el("div", { class: "slot-name", text: "Empty slot" }));
  } else {
    main.appendChild(el("div", { class: "slot-name", text: meta.name || meta.material || "Unnamed filament" }));
  }

  const subBits = ["CFS"];
  if (meta.present !== false) {
    if (meta.material && meta.material !== "—") subBits.push(meta.material);
    if (meta.vendor) subBits.push(meta.vendor);
    if (meta.color) subBits.push(meta.color);
  } else {
    subBits.push("Empty");
  }
  main.appendChild(el("div", { class: "slot-sub", text: subBits.join(" · ") }));

  const mapping = el("div", { class: "slot-mapping" });
  if (mapped) {
    mapping.appendChild(el("span", { class: "spool-tag", text: `#${mapped}` }));
    if (spool) {
      const sColor = spoolColor(spool);
      const sMat = spoolMaterial(spool);
      mapping.appendChild(el("span", { class: "spool-mini-swatch", style: { background: sColor } }));
      mapping.appendChild(el("span", {
        class: "slot-mapping-text",
        text: `Spoolman · ${spoolDisplayName(spool)}${sMat && sMat !== "—" ? " · " + sMat : ""} · ${sColor}`,
      }));
    } else {
      mapping.appendChild(el("span", { class: "slot-mapping-text", text: "Spoolman · loading…" }));
    }
  } else {
    mapping.appendChild(el("span", { class: "spool-unmapped", text: "No spool mapped" }));
  }
  main.appendChild(mapping);

  const spoolmanRem = spoolRemainingWeight(spool);
  const localRem = meta.spool_remaining_g != null ? meta.spool_remaining_g : meta.remaining_g;
  const rem = spool ? spoolmanRem : localRem;
  const remBox = el("div", { class: "slot-remaining" });
  if (spool) {
    remBox.appendChild(el("div", { class: "slot-remaining-value", text: spoolWeightText(spool) }));
    remBox.appendChild(el("div", { class: "slot-remaining-source", text: "Spoolman" }));
    remBox.title = "Spoolman remaining / initial weight";
    const r = Number(rem);
    if (Number.isFinite(r)) {
      if (r <= 50) remBox.classList.add("crit");
      else if (r <= 150) remBox.classList.add("low");
    }
  } else if (rem != null) {
    const r = Number(rem);
    remBox.appendChild(el("div", { class: "slot-remaining-value", text: fmtG(r) }));
    remBox.appendChild(el("div", { class: "slot-remaining-source", text: "Local" }));
    remBox.title = "Local remaining weight reference";
    if (Number.isFinite(r)) {
      if (r <= 50) remBox.classList.add("crit");
      else if (r <= 150) remBox.classList.add("low");
    }
  } else {
    remBox.textContent = "—";
    remBox.style.color = "var(--text-mute)";
  }

  const actions = el("div", { class: "slot-actions" });
  const mapBtn = el("button", {
    class: "btn btn-secondary btn-mini",
    attrs: { type: "button" },
    text: mapped ? "Change spool" : "Map spool",
    on: { click: (ev) => { ev.stopPropagation(); openSpoolmanPicker(sid); } },
  });
  actions.appendChild(mapBtn);
  if (mapped) {
    const clearBtn = el("button", {
      class: "btn btn-danger btn-mini",
      attrs: { type: "button" },
      text: "Unmap",
      on: { click: async (ev) => {
        ev.stopPropagation();
        await postJson("/api/ui/spoolman/mapping", { slot: sid, spool_id: null });
        await tick();
      } },
    });
    actions.appendChild(clearBtn);
  }
  const bookBtn = el("button", {
    class: "btn btn-ghost btn-mini",
    attrs: { type: "button" },
    text: "Weight",
    title: "Set local weight reference (does not affect Spoolman or printer)",
    on: { click: (ev) => { ev.stopPropagation(); openSpoolModal(sid, meta); } },
  });
  actions.appendChild(bookBtn);

  const card = el("div", { class: "slot-card" + (isActive ? " is-active" : "") }, [swatch, main, remBox, actions]);
  card.dataset.slot = sid;
  return card;
}

/* ---------- modals: spool bookkeeping ---------- */

function openSpoolModal(slotId, meta) {
  const modal = $("spoolModal");
  if (!modal) return;
  spoolModalOpen = true;
  spoolSlotId = slotId;
  if (spoolPrevPaused === null) spoolPrevPaused = refreshPaused;
  refreshPaused = true;
  applyRefreshTimer();

  $("spoolTitle").textContent = `Box ${slotId[0]} · Slot ${slotId[1]}`;
  $("spoolSub").textContent = [meta.name, meta.material, meta.color].filter(Boolean).join(" · ") || "—";

  const rem = meta.spool_remaining_g != null ? meta.spool_remaining_g : meta.remaining_g;
  $("spoolStart").value = "";
  $("spoolRemain").value = rem != null ? String(Math.round(Number(rem))) : "";

  const stats = $("spoolStats");
  if (stats) {
    const used = meta.spool_used_g;
    const total = meta.spool_consumed_g;
    if (rem != null && used != null) stats.textContent = `Remaining ${fmtG(rem)} · Used since last apply ${fmtG(used)} · Current spool total ${fmtG(total || 0)}`;
    else if (rem != null) stats.textContent = `Current remaining ${fmtG(rem)}`;
    else stats.textContent = "No local weight reference set for this slot.";
  }
  modal.hidden = false;
}
function closeSpoolModal() {
  const modal = $("spoolModal");
  if (modal) modal.hidden = true;
  spoolModalOpen = false;
  spoolSlotId = null;
  if (spoolPrevPaused !== null) {
    refreshPaused = spoolPrevPaused;
    spoolPrevPaused = null;
    applyRefreshTimer();
  }
}

/* ---------- modals: spoolman picker ---------- */

async function openSpoolmanPicker(slotId) {
  spoolmanPickerSlot = slotId;
  spoolmanPickerOpen = true;
  const modal = $("spoolmanPickerModal");
  $("spoolmanPickerTitle").textContent = "Select Spoolman spool";
  $("spoolmanPickerSub").textContent = `for CFS slot ${slotId}`;
  const search = $("spoolmanPickerSearch");
  if (search) { search.value = ""; spoolmanPickerFilter = ""; }
  $("spoolmanPickerStatus").textContent = "Loading spools…";
  $("spoolmanPickerList").innerHTML = "";
  modal.hidden = false;
  try {
    await loadSpoolmanSpools(true);
    renderSpoolmanPickerList();
  } catch (err) {
    $("spoolmanPickerStatus").textContent = "Could not load Spoolman spools: " + (err?.message || String(err));
  }
  if (search) search.focus();
}
function closeSpoolmanPicker() {
  const modal = $("spoolmanPickerModal");
  if (modal) modal.hidden = true;
  spoolmanPickerSlot = null;
  spoolmanPickerOpen = false;
}
function renderSpoolmanPickerList() {
  const list = $("spoolmanPickerList");
  if (!list) return;
  list.innerHTML = "";
  const q = spoolmanPickerFilter.trim().toLowerCase();
  const rows = spoolmanSpools.filter((spool) => {
    if (!q) return true;
    const hay = [spool.id, spoolDisplayName(spool), spoolMaterial(spool), spoolVendor(spool), spoolFilamentName(spool)].join(" ").toLowerCase();
    return hay.includes(q);
  });
  if (!rows.length) {
    list.appendChild(el("div", { class: "picker-empty", text: spoolmanSpools.length ? "No matching spools." : "No spools available." }));
    return;
  }
  for (const spool of rows) {
    const item = el("button", {
      class: "picker-item",
      attrs: { type: "button", role: "option" },
      on: { click: async () => {
        if (!spoolmanPickerSlot) return;
        await postJson("/api/ui/spoolman/mapping", { slot: spoolmanPickerSlot, spool_id: Number(spool.id) });
        closeSpoolmanPicker();
        await tick();
      } },
    }, [
      el("span", { class: "picker-swatch", style: { background: spoolColor(spool) } }),
      el("span", { class: "picker-main" }, [
        el("span", { class: "picker-title", text: `#${spool.id} · ${spoolDisplayName(spool)}` }),
        el("span", { class: "picker-fil", text: spoolFilamentName(spool) }),
      ]),
      el("span", { class: "picker-mat", text: spoolMaterial(spool) }),
      el("span", { class: "picker-last", text: fmtAgo(spool.last_used || spool.last_used_at || spool.updated_at) }),
      el("span", { class: "picker-weight", text: spoolWeightText(spool) }),
    ]);
    list.appendChild(item);
  }
}

/* ---------- modals: settings drawer ---------- */

function openSettingsModal() {
  settingsModalOpen = true;
  const modal = $("settingsModal");
  const cfg  = (latestState && latestState.printer_config) || {};
  const scfg = (latestState && latestState.spoolman_config) || {};
  $("settingsMoonrakerUrl").value      = cfg.moonraker_url || "";
  $("settingsPollInterval").value      = cfg.poll_interval_sec ?? 5;
  $("settingsFilamentDiameter").value  = cfg.filament_diameter_mm ?? 1.75;
  $("settingsCfsAutosync").checked     = cfg.cfs_autosync === true;
  $("settingsDebugMode").checked       = isDebugMode();

  $("settingsSpoolmanUrl").value        = scfg.url || "";
  $("settingsSpoolmanEnabled").checked  = scfg.enabled === true;
  $("settingsSpoolmanDryRun").checked   = scfg.dry_run !== false;
  $("settingsSpoolmanMode").value       = scfg.sync_mode === "live" ? "live" : "post_print";
  $("settingsSpoolmanLiveDelta").value  = scfg.live_min_delta_mm || 100;

  applyDebugVisibility();
  $("settingsStatus").textContent = "";
  $("settingsSpoolmanTestStatus").textContent = "";
  modal.hidden = false;
}
function closeSettingsModal() {
  settingsModalOpen = false;
  const modal = $("settingsModal");
  if (modal) modal.hidden = true;
}
function applyDebugVisibility() {
  const on = $("settingsDebugMode").checked;
  $("settingsDryRunRow").hidden = !on;
}

function renderUpdateStatus(info) {
  const status = $("settingsUpdateStatus");
  const apply = $("settingsUpdateApply");
  if (!status || !apply) return;

  const current = info?.current_short ? `Current ${info.current_short}` : "";
  const remote = info?.remote_short ? `Latest ${info.remote_short}` : "";
  const refs = [current, remote].filter(Boolean).join(" · ");
  const message = info?.message || "Unable to read update status.";
  status.textContent = refs ? `${message} ${refs}` : message;
  apply.hidden = !(info?.update_available && info?.can_update);
  apply.disabled = apply.hidden;
}

async function checkAppUpdate() {
  const status = $("settingsUpdateStatus");
  const apply = $("settingsUpdateApply");
  if (status) status.textContent = "Checking for updates...";
  if (apply) {
    apply.hidden = true;
    apply.disabled = true;
  }
  try {
    const j = await postJson("/api/ui/update/check", {});
    renderUpdateStatus(j.result || j);
  } catch (err) {
    if (status) status.textContent = "Could not check for updates: " + (err?.message || String(err));
  }
}

async function applyAppUpdate() {
  const status = $("settingsUpdateStatus");
  const apply = $("settingsUpdateApply");
  if (!confirm("Install the latest version and restart spoolman-cfs-sync?")) return;
  if (status) status.textContent = "Installing update...";
  if (apply) apply.disabled = true;
  try {
    const j = await postJson("/api/ui/update/apply", {});
    const info = j.result || j;
    renderUpdateStatus(info);
    if (info.started) {
      if (status) status.textContent = "Update installed. Restarting service...";
      setTimeout(() => window.location.reload(), 6000);
    }
  } catch (err) {
    if (status) status.textContent = "Could not install update: " + (err?.message || String(err));
    if (apply) apply.disabled = false;
  }
}

function initSettings() {
  $("settingsOpen").onclick   = openSettingsModal;
  $("settingsDebugMode").addEventListener("change", applyDebugVisibility);

  $("settingsSpoolmanTest").onclick = async () => {
    const status = $("settingsSpoolmanTestStatus");
    status.textContent = "Testing…";
    try {
      // Save URL first so the backend tests the URL in the input.
      await postJson("/api/ui/spoolman/config", { url: $("settingsSpoolmanUrl").value });
      await postJson("/api/ui/spoolman/test", {});
      await loadSpoolmanSpools(true).catch(() => []);
      await tick();
      const ok = latestState?.spoolman_status?.connected;
      status.textContent = ok ? "Reachable." : "Not reachable.";
    } catch (err) {
      status.textContent = "Failed: " + (err?.message || String(err));
    }
  };

  $("settingsSave").onclick = async () => {
    const status = $("settingsStatus");
    status.textContent = "Saving…";
    try {
      const debugOn = $("settingsDebugMode").checked;
      setDebugMode(debugOn);
      await postJson("/api/ui/printer/config", {
        moonraker_url:        $("settingsMoonrakerUrl").value || "",
        poll_interval_sec:    Number($("settingsPollInterval").value || 5),
        filament_diameter_mm: Number($("settingsFilamentDiameter").value || 1.75),
        cfs_autosync:         Boolean($("settingsCfsAutosync").checked),
      });
      const spoolmanPayload = {
        url:               $("settingsSpoolmanUrl").value || "",
        enabled:           Boolean($("settingsSpoolmanEnabled").checked),
        sync_mode:         $("settingsSpoolmanMode").value,
        live_min_delta_mm: Number($("settingsSpoolmanLiveDelta").value || 100),
      };
      if (debugOn) {
        spoolmanPayload.dry_run = Boolean($("settingsSpoolmanDryRun").checked);
      }
      await postJson("/api/ui/spoolman/config", spoolmanPayload);
      status.textContent = "Saved.";
      await tick();
    } catch (err) {
      status.textContent = "Could not save: " + (err?.message || String(err));
    }
  };

  $("settingsUpdateCheck").onclick = checkAppUpdate;
  $("settingsUpdateApply").onclick = applyAppUpdate;
}

/* ---------- generic modal close wiring ---------- */

function initModalCloses() {
  document.querySelectorAll("[data-close]").forEach((node) => {
    const key = node.dataset.close;
    node.addEventListener("click", () => {
      if (key === "spool")    closeSpoolModal();
      if (key === "picker")   closeSpoolmanPicker();
      if (key === "settings") closeSettingsModal();
    });
  });
  $("spoolClose")?.addEventListener?.("click", closeSpoolModal);
  $("spoolSaveStart").onclick = async () => {
    if (!spoolSlotId) return;
    const v = Number($("spoolStart").value || 0);
    if (!Number.isFinite(v) || v <= 0) return;
    await postJson("/api/ui/spool/set_start", { slot: spoolSlotId, start_g: v });
    closeSpoolModal();
    await tick();
  };
  $("spoolSaveRemain").onclick = async () => {
    if (!spoolSlotId) return;
    const v = Number($("spoolRemain").value || 0);
    if (!Number.isFinite(v) || v < 0) return;
    await postJson("/api/ui/spool/set_remaining", { slot: spoolSlotId, remaining_g: v });
    closeSpoolModal();
    await tick();
  };
  const search = $("spoolmanPickerSearch");
  if (search) {
    search.oninput = () => {
      spoolmanPickerFilter = search.value || "";
      renderSpoolmanPickerList();
    };
  }
}

/* ---------- sync records ---------- */

function syncStatusKind(status) {
  const s = String(status || "");
  if (s === "synced") return { key: "synced", label: "synced" };
  if (s === "dry_run") return { key: "dry_run", label: "dry-run" };
  if (s === "pending") return { key: "pending", label: "pending" };
  if (s === "timeout_uncertain" || s === "conflict") return { key: "timeout", label: "TIMEOUT — UNCERTAIN" };
  if (s === "failed") return { key: "failed", label: "failed" };
  if (s === "skipped_unmapped") return { key: "skipped", label: "unmapped" };
  if (s === "skipped_invalid_spool") return { key: "skipped", label: "invalid spool" };
  if (s.startsWith("skipped")) return { key: "skipped", label: "skipped" };
  return { key: "skipped", label: s || "unknown" };
}

const SYNC_DEFAULT = 6;
const SYNC_HARD_MAX = 50;
let syncExpanded = false;

function renderSyncRecords(state) {
  const wrap = $("syncList");
  const meta = $("syncMeta");
  if (!wrap) return;
  wrap.innerHTML = "";
  const cfg = state.spoolman_config || {};
  const status = state.spoolman_status || {};
  const records = state.spoolman_sync_records || {};

  const enabled = cfg.enabled === true;
  const dryRun  = cfg.dry_run !== false;
  const syncMode = cfg.sync_mode === "live" ? "live" : "post-print";
  const mode = enabled && !dryRun ? "writes enabled" : (enabled && dryRun ? "dry-run" : "disabled");
  if (meta) {
    meta.innerHTML = "";
    meta.appendChild(el("span", { class: "chip " + (enabled && !dryRun ? "chip-ok" : (enabled ? "chip-warn" : "chip-muted")), text: mode }));
    meta.appendChild(el("span", { class: "chip chip-muted", text: syncMode }));
    if (status.connected) meta.appendChild(el("span", { class: "chip chip-ok", text: "reachable" }));
  }

  const allEntries = Object.entries(records)
    .map(([k, r]) => [k, r || {}])
    .sort((a, b) => Number(b[1].updated_at || b[1].finished_at || 0) - Number(a[1].updated_at || a[1].finished_at || 0))
    .slice(0, SYNC_HARD_MAX);

  if (!allEntries.length) return;

  const entries = syncExpanded ? allEntries : allEntries.slice(0, SYNC_DEFAULT);

  for (const [key, rec] of entries) {
    const kind = syncStatusKind(rec.status);
    const row = el("div", { class: "sync-row" });
    row.appendChild(el("span", { class: "slot-id", text: rec.slot || "?" }));
    row.appendChild(el("span", { class: "sync-arrow", text: "→" }));
    const info = el("div", { class: "sync-info" }, [
      el("div", { class: "sync-amount", text: `${rec.spool_id ? "#" + rec.spool_id : "unmapped"} · ${fmtMm(rec.used_mm || 0)}${rec.used_g ? ` · ${fmtG(rec.used_g)}` : ""}` }),
      el("div", { class: "sync-job", text: `${rec.sync_phase ? rec.sync_phase + " · " : ""}${rec.job || "(unnamed job)"} · ${fmtAgo(rec.updated_at || rec.finished_at)}` }),
    ]);
    row.appendChild(info);
    row.appendChild(el("span", { class: "status-chip", attrs: { "data-status": kind.key }, text: kind.label }));

    // Safety: never offer retry for live-phase records, never for timeout/uncertain/conflict.
    // The backend is also the final authority — unmapped slots are never written to Spoolman.
    const retryableStatuses = ["failed", "pending", "skipped_invalid_spool", "skipped_unmapped", "dry_run"];
    const canRetry =
      rec.sync_phase !== "live" &&
      kind.key !== "timeout" &&
      retryableStatuses.includes(String(rec.status || ""));
    if (canRetry) {
      row.appendChild(el("button", {
        class: "btn btn-secondary btn-mini",
        attrs: { type: "button" },
        text: "Sync now",
        on: { click: async () => {
          await postJson("/api/ui/spoolman/retry", { record_key: key });
          await tick();
        } },
      }));
    } else {
      row.appendChild(el("span"));
    }

    if (rec.error) {
      row.appendChild(el("div", { class: "sync-err", text: String(rec.error).slice(0, 280) }));
    }
    if (kind.key === "timeout") row.style.borderColor = "var(--danger)";
    wrap.appendChild(row);
  }

  if (allEntries.length > SYNC_DEFAULT) {
    const extra = allEntries.length - SYNC_DEFAULT;
    wrap.appendChild(el("button", {
      class: "btn btn-ghost btn-mini sync-more",
      attrs: { type: "button" },
      text: syncExpanded ? "Show less" : `Show all (${extra} more)`,
      on: { click: () => {
        syncExpanded = !syncExpanded;
        if (latestState) render(latestState);
      } },
    }));
  }
}

/* ---------- history ---------- */

const HIST_DEFAULT = 4;
const histExpanded = new Set(); // slot ids currently expanded

function renderHistory(state, slots, connectedBoxes) {
  const wrap = $("slotHistory");
  if (!wrap) return;
  wrap.innerHTML = "";
  const history = state.slot_history || {};
  const active = state.cfs_active_slot || null;

  for (const sid of buildSlotIds(connectedBoxes)) {
    const meta = slotMeta(state, slots, sid);
    const epoch = Number(meta.spool_epoch || 0);
    const raw = Array.isArray(history[sid]) ? history[sid] : [];
    // All entries for the current spool epoch — never silently hidden.
    const allEntries = raw.filter((e) => Number((e || {}).epoch || 0) === epoch);
    const expanded = histExpanded.has(sid);
    const entries = expanded ? allEntries : allEntries.slice(0, HIST_DEFAULT);
    const totalG = allEntries.reduce((acc, e) => acc + Number(e.used_g || 0), 0);

    const head = el("div", { class: "hist-head" }, [
      el("div", { class: "hist-head-left" }, [
        el("div", { class: "hist-swatch", style: { background: meta.color || "#3f3f46" } }),
        el("span", { class: "hist-title", text: `${sid}${sid === active ? " · ACTIVE" : ""}` }),
      ]),
      el("div", { class: "hist-total", text: allEntries.length ? fmtG(totalG) : "—" }),
    ]);

    const list = el("div", { class: "hist-entries" });
    if (!allEntries.length) {
      list.appendChild(el("div", { class: "hist-empty", text: "No data for current spool." }));
    } else {
      for (const e of entries) {
        list.appendChild(el("div", { class: "hist-entry" }, [
          el("div", { class: "hist-job", text: e.job || "(unnamed job)" }),
          el("div", { class: "hist-nums", text: `${fmtG(e.used_g)} · ${fmtMm(e.used_mm || 0)}` }),
        ]));
      }
    }

    const slotCard = el("div", { class: "hist-slot" }, [head, list]);

    if (allEntries.length > HIST_DEFAULT) {
      const remaining = allEntries.length - HIST_DEFAULT;
      const toggle = el("button", {
        class: "btn btn-ghost btn-mini hist-more",
        attrs: { type: "button" },
        text: expanded ? "Show less" : `Show ${remaining} more`,
        on: { click: () => {
          if (expanded) histExpanded.delete(sid); else histExpanded.add(sid);
          if (latestState) render(latestState);
        } },
      });
      slotCard.appendChild(toggle);
    }
    wrap.appendChild(slotCard);
  }
}

function renderMoonHistory(state) {
  const wrap = $("moonHistory");
  if (!wrap) return;
  wrap.innerHTML = "";
  const hist = Array.isArray(state.moonraker_history) ? state.moonraker_history : [];
  if (!hist.length) {
    wrap.appendChild(el("div", { class: "moon-empty", text: "No Moonraker history data." }));
    return;
  }
  for (const e of hist.slice(0, 12)) {
    const g  = typeof e.filament_used_g_total === "number" ? e.filament_used_g_total : null;
    const mm = typeof e.filament_used_mm === "number" ? e.filament_used_mm : null;
    const nums = g != null ? fmtG(g) : (mm != null ? fmtMm(mm) : "—");
    wrap.appendChild(el("div", { class: "moon-row" }, [
      el("span", { text: e.job || "(unnamed job)" }),
      el("span", { class: "moon-nums", text: nums }),
      el("span", { class: "moon-sub", text: `${fmtTs(e.ts_end || e.ts_start)} · ${e.status || ""}` }),
    ]));
  }
}

/* ---------- active job ---------- */

function renderActiveJob(state, slots) {
  const body = $("jobBody");
  const meta = $("jobMeta");
  if (!body) return;
  body.innerHTML = "";

  const active = state.cfs_active_slot || null;
  const isPrinting = String(state.job_track_last_state || "").toLowerCase() === "printing";
  const jobName = state.job_track_name || state.current_job || "";

  if (!active || !(slots?.[active] || state.slots?.[active])) {
    if (meta) meta.textContent = isPrinting ? "Printing · no CFS slot reported" : "Idle";
    body.appendChild(el("div", { class: "job-empty", text: isPrinting
      ? `Printing ${jobName || "(unnamed job)"} — CFS has not reported an active slot.`
      : "No active print · no CFS slot selected." }));
    return;
  }

  const m = slotMeta(state, slots, active);
  if (meta) meta.textContent = isPrinting ? "Printing now" : "Ready";

  const slotMm = state.job_track_slot_mm && typeof state.job_track_slot_mm === "object" ? Number(state.job_track_slot_mm[active] || 0) : 0;
  const slotG  = state.job_track_slot_g  && typeof state.job_track_slot_g  === "object" ? Number(state.job_track_slot_g[active]  || 0) : 0;

  const swatch = el("div", { class: "job-swatch", style: { background: m.color } });
  const main = el("div", { class: "job-main" }, [
    el("div", { class: "job-slot-id", text: `BOX ${active[0]} · SLOT ${active[1]}` }),
    el("div", { class: "job-name", text: jobName || "(no active job)" }),
    el("div", { class: "job-sub", text: [m.name, m.material, m.vendor].filter(Boolean).join(" · ") || m.color }),
  ]);

  let live = null;
  if (isPrinting && slotMm > 0) {
    live = el("div", { class: "job-live" }, [
      el("span", { class: "pulse" }),
      el("span", { text: `${fmtMm(slotMm)}${slotG > 0 ? ` · ${fmtG(slotG)}` : ""}` }),
    ]);
  }
  body.appendChild(el("div", { class: "job-active" }, [swatch, main, live]));
}

/* ---------- warnings ---------- */

function renderWarnings(state) {
  const bar = $("warnBar");
  if (!bar) return;
  const status = state.spoolman_status || {};
  const warning = String(status.moonraker_native_warning || "").trim();
  if (warning) {
    bar.hidden = false;
    bar.classList.add("danger");
    bar.textContent = warning;
  } else {
    bar.hidden = true;
    bar.classList.remove("danger");
    bar.textContent = "";
  }
}

/* ---------- main render ---------- */

function render(state) {
  latestState = state;

  // Connection strip
  const printerOk = !!state.printer_connected;
  const printerErr = printerErrorLabel(state.printer_last_error);
  setCStat("printerBadge", printerOk ? "ok" : "bad",
    printerOk ? "connected" : ("offline" + (printerErr ? " · " + printerErr : "")));

  const cfsOk = !!state.cfs_connected;
  setCStat("cfsBadge", cfsOk ? "ok" : (printerOk ? "warn" : "idle"),
    cfsOk ? `detected · ${fmtAgo(state.cfs_last_update)}` : (printerOk ? "not detected" : "—"));

  const scfg = state.spoolman_config || {};
  const sstat = state.spoolman_status || {};
  const sEnabled = scfg.enabled === true;
  const sDry = scfg.dry_run !== false;
  const sReach = !!sstat.connected;
  let sState = "idle", sLabel = "disabled";
  if (sEnabled && sReach && !sDry)      { sState = "ok";   sLabel = "writes enabled"; }
  else if (sEnabled && sDry && sReach)  { sState = "warn"; sLabel = "dry-run"; }
  else if (sEnabled && !sReach)         { sState = "bad";  sLabel = "unreachable"; }
  else if (!sEnabled && scfg.url)       { sState = "idle"; sLabel = "off"; }
  setCStat("spoolmanBadge", sState, sLabel);

  // Slots / boxes
  const slots = (state.cfs_slots && Object.keys(state.cfs_slots).length) ? state.cfs_slots : state.slots;
  const connectedBoxes = connectedBoxesFor(slots);
  const active = state.cfs_active_slot || null;

  ensureSpoolmanSpoolsLoadedForPanel(scfg);

  const cfsMetaNode = $("cfsMeta");
  if (cfsMetaNode) cfsMetaNode.textContent = `${connectedBoxes.length} box${connectedBoxes.length === 1 ? "" : "es"} · ${connectedBoxes.length * 4} slots`;

  const grid = $("cfsGrid");
  if (grid) {
    grid.innerHTML = "";
    const boxesInfo = (slots && slots._boxes) ? slots._boxes : {};
    for (const boxNum of connectedBoxes) {
      const bi = boxesInfo[boxNum] || {};
      const env = el("div", { class: "cfs-box-env" });
      if (typeof bi.temperature_c === "number") env.appendChild(el("span", { class: "env", text: `${Math.round(bi.temperature_c)}°C` }));
      if (typeof bi.humidity_pct  === "number") env.appendChild(el("span", { class: "env", text: `${Math.round(bi.humidity_pct)}% RH` }));

      const head = el("div", { class: "cfs-box-head" }, [
        el("span", { class: "cfs-box-title", text: `BOX ${boxNum}` }),
        env.childNodes.length ? env : el("span"),
      ]);
      const slotsWrap = el("div", { class: "cfs-slots" });
      for (const letter of ["A","B","C","D"]) {
        const sid = `${boxNum}${letter}`;
        slotsWrap.appendChild(buildSlotCard(state, slots, sid, sid === active));
      }
      grid.appendChild(el("div", { class: "cfs-box" }, [head, slotsWrap]));
    }
  }

  renderActiveJob(state, slots);
  renderSyncRecords(state);
  renderHistory(state, slots, connectedBoxes);
  renderMoonHistory(state);
  renderWarnings(state);

  // Debug-only "Clear data"
  const clearBtn = $("clearAccountingBtn");
  if (clearBtn) {
    clearBtn.hidden = !isDebugMode();
    clearBtn.disabled = !!state.job_track_name;
    clearBtn.title = state.job_track_name ? "A print is being tracked" : "Clear local accounting / sync records";
  }
}

/* ---------- polling ---------- */

async function tick() {
  try {
    const j = await getJson("/api/ui/state");
    render(j.result || j);
  } catch {
    setCStat("printerBadge", "warn", "—");
    setCStat("cfsBadge", "warn", "—");
    setCStat("spoolmanBadge", "warn", "—");
  }
}

let refreshTimer = null;
let refreshMs = Number(localStorage.getItem("refreshMs") || 10000);
if (!Number.isFinite(refreshMs) || refreshMs < 2000) refreshMs = 10000;
let refreshPaused = localStorage.getItem("refreshPaused") === "1";

function applyRefreshTimer() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
  if (!refreshPaused) refreshTimer = setInterval(tick, refreshMs);
  const sel = $("refreshSelect");
  const btn = $("refreshToggle");
  if (sel) sel.value = String(refreshMs);
  if (btn) {
    btn.classList.toggle("paused", refreshPaused);
    btn.title = refreshPaused ? "Resume auto-refresh" : "Pause auto-refresh";
    btn.setAttribute("aria-label", btn.title);
    btn.innerHTML = refreshPaused
      ? '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>'
      : '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="5" width="4" height="14"></rect><rect x="14" y="5" width="4" height="14"></rect></svg>';
  }
}
function initRefreshControls() {
  const sel = $("refreshSelect");
  const btn = $("refreshToggle");
  if (sel) {
    sel.value = String(refreshMs);
    sel.onchange = () => {
      refreshMs = Number(sel.value || 10000);
      if (!Number.isFinite(refreshMs) || refreshMs < 2000) refreshMs = 10000;
      localStorage.setItem("refreshMs", String(refreshMs));
      applyRefreshTimer();
    };
  }
  if (btn) {
    btn.onclick = () => {
      refreshPaused = !refreshPaused;
      localStorage.setItem("refreshPaused", refreshPaused ? "1" : "0");
      if (!refreshPaused) tick();
      applyRefreshTimer();
    };
  }
  applyRefreshTimer();
}

/* ---------- misc init ---------- */

function initClearAccounting() {
  const btn = $("clearAccountingBtn");
  if (!btn) return;
  btn.onclick = async () => {
    if (!isDebugMode()) return;
    if (!confirm("Clear local accounting data and sync records?\n\nThis only affects local bookkeeping. Spoolman is not touched.")) return;
    await postJson("/api/ui/accounting/clear", {});
    await tick();
  };
}

function initKeyboard() {
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (spoolmanPickerOpen) closeSpoolmanPicker();
    else if (settingsModalOpen) closeSettingsModal();
    else if (spoolModalOpen) closeSpoolModal();
  });
}

function applyTheme(theme) {
  const root = document.documentElement;
  root.classList.remove("dark", "light");
  if (theme === "dark" || theme === "light") root.classList.add(theme);
}
function initThemeToggle() {
  const btn = $("themeToggle");
  const saved = localStorage.getItem("theme");
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
  if (!btn) return;
  btn.onclick = () => {
    const isDark = document.documentElement.classList.contains("dark") ||
      (!document.documentElement.classList.contains("light") && prefersDark);
    const next = isDark ? "light" : "dark";
    localStorage.setItem("theme", next);
    applyTheme(next);
  };
}

function boot() {
  initThemeToggle();
  initSettings();
  initModalCloses();
  initRefreshControls();
  initClearAccounting();
  initKeyboard();
  tick();
}

if (document.readyState === "loading") {
  window.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}
