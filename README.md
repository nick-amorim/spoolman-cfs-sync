# spoolman-cfs-sync

Creality CFS slot tracking with direct Spoolman post-print reconciliation.

This project is based on `jkef80/Filament-Management` and keeps its local CFS
slot accounting, then adds a conservative Spoolman sync layer.

## What It Does

- Tracks CFS slot usage through Moonraker polling.
- Attributes print usage to CFS slots such as `1A`, `1B`, `1C`, and `1D`.
- Maps each CFS slot to an optional Spoolman spool id.
- Reconciles usage to Spoolman after a print finishes.
- Sends only `use_length` to Spoolman so Spoolman remains the source of truth
  for filament weight calculations.
- Skips Spoolman sync for unmapped or unidentified slots.

## Safety Defaults

Spoolman sync is conservative by default:

- `dry_run` defaults to `true`.
- Real writes require `enabled=true` and `dry_run=false`.
- Unmapped slots are recorded as `skipped_unmapped` and no Spoolman request is
  sent.
- If a Spoolman usage request times out, the record becomes
  `timeout_uncertain` and is not automatically retried.
- The app warns when Moonraker's native Spoolman integration appears active,
  because native Moonraker usage deduction plus this app can double-account.

## Configuration

The app creates `data/config.json` on first run.

Example Spoolman section:

```json
{
  "spoolman": {
    "enabled": false,
    "dry_run": true,
    "url": "http://192.168.1.50:7912",
    "sync_mode": "post_print",
    "timeout_sec": 5,
    "slot_mappings": {
      "1A": 12,
      "1B": null,
      "1C": null,
      "1D": null
    }
  }
}
```

You can also edit Spoolman URL, dry-run/write mode, and slot mappings from the
web UI.

## Spoolman API

The app uses:

```http
POST /api/v1/spool/{spool_id}/use
Content-Type: application/json

{
  "use_length": 1234.5
}
```

Spoolman docs:

- https://donkie.github.io/Spoolman/
- https://github.com/Donkie/Spoolman

## Development Notes

Planning docs are in:

- `docs/discovery/spoolman-cfs-sync-discovery.md`
- `docs/plans/spoolman-cfs-sync-implementation-plan.md`

