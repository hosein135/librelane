#!/usr/bin/env bash
# =============================================================================
# run.sh — Auto setup-or-start for LibreLane web (Linux / macOS / WSL)
#
# Host bootstrap (scripts/host-prep.sh): git + curl via the distro package
# manager (static curl if that cannot run), free-disk check, git work-tree
# repair, then Nix. Python, Django, LibreLane, and EDA tools come from
# devops/flake.nix (nixpkgs + FOSSi cache).
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
sanitize_shell_file "${SCRIPT_DIR}/scripts/host-prep.sh"
# shellcheck source=scripts/host-prep.sh
. "${SCRIPT_DIR}/scripts/host-prep.sh"
CURL_STATIC_VERSION="8.20.0"
# NixOS 25.05's package manager is Nix 2.28.5 (nixpkgs nixVersions.stable).
# Any other Nix, including a newer 2.35, is replaced. nix upgrade-nix is not
# used: it installs the latest manager and changes flake.lock behavior.
NIX_PIN_VERSION="2.28.5"
NIX_PIN_MAJOR=2
NIX_PIN_MINOR=28
NIX_PIN_PATCH=5
NIX_INSTALL_URL="https://releases.nixos.org/nix/nix-${NIX_PIN_VERSION}/install"
SYSTEM_CACHE_ROOT="${XDG_CACHE_HOME:-${HOME}/.cache}/librelane-web"
SYSTEM_CACHE=""
SYSTEM_PROFILE=""
SYSTEM_DEVENV=""
SYSTEM_READY=""
SYSTEM_LOCK=""

FORCE_SETUP=false
PREP_ONLY=false
DO_LAUNCH=false
RUN_SH_ARGS=()
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
            cat <<'EOF'
LibreLane web — setup or start (Linux / macOS / WSL).

  ./run.sh              Postgres + Django API (:8000) + Next.js UI (:3000)
  ./run.sh --force-setup
  ./run.sh --prep-only  Nix env, database schema, PDKs, and data dir
  ./run.sh --build      production Next.js build + next start

Missing git and curl are installed with the distro package manager
(Homebrew on macOS). If curl still cannot be installed, a static binary
is downloaded. Nix 2.28.5 (the NixOS 25.05 package manager) is required.
Any other Nix is replaced. On NixOS, /nix is kept and 2.28.5 is
installed into your user profile.

sudo ./run.sh continues as the user who invoked sudo.

First run wants about 12 GiB free when the Nix store and PDKs share a
disk. Later runs keep at least 5 GiB free for flow data. Override with
LIBRELANE_INSTALL_FREE_BYTES, LIBRELANE_NIX_FREE_BYTES,
LIBRELANE_PDK_FREE_BYTES, or LIBRELANE_MIN_FREE_BYTES.
EOF
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
    # A previous nix-channel must not replace the locked nixpkgs.
    export NIX_PATH=""
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
    "${NIX_BIN:-nix}" --accept-flake-config "$@"
}

