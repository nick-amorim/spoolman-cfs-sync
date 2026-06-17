import json

import pytest
from fastapi import HTTPException

import main as appmod
from models.schemas import UiSpoolmanRetryRequest


@pytest.fixture
def state(monkeypatch):
    st = appmod.default_state()
    monkeypatch.setattr(appmod, "save_state", lambda s: None)
    return st


def spoolman_config(*, enabled=False, dry_run=True, mappings=None, sync_mode="post_print", live_min_delta_mm=100.0):
    cfg = appmod._default_spoolman_config()
    cfg.update(
        {
            "enabled": enabled,
            "dry_run": dry_run,
            "url": "http://spoolman.local:7912",
            "sync_mode": sync_mode,
            "live_min_delta_mm": live_min_delta_mm,
        }
    )
    if mappings is not None:
        cfg["slot_mappings"].update(mappings)
    return cfg


def make_record(*, spool_id=12, used_mm=123.4, used_g=0.37, status="pending"):
    return appmod._base_spoolman_record(
        job_key="moonraker:job-123",
        job_name="part.gcode",
        slot_id="1A",
        spool_id=spool_id,
        used_mm=used_mm,
        used_g=used_g,
        result="complete",
        status=status,
    )


def test_normalize_spoolman_config_accepts_file_and_normalized_shapes():
    file_shape = {
        "spoolman": {
            "enabled": True,
            "dry_run": False,
            "url": "http://spoolman.local:7912/",
            "timeout_sec": 99,
            "sync_mode": "live",
            "live_min_delta_mm": 0.5,
            "slot_mappings": {"1a": "42", "1B": "", "9Z": 100},
        }
    }
    normalized = appmod._normalize_spoolman_config(file_shape)

    assert normalized["enabled"] is True
    assert normalized["dry_run"] is False
    assert normalized["url"] == "http://spoolman.local:7912"
    assert normalized["timeout_sec"] == 30.0
    assert normalized["sync_mode"] == "live"
    assert normalized["live_min_delta_mm"] == 1.0
    assert normalized["slot_mappings"]["1A"] == 42
    assert normalized["slot_mappings"]["1B"] is None
    assert "9Z" not in normalized["slot_mappings"]

    assert appmod._normalize_spoolman_config(normalized) == normalized


