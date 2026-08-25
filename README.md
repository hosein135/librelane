# LibreLane Web (notebook → Django API + Next.js)

Turns the [LibreLane Colab notebook](notebook.ipynb) into a Linux web app:

- **`devops/flake.nix`** — Nixpkgs 25.05 + LibreLane / nix-eda + PostgreSQL + Node.js
- **`backend/`** — Django JSON API (`librelane_web`, `flow` app) with cookie auth
- **`frontend/`** — Next.js 15 App Router (TypeScript)
- **`database/`** — PostgreSQL schema (`ensure_db.sql`, `ensure_schema.sql`, `setup.sql`)

## Requirements

- **Linux** (or macOS) with [Nix](https://nixos.org/) 2.11+ (or let `./run.sh` install it)
- ~10 GB free disk (Nix store + sky130 PDK under `~/.ciel`)

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
database/           # PostgreSQL SQL (canonical schema)
devops/flake.nix
devops/web-shell.nix
backend/            # Django API
frontend/           # Next.js TypeScript UI
```

## Troubleshooting

**First run** — Nix downloads pre-built packages from `cache.nixos.org` and `nix-cache.fossi-foundation.org`. Then `./run.sh` starts project Postgres, applies `database/*.sql`, downloads the sky130 PDK (~1 GB) into `~/.ciel`, and starts Django + Next.js. Wait for `PDK ready.` in the terminal.

**Setup PDK failed in the web UI** — The PDK must finish downloading during `./run.sh` startup first. Restart with `./dev_run.sh`, wait for `PDK ready.`, then click Setup PDK again.

**Stale cache** — Run `./run.sh --force-setup` or delete `~/.cache/librelane-web` and retry.

**Reset database** — as Postgres superuser: `psql -f database/setup.sql` (destructive wipe).