file_hash() {
    local f="$1" sum
    if ! sum="$(host_file_hash "${f}")"; then
        error "Need sha256sum, shasum, or openssl to cache the Nix env."
        return 1
    fi
    printf '%s\n' "${sum}"
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

# Nix 2.35: "requires lock file changes but they're not allowed due to '--no-update-lock-file'".
# Older Nix: unlocked inputs, or a lock that does not match flake.nix.
flake_lock_rejected() {
    local err_file="$1"
    [ -f "${err_file}" ] || return 1
    grep -qE 'unlocked input|lock file contains|does not match|requires lock file changes|no-update-lock-file' \
        "${err_file}" 2>/dev/null
}

refresh_flake_lock() {
    warn "flake.lock does not match this Nix. Updating it, then retrying."
    nix_cmd flake lock "${FLAKE_DIR}"
    mkdir -p "${SYSTEM_CACHE}"
    cp -f "${FLAKE_DIR}/flake.lock" "${SYSTEM_LOCK}"
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
    "${NIX_BIN:-nix}" flake --help >/dev/null 2>&1 || return 1
    [ -f "${FLAKE_DIR}/flake.nix" ] || return 1
    [ -n "${SYSTEM_READY}" ] && [ -f "${SYSTEM_READY}" ] || return 1
    [ -e "${SYSTEM_PROFILE}" ] || return 1
    restore_cached_flake_lock
    [ -f "${FLAKE_DIR}/flake.lock" ] || return 1
    return 0
}

http_get() {
    host_http_get "$@"
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

    step "curl not found — downloading a static binary ..."
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

# Distro packages first (git + curl). Static curl covers hosts with no
# package manager. A git checkout still needs git itself.
ensure_bootstrap_tools() {
    local had_git=0 had_curl=0
    command -v git >/dev/null 2>&1 && had_git=1
    command -v curl >/dev/null 2>&1 && had_curl=1
    if [ "${had_git}" -eq 0 ] || [ "${had_curl}" -eq 0 ]; then
        host_ensure_git_and_curl \
            || warn "Could not install missing git/curl with the system package manager."
        if ! command -v curl >/dev/null 2>&1; then
            ensure_curl
        elif [ "${had_curl}" -eq 0 ]; then
            info "curl: $(curl --version 2>/dev/null | head -1)"
        fi
        if ! command -v git >/dev/null 2>&1; then
            if [ -e "${SCRIPT_DIR}/.git" ]; then
                error "This checkout is a Git work tree, but git is not installed."
                error "Install git and run ./run.sh again."
                exit 1
            fi
            warn "git is not installed. Nix can still download flake inputs; local Git checks are skipped."
        elif [ "${had_git}" -eq 0 ]; then
            info "git: $(git --version 2>/dev/null | head -1)"
        fi
    fi
    ensure_xz || exit 1
}

# The Nix installer refuses to unpack its .tar.xz unless `xz` is on PATH.
# tar -xJf is what actually extracts; this command only has to exist and
# decompress stdin when tar shells out to it.
install_python_xz() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -c 'import lzma' >/dev/null 2>&1 || return 1
    mkdir -p "${BOOTSTRAP_BIN}"
    cat > "${BOOTSTRAP_BIN}/xz" <<'EOF'
#!/bin/sh
exec python3 -c 'import lzma, sys
args = [a for a in sys.argv[1:] if a and not a.startswith("-")]
data = open(args[-1], "rb").read() if args else sys.stdin.buffer.read()
sys.stdout.buffer.write(lzma.decompress(data))
'
EOF
    chmod 755 "${BOOTSTRAP_BIN}/xz"
    prepend_bootstrap_path
}

ensure_xz() {
    if command -v xz >/dev/null 2>&1; then
        return 0
    fi
    host_ensure_commands xz \
        || warn "Could not install xz with the system package manager."
    if command -v xz >/dev/null 2>&1; then
        info "xz: $(xz --version 2>/dev/null | head -1)"
        return 0
    fi
    step "xz not found — using Python's lzma so the Nix installer can unpack."
    if ! install_python_xz; then
        error "The Nix installer needs xz to unpack its tarball."
        error "Install it and re-run. On Debian/Ubuntu: sudo apt-get install -y xz-utils"
        return 1
    fi
    info "xz: python lzma helper at ${BOOTSTRAP_BIN}/xz"
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

nix_activate_bin() {
    local bin="$1" dir
    NIX_BIN="${bin}"
    export NIX_BIN
    dir="$(dirname "${bin}")"
    case ":${PATH}:" in
        *":${dir}:"*) ;;
        *) export PATH="${dir}:${PATH}" ;;
    esac
    hash -r 2>/dev/null || true
}

# Drop a previous Nix (any version) so the pinned installer can run.
# NixOS keeps /nix; that store belongs to the system generation.
nix_remove_previous() {
    local u f
    if host_is_nixos; then
        error "This is NixOS. Refusing to delete /nix."
        error "Upgrade Nix with the system: sudo nixos-rebuild switch"
        return 1
    fi
    info "Removing the previous Nix installation."
    if command -v systemctl >/dev/null 2>&1; then
        host_as_root systemctl stop nix-daemon.socket nix-daemon.service >/dev/null 2>&1 || true
        host_as_root systemctl disable nix-daemon.socket nix-daemon.service >/dev/null 2>&1 || true
        host_as_root systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    for f in /etc/bashrc /etc/bash.bashrc /etc/profile /etc/zsh/zshrc /etc/zshrc; do
        if [ -f "${f}.backup-before-nix" ]; then
            host_as_root cp -a "${f}.backup-before-nix" "${f}" || true
        fi
    done
    host_as_root rm -rf \
        /nix \
        /etc/nix \
        /etc/profile.d/nix.sh \
        /etc/profile.d/nix-daemon.sh \
        /etc/tmpfiles.d/nix-daemon.conf \
        /usr/lib/systemd/system/nix-daemon.service \
        /usr/lib/systemd/system/nix-daemon.socket \
        /etc/systemd/system/nix-daemon.service \
        /etc/systemd/system/nix-daemon.socket \
        /root/.nix-profile /root/.nix-defexpr /root/.nix-channels \
        /root/.local/state/nix /root/.cache/nix \
        || return 1
    rm -rf \
        "${HOME:-}/.nix-profile" "${HOME:-}/.nix-defexpr" "${HOME:-}/.nix-channels" \
        "${HOME:-}/.local/state/nix" "${HOME:-}/.cache/nix" \
        "${HOME:-}/.config/nix"
    if command -v getent >/dev/null 2>&1 && getent group nixbld >/dev/null 2>&1; then
        while IFS= read -r u; do
            [ -n "${u}" ] || continue
            host_as_root userdel "${u}" >/dev/null 2>&1 || true
        done < <(getent passwd | awk -F: '$1 ~ /^nixbld[0-9]+$/ { print $1 }')
        host_as_root groupdel nixbld >/dev/null 2>&1 || true
    fi
    if command -v getent >/dev/null 2>&1 && getent group nix-daemon >/dev/null 2>&1; then
        host_as_root groupdel nix-daemon >/dev/null 2>&1 || true
    fi
    hash -r 2>/dev/null || true
    unset NIX_BIN || true
}

