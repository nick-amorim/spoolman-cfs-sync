# spoolman-cfs-sync Implementation Plan

Date: 2026-06-10
Branch: spoolman-sync-prototype
Status: planning only

## Decisions

We will implement direct Spoolman synchronization from this application.

The application will not depend on Moonraker's single active Spoolman spool for
CFS accounting. It will use the app's existing per-slot CFS tracking as the
source of truth, then send usage to Spoolman for each mapped slot.

Initial sync mode will be post-print reconciliation with dry-run enabled by
default.

Live sync can be considered later, after post-print reconciliation is proven
against real printer output.

## Review Resolution Notes

These developer notes capture the implementation-plan review findings and how
the plan incorporates them.

### Developer Note 1: Spoolman `/use` is not idempotent

`POST /api/v1/spool/{spool_id}/use` applies a usage delta. If Spoolman applies
the request but the client times out before receiving the response, retrying can
double-deduct the spool.

Plan correction:

- add `timeout_uncertain` as a sync status
- never auto-retry `timeout_uncertain`
- distinguish timeout/unknown-result failures from clean rejected failures
- require user verification in Spoolman before manual retry
- surface this clearly in the UI

### Developer Note 2: Moonraker native Spoolman conflict

Moonraker's native Spoolman integration may still deduct usage from its active
spool while this app deducts per CFS slot. Documentation alone is not enough to
prevent accidental double-accounting.

Plan correction:

- add best-effort detection for Moonraker's native Spoolman component
- show a persistent warning when detected
- do not silently compensate for Moonraker deductions
- do not automatically run `CLEAR_ACTIVE_SPOOL` in v1, because that is a
  printer-control side effect

### Developer Note 3: Stable print identity

The sync record key must identify a print instance, not just a filename.

Plan correction:

- define a stable print key explicitly
- prefer Moonraker `job_id` when available
- otherwise combine filename, print start timestamp, and print end timestamp
- append the CFS slot id to create the per-slot sync record key

### Developer Note 4: Send length only in v1

Spoolman accepts both length and weight, but the app's weight is derived from
local density settings and can differ from Spoolman's filament metadata.

Plan correction:

- send only `use_length` in v1
- keep local `used_g` for display, history, and diagnostics
- let Spoolman calculate weight from its own spool/filament metadata

### Developer Note 5: Idempotency belongs inside sync execution

Idempotency must not be a later layer bolted onto real writes.

Plan correction:

- all dry-run and real sync paths pass through the same sync-record gate
- no real write path exists without idempotency checks
- implementation order is updated accordingly

## API Basis

Spoolman serves its REST API under `/api/v1/`.

Relevant endpoints from the official API docs:

- `GET /api/v1/health`
- `GET /api/v1/info`
- `GET /api/v1/spool`
- `GET /api/v1/spool/{spool_id}`
- `POST /api/v1/spool/{spool_id}/use`

The usage endpoint accepts:

```json
{
  "use_length": 123.4,
  "use_weight": 5.6
}
```

Where:

- `use_length` is filament length to reduce by, in mm
- `use_weight` is filament weight to reduce by, in g

Sources:

- https://donkie.github.io/Spoolman/
- https://github.com/Donkie/Spoolman

## Safety Goals

The first implementation must be conservative.

It must:

- never send anything to Spoolman when dry-run is enabled
- never sync a slot that has no mapped Spoolman spool id
- never sync a slot whose mapped spool id cannot be validated
- never guess a Spoolman spool from material, color, or name
- never fail the whole app because Spoolman is offline
- never double-sync the same finished job and slot
- never automatically retry a timeout-uncertain Spoolman usage request
- actively warn if Moonraker native Spoolman integration is detected
- preserve existing local-only tracking behavior

## Graceful Skip Scenario

Important scenario:

A physical spool is loaded in CFS, but that spool is not in Spoolman or has not
been identified/mapped in this application.

Expected behavior:

- continue local CFS tracking
- continue local per-slot history
- do not send anything to Spoolman
- mark the sync record as `skipped_unmapped`
- show a warning/status in the UI
- allow the user to map the slot later

