import json

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
