import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import main as appmod


def audit_config(*, enabled=True, dry_run=False, mappings=None, sync_mode="post_print", live_min_delta_mm=100.0):
    cfg = appmod._default_spoolman_config()
    cfg.update(
        {
            "enabled": enabled,
            "dry_run": dry_run,
            "url": "http://spoolman.test:7912",
            "sync_mode": sync_mode,
            "live_min_delta_mm": live_min_delta_mm,
        }
    )
    cfg["slot_mappings"].update(mappings or {"1A": 1})
    return cfg


@pytest.fixture
def audit_env(monkeypatch, tmp_path):
    state = appmod.default_state()
    cfg = audit_config()
    inventory = {
        1: {"id": 1, "used_length": 1000.0, "remaining_length": 9000.0, "used_weight": 3.0, "remaining_weight": 997.0,
            "filament": {"id": 11, "name": "PLA Blue", "material": "PLA", "color_hex": "#3366ff"}},
        2: {"id": 2, "used_length": 500.0, "remaining_length": 9500.0, "remaining_weight": 999.0,
            "filament": {"id": 12, "name": "PLA Red", "material": "PLA", "color_hex": "#ff3333"}},
    }
    calls = []

    monkeypatch.setattr(appmod, "AUDITS_PATH", tmp_path / "print_audits.json")
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": cfg})
    monkeypatch.setattr(appmod, "save_state", lambda _state: None)

    def get_spool(spool_id, cfg=None):
        spool_id = int(spool_id)
        if spool_id not in inventory:
            raise appmod.SpoolmanHttpError(404, "not found")
        return json.loads(json.dumps(inventory[spool_id]))

    def use_spool(spool_id, used_mm, cfg=None):
        spool_id = int(spool_id)
        used_mm = float(used_mm)
        calls.append((spool_id, used_mm))
        inventory[spool_id]["used_length"] += used_mm
        inventory[spool_id]["remaining_length"] -= used_mm
        return {"id": spool_id, "used_length": inventory[spool_id]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_get_spool", get_spool)
    monkeypatch.setattr(appmod, "_spoolman_use_spool", use_spool)
    return state, cfg, inventory, calls


def start_audit(state, cfg, *, filename="part.gcode", job_id="job-1", started_at=10.0):
    state.job_track_name = filename
    state.job_track_id = job_id
    state.job_track_started_at = started_at
    return appmod._audit_ensure_active(state, cfg, filename, started_at, job_id)


def completed_audit():
    store = appmod.load_audits()
    assert store["active"] is None
    assert store["completed"]
    return store["completed"][0]


def test_live_chunks_and_final_reconciliation_are_one_verified_audit(audit_env):
    state, cfg, _inventory, calls = audit_env
    cfg.update({"sync_mode": "live", "live_min_delta_mm": 100.0})
    start_audit(state, cfg)

    state.job_track_slot_mm = {"1A": 150.0}
    state.job_track_slot_g = {"1A": 0.45}
    state.job_track_printer_used_mm = 150.0
    appmod._plan_spoolman_live_sync_for_current_job(state)

    state.job_track_slot_mm["1A"] = 250.0
    state.job_track_slot_g["1A"] = 0.75
    state.job_track_printer_used_mm = 250.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 250.0)

    audit = completed_audit()
    assert calls == [(1, 150.0), (1, 100.0)]
    assert audit["verdict"] == "verified"
    assert audit["expected"]["total_mm"] == 250.0
    assert audit["observed"]["total_mm"] == 250.0
    assert {event["phase"] for event in audit["events"]} == {"live", "final"}


@pytest.mark.parametrize("result", ["complete", "cancelled", "failed", "error"])
def test_post_print_terminal_results_are_audited(audit_env, result):
    state, cfg, _inventory, calls = audit_env
    start_audit(state, cfg, job_id=f"job-{result}")
    state.job_track_slot_mm = {"1A": 80.0}
    state.job_track_slot_g = {"1A": 0.24}
    state.job_track_printer_used_mm = 80.0

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, result, f"job-{result}", 80.0)

    audit = completed_audit()
    assert calls == [(1, 80.0)]
    assert audit["result"] == result
    assert audit["verdict"] == "verified"


def test_zero_usage_print_has_no_usage_verdict(audit_env):
    state, cfg, _inventory, calls = audit_env
    start_audit(state, cfg)
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 0.0)

    assert calls == []
    assert completed_audit()["verdict"] == "no_usage"


