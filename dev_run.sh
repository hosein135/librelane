#!/usr/bin/env bash
# =============================================================================
# dev_run.sh — Run LibreLane from a Linux-native persistent work copy (WSL-friendly)
#
# Syncs this repo off /mnt/c (or any Windows mount) into a reusable directory
# under ~/.cache/librelane-dev-runs/workdir, then runs ./run.sh there.
#
# While running, watches the source tree and live-syncs file changes into the
# workdir (polling — reliable on /mnt/c where inotify often does not work).
# Heavy dirs (node_modules, .next, Postgres data, …) are preserved.
#
# Usage:
#   ./dev_run.sh                 # sync + watch + ./run.sh
#   ./dev_run.sh --prep-only
#   ./dev_run.sh --force-setup
#   ./dev_run.sh --clean-workdir  # wipe workdir once, then sync + run
#   ./dev_run.sh --no-watch       # disable live change sync
#   ./dev_run.sh --help
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[librelane-dev]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[librelane-dev]${NC}  $*"; }
error() { echo -e "${RED}[librelane-dev]${NC} $*" >&2; }
step()  { echo -e "${CYAN}[librelane-dev]${NC}  $*"; }

sanitize_shell_file() {
    local f="$1"
    [ -f "${f}" ] || return 0
    if ! grep -q $'\r' "${f}" 2>/dev/null; then
        return 0
    fi
    warn "Fixing Windows (CRLF) line endings in $(basename "${f}") ..."
    local tmp="${f}.lf.$$"
    tr -d '\r' < "${f}" > "${tmp}" && mv -f "${tmp}" "${f}"
}

sanitize_shell_file "${BASH_SOURCE[0]}"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR=""
CHILD_PID=""
WATCHER_PID=""
CLEANING_UP=false
CLEAN_WORKDIR=false
ENABLE_WATCH=true
# Poll interval (seconds). /mnt/c has no reliable inotify.
SYNC_INTERVAL="${LIBRELANE_DEV_SYNC_INTERVAL:-1}"
RUN_ARGS=()

is_windows_mount() {
    case "$1" in
        /mnt/[a-zA-Z]/*|/mnt/[a-zA-Z]) return 0 ;;
        *) return 1 ;;
    esac
}

pick_linux_cache_root() {
    local cand resolved
    for cand in \
        "${XDG_CACHE_HOME:-${HOME}/.cache}/librelane-dev-runs" \
        "${HOME}/.cache/librelane-dev-runs" \
        "/tmp/librelane-dev-runs-${USER:-user}"
    do
        mkdir -p "${cand}" 2>/dev/null || continue
        resolved="$(cd "${cand}" && pwd -P 2>/dev/null || echo "${cand}")"
        if is_windows_mount "${resolved}"; then
            continue
        fi
        echo "${resolved}"
        return 0
    done
    return 1
}

# Dirs/files kept in the work copy across runs (not wiped on stop; not overwritten by sync).
# Paths starting with / are anchored to the repo root (so frontend/src/app/runs/ still syncs).
RSYNC_PRESERVE_EXCLUDES=(
    --exclude '.git/'
    --exclude 'vendor/'
    --exclude '/runs/'
    --exclude 'librelane_run/'
    --exclude '.librelane-data/'
    --exclude 'result/'
    --exclude '.librelane-nix-ready'
    --exclude '.librelane-bootstrap/'
    --exclude '__pycache__/'
    --exclude '*.py[cod]'
    --exclude '.direnv/'
    --exclude '.result/'
    --exclude '.result-*/'
    --exclude '.DS_Store'
    --exclude 'frontend/node_modules/'
    --exclude 'frontend/.next/'
    --exclude 'node_modules/'
    --exclude '.next/'
)

wipe_workdir() {
    if [ -z "${WORK_DIR}" ] || [ ! -d "${WORK_DIR}" ]; then
        return 0
    fi
    step "Wiping workdir ${WORK_DIR} (--clean-workdir) ..."
    rm -rf "${WORK_DIR}" 2>/dev/null || {
        sleep 0.5
        rm -rf "${WORK_DIR}" 2>/dev/null || {
            error "Could not wipe ${WORK_DIR}"
            return 1
        }
    }
    mkdir -p "${WORK_DIR}"
}

stop_watcher() {
    if [ -n "${WATCHER_PID}" ] && kill -0 "${WATCHER_PID}" 2>/dev/null; then
        kill "${WATCHER_PID}" 2>/dev/null || true
        wait "${WATCHER_PID}" 2>/dev/null || true
    fi
    WATCHER_PID=""
}