No automatic matching should happen in v1. Similar color/material/name may be a
future convenience, but only as a suggestion with explicit user confirmation.

## Proposed Configuration

Add Spoolman settings to `data/config.json`.

Example:

```json
{
  "moonraker_url": "http://192.168.1.50:7125",
  "poll_interval_sec": 5,
  "filament_diameter_mm": 1.75,
  "cfs_autosync": false,
  "spoolman": {
    "enabled": false,
    "dry_run": true,
    "url": "http://192.168.1.50:7912",
    "sync_mode": "post_print",
    "timeout_sec": 5,
    "slot_mappings": {
      "1A": null,
      "1B": null,
      "1C": null,
      "1D": null
    }
  }
}
```

Notes:

- `enabled=false` by default
- `dry_run=true` by default
- `slot_mappings` maps CFS slot ids to Spoolman spool ids
- `null`, missing, empty string, or invalid id means unmapped
- first implementation can support all `1A` through `4D`, while UI can focus on
  connected slots

## Proposed State Additions

Add sync status to persisted `state.json`.

Example:

```json
{
  "spoolman_status": {
    "connected": false,
    "last_check_at": 0,
    "last_error": "",
    "dry_run": true,
    "moonraker_native_detected": false,
    "moonraker_native_warning": ""
  },
  "spoolman_sync_records": {
    "print-key:1A": {
      "job_key": "print-key",
      "job": "part.gcode",
      "slot": "1A",
      "spool_id": 123,
      "used_mm": 3500,
      "used_g": 10.4,
      "status": "dry_run",
      "attempts": 1,
      "last_attempt_at": 1710000000,
      "synced_at": null,
      "error": ""
    }
  }
}
```

Suggested statuses:

- `pending`
- `dry_run`
- `synced`
- `skipped_unmapped`
- `skipped_invalid_spool`
- `failed`
- `timeout_uncertain`
- `conflict`

The sync key must include enough identity to avoid collisions between jobs. The
stable print key should be:

1. Moonraker `job_id`, if available.
2. Otherwise, a composite of filename, print start timestamp, and print end
   timestamp.
3. If only local tracking data is available, use filename plus
   `job_track_started_at` plus the finalization timestamp.

The per-slot sync record key is:

```text
<stable-print-key>:<slot-id>
```

## Backend Implementation Steps

### 1. Config helpers

Add helper functions in `main.py`:

- load normalized Spoolman config
- normalize URL
- normalize slot mappings
- check whether dry-run is active
- determine if a slot is mapped

Keep old configs compatible. Missing `spoolman` config should behave as
disabled.

### 2. Spoolman HTTP client helpers

Add small helper functions using the existing standard-library HTTP style:

- `_spoolman_get_json(path)`
- `_spoolman_post_json(path, payload)`
- `_spoolman_health()`
- `_spoolman_get_spool(spool_id)`
- `_spoolman_use_spool(spool_id, used_mm)`

Do not add a new dependency unless the current standard-library approach becomes
too awkward.

### 3. Spool validation

Before syncing a mapped slot:

- call `GET /api/v1/spool/{spool_id}`
- if 200, proceed
- if 404 or invalid, mark `skipped_invalid_spool`
- if connection fails, mark `failed` with retryable error

Validation may be cached for a short period later, but v1 can be simple.

### 4. Moonraker native Spoolman detection

Add a best-effort check for Moonraker's native Spoolman component.

Possible sources:

- Moonraker component/object list, if exposed
- Moonraker server/config endpoints, if accessible
- known Moonraker spoolman endpoints, if present

Behavior:

- if native Spoolman is detected, set
  `spoolman_status.moonraker_native_detected=true`
- show a persistent UI warning
- do not auto-clear Moonraker's active spool in v1
- do not block dry-run
- require explicit user awareness before real sync is enabled

### 5. Finished job detection

Hook into the existing print-finalization block in `moonraker_poll_loop()`.

Current behavior already:

- detects printing state from `print_stats`
- tracks slot deltas during printing
- finalizes per-slot history when printing ends
- resets in-flight tracking after finalization