@pytest.mark.parametrize(
    ("enabled", "dry_run", "verdict"),
    [(True, True, "dry_run"), (False, False, "sync_disabled")],
)
def test_dry_run_and_disabled_writes_have_explicit_verdicts(audit_env, enabled, dry_run, verdict):
    state, cfg, _inventory, calls = audit_env
    cfg.update({"enabled": enabled, "dry_run": dry_run})
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 75.0}
    state.job_track_printer_used_mm = 75.0

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 75.0)

    assert calls == []
    assert completed_audit()["verdict"] == verdict


def test_shared_spool_aggregates_slots_and_moonraker_cap(audit_env):
    state, cfg, _inventory, calls = audit_env
    cfg["slot_mappings"].update({"1A": 1, "1B": 1})
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 100.0, "1B": 300.0}
    state.job_track_slot_g = {"1A": 0.3, "1B": 0.9}
    state.job_track_printer_used_mm = 200.0

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 200.0)

    audit = completed_audit()
    assert calls == [(1, 50.0), (1, 150.0)]
    assert audit["expected"]["slots"] == {"1A": 50.0, "1B": 150.0}
    assert audit["expected"]["spools"] == {"1": 200.0}
    assert audit["verdict"] == "verified"


@pytest.mark.parametrize(("actual_delta", "label"), [(0.0, "missing"), (125.0, "external/excess")])
def test_complete_evidence_flags_missing_extra_and_external_changes(audit_env, monkeypatch, actual_delta, label):
    state, cfg, inventory, _calls = audit_env
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 100.0}
    state.job_track_printer_used_mm = 100.0

    def changed_use(spool_id, used_mm, cfg=None):
        inventory[int(spool_id)]["used_length"] += actual_delta
        return {"id": int(spool_id)}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", changed_use)
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 100.0)

    audit = completed_audit()
    assert audit["verdict"] == "needs_attention"
    assert label.split("/")[0] in audit["reasoning"].lower() or "expected" in audit["reasoning"].lower()


def test_timeout_uncertain_can_verify_from_inventory_evidence(audit_env, monkeypatch):
    state, cfg, inventory, _calls = audit_env
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 100.0}
    state.job_track_printer_used_mm = 100.0

    def uncertain_use(spool_id, used_mm, cfg=None):
        inventory[int(spool_id)]["used_length"] += float(used_mm)
        raise appmod.SpoolmanTimeoutError("timed out after write")

    monkeypatch.setattr(appmod, "_spoolman_use_spool", uncertain_use)
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 100.0)

    audit = completed_audit()
    assert audit["verdict"] == "verified"
    assert any("timeout_uncertain" in warning for warning in audit["warnings"])


def test_unmapped_used_slot_is_inconclusive(audit_env):
    state, cfg, _inventory, _calls = audit_env
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1B": 40.0}
    state.job_track_printer_used_mm = 40.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 40.0)

    audit = completed_audit()
    assert audit["verdict"] == "inconclusive"
    assert "no frozen Spoolman mapping" in " ".join(audit["warnings"])


def test_invalid_spool_during_write_is_inconclusive(audit_env, monkeypatch):
    state, cfg, inventory, _calls = audit_env
    calls = {"count": 0}

    def intermittently_missing(spool_id, cfg=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise appmod.SpoolmanHttpError(404, "deleted")
        return json.loads(json.dumps(inventory[int(spool_id)]))

    monkeypatch.setattr(appmod, "_spoolman_get_spool", intermittently_missing)
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 40.0}
    state.job_track_printer_used_mm = 40.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 40.0)

    assert completed_audit()["verdict"] == "inconclusive"


@pytest.mark.parametrize("mode", ["unavailable", "missing_length"])
def test_unavailable_snapshots_and_missing_length_are_inconclusive(audit_env, monkeypatch, mode):
    state, cfg, inventory, _calls = audit_env

    def incomplete_spool(spool_id, cfg=None):
        if mode == "unavailable":
            raise appmod.SpoolmanHttpError(404, "deleted")
        spool = json.loads(json.dumps(inventory[int(spool_id)]))
        spool.pop("used_length", None)
        return spool

    monkeypatch.setattr(appmod, "_spoolman_get_spool", incomplete_spool)
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 30.0}
    state.job_track_printer_used_mm = 30.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 30.0)

    assert completed_audit()["verdict"] == "inconclusive"


def test_mapping_changes_use_frozen_start_mapping(audit_env):
    state, cfg, _inventory, calls = audit_env
    start_audit(state, cfg)
    cfg["slot_mappings"]["1A"] = 2
    state.job_track_slot_mm = {"1A": 60.0}
    state.job_track_printer_used_mm = 60.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 60.0)

    audit = completed_audit()
    assert calls == [(1, 60.0)]
    assert audit["verdict"] == "verified"
    assert any("Mappings changed" in warning for warning in audit["warnings"])


