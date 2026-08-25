#!/usr/bin/env bash
# =============================================================================
# run.sh — Auto setup-or-start for LibreLane web (Linux / macOS / WSL)
#
# Host bootstrap: curl (static if missing) + Nix (official installer).
# Python, Django, LibreLane, and EDA tools come from devops/flake.nix (nixpkgs + FOSSi cache).
#
# Usage:
#   ./run.sh                # Postgres + Django API (:8000) + Next.js UI (:3000)
#   ./run.sh --force-setup
#   ./run.sh --prep-only
#   ./run.sh --build        # production Next.js build + next start
#   ./run.sh --help
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[librelane]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[librelane]${NC}  $*"; }
error() { echo -e "${RED}[librelane]${NC} $*" >&2; }
step()  { echo -e "${CYAN}[librelane]${NC}  $*"; }

# WSL/Windows mounts and some editors leave CRLF in shell scripts; bash then errors
# with "$'\r': command not found" when sourcing cached devenv.sh.
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

# run.sh may live on /mnt/c with CRLF — normalize once at startup.
sanitize_shell_file "${BASH_SOURCE[0]}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
READY_MARKER="${SCRIPT_DIR}/.librelane-nix-ready"
BOOTSTRAP_DIR="${SCRIPT_DIR}/.librelane-bootstrap"
BOOTSTRAP_BIN="${BOOTSTRAP_DIR}/bin"
LIBRELANE_DATA="${SCRIPT_DIR}/.librelane-data"
FLAKE_DIR="${SCRIPT_DIR}/devops"
CURL_STATIC_VERSION="8.20.0"
# Pin a Nix that still supports flakes. Latest 2.35.x crashes in non-TTY / WSL
# installs (ioctl PTY + NIX_BECOME abort).
NIX_INSTALL_URL="https://releases.nixos.org/nix/nix-2.24.12/install"
SYSTEM_CACHE_ROOT="${XDG_CACHE_HOME:-${HOME}/.cache}/librelane-web"
SYSTEM_CACHE=""
SYSTEM_PROFILE=""
SYSTEM_DEVENV=""
SYSTEM_READY=""
SYSTEM_LOCK=""

FORCE_SETUP=false
PREP_ONLY=false
DO_LAUNCH=false
FRONTEND_BUILD=false
CLEANING_UP=false
INTERRUPTED=false
FRONTEND_PID=""
BACKEND_PID=""
BUILD_PID=""
WEB_PID=""
STARTED_POSTGRES=false

for arg in "$@"; do
    case "$arg" in
        --help|-h)
            sed -n '3,20p' "$0" | sed 's/^# //'
            exit 0 ;;
        --__launch)    DO_LAUNCH=true ;;
        --force-setup) FORCE_SETUP=true ;;
        --prep-only)   PREP_ONLY=true ;;
        --build)       FRONTEND_BUILD=true ;;
        *)
            warn "Unknown argument: $arg" ;;
    esac
done

source_nix_profile() {
    if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
        # shellcheck source=/dev/null
        . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
    elif [ -f "${HOME}/.nix-profile/etc/profile.d/nix.sh" ]; then
        # shellcheck source=/dev/null
        . "${HOME}/.nix-profile/etc/profile.d/nix.sh"
    elif [ -f /etc/profile.d/nix.sh ]; then
        # shellcheck source=/dev/null
        . /etc/profile.d/nix.sh
    fi
}

enable_flakes() {
    # Pre-built EDA tools from FOSSi + nixpkgs binary caches (no source/.deb builds).
    export NIX_CONFIG="${NIX_CONFIG:-}
experimental-features = nix-command flakes
substituters = https://cache.nixos.org https://nix-cache.fossi-foundation.org
trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= nix-cache.fossi-foundation.org:3+K59iFwXqKsL7BNu6Guy0v+uTlwsxYQxjspXzqLYQs=
tarball-ttl = 31536000
warn-dirty = false
"
}

nix_cmd() {
    nix --accept-flake-config "$@"
}

file_hash() {
    local f="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "${f}" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "${f}" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "${f}" | awk '{print $NF}'
    else
        error "Need sha256sum, shasum, or openssl to cache the Nix env."
        return 1
    fi
}

init_system_cache_paths() {
    local key
    if [ ! -f "${FLAKE_DIR}/flake.nix" ]; then
        error "flake.nix missing in ${FLAKE_DIR}"
        exit 1
    fi
    key="$(file_hash "${FLAKE_DIR}/flake.nix")-$(file_hash "${FLAKE_DIR}/web-shell.nix" 2>/dev/null || echo none)"
    SYSTEM_CACHE="${SYSTEM_CACHE_ROOT}/${key}"
    SYSTEM_PROFILE="${SYSTEM_CACHE}/profile"
    SYSTEM_DEVENV="${SYSTEM_CACHE}/devenv.sh"
    SYSTEM_READY="${SYSTEM_CACHE}/ready"
    SYSTEM_LOCK="${SYSTEM_CACHE}/flake.lock"
}

restore_cached_flake_lock() {
    if [ ! -f "${FLAKE_DIR}/flake.lock" ] && [ -f "${SYSTEM_LOCK}" ]; then
        if grep -q '"path".*vendor/librelane' "${SYSTEM_LOCK}" 2>/dev/null; then
            warn "Skipping stale cached flake.lock (unlocked path vendor input)"
            return 0
        fi
        mkdir -p "${FLAKE_DIR}"
        cp -f "${SYSTEM_LOCK}" "${FLAKE_DIR}/flake.lock"
        info "Reusing flake.lock already fetched on this system"
    fi
}

flake_lock_needs_refresh() {
    [ ! -f "${FLAKE_DIR}/flake.lock" ] && return 0
    grep -q '"path".*vendor/librelane' "${FLAKE_DIR}/flake.lock" 2>/dev/null && return 0
    return 1
}

