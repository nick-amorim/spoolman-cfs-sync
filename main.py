from __future__ import annotations

import asyncio
import json
import math
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from models.schemas import (
    ApiResponse,
    AppState,
    FeedRequest,
    JobSetRequest,
    JobUpdateRequest,
    MoonrakerAllocateRequest,
    RetractRequest,
    SelectSlotRequest,
    SetAutoRequest,
    SlotState,
    SpoolApplyUsageRequest,
    SpoolResetRequest,
    UiSetColorRequest,
    UiSpoolSetRemainingRequest,
    UiSpoolSetStartRequest,
    UiSlotResetRequest,
    UiSlotUpdateRequest,
    UiPrinterConfigRequest,
    UiSpoolmanConfigRequest,
    UiSpoolmanMappingRequest,
    UiSpoolmanRetryRequest,
    UpdateSlotRequest,
)


# ---- Pydantic v1/v2 compatibility helpers ----

def _model_dump(obj) -> dict:
    """Return a plain dict for both Pydantic v1 and v2 models."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj.dict()


def _model_validate(cls, data):
    """Validate/parse a dict into a Pydantic model (v1/v2 compatible)."""
    if hasattr(cls, "model_validate"):
        return cls.model_validate(data)
    return cls.parse_obj(data)


def _req_dump(obj, *, exclude_unset: bool = False) -> dict:
    """Dump request models (v1/v2 compatible) with optional exclude_unset."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_unset=exclude_unset)
    return obj.dict(exclude_unset=exclude_unset)


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
STATIC_DIR = APP_DIR / "static"
STATE_PATH = DATA_DIR / "state.json"
PROFILES_PATH = DATA_DIR / "profiles.json"
CONFIG_PATH = DATA_DIR / "config.json"
MOONRAKER_POLL_TASK: Optional[asyncio.Task] = None

DEFAULT_SLOTS = [
    "1A", "1B", "1C", "1D",
    "2A", "2B", "2C", "2D",
    "3A", "3B", "3C", "3D",
    "4A", "4B", "4C", "4D",
]

SPOOLMAN_SYNC_STATUSES = {
    "pending",
    "dry_run",
    "synced",
    "skipped_unmapped",
    "skipped_invalid_spool",
    "failed",
    "timeout_uncertain",
    "conflict",
}


def _now() -> float:
    return time.time()


def _default_spoolman_config() -> dict:
    return {
        "enabled": False,
        "dry_run": True,
        "url": "",
        "sync_mode": "post_print",
        "live_min_delta_mm": 100.0,
        "timeout_sec": 5,
        "slot_mappings": {sid: None for sid in DEFAULT_SLOTS},
    }


def _default_spoolman_status() -> dict:
    return {
        "connected": False,
        "last_check_at": 0.0,
        "last_error": "",
        "dry_run": True,
        "sync_mode": "post_print",
        "moonraker_native_detected": False,
        "moonraker_native_warning": "",
    }


def _normalize_spoolman_config(cfg: dict) -> dict:
    out = _default_spoolman_config()
    raw = None
    if isinstance(cfg, dict):
        raw = cfg.get("spoolman") if isinstance(cfg.get("spoolman"), dict) else cfg
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k != "slot_mappings"})

        mappings = dict(out["slot_mappings"])
        raw_map = raw.get("slot_mappings")
        if isinstance(raw_map, dict):
            for sid, val in raw_map.items():
                sid_s = str(sid).strip().upper()
                if sid_s not in mappings:
                    continue
                try:
                    iv = int(val) if val not in (None, "") else None
                    mappings[sid_s] = iv if iv and iv > 0 else None
                except Exception:
                    mappings[sid_s] = None
        out["slot_mappings"] = mappings

    out["enabled"] = bool(out.get("enabled", False))
    out["dry_run"] = bool(out.get("dry_run", True))
    out["url"] = str(out.get("url") or "").strip().rstrip("/")
    try:
        timeout = float(out.get("timeout_sec", 5) or 5)
        out["timeout_sec"] = max(1.0, min(timeout, 30.0))
    except Exception:
        out["timeout_sec"] = 5.0
    sync_mode = str(out.get("sync_mode") or "post_print").strip().lower()
    if sync_mode not in ("post_print", "live"):
        out["sync_mode"] = "post_print"
    else:
        out["sync_mode"] = sync_mode
    try:
        live_min = float(out.get("live_min_delta_mm", 100.0) or 100.0)
        out["live_min_delta_mm"] = max(1.0, min(live_min, 5000.0))
    except Exception:
        out["live_min_delta_mm"] = 100.0
    return out


def _spoolman_public_config() -> dict:
    cfg = _normalize_spoolman_config(load_config())
    return {
        "enabled": cfg["enabled"],
        "dry_run": cfg["dry_run"],
        "url": cfg["url"],
        "sync_mode": cfg["sync_mode"],
        "live_min_delta_mm": cfg["live_min_delta_mm"],
        "timeout_sec": cfg["timeout_sec"],
        "slot_mappings": cfg["slot_mappings"],
    }


def _normalize_printer_config(cfg: dict) -> dict:
    raw = cfg if isinstance(cfg, dict) else {}
    out = {
        "moonraker_url": str(raw.get("moonraker_url") or "").strip().rstrip("/"),
        "poll_interval_sec": 5.0,
        "filament_diameter_mm": 1.75,
        "cfs_autosync": bool(raw.get("cfs_autosync", False)),
    }
    try:
        poll_raw = raw.get("poll_interval_sec", 5)
        out["poll_interval_sec"] = max(1.0, min(float(poll_raw if poll_raw is not None else 5), 60.0))
    except Exception:
        out["poll_interval_sec"] = 5.0
    try:
        diameter_raw = raw.get("filament_diameter_mm", 1.75)
        out["filament_diameter_mm"] = max(0.5, min(float(diameter_raw if diameter_raw is not None else 1.75), 5.0))
    except Exception:
        out["filament_diameter_mm"] = 1.75
    return out


def _printer_public_config() -> dict:
    return _normalize_printer_config(load_config())


def _spoolman_record_key(job_key: str, slot_id: str) -> str:
    return f"{str(job_key or '').strip()}:{str(slot_id or '').strip().upper()}"


def _stable_print_key(job_name: str, start_ts: float, end_ts: float, job_id: Optional[str] = None) -> str:
    """Build a stable print-instance key for idempotent per-slot sync records."""
    jid = str(job_id or "").strip()
    if jid:
        return f"moonraker:{jid}"
    safe_name = str(job_name or "").strip() or "unknown-job"
    try:
        start_i = int(float(start_ts or 0))
    except Exception:
        start_i = 0
    try:
        end_i = int(float(end_ts or 0))
    except Exception:
        end_i = 0
    return f"local:{safe_name}:{start_i}:{end_i}"


def _moonraker_current_job_id(print_stats: dict, virtual_sdcard: dict) -> str:
    """Best-effort current job id extraction across Moonraker/Creality variants."""
    candidates = []
    if isinstance(print_stats, dict):
        candidates.extend([print_stats.get("job_id"), print_stats.get("uid"), print_stats.get("id")])
    if isinstance(virtual_sdcard, dict):
        candidates.extend([virtual_sdcard.get("job_id"), virtual_sdcard.get("uid"), virtual_sdcard.get("id")])
        cpd = virtual_sdcard.get("cur_print_data")
        if isinstance(cpd, dict):
            candidates.extend([cpd.get("job_id"), cpd.get("uid"), cpd.get("id")])
    for val in candidates:
        s = str(val or "").strip()
        if s:
            return s
    return ""