def test_server_url_is_frozen_for_writes_and_final_snapshots(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    seen_urls = []

    def get_spool(spool_id, cfg=None):
        seen_urls.append(("get", cfg["url"]))
        return json.loads(json.dumps(inventory[int(spool_id)]))

    def use_spool(spool_id, used_mm, cfg=None):
        seen_urls.append(("use", cfg["url"]))
        calls.append((int(spool_id), float(used_mm)))
        inventory[int(spool_id)]["used_length"] += float(used_mm)
        return {"id": int(spool_id)}

    monkeypatch.setattr(appmod, "_spoolman_get_spool", get_spool)
    monkeypatch.setattr(appmod, "_spoolman_use_spool", use_spool)
    original_url = cfg["url"]
    start_audit(state, cfg)
    cfg["url"] = "http://different-spoolman.test:7912"
    state.job_track_slot_mm = {"1A": 40.0}
    state.job_track_printer_used_mm = 40.0

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 40.0)

    assert calls == [(1, 40.0)]
    assert seen_urls
    assert {url for _method, url in seen_urls} == {original_url}
    assert completed_audit()["verdict"] == "verified"


def test_matching_audit_survives_removed_current_server_and_mappings(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    seen_urls = []

    def get_spool(spool_id, cfg=None):
        seen_urls.append(cfg["url"])
        return json.loads(json.dumps(inventory[int(spool_id)]))

    def use_spool(spool_id, used_mm, cfg=None):
        seen_urls.append(cfg["url"])
        calls.append((int(spool_id), float(used_mm)))
        inventory[int(spool_id)]["used_length"] += float(used_mm)
        return {"id": int(spool_id)}

    monkeypatch.setattr(appmod, "_spoolman_get_spool", get_spool)
    monkeypatch.setattr(appmod, "_spoolman_use_spool", use_spool)
    original_url = cfg["url"]
    start_audit(state, cfg)
    cfg["url"] = ""
    cfg["slot_mappings"] = {}
    state.job_track_slot_mm = {"1A": 40.0}
    state.job_track_printer_used_mm = 40.0

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 40.0)

    audit = completed_audit()
    assert calls == [(1, 40.0)]
    assert seen_urls and set(seen_urls) == {original_url}
    assert audit["config"]["slot_mappings"]["1A"] == 1
    assert audit["verdict"] == "verified"


def test_active_audit_without_frozen_server_fails_closed(audit_env):
    state, cfg, _inventory, _calls = audit_env
    start_audit(state, cfg)
    store = appmod.load_audits()
    store["active"]["config"].pop("url")
    appmod.save_audits(store)

    effective, _audit = appmod._audit_cfg_for_print(state, cfg, "part.gcode", 10.0, "job-1")

    assert effective["url"] == ""
    assert effective["enabled"] is False
    assert effective["dry_run"] is True


@pytest.mark.parametrize("control", ["disable", "dry_run"])
def test_current_safety_controls_stop_writes_mid_print(audit_env, control):
    state, cfg, _inventory, calls = audit_env
    start_audit(state, cfg)
    if control == "disable":
        cfg["enabled"] = False
    else:
        cfg["dry_run"] = True
    state.job_track_slot_mm = {"1A": 50.0}
    state.job_track_printer_used_mm = 50.0

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 50.0)

    assert calls == []
    assert completed_audit()["verdict"] == "needs_attention"


@pytest.mark.parametrize(
    ("start_enabled", "start_dry_run", "expected_verdict"),
    [(False, False, "sync_disabled"), (True, True, "dry_run")],
)
def test_current_settings_cannot_arm_a_print_that_started_safe(audit_env, start_enabled, start_dry_run, expected_verdict):
    state, cfg, _inventory, calls = audit_env
    cfg.update({"enabled": start_enabled, "dry_run": start_dry_run})
    start_audit(state, cfg)
    cfg.update({"enabled": True, "dry_run": False})
    state.job_track_slot_mm = {"1A": 50.0}
    state.job_track_printer_used_mm = 50.0

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 50.0)

    assert calls == []
    assert completed_audit()["verdict"] == expected_verdict