invalidate_system_nix_cache() {
    rm -f "${READY_MARKER}" "${SYSTEM_READY}" "${SYSTEM_DEVENV}"
    if [ -n "${SYSTEM_PROFILE}" ]; then
        rm -f "${SYSTEM_PROFILE}" "${SYSTEM_PROFILE}"-*-link 2>/dev/null || true
        rm -rf "${SYSTEM_PROFILE}" 2>/dev/null || true
    fi
}

prepend_bootstrap_path() {
    if [ -d "${BOOTSTRAP_BIN}" ]; then
        case ":${PATH}:" in
            *":${BOOTSTRAP_BIN}:"*) ;;
            *) export PATH="${BOOTSTRAP_BIN}:${PATH}" ;;
        esac
    fi
}

# Drop Windows/WSL drive PATH entries so host pyenv-win / node.exe never win.
prefer_unix_path() {
    local cleaned="" part oldifs
    oldifs="${IFS}"
    IFS=':'
    # shellcheck disable=SC2086
    for part in ${PATH}; do
        case "${part}" in
            /mnt/[a-zA-Z]/*) continue ;;
            "") continue ;;
        esac
        if [ -z "${cleaned}" ]; then
            cleaned="${part}"
        else
            cleaned="${cleaned}:${part}"
        fi
    done
    IFS="${oldifs}"
    export PATH="${cleaned}"
    hash -r 2>/dev/null || true
}

nix_env_ready() {
    source_nix_profile || true
    enable_flakes
    prepend_bootstrap_path

    command -v nix >/dev/null 2>&1 || return 1
    command -v curl >/dev/null 2>&1 || return 1
    nix flake --help >/dev/null 2>&1 || return 1
    [ -f "${FLAKE_DIR}/flake.nix" ] || return 1
    [ -n "${SYSTEM_READY}" ] && [ -f "${SYSTEM_READY}" ] || return 1
    [ -e "${SYSTEM_PROFILE}" ] || return 1
    restore_cached_flake_lock
    [ -f "${FLAKE_DIR}/flake.lock" ] || return 1
    return 0
}

http_get() {
    local url="$1" dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --tlsv1.2 -o "${dest}" "${url}"
    else
        error "Cannot download ${url}: curl is required (bootstrap)."
        return 1
    fi
}

extract_tar_xz() {
    local archive="$1" dest="$2"
    mkdir -p "${dest}"
    if tar -xJf "${archive}" -C "${dest}" 2>/dev/null; then
        return 0
    fi
    error "Cannot extract ${archive}: need tar with xz support (tar -xJf)."
    return 1
}

static_curl_asset() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"
    case "${os}" in
        Linux)
            case "${arch}" in
                x86_64|amd64)  echo "curl-linux-x86_64-musl-${CURL_STATIC_VERSION}.tar.xz" ;;
                aarch64|arm64) echo "curl-linux-aarch64-musl-${CURL_STATIC_VERSION}.tar.xz" ;;
                *) error "Unsupported Linux arch for static curl: ${arch}"; return 1 ;;
            esac
            ;;
        Darwin)
            case "${arch}" in
                x86_64)        echo "curl-macos-x86_64-${CURL_STATIC_VERSION}.tar.xz" ;;
                arm64|aarch64) echo "curl-macos-arm64-${CURL_STATIC_VERSION}.tar.xz" ;;
                *) error "Unsupported macOS arch for static curl: ${arch}"; return 1 ;;
            esac
            ;;
        *)
            error "Unsupported OS for static curl: ${os}"
            return 1
            ;;
    esac
}

ensure_curl() {
    prepend_bootstrap_path
    if command -v curl >/dev/null 2>&1; then
        info "curl: $(curl --version 2>/dev/null | head -1)"
        return 0
    fi

    step "curl not found — installing static binary (no OS package manager) ..."
    local asset url archive extract_dir found
    asset="$(static_curl_asset)" || exit 1
    url="https://github.com/stunnel/static-curl/releases/download/${CURL_STATIC_VERSION}/${asset}"
    mkdir -p "${BOOTSTRAP_BIN}" "${BOOTSTRAP_DIR}/tmp"
    archive="${BOOTSTRAP_DIR}/tmp/${asset}"
    extract_dir="${BOOTSTRAP_DIR}/tmp/curl-extract-$$"
    rm -rf "${extract_dir}"
    mkdir -p "${extract_dir}"

    http_get "${url}" "${archive}" || exit 1
    extract_tar_xz "${archive}" "${extract_dir}" || exit 1

    found="$(find "${extract_dir}" -type f -name curl 2>/dev/null | head -1 || true)"
    if [ -z "${found}" ]; then
        error "Static curl archive had no 'curl' binary: ${asset}"
        exit 1
    fi
    cp -f "${found}" "${BOOTSTRAP_BIN}/curl"
    chmod +x "${BOOTSTRAP_BIN}/curl"
    rm -rf "${extract_dir}" "${archive}"
    prepend_bootstrap_path
    info "curl installed (static): $(curl --version 2>/dev/null | head -1)"
}

nix_present() {
    source_nix_profile || true
    if command -v nix >/dev/null 2>&1; then
        return 0
    fi
    if [ -x /nix/var/nix/profiles/default/bin/nix ]; then
        export PATH="/nix/var/nix/profiles/default/bin:${PATH}"
        command -v nix >/dev/null 2>&1 && return 0
    fi
    if [ -x "${HOME}/.nix-profile/bin/nix" ]; then
        export PATH="${HOME}/.nix-profile/bin:${PATH}"
        command -v nix >/dev/null 2>&1 && return 0
    fi
    return 1
}

systemd_running() {
    [ -d /run/systemd/system ] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    case "$(systemctl is-system-running 2>/dev/null || true)" in
        running|degraded) return 0 ;;
        *) return 1 ;;
    esac
}

is_wsl() {
    if [ -n "${WSL_DISTRO_NAME:-}" ] || [ -n "${WSL_INTEROP:-}" ]; then
        return 0
    fi
    if [ -r /proc/version ] && grep -qiE '(microsoft|wsl)' /proc/version 2>/dev/null; then
        return 0
    fi
    return 1
}

is_macos() {
    [ "$(uname -s)" = Darwin ]
}

is_linux() {
    [ "$(uname -s)" = Linux ]
}

# Root or a usable sudo (daemon install needs elevation on Linux).
can_install_nix_daemon() {
    if [ "$(id -u)" -eq 0 ]; then
        return 0
    fi
    command -v sudo >/dev/null 2>&1
}

# Pick multi-user (--daemon) vs single-user (--no-daemon) before downloading the installer.
# Equivalent manual commands:
#   curl --proto '=https' --tlsv1.2 -L https://nixos.org/nix/install | sh -s -- --daemon
#   curl --proto '=https' --tlsv1.2 -L https://nixos.org/nix/install | sh -s -- --no-daemon
detect_nix_install_mode() {
    if is_macos; then
        echo "daemon"
        return
    fi
    if is_wsl; then
        if systemd_running && can_install_nix_daemon; then
            echo "daemon"
        else
            echo "no-daemon"
        fi
        return
    fi
    if is_linux; then
        if systemd_running && can_install_nix_daemon; then
            echo "daemon"
        else
            echo "no-daemon"
        fi
        return
    fi
    echo "no-daemon"
}

explain_nix_install_choice() {
    local mode="$1"
    local manual_url="https://nixos.org/nix/install"
    case "${mode}" in
        daemon)
            if is_macos; then
                info "Nix install: multi-user (--daemon) — macOS uses launchd"
            elif is_wsl; then
                info "Nix install: multi-user (--daemon) — WSL with systemd + sudo"
            else
                info "Nix install: multi-user (--daemon) — Linux with systemd + sudo"
            fi
            info "Manual equivalent:"
            info "  curl --proto '=https' --tlsv1.2 -L ${manual_url} | sh -s -- --daemon"
            ;;
        *)
            if is_wsl; then
                info "Nix install: single-user (--no-daemon) — WSL without systemd (or no sudo)"
            elif is_linux; then
                info "Nix install: single-user (--no-daemon) — no systemd or no sudo for multi-user"
            else
                info "Nix install: single-user (--no-daemon) — $(uname -s) without systemd/launchd"
            fi
            info "Manual equivalent:"
            info "  curl --proto '=https' --tlsv1.2 -L ${manual_url} | sh -s -- --no-daemon"
            ;;
    esac
    info "This run uses pinned installer: ${NIX_INSTALL_URL}"
}

nix_install_flags() {
    local mode
    mode="$(detect_nix_install_mode)"
    case "${mode}" in
        daemon) echo "--daemon --yes" ;;
        *) echo "--no-daemon --yes" ;;
    esac
}

run_with_pty() {
    # Nix's installer (and nix-env) call tcgetattr; without a TTY they abort:
    # "Inappropriate ioctl for device" / Assertion 'pid != -1'.
    if [ -t 0 ] && [ -t 1 ]; then
        "$@"
        return $?
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import pty,sys; raise SystemExit(pty.spawn(sys.argv[1:]))' "$@"
        return $?
    fi
    if command -v script >/dev/null 2>&1; then
        case "$(uname -s)" in
            Darwin)
                script -q /dev/null "$@"
                return $?
                ;;
            *)
                mkdir -p "${BOOTSTRAP_DIR}/tmp"
                local wrapper st
                wrapper="$(mktemp "${BOOTSTRAP_DIR}/tmp/pty-XXXXXX")"
                {
                    printf '#!/usr/bin/env bash\nset -- '
                    printf '%q ' "$@"
                    printf '\nexec "$@"\n'
                } > "${wrapper}"
                chmod +x "${wrapper}"
                script -q -e -c "${wrapper}" /dev/null
                st=$?
                rm -f "${wrapper}"
                return "${st}"
                ;;
        esac
    fi
    warn "No PTY helper — Nix install may fail without a real terminal"
    "$@"
}

remove_incomplete_nix() {
    if nix_present; then
        return 0
    fi
    if [ ! -e /nix ] && [ ! -e "${HOME}/.nix-profile" ]; then
        return 0
    fi
    warn "Previous Nix install did not finish — clearing the incomplete tree"
    if [ "$(id -u)" -eq 0 ] || { [ -e /nix ] && [ -O /nix ]; } || { [ -e /nix ] && [ -w /nix ]; }; then
        rm -rf /nix "${HOME}/.nix-profile" "${HOME}/.nix-defexpr" "${HOME}/.nix-channels" \
            /etc/nix 2>/dev/null || true
        rm -f /etc/profile.d/nix.sh /etc/profile.d/nix-daemon.sh 2>/dev/null || true
    else
        error "Incomplete Nix files in /nix but this user cannot remove them."
        error "As root run:  rm -rf /nix ~/.nix-profile ~/.nix-defexpr ~/.nix-channels"
        error "Then re-run ./run.sh"
        exit 1
    fi
}

ensure_nix() {
    if nix_present; then
        info "Nix: $(nix --version 2>/dev/null || true)"
        return 0
    fi

    step "Nix not found — installing via official installer (no OS package manager) ..."
    ensure_curl
    remove_incomplete_nix

    export USER="${USER:-$(id -un 2>/dev/null || echo root)}"
    local tmp="${TMPDIR:-/tmp}"
    case "${tmp}" in
        */) ;;
        *) tmp="${tmp}/" ;;
    esac
    export TMPDIR="${tmp}"

    mkdir -p "${BOOTSTRAP_DIR}/tmp"
    local installer flags mode
    installer="${BOOTSTRAP_DIR}/tmp/nix-installer.sh"
    http_get "${NIX_INSTALL_URL}" "${installer}" || exit 1
    chmod +x "${installer}"

    mode="$(detect_nix_install_mode)"
    case "${mode}" in
        daemon) flags="--daemon --yes" ;;
        *) flags="--no-daemon --yes" ;;
    esac
    explain_nix_install_choice "${mode}"
    info "Nix installer flags: ${flags}"
    # shellcheck disable=SC2086
    if ! run_with_pty sh "${installer}" ${flags}; then
        if [ "${mode}" = "daemon" ]; then
            warn "Daemon install failed — retrying single-user (--no-daemon)"
            remove_incomplete_nix
            if ! run_with_pty sh "${installer}" --no-daemon --yes; then
                error "Nix installer failed. If /nix was left behind, remove it and re-run."
                exit 1
            fi
        else
            error "Nix installer failed. If /nix was left behind, remove it and re-run."
            exit 1
        fi
    fi

    hash -r 2>/dev/null || true
    source_nix_profile || true
    if ! nix_present; then
        error "Nix installed but not on PATH. Open a new terminal and re-run."
        exit 1
    fi
    info "Nix installed: $(nix --version)"
}

