# spoolman-cfs-sync Discovery

Date: 2026-06-10
Branch: spoolman-sync-prototype

## Scope

This document captures discovery for turning `jkef80/Filament-Management` into
`spoolman-cfs-sync`.

The goal is not to implement yet. The goal is to understand the existing app,
the CFS usage attribution problem, Spoolman/Moonraker integration behavior, and
the likely integration points so we can write a safer implementation plan next.

## Problem Statement

Before CFS, the print workflow selected one spool before printing. Moonraker's
Spoolman integration could deduct filament usage from that active spool.

With CFS, the printer can switch physical CFS slots during the print. If
Moonraker still sees only the spool selected at print start, all usage is
deducted from that one spool. This defeats Spoolman inventory tracking for
multi-material or multi-color CFS prints.

The missing behavior is slot-aware usage attribution:

- detect which CFS slot is active while extrusion increases
- map that CFS slot to a Spoolman spool id
- send usage for each slot to the correct Spoolman spool
- avoid double-accounting when the app restarts, polling repeats, or a job ends

## Current Repository Baseline

The cloned upstream project is a small FastAPI application with static frontend
assets.

Important files:

- `main.py`: backend, data files, Moonraker polling, CFS extraction, usage tracking, API endpoints
- `models/schemas.py`: Pydantic state and request schemas
- `static/app.js`: browser UI for CFS slots, local spool state, live usage, history assignment
- `static/index.html`, `static/app.css`, `static/style.css`: UI shell and styles
- `install.sh`, `update.sh`, `uninstall.sh`: Linux install/update/remove scripts
- `filament-management.service.example`: systemd service example

The app creates local runtime files under its own `data` directory:

- `data/state.json`
- `data/profiles.json`
- `data/config.json`

`data/config.json` currently supports:

- `moonraker_url`
- `poll_interval_sec`
- `filament_diameter_mm`
- `cfs_autosync`

No Spoolman settings exist yet.

## What The App Already Does Well

The upstream project is already close to the hardest part: CFS slot attribution.

Backend behavior:

- polls Moonraker when `moonraker_url` is configured
- discovers CFS-related Moonraker objects
- reads `print_stats`, `virtual_sdcard`, and CFS objects
- normalizes CFS slot ids as `1A` through `4D`
- tracks `cfs_active_slot`
- updates `active_slot` from printer-reported active CFS slot
- tracks in-flight print usage by deltas of total filament used
- assigns each delta to the current CFS slot
- stores per-slot usage in:
  - `job_track_slot_mm`
  - `job_track_slot_g`
  - `slot_history`
  - `spool_epoch_consumed_g_total`

Useful functions in `main.py`:

- `_ensure_data_files()`
- `_extract_cfs_slot_data(status)`
- `moonraker_poll_loop()`
- `_inc_slot_epoch_consumed(state, slot_id, delta_g)`
- `_hist_push(...)`
- `_hist_upsert_by_src(...)`
- `_moonraker_fetch_history(...)`
- `api_moonraker_allocate(...)`
- `api_ui_spool_set_start(...)`
- `api_ui_spool_set_remaining(...)`

The comments in `_apply_job_usage()` are important: the app intentionally does
not decrement local remaining weight through the older whole-job path because
CFS can switch slots mid-print. The per-slot tracker is treated as the source of
truth for accurate CFS accounting.

## Current Local Spool Model

`SlotState` in `models/schemas.py` stores local spool bookkeeping:

- `spool_ref_remaining_g`
- `spool_ref_consumed_g`
- `spool_ref_set_at`
- `spool_epoch`
- `spool_epoch_consumed_g_total`
- legacy `spool_start_g`
- legacy `remaining_g`

This design is local-only. It computes displayed remaining weight from:

```text
remaining = reference_remaining - (epoch_consumed - reference_consumed)
```

That is useful for a standalone tracker, but it does not update Spoolman.

## Current UI Behavior

`static/app.js` is a local dashboard.

It:

- polls `/api/ui/state`
- prefers `state.cfs_slots` for real CFS slot display
- merges local `state.slots` for local spool accounting fields
- shows active CFS slot
- shows live usage during printing from `job_track_slot_mm` / `job_track_slot_g`
- lets the user set local spool starting weight or measured remaining weight
- shows local per-slot history
- supports manual allocation of Moonraker history jobs to local CFS slots

It does not expose or sync Spoolman spool ids.

## What The App Does Not Do Yet

No current Spoolman behavior was found in source:

- no `spoolman` config keys
- no Spoolman API client
- no Moonraker spoolman endpoint calls
- no slot-to-Spoolman-spool mapping
- no Spoolman sync status
- no idempotency marker for remote Spoolman usage updates
- no UI to select or validate Spoolman spools

The app currently solves local CFS accounting, not Spoolman inventory updates.

## Moonraker And Spoolman Facts

Moonraker has a `[spoolman]` component. The Moonraker documentation says this
component enables integration with Spoolman and that Moonraker automatically
sends filament usage updates to the Spoolman database.

Moonraker requires a Spoolman server URL:

```ini
[spoolman]
server: http://192.168.0.123:7912
sync_rate: 5
```

Moonraker also registers a Klipper remote method named
`spoolman_set_active_spool`. Documentation shows macros that call this remote
method from Klipper, for example `SET_ACTIVE_SPOOL ID=1` and
`CLEAR_ACTIVE_SPOOL`.

Implication: Moonraker's native Spoolman path is centered on one active spool at
a time. This fits the pre-CFS workflow, but it does not by itself know that CFS
slot changes should switch the active Spoolman spool.

Source:

- https://moonraker.readthedocs.io/en/latest/configuration/#spoolman

Spoolman itself is a self-hosted filament inventory service. Its README states
that it integrates with Moonraker/Klipper and has a REST API for integration.

Source:

- https://github.com/Donkie/Spoolman

## Candidate Integration Approaches

These are discovery findings, not a final plan.

### Approach A: Switch Moonraker's active spool on CFS slot change

Concept:

- map each CFS slot to a Spoolman spool id
- whenever CFS active slot changes, set Moonraker/Spoolman active spool to the
  mapped id
- let Moonraker's existing Spoolman component deduct filament usage normally

Pros:

- uses Moonraker's native Spoolman integration
- minimal direct Spoolman write logic
- likely compatible with existing frontends

Cons:

- timing matters; if the active spool update lags behind extrusion, usage can be
  attributed to the previous spool
- must confirm whether active-spool changes can be triggered safely through
  HTTP/Moonraker from this app, or only through Klipper macros
- may still struggle with print segments that switch quickly
- depends on Moonraker's existing accounting behavior and sync timing

### Approach B: Directly update Spoolman from per-slot deltas

Concept:

- keep this app as the usage authority
- map each CFS slot to a Spoolman spool id
- when per-slot usage deltas are recorded, send usage directly to Spoolman

Pros:

- aligns with this app's existing per-slot delta tracking
- can account slot usage independent of Moonraker's single active spool
- can add explicit idempotency and retry tracking in local state

Cons:

- must verify exact Spoolman API endpoints and semantics
- higher risk of double-accounting unless carefully designed
- may conflict with Moonraker's native Spoolman deduction if both are enabled
- needs clear user guidance on whether to disable native Moonraker Spoolman
  accounting or use a safe mode

### Approach C: Post-print reconciliation only

Concept:

- track per-slot usage during print locally
- do not update Spoolman live
- after print completion, apply final per-slot usage to Spoolman

Pros:

- lower risk than live direct updates
- easier to make idempotent with one sync record per finished job and slot
- avoids timing races during tool switches

Cons:

- Spoolman is not live during the print
- failed/canceled prints need careful finalization logic
- if the app is not running at print end, reconciliation depends on persisted
  in-flight tracking

## Current Best Direction To Explore Next

The safest likely direction is a staged design:

1. Add Spoolman configuration and slot-to-spool mapping.
2. Add read-only Spoolman connectivity checks.
3. Add a dry-run sync log using the existing per-slot delta tracker.
4. Add post-print reconciliation first, with idempotency.
5. Consider live updates only after post-print accounting is proven.

This keeps the first printer test low-risk and lets us compare this app's local
per-slot totals against expected Spoolman changes before enabling writes.

## Key Risks

### Double accounting

If Moonraker's native `[spoolman]` integration is already deducting usage from
the initially selected spool, direct Spoolman updates from this app could deduct
additional usage unless we disable, bypass, or compensate for native behavior.

This is the central risk.

### Idempotency

Polling loops run repeatedly. The app can restart. Print completion can be seen
more than once. Any remote Spoolman write needs a durable local record of what
was already synced.

Possible state shape:

```json
{
  "spoolman_sync": {
    "<job-key>:<slot-id>": {
      "spool_id": 123,
      "used_g": 10.4,
      "used_mm": 3500,
      "synced_at": 1710000000,
      "status": "synced"
    }
  }
}
```

### Source of truth

The current app calculates grams using local material density profiles. Spoolman
also knows filament metadata. We need decide whether to sync grams, length, or
both depending on the Spoolman API contract.

### Slot identity

The app supports `1A` through `4D`, even if a K1-SE with one CFS may only expose
one four-slot box. The UI and backend should keep the broader model, but the
mapping UI should make connected slots obvious.

### Firmware object variability

`_extract_cfs_slot_data()` is intentionally heuristic because Creality firmware
objects are not standardized. Any sync logic should gracefully refuse to write
to Spoolman if no reliable active slot is known.

### Network and auth behavior

Spoolman may be reached directly or through Moonraker. We need decide:

- direct Spoolman URL
- Moonraker Spoolman API / active spool path
- authentication requirements, if any
- timeout/retry behavior

## Open Questions For The Implementation Plan

1. Is Moonraker's native `[spoolman]` currently enabled on the printer?
2. If yes, can or should it be disabled during testing to avoid double-accounting?
3. Do we want direct Spoolman writes, Moonraker active-spool switching, or both
   as configurable modes?
4. What endpoint should be used to update Spoolman usage safely?
5. Does Spoolman accept usage by grams, length, or remaining weight update?
6. Should sync happen live, post-print, or both?
7. How should the UI map CFS slots to Spoolman spools?
8. Should slot mappings live in `data/config.json` or `data/state.json`?
9. What should happen if a slot has no mapped Spoolman spool id?
10. What should happen if active CFS slot is unknown during an extrusion delta?
11. How will we test with a sample state/history file before touching a real
    Spoolman instance?

## Files Likely To Change Later

No implementation changes are made in this discovery step. Likely future files:

- `main.py`
  - Spoolman config loading
  - Spoolman client helpers
  - sync lifecycle and retry/idempotency
  - API endpoints for connectivity, mappings, and sync status
- `models/schemas.py`
  - Spoolman config/status/sync schemas
  - slot mapping fields
  - persisted sync records
- `static/app.js`
  - mapping UI
  - sync status UI
  - dry-run/sync controls
- `static/index.html`
  - UI containers for Spoolman section
- `static/app.css` / `static/style.css`
  - styles for Spoolman mapping/status controls
- `README.md`
  - rename and document setup
- `install.sh`
  - service naming, install path, and project rename if needed

## Suggested Test Fixtures Later

Create small fixtures before implementation reaches a real printer:

- app state with no CFS connection
- app state with one CFS box and no active slot
- app state with one active slot and a simple print delta
- app state with slot switch mid-print
- completed print with usage across two slots
- repeated completion event to test idempotency
- Spoolman unavailable
- slot mapped to invalid spool id
- slot unmapped

## Decision Boundary Before Development

Before coding, create an implementation plan that answers:

- direct Spoolman writes vs Moonraker active spool switching
- live sync vs post-print reconciliation
- how to prevent double-accounting
- exact persisted state shape
- exact UI scope for first version
- manual test sequence on the real K1-SE/CFS