Before resetting `job_track_slot_mm` and `job_track_slot_g`, call a new sync
planner that creates or updates sync records for each slot used by the finished
job.

### 6. Sync planner

Create a function like:

```python
def _plan_spoolman_sync_for_finished_job(state, job_name, start_ts, end_ts, result):
    ...
```

Responsibilities:

- compute the stable print key
- read final per-slot usage from `job_track_slot_mm` and `job_track_slot_g`
- create one record per slot with positive usage
- if slot has no mapping, record `skipped_unmapped`
- if Spoolman disabled, do nothing or record local-only status
- if dry-run, record `dry_run`
- if real sync enabled, validate and sync
- save durable sync records before/after attempting remote writes
- route all dry-run and real sync through the same idempotency gate

### 7. Remote sync

For real writes:

```http
POST /api/v1/spool/{spool_id}/use
Content-Type: application/json

{
  "use_length": <used_mm>
}
```

Send only `use_length` in v1. The app should keep `used_g` for local history,
display, and diagnostics, but Spoolman should derive weight from its own
filament/spool metadata.

### 8. Idempotency and uncertain timeouts

Before calling Spoolman:

- compute the sync record key
- if existing record status is `synced`, do not send again
- if existing record status is `dry_run` and dry-run is still enabled, do not
  duplicate logs
- if existing record status is `failed`, allow retry for clean failures
- if existing record status is `timeout_uncertain`, do not automatically retry
- if usage values changed for the same key, mark as conflict instead of blindly
  resending

This is the most important correctness guard.

Timeout behavior:

- if the client times out while calling `/use`, mark the record
  `timeout_uncertain`
- do not issue another `/use` request automatically
- require the user to inspect Spoolman and decide whether a manual retry is
  appropriate
- show `timeout_uncertain` separately from ordinary `failed` records in the UI

### 9. API endpoints for UI

Add UI-safe endpoints:

- `GET /api/ui/spoolman/status`
- `POST /api/ui/spoolman/test`
- `POST /api/ui/spoolman/config`
- `POST /api/ui/spoolman/mapping`
- `POST /api/ui/spoolman/retry`

The exact endpoint names can be adjusted during implementation, but the UI needs
ways to:

- test Spoolman connectivity
- view dry-run/enabled status
- view Moonraker native Spoolman warning state
- map slots to Spoolman spool ids
- see skipped/failed/synced records
- retry clean failed records
- handle `timeout_uncertain` records with explicit warning text

## UI Implementation Steps

### 1. Add a Spoolman panel

Add a compact panel to the existing UI showing:

- Spoolman connection state
- enabled/disabled
- dry-run/real sync
- configured Spoolman URL
- latest error
- Moonraker native Spoolman warning, if detected

### 2. Add slot mappings

For each connected slot:

- show CFS slot id
- show material/color from CFS
- show mapped Spoolman spool id
- allow manual edit of spool id
- show mapping state:
  - mapped
  - unmapped
  - invalid
  - archived, if available from Spoolman response later

First version can use numeric spool id inputs. A searchable picker can come
later.

### 3. Show sync records

Show recent sync records:

- job
- slot
- spool id
- used mm
- used g
- status
- error
- timestamp

Skipped unmapped records should be visible but calm. They are expected when a
spool is not in Spoolman or not identified.

`timeout_uncertain` records should be visually distinct from normal failures and
must explain that Spoolman may already have deducted the usage.

### 4. Dry-run visibility

Dry-run must be obvious. The UI should make it hard to mistake dry-run for real
inventory updates.

## Testing Plan

### Local unit-style tests

Add tests or fixtures for pure functions where practical:

- config normalization
- mapping lookup
- sync key generation
- dry-run record creation
- unmapped slot skip
- invalid spool skip
- idempotent no-op for already synced records
- timeout-uncertain no-auto-retry behavior
- stable print key generation

### Manual local fixture tests

Use synthetic `state.json` data:

1. finished job with one mapped slot
2. finished job with two mapped slots
3. finished job with one mapped and one unmapped slot
4. finished job with no active CFS slot
5. repeated print completion event
6. Spoolman unavailable
7. invalid spool id
8. usage request timeout after submit
9. Moonraker native Spoolman detected