ensure_flakes() {
    enable_flakes
    if ! nix flake --help >/dev/null 2>&1; then
        error "This Nix build does not support flakes. Upgrade Nix, then re-run."
        exit 1
    fi
}

ensure_flake_lock() {
    if [ ! -f "${FLAKE_DIR}/flake.nix" ]; then
        error "flake.nix missing in ${FLAKE_DIR}"
        exit 1
    fi
    if [ ! -f "${FLAKE_DIR}/flake.lock" ] && [ -f "${SYSTEM_LOCK}" ]; then
        restore_cached_flake_lock
    fi
    if flake_lock_needs_refresh; then
        step "Creating/updating flake.lock (nixpkgs 25.05) ..."
        nix_cmd flake lock "${FLAKE_DIR}"
    fi
    mkdir -p "${SYSTEM_CACHE}"
    cp -f "${FLAKE_DIR}/flake.lock" "${SYSTEM_LOCK}"
}

check_host_os() {
    case "$(uname -s)" in
        Linux|Darwin) ;;
        *)
            error "Unsupported OS: $(uname -s). Use Linux, macOS, or WSL."
            return 1
            ;;
    esac
}

realize_nix_shell() {
    mkdir -p "${SYSTEM_CACHE}"
    step "Fetching Nix packages into the system cache (once per machine / flake) ..."
    local develop_err="${SYSTEM_CACHE}/develop.err"
    if ! nix_cmd develop "${FLAKE_DIR}" \
        --profile "${SYSTEM_PROFILE}" \
        --no-update-lock-file \
        --command true 2>"${develop_err}"; then
        if grep -qE 'unlocked input|lock file contains|does not match' "${develop_err}" 2>/dev/null; then
            warn "flake.lock out of date — refreshing and retrying ..."
            nix_cmd flake lock "${FLAKE_DIR}"
            cp -f "${FLAKE_DIR}/flake.lock" "${SYSTEM_LOCK}"
            nix_cmd develop "${FLAKE_DIR}" \
                --profile "${SYSTEM_PROFILE}" \
                --no-update-lock-file \
                --command true
        else
            cat "${develop_err}" >&2
            exit 1
        fi
    fi
    if nix_cmd print-dev-env "${FLAKE_DIR}" --offline --no-update-lock-file \
        | tr -d '\r' > "${SYSTEM_DEVENV}.tmp"; then
        mv -f "${SYSTEM_DEVENV}.tmp" "${SYSTEM_DEVENV}"
        sanitize_shell_file "${SYSTEM_DEVENV}"
    else
        rm -f "${SYSTEM_DEVENV}.tmp"
        warn "nix print-dev-env failed — later runs will use nix develop --offline"
    fi
    cp -f "${FLAKE_DIR}/flake.lock" "${SYSTEM_LOCK}"
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "${SYSTEM_READY}"
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "${READY_MARKER}"
    info "Nix packages cached on this system → ${SYSTEM_CACHE}"
}

