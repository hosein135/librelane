# LibreLane Web (notebook → Django)

Turns the [LibreLane Colab notebook](notebook.ipynb) into a Linux web app:

- **`devops/flake.nix`** — Nixpkgs 25.05 + LibreLane / nix-eda packages (pre-built via FOSSi cache)
- **`backend/`** — Django project (`librelane_web`, `flow` app, `manage.py`)
- **`frontend/`** — Templates and static assets

## Requirements

- **Linux** (or macOS) with [Nix](https://nixos.org/) 2.11+ (or let `./run.sh` install it)
- ~10 GB free disk (Nix store + sky130 PDK under `~/.ciel`)

## Quick start

```bash
cd /path/to/librelane
chmod +x run.sh dev_run.sh
./run.sh
```

Open **http://127.0.0.1:8000/**

On WSL with the repo on `/mnt/c`, use **`./dev_run.sh`** — it syncs to `~/.cache/librelane-dev-runs/workdir` and runs `./run.sh` there.

```bash
./dev_run.sh              # sync + watch + run
./dev_run.sh --prep-only  # Nix env only
./dev_run.sh --force-setup
```

### Web UI workflow

1. **Create run** — design `spm`, PDK `sky130A`, clock period `10`
2. **Setup PDK** — configures the flow (PDK is downloaded automatically on first `./run.sh`)
3. **Run full flow** — all notebook steps, or run steps individually

Data and logs: **`.librelane-data/`** in the project (or `LIBRELANE_DATA_DIR`)

## Manual Nix shell

```bash
nix develop devops --accept-flake-config
librelane-web
```

CLI:

```bash
librelane-manage migrate
librelane-manage run_flow <run_id>
```

Do **not** use system `python` — only `librelane-manage` / `librelane-web` from the Nix shell.

## Project layout

```
run.sh
dev_run.sh
devops/flake.nix
devops/web-shell.nix
backend/
frontend/templates/
frontend/static/
designs/spm.v
```

## Troubleshooting

**First run** — Nix downloads pre-built packages from `cache.nixos.org` and `nix-cache.fossi-foundation.org`. Then `./run.sh` downloads the sky130 PDK (~1 GB) into `~/.ciel` before starting the web server. Wait for `PDK ready.` in the terminal.

**Setup PDK failed in the web UI** — The PDK must finish downloading during `./run.sh` startup first. Restart with `./dev_run.sh`, wait for `PDK ready.`, then click Setup PDK again.

**Stale cache** — Run `./run.sh --force-setup` or delete `~/.cache/librelane-web` and retry.

**Substituter errors** — If Nix refuses FOSSi cache signatures, ensure `./run.sh` set `trusted-public-keys` (or add the FOSSi key from [LibreLane Nix docs](https://librelane.org/) to `~/.config/nix/nix.conf` and restart the daemon).

**Flakes** — `./run.sh` enables flakes and binary caches via `NIX_CONFIG`.