nix_reload_bin() {
    local best
    hash -r 2>/dev/null || true
    source_nix_profile || true
    enable_flakes
    if ! best="$(host_nix_select_bin)"; then
        error "Nix install finished, but the nix command is still missing."
        return 1
    fi
    nix_activate_bin "${best}"
}

nix_version_is_pinned() {
    [ "$1" -eq "${NIX_PIN_MAJOR}" ] \
        && [ "$2" -eq "${NIX_PIN_MINOR}" ] \
        && [ "$3" -eq "${NIX_PIN_PATCH}" ]
}

# Use an already-installed Nix 2.28.5 even when a newer distro or NixOS
# binary is earlier on PATH.
nix_prefer_pinned_bin() {
    local cand maj min pat
    for cand in \
        "${HOME:-}/.nix-profile/bin/nix" \
        "/nix/var/nix/profiles/per-user/${USER:-}/profile/bin/nix" \
        /nix/var/nix/profiles/default/bin/nix \
        /run/current-system/sw/bin/nix
    do
        [ -n "${cand}" ] && [ -x "${cand}" ] || continue
        if read -r maj min pat < <(host_nix_parse_version "${cand}"); then
            if nix_version_is_pinned "${maj}" "${min}" "${pat}"; then
                nix_activate_bin "${cand}"
                return 0
            fi
        fi
    done
    return 1
}

nixpkgs_25_05_rev() {
    local lock="${FLAKE_DIR}/flake.lock" rev
    [ -f "${lock}" ] || return 1
    rev="$(awk '
        /"rev":/ { gsub(/[",]/, "", $2); rev = $2 }
        /"ref": "nixos-25.05"/ { print rev; exit }
    ' "${lock}")"
    [ -n "${rev}" ] || return 1
    printf '%s\n' "${rev}"
}

# NixOS owns /nix. Put the pinned package manager in the user profile.
nix_install_pinned_on_nixos() {
    local rev
    rev="$(nixpkgs_25_05_rev)" || {
        error "Could not read the nixos-25.05 revision from ${FLAKE_DIR}/flake.lock."
        return 1
    }
    info "This is NixOS. Installing Nix ${NIX_PIN_VERSION} into the user profile."
    info "The system /nix store is left in place."
    "${NIX_BIN}" --extra-experimental-features "nix-command flakes" profile install \
        --accept-flake-config \
        "github:NixOS/nixpkgs/${rev}#nixVersions.nix_2_28" || return 1
    if ! nix_prefer_pinned_bin; then
        error "Nix ${NIX_PIN_VERSION} installed, but it is not available in the user profile."
        return 1
    fi
}

nix_install_pinned() {
    local installer flags mode
    if host_is_nixos; then
        error "Nix is not available on this NixOS system."
        error "Boot a generation that includes nix, then run ./run.sh again."
        return 1
    fi
    ensure_curl
    ensure_xz || return 1
    if [ "${NIX_REPLACE:-}" = "1" ]; then
        nix_remove_previous || return 1
    elif ! nix_present && { [ -e /nix ] || [ -e "${HOME}/.nix-profile" ]; }; then
        nix_remove_previous || return 1
    fi

    export USER="${USER:-$(id -un 2>/dev/null || echo root)}"
    local tmp="${TMPDIR:-/tmp}"
    case "${tmp}" in
        */) ;;
        *) tmp="${tmp}/" ;;
    esac
    export TMPDIR="${tmp}"
    export NIX_INSTALLER_YES=1
    export NIX_INSTALLER_NO_CHANNEL_ADD=1

    mkdir -p "${BOOTSTRAP_DIR}/tmp"
    installer="${BOOTSTRAP_DIR}/tmp/nix-installer.sh"
    http_get "${NIX_INSTALL_URL}" "${installer}" || return 1
    chmod +x "${installer}"

    mode="$(detect_nix_install_mode)"
    case "${mode}" in
        daemon) flags="--daemon --yes --no-channel-add" ;;
        *) flags="--no-daemon --yes --no-channel-add" ;;
    esac
    explain_nix_install_choice "${mode}"
    info "Nix installer flags: ${flags}"
    if [ "${mode}" != "daemon" ]; then
        host_ensure_user_nix_dir || return 1
    fi
    # shellcheck disable=SC2086
    if ! run_with_pty sh "${installer}" ${flags}; then
        if [ "${mode}" = "daemon" ]; then
            warn "Daemon install failed — retrying single-user (--no-daemon)"
            nix_remove_previous || return 1
            host_ensure_user_nix_dir || return 1
            if ! run_with_pty sh "${installer}" --no-daemon --yes --no-channel-add; then
                error "Nix installer failed. If /nix was left behind, remove it and re-run."
                return 1
            fi
        else
            warn "Nix installer failed. Removing the previous tree and retrying once."
            nix_remove_previous || return 1
            host_ensure_user_nix_dir || return 1
            # shellcheck disable=SC2086
            if ! run_with_pty sh "${installer}" ${flags}; then
                error "Nix installer failed. If /nix was left behind, remove it and re-run."
                return 1
            fi
        fi
    fi
    rm -f "${installer}"
    hash -r 2>/dev/null || true
    source_nix_profile || true
    if ! nix_present && ! host_nix_select_bin >/dev/null 2>&1; then
        error "Nix installed but not on PATH. Open a new terminal and re-run."
        return 1
    fi
    nix_reload_bin || return 1
    info "Nix installed: $("${NIX_BIN}" --version 2>/dev/null || true)"
}