setup_first_time() {
    step "First-time (or incomplete) setup — preparing LibreLane Nix environment ..."
    ensure_curl
    ensure_nix
    ensure_flakes
    ensure_flake_lock
    check_host_os
    realize_nix_shell
}

dir_chmod_0700_works() {
    local base="$1" probe mode
    mkdir -p "${base}" || return 1
    probe="${base}/.perm-probe-$$"
    mkdir "${probe}" || return 1
    chmod 0700 "${probe}" 2>/dev/null || {
        rmdir "${probe}" 2>/dev/null || true
        return 1
    }
    mode="$(stat -c '%a' "${probe}" 2>/dev/null || stat -f '%OLp' "${probe}" 2>/dev/null || echo "")"
    rm -rf "${probe}"
    [ "${mode}" = "700" ]
}

choose_librelane_data() {
    # Prefer project-local data; on /mnt/* use ~/.local/share for Postgres sockets.
    case "${SCRIPT_DIR}" in
        /mnt/*)
            LIBRELANE_DATA="${HOME}/.local/share/librelane-web"
            ;;
        *)
            LIBRELANE_DATA="${SCRIPT_DIR}/.librelane-data"
            ;;
    esac
}

export_runtime_env() {
    choose_librelane_data
    export LIBRELANE_WEB_ROOT="${SCRIPT_DIR}"
    export LIBRELANE_DATA_DIR="${LIBRELANE_DATA_DIR:-${LIBRELANE_DATA}}"
    mkdir -p "${LIBRELANE_DATA_DIR}"
    export PGHOST="${PGHOST:-127.0.0.1}"
    export PGPORT="${PGPORT:-5432}"
    export PGUSER="${PGUSER:-theapp}"
    export PGPASSWORD="${PGPASSWORD:-theapp}"
    export PGDATABASE="${PGDATABASE:-theapp}"
    export LIBRELANE_PGDATA="${LIBRELANE_PGDATA:-${LIBRELANE_DATA_DIR}/pg}"
    export PGDATA="${LIBRELANE_PGDATA}"
    export BACKEND_PORT="${BACKEND_PORT:-8000}"
    export LIBRELANE_WEB_HOST="${LIBRELANE_WEB_HOST:-127.0.0.1}"
    export LIBRELANE_WEB_PORT="${LIBRELANE_WEB_PORT:-${BACKEND_PORT}}"
    export BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:${BACKEND_PORT}}"
    export DJANGO_ORIGIN="${DJANGO_ORIGIN:-${BACKEND_URL}}"
    export PORT="${PORT:-3000}"
    export HOST="${HOST:-127.0.0.1}"
    export NEXT_ORIGIN="${NEXT_ORIGIN:-http://127.0.0.1:${PORT}}"
    export NEXT_TELEMETRY_DISABLED=1
    export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-librelane_web.settings}"
    export PDK_ROOT="${PDK_ROOT:-${HOME}/.ciel}"
    export LIBRELANE_PDK_FAMILY="${LIBRELANE_PDK_FAMILY:-sky130}"
    sanitize_ld_library_path
}

postgres_ready() {
    command -v pg_isready >/dev/null 2>&1 || return 1
    pg_isready -h "${PGHOST}" -p "${PGPORT}" >/dev/null 2>&1
}

app_db_ready() {
    PGPASSWORD="${PGPASSWORD}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" \
        -v ON_ERROR_STOP=1 -c "SELECT 1" >/dev/null 2>&1
}

schema_ready() {
    local count
    count="$(
        PGPASSWORD="${PGPASSWORD}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" \
            -Atqc "SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema = 'public'
                     AND table_name IN ('users','flow_runs','flow_step_results','flow_run_files')" \
            2>/dev/null || echo 0
    )"
    [ "${count}" = "4" ]
}

our_postgres_running() {
    [ -n "${PGDATA:-}" ] && [ -f "${PGDATA}/postmaster.pid" ] \
        && command -v pg_ctl >/dev/null 2>&1 \
        && pg_ctl -D "${PGDATA}" status >/dev/null 2>&1
}

try_bootstrap_app_db() {
    local super host pass err
    err="${LIBRELANE_DATA_DIR}/bootstrap.err"
    mkdir -p "${LIBRELANE_DATA_DIR}"
    for host in "${PGHOST}" "${LIBRELANE_DATA_DIR}" ""; do
        for super in "${PGUSER_SUPER:-}" postgres "$(whoami)"; do
            [ -z "${super}" ] && continue
            pass="${PGPASSWORD_SUPER:-}"
            if [ -z "${host}" ]; then
                if PGPASSWORD="${pass}" psql -p "${PGPORT}" -U "${super}" -d postgres \
                    -v ON_ERROR_STOP=1 -f "${SCRIPT_DIR}/database/ensure_db.sql" \
                    >"${err}" 2>&1; then
                    return 0
                fi
            else
                if PGPASSWORD="${pass}" psql -h "${host}" -p "${PGPORT}" -U "${super}" -d postgres \
                    -v ON_ERROR_STOP=1 -f "${SCRIPT_DIR}/database/ensure_db.sql" \
                    >"${err}" 2>&1; then
                    return 0
                fi
            fi
        done
    done
    return 1
}

pick_free_pg_port() {
    local p
    for p in "${PGPORT}" 5433 5434 5435 5440 55432; do
        if pg_isready -h "${PGHOST}" -p "${p}" >/dev/null 2>&1; then
            continue
        fi
        printf '%s\n' "${p}"
        return 0
    done
    error "No free TCP port for project Postgres."
    return 1
}

start_project_postgres() {
    mkdir -p "${LIBRELANE_DATA_DIR}"
    if [ ! -f "${PGDATA}/PG_VERSION" ]; then
        step "initdb → ${PGDATA}"
        initdb -D "${PGDATA}" --auth=trust --username=postgres --encoding=UTF8 --locale=C
    fi
    step "Starting project Postgres on ${PGHOST}:${PGPORT} ..."
    pg_ctl -D "${PGDATA}" -l "${LIBRELANE_DATA_DIR}/postgres.log" \
        -o "-p ${PGPORT} -h ${PGHOST} -k ${LIBRELANE_DATA_DIR} -c max_connections=200" start
    STARTED_POSTGRES=true
    printf '%s\n' "${PGPORT}" > "${LIBRELANE_DATA_DIR}/pg.port"
    local i
    for i in $(seq 1 30); do
        postgres_ready && break
        sleep 1
    done
    if ! postgres_ready; then
        error "Postgres failed to start. See ${LIBRELANE_DATA_DIR}/postgres.log"
        tail -n 40 "${LIBRELANE_DATA_DIR}/postgres.log" >&2 || true
        exit 1
    fi
    info "Project Postgres is up (${PGHOST}:${PGPORT})"
}

restore_project_pg_port() {
    local saved
    [ -f "${LIBRELANE_DATA_DIR}/pg.port" ] || return 0
    saved="$(tr -d '[:space:]' < "${LIBRELANE_DATA_DIR}/pg.port" 2>/dev/null || true)"
    case "${saved}" in
        ""|*[!0-9]*) return 0 ;;
    esac
    if our_postgres_running || pg_isready -h "${PGHOST}" -p "${saved}" >/dev/null 2>&1; then
        if [ "${saved}" != "${PGPORT}" ]; then
            info "Reusing project Postgres port ${saved}"
            export PGPORT="${saved}"
        fi
    fi
}

ensure_schema() {
    if ! app_db_ready; then
        error "Database «${PGDATABASE}» is not reachable."
        exit 1
    fi
    if schema_ready; then
        info "App schema is up to date."
        return 0
    fi
    step "Ensuring app schema ..."
    PGPASSWORD="${PGPASSWORD}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" \
        -v ON_ERROR_STOP=1 -f "${SCRIPT_DIR}/database/ensure_schema.sql" >/dev/null
    if ! schema_ready; then
        error "Schema ensure ran but required tables are still missing."
        exit 1
    fi
    info "App schema is ready."
}

ensure_postgres() {
    export_runtime_env
    restore_project_pg_port
    if ! command -v initdb >/dev/null 2>&1 || ! command -v pg_ctl >/dev/null 2>&1; then
        error "PostgreSQL tools missing. Enter via: nix develop ${FLAKE_DIR}"
        exit 1
    fi
    if app_db_ready; then
        info "Postgres ready at ${PGHOST}:${PGPORT} (database «${PGDATABASE}»)"
        ensure_schema
        return 0
    fi
    if our_postgres_running; then
        if ! app_db_ready; then
            try_bootstrap_app_db || exit 1
        fi
        ensure_schema
        return 0
    fi
    if postgres_ready; then
        if try_bootstrap_app_db && app_db_ready; then
            ensure_schema
            return 0
        fi
        local free
        free="$(pick_free_pg_port)" || exit 1
        export PGPORT="${free}"
        start_project_postgres
    else
        start_project_postgres
    fi
    try_bootstrap_app_db || exit 1
    ensure_schema
}

ensure_django_migrations() {
    if command -v librelane-manage >/dev/null 2>&1; then
        librelane-manage migrate --noinput
        return 0
    fi
    error "librelane-manage missing from Nix shell."
    exit 1
}

# WSL often cannot exec Nix node/npm shebangs correctly (bash ends up parsing
# npm's JS: require('../lib/cli.js')). Mirror jadex_django: wrap node + npm-cli.js.
nix_ld_linux() {
    local probe ld
    for probe in "$(command -v psql 2>/dev/null)" "$(command -v pg_ctl 2>/dev/null)" "$(command -v python 2>/dev/null)"; do
        [ -n "${probe}" ] && [ -e "${probe}" ] || continue
        ld="$(ldd "${probe}" 2>/dev/null | awk '/ld-linux/{print $1; exit}')"
        case "${ld}" in
            /nix/store/*)
                [ -x "${ld}" ] || continue
                printf '%s\n' "${ld}"
                return 0
                ;;
        esac
        ld="$(ldd "${probe}" 2>/dev/null | awk '/ld-linux/{print $3; exit}')"
        case "${ld}" in
            /nix/store/*)
                [ -x "${ld}" ] || continue
                printf '%s\n' "${ld}"
                return 0
                ;;
        esac
    done
    return 1
}

file_is_elf() {
    local f="${1:-}" magic
    [ -f "${f}" ] || return 1
    magic="$(od -An -N4 -tx1 "${f}" 2>/dev/null | tr -d ' \n')"
    [ "${magic}" = "7f454c46" ]
}

nix_elf_library_path() {
    local nix_ld
    nix_ld="$(nix_ld_linux || true)"
    if [ -z "${nix_ld}" ]; then
        printf '%s\n' "${LD_LIBRARY_PATH:-}${LIBRARY_PATH:+:${LIBRARY_PATH}}"
        return 0
    fi
    printf '%s\n' "$(dirname "${nix_ld}"):${LD_LIBRARY_PATH:-}${LIBRARY_PATH:+:${LIBRARY_PATH}}"
}

is_unix_node() {
    local n="${1:-}"
    [ -n "${n}" ] && [ -x "${n}" ] || return 1
    case "${n}" in
        *.exe|*.cmd|*.bat|/mnt/[a-zA-Z]/*|*/nix-bin/node|*ld-linux*) return 1 ;;
    esac
    return 0
}

