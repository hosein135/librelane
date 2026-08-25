#!/usr/bin/env bash
# =============================================================================
# run.sh — Auto setup-or-start for LibreLane web (Linux / macOS / WSL)
#
# Host bootstrap: curl (static if missing) + Nix (official installer).
# Python, Django, LibreLane, and EDA tools come from devops/flake.nix (nixpkgs + FOSSi cache).
#
# Usage:
#   ./run.sh
#   ./run.sh --force-setup
#   ./run.sh --prep-only
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
CLEANING_UP=false
INTERRUPTED=false
FRONTEND_PID=""
BACKEND_PID=""
BUILD_PID=""
STARTED_POSTGRES=false

for arg in "$@"; do
    case "$arg" in
        --help|-h)
            sed -n '3,30p' "$0" | sed 's/^# //'
            exit 0 ;;
        --__launch)    DO_LAUNCH=true ;;
        --force-setup) FORCE_SETUP=true ;;
        --prep-only)   PREP_ONLY=true ;;
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

WEB_PID=""

export_runtime_env() {
    export LIBRELANE_WEB_ROOT="${SCRIPT_DIR}"
    export LIBRELANE_DATA_DIR="${LIBRELANE_DATA_DIR:-${LIBRELANE_DATA}}"
    mkdir -p "${LIBRELANE_DATA_DIR}"
    export LIBRELANE_WEB_HOST="${LIBRELANE_WEB_HOST:-0.0.0.0}"
    export LIBRELANE_WEB_PORT="${LIBRELANE_WEB_PORT:-8000}"
    export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-librelane_web.settings}"
    export PDK_ROOT="${PDK_ROOT:-${HOME}/.ciel}"
    export LIBRELANE_PDK_FAMILY="${LIBRELANE_PDK_FAMILY:-sky130}"
    sanitize_ld_library_path
}

ensure_pdk_download() {
    if ! command -v librelane-manage >/dev/null 2>&1; then
        warn "librelane-manage not in PATH — skipping PDK download"
        return 0
    fi
    export_runtime_env
    if librelane-manage ensure_pdk --check-only >/dev/null 2>&1; then
        info "PDK «${LIBRELANE_PDK_FAMILY}» ready under ${PDK_ROOT}"
        return 0
    fi
    step "First run: downloading sky130 PDK (~1 GB from FOSSi). This can take 30+ minutes …"
    if ! librelane-manage ensure_pdk; then
        error "PDK download failed."
        error "Check network access to https://fossi-foundation.github.io/ciel-releases"
        exit 1
    fi
    info "PDK ready."
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
    fi
    kill_port_listeners "${LIBRELANE_WEB_PORT:-8000}"
}

stop_pid() {
    local pid="$1"
    [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null || return 0
    kill -TERM "${pid}" 2>/dev/null || true
}

shutdown_stack() {
    local code=$?
    if [ "${CLEANING_UP}" = true ]; then return 0; fi
    CLEANING_UP=true
    trap - INT TERM EXIT
    echo ""
    step "Stopping LibreLane web server ..."
    stop_pid "${WEB_PID}"
    kill_web_server
    WEB_PID=""
    info "Stopped."
    if [ "${INTERRUPTED}" = true ]; then exit 0; fi
    exit "${code}"
}

start_web_server() {
    if command -v librelane-web >/dev/null 2>&1; then
        step "Starting librelane-web on http://127.0.0.1:${LIBRELANE_WEB_PORT} ..."
        librelane-web &
        WEB_PID=$!
        wait "${WEB_PID}"
        return
    fi
    step "librelane-web not in PATH - using manage.py runserver ..."
    if command -v librelane-manage >/dev/null 2>&1; then
        librelane-manage migrate --noinput
        exec librelane-manage runserver "${LIBRELANE_WEB_HOST}:${LIBRELANE_WEB_PORT}"
    fi
    error "Neither librelane-web nor librelane-manage found in Nix environment."
    exit 1
}

launch_stack() {
    prefer_unix_path
    export_runtime_env
    trap 'INTERRUPTED=true; shutdown_stack' INT
    trap 'INTERRUPTED=true; shutdown_stack' TERM
    trap shutdown_stack EXIT
    step "Clearing leftover listeners on :${LIBRELANE_WEB_PORT} ..."
    kill_web_server
    sleep 0.3
    if [ "${PREP_ONLY}" = true ]; then
        ensure_pdk_download
        info "Prep-only - Nix env, PDK, and data dir ready."
        trap - INT TERM EXIT
        return 0
    fi
    ensure_pdk_download
    start_web_server
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
