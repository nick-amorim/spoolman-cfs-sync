/* UI for spoolman-cfs-sync */

const $ = (id) => document.getElementById(id);
const DEBUG_MODE_KEY = "spoolmanCfsSyncDebugMode";

let latestState = null;
let spoolmanSpools = [];
let spoolmanSpoolsLoadingPromise = null;
let spoolmanSpoolsLoadedAt = 0;
let spoolmanPickerSlot = null;
let spoolmanPickerFilter = "";
let spoolModalOpen = false;
let spoolPrevPaused = null;
let spoolSlotId = null;
let settingsModalOpen = false;
let spoolmanPickerOpen = false;

function isDebugMode() {
  try { return localStorage.getItem(DEBUG_MODE_KEY) === "1"; }
  catch { return false; }
}

function setDebugMode(enabled) {
  try { localStorage.setItem(DEBUG_MODE_KEY, enabled ? "1" : "0"); }
  catch {}
}

function fmtTs(ts) {
  if (!ts) return "-";
  try { return new Date(Number(ts) * 1000).toLocaleString(); }
  catch { return "-"; }
}

function fmtAgo(value) {
  if (!value) return "-";
  const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  const t = d.getTime();
  if (!Number.isFinite(t)) return "-";
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 48) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)} days ago`;
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

function normalizeColor(value, fallback = "#6b7280") {
  let c = String(value || "").trim();
  if (!c) return fallback;
  if (!c.startsWith("#")) c = "#" + c;
  return /^#[0-9a-fA-F]{6}$/.test(c) ? c.toUpperCase() : fallback;
}

function badge(el, text, cls) {
  if (!el) return;
  el.classList.remove("ok", "bad", "warn");
  if (cls) el.classList.add(cls);
  el.textContent = text;
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

function buildSlotIds(connectedBoxes) {
  const ids = [];
  for (const b of connectedBoxes || []) {
    for (const l of ["A", "B", "C", "D"]) ids.push(`${b}${l}`);
  }
  return ids;
}

function connectedBoxesFor(slots) {
  const boxesInfo = (slots && slots._boxes) ? slots._boxes : {};
  const boxes = [];
  for (const n of ["1", "2", "3", "4"]) {
    const bi = boxesInfo[n];
    if (bi && bi.connected === true) boxes.push(n);
  }
  if (!boxes.length) boxes.push("1");
  return boxes;
}

function slotMeta(state, slots, sid) {
  const remote = (slots && slots[sid]) ? slots[sid] : {};
  const local = (state.slots && state.slots[sid]) ? state.slots[sid] : {};
  return {
    present: remote.present ?? local.present ?? true,
    material: String((remote.material ?? local.material) || "").toUpperCase(),
    color: normalizeColor(remote.color ?? remote.color_hex ?? local.color ?? local.color_hex, "#2a3442"),
    name: String((remote.name ?? local.name) || ""),
    remaining_g: local.remaining_g ?? null,
    spool_remaining_g: local.spool_remaining_g ?? null,
    spool_used_g: local.spool_used_g ?? null,
    spool_consumed_g: local.spool_consumed_g ?? null,
    spool_epoch: local.spool_epoch ?? 0,
  };
}

function slotCard(slotId, label, meta, isActive) {
  const wrap = document.createElement("div");
  wrap.className = "slot" + (isActive ? " active" : "");
  wrap.dataset.slotid = slotId;

  const left = document.createElement("div");
  left.className = "slotLeft";
  const sw = document.createElement("div");
  sw.className = "swatch";
  sw.style.background = meta.color || "#2a3442";
  left.appendChild(sw);

  const txt = document.createElement("div");
  txt.className = "slotText";
  const nm = document.createElement("div");
  nm.className = "slotName";
  nm.textContent = label;
  txt.appendChild(nm);

  const sub = document.createElement("div");
  sub.className = "slotSub";
  const parts = [];
  if (meta.name) parts.push(meta.name);
  if (meta.material) parts.push(meta.material);
  if (meta.color) parts.push(meta.color);
  sub.textContent = parts.length ? parts.join(" - ") : "-";
  txt.appendChild(sub);

  const rem = meta.spool_remaining_g != null ? meta.spool_remaining_g : meta.remaining_g;
  if (rem != null) {
    const row = document.createElement("div");
    row.className = "spoolRow";
    const rest = document.createElement("div");
    rest.className = "spoolRest";
    rest.textContent = fmtG(rem);
    row.appendChild(rest);
    txt.appendChild(row);
    const r = Number(rem);
    if (Number.isFinite(r)) {
      if (r <= 50) wrap.classList.add("spoolCrit");
      else if (r <= 150) wrap.classList.add("spoolLow");
    }
  }

  left.appendChild(txt);
  wrap.appendChild(left);

  const right = document.createElement("div");
  right.className = "slotRight";
  const tag = document.createElement("div");
  tag.className = "tag" + (!meta.material ? " muted" : "");
  tag.textContent = meta.present === false ? "empty" : (isActive ? "active" : "ready");
  right.appendChild(tag);
  wrap.appendChild(right);

  wrap.addEventListener("click", (ev) => {
    ev.preventDefault();
    openSpoolModal(slotId, meta);
  });
  return wrap;
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
  return String(spool?.material || fil.material || "-").toUpperCase();
}

function spoolDisplayName(spool) {
  if (!spool) return "No spool selected";
  return [spoolVendor(spool), spoolFilamentName(spool)].filter(Boolean).join(" ") || `Spool #${spool.id}`;
}