@pytest.mark.parametrize("mode", ["no_usage", "dry_run", "sync_disabled"])
def test_unexpected_inventory_changes_override_informational_verdicts(audit_env, mode):
    state, cfg, inventory, calls = audit_env
    if mode == "dry_run":
        cfg.update({"enabled": True, "dry_run": True})
    elif mode == "sync_disabled":
        cfg.update({"enabled": False, "dry_run": False})
    start_audit(state, cfg)
    inventory[1]["used_length"] += 25.0
    if mode == "no_usage":
        state.job_track_slot_mm = {}
        state.job_track_printer_used_mm = 0.0
    else:
        state.job_track_slot_mm = {"1A": 50.0}
        state.job_track_printer_used_mm = 50.0

    appmod._plan_spoolman_sync_for_finished_job(
        state,
        "part.gcode",
        10,
        20,
        "complete",
        "job-1",
        state.job_track_printer_used_mm,
    )

    assert calls == []
    audit = completed_audit()
    assert audit["verdict"] == "needs_attention"
    assert audit["observed"]["spools"]["1"]["difference_mm"] == 25.0


def test_mapped_spool_snapshots_run_concurrently(audit_env, monkeypatch):
    state, cfg, inventory, _calls = audit_env
    cfg["slot_mappings"].update({"1A": 1, "1B": 2, "1C": 3, "1D": 4})
    inventory.update(
        {
            3: {"id": 3, "used_length": 0.0},
            4: {"id": 4, "used_length": 0.0},
        }
    )
    lock = threading.Lock()
    active = 0
    maximum = 0

    def slow_get(spool_id, cfg=None):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return json.loads(json.dumps(inventory[int(spool_id)]))

    monkeypatch.setattr(appmod, "_spoolman_get_spool", slow_get)
    start_audit(state, cfg)

    assert maximum > 1
    assert len(appmod.load_audits()["active"]["snapshots"]["before"]) == 4


def test_safe_retry_uses_frozen_mapping_and_refreshes_verdict(audit_env, monkeypatch):
    state, cfg, _inventory, calls = audit_env
    cfg["dry_run"] = True
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 50.0}
    state.job_track_printer_used_mm = 50.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 50.0)
    audit = completed_audit()
    record_key = next(iter(state.spoolman_sync_records))
    assert audit["verdict"] == "dry_run"

    cfg["dry_run"] = False
    cfg["slot_mappings"]["1A"] = 2
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    appmod.api_ui_spoolman_retry(appmod.UiSpoolmanRetryRequest(record_key=record_key))

    refreshed = completed_audit()
    assert calls == [(1, 50.0)]
    assert refreshed["verdict"] == "verified"
    assert any("safe retry" in warning for warning in refreshed["warnings"])


def test_same_filename_new_job_id_preserves_interrupted_audit(audit_env):
    state, cfg, _inventory, _calls = audit_env
    first = start_audit(state, cfg, filename="same.gcode", job_id="job-1")
    second = appmod._audit_ensure_active(state, cfg, "same.gcode", 11.0, "job-2")
    store = appmod.load_audits()

    assert first["audit_id"] != second["audit_id"]
    assert store["active"]["job_id"] == "job-2"
    assert store["completed"][0]["result"] == "interrupted"
    assert store["completed"][0]["verdict"] == "inconclusive"


def test_unauditable_new_print_still_preserves_old_audit_as_interrupted(audit_env):
    state, cfg, _inventory, _calls = audit_env
    first = start_audit(state, cfg, filename="old.gcode", job_id="job-old")
    cfg["url"] = ""
    cfg["slot_mappings"] = {}

    second = appmod._audit_ensure_active(state, cfg, "new.gcode", 20.0, "job-new")
    store = appmod.load_audits()

    assert second is None
    assert store["active"] is None
    assert store["completed"][0]["audit_id"] == first["audit_id"]
    assert store["completed"][0]["result"] == "interrupted"
    assert store["completed"][0]["verdict"] == "inconclusive"


def test_restart_reuses_active_audit_without_resnapshot(audit_env, monkeypatch):
    state, cfg, inventory, _calls = audit_env
    count = {"value": 0}

    def counted_get(spool_id, cfg=None):
        count["value"] += 1
        return json.loads(json.dumps(inventory[int(spool_id)]))

    monkeypatch.setattr(appmod, "_spoolman_get_spool", counted_get)
    first = start_audit(state, cfg)
    second = appmod._audit_ensure_active(state, cfg, "part.gcode", 10.0, "job-1")

    assert first["audit_id"] == second["audit_id"]
    assert count["value"] == 1


