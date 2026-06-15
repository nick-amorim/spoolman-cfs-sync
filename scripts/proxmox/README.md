# Proxmox LXC Deployment

This folder contains a self-contained Proxmox LXC installer inspired by the
Community Scripts workflow: run one command on the Proxmox host, get a dedicated
container with the app installed as a systemd service, then update it with a
single helper command later.

It does not depend on the Community Scripts repository internals.

## Quick Install

Run this from the Proxmox host shell as `root`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh)
```

The installer shows the default settings and asks whether to use them. If you
answer `n`, it opens advanced prompts with the defaults already filled in.

The default install creates:

- Debian 12 unprivileged LXC
- the next available CTID
- 1 CPU core
- 512 MB RAM
- 512 MB swap
- 4 GB disk
- DHCP networking on `vmbr0`
- app listening on port `8005`
- systemd service named `spoolman-cfs-sync`
- update command inside the LXC: `update`

For a non-interactive install with the same defaults:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) --default
```

## Install With Initial URLs

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) \
  --ctid 120 \
  --hostname spoolman-cfs-sync \
  --storage local-lvm \
  --bridge vmbr0 \
  --moonraker-url http://PRINTER_IP:7125 \
  --spoolman-url http://SPOOLMAN_IP:7912
```

The installer writes those URLs into `data/config.json`, but Spoolman writes
still start disabled and dry-run by default. Open the UI, map slots, then enable
sync when ready.

## Advanced Mode

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) --advanced
```

Advanced mode prompts for common container settings.

## Static IP

Pass the Proxmox LXC network IP config directly:

```bash
--ip 192.168.1.50/24,gw=192.168.1.1
```

Full example:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) \
  --ctid 120 \
  --ip 192.168.1.50/24,gw=192.168.1.1
```

## Update

From inside the LXC:

```bash
update
```

The explicit app-specific command `spoolman-cfs-sync-update` is also installed
and does the same thing.

From the Proxmox host:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/nick-amorim/spoolman-cfs-sync/main/scripts/proxmox/install-lxc.sh) --update 120
```

`120` is the CTID of the existing LXC. Replace it with the container ID shown
after install. The host-side command enters that container and runs the same
in-container updater for you.

The updater:

1. fetches the configured branch from Git
2. resets the app code to `origin/<branch>`
3. updates Python dependencies
4. restarts the systemd service

Runtime files under `data/` are preserved.

## Installer Output

The installer keeps normal output concise:

- each major step prints an `[INFO]` line
- successful steps print an `[OK]` line
- detailed command output is written to a `/tmp/spoolman-cfs-sync-*.log` file
- if a step fails, the installer prints the last log lines automatically

Downloading the Debian template and installing system packages can take a few
minutes depending on the Proxmox host and network speed.

## Service Commands

Inside the LXC:

```bash
systemctl status spoolman-cfs-sync
journalctl -u spoolman-cfs-sync -f
systemctl restart spoolman-cfs-sync
update
```

## Installed Paths

| Path | Purpose |
| --- | --- |
| `/opt/spoolman-cfs-sync` | Application checkout and virtual environment |
| `/opt/spoolman-cfs-sync/data/config.json` | Runtime config |
| `/opt/spoolman-cfs-sync/data/state.json` | Runtime state |
| `/etc/systemd/system/spoolman-cfs-sync.service` | systemd service |
| `/etc/spoolman-cfs-sync.env` | Update helper settings |
| `/usr/local/bin/update` | Community-script-style updater |
| `/usr/local/bin/spoolman-cfs-sync-update` | In-container updater |

## Safety Notes

- The LXC should be always-on, so live sync is not interrupted by a workstation
  shutdown or crash.
- Keep Moonraker's native Spoolman integration disabled to avoid double
  accounting.
- Keep Spoolman writes disabled until every CFS slot is mapped correctly.
- Back up the LXC or at least `/opt/spoolman-cfs-sync/data/` before major
  changes.