function spoolWeightText(spool) {
  if (!spool) return "-";
  const remaining = spool.remaining_weight ?? spool.remaining_weight_g ?? spool.weight_remaining;
  const initial = spool.initial_weight ?? spool.initial_weight_g ?? spool.weight;
  const left = remaining != null ? fmtG(remaining) : "-";
  return initial != null ? `${left} / ${fmtG(initial)}` : left;
}

function spoolMappingText(spoolId, spool) {
  if (!spoolId) return "No spool selected";
  if (!spool) return `#${spoolId} - loading details...`;
  return `#${spoolId} | ${spoolDisplayName(spool)} - ${spoolMaterial(spool)} - ${spoolWeightText(spool)}`;
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
  if (status) status.textContent = "Loading spools...";
  spoolmanSpoolsLoadingPromise = (async () => {
    const j = await getJson("/api/ui/spoolman/spools");
    const result = j.result || j;
    spoolmanSpools = Array.isArray(result.spools) ? result.spools : [];
    spoolmanSpoolsLoadedAt = Date.now();
    if (status) status.textContent = `${spoolmanSpools.length} spools available`;
    return spoolmanSpools;
  })();
  try {
    return await spoolmanSpoolsLoadingPromise;
  } finally {
    spoolmanSpoolsLoadingPromise = null;
  }
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
    .catch(() => { if (latestState) render(latestState); });
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
    const empty = document.createElement("div");
    empty.className = "tag muted";
    empty.textContent = "No matching spools";
    list.appendChild(empty);
    return;
  }

  for (const spool of rows) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "spoolmanPickerItem";

    const sw = document.createElement("span");
    sw.className = "spoolmanPickerSwatch";
    sw.style.background = spoolColor(spool);
    item.appendChild(sw);

    const main = document.createElement("span");
    main.className = "spoolmanPickerMain";
    const title = document.createElement("span");
    title.className = "spoolmanPickerTitle";
    title.textContent = `#${spool.id} | ${spoolDisplayName(spool)}`;
    const fil = document.createElement("span");
    fil.className = "spoolmanPickerFilament";
    fil.textContent = spoolFilamentName(spool);
    main.appendChild(title);
    main.appendChild(fil);
    item.appendChild(main);

    const mat = document.createElement("span");
    mat.className = "spoolmanPickerMaterial";
    mat.textContent = spoolMaterial(spool);
    item.appendChild(mat);

    const last = document.createElement("span");
    last.className = "spoolmanPickerLast";
    last.textContent = fmtAgo(spool.last_used || spool.last_used_at || spool.updated_at);
    item.appendChild(last);

    const weight = document.createElement("span");
    weight.className = "spoolmanPickerWeight";
    weight.textContent = spoolWeightText(spool);
    item.appendChild(weight);

    item.onclick = async () => {
      if (!spoolmanPickerSlot) return;
      await postJson("/api/ui/spoolman/mapping", { slot: spoolmanPickerSlot, spool_id: Number(spool.id) });
      closeSpoolmanPicker();
      await tick();
    };

    list.appendChild(item);
  }
}