nix_replace_and_recheck() {
    if host_is_nixos; then
        error "This is NixOS. Refusing to delete /nix."
        error "Set nix.package = pkgs.nixVersions.nix_2_28 and run: sudo nixos-rebuild switch"
        return 1
    fi
    if [ "${NIX_REPAIR:-}" = "1" ]; then
        error "Nix is still not usable after replacing the previous install."
        return 1
    fi
    export NIX_REPAIR=1
    export NIX_REPLACE=1
    step "Replacing this Nix with pinned Nix ${NIX_PIN_VERSION}."
    nix_install_pinned || return 1
    nix_reload_bin || return 1
    nix_check_existing
}

# Start a stopped daemon, or re-enter as a member of nix-daemon.
# Returns 0 when the caller should run the Nix check again.
nix_revive_store() {
    local msg="$1" members a quoted
    case "${msg}" in
        *daemon*|*socket*|*Connection\ refused*|*disconnected*)
            if command -v systemctl >/dev/null 2>&1; then
                info "Starting the Nix daemon from the previous install."
                host_as_root systemctl start nix-daemon.socket nix-daemon.service >/dev/null 2>&1 || true
                return 0
            fi
            ;;
    esac
    case "${msg}" in
        *Permission\ denied*|*Operation\ not\ permitted*|*not\ allowed*)
            if [ "${NIX_SG:-}" = "1" ]; then
                return 1
            fi
            if command -v getent >/dev/null 2>&1 && getent group nix-daemon >/dev/null 2>&1; then
                members="$(getent group nix-daemon | awk -F: '{ print $4 }')"
                case ",${members}," in
                    *",${USER},"*) ;;
                    *)
                        info "Adding ${USER} to the nix-daemon group."
                        host_as_root usermod -aG nix-daemon "${USER}" || return 1
                        ;;
                esac
                if command -v sg >/dev/null 2>&1; then
                    info "Opening the Nix store as a member of nix-daemon."
                    export NIX_SG=1
                    quoted=""
                    if [ "${#RUN_SH_ARGS[@]}" -gt 0 ]; then
                        for a in "${RUN_SH_ARGS[@]}"; do
                            quoted="${quoted} $(printf '%q' "${a}")"
                        done
                    fi
                    exec sg nix-daemon -c "cd $(printf '%q' "${SCRIPT_DIR}") && exec $(printf '%q' "${SCRIPT_DIR}/run.sh")${quoted}"
                fi
            fi
            ;;
    esac
    return 1
}