def test_repeated_record_states_replace_the_existing_audit_event(audit_env):
    state, cfg, _inventory, _calls = audit_env
    audit = start_audit(state, cfg)
    record = {
        "audit_id": audit["audit_id"],
        "sync_phase": "post_print",
        "slot": "1A",
        "spool_id": 1,
        "used_mm": 25.0,
        "status": "pending",
        "attempts": 0,
        "updated_at": 11.0,
    }
    appmod._audit_record_event("record-1", record)
    record.update({"status": "synced", "attempts": 1, "updated_at": 12.0})
    appmod._audit_record_event("record-1", record)

    active = appmod.load_audits()["active"]
    assert len(active["events"]) == 1
    assert active["events"][0]["record_key"] == "record-1"
    assert active["events"][0]["status"] == "synced"
    assert active["events"][0]["attempts"] == 1
    assert active["events_dropped"] == 0


def test_audit_event_history_is_capped_and_tracks_dropped_events(audit_env, monkeypatch):
    state, cfg, _inventory, _calls = audit_env
    assert appmod.AUDIT_EVENT_RETENTION == 1000
    monkeypatch.setattr(appmod, "AUDIT_EVENT_RETENTION", 3)
    audit = start_audit(state, cfg)

    for index in range(5):
        appmod._audit_record_event(
            f"record-{index}",
            {
                "audit_id": audit["audit_id"],
                "sync_phase": "live",
                "slot": "1A",
                "spool_id": 1,
                "used_mm": float(index),
                "status": "synced",
                "attempts": 1,
                "updated_at": 11.0 + index,
            },
        )

    active = appmod.load_audits()["active"]
    assert [event["record_key"] for event in active["events"]] == ["record-2", "record-3", "record-4"]
    assert active["events_dropped"] == 2

    appmod._audit_finalize(state, cfg, "part.gcode", 10.0, 20.0, "complete", "job-1", 0.0)
    audit = completed_audit()
    assert audit["events_dropped"] == 2
    assert "Audit event retention omitted 2 oldest events." in audit["warnings"]


def test_retention_keeps_latest_100_completed_plus_active(audit_env):
    _state, _cfg, _inventory, _calls = audit_env
    store = appmod._empty_audit_store()
    store["active"] = {"audit_id": "active"}
    store["completed"] = [{"audit_id": f"audit-{i}"} for i in range(105)]
    appmod.save_audits(store)

    saved = appmod.load_audits()
    assert saved["active"]["audit_id"] == "active"
    assert len(saved["completed"]) == 100
    assert saved["completed"][-1]["audit_id"] == "audit-99"


def test_export_is_valid_sanitized_json(audit_env):
    state, cfg, _inventory, _calls = audit_env
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 10.0}
    state.job_track_printer_used_mm = 10.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 10.0)
    audit = completed_audit()
    audit["url"] = "http://secret"
    audit["raw_cfs_payload"] = {"secret": True}
    store = appmod.load_audits()
    store["completed"][0] = audit
    appmod.save_audits(store)

    response = appmod.api_ui_audit_export(audit["audit_id"])
    payload = json.loads(response.body)
    encoded = json.dumps(payload)
    assert payload["schema"] == "spoolman-cfs-sync.print-audit.v1"
    assert "http://secret" not in encoded
    assert "raw_cfs_payload" not in encoded
    assert response.headers["content-disposition"].startswith("attachment;")


def test_clear_data_removes_audits_but_not_spoolman_inventory(audit_env):
    state, cfg, _inventory, _calls = audit_env
    start_audit(state, cfg)
    state.job_track_name = ""
    appmod._clear_local_accounting(state)

    assert appmod.load_audits() == appmod._empty_audit_store()


def test_ui_state_limits_compatibility_records_without_deleting_internal_records(audit_env):
    state, _cfg, _inventory, _calls = audit_env
    state.spoolman_sync_records = {
        f"record-{i}": {"updated_at": float(i), "status": "synced"} for i in range(75)
    }
    payload = appmod._ui_state_dict(state)

    assert len(payload["spoolman_sync_records"]) == appmod.UI_SYNC_RECORD_LIMIT
    assert len(state.spoolman_sync_records) == 75
    assert "record-74" in payload["spoolman_sync_records"]


def test_legacy_records_remain_available_without_audit_verdict(audit_env, monkeypatch):
    state, _cfg, _inventory, _calls = audit_env
    state.spoolman_sync_records = {
        "legacy:1A": {"job": "old.gcode", "slot": "1A", "status": "synced", "updated_at": 1.0}
    }
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    payload = appmod.api_ui_audits().result
    assert payload["legacy_records"][0]["record_key"] == "legacy:1A"
    assert "verdict" not in payload["legacy_records"][0]


