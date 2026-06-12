# Testing

## Automated Unit Tests

The automated tests cover the Spoolman sync safety logic without contacting a
real printer or a real Spoolman server.

Install dev dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Run the tests:

```powershell
python -m pytest tests
```

## Local Environment Note

The project has been tested locally with Python 3.12.10 in `.venv`.

This workstation also has Python 3.14.5 installed, but the project pins
`pydantic==2.8.2`, whose matching `pydantic-core` release does not provide a
ready wheel for Python 3.14. Use Python 3.11 or 3.12 for local test execution,
or update the runtime dependency pins after validating compatibility on the
target printer.

Known passing command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

Last local result:

```text
14 passed
```

## Covered Cases

- Spoolman config normalization.
- Stable print key generation with Moonraker job id preference.
- Best-effort current job id extraction from Moonraker/Creality status.
- Unmapped slot handling with no network call.
- Dry-run record creation with no network call.
- Real sync path validates spool and sends one usage request.
- Already synced records do not send duplicate usage.
- Changed usage for an existing record becomes a conflict.
- Uncertain `/use` failures become `timeout_uncertain`.
- Clean HTTP 4xx `/use` rejections remain retryable failures.
- `timeout_uncertain` records are blocked from UI retry.
- `skipped_unmapped` records can be retried after mapping a spool id.