nix_foreign_channel() {
    local url="" ver="" f
    if command -v nix-channel >/dev/null 2>&1; then
        url="$(nix-channel --list 2>/dev/null | awk '$1=="nixpkgs" || $1=="nixos" { print $2; exit }' || true)"
    fi
    for f in \
        "${HOME:-}/.nix-defexpr/channels/nixpkgs/.version" \
        "/nix/var/nix/profiles/per-user/${USER:-}/channels/nixpkgs/.version" \
        "/nix/var/nix/profiles/per-user/root/channels/nixpkgs/.version"
    do
        [ -f "${f}" ] || continue
        ver="$(tr -d '[:space:]' < "${f}" || true)"
        [ -n "${ver}" ] && break
    done
    if [ -z "${url}" ] && [ -z "${ver}" ]; then
        return 0
    fi
    if [[ "${url}" =~ nixos-25\.05([^0-9]|$) ]]; then
        return 0
    fi
    if [ -z "${url}" ] && [[ "${ver}" =~ ^25\.05([^0-9]|$) ]]; then
        return 0
    fi
    if [ -n "${url}" ] && [ -n "${ver}" ]; then
        printf '%s (checkout %s)\n' "${url}" "${ver}"
    elif [ -n "${url}" ]; then
        printf '%s\n' "${url}"
    else
        printf 'checkout %s\n' "${ver}"
    fi
}

nix_store_error() {
    local msg="$1" line
    error "Nix is installed, but the existing store cannot be used."
    while IFS= read -r line; do
        [ -n "${line}" ] || continue
        error "${line}"
    done <<EOF
$(printf '%s\n' "${msg}" | head -n 6)
EOF
    case "${msg}" in
        *experimental*feature*|*flakes*)
            error "This run sets experimental-features in NIX_CONFIG. If that is overridden, add to nix.conf:"
            error "  experimental-features = nix-command flakes"
            ;;
        *daemon*|*socket*|*Connection\ refused*|*disconnected*)
            error "Start the Nix daemon:  sudo systemctl start nix-daemon"
            error "If this user was just added to the nix-daemon group, log out and back in."
            ;;
        *Permission\ denied*|*Operation\ not\ permitted*|*not\ allowed*)
            error "This user cannot access the store from the previous Nix install."
            error "Multi-user Nix: add the user to nix-daemon and log in again."
            error "Single-user Nix: run as the user who originally installed Nix."
            ;;
        *database*|*schema*|*not\ compatible*)
            error "The Nix database was written by a different Nix version."
            error "Re-run ./run.sh to replace it with Nix ${NIX_PIN_VERSION}."
            ;;
        *)
            error "Re-run ./run.sh after the previous Nix is removed, or reinstall from ${NIX_INSTALL_URL}"
            ;;
    esac
}

nix_expected_system() {
    local os arch cpu sys
    os="$(uname -s)"
    arch="$(uname -m)"
    case "${os}" in
        Linux) sys="linux" ;;
        Darwin) sys="darwin" ;;
        *) return 1 ;;
    esac
    case "${arch}" in
        x86_64|amd64) cpu="x86_64" ;;
        aarch64|arm64) cpu="aarch64" ;;
        *) return 1 ;;
    esac
    printf '%s-%s\n' "${cpu}" "${sys}"
}