async function openSpoolmanPicker(slotId) {
  spoolmanPickerSlot = slotId;
  spoolmanPickerOpen = true;
  const modal = $("spoolmanPickerModal");
  const title = $("spoolmanPickerTitle");
  const sub = $("spoolmanPickerSub");
  const search = $("spoolmanPickerSearch");
  const status = $("spoolmanPickerStatus");
  if (title) title.textContent = "Select Spool";
  if (sub) sub.textContent = `CFS slot ${slotId}`;
  if (search) {
    search.value = "";
    spoolmanPickerFilter = "";
  }
  if (status) status.textContent = "Loading spools...";
  if (modal) modal.style.display = "block";
  try {
    await loadSpoolmanSpools(true);
    renderSpoolmanPickerList();
  } catch (err) {
    if (status) status.textContent = "Could not load Spoolman spools: " + (err?.message || String(err));
  }
  if (search) search.focus();
}

function closeSpoolmanPicker() {
  const modal = $("spoolmanPickerModal");
  if (modal) modal.style.display = "none";
  spoolmanPickerSlot = null;
  spoolmanPickerOpen = false;
}

function initSpoolmanPicker() {
  const close = $("spoolmanPickerClose");
  const back = $("spoolmanPickerBackdrop");
  const search = $("spoolmanPickerSearch");
  if (close) close.onclick = closeSpoolmanPicker;
  if (back) back.onclick = closeSpoolmanPicker;
  if (search) {
    search.oninput = () => {
      spoolmanPickerFilter = search.value || "";
      renderSpoolmanPickerList();
    };
  }
}

function closeSpoolModal() {
  const modal = $("spoolModal");
  if (modal) modal.style.display = "none";
  spoolModalOpen = false;
  spoolSlotId = null;
  if (spoolPrevPaused !== null) {
    refreshPaused = spoolPrevPaused;
    spoolPrevPaused = null;
    applyRefreshTimer();
  }
}

function openSpoolModal(slotId, meta) {
  const modal = $("spoolModal");
  if (!modal) return;
  spoolModalOpen = true;
  spoolSlotId = slotId;
  if (spoolPrevPaused === null) spoolPrevPaused = refreshPaused;
  refreshPaused = true;
  applyRefreshTimer();

  const title = $("spoolTitle");
  const sub = $("spoolSub");
  const stats = $("spoolStats");
  if (title) title.textContent = `Box ${slotId[0]} - Slot ${slotId[1]}`;
  if (sub) sub.textContent = [meta.name, meta.material, meta.color].filter(Boolean).join(" - ") || "-";

  const startEl = $("spoolStart");
  const remEl = $("spoolRemain");
  const rem = meta.spool_remaining_g != null ? meta.spool_remaining_g : meta.remaining_g;
  if (startEl) startEl.value = "";
  if (remEl) remEl.value = rem != null ? String(Math.round(Number(rem))) : "";

  if (stats) {
    const used = meta.spool_used_g;
    const total = meta.spool_consumed_g;
    if (rem != null && used != null) stats.textContent = `Remaining: ${fmtG(rem)} - Used since last apply: ${fmtG(used)} - Current spool total: ${fmtG(total || 0)}`;
    else if (rem != null) stats.textContent = `Current remaining: ${fmtG(rem)}`;
    else stats.textContent = "No local weight reference has been set for this slot.";
  }

  modal.style.display = "block";
}

function initSpoolModal() {
  const close = $("spoolClose");
  const back = $("spoolBackdrop");
  const saveStart = $("spoolSaveStart");
  const saveRemain = $("spoolSaveRemain");
  if (close) close.onclick = closeSpoolModal;
  if (back) back.onclick = closeSpoolModal;
  if (saveStart) {
    saveStart.onclick = async () => {
      if (!spoolSlotId) return;
      const v = Number(($("spoolStart") || {}).value || 0);
      if (!Number.isFinite(v) || v <= 0) return;
      await postJson("/api/ui/spool/set_start", { slot: spoolSlotId, start_g: v });
      closeSpoolModal();
      await tick();
    };
  }
  if (saveRemain) {
    saveRemain.onclick = async () => {
      if (!spoolSlotId) return;
      const v = Number(($("spoolRemain") || {}).value || 0);
      if (!Number.isFinite(v) || v < 0) return;
      await postJson("/api/ui/spool/set_remaining", { slot: spoolSlotId, remaining_g: v });
      closeSpoolModal();
      await tick();
    };
  }
}

