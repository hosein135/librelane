# LibreLane Web (notebook → Django API + Next.js)

Turns the [LibreLane Colab notebook](notebook.ipynb) into a Linux web app:

- **`devops/flake.nix`** — Nixpkgs 25.05 + LibreLane / nix-eda + PostgreSQL + Node.js
- **`backend/`** — Django JSON API (`librelane_web`, `flow` app) with cookie auth
- **`frontend/`** — Next.js 15 App Router (TypeScript)
- **`database/`** — PostgreSQL schema (`ensure_db.sql`, `ensure_schema.sql`, `setup.sql`)

## Requirements

- **Linux or macOS** (WSL is fine). `./run.sh` requires Nix **2.28.5**, the package manager shipped with NixOS 25.05. Any other Nix (older or newer, including a NixOS release's Nix) is replaced with that release. On NixOS itself, `/nix` is kept and 2.28.5 is installed into your user profile. Missing `git` and `curl` are installed with the distro package manager (Homebrew on macOS). If curl still cannot be installed, a static curl is downloaded. `sudo ./run.sh` continues as the user who invoked sudo.
- **Free disk** on first run: about 12 GB when the Nix store and PDKs share a disk (`LIBRELANE_INSTALL_FREE_BYTES`). PDKs on their own disk need about 4 GB (`LIBRELANE_PDK_FREE_BYTES`). Later runs keep at least 5 GB free for flow data (`LIBRELANE_MIN_FREE_BYTES`, the same floor the app enforces). PDKs land under `~/.ciel`.

## Quick start

```bash
cd /path/to/librelane
chmod +x run.sh dev_run.sh
./run.sh
```

Open **http://127.0.0.1:3000/** (Next.js UI). Django API listens on **:8000**.

On WSL with the repo on `/mnt/c`, use **`./dev_run.sh`** — it syncs to `~/.cache/librelane-dev-runs/workdir` and runs `./run.sh` there.

```bash
./dev_run.sh              # sync + watch + run
./dev_run.sh --prep-only  # Nix env only
./dev_run.sh --force-setup
./run.sh --build          # production Next.js build
```

### Web UI workflow

1. **Sign up / Sign in**
2. **Create run** — upload one or more `.v` / `.sv` files, confirm the detected top module (or pick manually), choose PDK / clock
3. **Setup PDK** — configures the flow (PDK is downloaded automatically on first `./run.sh`)
4. **Run full flow** — all notebook steps, or run steps individually

While a run is executing (`setting_up` / `running`), the same user cannot start another run.
When a flow finishes (completed or failed), artifacts are stored in Postgres and the long-named temporary folder under the OS temp dir (`…/librelane_runs/{username}_{runname}_{YYYYMMDD_HHMMSS}/`) is removed. Deleting a run also removes that temp folder (and DB rows / stored files).

Data: **`.librelane-data/`** (Postgres + logs) or `LIBRELANE_DATA_DIR`.

## Manual Nix shell

```bash
nix develop devops --accept-flake-config
# start Postgres + apply database/ensure_*.sql (prefer ./run.sh)
librelane-web   # Django API only
# in another terminal: cd frontend && npm install && npm run dev
```

Do **not** use system `python` — only `librelane-manage` / `librelane-web` from the Nix shell.

## Project layout

```
run.sh
dev_run.sh
scripts/host-prep.sh  # git, curl, and free-disk preflight
database/             # PostgreSQL SQL (canonical schema)
devops/flake.nix
devops/web-shell.nix
backend/              # Django API
frontend/             # Next.js TypeScript UI
```

## Troubleshooting

**First run** — Nix downloads pre-built packages from `cache.nixos.org` and `nix-cache.fossi-foundation.org`. Then `./run.sh` starts project Postgres, applies `database/*.sql`, downloads sky130, gf180mcu, and ihp-sg13g2 into `~/.ciel`, and starts Django + Next.js. Wait for `PDKs ready.` in the terminal.

**Setup PDK failed in the web UI** — The PDK must finish downloading during `./run.sh` startup first. Restart with `./dev_run.sh`, wait for `PDK ready.`, then click Setup PDK again.

**Stale cache** — Run `./run.sh --force-setup` or delete `~/.cache/librelane-web` and retry.

**Reset database** — as Postgres superuser: `psql -f database/setup.sql` (destructive wipe).