def _parse_iso_ts(val: str) -> Optional[float]:
    try:
        # Accept "Z" and timezone offsets
        if val.endswith("Z"):
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _ensure_data_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    if not PROFILES_PATH.exists():
        PROFILES_PATH.write_text(
            json.dumps(
                {
                    "PLA": {"density_g_cm3": 1.24, "notes": "Default profile"},
                    "ABS": {"density_g_cm3": 1.04, "notes": "Default profile"},
                    "PETG": {"density_g_cm3": 1.27, "notes": "Default profile"},
                    "TPU": {"density_g_cm3": 1.20, "notes": "Default profile"},
                    "ASA": {"density_g_cm3": 1.07, "notes": "Default profile"},
                    "PA": {"density_g_cm3": 1.15, "notes": "Default profile"},
                    "PC": {"density_g_cm3": 1.20, "notes": "Default profile"},
                    "OTHER": {"density_g_cm3": 1.20, "notes": "Fallback"},
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(
                {
                    # Optional: set this to enable automatic job usage reading from Moonraker
                    # Example: "http://192.168.178.148:7125"
                    "moonraker_url": "",
                    "poll_interval_sec": 5,
                    # Filament diameter used for mm->g conversion
                    "filament_diameter_mm": 1.75,
                    # If true, import material/color/name from detected CFS objects into local slots (read-only to printer)
                    "cfs_autosync": False,
                    "spoolman": _default_spoolman_config(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    if not STATE_PATH.exists():
        slots: Dict[str, dict] = {}
        for s in DEFAULT_SLOTS:
            slots[s] = _model_dump(SlotState(slot=s))
        state = {
            "active_slot": "1A",
            "auto_mode": False,
            "slots": slots,
            "current_job": "",
            "current_job_filament_mm": 0,
            "current_job_filament_g": 0.0,
            "last_accounted_job_mm": 0,
            "last_accounted_slot": None,
            # per-slot usage history (newest first)
            "slot_history": {},
            # in-flight job attribution (persisted so a restart doesn't lose the active print)
            "job_track_name": "",
            "job_track_id": "",
            "job_track_started_at": 0.0,
            "job_track_last_mm": 0,
            "job_track_slot_mm": {},
            "job_track_slot_g": {},
            "job_track_last_state": "",
            "job_track_file_path": "",
            "job_track_last_file_position": 0,
            "job_track_file_size": 0,
            "job_track_extruder_mode": "relative",
            "job_track_last_e": 0.0,
            "job_track_parser_slot": "",
            "job_track_parser_tail": "",
            "job_track_spoolman_live_synced_mm": {},
            "job_track_spoolman_live_last_attempt_mm": {},
            "job_track_spoolman_live_seq": {},
            "job_track_spoolman_live_blocked": {},
            # snapshot from Moonraker history (global list)
            "moonraker_history": [],
            # local manual allocations for Moonraker history -> slots
            "moonraker_allocations": {},
            "spoolman_status": _default_spoolman_status(),
            "spoolman_sync_records": {},
            "updated_at": _now(),
        }
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def load_profiles() -> dict:
    _ensure_data_files()
    try:
        return json.loads(PROFILES_PATH.read_text())
    except Exception:
        return {}


def load_config() -> dict:
    _ensure_data_files()
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {
            "moonraker_url": "",
            "poll_interval_sec": 5,
            "filament_diameter_mm": 1.75,
            "cfs_autosync": False,
            "spoolman": _default_spoolman_config(),
        }


def _migrate_state_dict(data: dict) -> dict:
    """Make state.json tolerant to older/hand-edited formats."""
    if not isinstance(data, dict):
        return data

    # updated_at: allow ISO string
    if isinstance(data.get("updated_at"), str):
        ts = _parse_iso_ts(data["updated_at"])
        if ts is not None:
            data["updated_at"] = ts

    # Some users wrote last_update instead of updated_at
    if "updated_at" not in data and "last_update" in data:
        if data["last_update"] is None:
            data["updated_at"] = 0.0
        elif isinstance(data["last_update"], str):
            data["updated_at"] = _parse_iso_ts(data["last_update"]) or 0.0
        else:
            try:
                data["updated_at"] = float(data["last_update"])
            except Exception:
                data["updated_at"] = 0.0

    # Ensure new fields exist
    data.setdefault("current_job", data.get("job", {}).get("name", ""))
    data.setdefault("current_job_filament_mm", int(data.get("job", {}).get("used_mm", 0) or 0))
    data.setdefault("current_job_filament_g", float(data.get("job", {}).get("used_g", 0.0) or 0.0))
    data.setdefault("last_accounted_job_mm", int(data.get("last_accounted_job_mm", 0) or 0))
    data.setdefault("last_accounted_slot", data.get("last_accounted_slot"))

    # Slots: allow keys like "2A": {material,color,...} without slot field
    slots = data.get("slots", {}) or {}
    if isinstance(slots, dict):
        for slot_id, sd in list(slots.items()):
            if not isinstance(sd, dict):
                continue
            sd.setdefault("slot", slot_id)
            # allow 'color' key
            if "color" in sd and "color_hex" not in sd:
                sd["color_hex"] = sd.pop("color")
            # legacy key 'vendor' -> 'manufacturer'
            if "vendor" in sd and "manufacturer" not in sd:
                sd["manufacturer"] = sd.pop("vendor")
            # tolerate placeholders for material
            mat = sd.get("material")
            if isinstance(mat, str) and mat.strip() in ("", "-", "\u2014", "\u2013"):
                sd["material"] = "OTHER"
            # allow 'remaining_g' as int
            if "remaining_g" in sd and sd["remaining_g"] is not None:
                try:
                    sd["remaining_g"] = float(sd["remaining_g"])
                except Exception:
                    sd["remaining_g"] = None
            slots[slot_id] = sd
        # ensure all CFS banks exist (1A-4D)
        for sid in (
            "1A", "1B", "1C", "1D",
            "2A", "2B", "2C", "2D",
            "3A", "3B", "3C", "3D",
            "4A", "4B", "4C", "4D",
        ):
            if sid not in slots:
                slots[sid] = {
                    "slot": sid,
                    "material": "OTHER",
                    "color_hex": "#00aaff",
                    "name": "",
                    "manufacturer": "",
                    "remaining_g": 0.0,
                    "notes": "",
                }
        data["slots"] = slots

    data.setdefault("printer_connected", False)
    data.setdefault("printer_last_error", "")

    data.setdefault("cfs_connected", False)
    data.setdefault("cfs_last_update", 0.0)
    data.setdefault("cfs_active_slot", None)
    data.setdefault("cfs_slots", {})
    data.setdefault("cfs_raw", {})

    # --- history defaults ---
    data.setdefault("slot_history", {})
    data.setdefault("job_track_name", "")
    data.setdefault("job_track_id", "")
    data.setdefault("job_track_started_at", 0.0)
    data.setdefault("job_track_last_mm", 0)
    data.setdefault("job_track_slot_mm", {})
    data.setdefault("job_track_slot_g", {})
    data.setdefault("job_track_last_state", "")
    data.setdefault("job_track_file_path", "")
    data.setdefault("job_track_last_file_position", 0)
    data.setdefault("job_track_file_size", 0)
    data.setdefault("job_track_extruder_mode", "relative")
    data.setdefault("job_track_last_e", 0.0)
    data.setdefault("job_track_parser_slot", "")
    data.setdefault("job_track_parser_tail", "")
    data.setdefault("job_track_spoolman_live_synced_mm", {})
    data.setdefault("job_track_spoolman_live_last_attempt_mm", {})
    data.setdefault("job_track_spoolman_live_seq", {})
    data.setdefault("job_track_spoolman_live_blocked", {})

    # Moonraker history snapshot
    data.setdefault("moonraker_history", [])
    data.setdefault("moonraker_allocations", {})

    # Spoolman sync state
    status = data.get("spoolman_status")
    if not isinstance(status, dict):
        status = {}
    merged_status = _default_spoolman_status()
    merged_status.update(status)
    data["spoolman_status"] = merged_status

    records = data.get("spoolman_sync_records")
    if not isinstance(records, dict):
        records = {}
    for key, rec in list(records.items()):
        if not isinstance(rec, dict):
            records.pop(key, None)
            continue
        status_val = str(rec.get("status") or "").strip()
        if status_val not in SPOOLMAN_SYNC_STATUSES:
            rec["status"] = "failed" if status_val else "pending"
        records[key] = rec
    data["spoolman_sync_records"] = records

    return data


def load_state() -> AppState:
    _ensure_data_files()
    try:
        data = json.loads(STATE_PATH.read_text())
        data = _migrate_state_dict(data)
        return _model_validate(AppState, data)
    except Exception as e:
        # Corrupt/partial state files should never prevent the app from starting.
        print(f"[STATE] load failed: {e}")
        return default_state()


def _job_key(job_id: str, ts_end: Optional[float], job: str) -> str:
    """Build a stable key for a job in our local allocation store."""
    j = (job_id or "").strip() or (job or "").strip()
    try:
        te = float(ts_end) if ts_end is not None else 0.0
    except Exception:
        te = 0.0
    return f"{j}:{te:.0f}"


def save_state(state: AppState) -> None:
    state.updated_at = _now()
    STATE_PATH.write_text(json.dumps(_model_dump(state), indent=2, ensure_ascii=False))


# --- Printer adapter (Dummy) ---
# Keep it minimal: this project is about material management.
# You can later replace these functions with real Moonraker/CFS actions.

def adapter_feed(mm: float) -> None:
    print(f"[ADAPTER] feed {mm}mm")


def adapter_retract(mm: float) -> None:
    print(f"[ADAPTER] retract {mm}mm")


# --- Conversion helpers ---

def mm_to_g(material: str, mm: float) -> float:
    cfg = load_config()
    d_mm = float(cfg.get("filament_diameter_mm", 1.75) or 1.75)
    profiles = load_profiles()
    density = float((profiles.get(material) or {}).get("density_g_cm3", 1.20))

    # grams = density(g/cm^3) * volume(cm^3)
    # volume = area * length
    # area(mm^2) = pi*(d/2)^2 ; to cm^2 => /100
    # length(mm) to cm => /10
    area_cm2 = math.pi * (d_mm / 2.0) ** 2 / 100.0
    length_cm = mm / 10.0
    g = density * area_cm2 * length_cm
    return float(max(0.0, g))


def _apply_job_usage(state: AppState, job_name: str, total_used_mm: int, slot_override: Optional[str] = None) -> None:
    """Update job counters.

    Note: We intentionally do NOT decrement remaining_g here anymore.
    Creality K2's CFS can change slots mid-print (multi-color). Accurate
    remaining deduction is handled by the per-slot tracker finalized at
    print end.
    """
    total_used_mm = int(max(0, total_used_mm))

    # Decide which slot to account against
    slot_id = slot_override or state.last_accounted_slot or state.active_slot

    # If job name changed, reset delta baseline
    if job_name != (state.current_job or ""):
        state.last_accounted_job_mm = 0

    delta_mm = max(0, total_used_mm - int(state.last_accounted_job_mm or 0))

    material = state.slots[slot_id].material

    # Update state
    state.current_job = job_name
    state.current_job_filament_mm = total_used_mm
    state.current_job_filament_g = mm_to_g(material, float(total_used_mm))
    state.last_accounted_job_mm = total_used_mm
    state.last_accounted_slot = slot_id


def _hist_push(state: AppState, slot_id: str, entry: dict, keep: int = 50) -> None:
    """Append a history entry for a slot (newest first)."""
    try:
        # Tag entries with the current spool epoch so UI can hide old-roll prints
        try:
            entry.setdefault("epoch", int(getattr(state.slots.get(slot_id), "spool_epoch", 0) or 0))
        except Exception:
            entry.setdefault("epoch", 0)
        h = state.slot_history.get(slot_id)
        if not isinstance(h, list):
            h = []
        h.insert(0, entry)
        state.slot_history[slot_id] = h[:keep]
    except Exception:
        # never fail the poll loop due to history
        pass


def _hist_upsert_by_src(state: AppState, slot_id: str, src: str, entry: dict, keep: int = 50) -> None:
    """Insert or replace a history entry identified by a stable _src marker.

    Used to show a "live" (in-progress) entry per slot during printing without
    spamming the history list.
    """
    try:
        if not src:
            _hist_push(state, slot_id, entry, keep=keep)
            return

        entry["_src"] = src

        # Tag entries with the current spool epoch so UI can hide old-roll prints
        try:
            entry.setdefault("epoch", int(getattr(state.slots.get(slot_id), "spool_epoch", 0) or 0))
        except Exception:
            entry.setdefault("epoch", 0)

        h = state.slot_history.get(slot_id)
        if not isinstance(h, list):
            h = []

        # Drop existing entries with same source marker
        h = [e for e in h if not (isinstance(e, dict) and e.get("_src") == src)]
        h.insert(0, entry)
        state.slot_history[slot_id] = h[:keep]
    except Exception:
        # never fail the poll loop due to history
        pass


def _inc_slot_epoch_consumed(state: AppState, slot_id: str, delta_g: float) -> None:
    """Increment the running consumed-total for the current spool epoch."""
    try:
        s = state.slots.get(slot_id)
        if not s:
            return
        s.spool_epoch_consumed_g_total = float(getattr(s, "spool_epoch_consumed_g_total", 0.0) or 0.0) + float(delta_g)
        state.slots[slot_id] = s
    except Exception:
        return


# --- Minimal Moonraker polling (optional) ---

def _http_get_json(url: str, timeout: float = 2.5) -> dict:
    # NOTE: FastAPI also exports a Request type; avoid name clash by using
    # UrlRequest for outbound HTTP requests.
    req = UrlRequest(url, headers={"User-Agent": "filament-manager/1.0"})
    with urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return json.loads(raw)


_GCODE_E_RE = re.compile(r"(?:^|\s)E([-+]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
_GCODE_TOOL_DIRECT_RE = re.compile(r"^T([1-4])([A-D])(?:\s|$)", re.IGNORECASE)
_GCODE_TOOL_INDEX_RE = re.compile(r"^T([0-3])(?:\s|$)", re.IGNORECASE)


def _job_track_total_mm(state: AppState) -> int:
    try:
        return int(round(sum(float(v or 0) for v in (state.job_track_slot_mm or {}).values())))
    except Exception:
        return 0


def _moonraker_gcode_path(filename: str, virtual_sdcard: dict) -> str:
    candidates = []
    if isinstance(virtual_sdcard, dict):
        candidates.append(virtual_sdcard.get("file_path"))
        cpd = virtual_sdcard.get("cur_print_data")
        if isinstance(cpd, dict):
            candidates.extend([cpd.get("file_path"), cpd.get("filename"), cpd.get("name")])
    candidates.append(filename)
    for val in candidates:
        raw = str(val or "").strip()
        if not raw:
            continue
        raw = raw.replace("\\", "/")
        marker = "/gcodes/"
        if marker in raw:
            raw = raw.split(marker, 1)[1]
        elif raw.startswith("/"):
            raw = raw.rsplit("/", 1)[-1]
        return raw.lstrip("/")
    return ""


def _moonraker_file_url(base: str, gcode_path: str) -> str:
    base = str(base or "").strip().rstrip("/")
    safe_path = "/".join(quote(part, safe="") for part in str(gcode_path or "").split("/") if part)
    return f"{base}/server/files/gcodes/{safe_path}"


def _http_get_text_range(url: str, start: int, end: int, timeout: float = 10.0) -> str:
    start_i = max(0, int(start or 0))
    end_i = max(start_i, int(end or 0))
    headers = {
        "User-Agent": "filament-manager/1.0",
        "Range": f"bytes={start_i}-{end_i}",
    }
    req = UrlRequest(url, headers=headers)
    with urlopen(req, timeout=timeout) as r:
        data = r.read()
        status = int(getattr(r, "status", 200) or 200)
    if status == 200 and start_i > 0:
        data = data[start_i : end_i + 1]
    return data.decode("utf-8", errors="ignore")


def _gcode_tool_to_slot(command: str) -> Optional[str]:
    cmd = str(command or "").strip().upper()
    direct = _GCODE_TOOL_DIRECT_RE.match(cmd)
    if direct:
        return f"{direct.group(1)}{direct.group(2)}"
    indexed = _GCODE_TOOL_INDEX_RE.match(cmd)
    if indexed:
        idx = int(indexed.group(1))
        return f"1{'ABCD'[idx]}"
    return None


def _parse_gcode_usage_chunk(
    state: AppState,
    text: str,
    *,
    fallback_slot: str = "",
    final: bool = False,
) -> int:
    chunk = f"{state.job_track_parser_tail or ''}{text or ''}"
    if not chunk:
        return 0

    lines = chunk.splitlines(keepends=True)
    if lines and not (chunk.endswith("\n") or chunk.endswith("\r")) and not final:
        state.job_track_parser_tail = lines.pop()
    else:
        state.job_track_parser_tail = ""

    current_slot = str(state.job_track_parser_slot or fallback_slot or state.cfs_active_slot or state.active_slot or "").strip().upper()
    mode = str(state.job_track_extruder_mode or "relative").strip().lower()
    if mode not in ("relative", "absolute"):
        mode = "relative"
    last_e = float(state.job_track_last_e or 0.0)
    added_total = 0

    for raw_line in lines:
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            continue
        upper = line.upper()

        slot = _gcode_tool_to_slot(upper)
        if slot:
            current_slot = slot
            continue

        if upper.startswith("M83"):
            mode = "relative"
            continue
        if upper.startswith("M82"):
            mode = "absolute"
            continue
        if upper.startswith("G92"):
            e_match = _GCODE_E_RE.search(upper)
            if e_match:
                try:
                    last_e = float(e_match.group(1))
                except Exception:
                    last_e = 0.0
            continue
        if not (upper.startswith("G0") or upper.startswith("G1")):
            continue

        e_match = _GCODE_E_RE.search(upper)
        if not e_match:
            continue
        try:
            e_val = float(e_match.group(1))
        except Exception:
            continue

        if mode == "absolute":
            delta = e_val - last_e
            last_e = e_val
        else:
            delta = e_val
        if delta <= 0:
            continue

        slot_id = current_slot if current_slot in DEFAULT_SLOTS else fallback_slot
        if not slot_id:
            continue
        delta_mm = float(delta)
        if delta_mm <= 0:
            continue
        state.job_track_slot_mm[slot_id] = float(state.job_track_slot_mm.get(slot_id, 0.0)) + delta_mm
        added_total += int(round(delta_mm))
        try:
            mat = state.slots.get(slot_id).material if slot_id in state.slots else "OTHER"
            g_delta = float(mm_to_g(str(mat), float(delta_mm)))
        except Exception:
            g_delta = 0.0
        if g_delta > 0:
            state.job_track_slot_g[slot_id] = float(state.job_track_slot_g.get(slot_id, 0.0)) + g_delta
            _inc_slot_epoch_consumed(state, slot_id, float(g_delta))

    state.job_track_parser_slot = current_slot
    state.job_track_extruder_mode = mode
    state.job_track_last_e = float(last_e)
    return added_total


def _track_usage_from_gcode_file(
    state: AppState,
    *,
    base_url: str,
    filename: str,
    virtual_sdcard: dict,
    file_position: int,
    fallback_slot: str,
) -> bool:
    gcode_path = _moonraker_gcode_path(filename, virtual_sdcard)
    if not gcode_path:
        return False

    if state.job_track_file_path and state.job_track_file_path != gcode_path:
        state.job_track_last_file_position = 0
        state.job_track_file_size = 0
        state.job_track_parser_tail = ""
        state.job_track_last_e = 0.0
        state.job_track_parser_slot = ""
    state.job_track_file_path = gcode_path

    last_pos = int(state.job_track_last_file_position or 0)
    curr_pos = int(max(0, file_position or 0))
    if curr_pos <= last_pos:
        return True

    url = _moonraker_file_url(base_url, gcode_path)
    chunk = _http_get_text_range(url, last_pos, curr_pos - 1)
    _parse_gcode_usage_chunk(state, chunk, fallback_slot=fallback_slot)
    state.job_track_last_file_position = curr_pos
    state.job_track_last_mm = _job_track_total_mm(state)
    return True


def _finish_gcode_usage_tracking(
    state: AppState,
    *,
    base_url: str,
    fallback_slot: str,
    completed: bool,
) -> None:
    if not completed:
        _parse_gcode_usage_chunk(state, "", fallback_slot=fallback_slot, final=True)
        state.job_track_last_mm = _job_track_total_mm(state)
        return
    gcode_path = str(state.job_track_file_path or "").strip()
    file_size = int(state.job_track_file_size or 0)
    last_pos = int(state.job_track_last_file_position or 0)
    if gcode_path and file_size > last_pos:
        url = _moonraker_file_url(base_url, gcode_path)
        chunk = _http_get_text_range(url, last_pos, file_size - 1)
        _parse_gcode_usage_chunk(state, chunk, fallback_slot=fallback_slot, final=True)
        state.job_track_last_file_position = file_size
    else:
        _parse_gcode_usage_chunk(state, "", fallback_slot=fallback_slot, final=True)
    state.job_track_last_mm = _job_track_total_mm(state)


class SpoolmanTimeoutError(Exception):
    pass


class SpoolmanHttpError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _spoolman_build_url(path: str, cfg: Optional[dict] = None) -> str:
    scfg = _normalize_spoolman_config(cfg or load_config())
    base = scfg.get("url") or ""
    if not base:
        raise SpoolmanHttpError(0, "Spoolman URL is not configured")
    p = str(path or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    if not p.startswith("/api/v1/") and p != "/api/v1":
        p = "/api/v1" + p
    return base.rstrip("/") + p


def _spoolman_request_json(path: str, *, method: str = "GET", payload: Optional[dict] = None, cfg: Optional[dict] = None) -> dict:
    scfg = _normalize_spoolman_config(cfg or load_config())
    url = _spoolman_build_url(path, scfg)
    data = None
    headers = {"User-Agent": "spoolman-cfs-sync/0.1"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = UrlRequest(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=float(scfg.get("timeout_sec", 5) or 5)) as r:
            raw = r.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}
    except (socket.timeout, TimeoutError) as e:
        raise SpoolmanTimeoutError(str(e) or "Spoolman request timed out") from e
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise SpoolmanHttpError(int(e.code or 0), body or str(e)) from e
    except URLError as e:
        reason = getattr(e, "reason", "")
        if isinstance(reason, socket.timeout):
            raise SpoolmanTimeoutError(str(reason) or "Spoolman request timed out") from e
        raise SpoolmanHttpError(0, str(reason or e)) from e


def _spoolman_health(cfg: Optional[dict] = None) -> dict:
    return _spoolman_request_json("/health", cfg=cfg)


def _spoolman_get_spool(spool_id: int, cfg: Optional[dict] = None) -> dict:
    return _spoolman_request_json(f"/spool/{int(spool_id)}", cfg=cfg)


def _spoolman_list_spools(cfg: Optional[dict] = None) -> list:
    data = _spoolman_request_json("/spool", cfg=cfg)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "results", "spools", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def _spoolman_use_spool(spool_id: int, used_mm: float, cfg: Optional[dict] = None) -> dict:
    return _spoolman_request_json(
        f"/spool/{int(spool_id)}/use",
        method="PUT",
        payload={"use_length": float(max(0.0, used_mm))},
        cfg=cfg,
    )


def _set_spoolman_status(state: AppState, **updates) -> None:
    status = dict(getattr(state, "spoolman_status", {}) or {})
    merged = _default_spoolman_status()
    merged.update(status)
    merged.update(updates)
    state.spoolman_status = merged


def _mark_spoolman_use_uncertain(state: AppState, record: dict, spool_id: int, reason: str) -> None:
    record["status"] = "timeout_uncertain"
    record["error"] = (
        "Spoolman usage request result is uncertain. Spoolman may already have "
        f"deducted this usage from spool {spool_id}. Verify Spoolman inventory before retrying. {reason}"
    )
    _set_spoolman_status(state, connected=False, last_check_at=_now(), last_error=record["error"])


def _record_usage_matches(existing: dict, used_mm: float, used_g: float, spool_id: Optional[int]) -> bool:
    try:
        return (
            abs(float(existing.get("used_mm") or 0.0) - float(used_mm or 0.0)) < 0.001
            and abs(float(existing.get("used_g") or 0.0) - float(used_g or 0.0)) < 0.001
        )
    except Exception:
        return False


def _base_spoolman_record(
    *,
    job_key: str,
    job_name: str,
    slot_id: str,
    spool_id: Optional[int],
    used_mm: float,
    used_g: float,
    result: str,
    status: str = "pending",
    error: str = "",
) -> dict:
    now = _now()
    return {
        "job_key": job_key,
        "job": job_name,
        "slot": slot_id,
        "spool_id": int(spool_id) if spool_id else None,
        "used_mm": float(round(float(used_mm or 0.0), 3)),
        "used_g": float(round(float(used_g or 0.0), 3)),
        "result": result,
        "status": status,
        "attempts": 0,
        "last_attempt_at": 0.0,
        "synced_at": None,
        "created_at": now,
        "updated_at": now,
        "error": error,
    }


def _spoolman_record_key_phase(job_key: str, slot_id: str, phase: str) -> str:
    return f"{_spoolman_record_key(job_key, slot_id)}:{str(phase or '').strip()}"


def _save_spoolman_record(state: AppState, key: str, record: dict) -> None:
    record["updated_at"] = _now()
    records = dict(getattr(state, "spoolman_sync_records", {}) or {})
    records[key] = record
    state.spoolman_sync_records = records
    save_state(state)


def _spoolman_sync_record(state: AppState, key: str, record: dict, cfg: dict) -> dict:
    existing = (getattr(state, "spoolman_sync_records", {}) or {}).get(key)
    if isinstance(existing, dict):
        existing_status = str(existing.get("status") or "")
        if existing_status == "synced":
            return existing
        if existing_status == "timeout_uncertain":
            return existing
        if not _record_usage_matches(existing, record["used_mm"], record["used_g"], record.get("spool_id")):
            conflict = dict(existing)
            conflict["status"] = "conflict"
            conflict["error"] = "Existing sync record has different usage; refusing to resend."
            _save_spoolman_record(state, key, conflict)
            return conflict
        record["attempts"] = int(existing.get("attempts") or 0)
        record["created_at"] = existing.get("created_at") or record.get("created_at")

    spool_id = record.get("spool_id")
    if not spool_id:
        record["status"] = "skipped_unmapped"
        record["error"] = "No Spoolman spool id is mapped for this CFS slot."
        _save_spoolman_record(state, key, record)
        return record

    if bool(cfg.get("dry_run", True)):
        record["status"] = "dry_run"
        record["error"] = ""
        _save_spoolman_record(state, key, record)
        return record

    if not bool(cfg.get("enabled", False)):
        record["status"] = "pending"
        record["error"] = "Spoolman sync is disabled."
        _save_spoolman_record(state, key, record)
        return record

    record["status"] = "pending"
    record["attempts"] = int(record.get("attempts") or 0) + 1
    record["last_attempt_at"] = _now()
    _save_spoolman_record(state, key, record)

    try:
        _spoolman_get_spool(int(spool_id), cfg)
        _set_spoolman_status(state, connected=True, last_check_at=_now(), last_error="")
    except SpoolmanHttpError as e:
        if e.status_code == 404:
            record["status"] = "skipped_invalid_spool"
            record["error"] = f"Spoolman spool id {spool_id} was not found."
        else:
            record["status"] = "failed"
            record["error"] = str(e)
            _set_spoolman_status(state, connected=False, last_check_at=_now(), last_error=str(e))
        _save_spoolman_record(state, key, record)
        return record
    except Exception as e:
        record["status"] = "failed"
        record["error"] = str(e)
        _set_spoolman_status(state, connected=False, last_check_at=_now(), last_error=str(e))
        _save_spoolman_record(state, key, record)
        return record

    try:
        _spoolman_use_spool(int(spool_id), float(record.get("used_mm") or 0.0), cfg)
        record["status"] = "synced"
        record["synced_at"] = _now()
        record["error"] = ""
        _set_spoolman_status(state, connected=True, last_check_at=_now(), last_error="")
    except SpoolmanTimeoutError as e:
        _mark_spoolman_use_uncertain(state, record, int(spool_id), str(e) or "Request timed out.")
    except SpoolmanHttpError as e:
        if e.status_code and 400 <= int(e.status_code) < 500 and int(e.status_code) != 408:
            record["status"] = "failed"
            record["error"] = str(e)
            _set_spoolman_status(state, connected=False, last_check_at=_now(), last_error=str(e))
        else:
            _mark_spoolman_use_uncertain(
                state,
                record,
                int(spool_id),
                f"HTTP status {e.status_code}: {e}",
            )
    except Exception as e:
        _mark_spoolman_use_uncertain(state, record, int(spool_id), str(e))

    _save_spoolman_record(state, key, record)
    return record


def _live_spoolman_print_key(job_name: str, start_ts: float, job_id: Optional[str] = None) -> str:
    if str(job_id or "").strip():
        return _stable_print_key(job_name, start_ts, 0, job_id=job_id)
    safe_name = str(job_name or "unknown").replace(":", "_")
    return f"live:{safe_name}:{int(float(start_ts or 0.0))}"


def _live_spoolman_maps(state: AppState) -> tuple[dict, dict, dict, dict]:
    synced = getattr(state, "job_track_spoolman_live_synced_mm", {})
    attempted = getattr(state, "job_track_spoolman_live_last_attempt_mm", {})
    seqs = getattr(state, "job_track_spoolman_live_seq", {})
    blocked = getattr(state, "job_track_spoolman_live_blocked", {})
    if not isinstance(synced, dict):
        synced = {}
    if not isinstance(attempted, dict):
        attempted = {}
    if not isinstance(seqs, dict):
        seqs = {}
    if not isinstance(blocked, dict):
        blocked = {}
    return synced, attempted, seqs, blocked


def _slot_used_g_for_mm(state: AppState, slot_id: str, used_mm: float, slot_g_total: dict, slot_mm_total: dict) -> float:
    try:
        total_mm = float(slot_mm_total.get(slot_id, 0.0) or 0.0)
        total_g = float(slot_g_total.get(slot_id, 0.0) or 0.0)
        if total_mm > 0 and total_g > 0:
            return float(used_mm) * (total_g / total_mm)
    except Exception:
        pass
    try:
        mat = state.slots.get(slot_id).material if slot_id in state.slots else "OTHER"
        return float(mm_to_g(str(mat), float(used_mm)))
    except Exception:
        return 0.0


def _has_blocking_live_spoolman_record(state: AppState, job_key: str, slot_id: str) -> Optional[dict]:
    prefix = f"{_spoolman_record_key(job_key, slot_id)}:live:"
    records = getattr(state, "spoolman_sync_records", {}) or {}
    if not isinstance(records, dict):
        return None
    for key, rec in records.items():
        if not str(key).startswith(prefix) or not isinstance(rec, dict):
            continue
        if str(rec.get("status") or "") in ("timeout_uncertain", "conflict"):
            return rec
    return None


def _plan_spoolman_live_sync_for_current_job(state: AppState) -> None:
    cfg = _normalize_spoolman_config(load_config())
    if str(cfg.get("sync_mode") or "") != "live":
        return
    if not (bool(cfg.get("dry_run", True)) or bool(cfg.get("enabled", False))):
        return

    job_name = str(getattr(state, "job_track_name", "") or "").strip()
    if not job_name:
        return
    start_ts = float(getattr(state, "job_track_started_at", 0.0) or 0.0)
    job_id = str(getattr(state, "job_track_id", "") or "").strip()
    job_key = _live_spoolman_print_key(job_name, start_ts, job_id=job_id)

    slot_mm = getattr(state, "job_track_slot_mm", {}) if isinstance(getattr(state, "job_track_slot_mm", {}), dict) else {}
    slot_g = getattr(state, "job_track_slot_g", {}) if isinstance(getattr(state, "job_track_slot_g", {}), dict) else {}
    mappings = cfg.get("slot_mappings") if isinstance(cfg.get("slot_mappings"), dict) else {}
    synced, attempted, seqs, blocked = _live_spoolman_maps(state)
    min_delta = float(cfg.get("live_min_delta_mm", 100.0) or 100.0)
    changed = False

    for sid, total_val in slot_mm.items():
        sid_s = str(sid).strip().upper()
        if sid_s not in DEFAULT_SLOTS:
            continue
        if sid_s in blocked:
            continue
        spool_id = mappings.get(sid_s)
        if not spool_id:
            continue
        try:
            total_mm = float(total_val or 0.0)
            synced_mm = float(synced.get(sid_s, 0.0) or 0.0)
            attempted_mm = float(attempted.get(sid_s, 0.0) or 0.0)
        except Exception:
            continue
        if total_mm <= 0 or total_mm <= synced_mm:
            continue
        if (total_mm - attempted_mm) < min_delta:
            continue

        used_mm = float(max(0.0, total_mm - synced_mm))
        used_g = _slot_used_g_for_mm(state, sid_s, used_mm, slot_g, slot_mm)
        seq = int(seqs.get(sid_s, 0) or 0) + 1
        record = _base_spoolman_record(
            job_key=job_key,
            job_name=job_name,
            slot_id=sid_s,
            spool_id=spool_id,
            used_mm=used_mm,
            used_g=used_g,
            result="printing",
        )
        record["sync_phase"] = "live"
        record["total_mm_after"] = float(round(total_mm, 3))
        key = _spoolman_record_key_phase(job_key, sid_s, f"live:{seq}")

        rec = _spoolman_sync_record(state, key, record, cfg)
        status = str(rec.get("status") or "")
        attempted[sid_s] = total_mm
        seqs[sid_s] = seq
        changed = True
        if status == "synced":
            synced[sid_s] = total_mm
        elif status in ("timeout_uncertain", "conflict"):
            blocked[sid_s] = {
                "record_key": key,
                "status": status,
                "error": rec.get("error", ""),
                "at_mm": total_mm,
            }

    state.job_track_spoolman_live_synced_mm = synced
    state.job_track_spoolman_live_last_attempt_mm = attempted
    state.job_track_spoolman_live_seq = seqs
    state.job_track_spoolman_live_blocked = blocked
    if changed:
        save_state(state)


def _plan_spoolman_sync_for_finished_job(
    state: AppState,
    job_name: str,
    start_ts: float,
    end_ts: float,
    result: str,
    job_id: Optional[str] = None,
    printer_total_mm: Optional[float] = None,
) -> None:
    cfg = _normalize_spoolman_config(load_config())
    if not (bool(cfg.get("dry_run", True)) or bool(cfg.get("enabled", False))):
        return

    slot_mm = getattr(state, "job_track_slot_mm", {}) if isinstance(getattr(state, "job_track_slot_mm", {}), dict) else {}
    slot_g = getattr(state, "job_track_slot_g", {}) if isinstance(getattr(state, "job_track_slot_g", {}), dict) else {}
    mappings = cfg.get("slot_mappings") if isinstance(cfg.get("slot_mappings"), dict) else {}
    job_key = _stable_print_key(job_name, start_ts, end_ts, job_id=job_id)
    live_mode = str(cfg.get("sync_mode") or "") == "live"
    live_job_key = _live_spoolman_print_key(job_name, start_ts, job_id=job_id)
    live_synced, _, _, live_blocked = _live_spoolman_maps(state)
    scale = 1.0
    try:
        parsed_total_mm = sum(max(0.0, float(v or 0.0)) for v in slot_mm.values())
        printer_total = float(printer_total_mm or 0.0)
        if printer_total > 0 and parsed_total_mm > printer_total:
            scale = max(0.0, min(1.0, printer_total / parsed_total_mm))
    except Exception:
        scale = 1.0

    for sid, mm_val in slot_mm.items():
        sid_s = str(sid).strip().upper()
        try:
            total_mm = float(mm_val or 0.0) * scale
        except Exception:
            total_mm = 0.0
        if total_mm <= 0:
            continue

        if live_mode:
            if sid_s in live_blocked or _has_blocking_live_spoolman_record(state, live_job_key, sid_s):
                record = _base_spoolman_record(
                    job_key=job_key,
                    job_name=job_name,
                    slot_id=sid_s,
                    spool_id=mappings.get(sid_s),
                    used_mm=max(0.0, total_mm - float(live_synced.get(sid_s, 0.0) or 0.0)),
                    used_g=_slot_used_g_for_mm(
                        state,
                        sid_s,
                        max(0.0, total_mm - float(live_synced.get(sid_s, 0.0) or 0.0)),
                        slot_g,
                        slot_mm,
                    ),
                    result=result,
                    status="timeout_uncertain",
                    error="Live Spoolman sync for this slot has an uncertain record. Verify Spoolman inventory before final reconciliation.",
                )
                record["sync_phase"] = "final"
                _save_spoolman_record(state, _spoolman_record_key_phase(job_key, sid_s, "final"), record)
                continue
            try:
                used_mm = max(0.0, total_mm - float(live_synced.get(sid_s, 0.0) or 0.0))
            except Exception:
                used_mm = total_mm
        else:
            used_mm = total_mm
        if used_mm <= 0:
            continue
        used_g = _slot_used_g_for_mm(state, sid_s, used_mm, slot_g, slot_mm)

        spool_id = mappings.get(sid_s)
        record = _base_spoolman_record(
            job_key=job_key,
            job_name=job_name,
            slot_id=sid_s,
            spool_id=spool_id,
            used_mm=used_mm,
            used_g=used_g,
            result=result,
        )
        record["sync_phase"] = "final" if live_mode else "post_print"
        key = _spoolman_record_key_phase(job_key, sid_s, "final") if live_mode else _spoolman_record_key(job_key, sid_s)
        _spoolman_sync_record(state, key, record, cfg)


def _update_spoolman_config(update: dict) -> dict:
    cfg = load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    spool_cfg = _normalize_spoolman_config(cfg)
    raw = cfg.get("spoolman") if isinstance(cfg.get("spoolman"), dict) else {}
    raw = dict(raw)
    raw.update(spool_cfg)
    for key in ("enabled", "dry_run", "url", "timeout_sec", "sync_mode", "live_min_delta_mm"):
        if key in update and update[key] is not None:
            raw[key] = update[key]
    cfg["spoolman"] = _normalize_spoolman_config({"spoolman": raw})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    return _normalize_spoolman_config(cfg)


def _update_printer_config(update: dict) -> dict:
    cfg = load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    raw = dict(cfg)
    for key in ("moonraker_url", "poll_interval_sec", "filament_diameter_mm", "cfs_autosync"):
        if key in update and update[key] is not None:
            raw[key] = update[key]
    normalized = _normalize_printer_config(raw)
    cfg.update(normalized)
    cfg.setdefault("spoolman", _default_spoolman_config())
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    return normalized


def _update_spoolman_mapping(slot_id: str, spool_id: Optional[int]) -> dict:
    cfg = load_config()
    spool_cfg = _normalize_spoolman_config(cfg)
    mappings = dict(spool_cfg.get("slot_mappings") or {})
    sid = str(slot_id or "").strip().upper()
    if sid not in mappings:
        raise HTTPException(status_code=404, detail="Unknown slot")
    mappings[sid] = int(spool_id) if spool_id else None
    raw = cfg.get("spoolman") if isinstance(cfg.get("spoolman"), dict) else {}
    raw = dict(raw)
    raw["slot_mappings"] = mappings
    cfg["spoolman"] = _normalize_spoolman_config({"spoolman": raw})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    return _normalize_spoolman_config(cfg)


def _moonraker_fetch_history(base: str, limit: int = 20) -> list[dict]:
    """Fetch Moonraker job history list (best effort).

    Moonraker provides this at:
      GET /server/history/list?limit=<n>&order=desc
    Note: Creality firmware usually exposes the history component, but
    per-slot attribution is not guaranteed.
    """
    try:
        url = base.rstrip("/") + "/server/history/list?" + urlencode({"limit": int(limit), "order": "desc"})
        data = _http_get_json(url, timeout=3.5)
        jobs = (((data or {}).get("result") or {}).get("jobs") or [])
        out: list[dict] = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            fn = j.get("filename") or ""
            if isinstance(fn, str) and "/" in fn:
                fn = fn.rsplit("/", 1)[-1]
            # Moonraker reports filament_used as float; documentation says mm,
            # however some frontends treat it as meters. We keep both a raw
            # value and a derived mm estimate.
            fu = j.get("filament_used")
            fu_raw = None
            fu_mm = None
            try:
                fu_raw = float(fu)
                # Heuristic: if the value is small (< 200) it's likely meters.
                # Otherwise treat it as mm.
                fu_mm = fu_raw * 1000.0 if fu_raw < 200 else fu_raw
            except Exception:
                pass

            meta = j.get("metadata") or {}
            fu_g_list = None
            try:
                lst = meta.get("filament_used_g")
                if isinstance(lst, list) and lst:
                    fu_g_list = [float(x) for x in lst]
            except Exception:
                fu_g_list = None

            # If firmware didn't provide grams, compute a best-effort estimate from mm + filament_type
            fu_g_total = None
            try:
                if isinstance(fu_g_list, list) and fu_g_list:
                    fu_g_total = float(sum(fu_g_list))
                elif fu_mm is not None:
                    mat = None
                    if isinstance(meta, dict):
                        mat = meta.get("filament_type")
                    mat_s = str(mat).strip().upper() if mat else "OTHER"
                    fu_g_total = float(mm_to_g(mat_s, float(fu_mm)))
            except Exception:
                fu_g_total = None

            out.append(
                {
                    "job_id": j.get("job_id") or j.get("uid") or "",
                    "ts_start": j.get("start_time"),
                    "ts_end": j.get("end_time"),
                    "status": j.get("status") or "",
                    "job": fn,
                    "filament_used_raw": fu_raw,
                    "filament_used_mm": fu_mm,
                    "filament_used_g": fu_g_list,
                    "filament_used_g_total": (float(round(fu_g_total, 2)) if fu_g_total is not None else None),
                    "filament_type": (meta.get("filament_type") if isinstance(meta, dict) else None),
                    "colors": (meta.get("default_filament_colour") if isinstance(meta, dict) else None),
                }
            )
        return out
    except Exception:
        return []

def _moonraker_build_url(base: str, objects: list[str]) -> str:
    """Build Moonraker objects/query URL.

    Moonraker supports multiple syntaxes depending on version/vendor fork.
    Creality K-series (K2 Plus) reliably supports the ampersand form:
      /printer/objects/query?print_stats&virtual_sdcard&box&filament_rack

    Some upstream versions also accept `objects=toolhead,print_stats`, but that
    isn't consistently supported on Creality firmware. For maximum compatibility
    we use the ampersand form.
    """
    safe = [quote(str(o).strip(), safe="") for o in (objects or []) if str(o).strip()]
    qs = "&".join(safe)
    return base.rstrip("/") + "/printer/objects/query?" + qs


def _moonraker_list_objects(base: str) -> list[str]:
    data = _http_get_json(base.rstrip("/") + "/printer/objects/list")
    return list((((data or {}).get("result") or {}).get("objects") or []))


def _moonraker_cfs_objects(base: str) -> list[str]:
    cfs_objects: list[str] = []
    objs = _moonraker_list_objects(base)
    for o in objs:
        lo = str(o).lower()
        if any(x in lo for x in ("cfs", "ams", "mmu", "spool", "filament_box", "filamentbox")):
            cfs_objects.append(str(o))
        # Creality K-series / K2 Plus objects
        if lo in ("box", "filament_rack"):
            cfs_objects.append(str(o))
    seen: set[str] = set()
    out: list[str] = []
    for obj in cfs_objects:
        if obj in seen:
            continue
        seen.add(obj)
        out.append(obj)
    return out[:12]


def _clear_cfs_connection(state: AppState) -> None:
    state.cfs_connected = False
    state.cfs_active_slot = None
    state.cfs_slots = {}
    state.cfs_raw = {}


def _moonraker_detect_native_spoolman(base: str) -> tuple[bool, str]:
    """Best-effort detection of Moonraker's native Spoolman component."""
    warning = (
        "Moonraker Spoolman integration detected. This may cause double-accounting "
        "if Moonraker and spoolman-cfs-sync both deduct filament usage."
    )
    try:
        info = _http_get_json(base.rstrip("/") + "/server/info", timeout=2.5)
        result = (info or {}).get("result") or {}
        comps = result.get("components")
        if isinstance(comps, list) and any(str(c).lower() == "spoolman" for c in comps):
            return True, warning
    except Exception:
        pass

    try:
        cfg = _http_get_json(base.rstrip("/") + "/server/config", timeout=2.5)
        raw = json.dumps(cfg or {}).lower()
        if '"spoolman"' in raw or "[spoolman]" in raw:
            return True, warning
    except Exception:
        pass

    return False, ""


def _walk(obj, path=""):
    # generator over (path, value) for nested dict/list
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, v
            yield from _walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield p, v
            yield from _walk(v, p)


_SLOT_RE = __import__("re").compile(r"^[1-4][A-D]$")


def _extract_cfs_slot_data(status: dict) -> tuple[Optional[str], dict]:
    """Best-effort extraction of CFS slot metadata from Moonraker status.

    Creality's firmware is not standardized, so we try heuristics:
    - Any dict key that looks like '1A', '2D', ... is treated as a slot.
    - Any nested dict with fields like slot/id/index and color/material/name.
    Returns (active_slot, slots_dict).
    """
    active = None
    slots: dict[str, dict] = {}

    # --- Creality K-series "box" + "filament_rack" objects (K2 Plus / CFS) ---
    # Firmware exposes:
    #   box.T1..T4 with arrays: color_value/material_type/remain_len, and box.<Tn>.filament = "A".."D"
    #   filament_rack.remain_material_color/type
    # We normalize to internal slot ids: "1A".."4D".
    try:
        box = (status or {}).get("box")
        rack = (status or {}).get("filament_rack")
        if isinstance(box, dict):
            # Build lookups from box.same_material: [material_code, color_code, ["T2D"], "ABS"]
            mat_name_by_code: dict[str, str] = {}
            sm = box.get("same_material")
            if isinstance(sm, list):
                for row in sm:
                    if not isinstance(row, list) or len(row) < 4:
                        continue
                    mcode, _ccode, _slots_list, mname = row[0], row[1], row[2], row[3]
                    if isinstance(mcode, str) and isinstance(mname, str):
                        mat_name_by_code[mcode] = mname.strip().upper()

            def _hex_color(creality_val: str) -> Optional[str]:
                if not isinstance(creality_val, str):
                    return None
                v = creality_val.strip().lower()
                # values look like "0ffa800" or "00a2989"; take last 6 hex chars
                hex6 = v[-6:]
                if len(hex6) == 6 and all(ch in "0123456789abcdef" for ch in hex6):
                    return f"#{hex6}".lower()
                return None

            boxes: dict[str, dict] = {}

            for ti in ("T1", "T2", "T3", "T4"):
                t = box.get(ti)
                if not isinstance(t, dict):
                    continue

                # Box connection state: "connect" when a CFS is present.
                bnum = str(ti[1])
                bstate = str(t.get("state") or "")
                is_conn = (bstate.lower() == "connect")
                boxes[bnum] = {
                    "connected": is_conn,
                    "state": bstate,
                    # Best-effort environmental info per CFS box (Creality)
                    "temperature_c": None,
                    "humidity_pct": None,
                }

                # Temperature / humidity are often strings like "32" and "31"
                try:
                    tval = t.get("temperature")
                    hval = t.get("dry_and_humidity")
                    if tval is not None and str(tval).strip().lower() != "none":
                        boxes[bnum]["temperature_c"] = float(str(tval).strip())
                    if hval is not None and str(hval).strip().lower() != "none":
                        boxes[bnum]["humidity_pct"] = float(str(hval).strip())
                except Exception:
                    pass

                # If the box isn't connected, mark its slots as not present and continue.
                if not is_conn:
                    for letter in ("A", "B", "C", "D"):
                        sid = f"{bnum}{letter}"
                        slots[sid] = {"present": False}
                    continue
                colors = t.get("color_value")
                mats = t.get("material_type")
                if not (isinstance(colors, list) and isinstance(mats, list)):
                    continue

                for idx, letter in enumerate(("A", "B", "C", "D")):
                    sid = f"{ti[1]}{letter}"  # "1A".."4D"
                    raw_color = colors[idx] if idx < len(colors) else None
                    raw_mat = mats[idx] if idx < len(mats) else None
                    out: dict = {"present": True}

                    # Creality uses "-1" to signal an empty slot
                    if isinstance(raw_mat, str) and raw_mat.strip() == "-1":
                        slots[sid] = {"present": False, "material": "", "color": ""}
                        continue

                    col = _hex_color(str(raw_color)) if raw_color is not None else None
                    if col:
                        out["color"] = col
                    if isinstance(raw_mat, str):
                        out["material"] = mat_name_by_code.get(raw_mat, raw_mat).strip().upper()

                    slots[sid] = out

                fil = t.get("filament")
                if isinstance(fil, str) and fil in ("A", "B", "C", "D"):
                    active = f"{ti[1]}{fil}"

            if active is None and isinstance(rack, dict):
                rc = rack.get("remain_material_color")
                rt = rack.get("remain_material_type")
                rc_hex = _hex_color(str(rc)) if rc is not None else None
                rt_norm = mat_name_by_code.get(rt, rt).strip().upper() if isinstance(rt, str) else None
                if rc_hex and rt_norm:
                    for sid, meta in slots.items():
                        if meta.get("color") == rc_hex and meta.get("material") == rt_norm:
                            active = sid
                            break

            if slots:
                mp = box.get("map")
                if isinstance(mp, dict):
                    slots["_map"] = {"raw": mp}
                # Add box connection metadata for the frontend
                if boxes:
                    slots["_boxes"] = boxes
                return active, slots
    except Exception:
        pass

    # 1) Direct keys
    for k, v in (status or {}).items():
        if isinstance(k, str) and _SLOT_RE.match(k) and isinstance(v, dict):
            slots[k] = v

    # 2) Walk nested structures to find slot-like dicts
    for p, v in _walk(status or {}):
        if not isinstance(v, dict):
            continue
        # Active slot hints
        for ak in ("active_slot", "current_slot", "slot", "cfs_slot", "ams_slot"):
            if ak in v and isinstance(v[ak], str) and _SLOT_RE.match(v[ak]):
                active = v[ak]
        # Slot dictionaries keyed by slot id
        if any(key in p.lower() for key in ("cfs", "ams", "mmu", "filament", "spool")):
            for kk, vv in v.items():
                if isinstance(kk, str) and _SLOT_RE.match(kk) and isinstance(vv, dict):
                    slots.setdefault(kk, vv)

    # Normalize fields we care about
    norm: dict[str, dict] = {}
    for sid, raw in slots.items():
        if not isinstance(raw, dict):
            continue
        out = {}
        # presence / loaded flags
        for pk in ("present", "loaded", "has_filament", "is_loaded", "enabled"):
            if pk in raw and isinstance(raw[pk], (bool, int)):
                out["present"] = bool(raw[pk])
                break
        # material
        for mk in ("material", "type", "filament_type"):
            if mk in raw and isinstance(raw[mk], str):
                out["material"] = raw[mk].strip().upper()
                break
        # color
        for ck in ("color", "color_hex", "colour", "rgb"):
            if ck in raw:
                out["color"] = raw[ck]
                break
        # name/vendor
        for nk in ("name", "label", "spool_name"):
            if nk in raw and isinstance(raw[nk], str):
                out["name"] = raw[nk]
                break
        for vk in ("vendor", "manufacturer", "brand"):
            if vk in raw and isinstance(raw[vk], str):
                out["manufacturer"] = raw[vk]
                break

        norm[sid] = out or {"raw": raw}

    return active, norm




async def moonraker_poll_loop() -> None:
    cfg = load_config()
    base = (cfg.get("moonraker_url") or "").strip()
    if not base:
        return

    interval = float(cfg.get("poll_interval_sec", 5) or 5)
    if interval < 1:
        interval = 1

    # Always query job usage
    base_objects = ["print_stats", "virtual_sdcard"]

    # Best-effort: discover CFS-related objects once, then include them in polling.
    cfs_objects: list[str] = []
    try:
        cfs_objects = await asyncio.to_thread(_moonraker_cfs_objects, base)
    except Exception:
        cfs_objects = []

    poll_objects = base_objects + cfs_objects
    url = _moonraker_build_url(base, poll_objects)
    last_cfs_discovery = _now()

    native_spoolman_detected, native_spoolman_warning = await asyncio.to_thread(_moonraker_detect_native_spoolman, base)

    # Optional: if enabled, we import material/color/name from CFS objects into our local slots.
    cfs_autosync = bool(cfg.get("cfs_autosync", False))

    # Pull Moonraker's global history occasionally (read-only).
    last_hist_fetch = 0.0
    hist_every_sec = 60.0

    while True:
        try:
            if not cfs_objects and (_now() - last_cfs_discovery) >= 30.0:
                last_cfs_discovery = _now()
                try:
                    cfs_objects = await asyncio.to_thread(_moonraker_cfs_objects, base)
                    poll_objects = base_objects + cfs_objects
                    url = _moonraker_build_url(base, poll_objects)
                except Exception:
                    cfs_objects = []

            data = await asyncio.to_thread(_http_get_json, url)
            status = (((data or {}).get("result") or {}).get("status") or {})
            ps = status.get("print_stats") or {}
            vsd = status.get("virtual_sdcard") or {}

            ps_state = str(ps.get("state") or "").lower()

            filename = ps.get("filename") or vsd.get("file_path") or ""
            if isinstance(filename, str) and "/" in filename:
                filename = filename.rsplit("/", 1)[-1]
            job_id = _moonraker_current_job_id(ps, vsd)
            used = ps.get("filament_used")
            if used is None:
                used_mm = 0
            else:
                used_mm = int(float(used))
            try:
                file_position = int(float(vsd.get("file_position") or 0))
            except Exception:
                file_position = 0
            try:
                file_size = int(float(vsd.get("file_size") or 0))
            except Exception:
                file_size = 0

            used_g = 0.0
            try:
                meta = ((vsd.get("cur_print_data") or {}).get("metadata") or {})
                lst = meta.get("filament_used_g")
                if isinstance(lst, list) and lst:
                    used_g = float(sum(float(x) for x in lst if x is not None))
            except Exception:
                used_g = 0.0

            st = load_state()
            st.printer_connected = True
            st.printer_last_error = ""
            spoolman_cfg_now = _normalize_spoolman_config(load_config())
            _set_spoolman_status(
                st,
                dry_run=bool(spoolman_cfg_now.get("dry_run", True)),
                sync_mode=str(spoolman_cfg_now.get("sync_mode") or "post_print"),
                moonraker_native_detected=bool(native_spoolman_detected),
                moonraker_native_warning=native_spoolman_warning,
            )

            # --- CFS read-only extraction (best effort) ---
            cfs_status = {k: v for k, v in (status or {}).items() if k not in ("print_stats", "virtual_sdcard")}
            if cfs_status:
                active_slot, slots_meta = _extract_cfs_slot_data(cfs_status)
                st.cfs_connected = True
                st.cfs_last_update = _now()
                st.cfs_active_slot = active_slot
                st.cfs_slots = slots_meta
                # store a small raw snapshot for debugging in the UI
                st.cfs_raw = {k: cfs_status[k] for k in list(cfs_status)[:4]}

                # If the printer reports an active slot, we can reflect it locally (no POST to printer)
                if active_slot and active_slot in st.slots:
                    st.active_slot = active_slot

                # Optional: import metadata into local slots (still read-only to printer)
                if cfs_autosync and slots_meta:
                    for sid, meta in slots_meta.items():
                        if sid not in st.slots:
                            continue
                        s = st.slots[sid]
                        mat = meta.get("material")
                        if isinstance(mat, str) and mat.strip():
                            # unknown material will be normalized to OTHER by schema
                            s.material = mat.strip().upper()  # type: ignore
                        col = meta.get("color")
                        if isinstance(col, str) and col.startswith("#") and len(col) == 7:
                            s.color_hex = col.lower()
                        name = meta.get("name")
                        if isinstance(name, str):
                            s.name = name
                        mfg = meta.get("manufacturer")
                        if isinstance(mfg, str):
                            s.manufacturer = mfg
                        st.slots[sid] = s
            else:
                _clear_cfs_connection(st)

            # --- Per-slot history tracking (read-only) ---
            # Attribute delta filament_used(mm) to the currently active slot during a print.
            # This enables per-slot history (and later accurate remaining_g calculations) even
            # for multi-color prints.
            try:
                is_printing = ps_state in ("printing", "paused")
                tracking = bool(st.job_track_name)
                curr_slot = (st.cfs_active_slot or st.active_slot or "").strip()

                # Start tracking when a print begins
                if is_printing and filename:
                    if (not tracking) or (st.job_track_name != filename):
                        st.job_track_name = filename
                        st.job_track_id = job_id
                        st.job_track_started_at = _now()
                        st.job_track_last_mm = 0
                        st.job_track_slot_mm = {}
                        st.job_track_slot_g = {}
                        st.job_track_last_state = ps_state
                        st.job_track_file_path = ""
                        st.job_track_last_file_position = 0
                        st.job_track_file_size = 0
                        st.job_track_extruder_mode = "relative"
                        st.job_track_last_e = 0.0
                        st.job_track_parser_slot = curr_slot
                        st.job_track_parser_tail = ""
                        st.job_track_spoolman_live_synced_mm = {}
                        st.job_track_spoolman_live_last_attempt_mm = {}
                        st.job_track_spoolman_live_seq = {}
                        st.job_track_spoolman_live_blocked = {}
                    elif job_id and not str(getattr(st, "job_track_id", "") or "").strip():
                        st.job_track_id = job_id

                    if file_size > 0:
                        st.job_track_file_size = max(int(st.job_track_file_size or 0), file_size)

                    # Attribute executed G-code extrusion to slots. Creality's
                    # print_stats.filament_used follows the live E position and
                    # can move backwards during retractions/tool changes, so it
                    # is not safe as an accumulated counter.
                    if file_position > 0 and curr_slot:
                        try:
                            _track_usage_from_gcode_file(
                                st,
                                base_url=base,
                                filename=filename,
                                virtual_sdcard=vsd,
                                file_position=file_position,
                                fallback_slot=curr_slot,
                            )
                        except Exception as e:
                            _set_spoolman_status(st, last_error=f"G-code usage parser failed: {e}")
                    st.job_track_last_mm = _job_track_total_mm(st)
                    st.job_track_last_state = ps_state

                    try:
                        _plan_spoolman_live_sync_for_current_job(st)
                    except Exception as e:
                        _set_spoolman_status(st, last_error=f"Live Spoolman sync failed: {e}")

                    # Publish a single "live" history entry per slot for the current job.
                    # This makes the right-hand "History by Slot" useful during
                    # multi-color prints (usage is attributed while printing, not only at the end).
                    try:
                        now_ts = _now()
                        slot_mm_live = st.job_track_slot_mm if isinstance(st.job_track_slot_mm, dict) else {}
                        for sid, mm_live in slot_mm_live.items():
                            try:
                                mm_i = int(round(float(mm_live or 0)))
                                if mm_i <= 0:
                                    continue
                                mat = st.slots.get(sid).material if sid in st.slots else "OTHER"
                                g_live = float(round(mm_to_g(str(mat), float(mm_i)), 2))
                                src = f"live:{st.job_track_started_at}:{st.job_track_name}:{sid}"
                                _hist_upsert_by_src(
                                    st,
                                    sid,
                                    src,
                                    {
                                        "ts": float(now_ts),
                                        "job": st.job_track_name,
                                        "used_mm": mm_i,
                                        "used_g": g_live,
                                        "result": "printing",
                                    },
                                )
                            except Exception:
                                continue
                    except Exception:
                        pass

                # Finalize when printing ends (complete/cancel/error/standby)
                if (not is_printing) and tracking and st.job_track_name:
                    try:
                        _finish_gcode_usage_tracking(
                            st,
                            base_url=base,
                            fallback_slot=curr_slot,
                            completed=ps_state in ("complete", "completed"),
                        )
                    except Exception as e:
                        _set_spoolman_status(st, last_error=f"G-code usage finalization failed: {e}")

                    # Determine an end timestamp from Creality virtual_sdcard if available
                    end_ts = _now()
                    try:
                        cpd = (vsd.get("cur_print_data") or {})
                        et = cpd.get("end_time")
                        if et is not None:
                            end_ts = float(et)
                    except Exception:
                        pass

                    # Create history entries per slot (only if we have consumption)
                    slot_mm = st.job_track_slot_mm if isinstance(st.job_track_slot_mm, dict) else {}
                    for sid, mm in slot_mm.items():
                        try:
                            mm_i = int(round(float(mm)))
                            if mm_i <= 0:
                                continue
                            mat = st.slots.get(sid).material if sid in st.slots else "OTHER"
                            g = float(round(mm_to_g(str(mat), float(mm_i)), 2))

                            # Remove any live entry for this job/slot (so we don't show duplicates)
                            try:
                                live_src = f"live:{st.job_track_started_at}:{st.job_track_name}:{sid}"
                                h0 = st.slot_history.get(sid)
                                if isinstance(h0, list):
                                    st.slot_history[sid] = [e for e in h0 if not (isinstance(e, dict) and e.get("_src") == live_src)]
                            except Exception:
                                pass

                            _hist_push(
                                st,
                                sid,
                                {
                                    "ts": float(end_ts),
                                    "job": st.job_track_name,
                                    "used_mm": mm_i,
                                    "used_g": g,
                                    "result": ps_state,
                                },
                            )
                        except Exception:
                            continue

                    try:
                        _plan_spoolman_sync_for_finished_job(
                            st,
                            st.job_track_name,
                            float(st.job_track_started_at or 0.0),
                            float(end_ts),
                            ps_state,
                            str(getattr(st, "job_track_id", "") or ""),
                            float(used_mm or 0.0),
                        )
                    except Exception as e:
                        _set_spoolman_status(st, last_error=f"Spoolman sync planning failed: {e}")

                    # Reset tracking
                    st.job_track_name = ""
                    st.job_track_id = ""
                    st.job_track_started_at = 0.0
                    st.job_track_last_mm = 0
                    st.job_track_slot_mm = {}
                    st.job_track_slot_g = {}
                    st.job_track_last_state = ps_state
                    st.job_track_file_path = ""
                    st.job_track_last_file_position = 0
                    st.job_track_file_size = 0
                    st.job_track_extruder_mode = "relative"
                    st.job_track_last_e = 0.0
                    st.job_track_parser_slot = ""
                    st.job_track_parser_tail = ""
                    st.job_track_spoolman_live_synced_mm = {}
                    st.job_track_spoolman_live_last_attempt_mm = {}
                    st.job_track_spoolman_live_seq = {}
                    st.job_track_spoolman_live_blocked = {}
            except Exception:
                pass

            # --- Job usage accounting ---
            if filename or used_mm:
                display_used_mm = _job_track_total_mm(st) if st.job_track_name else used_mm
                _apply_job_usage(st, filename or st.current_job or "", display_used_mm)
                if used_g > 0.0:
                    st.current_job_filament_g = float(round(used_g, 2))

            # --- Moonraker history snapshot (global) ---
            # This is useful to show past jobs even if our per-slot tracker
            # wasn't running.  It won't reliably attribute usage to CFS slots,
            # so the UI shows it separately.
            try:
                now = _now()
                if (now - last_hist_fetch) >= hist_every_sec:
                    hist = await asyncio.to_thread(_moonraker_fetch_history, base, 20)
                    if hist:
                        st.moonraker_history = hist
                    last_hist_fetch = now
            except Exception:
                pass

            save_state(st)
        except Exception as e:
            st = load_state()
            st.printer_connected = False
            st.printer_last_error = str(e)
            _clear_cfs_connection(st)
            st.updated_at = time.time()
            save_state(st)

        await asyncio.sleep(interval)


async def _restart_moonraker_poll_task() -> None:
    global MOONRAKER_POLL_TASK
    if MOONRAKER_POLL_TASK and not MOONRAKER_POLL_TASK.done():
        MOONRAKER_POLL_TASK.cancel()
        try:
            await MOONRAKER_POLL_TASK
        except asyncio.CancelledError:
            pass
    MOONRAKER_POLL_TASK = None
    cfg = load_config()
    if (cfg.get("moonraker_url") or "").strip():
        MOONRAKER_POLL_TASK = asyncio.create_task(moonraker_poll_loop())



app = FastAPI(title="3D Printer Filament Manager", version="0.1.1")


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    """Disable browser caching for /static assets.

    This project is frequently updated in-place on the host. Some browsers keep
    serving an older /static/app.js via 304 responses unless caching is
    explicitly disabled. Prevent that.
    """
    response = await call_next(request)
    path = request.url.path or ""
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Static UI on /
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def _startup():
    _ensure_data_files()
    await _restart_moonraker_poll_task()


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# --- Public API ---
@app.get("/api/state", response_model=AppState)
def api_state():
    return load_state()


@app.post("/api/moonraker/allocate", response_model=AppState)
def api_moonraker_allocate(req: MoonrakerAllocateRequest):
    """Store local per-slot allocation for a Moonraker history job.

    This never talks to the printer. It only enriches our local per-slot history.
    """
    st = load_state()
    key = (req.job_key or "").strip() or _job_key(req.job_key, req.ts, req.job)

    # Normalize alloc_g: drop zeros/negatives
    alloc: Dict[str, float] = {}
    for sid, g in (req.alloc_g or {}).items():
        try:
            gv = float(g)
            if gv > 0:
                alloc[str(sid)] = float(round(gv, 2))
        except Exception:
            continue

    if not alloc:
        raise HTTPException(status_code=400, detail="alloc_g must contain at least one positive value")

    # Persist allocation
    st.moonraker_allocations[key] = {"job": req.job, "ts": float(req.ts), "alloc_g": alloc}

    # Push entries into per-slot history (and replace previous pushes for this key)
    # We keep a marker so we can de-duplicate.
    marker = f"moonraker:{key}"
    for sid in alloc.keys():
        h = st.slot_history.get(sid)
        if isinstance(h, list):
            # Remove previous entries for this marker and adjust epoch totals accordingly.
            new_h = []
            removed_g = 0.0
            for e in h:
                if isinstance(e, dict) and e.get("_src") == marker:
                    try:
                        removed_g += float(e.get("used_g") or 0.0)
                    except Exception:
                        pass
                    continue
                new_h.append(e)
            st.slot_history[sid] = new_h
            if removed_g > 0:
                try:
                    s = st.slots.get(sid)
                    if s:
                        # Only subtract from current epoch total if the marker entries
                        # were added in the current epoch.
                        # (Older epochs should not affect current totals.)
                        # We approximate by checking the current slot epoch matches the
                        # epoch on the first removed entry if available.
                        s.spool_epoch_consumed_g_total = max(0.0, float(getattr(s, "spool_epoch_consumed_g_total", 0.0) or 0.0) - float(removed_g))
                        st.slots[sid] = s
                except Exception:
                    pass

    for sid, g in alloc.items():
        _hist_push(
            st,
            sid,
            {
                "ts": float(req.ts),
                "job": req.job,
                "used_mm": 0,
                "used_g": float(round(float(g), 2)),
                "result": "history",
                "_src": marker,
            },
        )
        _inc_slot_epoch_consumed(st, sid, float(g))

    save_state(st)
    return st


def _ui_state_dict(state: AppState) -> dict:
    """Convert internal AppState to the UI payload the static frontend expects."""
    d = _model_dump(state)
    slots_in = d.get("slots", {}) or {}
    slots_out: Dict[str, dict] = {}
    for slot_id, sd in slots_in.items():
        if not isinstance(sd, dict):
            sd = _model_dump(sd)
        out = dict(sd)
        if "color_hex" in out and "color" not in out:
            out["color"] = out.pop("color_hex")
        if "manufacturer" in out and "vendor" not in out:
            out["vendor"] = out.get("manufacturer", "")

        # Derived spool metrics (purely local)
        # - spool_consumed_g: running total for current epoch (stable even if UI history is trimmed)
        # - spool_used_g: consumption since the last "Apply" reference
        # - spool_remaining_g: computed remaining weight
        try:
            consumed = float(out.get("spool_epoch_consumed_g_total") or 0.0)
            out["spool_consumed_g"] = round(consumed, 2)

            ref_rem = out.get("spool_ref_remaining_g")
            ref_cons = out.get("spool_ref_consumed_g")
            if ref_rem is not None and ref_cons is not None:
                # Remaining decreases only by consumption since reference point
                since = max(0.0, consumed - float(ref_cons))
                remaining = max(0.0, float(ref_rem) - since)
                out["spool_remaining_g"] = round(remaining, 1)
                out["spool_used_g"] = round(since, 1)
        except Exception:
            pass

        slots_out[slot_id] = out
    d["slots"] = slots_out

    # UI expects job info as flat fields
    d.setdefault("current_job", "")
    d.setdefault("current_job_filament_mm", 0)
    d.setdefault("current_job_filament_g", 0.0)

    # printer connection info for header badge
    d.setdefault("printer_connected", False)
    d.setdefault("printer_last_error", "")

    d.setdefault("cfs_connected", False)
    d.setdefault("cfs_last_update", 0.0)
    d.setdefault("cfs_active_slot", None)
    d.setdefault("cfs_slots", {})
    d.setdefault("cfs_raw", {})
    d.setdefault("spoolman_status", _default_spoolman_status())
    d.setdefault("spoolman_sync_records", {})
    d["printer_config"] = _printer_public_config()
    d["spoolman_config"] = _spoolman_public_config()

    return d


def _slot_consumed_g_epoch(state: AppState, slot: str) -> float:
    try:
        s = state.slots.get(slot)
        return float(getattr(s, "spool_epoch_consumed_g_total", 0.0) or 0.0)
    except Exception:
        return 0.0


def _clear_local_accounting(state: AppState) -> AppState:
    if str(getattr(state, "job_track_name", "") or "").strip():
        raise HTTPException(status_code=409, detail="Cannot clear local accounting while a print is being tracked.")

    state.slot_history = {}
    state.moonraker_allocations = {}
    state.spoolman_sync_records = {}

    state.current_job = ""
    state.current_job_filament_mm = 0
    state.current_job_filament_g = 0.0
    state.last_accounted_job_mm = 0
    state.last_accounted_slot = None

    state.job_track_name = ""
    state.job_track_id = ""
    state.job_track_started_at = 0.0
    state.job_track_last_mm = 0
    state.job_track_slot_mm = {}
    state.job_track_slot_g = {}
    state.job_track_last_state = ""
    state.job_track_file_path = ""
    state.job_track_last_file_position = 0
    state.job_track_file_size = 0
    state.job_track_extruder_mode = "relative"
    state.job_track_last_e = 0.0
    state.job_track_parser_slot = ""
    state.job_track_parser_tail = ""
    state.job_track_spoolman_live_synced_mm = {}
    state.job_track_spoolman_live_last_attempt_mm = {}
    state.job_track_spoolman_live_seq = {}
    state.job_track_spoolman_live_blocked = {}

    for sid, slot in list((state.slots or {}).items()):
        try:
            slot.spool_epoch_consumed_g_total = 0.0
            if slot.spool_ref_consumed_g is not None:
                slot.spool_ref_consumed_g = 0.0
            state.slots[sid] = slot
        except Exception:
            continue

    save_state(state)
    return state


# --- UI API (static frontend uses /api/ui/* and expects {"result": ...}) ---
@app.get("/api/ui/state", response_model=ApiResponse)
def api_ui_state() -> ApiResponse:
    return ApiResponse(result=_ui_state_dict(load_state()))


@app.post("/api/ui/printer/config", response_model=ApiResponse)
async def api_ui_printer_config(req: UiPrinterConfigRequest) -> ApiResponse:
    update = _req_dump(req, exclude_unset=True)
    _update_printer_config(update)
    await _restart_moonraker_poll_task()
    return ApiResponse(result=_ui_state_dict(load_state()))


@app.get("/api/ui/spoolman/status", response_model=ApiResponse)
def api_ui_spoolman_status() -> ApiResponse:
    st = load_state()
    payload = {
        "config": _spoolman_public_config(),
        "status": st.spoolman_status,
        "records": st.spoolman_sync_records,
    }
    return ApiResponse(result=payload)


@app.post("/api/ui/spoolman/config", response_model=ApiResponse)
def api_ui_spoolman_config(req: UiSpoolmanConfigRequest) -> ApiResponse:
    update = _req_dump(req, exclude_unset=True)
    cfg = _update_spoolman_config(update)
    st = load_state()
    _set_spoolman_status(
        st,
        dry_run=bool(cfg.get("dry_run", True)),
        sync_mode=str(cfg.get("sync_mode") or "post_print"),
    )
    save_state(st)
    return ApiResponse(result=_ui_state_dict(st))


@app.post("/api/ui/spoolman/mapping", response_model=ApiResponse)
def api_ui_spoolman_mapping(req: UiSpoolmanMappingRequest) -> ApiResponse:
    _update_spoolman_mapping(req.slot, req.spool_id)
    return ApiResponse(result=_ui_state_dict(load_state()))


@app.post("/api/ui/spoolman/test", response_model=ApiResponse)
def api_ui_spoolman_test() -> ApiResponse:
    st = load_state()
    cfg = _normalize_spoolman_config(load_config())
    try:
        _spoolman_health(cfg)
        _set_spoolman_status(
            st,
            connected=True,
            last_check_at=_now(),
            last_error="",
            dry_run=bool(cfg.get("dry_run", True)),
            sync_mode=str(cfg.get("sync_mode") or "post_print"),
        )
    except Exception as e:
        _set_spoolman_status(
            st,
            connected=False,
            last_check_at=_now(),
            last_error=str(e),
            dry_run=bool(cfg.get("dry_run", True)),
            sync_mode=str(cfg.get("sync_mode") or "post_print"),
        )
    save_state(st)
    return ApiResponse(result=_ui_state_dict(st))


@app.get("/api/ui/spoolman/spools", response_model=ApiResponse)
def api_ui_spoolman_spools() -> ApiResponse:
    st = load_state()
    cfg = _normalize_spoolman_config(load_config())
    try:
        spools = _spoolman_list_spools(cfg)
        _set_spoolman_status(
            st,
            connected=True,
            last_check_at=_now(),
            last_error="",
            dry_run=bool(cfg.get("dry_run", True)),
            sync_mode=str(cfg.get("sync_mode") or "post_print"),
        )
        save_state(st)
        return ApiResponse(result={"spools": spools, "status": st.spoolman_status})
    except Exception as e:
        _set_spoolman_status(
            st,
            connected=False,
            last_check_at=_now(),
            last_error=str(e),
            dry_run=bool(cfg.get("dry_run", True)),
            sync_mode=str(cfg.get("sync_mode") or "post_print"),
        )
        save_state(st)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/ui/spoolman/retry", response_model=ApiResponse)
def api_ui_spoolman_retry(req: UiSpoolmanRetryRequest) -> ApiResponse:
    st = load_state()
    key = (req.record_key or "").strip()
    rec = (st.spoolman_sync_records or {}).get(key)
    if not isinstance(rec, dict):
        raise HTTPException(status_code=404, detail="Unknown Spoolman sync record")
    status = str(rec.get("status") or "")
    if status == "timeout_uncertain":
        raise HTTPException(
            status_code=409,
            detail="Timeout-uncertain records are not automatically retryable. Verify Spoolman inventory first.",
        )
    if str(rec.get("sync_phase") or "") == "live":
        raise HTTPException(
            status_code=409,
            detail="Live sync records are reconciled by later live chunks or the final sync record.",
        )
    if status not in ("failed", "pending", "skipped_invalid_spool", "skipped_unmapped", "dry_run"):
        raise HTTPException(status_code=409, detail=f"Record with status {status!r} cannot be retried")
    cfg = _normalize_spoolman_config(load_config())
    retry_record = dict(rec)
    sid = str(retry_record.get("slot") or "").strip().upper()
    mappings = cfg.get("slot_mappings") if isinstance(cfg.get("slot_mappings"), dict) else {}
    if sid in mappings:
        retry_record["spool_id"] = mappings.get(sid)
    _spoolman_sync_record(st, key, retry_record, cfg)
    return ApiResponse(result=_ui_state_dict(load_state()))


@app.post("/api/ui/accounting/clear", response_model=ApiResponse)
def api_ui_accounting_clear() -> ApiResponse:
    st = _clear_local_accounting(load_state())
    return ApiResponse(result=_ui_state_dict(st))


@app.post("/api/ui/moonraker/allocate", response_model=ApiResponse)
def api_ui_moonraker_allocate(req: MoonrakerAllocateRequest) -> ApiResponse:
    st = api_moonraker_allocate(req)
    return ApiResponse(result=_ui_state_dict(st))


@app.post("/api/select_slot", response_model=AppState)
def api_select_slot(req: SelectSlotRequest):
    state = load_state()
    if req.slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")
    state.active_slot = req.slot
    save_state(state)
    return state


@app.post("/api/ui/select_slot", response_model=ApiResponse)
def api_ui_select_slot(req: SelectSlotRequest) -> ApiResponse:
    state = api_select_slot(req)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/set_auto", response_model=AppState)
def api_set_auto(req: SetAutoRequest):
    state = load_state()
    state.auto_mode = bool(req.enabled)
    save_state(state)
    return state


@app.post("/api/ui/set_auto", response_model=ApiResponse)
def api_ui_set_auto(req: SetAutoRequest) -> ApiResponse:
    state = api_set_auto(req)
    return ApiResponse(result=_ui_state_dict(state))


@app.patch("/api/slots/{slot}", response_model=AppState)
def api_update_slot(slot: str, req: UpdateSlotRequest):
    state = load_state()
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    s = state.slots[slot]
    update = _req_dump(req, exclude_unset=True)
    for k, v in update.items():
        setattr(s, k, v)

    state.slots[slot] = s
    save_state(state)
    return state


@app.post("/api/ui/slot/update", response_model=ApiResponse)
def api_ui_slot_update(req: UiSlotUpdateRequest) -> ApiResponse:
    state = load_state()
    slot = req.slot
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    s = state.slots[slot]
    upd = _req_dump(req, exclude_unset=True)

    # UI uses 'color' but internal uses 'color_hex'
    if "color" in upd:
        s.color_hex = upd.pop("color")

    upd.pop("slot", None)

    # vendor -> manufacturer
    if "vendor" in upd and upd.get("vendor") is not None:
        upd["manufacturer"] = upd.pop("vendor")

    for k, v in upd.items():
        if v is None:
            continue
        if hasattr(s, k):
            setattr(s, k, v)

    state.slots[slot] = s
    save_state(state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/slot/reset", response_model=ApiResponse)
def api_ui_slot_reset(req: UiSlotResetRequest) -> ApiResponse:
    state = load_state()
    slot = req.slot
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")
    state.slots[slot].remaining_g = float(req.remaining_g)
    save_state(state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/spool/set_start", response_model=ApiResponse)
def api_ui_spool_set_start(req: UiSpoolSetStartRequest) -> ApiResponse:
    """Roll change: set new spool baseline (local only).

    Historical entries are kept, but hidden by incrementing the slot's spool_epoch.
    The new spool's remaining weight is set as the reference point.
    """
    state = load_state()
    slot = req.slot
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    start_g = float(req.start_g)
    s = state.slots[slot]
    # New roll => new epoch
    try:
        s.spool_epoch = int(getattr(s, "spool_epoch", 0) or 0) + 1
    except Exception:
        s.spool_epoch = 1

    # Reset accounting for the new epoch
    s.spool_epoch_consumed_g_total = 0.0
    s.spool_ref_remaining_g = start_g
    s.spool_ref_consumed_g = 0.0
    s.spool_ref_set_at = time.time()
    # keep legacy fields for debugging only
    s.spool_start_g = start_g
    s.remaining_g = start_g
    state.slots[slot] = s
    save_state(state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/spool/set_remaining", response_model=ApiResponse)
def api_ui_spool_set_remaining(req: UiSpoolSetRemainingRequest) -> ApiResponse:
    """Apply measured remaining weight as the new reference (local only).

    Does NOT reset epoch and does not delete history. Remaining is computed as:
      remaining = ref_remaining - (consumed_epoch - ref_consumed)
    """
    state = load_state()
    slot = req.slot
    if slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    rem_g = float(req.remaining_g)
    s = state.slots[slot]
    consumed_now = _slot_consumed_g_epoch(state, slot)
    s.spool_ref_remaining_g = rem_g
    s.spool_ref_consumed_g = float(round(consumed_now, 4))
    s.spool_ref_set_at = time.time()
    # legacy
    s.remaining_g = rem_g
    state.slots[slot] = s
    save_state(state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/ui/set_color", response_model=ApiResponse)
def api_ui_set_color(req: UiSetColorRequest) -> ApiResponse:
    state = load_state()
    if req.slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")
    state.slots[req.slot].color_hex = req.color
    save_state(state)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/spool/reset", response_model=AppState)
def api_spool_reset(req: SpoolResetRequest):
    state = load_state()
    if req.slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")
    state.slots[req.slot].remaining_g = float(req.remaining_g)
    save_state(state)
    return state


@app.post("/api/spool/apply_usage", response_model=AppState)
def api_spool_apply_usage(req: SpoolApplyUsageRequest):
    state = load_state()
    if req.slot not in state.slots:
        raise HTTPException(status_code=404, detail="Unknown slot")

    current = state.slots[req.slot].remaining_g
    if current is None:
        raise HTTPException(status_code=409, detail="remaining_g is not set for this slot")

    new_val = max(0.0, float(current) - float(req.used_g))
    state.slots[req.slot].remaining_g = new_val
    save_state(state)
    return state


@app.post("/api/job/set", response_model=AppState)
def api_job_set(req: JobSetRequest):
    state = load_state()
    state.current_job = req.name
    state.current_job_filament_mm = 0
    state.current_job_filament_g = 0.0
    state.last_accounted_job_mm = 0
    state.last_accounted_slot = state.active_slot
    save_state(state)
    return state


@app.post("/api/ui/job/set", response_model=ApiResponse)
def api_ui_job_set(req: JobSetRequest) -> ApiResponse:
    state = api_job_set(req)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/job/update", response_model=AppState)
def api_job_update(req: JobUpdateRequest):
    state = load_state()
    _apply_job_usage(state, state.current_job or "", int(req.used_mm), slot_override=req.slot)
    save_state(state)
    return state


@app.post("/api/ui/job/update", response_model=ApiResponse)
def api_ui_job_update(req: JobUpdateRequest) -> ApiResponse:
    state = api_job_update(req)
    return ApiResponse(result=_ui_state_dict(state))


@app.post("/api/feed")
def api_feed(req: FeedRequest):
    adapter_feed(req.mm)
    return {"ok": True}


@app.post("/api/ui/feed", response_model=ApiResponse)
def api_ui_feed(req: FeedRequest) -> ApiResponse:
    api_feed(req)
    return ApiResponse(result={"ok": True})


@app.post("/api/retract")
def api_retract(req: RetractRequest):
    adapter_retract(req.mm)
    return {"ok": True}


@app.post("/api/ui/retract", response_model=ApiResponse)
def api_ui_retract(req: RetractRequest) -> ApiResponse:
    api_retract(req)
    return ApiResponse(result={"ok": True})


@app.get("/api/ui/help", response_model=ApiResponse)
def api_ui_help() -> ApiResponse:
    text = (
        "Click a slot to open its local spool editor.\n"
        "Color and material data are read from CFS when available.\n"
        "Feed/retract actions are adapter hooks for future hardware integration.\n"
        "Job usage: when Moonraker is configured in data/config.json, job and filament_used values are imported automatically.\n"
        "Alternatively, you can update usage manually through /api/ui/job/update."
    )
    return ApiResponse(result={"text": text})


# Health
@app.get("/api/health")
def api_health():
    return {"ok": True, "ts": _now()}



def default_state() -> AppState:
    """Safe defaults if state.json is missing/broken.

    Must always include all 4x4 CFS slots so the UI never crashes, even if the
    state file is corrupted.
    """
    slots: Dict[str, SlotState] = {}
    for sid in DEFAULT_SLOTS:
        slots[sid] = SlotState(slot=sid, material="OTHER", color_hex="#00aaff", remaining_g=0.0)

    # Sensible demo defaults for a single CFS box.
    slots["1A"].material = "ABS"
    slots["1A"].color_hex = "#4b0082"  # indigo-ish
    slots["1A"].remaining_g = 1000.0

    return AppState(
        active_slot="1A",
        auto_mode=False,
        updated_at=_now(),
        slots=slots,  # type: ignore[arg-type]
        current_job="",
        current_job_filament_mm=0,
        current_job_filament_g=0.0,
        printer_connected=False,
        printer_last_error="",
        cfs_connected=False,
        cfs_last_update=0.0,
        cfs_active_slot=None,
        cfs_slots={},
        cfs_raw={},
        job_track_spoolman_live_synced_mm={},
        job_track_spoolman_live_last_attempt_mm={},
        job_track_spoolman_live_seq={},
        job_track_spoolman_live_blocked={},
        spoolman_status=_default_spoolman_status(),
        spoolman_sync_records={},
    )