### Real printer dry-run test

1. Configure Spoolman URL.
2. Leave `enabled=false` or `dry_run=true`.
3. Map one or more CFS slots.
4. Run a small CFS print.
5. Confirm local per-slot usage.
6. Confirm dry-run records match expected Spoolman writes.
7. Confirm no Spoolman inventory changed.

### Real Spoolman write test

Only after dry-run is verified:

1. Make a backup or note current Spoolman spool values.
2. Disable/avoid Moonraker native Spoolman active-spool deduction for this test.
3. Set `enabled=true`, `dry_run=false`.
4. Run a tiny print with a mapped slot.
5. Confirm one Spoolman spool changed by expected amount.
6. Repeat with a multi-slot CFS print.
7. Confirm unmapped slot is skipped and does not block mapped slot sync.

## Moonraker Native Spoolman Caution

Direct sync can conflict with Moonraker's native Spoolman integration if
Moonraker is still deducting from the initially selected active spool.

The implementation should document this clearly and detect it where possible.
During testing, avoid native Moonraker deduction or ensure no active spool is
selected there.

This app should not try to silently compensate for Moonraker's independent
deductions in v1. It should also not automatically send `CLEAR_ACTIVE_SPOOL` in
v1.

When detected, the UI should show a persistent warning:

```text
Moonraker Spoolman integration detected. This may cause double-accounting if
Moonraker and spoolman-cfs-sync both deduct filament usage.
```

## Failure Behavior

Spoolman offline:

- local tracking continues
- sync records become `failed`
- user can retry later

Spoolman usage request timeout:

- local tracking continues
- sync record becomes `timeout_uncertain`
- no automatic retry is allowed
- UI tells the user to verify Spoolman inventory before manual retry

Unmapped slot:

- local tracking continues
- record becomes `skipped_unmapped`
- no network request is sent

Invalid spool id:

- local tracking continues
- record becomes `skipped_invalid_spool`
- no usage request is sent

Partial multi-slot failure:

- successful mapped slots can sync
- failed or skipped slots retain their own status
- one bad slot must not block other slots

Repeated completion event:

- already synced records are not sent again

App restart:

- persisted sync records prevent duplicate writes
- in-flight tracking should continue to use existing persisted job tracking

## Files Expected To Change

Implementation will likely touch:

- `main.py`
- `models/schemas.py`
- `static/app.js`
- `static/index.html`
- `static/app.css`
- `static/style.css`
- `README.md`

Potential later changes:

- `install.sh`
- `update.sh`
- `uninstall.sh`
- service name/template

## Implementation Order

1. Add config/state schema support.
2. Add Spoolman client helpers.
3. Add stable print key generation.
4. Add sync-record gate with idempotency and timeout-uncertain statuses.
5. Add dry-run sync record creation for finished jobs through that gate.
6. Add Moonraker native Spoolman detection and warning state.
7. Add basic UI status, warning, and mapping controls.
8. Add connectivity test endpoint.
9. Add real Spoolman write path behind `enabled=true` and `dry_run=false`,
   using the same idempotency gate.
10. Add retry path for clean failed records; keep timeout-uncertain manual only.
11. Update README with setup and safety notes.
12. Run local syntax/test checks.
13. Perform dry-run printer test.
14. Perform real write test only after dry-run approval.

## Out Of Scope For First Implementation

- automatic spool matching by material/color/name
- automatic creation of missing Spoolman spools
- live per-extrusion Spoolman writes
- Moonraker active-spool switching
- compensating for Moonraker native Spoolman deductions
- multi-user permissions or authentication flows beyond URL access

## Ready Criteria Before Coding

Development can start when we accept:

- direct Spoolman API sync
- post-print reconciliation first
- dry-run default
- manual slot-to-spool-id mapping
- unmapped/unidentified slots are skipped without remote writes
- idempotency is required before any real sync write
- timeout-uncertain records are never retried automatically
- Moonraker native Spoolman detection produces a persistent warning
- v1 sends only `use_length` to Spoolman