forward_signal_to_child() {
    stop_watcher
    if [ -n "${CHILD_PID}" ] && kill -0 "${CHILD_PID}" 2>/dev/null; then
        kill -INT "${CHILD_PID}" 2>/dev/null || kill -TERM "${CHILD_PID}" 2>/dev/null || true
        while kill -0 "${CHILD_PID}" 2>/dev/null; do
            wait "${CHILD_PID}" 2>/dev/null || true
        done
    fi
    CHILD_PID=""
}

on_interrupt() {
    if [ "${CLEANING_UP}" = true ]; then
        return 0
    fi
    CLEANING_UP=true
    trap - INT TERM EXIT
    info "Ctrl+C — stopping watcher + run.sh (keeping workdir for next run) ..."
    forward_signal_to_child
    if [ -n "${WORK_DIR}" ]; then
        info "Workdir kept: ${WORK_DIR}"
        info "Wipe next time with: ./dev_run.sh --clean-workdir"
    fi
    exit 0
}

on_exit() {
    if [ "${CLEANING_UP}" = true ]; then
        return 0
    fi
    CLEANING_UP=true
    trap - INT TERM EXIT
    forward_signal_to_child
    if [ -n "${WORK_DIR}" ]; then
        info "Workdir kept: ${WORK_DIR}"
    fi
}

# Apply source → dest. Prints changed paths on stdout when quiet=false via itemize.
# Returns 0 always for watcher loop (errors logged).
rsync_source_to() {
    local dest="$1"
    local itemize="${2:-false}"
    mkdir -p "${dest}"

    if ! command -v rsync >/dev/null 2>&1; then
        tar -C "${SOURCE_DIR}" \
            --exclude='.git' \
            --exclude='frontend/node_modules' \
            --exclude='frontend/.next' \
            --exclude='frontend/out' \
            --exclude='.venv' \
            --exclude='.librelane-data' \
            --exclude='.librelane-bootstrap' \
            --exclude='.librelane-nix-ready' \
            --exclude='.vfox' \
            --exclude='__pycache__' \
            --exclude='.direnv' \
            --exclude='media' \
            -cf - . | tar -C "${dest}" -xf -
        return 0
    fi

    # --no-owner/--no-group/--no-perms: avoid false “changes” on /mnt/c metadata.
    # --delete: drop removed source files; excluded heavy dirs stay on dest.
    if [ "${itemize}" = true ]; then
        rsync -a --delete --no-owner --no-group --no-perms \
            --itemize-changes \
            "${RSYNC_PRESERVE_EXCLUDES[@]}" \
            "${SOURCE_DIR}/" "${dest}/" 2>/dev/null || true
    else
        rsync -a --delete --no-owner --no-group --no-perms \
            "${RSYNC_PRESERVE_EXCLUDES[@]}" \
            "${SOURCE_DIR}/" "${dest}/"
    fi
}

finish_sync_scripts() {
    local dest="$1"
    chmod +x "${dest}/run.sh" "${dest}/dev_run.sh" 2>/dev/null || true
    [ -f "${dest}/vfox-run.sh" ] && chmod +x "${dest}/vfox-run.sh" 2>/dev/null || true
    sanitize_shell_file "${dest}/run.sh"
}

sync_repo_to() {
    local dest="$1"
    mkdir -p "${dest}"

    if [ -d "${dest}/frontend/node_modules" ] || [ -d "${dest}/.librelane-data" ]; then
        step "Refreshing source → ${dest} (reusing node_modules / .next / DB / caches) ..."
    else
        step "Copying project → ${dest} (first sync; later runs reuse heavy dirs) ..."
    fi

    if ! command -v rsync >/dev/null 2>&1; then
        warn "rsync not found — using tar overlay (existing node_modules / caches kept)"
    fi
    rsync_source_to "${dest}" false
    finish_sync_scripts "${dest}"
}

# Fingerprint of tracked source files (mtime+size+path). Used when rsync is missing
# or to decide whether to log; primary apply path still uses rsync.
source_fingerprint() {
    # Exclude the same heavy / irrelevant trees as rsync.
    find "${SOURCE_DIR}" \
        \( -path "${SOURCE_DIR}/.git" -o \
           -path "${SOURCE_DIR}/frontend/node_modules" -o \
           -path "${SOURCE_DIR}/frontend/.next" -o \
           -path "${SOURCE_DIR}/frontend/out" -o \
           -path "${SOURCE_DIR}/.venv" -o \
           -path "${SOURCE_DIR}/.librelane-data" -o \
           -path "${SOURCE_DIR}/.librelane-bootstrap" -o \
           -path "${SOURCE_DIR}/.vfox" -o \
           -path "${SOURCE_DIR}/.direnv" -o \
           -path "${SOURCE_DIR}/media" -o \
           -name '__pycache__' \) -prune -o \
        -type f -printf '%T@ %s %P\n' 2>/dev/null | sort | sha256sum 2>/dev/null | awk '{print $1}'
}

