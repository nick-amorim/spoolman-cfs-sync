# spoolman-cfs-sync

Creality CFS-aware filament tracking with direct Spoolman synchronization.

This project is based on
[`jkef80/Filament-Management`](https://github.com/jkef80/Filament-Management).
It keeps the original idea of local CFS slot accounting, then adds a
slot-aware Spoolman reconciliation layer for printers where one print can use
multiple CFS slots.

The main use case is a rooted Creality K-series printer with CFS, Moonraker, and
Spoolman.

## Why This Exists

Moonraker's native Spoolman integration tracks one active spool. That works well
for single-filament prints, but it becomes unreliable for CFS prints because the
CFS can switch slots during the job. If Moonraker only knows the spool selected
at the beginning, all usage can be deducted from the wrong Spoolman spool.

`spoolman-cfs-sync` solves that by:

- tracking print usage per CFS slot
- mapping each CFS slot to a Spoolman spool
- sending Spoolman usage updates per used slot, either after the print or during the print
- skipping slots that are not mapped
- preserving local history and safety records to avoid duplicate deductions

## Current Status

This is a working prototype validated against a rooted Creality K1-SE with CFS.

The implementation is intentionally conservative:

- sync can run post-print or in live chunks
- the app sends filament length only, not calculated weight
- Spoolman remains the source of truth for weight calculations
- timeout-uncertain writes are never retried automatically
- unmapped slots never send anything to Spoolman

## Features

- Moonraker polling for printer, job, virtual SD, and CFS state
- CFS slot display for connected boxes
- active CFS slot display
- local history by slot
- local spool weight reference tracking
- Spoolman URL configuration from the UI
- Spoolman connectivity test
- searchable Spoolman spool picker
- per-slot Spoolman spool mapping
- clear mapping button for empty CFS slots
- post-print or live Spoolman usage sync
- recent sync record history
- manual retry for safe retryable records
- debug mode for dry-run and local test-data cleanup controls
- warning when Moonraker's native Spoolman integration appears to be enabled
- tests for sync safety, parser behavior, and failure handling

## How It Works

During a print, the app reads the executed G-code stream using Moonraker's
`virtual_sdcard.file_position`. It parses positive extrusion moves and attributes
them to the currently selected tool or CFS slot.

Common Orca/Creality tool mapping:

```text
T0 -> 1A
T1 -> 1B
T2 -> 1C
T3 -> 1D
```

Direct slot-style tool commands such as `T1A` are also supported.

In post-print mode, the app creates one sync record per used CFS slot when the
print finishes. Each record is keyed by the print identity and slot id so the
same slot/job is not sent twice.

In live mode, the app sends usage in chunks while a print is running. The
default chunk threshold is `100 mm` of new filament per mapped slot. When the
print finishes, the app sends only the unsynced remainder for each slot.

For a mapped slot, the app calls Spoolman:

```http
PUT /api/v1/spool/{spool_id}/use
Content-Type: application/json

{
  "use_length": 2345.949
}
```

Only `use_length` is sent. Spoolman calculates weight from its own filament and
spool metadata.

## Safety Model

The sync layer is designed to avoid accidental inventory damage.

Important statuses:

| Status | Meaning |
| --- | --- |
| `dry_run` | Usage was recorded locally but not sent to Spoolman. |
| `synced` | Usage was successfully sent to Spoolman. |
| `skipped_unmapped` | The slot had no Spoolman spool mapping. No request was sent. |
| `skipped_invalid_spool` | The mapped Spoolman spool id did not validate. No usage request was sent. |
| `failed` | A clean retryable failure happened before the usage result became uncertain. |
| `timeout_uncertain` | The usage request result is unknown. Spoolman may already have deducted usage. |
| `conflict` | An existing record for the same print/slot has different usage. The app refuses to resend. |

`timeout_uncertain` is intentionally not retryable from the UI. Check Spoolman
inventory manually before deciding what to do.

## Requirements

- Python 3.11 or 3.12
- Moonraker reachable from the machine running this app
- Spoolman reachable from the machine running this app
- Rooted Creality K-series printer with Moonraker access
- CFS installed and visible through Moonraker/Creality objects

Python 3.12 is the recommended local development version for the current pinned
dependencies.

## Quick Start

Clone your fork or this repository:

```powershell
git clone https://github.com/nick-amorim/spoolman-cfs-sync.git
cd spoolman-cfs-sync
```

Create a virtual environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Start the app:

```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8005
```

Open:

```text
http://localhost:8005
```

If running on another machine, replace `localhost` with that machine's IP.

## Linux / Printer-Adjacent Start

Example manual setup on Linux:

```bash
git clone https://github.com/nick-amorim/spoolman-cfs-sync.git
cd spoolman-cfs-sync
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8005
```

Manual startup is useful for development or non-Proxmox Linux installs. For an
always-on deployment, prefer the Proxmox LXC helper below.

## Proxmox LXC Deployment

For an always-on install near your printer, use the Proxmox LXC helper script.
Run this from the Proxmox host shell as `root`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh)
```

The installer suggests the next available CTID and default resources. Accept the
default install or choose advanced mode to change CTID, memory, storage, network,
and related settings.

With initial printer and Spoolman URLs:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) \
  --moonraker-url http://PRINTER_IP:7125 \
  --spoolman-url http://SPOOLMAN_IP:7912
```

The installer creates a Debian LXC, installs the app as a systemd service, and
adds an update helper inside the container:

```bash
update
```