def _complete_missing_audit(state, cfg, calls, monkeypatch, *, used_mm=100.0):
    """Finish an audit whose original write was acknowledged but did not apply."""
    def unapplied_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        return {"id": int(spool_id), "used_length": 1000.0}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", unapplied_use)
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": used_mm}
    state.job_track_printer_used_mm = used_mm
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", used_mm)
    audit = completed_audit()
    assert audit["verdict"] == "needs_attention"
    return audit


def test_audit_detail_exposes_per_spool_fix_for_confirmed_missing_usage(audit_env, monkeypatch):
    state, cfg, _inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    detail = appmod.api_ui_audit_detail(audit["audit_id"]).result
    remediation = detail["remediation"]["spools"]["1"]

    assert remediation["kind"] == "fixable_missing"
    assert remediation["can_fix"] is True
    assert remediation["missing_mm"] == 100.0
    assert remediation["source_slots"] == ["1A"]


def test_audit_fix_deducts_only_the_fresh_confirmed_shortfall(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)

    def apply_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += float(amount)
        inventory[int(spool_id)]["remaining_length"] -= float(amount)
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", apply_use)
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    result = appmod.api_ui_spoolman_audit_fix(
        appmod.UiSpoolmanAuditFixRequest(
            audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
        )
    ).result

    assert calls == [(1, 100.0), (1, 100.0)]
    assert result["correction"]["status"] == "synced"
    assert result["correction"]["missing_mm"] == 100.0
    correction = state.spoolman_sync_records[result["correction"]["record_key"]]
    assert correction["sync_phase"] == "audit_fix"
    assert correction["used_mm"] == 100.0
    assert correction["used_g"] == 0.0
    assert correction["source_slots"] == ["1A"]
    assert completed_audit()["verdict"] == "verified"
    assert any(event["phase"] == "audit_fix" for event in completed_audit()["events"])


def test_audit_fix_rejects_stale_confirmation_without_writing(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)
    inventory[1]["used_length"] += 25.0
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    with pytest.raises(appmod.HTTPException) as exc:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(
                audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
            )
        )

    assert exc.value.status_code == 409
    assert "changed" in str(exc.value.detail)
    assert calls == [(1, 100.0)]
    assert inventory[1]["used_length"] == 1025.0


def test_audit_fix_blocks_uncertain_original_record(audit_env, monkeypatch):
    state, cfg, _inventory, _calls = audit_env

    def timeout_use(_spool_id, _amount, cfg=None):
        raise appmod.SpoolmanTimeoutError("network timeout")

    monkeypatch.setattr(appmod, "_spoolman_use_spool", timeout_use)
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 100.0}
    state.job_track_printer_used_mm = 100.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 100.0)
    audit = completed_audit()
    assert audit["verdict"] == "needs_attention"
    assert next(iter(state.spoolman_sync_records.values()))["status"] == "timeout_uncertain"
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    with pytest.raises(appmod.HTTPException) as exc:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(
                audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
            )
        )

    assert exc.value.status_code == 409
    assert "may already have changed inventory" in str(exc.value.detail)


def test_audit_fix_never_reverses_an_excess_deduction(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env

    def excess_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += float(amount) + 25.0
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", excess_use)
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 100.0}
    state.job_track_printer_used_mm = 100.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 100.0)
    audit = completed_audit()
    assert audit["verdict"] == "needs_attention"
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    with pytest.raises(appmod.HTTPException) as exc:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(
                audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
            )
        )

    assert exc.value.status_code == 409
    assert "too much" in str(exc.value.detail)
    assert calls == [(1, 100.0)]


@pytest.mark.parametrize("control", ["enabled", "dry_run"])
def test_audit_fix_respects_current_runtime_safety_controls(audit_env, monkeypatch, control):
    state, cfg, _inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)
    cfg[control] = False if control == "enabled" else True
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    with pytest.raises(appmod.HTTPException) as exc:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(
                audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
            )
        )

    assert exc.value.status_code == 409
    assert "Enable real Spoolman sync" in str(exc.value.detail)
    assert calls == [(1, 100.0)]