# Previous Nix: too old for flake.lock, a different store schema, or a
# daemon left behind by an earlier install. A newer working Nix is kept.
nix_check_existing() {
    local first best maj min pat channel err json url sver
    local smaj smin sys feats store_msg expected

    source_nix_profile || true
    enable_flakes
    first="$(command -v nix 2>/dev/null || true)"
    if nix_prefer_pinned_bin; then
        best="${NIX_BIN}"
    elif ! best="$(host_nix_select_bin)"; then
        if [ "${NIX_REPAIR:-}" = "1" ]; then
            error "Nix binaries still do not run after reinstall."
            return 1
        fi
        if host_is_nixos; then
            error "Nix is not available on this NixOS system."
            error "Boot a generation that includes nix, then run ./run.sh again."
            return 1
        fi
        info "The Nix binary on this machine does not run. Replacing it."
        nix_replace_and_recheck || return 1
        return 0
    else
        nix_activate_bin "${best}"
        if [ -n "${first}" ] && [ "${first}" != "${best}" ]; then
            info "Using ${best} (newer Nix than ${first})."
        fi
    fi

    if ! read -r maj min pat < <(host_nix_parse_version "${NIX_BIN}"); then
        if [ "${NIX_REPAIR:-}" = "1" ]; then
            error "Nix at ${NIX_BIN} still does not report a version."
            return 1
        fi
        info "Nix at ${NIX_BIN} did not report a version. Replacing it."
        nix_replace_and_recheck || return 1
        return 0
    fi

    channel="$(nix_foreign_channel || true)"
    if ! nix_version_is_pinned "${maj}" "${min}" "${pat}"; then
        if [ "${NIX_REPAIR:-}" = "1" ]; then
            error "Nix ${maj}.${min}.${pat} is still not Nix ${NIX_PIN_VERSION} after reinstall."
            return 1
        fi
        if host_is_nixos; then
            if [ "${NIX_NIXOS_PIN_TRIED:-}" = "1" ]; then
                error "This NixOS system still provides Nix ${maj}.${min}.${pat}, not Nix ${NIX_PIN_VERSION}."
                error "Set nix.package = pkgs.nixVersions.nix_2_28 and run: sudo nixos-rebuild switch"
                return 1
            fi
            export NIX_NIXOS_PIN_TRIED=1
            nix_install_pinned_on_nixos || return 1
            nix_check_existing || return 1
            return 0
        fi
        info "Nix ${maj}.${min}.${pat} is not pinned Nix ${NIX_PIN_VERSION} (NixOS 25.05). Replacing it."
        if [ -n "${channel}" ]; then
            info "Existing nixpkgs channel: ${channel}"
        fi
        nix_replace_and_recheck || return 1
        return 0
    fi

    if [ -n "${channel}" ]; then
        warn "A previous nixpkgs channel is ${channel}."
        warn "That channel is outside NixOS 25.05. This run ignores it."
        warn "Packages come from devops/flake.lock (github:NixOS/nixpkgs/nixos-25.05)."
    fi

    err="${TMPDIR:-/tmp}/librelane-nix-check.$$"
    json="$("${NIX_BIN}" store info --json 2>"${err}" || true)"
    if [ -z "${json}" ]; then
        json="$("${NIX_BIN}" store ping --json 2>"${err}" || true)"
    fi
    if [ -z "${json}" ]; then
        store_msg="$(cat "${err}" 2>/dev/null || true)"
        rm -f "${err}"
        if [ "${NIX_STORE_RETRY:-}" != "1" ]; then
            export NIX_STORE_RETRY=1
            if nix_revive_store "${store_msg}"; then
                nix_check_existing || return 1
                return 0
            fi
        fi
        if [ "${NIX_REPAIR:-}" != "1" ]; then
            info "The existing Nix store is unusable. Installing Nix ${NIX_PIN_VERSION}."
            nix_replace_and_recheck || return 1
            return 0
        fi
        nix_store_error "${store_msg}"
        return 1
    fi
    rm -f "${err}"

    url="$(printf '%s' "${json}" | sed -n 's/.*"url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
    sver="$(printf '%s' "${json}" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
    if [[ "${sver}" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
        smaj="${BASH_REMATCH[1]}"
        smin="${BASH_REMATCH[2]}"
        if [ "${smaj}" != "${maj}" ] || [ "${smin}" != "${min}" ]; then
            if [ "${NIX_REPAIR:-}" = "1" ]; then
                error "Nix client ${maj}.${min}.${pat} still does not match the store (${sver})."
                return 1
            fi
            info "Nix client ${maj}.${min}.${pat} does not match the store (${sver}). Replacing Nix."
            nix_replace_and_recheck || return 1
            return 0
        fi
    fi

    if [ -S /nix/var/nix/daemon-socket/socket ] && [[ "${url}" != daemon* ]]; then
        warn "A Nix daemon socket exists, but ${NIX_BIN} is using a local store."
        warn "A single-user Nix and a multi-user Nix are both installed."
    fi

    if [[ "${url}" == daemon* ]] && ! grep -q '^nixbld1:' /etc/passwd 2>/dev/null; then
        if [ "${NIX_REPAIR:-}" = "1" ]; then
            error "The Nix daemon still has no nixbld build users."
            return 1
        fi
        info "The Nix daemon has no nixbld build users. Replacing Nix."
        nix_replace_and_recheck || return 1
        return 0
    fi

    if ! "${NIX_BIN}" flake --help >/dev/null 2>&1; then
        if [ "${NIX_REPAIR:-}" = "1" ]; then
            error "Nix ${maj}.${min}.${pat} still cannot run flakes."
            return 1
        fi
        info "Nix ${maj}.${min}.${pat} cannot run flakes. Replacing it."
        nix_replace_and_recheck || return 1
        return 0
    fi

    sys="$("${NIX_BIN}" config show system 2>/dev/null || true)"
    expected="$(nix_expected_system 2>/dev/null || true)"
    if [ -n "${sys}" ] && [ -n "${expected}" ] && [ "${sys}" != "${expected}" ]; then
        error "Nix is configured for system ${sys}. This machine is ${expected}."
        error "Fix the system setting in /etc/nix/nix.conf or ~/.config/nix/nix.conf."
        return 1
    fi

    if [ "$("${NIX_BIN}" config show restrict-eval 2>/dev/null || true)" = "true" ]; then
        error "Nix restrict-eval is enabled, so this flake cannot fetch NixOS 25.05."
        error "Set restrict-eval = false in the Nix configuration and retry."
        return 1
    fi

    feats="$("${NIX_BIN}" config show experimental-features 2>/dev/null || true)"
    if ! printf '%s' "${feats}" | grep -q 'flakes' \
        || ! printf '%s' "${feats}" | grep -q 'nix-command'; then
        warn "nix.conf does not list flakes. This run enables them with NIX_CONFIG."
    fi

    info "Nix ${maj}.${min}.${pat} (pinned for NixOS 25.05)"
    return 0
}

ensure_nix() {
    source_nix_profile || true
    enable_flakes
    if nix_present || host_nix_select_bin >/dev/null 2>&1; then
        nix_check_existing || exit 1
        return 0
    fi
    step "Nix not found — installing Nix ${NIX_PIN_VERSION} (no OS package manager) ..."
    nix_install_pinned || exit 1
    nix_check_existing || exit 1
}

ensure_flakes() {
    enable_flakes
    if ! "${NIX_BIN:-nix}" flake --help >/dev/null 2>&1; then
        error "This Nix build does not support flakes. Upgrade Nix, then re-run."
        exit 1
    fi
}

ensure_flake_lock() {
    local meta_err
    if [ ! -f "${FLAKE_DIR}/flake.nix" ]; then
        error "flake.nix missing in ${FLAKE_DIR}"
        exit 1
    fi
    if [ ! -f "${FLAKE_DIR}/flake.lock" ] && [ -f "${SYSTEM_LOCK}" ]; then
        restore_cached_flake_lock
    fi
    mkdir -p "${SYSTEM_CACHE}"
    if flake_lock_needs_refresh; then
        step "Creating/updating flake.lock (nixpkgs 25.05) ..."
        nix_cmd flake lock "${FLAKE_DIR}"
    elif [ -f "${FLAKE_DIR}/flake.lock" ]; then
        # Nix 2.35 rejects a lock written by an older Nix when --no-update-lock-file
        # is set. Probe before the dev-shell download so that case updates the lock.
        meta_err="${SYSTEM_CACHE}/lock.err"
        if ! nix_cmd flake metadata "${FLAKE_DIR}" --no-update-lock-file --json \
            >/dev/null 2>"${meta_err}"; then
            if flake_lock_rejected "${meta_err}"; then
                refresh_flake_lock
            else
                cat "${meta_err}" >&2
                exit 1
            fi
        fi
        rm -f "${meta_err}"
    fi
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
    nix_develop_locked() {
        nix_cmd develop "${FLAKE_DIR}" \
            --profile "${SYSTEM_PROFILE}" \
            --no-update-lock-file \
            --command true 2>"${develop_err}"
    }
    if ! nix_develop_locked; then
        if flake_lock_rejected "${develop_err}"; then
            refresh_flake_lock
            if ! nix_develop_locked; then
                if flake_lock_rejected "${develop_err}"; then
                    # A dirty git checkout is fetched as git+file://. Nix 2.35 can
                    # still insist on writing the lock during that fetch.
                    warn "Nix still wants to write flake.lock. Allowing that for this fetch."
                    nix_cmd develop "${FLAKE_DIR}" \
                        --profile "${SYSTEM_PROFILE}" \
                        --command true 2>"${develop_err}" \
                        || { cat "${develop_err}" >&2; exit 1; }
                    cp -f "${FLAKE_DIR}/flake.lock" "${SYSTEM_LOCK}"
                else
                    cat "${develop_err}" >&2
                    exit 1
                fi
            fi
        else
            cat "${develop_err}" >&2
            exit 1
        fi
    fi
    if ! nix_cmd print-dev-env "${FLAKE_DIR}" --offline --no-update-lock-file \
        2>"${develop_err}" | tr -d '\r' > "${SYSTEM_DEVENV}.tmp"; then
        if flake_lock_rejected "${develop_err}"; then
            warn "Nix still wants to write flake.lock. Saving the dev environment anyway."
            nix_cmd print-dev-env "${FLAKE_DIR}" --offline \
                2>"${develop_err}" | tr -d '\r' > "${SYSTEM_DEVENV}.tmp" \
                || rm -f "${SYSTEM_DEVENV}.tmp"
        else
            rm -f "${SYSTEM_DEVENV}.tmp"
        fi
    fi
    if [ -s "${SYSTEM_DEVENV}.tmp" ]; then
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
    ensure_bootstrap_tools
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
    # Postgres requires a directory whose mode stays 0700. Windows mounts
    # (/mnt/* and some network filesystems) ignore chmod, so those use
    # ~/.local/share instead of the checkout.
    if [ "${DATA_DIR_PICKED:-false}" = true ]; then
        return 0
    fi
    local project fallback
    project="${SCRIPT_DIR}/.librelane-data"
    fallback="${HOME}/.local/share/librelane-web"

    if [ -n "${LIBRELANE_DATA_DIR:-}" ]; then
        LIBRELANE_DATA="${LIBRELANE_DATA_DIR}"
        if ! dir_chmod_0700_works "${LIBRELANE_DATA}"; then
            warn "LIBRELANE_DATA_DIR=${LIBRELANE_DATA} does not keep mode 0700. Postgres may refuse to start."
        fi
        DATA_DIR_PICKED=true
        return 0
    fi

    case "${SCRIPT_DIR}" in
        /mnt/*)
            info "Repo is on a Windows mount — Postgres data will live in ${fallback}"
            LIBRELANE_DATA="${fallback}"
            ;;
        *)
            if dir_chmod_0700_works "${project}"; then
                LIBRELANE_DATA="${project}"
                DATA_DIR_PICKED=true
                return 0
            fi
            warn "Cannot use mode 0700 on ${project}. Postgres data will live in ${fallback}"
            LIBRELANE_DATA="${fallback}"
            ;;
    esac
    if ! dir_chmod_0700_works "${LIBRELANE_DATA}"; then
        error "Data directory ${LIBRELANE_DATA} does not accept mode 0700."
        error "Set LIBRELANE_DATA_DIR to a Linux filesystem."
        exit 1
    fi
    DATA_DIR_PICKED=true
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
    local count cols
    count="$(
        PGPASSWORD="${PGPASSWORD}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" \
            -Atqc "SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema = 'public'
                     AND table_name IN ('users','flow_runs','flow_step_results','flow_run_files')" \
            2>/dev/null || echo 0
    )"
    [ "${count}" = "4" ] || return 1
    # Also require columns added after the initial table create (ALTER IF NOT EXISTS).
    cols="$(
        PGPASSWORD="${PGPASSWORD}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" \
            -Atqc "SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'flow_runs'
                     AND column_name IN ('disk_bytes','db_bytes','temp_folder_name','artifacts_stored','name')" \
            2>/dev/null || echo 0
    )"
    [ "${cols}" = "5" ]
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
    # Always apply idempotent SQL (CREATE IF NOT EXISTS + ALTER ADD COLUMN IF NOT EXISTS)
    # so new columns land on existing databases. Skip the noisy step line when already current.
    if schema_ready; then
        PGPASSWORD="${PGPASSWORD}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" \
            -v ON_ERROR_STOP=1 -f "${SCRIPT_DIR}/database/ensure_schema.sql" >/dev/null
        info "App schema is up to date."
        return 0
    fi
    step "Ensuring app schema ..."
    PGPASSWORD="${PGPASSWORD}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDATABASE}" \
        -v ON_ERROR_STOP=1 -f "${SCRIPT_DIR}/database/ensure_schema.sql" >/dev/null
    if ! schema_ready; then
        error "Schema ensure ran but required tables/columns are still missing."
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
    # Nix may have filled the store since the outer check. Fail before Postgres.
    host_check_free_space true || exit 1
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
        # No --no-update-lock-file here. Nix 2.35 rejects a dirty git+file flake
        # when that flag is set, and this path is only the fallback when
        # devenv.sh could not be saved.
        nix_cmd develop "${FLAKE_DIR}" \
            --profile "${SYSTEM_PROFILE}" \
            --offline \
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

    RUN_SH_ARGS=("$@")
    host_rerun_as_desktop_user "${SCRIPT_DIR}/run.sh" "$@" || exit 1
    check_host_os || exit 1
    init_system_cache_paths
    export_runtime_env
    ensure_bootstrap_tools
    host_prepare_git "${SCRIPT_DIR}" true || exit 1
    # --force-setup deletes the Nix marker; budget the download before that work.
    if [ "${FORCE_SETUP}" = true ]; then
        invalidate_system_nix_cache
    fi
    host_check_free_space || exit 1
    # Also when the dev env is already cached: an older or mismatched Nix
    # must be upgraded or replaced before nix develop.
    ensure_nix

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
