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


def spoolman_config(*, enabled=False, dry_run=True, mappings=None):
    cfg = appmod._default_spoolman_config()
    cfg.update(
        {
            "enabled": enabled,
            "dry_run": dry_run,
            "url": "http://spoolman.local:7912",
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
            "slot_mappings": {"1a": "42", "1B": "", "9Z": 100},
        }
    }
    normalized = appmod._normalize_spoolman_config(file_shape)

    assert normalized["enabled"] is True
    assert normalized["dry_run"] is False
    assert normalized["url"] == "http://spoolman.local:7912"
    assert normalized["timeout_sec"] == 30.0
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


def test_moonraker_build_url_encodes_object_names_with_spaces():
    url = appmod._moonraker_build_url(
        "http://printer.local:7125/",
        ["print_stats", "gcode_macro SET_ACTIVE_SPOOL"],
    )

    assert url == "http://printer.local:7125/printer/objects/query?print_stats&gcode_macro%20SET_ACTIVE_SPOOL"


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