def test_audit_fix_uses_the_frozen_server_and_audited_spool(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)
    original_url = cfg["url"]
    seen_urls = []

    def get_spool(spool_id, effective_cfg=None):
        seen_urls.append(effective_cfg["url"])
        return json.loads(json.dumps(inventory[int(spool_id)]))

    def apply_use(spool_id, amount, effective_cfg=None):
        assert int(spool_id) == 1
        assert effective_cfg["url"] == original_url
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += float(amount)
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    cfg.update({"url": "http://changed.test:7912"})
    cfg["slot_mappings"]["1A"] = 2
    monkeypatch.setattr(appmod, "_spoolman_get_spool", get_spool)
    monkeypatch.setattr(appmod, "_spoolman_use_spool", apply_use)
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    appmod.api_ui_spoolman_audit_fix(
        appmod.UiSpoolmanAuditFixRequest(
            audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
        )
    )

    assert seen_urls and set(seen_urls) == {original_url}
    assert calls == [(1, 100.0), (1, 100.0)]


def test_audit_fix_records_cannot_use_the_generic_retry_endpoint(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)

    def apply_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += float(amount)
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", apply_use)
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    result = appmod.api_ui_spoolman_audit_fix(
        appmod.UiSpoolmanAuditFixRequest(
            audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
        )
    ).result

    with pytest.raises(appmod.HTTPException) as exc:
        appmod.api_ui_spoolman_retry(
            appmod.UiSpoolmanRetryRequest(record_key=result["correction"]["record_key"])
        )

    assert exc.value.status_code == 409
    assert "fresh inventory check" in str(exc.value.detail)


def test_audit_fix_sends_only_the_partial_missing_difference(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env

    def partial_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += 25.0
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", partial_use)
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 100.0}
    state.job_track_printer_used_mm = 100.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 100.0)
    audit = completed_audit()
    assert audit["observed"]["spools"]["1"]["difference_mm"] == -75.0
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    result = appmod.api_ui_spoolman_audit_fix(
        appmod.UiSpoolmanAuditFixRequest(
            audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=75.0,
        )
    ).result

    assert result["correction"]["status"] == "synced"
    assert calls == [(1, 100.0), (1, 75.0)]


def test_audit_fix_refreshes_without_write_when_external_change_already_resolved_it(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)
    inventory[1]["used_length"] += 100.0
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    result = appmod.api_ui_spoolman_audit_fix(
        appmod.UiSpoolmanAuditFixRequest(
            audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
        )
    ).result

    assert result["correction"] == {"status": "not_needed", "missing_mm": 0.0}
    assert calls == [(1, 100.0)]
    assert completed_audit()["verdict"] == "verified"


def test_audit_fix_blocks_missing_start_or_current_inventory_evidence(audit_env, monkeypatch):
    state, cfg, _inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)
    store = appmod.load_audits()
    store["completed"][0]["snapshots"]["before"]["1"]["used_length_mm"] = None
    appmod.save_audits(store)
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    with pytest.raises(appmod.HTTPException) as missing_before:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(
                audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
            )
        )

    assert missing_before.value.status_code == 409
    assert calls == [(1, 100.0)]


def test_audit_fix_blocks_missing_fresh_spoolman_evidence(audit_env, monkeypatch):
    state, cfg, _inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)

    def unavailable_get(_spool_id, cfg=None):
        raise appmod.SpoolmanHttpError(503, "Spoolman unavailable")

    monkeypatch.setattr(appmod, "_spoolman_get_spool", unavailable_get)
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    with pytest.raises(appmod.HTTPException) as exc:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(
                audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
            )
        )

    assert exc.value.status_code == 502
    assert calls == [(1, 100.0)]


def test_audit_fix_rejects_unknown_active_and_non_attention_audits(audit_env, monkeypatch):
    state, cfg, _inventory, calls = audit_env
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    with pytest.raises(appmod.HTTPException) as unknown:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(audit_id="missing", spool_id=1, expected_missing_mm=10.0)
        )
    assert unknown.value.status_code == 404

    active = start_audit(state, cfg)
    with pytest.raises(appmod.HTTPException) as active_error:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(audit_id=active["audit_id"], spool_id=1, expected_missing_mm=10.0)
        )
    assert active_error.value.status_code == 409
    assert calls == []


def test_audit_fix_blocks_conflicting_audit_record(audit_env, monkeypatch):
    state, cfg, _inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)
    state.spoolman_sync_records["manual-conflict"] = {
        "audit_id": audit["audit_id"], "spool_id": 1, "status": "conflict", "sync_phase": "audit_fix",
    }
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    with pytest.raises(appmod.HTTPException) as exc:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(
                audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
            )
        )

    assert exc.value.status_code == 409
    assert "may already have changed inventory" in str(exc.value.detail)
    assert calls == [(1, 100.0)]