`spoolman-cfs-sync-update` is also available as the explicit app-specific
updater.

From the Proxmox host, update an existing container with:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) --update 120
```

`120` is the CTID of the existing LXC. Replace it with the container ID shown
after install.

See [scripts/proxmox/README.md](scripts/proxmox/README.md) for advanced options,
static IP configuration, service commands, and safety notes.

## First Configuration

The app creates `data/config.json` on first run. You can configure most settings
from the web UI.

Open **Settings** and set:

- Moonraker URL, for example `http://PRINTER_IP:7125`
- Poll interval, usually `5`
- Filament diameter, usually `1.75`
- whether to import CFS material, color, and name into local slots

Then use the **Spoolman Sync** panel:

1. Enter your Spoolman base URL, for example `http://SPOOLMAN_IP:7912`.
2. Click **Test**.
3. Enable sync.
4. For each CFS slot, click **Select Spool**.
5. Pick the matching Spoolman spool from the searchable list.
6. Use **Clear** if the CFS slot becomes empty or should not sync.

The app expects the base Spoolman URL only. It appends `/api/v1` internally.

## Example Config

```json
{
  "moonraker_url": "http://PRINTER_IP:7125",
  "poll_interval_sec": 5.0,
  "filament_diameter_mm": 1.75,
  "cfs_autosync": true,
  "spoolman": {
    "enabled": true,
    "dry_run": false,
    "url": "http://SPOOLMAN_IP:7912",
    "sync_mode": "live",
    "live_min_delta_mm": 100.0,
    "timeout_sec": 5.0,
    "slot_mappings": {
      "1A": 16,
      "1B": 7,
      "1C": 1,
      "1D": 10,
      "2A": null,
      "2B": null,
      "2C": null,
      "2D": null
    }
  }
}
```

Do not commit `data/config.json`. It is intentionally ignored by Git.

## UI Guide

### Box Cards

Shows connected CFS boxes and slots. Each slot displays:

- slot label
- material
- color
- local remaining weight, if a local reference was set
- active/ready/empty state

If the printer does not report an active CFS slot, the active slot panel shows
`No active slot` instead of falling back to `1A`.

### Active Slot

Shows the currently reported CFS active slot. During printing, it can show live
slot usage based on parsed executed G-code.

### History By Slot

Shows recent local usage entries per slot. This is local app history and is kept
separate from Spoolman inventory.

### Local Spool Weight

Click a slot to open the local spool editor:

- **Current weight (g)** updates the local remaining-weight reference.
- **New spool (g)** starts a new local spool epoch for that slot.

This is local display/accounting only. Spoolman inventory is changed only by the
Spoolman sync records.

### Spoolman Sync

Controls Spoolman integration:

- Spoolman URL
- enable sync
- sync mode: Post-print or Live
- live sync threshold in millimeters
- connection test
- slot-to-spool mappings
- recent sync records
- retry buttons for retryable records

Mapped rows display spool color, id, name, material, and remaining weight when
Spoolman details are available. Live sync records are informational and are not
manually retryable from the UI; later live chunks or the final record handle
reconciliation.

### Debug Mode

Open **Settings** and enable **Debug mode** to reveal:

- Dry-run toggle
- Clear Test Data button

Normal use should keep debug mode off.

## Native Moonraker Spoolman Warning

If Moonraker's native Spoolman integration has an active spool selected at the
same time as this app, filament can be deducted twice:

1. Moonraker deducts from its single active spool.
2. `spoolman-cfs-sync` deducts from each mapped CFS slot.

The app shows a warning only when Moonraker reports an active native Spoolman
spool. If the component is installed but no spool is selected in Moonraker, no
warning is shown.

## Testing

Install dev dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Run tests:

```powershell
python -m pytest tests
```

Run Python compile checks:

```powershell
python -m py_compile main.py models\schemas.py
```

Known local passing result:

```text
38 passed
```

GitHub Actions also runs the test suite on pull requests and pushes.

## Development Workflow

Recommended flow:

```powershell
git switch -c feature/my-change origin/main
# make changes
python -m pytest tests
git add .
git commit -m "Describe the change"
git push -u origin feature/my-change
```

Open a pull request into your fork's `main`. The repository is configured for:

- squash merge only
- protected `main`
- required `pytest` check
- force-push and deletion protection on `main`

## Important Files

| Path | Purpose |
| --- | --- |
| `main.py` | FastAPI app, Moonraker polling, CFS tracking, Spoolman sync logic. |
| `models/schemas.py` | Pydantic request/state models. |
| `static/index.html` | Main UI shell. |
| `static/app.js` | Browser UI behavior. |
| `static/style.css` | UI styling. |
| `scripts/proxmox/` | Proxmox LXC installer and update helper scripts. |
| `data/config.json` | Local runtime config, ignored by Git. |
| `data/state.json` | Local runtime state, ignored by Git. |
| `tests/test_spoolman_sync.py` | Regression tests for sync and parser behavior. |
| `.github/workflows/tests.yml` | CI workflow. |

## Current Limitations

- No automatic spool creation in Spoolman.
- No automatic spool matching by color/material/name.
- Live sync is chunked by filament length threshold, not every extrusion move.
- No automatic compensation for Moonraker native Spoolman deductions.

## Credits

Based on
[`jkef80/Filament-Management`](https://github.com/jkef80/Filament-Management).

Spoolman:

- https://github.com/Donkie/Spoolman
- https://donkie.github.io/Spoolman/