live_sync_once() {
    local dest="$1"
    local changes count

    if command -v rsync >/dev/null 2>&1; then
        changes="$(rsync_source_to "${dest}" true | grep -E '^(>f|\*deleting|c)' || true)"
        if [ -n "${changes}" ]; then
            count="$(printf '%s\n' "${changes}" | grep -c . || true)"
            info "Live sync: ${count} change(s) → ${dest}"
            printf '%s\n' "${changes}" | head -n 8 | while IFS= read -r line; do
                info "  ${line}"
            done
            if [ "${count}" -gt 8 ] 2>/dev/null; then
                info "  …"
            fi
            finish_sync_scripts "${dest}"
        fi
    else
        # No rsync: fingerprint + full tar overlay when something changed.
        local fp fp_file
        fp_file="${dest}/.librelane-dev-source.fp"
        fp="$(source_fingerprint || echo "")"
        if [ -z "${fp}" ]; then
            return 0
        fi
        if [ ! -f "${fp_file}" ] || [ "$(cat "${fp_file}" 2>/dev/null || true)" != "${fp}" ]; then
            rsync_source_to "${dest}" false
            finish_sync_scripts "${dest}"
            printf '%s\n' "${fp}" > "${fp_file}"
            info "Live sync: source changed → ${dest}"
        fi
    fi
}

start_live_watcher() {
    local dest="$1"
    step "Live change detector on (every ${SYNC_INTERVAL}s) — edits in source sync to workdir"
    (
        # Isolate from parent set -e so one failed sync does not kill the loop.
        set +e
        while true; do
            sleep "${SYNC_INTERVAL}"
            live_sync_once "${dest}"
        done
    ) &
    WATCHER_PID=$!
}

for arg in "$@"; do
    case "$arg" in
        --help|-h)
            sed -n '3,21p' "$0" | sed 's/^# //'
            exit 0 ;;
        --clean-workdir)
            CLEAN_WORKDIR=true
            ;;
        --no-watch)
            ENABLE_WATCH=false
            ;;
        *)
            RUN_ARGS+=("$arg")
            ;;
    esac
done

if [ ! -f "${SOURCE_DIR}/run.sh" ]; then
    error "run.sh missing next to this script: ${SOURCE_DIR}"
    exit 1
fi

trap on_interrupt INT TERM
trap on_exit EXIT

CACHE_ROOT="$(pick_linux_cache_root)" || {
    error "Could not create a Linux-native cache dir under \$HOME or /tmp."
    exit 1
}

WORK_DIR="${CACHE_ROOT}/workdir"
mkdir -p "${WORK_DIR}"
WORK_RESOLVED="$(cd "${WORK_DIR}" && pwd -P)"
if is_windows_mount "${WORK_RESOLVED}"; then
    error "Workdir resolved to a Windows mount: ${WORK_RESOLVED}"
    error "Refusing to run — fix \$HOME /tmp so they are not under /mnt/c."
    exit 1
fi
WORK_DIR="${WORK_RESOLVED}"

if is_windows_mount "${SOURCE_DIR}"; then
    info "Source is on a Windows mount (${SOURCE_DIR})"
else
    info "Source: ${SOURCE_DIR}"
fi
info "Workdir (Linux, persistent): ${WORK_DIR}"

if [ "${CLEAN_WORKDIR}" = true ]; then
    wipe_workdir
fi

sync_repo_to "${WORK_DIR}"

if [ "${ENABLE_WATCH}" = true ]; then
    start_live_watcher "${WORK_DIR}"
else
    info "Live change detector disabled (--no-watch)"
fi

step "Starting ./run.sh in work copy ..."
(
    cd "${WORK_DIR}"
    exec bash ./run.sh "${RUN_ARGS[@]+"${RUN_ARGS[@]}"}"
) &
CHILD_PID=$!

# Mirror run.sh: wait survives early return from Ctrl+C handler.
exit_code=0
while kill -0 "${CHILD_PID}" 2>/dev/null; do
    wait "${CHILD_PID}" 2>/dev/null || exit_code=$?
done
CHILD_PID=""

# EXIT trap stops watcher + child if needed and keeps WORK_DIR.
exit "${exit_code}"