def test_update_printer_config_preserves_spoolman_settings(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    existing = {
        "moonraker_url": "",
        "poll_interval_sec": 5,
        "filament_diameter_mm": 1.75,
        "cfs_autosync": False,
        "spoolman": spoolman_config(dry_run=True, mappings={"1A": 16}),
    }
    config_path.write_text(json.dumps(existing))
    monkeypatch.setattr(appmod, "CONFIG_PATH", config_path)
    monkeypatch.setattr(appmod, "_ensure_data_files", lambda: None)

    updated = appmod._update_printer_config(
        {
            "moonraker_url": "http://192.168.1.50:7125/",
            "poll_interval_sec": 0,
            "filament_diameter_mm": 1.74,
            "cfs_autosync": True,
        }
    )

    saved = json.loads(config_path.read_text())
    assert updated["moonraker_url"] == "http://192.168.1.50:7125"
    assert updated["poll_interval_sec"] == 1.0
    assert updated["filament_diameter_mm"] == 1.74
    assert updated["cfs_autosync"] is True
    assert saved["spoolman"]["slot_mappings"]["1A"] == 16


def test_stable_print_key_prefers_moonraker_job_id():
    assert appmod._stable_print_key("part.gcode", 10, 20, job_id="abc-123") == "moonraker:abc-123"
    assert appmod._stable_print_key("part.gcode", 10.4, 20.9) == "local:part.gcode:10:20"


def test_current_job_id_is_best_effort_across_sources():
    assert appmod._moonraker_current_job_id({"job_id": "ps-job"}, {}) == "ps-job"
    assert appmod._moonraker_current_job_id({}, {"uid": "vsd-uid"}) == "vsd-uid"
    assert appmod._moonraker_current_job_id({}, {"cur_print_data": {"id": "cpd-id"}}) == "cpd-id"
    assert appmod._moonraker_current_job_id({}, {}) == ""


def test_clear_cfs_connection_removes_stale_printer_data(state):
    state.cfs_connected = True
    state.cfs_last_update = 123.0
    state.cfs_active_slot = "1A"
    state.cfs_slots = {"1A": {"material": "PLA"}}
    state.cfs_raw = {"box": {"T1": {}}}

    appmod._clear_cfs_connection(state)

    assert state.cfs_connected is False
    assert state.cfs_last_update == 123.0
    assert state.cfs_active_slot is None
    assert state.cfs_slots == {}
    assert state.cfs_raw == {}


def test_moonraker_build_url_encodes_object_names_with_spaces():
    url = appmod._moonraker_build_url(
        "http://printer.local:7125/",
        ["print_stats", "gcode_macro SET_ACTIVE_SPOOL"],
    )

    assert url == "http://printer.local:7125/printer/objects/query?print_stats&gcode_macro%20SET_ACTIVE_SPOOL"


def test_moonraker_native_spoolman_without_active_spool_has_no_warning(monkeypatch):
    def fake_get(url, timeout=5.0):
        if url.endswith("/server/info"):
            return {"result": {"components": ["database", "spoolman"]}}
        if url.endswith("/server/config"):
            return {"result": {"config": {"spoolman": {"server": "http://spoolman.local:7912"}}}}
        if url.endswith("/server/spoolman/status"):
            return {"result": {"spoolman_connected": True, "spool_id": None}}
        raise AssertionError(url)

    monkeypatch.setattr(appmod, "_http_get_json", fake_get)

    detected, warning = appmod._moonraker_detect_native_spoolman("http://printer.local:7125")

    assert detected is True
    assert warning == ""


def test_moonraker_native_spoolman_with_active_spool_warns(monkeypatch):
    def fake_get(url, timeout=5.0):
        if url.endswith("/server/info"):
            return {"result": {"components": ["spoolman"]}}
        if url.endswith("/server/config"):
            return {"result": {}}
        if url.endswith("/server/spoolman/status"):
            return {"result": {"spoolman_connected": True, "spool_id": 16}}
        raise AssertionError(url)

    monkeypatch.setattr(appmod, "_http_get_json", fake_get)

    detected, warning = appmod._moonraker_detect_native_spoolman("http://printer.local:7125")

    assert detected is True
    assert "active spool selected" in warning


def test_moonraker_native_spoolman_not_installed_has_no_warning(monkeypatch):
    def fake_get(url, timeout=5.0):
        if url.endswith("/server/info"):
            return {"result": {"components": ["database"]}}
        if url.endswith("/server/config"):
            return {"result": {"config": {}}}
        raise AssertionError(url)

    monkeypatch.setattr(appmod, "_http_get_json", fake_get)

    detected, warning = appmod._moonraker_detect_native_spoolman("http://printer.local:7125")

    assert detected is False
    assert warning == ""


def test_app_update_check_reports_available_clean_checkout(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(appmod, "APP_DIR", tmp_path)

    def fake_run(args, *, timeout=30.0, check=True):
        cmd = tuple(args)
        if cmd[:2] == ("git", "config"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd == ("git", "rev-parse", "--is-inside-work-tree"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="true\n", stderr="")
        if cmd == ("git", "fetch", "--quiet", "origin", "main"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd == ("git", "rev-parse", "HEAD"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="1111111111111111111111111111111111111111\n", stderr="")
        if cmd == ("git", "rev-parse", "origin/main"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="2222222222222222222222222222222222222222\n", stderr="")
        if cmd == ("git", "rev-parse", "--abbrev-ref", "HEAD"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if cmd == ("git", "status", "--porcelain"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd[:3] == ("git", "merge-base", "--is-ancestor"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(appmod, "_run_app_update_cmd", fake_run)

    result = appmod._app_update_check(fetch=True)

    assert result["supported"] is True
    assert result["update_available"] is True
    assert result["can_update"] is True
    assert result["current_short"] == "11111111"
    assert result["remote_short"] == "22222222"


def test_app_update_check_blocks_dirty_checkout(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(appmod, "APP_DIR", tmp_path)

    def fake_run(args, *, timeout=30.0, check=True):
        cmd = tuple(args)
        if cmd[:2] == ("git", "config"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        values = {
            ("git", "rev-parse", "--is-inside-work-tree"): "true\n",
            ("git", "rev-parse", "HEAD"): "1111111111111111111111111111111111111111\n",
            ("git", "rev-parse", "origin/main"): "2222222222222222222222222222222222222222\n",
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main\n",
            ("git", "status", "--porcelain"): " M main.py\n",
        }
        if cmd in values:
            return appmod.subprocess.CompletedProcess(args, 0, stdout=values[cmd], stderr="")
        if cmd[:3] == ("git", "merge-base", "--is-ancestor"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(appmod, "_run_app_update_cmd", fake_run)

    result = appmod._app_update_check(fetch=False)

    assert result["update_available"] is True
    assert result["can_update"] is False
    assert result["dirty"] is True
    assert "local changes" in result["message"]


def test_apply_app_update_resets_to_origin_and_installs_requirements(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    monkeypatch.setattr(appmod, "APP_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_UPDATE_LOCK", appmod.threading.Lock())
    calls = []

    def fake_run(args, *, timeout=30.0, check=True):
        calls.append(tuple(args))
        cmd = tuple(args)
        if cmd == ("git", "rev-parse", "--is-inside-work-tree"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="true\n", stderr="")
        if cmd == ("git", "fetch", "--quiet", "origin", "main"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd == ("git", "rev-parse", "HEAD"):
            stdout = "1111111111111111111111111111111111111111\n"
            if ("git", "reset", "--hard", "origin/main") in calls:
                stdout = "2222222222222222222222222222222222222222\n"
            return appmod.subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if cmd == ("git", "rev-parse", "origin/main"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="2222222222222222222222222222222222222222\n", stderr="")
        if cmd == ("git", "rev-parse", "--abbrev-ref", "HEAD"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if cmd == ("git", "status", "--porcelain"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd[:3] == ("git", "merge-base", "--is-ancestor"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd[:2] == ("git", "config"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd == ("git", "checkout", "-q", "main"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if cmd == ("git", "reset", "--hard", "origin/main"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if len(cmd) >= 4 and cmd[1:4] == ("-m", "pip", "install"):
            return appmod.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(appmod, "_run_app_update_cmd", fake_run)
    monkeypatch.setattr(appmod, "_schedule_app_restart", lambda delay_sec=2.0: calls.append(("restart", str(delay_sec))))

    result = appmod._apply_app_update(schedule_restart=True)

    assert result["started"] is True
    assert result["restart_scheduled"] is True
    assert ("git", "config", "--global", "--add", "safe.directory", str(tmp_path)) in calls
    assert ("git", "checkout", "-q", "main") in calls
    assert ("git", "reset", "--hard", "origin/main") in calls
    assert any(cmd[1:4] == ("-m", "pip", "install") for cmd in calls)
    assert ("restart", "2.0") in calls


def test_gcode_parser_maps_orca_tool_indexes_to_cfs_slots(monkeypatch, state):
    monkeypatch.setattr(appmod, "mm_to_g", lambda material, mm: mm / 100.0)

    appmod._parse_gcode_usage_chunk(
        state,
        """
        M83 ; relative extrusion
        T3
        G1 X1 Y1 E.4
        G1 X2 Y2 E.6
        G1 E-.2
        T0
        G1 X3 Y3 E1.25
        """,
        fallback_slot="1A",
        final=True,
    )

    assert state.job_track_slot_mm["1D"] == pytest.approx(1.0)
    assert state.job_track_slot_mm["1A"] == pytest.approx(1.25)
    assert state.job_track_slot_g["1D"] == pytest.approx(0.01)
    assert state.job_track_slot_g["1A"] == pytest.approx(0.0125)
    assert appmod._job_track_total_mm(state) == 2


def test_gcode_parser_handles_absolute_mode_and_g92(state):
    appmod._parse_gcode_usage_chunk(
        state,
        """
        M82
        T1
        G92 E0
        G1 E2.0
        G1 E1.5
        G1 E2.0
        G92 E0
        G1 E0.25
        """,
        fallback_slot="1A",
        final=True,
    )

    assert state.job_track_slot_mm["1B"] == pytest.approx(2.75)
    assert appmod._job_track_total_mm(state) == 3


def test_gcode_parser_persists_partial_line_between_chunks(state):
    appmod._parse_gcode_usage_chunk(state, "M83\nT2\nG1 X1 E", fallback_slot="1A")

    assert state.job_track_parser_tail == "G1 X1 E"
    assert state.job_track_slot_mm == {}

    appmod._parse_gcode_usage_chunk(state, ".75\n", fallback_slot="1A")

    assert state.job_track_parser_tail == ""
    assert state.job_track_slot_mm["1C"] == pytest.approx(0.75)


def test_moonraker_gcode_file_url_encodes_path_segments():
    url = appmod._moonraker_file_url(
        "http://printer.local:7125/",
        "Folder One/Keychain Draft_PLA_2h3m.gcode",
    )

    assert url == "http://printer.local:7125/server/files/gcodes/Folder%20One/Keychain%20Draft_PLA_2h3m.gcode"


def test_clear_local_accounting_removes_test_data(monkeypatch, state):
    saved = []
    monkeypatch.setattr(appmod, "save_state", lambda s: saved.append(s))
    state.slot_history = {"1A": [{"used_mm": 10}]}
    state.moonraker_allocations = {"job": {"alloc_g": {"1A": 1.2}}}
    state.spoolman_sync_records = {"job:1A": make_record(status="dry_run")}
    state.current_job = "old.gcode"
    state.current_job_filament_mm = 123
    state.current_job_filament_g = 0.4
    state.last_accounted_job_mm = 123
    state.last_accounted_slot = "1A"
    state.job_track_last_mm = 123
    state.job_track_printer_used_mm = 123.0
    state.job_track_file_path = "old.gcode"
    state.job_track_last_file_position = 456
    state.job_track_file_size = 789
    state.job_track_extruder_mode = "absolute"
    state.job_track_last_e = 12.3
    state.job_track_parser_slot = "1A"
    state.job_track_parser_tail = "G1 X1 E"
    state.slots["1A"].spool_epoch_consumed_g_total = 1.5
    state.slots["1A"].spool_ref_consumed_g = 1.0

    appmod._clear_local_accounting(state)

    assert state.slot_history == {}
    assert state.moonraker_allocations == {}
    assert state.spoolman_sync_records == {}
    assert state.current_job == ""
    assert state.current_job_filament_mm == 0
    assert state.current_job_filament_g == 0.0
    assert state.last_accounted_job_mm == 0
    assert state.last_accounted_slot is None
    assert state.job_track_last_mm == 0
    assert state.job_track_printer_used_mm == 0.0
    assert state.job_track_file_path == ""
    assert state.job_track_last_file_position == 0
    assert state.job_track_file_size == 0
    assert state.job_track_extruder_mode == "relative"
    assert state.job_track_last_e == 0.0
    assert state.job_track_parser_slot == ""
    assert state.job_track_parser_tail == ""
    assert state.slots["1A"].spool_epoch_consumed_g_total == 0.0
    assert state.slots["1A"].spool_ref_consumed_g == 0.0
    assert saved == [state]


def test_clear_local_accounting_blocks_active_tracking(monkeypatch, state):
    monkeypatch.setattr(appmod, "save_state", lambda s: pytest.fail("should not save"))
    state.job_track_name = "printing.gcode"

    with pytest.raises(HTTPException) as exc:
        appmod._clear_local_accounting(state)

    assert exc.value.status_code == 409


def test_unmapped_finished_job_records_skip_without_network(monkeypatch, state):
    state.job_track_slot_mm = {"1A": 250}
    state.job_track_slot_g = {"1A": 0.75}
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": spoolman_config(dry_run=True)})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda *args, **kwargs: pytest.fail("network called"))
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda *args, **kwargs: pytest.fail("network called"))

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete")

    rec = state.spoolman_sync_records["local:part.gcode:10:20:1A"]
    assert rec["status"] == "skipped_unmapped"
    assert rec["spool_id"] is None


def test_dry_run_records_mapped_usage_without_network(monkeypatch, state):
    state.job_track_slot_mm = {"1A": 250}
    state.job_track_slot_g = {"1A": 0.75}
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": spoolman_config(dry_run=True, mappings={"1A": 12})})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda *args, **kwargs: pytest.fail("network called"))
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda *args, **kwargs: pytest.fail("network called"))

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", job_id="job-123")

    rec = state.spoolman_sync_records["moonraker:job-123:1A"]
    assert rec["status"] == "dry_run"
    assert rec["spool_id"] == 12
    assert rec["used_mm"] == 250.0


def test_real_sync_validates_spool_and_posts_length_once(monkeypatch, state):
    calls = []
    cfg = spoolman_config(enabled=True, dry_run=False, mappings={"1A": 12})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda spool_id, used_mm, cfg=None: calls.append((spool_id, used_mm)) or {})

    rec = appmod._spoolman_sync_record(state, "moonraker:job-123:1A", make_record(), cfg)

    assert rec["status"] == "synced"
    assert calls == [(12, 123.4)]

    second = appmod._spoolman_sync_record(state, "moonraker:job-123:1A", make_record(), cfg)

    assert second["status"] == "synced"
    assert calls == [(12, 123.4)]


def test_live_sync_posts_threshold_chunks_and_checkpoints(monkeypatch, state):
    calls = []
    cfg = spoolman_config(
        enabled=True,
        dry_run=False,
        mappings={"1A": 12},
        sync_mode="live",
        live_min_delta_mm=100.0,
    )
    state.job_track_name = "part.gcode"
    state.job_track_id = "job-123"
    state.job_track_started_at = 10
    state.job_track_slot_mm = {"1A": 150.0}
    state.job_track_slot_g = {"1A": 1.5}
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": cfg})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda spool_id, used_mm, cfg=None: calls.append((spool_id, used_mm)) or {})

    appmod._plan_spoolman_live_sync_for_current_job(state)

    assert calls == [(12, 150.0)]
    assert state.job_track_spoolman_live_synced_mm["1A"] == 150.0
    rec = state.spoolman_sync_records["moonraker:job-123:1A:live:1"]
    assert rec["sync_phase"] == "live"
    assert rec["status"] == "synced"

    appmod._plan_spoolman_live_sync_for_current_job(state)
    assert calls == [(12, 150.0)]

    state.job_track_slot_mm["1A"] = 230.0
    state.job_track_slot_g["1A"] = 2.3
    appmod._plan_spoolman_live_sync_for_current_job(state)
    assert calls == [(12, 150.0)]

    state.job_track_slot_mm["1A"] = 260.0
    state.job_track_slot_g["1A"] = 2.6
    appmod._plan_spoolman_live_sync_for_current_job(state)
    assert calls == [(12, 150.0), (12, 110.0)]
    assert state.job_track_spoolman_live_synced_mm["1A"] == 260.0


def test_live_sync_caps_parser_usage_to_printer_reported_total(monkeypatch, state):
    calls = []
    cfg = spoolman_config(
        enabled=True,
        dry_run=False,
        mappings={"1A": 16, "1D": 10},
        sync_mode="live",
        live_min_delta_mm=1.0,
    )
    state.current_job_filament_mm = 400.0
    state.job_track_printer_used_mm = 200.0
    state.job_track_name = "part.gcode"
    state.job_track_id = "job-123"
    state.job_track_started_at = 10
    state.job_track_slot_mm = {"1A": 100.0, "1D": 300.0}
    state.job_track_slot_g = {"1A": 1.0, "1D": 3.0}
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": cfg})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda spool_id, used_mm, cfg=None: calls.append((spool_id, used_mm)) or {})

    appmod._plan_spoolman_live_sync_for_current_job(state)

    assert calls == [(16, 50.0), (10, 150.0)]
    assert state.job_track_spoolman_live_synced_mm["1A"] == 50.0
    assert state.job_track_spoolman_live_synced_mm["1D"] == 150.0


def test_live_final_sync_posts_only_unsynced_remainder(monkeypatch, state):
    calls = []
    cfg = spoolman_config(enabled=True, dry_run=False, mappings={"1A": 12}, sync_mode="live")
    state.job_track_slot_mm = {"1A": 250.0}
    state.job_track_slot_g = {"1A": 2.5}
    state.job_track_spoolman_live_synced_mm = {"1A": 200.0}
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": cfg})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda spool_id, used_mm, cfg=None: calls.append((spool_id, used_mm)) or {})

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", job_id="job-123")

    assert calls == [(12, 50.0)]
    rec = state.spoolman_sync_records["moonraker:job-123:1A:final"]
    assert rec["sync_phase"] == "final"
    assert rec["used_mm"] == 50.0
    assert rec["status"] == "synced"


def test_live_final_sync_caps_parser_total_to_printer_reported_total(monkeypatch, state):
    calls = []
    cfg = spoolman_config(enabled=True, dry_run=False, mappings={"1C": 1}, sync_mode="live")
    state.job_track_slot_mm = {"1C": 9084.64}
    state.job_track_slot_g = {"1C": 27.1}
    state.job_track_spoolman_live_synced_mm = {"1C": 311.814}
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": cfg})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda spool_id, used_mm, cfg=None: calls.append((spool_id, used_mm)) or {})

    appmod._plan_spoolman_sync_for_finished_job(
        state,
        "10x5mm-magnet-dispenser-barrel_PLA_1h17m.gcode",
        10,
        20,
        "complete",
        printer_total_mm=7683.44,
    )

    assert calls == [(1, pytest.approx(7371.626, abs=0.001))]
    rec = state.spoolman_sync_records["local:10x5mm-magnet-dispenser-barrel_PLA_1h17m.gcode:10:20:1C:final"]
    assert rec["sync_phase"] == "final"
    assert rec["used_mm"] == pytest.approx(7371.626, abs=0.001)


def test_live_uncertain_record_blocks_final_reconciliation(monkeypatch, state):
    calls = []
    cfg = spoolman_config(enabled=True, dry_run=False, mappings={"1A": 12}, sync_mode="live")
    state.job_track_slot_mm = {"1A": 250.0}
    state.job_track_slot_g = {"1A": 2.5}
    state.job_track_spoolman_live_synced_mm = {"1A": 150.0}
    state.job_track_spoolman_live_blocked = {"1A": {"status": "timeout_uncertain"}}
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": cfg})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda spool_id, used_mm, cfg=None: calls.append((spool_id, used_mm)) or {})

    appmod._plan_spoolman_sync_for_finished_job(state, "part.gcode", 10, 20, "complete", job_id="job-123")

    assert calls == []
    rec = state.spoolman_sync_records["moonraker:job-123:1A:final"]
    assert rec["sync_phase"] == "final"
    assert rec["status"] == "timeout_uncertain"
    assert rec["used_mm"] == 100.0


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([{"id": 1}], [{"id": 1}]),
        ({"items": [{"id": 2}]}, [{"id": 2}]),
        ({"results": [{"id": 3}]}, [{"id": 3}]),
        ({"spools": [{"id": 4}]}, [{"id": 4}]),
        ({"data": [{"id": 5}]}, [{"id": 5}]),
        ({"unexpected": []}, []),
    ],
)
def test_spoolman_list_spools_accepts_common_response_shapes(monkeypatch, payload, expected):
    monkeypatch.setattr(appmod, "_spoolman_request_json", lambda path, cfg=None: payload)

    assert appmod._spoolman_list_spools(spoolman_config()) == expected


def test_spoolman_use_spool_uses_put_method(monkeypatch):
    calls = []

    def fake_request(path, *, method="GET", payload=None, cfg=None):
        calls.append({"path": path, "method": method, "payload": payload})
        return {}

    monkeypatch.setattr(appmod, "_spoolman_request_json", fake_request)

    appmod._spoolman_use_spool(10, 2345.949, spoolman_config())

    assert calls == [
        {
            "path": "/spool/10/use",
            "method": "PUT",
            "payload": {"use_length": 2345.949},
        }
    ]


def test_existing_record_with_different_usage_becomes_conflict(monkeypatch, state):
    cfg = spoolman_config(enabled=True, dry_run=False, mappings={"1A": 12})
    state.spoolman_sync_records["moonraker:job-123:1A"] = make_record(used_mm=100.0, status="failed")
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda *args, **kwargs: pytest.fail("network called"))
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda *args, **kwargs: pytest.fail("network called"))

    rec = appmod._spoolman_sync_record(state, "moonraker:job-123:1A", make_record(used_mm=101.0), cfg)

    assert rec["status"] == "conflict"


@pytest.mark.parametrize(
    "error",
    [
        appmod.SpoolmanTimeoutError("timed out"),
        appmod.SpoolmanHttpError(408, "request timeout"),
        appmod.SpoolmanHttpError(500, "server error"),
        RuntimeError("connection dropped"),
    ],
)
def test_uncertain_use_failures_become_timeout_uncertain(monkeypatch, state, error):
    cfg = spoolman_config(enabled=True, dry_run=False, mappings={"1A": 12})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})

    def fail_use(*args, **kwargs):
        raise error

    monkeypatch.setattr(appmod, "_spoolman_use_spool", fail_use)

    rec = appmod._spoolman_sync_record(state, "moonraker:job-123:1A", make_record(), cfg)

    assert rec["status"] == "timeout_uncertain"
    assert "Verify Spoolman inventory" in rec["error"]


def test_clean_use_rejection_remains_retryable_failure(monkeypatch, state):
    cfg = spoolman_config(enabled=True, dry_run=False, mappings={"1A": 12})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})

    def fail_use(*args, **kwargs):
        raise appmod.SpoolmanHttpError(400, "bad request")

    monkeypatch.setattr(appmod, "_spoolman_use_spool", fail_use)

    rec = appmod._spoolman_sync_record(state, "moonraker:job-123:1A", make_record(), cfg)

    assert rec["status"] == "failed"
    assert rec["error"] == "bad request"


def test_timeout_uncertain_record_is_not_ui_retryable(monkeypatch, state):
    key = "moonraker:job-123:1A"
    state.spoolman_sync_records[key] = make_record(status="timeout_uncertain")
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    with pytest.raises(HTTPException) as exc:
        appmod.api_ui_spoolman_retry(UiSpoolmanRetryRequest(record_key=key))

    assert exc.value.status_code == 409
    assert "not automatically retryable" in exc.value.detail


def test_live_sync_record_is_not_ui_retryable(monkeypatch, state):
    key = "moonraker:job-123:1A:live:1"
    rec = make_record(status="failed")
    rec["sync_phase"] = "live"
    state.spoolman_sync_records[key] = rec
    monkeypatch.setattr(appmod, "load_state", lambda: state)

    with pytest.raises(HTTPException) as exc:
        appmod.api_ui_spoolman_retry(UiSpoolmanRetryRequest(record_key=key))

    assert exc.value.status_code == 409
    assert "Live sync records" in exc.value.detail


def test_skipped_unmapped_retry_uses_current_mapping(monkeypatch, state):
    key = "moonraker:job-123:1A"
    state.spoolman_sync_records[key] = make_record(spool_id=None, status="skipped_unmapped")
    monkeypatch.setattr(appmod, "load_state", lambda: state)
    monkeypatch.setattr(appmod, "load_config", lambda: {"spoolman": spoolman_config(enabled=True, dry_run=False, mappings={"1A": 55})})
    monkeypatch.setattr(appmod, "_spoolman_get_spool", lambda spool_id, cfg=None: {"id": spool_id})
    monkeypatch.setattr(appmod, "_spoolman_use_spool", lambda spool_id, used_mm, cfg=None: {})

    appmod.api_ui_spoolman_retry(UiSpoolmanRetryRequest(record_key=key))

    rec = state.spoolman_sync_records[key]
    assert rec["status"] == "synced"
    assert rec["spool_id"] == 55