function openSettingsModal() {
  settingsModalOpen = true;
  const modal = $("settingsModal");
  const cfg = (latestState && latestState.printer_config) || {};
  if ($("settingsMoonrakerUrl")) $("settingsMoonrakerUrl").value = cfg.moonraker_url || "";
  if ($("settingsPollInterval")) $("settingsPollInterval").value = cfg.poll_interval_sec ?? "5";
  if ($("settingsFilamentDiameter")) $("settingsFilamentDiameter").value = cfg.filament_diameter_mm ?? "1.75";
  if ($("settingsCfsAutosync")) $("settingsCfsAutosync").checked = cfg.cfs_autosync === true;
  if ($("settingsDebugMode")) $("settingsDebugMode").checked = isDebugMode();
  if ($("settingsStatus")) $("settingsStatus").textContent = "";
  if (modal) modal.style.display = "block";
}

function closeSettingsModal() {
  settingsModalOpen = false;
  const modal = $("settingsModal");
  if (modal) modal.style.display = "none";
}

function initSettingsModal() {
  if ($("settingsOpen")) $("settingsOpen").onclick = openSettingsModal;
  if ($("settingsClose")) $("settingsClose").onclick = closeSettingsModal;
  if ($("settingsBackdrop")) $("settingsBackdrop").onclick = closeSettingsModal;
  if ($("settingsSave")) {
    $("settingsSave").onclick = async () => {
      const status = $("settingsStatus");
      try {
        setDebugMode(Boolean(($("settingsDebugMode") || {}).checked));
        await postJson("/api/ui/printer/config", {
          moonraker_url: ($("settingsMoonrakerUrl") || {}).value || "",
          poll_interval_sec: Number(($("settingsPollInterval") || {}).value || 5),
          filament_diameter_mm: Number(($("settingsFilamentDiameter") || {}).value || 1.75),
          cfs_autosync: Boolean(($("settingsCfsAutosync") || {}).checked),
        });
        if (status) status.textContent = "Settings saved.";
        await tick();
      } catch (err) {
        if (status) status.textContent = "Could not save settings: " + (err?.message || String(err));
      }
    };
  }
}

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
    const entries = raw.filter((e) => Number((e || {}).epoch || 0) === epoch).slice(0, 4);
    const card = document.createElement("div");
    card.className = "histSlot";
    const head = document.createElement("div");
    head.className = "histHead";
    const title = document.createElement("div");
    title.className = "histTitle";
    const sw = document.createElement("div");
    sw.className = "swatch";
    sw.style.width = "22px";
    sw.style.height = "22px";
    sw.style.background = meta.color || "#2a3442";
    title.appendChild(sw);
    const nm = document.createElement("div");
    nm.className = "histSlotName";
    nm.textContent = `Box ${sid[0]} - Slot ${sid[1]}` + (sid === active ? " - active" : "");
    title.appendChild(nm);
    head.appendChild(title);
    const totalG = entries.reduce((acc, e) => acc + Number(e.used_g || 0), 0);
    const metaEl = document.createElement("div");
    metaEl.className = "histMeta";
    metaEl.textContent = entries.length ? fmtG(totalG) : "-";
    head.appendChild(metaEl);
    card.appendChild(head);

    const list = document.createElement("div");
    list.className = "histList";
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "tag muted";
      empty.textContent = "No data yet";
      list.appendChild(empty);
    } else {
      for (const e of entries) {
        const det = document.createElement("details");
        det.className = "histEntry";
        const sum = document.createElement("summary");
        const row = document.createElement("div");
        row.className = "histRow";
        const job = document.createElement("div");
        job.className = "histJob";
        job.textContent = e.job || "(unnamed job)";
        const nums = document.createElement("div");
        nums.className = "histNums";
        nums.textContent = `${fmtG(e.used_g)} (${fmtMm(e.used_mm || 0)})`;
        row.appendChild(job);
        row.appendChild(nums);
        sum.appendChild(row);
        det.appendChild(sum);
        const sub = document.createElement("div");
        sub.className = "histSub";
        sub.textContent = `${fmtTs(e.ts)} - ${meta.material || "-"} ${meta.color || ""} - ${e.result || ""}`;
        det.appendChild(sub);
        list.appendChild(det);
      }
    }
    card.appendChild(list);
    wrap.appendChild(card);
  }
}