librelane_node() {
    local n p oldifs
    n="$(command -v node 2>/dev/null || true)"
    if is_unix_node "${n}"; then
        printf '%s\n' "${n}"
        return 0
    fi
    oldifs="${IFS}"
    IFS=':'
    for p in ${PATH}; do
        IFS="${oldifs}"
        if is_unix_node "${p}/node"; then
            printf '%s\n' "${p}/node"
            return 0
        fi
    done
    IFS="${oldifs}"
    error "Nix node binary not found on PATH"
    return 1
}

resolve_node_elf() {
    local n wrapped line
    n="$(librelane_node)" || return 1
    n="$(readlink -f "${n}" 2>/dev/null || printf '%s' "${n}")"
    if file_is_elf "${n}"; then
        printf '%s\n' "${n}"
        return 0
    fi
    wrapped="$(dirname "${n}")/.node-wrapped"
    if file_is_elf "${wrapped}"; then
        printf '%s\n' "${wrapped}"
        return 0
    fi
    if [ -f "${n}" ]; then
        line="$(grep -oE '/nix/store/[^[:space:]\"'\'']+' "${n}" 2>/dev/null | while read -r p; do
            case "${p}" in
                *ld-linux*) continue ;;
            esac
            if file_is_elf "${p}"; then
                printf '%s\n' "${p}"
                break
            fi
        done)"
        if [ -n "${line}" ]; then
            printf '%s\n' "${line}"
            return 0
        fi
    fi
    printf '%s\n' "${n}"
}