def test_audit_fix_timeout_blocks_a_later_correction_attempt(audit_env, monkeypatch):
    state, cfg, _inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)

    def timeout_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        raise appmod.SpoolmanTimeoutError("network timeout")

    monkeypatch.setattr(appmod, "_spoolman_use_spool", timeout_use)
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    first = appmod.api_ui_spoolman_audit_fix(
        appmod.UiSpoolmanAuditFixRequest(
            audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
        )
    ).result
    assert first["correction"]["status"] == "timeout_uncertain"

    with pytest.raises(appmod.HTTPException) as blocked:
        appmod.api_ui_spoolman_audit_fix(
            appmod.UiSpoolmanAuditFixRequest(
                audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
            )
        )

    assert blocked.value.status_code == 409
    assert calls == [(1, 100.0), (1, 100.0)]


def test_audit_fix_clean_validation_failure_retries_only_the_same_fresh_basis(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)
    get_count = {"value": 0}

    def intermittent_get(spool_id, cfg=None):
        get_count["value"] += 1
        if get_count["value"] == 2:
            raise appmod.SpoolmanHttpError(503, "validation unavailable")
        return json.loads(json.dumps(inventory[int(spool_id)]))

    def apply_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += float(amount)
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_get_spool", intermittent_get)
    monkeypatch.setattr(appmod, "_spoolman_use_spool", apply_use)
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    request = appmod.UiSpoolmanAuditFixRequest(
        audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
    )

    failed = appmod.api_ui_spoolman_audit_fix(request).result
    retried = appmod.api_ui_spoolman_audit_fix(request).result

    assert failed["correction"]["status"] == "failed"
    assert retried["correction"]["status"] == "synced"
    assert failed["correction"]["record_key"] == retried["correction"]["record_key"]
    assert calls == [(1, 100.0), (1, 100.0)]


def test_audit_fix_is_aggregated_per_shared_spool(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    cfg["slot_mappings"].update({"1A": 1, "1B": 1})

    def unapplied_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", unapplied_use)
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 75.0, "1B": 125.0}
    state.job_track_printer_used_mm = 200.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 200.0)
    audit = completed_audit()
    assert audit["expected"]["spools"] == {"1": 200.0}
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    def apply_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += float(amount)
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", apply_use)
    result = appmod.api_ui_spoolman_audit_fix(
        appmod.UiSpoolmanAuditFixRequest(
            audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=200.0,
        )
    ).result

    assert result["correction"]["status"] == "synced"
    assert calls == [(1, 75.0), (1, 125.0), (1, 200.0)]
    assert state.spoolman_sync_records[result["correction"]["record_key"]]["source_slots"] == ["1A", "1B"]


def test_audit_fix_leaves_other_spool_discrepancies_for_independent_review(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    cfg["slot_mappings"].update({"1A": 1, "1B": 2})

    def unapplied_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", unapplied_use)
    start_audit(state, cfg)
    state.job_track_slot_mm = {"1A": 100.0, "1B": 100.0}
    state.job_track_printer_used_mm = 200.0
    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", "job-1", 200.0)
    audit = completed_audit()
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    def apply_use(spool_id, amount, cfg=None):
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += float(amount)
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", apply_use)
    result = appmod.api_ui_spoolman_audit_fix(
        appmod.UiSpoolmanAuditFixRequest(
            audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
        )
    ).result

    assert result["audit"]["verdict"] == "needs_attention"
    assert result["remediation"]["spools"]["2"]["can_fix"] is True
    assert calls == [(1, 100.0), (2, 100.0), (1, 100.0)]


def test_audit_fix_serializes_concurrent_requests_without_double_deducting(audit_env, monkeypatch):
    state, cfg, inventory, calls = audit_env
    audit = _complete_missing_audit(state, cfg, calls, monkeypatch)

    def slow_apply(spool_id, amount, cfg=None):
        time.sleep(0.05)
        calls.append((int(spool_id), float(amount)))
        inventory[int(spool_id)]["used_length"] += float(amount)
        return {"id": int(spool_id), "used_length": inventory[int(spool_id)]["used_length"]}

    monkeypatch.setattr(appmod, "_spoolman_use_spool", slow_apply)
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    request = appmod.UiSpoolmanAuditFixRequest(
        audit_id=audit["audit_id"], spool_id=1, expected_missing_mm=100.0,
    )

    def attempt():
        try:
            return appmod.api_ui_spoolman_audit_fix(request).result["correction"]["status"]
        except appmod.HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: attempt(), range(2)))

    assert sorted(outcomes, key=str) == [409, "synced"]
    assert calls == [(1, 100.0), (1, 100.0)]