function renderMoonHistory(state) {
  const wrap = $("moonHistory");
  if (!wrap) return;
  wrap.innerHTML = "";
  const hist = Array.isArray(state.moonraker_history) ? state.moonraker_history : [];
  if (!hist.length) {
    const empty = document.createElement("div");
    empty.className = "tag muted";
    empty.textContent = "No Moonraker history data";
    wrap.appendChild(empty);
    return;
  }
  for (const e of hist.slice(0, 12)) {
    const det = document.createElement("details");
    det.className = "moonEntry";
    const sum = document.createElement("summary");
    const row = document.createElement("div");
    row.className = "moonRow";
    const job = document.createElement("div");
    job.className = "moonJob";
    job.textContent = e.job || "(unnamed job)";
    const nums = document.createElement("div");
    nums.className = "moonNums";
    const g = typeof e.filament_used_g_total === "number" ? e.filament_used_g_total : null;
    const mm = typeof e.filament_used_mm === "number" ? e.filament_used_mm : null;
    nums.textContent = g != null ? fmtG(g) : (mm != null ? fmtMm(mm) : "-");
    row.appendChild(job);
    row.appendChild(nums);
    sum.appendChild(row);
    det.appendChild(sum);
    const sub = document.createElement("div");
    sub.className = "moonSub";
    sub.textContent = `${fmtTs(e.ts_end || e.ts_start)} - ${e.status || ""}`;
    det.appendChild(sub);
    wrap.appendChild(det);
  }
}

function spoolmanStatusLabel(status) {
  const s = String(status || "");
  if (s === "synced") return "synced";
  if (s === "dry_run") return "dry-run";
  if (s === "skipped_unmapped") return "unmapped";
  if (s === "skipped_invalid_spool") return "invalid spool";
  if (s === "timeout_uncertain") return "uncertain";
  return s || "unknown";
}