ensure_node_wrappers() {
    local dir="${LIBRELANE_DATA_DIR}/nix-bin"
    local node_elf nix_ld lib_path prefix npm_js
    mkdir -p "${dir}"
    node_elf="$(resolve_node_elf)" || return 1
    prefix="$(cd "$(dirname "${node_elf}")/.." && pwd)"
    npm_js="${prefix}/lib/cli.js"
    if [ ! -f "${npm_js}" ]; then
        npm_js="${prefix}/lib/node_modules/npm/bin/npm-cli.js"
    fi
    if [ ! -f "${npm_js}" ]; then
        npm_js="$(command -v npm 2>/dev/null || true)"
        case "${npm_js}" in
            *.cmd|*.bat|/mnt/[a-zA-Z]/*|*/nix-bin/npm) npm_js="" ;;
        esac
    fi
    if [ ! -f "${npm_js}" ]; then
        error "npm CLI not found next to ${node_elf}"
        return 1
    fi
    nix_ld="$(nix_ld_linux || true)"
    lib_path="$(nix_elf_library_path)"
    rm -f "${dir}/node" "${dir}/npm" "${dir}/execpath-patch.cjs"
    {
        printf '%s\n' \
            'try {' \
            '  var w = process.env.LIBRELANE_NODE_WRAPPER;' \
            '  if (w) {' \
            '    Object.defineProperty(process, "execPath", { configurable: true, enumerable: true, value: w });' \
            '    process.argv[0] = w;' \
            '  }' \
            '} catch (e) {}'
    } | tr -d '\r' > "${dir}/execpath-patch.cjs"
    if [ -n "${nix_ld}" ] && file_is_elf "${node_elf}"; then
        {
            printf '%s\n' \
                '#!/bin/sh' \
                "export LIBRELANE_NODE_WRAPPER='${dir}/node'" \
                "exec '${nix_ld}' --library-path '${lib_path}' '${node_elf}' -r '${dir}/execpath-patch.cjs' \"\$@\""
        } | tr -d '\r' > "${dir}/node"
    else
        {
            printf '%s\n' \
                '#!/bin/sh' \
                "export LIBRELANE_NODE_WRAPPER='${dir}/node'" \
                "exec '${node_elf}' -r '${dir}/execpath-patch.cjs' \"\$@\""
        } | tr -d '\r' > "${dir}/node"
    fi
    {
        printf '%s\n' \
            '#!/bin/sh' \
            "exec /bin/sh '${dir}/node' '${npm_js}' \"\$@\""
    } | tr -d '\r' > "${dir}/npm"
    chmod 755 "${dir}/node" "${dir}/npm"
    case ":${PATH}:" in
        *":${dir}:"*) ;;
        *) export PATH="${dir}:${PATH}" ;;
    esac
}

run_npm() {
    ensure_node_wrappers || return 1
    /bin/sh "${LIBRELANE_DATA_DIR}/nix-bin/npm" "$@"
}

ensure_pdk_download() {
    if ! command -v librelane-manage >/dev/null 2>&1; then
        warn "librelane-manage not in PATH — skipping PDK download"
        return 0
    fi
    export_runtime_env
    if librelane-manage ensure_pdk --check-only >/dev/null 2>&1; then
        info "Supported PDKs ready under ${PDK_ROOT}"
        return 0
    fi
    step "First run: downloading supported PDKs into ${PDK_ROOT}:"
    info "  - sky130 (~1 GB)"
    info "  - gf180mcu (~800 MB)"
    info "  - ihp-sg13g2 (~1.5 GB)"
    info "Sizes are approximate; first run can take a long time."
    if ! librelane-manage ensure_pdk; then
        error "PDK download failed."
        error "Check network access to https://fossi-foundation.github.io/ciel-releases"
        exit 1
    fi
    info "PDKs ready."
}

sanitize_ld_library_path() {
    local in="${LD_LIBRARY_PATH:-}" out="" p oldifs
    oldifs="${IFS}"
    IFS=':'
    set -f
    for p in ${in}; do
        case "${p}" in
            *glibc*|*ld-linux*|"") continue ;;
        esac
        if [ -z "${out}" ]; then out="${p}"; else out="${out}:${p}"; fi
    done
    set +f
    IFS="${oldifs}"
    export LD_LIBRARY_PATH="${out}"
}

kill_port_listeners() {
    local port="$1" p
    if command -v lsof >/dev/null 2>&1; then
        while IFS= read -r p; do
            [ -n "${p}" ] && kill -TERM "${p}" 2>/dev/null || true
        done < <(lsof -ti ":${port}" 2>/dev/null || true)
    fi
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "${port}/tcp" 2>/dev/null || true
    fi
}

kill_web_server() {
    if command -v pkill >/dev/null 2>&1; then
        pkill -f "librelane-web" 2>/dev/null || true
        pkill -f "${SCRIPT_DIR}/backend/manage.py runserver" 2>/dev/null || true
        pkill -f "manage.py runserver" 2>/dev/null || true
        pkill -f "next dev" 2>/dev/null || true
        pkill -f "next start" 2>/dev/null || true
        pkill -f "next-server" 2>/dev/null || true
    fi
    kill_port_listeners "${LIBRELANE_WEB_PORT:-8000}"
    kill_port_listeners "${PORT:-3000}"
}

stop_pid() {
    local pid="$1"
    [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null || return 0
    local child
    if command -v pgrep >/dev/null 2>&1; then
        for child in $(pgrep -P "${pid}" 2>/dev/null || true); do
            stop_pid "${child}"
        done
    fi
    kill -TERM "${pid}" 2>/dev/null || true
}

shutdown_stack() {
    local code=$?
    if [ "${CLEANING_UP}" = true ]; then return 0; fi
    CLEANING_UP=true
    trap - INT TERM EXIT
    echo ""
    step "Stopping LibreLane stack ..."
    stop_pid "${FRONTEND_PID}"
    stop_pid "${BACKEND_PID}"
    stop_pid "${WEB_PID}"
    stop_pid "${BUILD_PID}"
    kill_web_server
    if [ "${STARTED_POSTGRES}" = true ] && our_postgres_running; then
        step "Stopping project Postgres ..."
        pg_ctl -D "${PGDATA}" stop -m fast >/dev/null 2>&1 || true
    fi
    FRONTEND_PID=""; BACKEND_PID=""; WEB_PID=""; BUILD_PID=""
    info "Stopped."
    if [ "${INTERRUPTED}" = true ]; then exit 0; fi
    exit "${code}"
}

wait_for_http() {
    local url="$1" label="$2" tries="${3:-60}"
    local i code
    for i in $(seq 1 "${tries}"); do
        code="$(curl -sS -o /dev/null -m 2 -w '%{http_code}' "${url}" 2>/dev/null || echo "")"
        case "${code}" in
            [1-5][0-9][0-9]) return 0 ;;
        esac
        sleep 1
    done
    error "${label} did not become ready at ${url}"
    return 1
}