function renderSpoolman(state, connectedBoxes, slots) {
  const panel = $("spoolmanPanel");
  const meta = $("spoolmanMeta");
  if (!panel) return;
  panel.innerHTML = "";
  const debugMode = isDebugMode();
  const cfg = state.spoolman_config || {};
  const status = state.spoolman_status || {};
  const mappings = cfg.slot_mappings || {};
  const records = state.spoolman_sync_records || {};
  const dryRun = cfg.dry_run !== false;
  const enabled = cfg.enabled === true;
  const syncMode = cfg.sync_mode === "live" ? "live" : "post_print";
  ensureSpoolmanSpoolsLoadedForPanel(cfg);
  if (meta) {
    const mode = enabled && !dryRun ? "writes enabled" : (dryRun ? "dry-run" : "disabled");
    const timing = syncMode === "live" ? "live" : "post-print";
    meta.textContent = debugMode ? `${mode} - ${timing} - debug` : `${mode} - ${timing}`;
  }

  const statusRow = document.createElement("div");
  statusRow.className = "spoolmanStatus";
  const conn = document.createElement("span");
  conn.className = "tag " + (status.connected ? "ok" : "muted");
  conn.textContent = status.connected ? "Spoolman reachable" : "Spoolman not checked";
  statusRow.appendChild(conn);
  const mode = document.createElement("span");
  mode.className = "tag " + (enabled && !dryRun ? "ok" : (dryRun ? "warn" : "muted"));
  mode.textContent = enabled && !dryRun ? "Sync enabled" : (dryRun ? "Dry-run mode" : "Sync disabled");
  statusRow.appendChild(mode);
  const timing = document.createElement("span");
  timing.className = "tag " + (syncMode === "live" ? "ok" : "muted");
  timing.textContent = syncMode === "live" ? "Live sync" : "Post-print sync";
  statusRow.appendChild(timing);
  panel.appendChild(statusRow);

  if (status.moonraker_native_detected) {
    const warn = document.createElement("div");
    warn.className = "spoolmanWarn";
    warn.textContent = status.moonraker_native_warning || "Moonraker Spoolman integration detected.";
    panel.appendChild(warn);
  }

  const cfgBox = document.createElement("div");
  cfgBox.className = "spoolmanConfig";
  const urlInput = document.createElement("input");
  urlInput.type = "text";
  urlInput.className = "spoolmanUrl";
  urlInput.placeholder = "http://spoolman-host:7912";
  urlInput.value = cfg.url || "";
  cfgBox.appendChild(urlInput);

  const enLabel = document.createElement("label");
  enLabel.className = "spoolmanCheck";
  const enInput = document.createElement("input");
  enInput.type = "checkbox";
  enInput.checked = enabled;
  enLabel.appendChild(enInput);
  enLabel.appendChild(document.createTextNode("Enable sync"));
  cfgBox.appendChild(enLabel);

  const dryLabel = document.createElement("label");
  dryLabel.className = "spoolmanCheck";
  const dryInput = document.createElement("input");
  dryInput.type = "checkbox";
  dryInput.checked = dryRun;
  dryLabel.appendChild(dryInput);
  dryLabel.appendChild(document.createTextNode("Dry-run"));
  if (debugMode) cfgBox.appendChild(dryLabel);

  const modeSelect = document.createElement("select");
  modeSelect.className = "spoolmanMode";
  const postOpt = document.createElement("option");
  postOpt.value = "post_print";
  postOpt.textContent = "Post-print";
  modeSelect.appendChild(postOpt);
  const liveOpt = document.createElement("option");
  liveOpt.value = "live";
  liveOpt.textContent = "Live";
  modeSelect.appendChild(liveOpt);
  modeSelect.value = syncMode;
  cfgBox.appendChild(modeSelect);

  const liveDeltaInput = document.createElement("input");
  liveDeltaInput.type = "number";
  liveDeltaInput.min = "1";
  liveDeltaInput.max = "5000";
  liveDeltaInput.step = "1";
  liveDeltaInput.className = "spoolmanLiveDelta";
  liveDeltaInput.title = "Minimum new filament length before a live Spoolman update";
  liveDeltaInput.value = String(cfg.live_min_delta_mm || 100);
  const updateLiveDeltaVisibility = () => {
    liveDeltaInput.style.display = modeSelect.value === "live" ? "" : "none";
  };
  modeSelect.onchange = updateLiveDeltaVisibility;
  updateLiveDeltaVisibility();
  cfgBox.appendChild(liveDeltaInput);

  const saveBtn = document.createElement("button");
  saveBtn.className = "btn mini";
  saveBtn.type = "button";
  saveBtn.textContent = "Save";
  saveBtn.onclick = async () => {
    await postJson("/api/ui/spoolman/config", {
      url: urlInput.value,
      enabled: enInput.checked,
      dry_run: debugMode ? dryInput.checked : false,
      sync_mode: modeSelect.value,
      live_min_delta_mm: Number(liveDeltaInput.value || 100),
    });
    await tick();
  };
  cfgBox.appendChild(saveBtn);

  const testBtn = document.createElement("button");
  testBtn.className = "btn mini";
  testBtn.type = "button";
  testBtn.textContent = "Test";
  testBtn.onclick = async () => {
    await postJson("/api/ui/spoolman/test", {});
    await loadSpoolmanSpools(true).catch(() => []);
    await tick();
  };
  cfgBox.appendChild(testBtn);
  panel.appendChild(cfgBox);

  const mapWrap = document.createElement("div");
  mapWrap.className = "spoolmanMappings";
  for (const sid of buildSlotIds(connectedBoxes)) {
    const row = document.createElement("div");
    row.className = "spoolmanMapRow";
    const label = document.createElement("div");
    label.className = "spoolmanSlot";
    label.textContent = sid;
    row.appendChild(label);

    const selected = document.createElement("div");
    selected.className = "spoolmanSelected";
    const mapped = mappings[sid];
    const spool = findSpool(mapped);
    const slot = slotMeta(state, slots, sid);
    const sw = document.createElement("span");
    sw.className = "spoolmanSelectedSwatch" + (!mapped ? " empty" : "");
    sw.style.background = mapped ? spoolColor(spool, slot.color) : "transparent";
    selected.appendChild(sw);
    const text = document.createElement("span");
    text.className = "spoolmanSelectedText";
    text.textContent = spoolMappingText(mapped, spool);
    selected.appendChild(text);
    row.appendChild(selected);

    const actions = document.createElement("div");
    actions.className = "spoolmanMapActions";
    const selectBtn = document.createElement("button");
    selectBtn.className = "btn mini";
    selectBtn.type = "button";
    selectBtn.textContent = "Select Spool";
    selectBtn.onclick = () => openSpoolmanPicker(sid);
    actions.appendChild(selectBtn);
    const clearBtn = document.createElement("button");
    clearBtn.className = "btn mini danger";
    clearBtn.type = "button";
    clearBtn.textContent = "Clear";
    clearBtn.disabled = !mapped;
    clearBtn.onclick = async () => {
      await postJson("/api/ui/spoolman/mapping", { slot: sid, spool_id: null });
      await tick();
    };
    actions.appendChild(clearBtn);
    row.appendChild(actions);
    mapWrap.appendChild(row);
  }
  panel.appendChild(mapWrap);

  const recordTitle = document.createElement("div");
  recordTitle.className = "spoolmanSubTitle";
  recordTitle.textContent = "Recent sync records";
  panel.appendChild(recordTitle);
  const recWrap = document.createElement("div");
  recWrap.className = "spoolmanRecords";
  const entries = Object.entries(records).map(([key, rec]) => [key, rec || {}]).sort((a, b) => Number(b[1].updated_at || b[1].finished_at || 0) - Number(a[1].updated_at || a[1].finished_at || 0)).slice(0, 12);
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "tag muted";
    empty.textContent = "No sync records yet";
    recWrap.appendChild(empty);
  } else {
    for (const [key, rec] of entries) {
      const row = document.createElement("div");
      row.className = "spoolmanRecord " + String(rec.status || "");
      const main = document.createElement("div");
      main.className = "spoolmanRecordMain";
      main.textContent = `${rec.slot || "?"} -> ${rec.spool_id ? "#" + rec.spool_id : "unmapped"} - ${fmtMm(rec.used_mm || 0)}`;
      row.appendChild(main);
      const sub = document.createElement("div");
      sub.className = "spoolmanRecordSub";
      const phase = rec.sync_phase ? `${rec.sync_phase} - ` : "";
      sub.textContent = `${spoolmanStatusLabel(rec.status)} - ${phase}${rec.job || ""}`;
      row.appendChild(sub);
      if (rec.error) {
        const err = document.createElement("div");
        err.className = "spoolmanRecordErr";
        err.textContent = String(rec.error).slice(0, 220);
        row.appendChild(err);
      }
      if (rec.sync_phase !== "live" && ["failed", "pending", "skipped_invalid_spool", "skipped_unmapped", "dry_run"].includes(String(rec.status || ""))) {
        const retry = document.createElement("button");
        retry.className = "btn mini";
        retry.type = "button";
        retry.textContent = "Sync Now";
        retry.onclick = async () => {
          await postJson("/api/ui/spoolman/retry", { record_key: key });
          await tick();
        };
        row.appendChild(retry);
      }
      recWrap.appendChild(row);
    }
  }
  panel.appendChild(recWrap);
}