start_backend() {
    step "Starting Django API on :${BACKEND_PORT} ..."
    ensure_django_migrations
    if command -v librelane-web >/dev/null 2>&1; then
        # librelane-web also migrates; env already set.
        LIBRELANE_WEB_HOST="${HOST}" LIBRELANE_WEB_PORT="${BACKEND_PORT}" librelane-web \
            >"${LIBRELANE_DATA_DIR}/backend.log" 2>&1 &
        BACKEND_PID=$!
    elif command -v librelane-manage >/dev/null 2>&1; then
        librelane-manage runserver "${HOST}:${BACKEND_PORT}" \
            >"${LIBRELANE_DATA_DIR}/backend.log" 2>&1 &
        BACKEND_PID=$!
    else
        error "Neither librelane-web nor librelane-manage found in Nix environment."
        exit 1
    fi
    echo "${BACKEND_PID}" >"${LIBRELANE_DATA_DIR}/backend.pid"
    if wait_for_http "http://127.0.0.1:${BACKEND_PORT}/" "Backend" 80; then
        info "Backend API: http://127.0.0.1:${BACKEND_PORT}"
        return 0
    fi
    error "Backend did not become ready. Last log lines:"
    tail -n 40 "${LIBRELANE_DATA_DIR}/backend.log" >&2 || true
    exit 1
}

start_frontend() {
    step "Starting Next.js frontend on :${PORT} ..."
    cd "${SCRIPT_DIR}/frontend"
    mkdir -p .next
    if [ ! -d node_modules ]; then
        step "npm install (first time) ..."
        run_npm install
    fi
    case "${SCRIPT_DIR}" in
        /mnt/*)
            export WATCHPACK_POLLING=true
            export CHOKIDAR_USEPOLLING=true
            ;;
    esac
    if [ "${FRONTEND_BUILD}" = true ]; then
        step "Building production UI ..."
        DJANGO_ORIGIN="${DJANGO_ORIGIN}" NEXT_ORIGIN="${NEXT_ORIGIN}" run_npm run build
        DJANGO_ORIGIN="${DJANGO_ORIGIN}" PORT="${PORT}" HOST="${HOST}" \
            run_npm run start -- --port "${PORT}" --hostname "${HOST}" \
            >"${LIBRELANE_DATA_DIR}/frontend.log" 2>&1 &
    else
        info "Dev frontend (next dev)"
        DJANGO_ORIGIN="${DJANGO_ORIGIN}" PORT="${PORT}" HOST="${HOST}" \
            run_npm run dev -- --port "${PORT}" --hostname "${HOST}" \
            >"${LIBRELANE_DATA_DIR}/frontend.log" 2>&1 &
    fi
    FRONTEND_PID=$!
    echo "${FRONTEND_PID}" >"${LIBRELANE_DATA_DIR}/frontend.pid"
    cd "${SCRIPT_DIR}"
    if wait_for_http "http://127.0.0.1:${PORT}/login" "Frontend" 120; then
        :
    else
        error "Frontend did not become ready. See ${LIBRELANE_DATA_DIR}/frontend.log"
        tail -n 40 "${LIBRELANE_DATA_DIR}/frontend.log" >&2 || true
        exit 1
    fi
    echo ""
    info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    info "  App UI:  http://127.0.0.1:${PORT}"
    info "  API:     http://127.0.0.1:${BACKEND_PORT}"
    info "  Ctrl+C stops frontend, backend, and Postgres."
    info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    wait "${FRONTEND_PID}" || true
}

launch_stack() {
    prefer_unix_path
    export_runtime_env
    trap 'INTERRUPTED=true; shutdown_stack' INT
    trap 'INTERRUPTED=true; shutdown_stack' TERM
    trap shutdown_stack EXIT
    step "Clearing leftover listeners on :${BACKEND_PORT} / :${PORT} ..."
    kill_web_server
    sleep 0.3
    ensure_postgres
    if [ "${PREP_ONLY}" = true ]; then
        ensure_pdk_download
        ensure_django_migrations
        info "Prep-only - Nix env, Postgres schema, PDK, and data dir ready."
        trap - INT TERM EXIT
        return 0
    fi
    ensure_pdk_download
    start_backend
    start_frontend
}

run_inside_nix() {
    step "Nix environment ready - starting LibreLane web ..."
    local launch_pid=0
    forward_launch_signal() {
        INTERRUPTED=true
        if [ "${launch_pid}" -gt 0 ] 2>/dev/null && kill -0 "${launch_pid}" 2>/dev/null; then
            kill -INT "${launch_pid}" 2>/dev/null || kill -TERM "${launch_pid}" 2>/dev/null || true
        fi
    }
    trap forward_launch_signal INT TERM
    if [ -f "${SYSTEM_DEVENV}" ]; then
        info "Using cached Nix env (no download)"
        sanitize_shell_file "${SYSTEM_DEVENV}"
        (
            set +u
            # shellcheck disable=SC1090
            . "${SYSTEM_DEVENV}"
            cd "${SCRIPT_DIR}"
            exec bash "${SCRIPT_DIR}/run.sh" --__launch "$@"
        ) &
        launch_pid=$!
    else
        nix_cmd develop "${FLAKE_DIR}" \
            --profile "${SYSTEM_PROFILE}" \
            --offline \
            --no-update-lock-file \
            --command bash "${SCRIPT_DIR}/run.sh" --__launch "$@" &
        launch_pid=$!
    fi
    while kill -0 "${launch_pid}" 2>/dev/null; do
        wait "${launch_pid}" 2>/dev/null || true
    done
    trap - INT TERM
}

main() {
    if [ "${DO_LAUNCH}" = true ]; then
        launch_stack
        exit 0
    fi

    init_system_cache_paths

    if [ "${FORCE_SETUP}" = true ]; then
        invalidate_system_nix_cache
        setup_first_time
    elif nix_env_ready; then
        info "LibreLane Nix environment already cached - starting."
        source_nix_profile || true
        enable_flakes
        restore_cached_flake_lock
    else
        setup_first_time
    fi

    if [ "${PREP_ONLY}" = true ]; then
        run_inside_nix --prep-only
        info "Prep-only - done."
        exit 0
    fi

    run_inside_nix
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