function render(state) {
  latestState = state;
  const printerOk = !!state.printer_connected;
  badge($("printerBadge"), printerOk ? "Printer: connected" : "Printer: disconnected", printerOk ? "ok" : "bad");
  if (!printerOk && state.printer_last_error && $("printerBadge")) $("printerBadge").textContent += " (" + state.printer_last_error + ")";
  const cfsOk = !!state.cfs_connected;
  badge($("cfsBadge"), cfsOk ? ("CFS: detected - " + fmtTs(state.cfs_last_update)) : "CFS: -", cfsOk ? "ok" : "warn");

  const slots = (state.cfs_slots && Object.keys(state.cfs_slots).length) ? state.cfs_slots : state.slots;
  const connectedBoxes = connectedBoxesFor(slots);
  const active = state.cfs_active_slot || null;

  const clearBtn = $("clearAccountingBtn");
  if (clearBtn) {
    clearBtn.style.display = isDebugMode() ? "inline-flex" : "none";
    clearBtn.disabled = !!state.job_track_name;
    clearBtn.title = state.job_track_name ? "A print is being tracked" : "Clear local test accounting data";
  }

  const boxesGrid = $("boxesGrid");
  if (boxesGrid) {
    boxesGrid.innerHTML = "";
    const boxesInfo = (slots && slots._boxes) ? slots._boxes : {};
    for (const boxNum of connectedBoxes) {
      const card = document.createElement("div");
      card.className = "card";
      const head = document.createElement("div");
      head.className = "cardHead";
      const title = document.createElement("div");
      title.className = "cardTitle";
      title.textContent = `Box ${boxNum}`;
      head.appendChild(title);
      const env = document.createElement("div");
      env.className = "cardMeta";
      const bi = boxesInfo[boxNum] || {};
      if (typeof bi.temperature_c === "number") {
        const t = document.createElement("span");
        t.className = "envItem";
        t.textContent = `${Math.round(bi.temperature_c)} C`;
        env.appendChild(t);
      }
      if (typeof bi.humidity_pct === "number") {
        const h = document.createElement("span");
        h.className = "envItem";
        h.textContent = `${Math.round(bi.humidity_pct)}% RH`;
        env.appendChild(h);
      }
      if (env.childNodes.length) head.appendChild(env);
      card.appendChild(head);
      const slotWrap = document.createElement("div");
      slotWrap.className = "slots";
      for (const letter of ["A", "B", "C", "D"]) {
        const sid = `${boxNum}${letter}`;
        slotWrap.appendChild(slotCard(sid, `Slot ${letter}`, slotMeta(state, slots, sid), sid === active));
      }
      card.appendChild(slotWrap);
      boxesGrid.appendChild(card);
    }
  }

  renderHistory(state, slots, connectedBoxes);
  renderMoonHistory(state);
  renderSpoolman(state, connectedBoxes, slots);

  const activeRow = $("activeRow");
  const activeMeta = $("activeMeta");
  const activeLive = $("activeLive");
  if (activeRow) activeRow.innerHTML = "";
  if (activeLive) {
    activeLive.style.display = "none";
    activeLive.innerHTML = "";
  }
  if (active && (slots?.[active] || state.slots?.[active])) {
    const m = slotMeta(state, slots, active);
    if (activeRow) activeRow.appendChild(slotCard(active, `Box ${active[0]} - Slot ${active[1]}`, m, true));
    if (activeMeta) activeMeta.textContent = m.material ? `${m.material} - ${m.color || ""}` : "-";
    const isPrinting = String(state.job_track_last_state || "").toLowerCase() === "printing";
    const slotMm = state.job_track_slot_mm && typeof state.job_track_slot_mm === "object" ? Number(state.job_track_slot_mm[active] || 0) : 0;
    const slotG = state.job_track_slot_g && typeof state.job_track_slot_g === "object" ? Number(state.job_track_slot_g[active] || 0) : 0;
    if (activeLive && isPrinting && slotMm > 0) {
      const p1 = document.createElement("span");
      p1.className = "pill";
      p1.textContent = `Live: ${fmtMm(slotMm)}`;
      activeLive.appendChild(p1);
      if (slotG > 0) {
        const p2 = document.createElement("span");
        p2.className = "pill";
        p2.textContent = fmtG(slotG);
        activeLive.appendChild(p2);
      }
      activeLive.style.display = "flex";
    }
  } else {
    if (activeMeta) activeMeta.textContent = "No active slot";
    if (activeRow) {
      const empty = document.createElement("div");
      empty.className = "tag muted";
      empty.textContent = "No active slot reported by CFS";
      activeRow.appendChild(empty);
    }
  }
}

async function tick() {
  try {
    const rightCol = document.querySelector(".rightCol");
    const scrollTop = rightCol ? rightCol.scrollTop : null;
    const j = await getJson("/api/ui/state");
    render(j.result || j);
    if (rightCol && scrollTop != null) rightCol.scrollTop = scrollTop;
  } catch {
    badge($("printerBadge"), "Printer: -", "warn");
    badge($("cfsBadge"), "CFS: -", "warn");
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
    btn.textContent = refreshPaused ? "Resume" : "Pause";
    btn.classList.toggle("paused", refreshPaused);
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

function initClearAccounting() {
  const btn = $("clearAccountingBtn");
  if (!btn) return;
  btn.onclick = async () => {
    if (!isDebugMode()) return;
    if (!confirm("Clear local test accounting data and sync records?")) return;
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

function boot() {
  initSpoolModal();
  initSpoolmanPicker();
  initSettingsModal();
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



