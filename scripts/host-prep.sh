#!/usr/bin/env bash
# Host preflight sourced by run.sh and dev_run.sh.
# Git + curl install, git work-tree repair, and free-disk checks follow the
# same ideas as mmi_cad_dev (run.sh + nix/host-linux.sh), sized for this app:
# Nix store, Ciel PDKs, and the Postgres/run data directory.
#
# Expects the caller to define info, warn, and error.

# --- disk ---------------------------------------------------------------

host_fs_free_bytes() {
    local kb
    kb="$(df -P -k "$1" 2>/dev/null | awk 'NR==2 { print $4 }')"
    if [ -z "${kb}" ] || ! [ "${kb}" -ge 0 ] 2>/dev/null; then
        return 1
    fi
    printf '%s' $((kb * 1024))
}

host_fs_mount() {
    df -P -k "$1" 2>/dev/null | awk 'NR==2 {
        for (i = 6; i <= NF; i++) {
            printf "%s%s", (i > 6 ? " " : ""), $i
        }
        printf "\n"
    }'
}

host_fmt_bytes() {
    awk -v b="$1" 'BEGIN {
        if (b >= 1073741824) printf "%.1f GiB", b / 1073741824
        else if (b >= 1048576) printf "%.0f MiB", b / 1048576
        else printf "%d B", b + 0
    }'
}

# Nearest existing parent, so df works before the directory is created.
host_existing_ancestor() {
    local p="$1"
    case "${p}" in
        "~") p="${HOME:-}" ;;
        "~/"*) p="${HOME:-}/${p#"~/"}" ;;
    esac
    [ -n "${p}" ] || p="/"
    while [ -n "${p}" ] && [ ! -e "${p}" ]; do
        if [ "${p}" = "/" ] || [ "${p}" = "." ]; then
            break
        fi
        p="$(dirname "${p}")"
    done
    if [ -z "${p}" ] || [ ! -e "${p}" ]; then
        printf '%s' "/"
        return 0
    fi
    printf '%s' "${p}"
}

host_reset_mount_needs() {
    HOST_MOUNT_NEEDS=""
}

# mode is "max" (tighter of the two budgets) or "add".
host_record_need() {
    local mount="$1" add="$2" mode="$3"
    local m b new out="" found=0
    [ -n "${mount}" ] || return 0
    while IFS=$'\t' read -r m b; do
        [ -n "${m}" ] || continue
        if [ "${m}" = "${mount}" ]; then
            found=1
            if [ "${mode}" = "add" ]; then
                new=$((b + add))
            elif [ "${add}" -gt "${b}" ]; then
                new="${add}"
            else
                new="${b}"
            fi
            out="${out}${m}"$'\t'"${new}"$'\n'
        else
            out="${out}${m}"$'\t'"${b}"$'\n'
        fi
    done <<EOF
${HOST_MOUNT_NEEDS:-}
EOF
    if [ "${found}" -eq 0 ]; then
        out="${out}${mount}"$'\t'"${add}"$'\n'
    fi
    HOST_MOUNT_NEEDS="${out}"
}

host_file_hash() {
    local f="$1" sum=""
    [ -f "${f}" ] || return 1
    if command -v sha256sum >/dev/null 2>&1; then
        sum="$(sha256sum "${f}" | awk '{print $1}')" || return 1
    elif command -v shasum >/dev/null 2>&1; then
        sum="$(shasum -a 256 "${f}" | awk '{print $1}')" || return 1
    elif command -v openssl >/dev/null 2>&1; then
        sum="$(openssl dgst -sha256 "${f}" | awk '{print $NF}')" || return 1
    else
        return 1
    fi
    [ -n "${sum}" ] || return 1
    printf '%s\n' "${sum}"
}

# Exact Nix-env marker run.sh writes under ~/.cache/librelane-web/<flake hash>/ready.
host_nix_ready_marker() {
    local flake_dir="$1" root key web
    [ -n "${flake_dir}" ] && [ -f "${flake_dir}/flake.nix" ] || return 1
    root="${SYSTEM_CACHE_ROOT:-${XDG_CACHE_HOME:-${HOME:-}/.cache}/librelane-web}"
    web="none"
    if [ -f "${flake_dir}/web-shell.nix" ]; then
        web="$(host_file_hash "${flake_dir}/web-shell.nix")" || web="none"
    fi
    key="$(host_file_hash "${flake_dir}/flake.nix")" || return 1
    printf '%s\n' "${root}/${key}-${web}/ready"
}

# Ciel enables each family as a non-empty directory (often a symlink) under PDK_ROOT.
host_pdks_present() {
    local root="$1" name any
    [ -n "${root}" ] || return 1
    for name in sky130A gf180mcuD ihp-sg13g2; do
        [ -d "${root}/${name}" ] || return 1
        any="$(ls -A "${root}/${name}" 2>/dev/null | head -n 1 || true)"
        [ -n "${any}" ] || return 1
    done
    return 0
}

# quiet=true prints only failures.
# dev_workdir, when set, reserves space for the Linux work copy (node_modules).
host_check_free_space() {
    local quiet="${1:-false}"
    local dev_workdir="${2:-}"
    local store_path store_mnt pdk_mnt data_mnt tmp_mnt dev_mnt
    local pdk_root data_root tmp_root marker
    local nix_ready pdks_ready
    local install_free runtime_free pdk_free nix_only_free tmp_free
    local store_need dev_need
    local m b free fail checked

    install_free="${LIBRELANE_INSTALL_FREE_BYTES:-$((12 * 1024 * 1024 * 1024))}"
    runtime_free="${LIBRELANE_MIN_FREE_BYTES:-$((5 * 1024 * 1024 * 1024))}"
    pdk_free="${LIBRELANE_PDK_FREE_BYTES:-$((4 * 1024 * 1024 * 1024))}"
    nix_only_free="${LIBRELANE_NIX_FREE_BYTES:-$((8 * 1024 * 1024 * 1024))}"
    tmp_free="${LIBRELANE_TMP_FREE_BYTES:-$((512 * 1024 * 1024))}"

    if [ -d /nix/store ]; then
        store_path=/nix/store
    elif [ -d /nix ]; then
        store_path=/nix
    else
        store_path=/
    fi
    pdk_root="${PDK_ROOT:-${HOME:-}/.ciel}"
    data_root="${LIBRELANE_DATA_DIR:-${SCRIPT_DIR:-}/.librelane-data}"
    tmp_root="${TMPDIR:-/tmp}"

    store_mnt="$(host_fs_mount "$(host_existing_ancestor "${store_path}")" || true)"
    pdk_mnt="$(host_fs_mount "$(host_existing_ancestor "${pdk_root}")" || true)"
    data_mnt="$(host_fs_mount "$(host_existing_ancestor "${data_root}")" || true)"
    tmp_mnt="$(host_fs_mount "$(host_existing_ancestor "${tmp_root}")" || true)"

    nix_ready=false
    if [ -n "${SYSTEM_READY:-}" ]; then
        if [ -f "${SYSTEM_READY}" ]; then
            nix_ready=true
        fi
    else
        marker="$(host_nix_ready_marker "${FLAKE_DIR:-${SCRIPT_DIR:-}/devops}" 2>/dev/null || true)"
        if [ -n "${marker}" ] && [ -f "${marker}" ]; then
            nix_ready=true
        fi
    fi
    if host_pdks_present "${pdk_root}"; then
        pdks_ready=true
    else
        pdks_ready=false
    fi

    # Budgets that share a mount use max(), except the first-run figure already
    # includes Nix + PDKs + headroom on that one disk (LIBRELANE_INSTALL_FREE_BYTES).
    # A later PDK download on the data disk must leave the run reserve free, so
    # that case asks for PDK size + LIBRELANE_MIN_FREE_BYTES.
    host_reset_mount_needs
    store_need=$((1024 * 1024 * 1024))
    if [ -n "${store_mnt}" ] && [ "${nix_ready}" != true ] && [ "${pdk_mnt}" = "${store_mnt}" ]; then
        if [ "${pdks_ready}" = true ]; then
            store_need="${nix_only_free}"
        else
            store_need="${install_free}"
        fi
    elif [ "${nix_ready}" != true ]; then
        store_need="${nix_only_free}"
    elif [ "${pdks_ready}" != true ] && [ -n "${pdk_mnt}" ] && [ "${pdk_mnt}" = "${store_mnt}" ]; then
        store_need="${pdk_free}"
    fi
    if [ -n "${store_mnt}" ]; then
        host_record_need "${store_mnt}" "${store_need}" max
    fi

    if [ -n "${pdk_mnt}" ] && [ "${pdk_mnt}" != "${store_mnt}" ]; then
        if [ "${pdks_ready}" = true ]; then
            host_record_need "${pdk_mnt}" $((256 * 1024 * 1024)) max
        else
            host_record_need "${pdk_mnt}" "${pdk_free}" max
        fi
    fi

    if [ -n "${data_mnt}" ]; then
        if [ "${data_mnt}" = "${store_mnt}" ] && [ "${store_need}" = "${install_free}" ]; then
            :
        elif [ "${pdks_ready}" != true ] && [ "${data_mnt}" = "${pdk_mnt}" ]; then
            host_record_need "${data_mnt}" $((pdk_free + runtime_free)) max
        else
            host_record_need "${data_mnt}" "${runtime_free}" max
        fi
    fi

    if [ -n "${tmp_mnt}" ]; then
        host_record_need "${tmp_mnt}" "${tmp_free}" max
    fi

    if [ -n "${dev_workdir}" ]; then
        if [ -n "${LIBRELANE_DEV_FREE_BYTES:-}" ]; then
            dev_need="${LIBRELANE_DEV_FREE_BYTES}"
        elif [ -d "${dev_workdir}/frontend/node_modules" ]; then
            dev_need=$((1024 * 1024 * 1024))
        else
            dev_need=$((3 * 1024 * 1024 * 1024))
        fi
        dev_mnt="$(host_fs_mount "$(host_existing_ancestor "${dev_workdir}")" || true)"
        if [ -n "${dev_mnt}" ]; then
            host_record_need "${dev_mnt}" "${dev_need}" max
        fi
    fi

    if [ "${quiet}" != true ]; then
        info "Storage: data ${data_root}"
        if [ "${pdks_ready}" = true ]; then
            info "Storage: PDKs ${pdk_root} (installed)"
        else
            info "Storage: PDKs ${pdk_root} (will download sky130, gf180mcu, ihp-sg13g2)"
        fi
        info "Storage: Nix cache ${SYSTEM_CACHE_ROOT:-${XDG_CACHE_HOME:-${HOME:-}/.cache}/librelane-web}"
        if [ -n "${dev_workdir}" ]; then
            info "Storage: dev workdir ${dev_workdir}"
        fi
    fi

    fail=0
    checked=0
    while IFS=$'\t' read -r m b; do
        [ -n "${m}" ] || continue
        checked=$((checked + 1))
        if ! free="$(host_fs_free_bytes "${m}")"; then
            warn "Could not measure free space on ${m}."
            continue
        fi
        if [ "${free}" -lt "${b}" ]; then
            if [ "${fail}" -eq 0 ]; then
                error "Not enough free disk space."
            fi
            error "  ${m}: $(host_fmt_bytes "${free}") free, need $(host_fmt_bytes "${b}")"
            fail=1
        elif [ "${quiet}" != true ]; then
            info "Disk: ${m} has $(host_fmt_bytes "${free}") free (need $(host_fmt_bytes "${b}"))"
        fi
    done <<EOF
${HOST_MOUNT_NEEDS:-}
EOF

    if [ "${checked}" -eq 0 ]; then
        warn "Could not measure free disk space. Continuing."
        return 0
    fi
    if [ "${fail}" -ne 0 ]; then
        error "Free disk space and retry."
        error "Budgets: LIBRELANE_INSTALL_FREE_BYTES (first Nix+PDK disk), LIBRELANE_NIX_FREE_BYTES (Nix disk alone), LIBRELANE_PDK_FREE_BYTES, LIBRELANE_MIN_FREE_BYTES (run data)."
        return 1
    fi
    return 0
}

# --- sudo back to the desktop user --------------------------------------

host_user_home() {
    local u="$1" home=""
    if command -v getent >/dev/null 2>&1; then
        home="$(getent passwd "${u}" 2>/dev/null | awk -F: '{ print $6; exit }' || true)"
    fi
    if [ -z "${home}" ] && [ -f /etc/passwd ]; then
        home="$(awk -F: -v u="${u}" '$1 == u { print $6; exit }' /etc/passwd || true)"
    fi
    if [ -z "${home}" ] && command -v dscl >/dev/null 2>&1; then
        home="$(dscl . -read "/Users/${u}" NFSHomeDirectory 2>/dev/null | awk '{ print $2; exit }' || true)"
    fi
    printf '%s' "${home}"
}

# sudo ./run.sh would install Nix and data as root. Continue as SUDO_USER.
# Plain root (containers, CI) is left as root.
host_rerun_as_desktop_user() {
    local script="$1"
    shift
    local home uid quoted a workdir
    [ "$(id -u)" -eq 0 ] || return 0
    if [ "${HOST_AS_DESKTOP_USER:-}" = "1" ]; then
        error "Still root after switching to the desktop user."
        return 1
    fi
    if [ -z "${SUDO_USER:-}" ] || [ "${SUDO_USER}" = "root" ]; then
        warn "Running as root. Nix, PDKs, and the data directory will belong to root."
        return 0
    fi
    USER="${SUDO_USER}"
    export USER
    export LOGNAME="${USER}"
    home="$(host_user_home "${USER}")"
    if [ -z "${home}" ] || [ ! -d "${home}" ]; then
        error "No home directory for ${USER}."
        return 1
    fi
    uid="$(id -u "${USER}")"
    info "Continuing as ${USER} (sudo stays on the desktop account)."
    export HOST_AS_DESKTOP_USER=1
    export HOME="${home}"
    export PATH="${home}/.nix-profile/bin:${home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"
    if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -d "${XDG_RUNTIME_DIR}" ]; then
        if [ -d "/run/user/${uid}" ]; then
            export XDG_RUNTIME_DIR="/run/user/${uid}"
        fi
    fi
    workdir="$(cd "$(dirname "${script}")" && pwd)"
    cd "${workdir}"
    if command -v runuser >/dev/null 2>&1; then
        exec runuser -u "${USER}" --preserve-environment -- "${script}" "$@"
    fi
    if command -v sudo >/dev/null 2>&1; then
        exec sudo -u "${USER}" --preserve-env -- "${script}" "$@"
    fi
    quoted=""
    for a in "$@"; do
        quoted="${quoted} $(printf '%q' "${a}")"
    done
    exec su -m "${USER}" -s /bin/bash -c "cd $(printf '%q' "${workdir}") && exec $(printf '%q' "${script}")${quoted}"
}

# --- package manager: git and curl before Nix ---------------------------

host_os_release_value() {
    local key="$1" line file
    file="${HOST_OS_RELEASE:-/etc/os-release}"
    [ -r "${file}" ] || return 1
    line="$(grep -m1 "^${key}=" "${file}" 2>/dev/null || true)"
    [ -n "${line}" ] || return 1
    line="${line#${key}=}"
    line="${line#\"}"
    line="${line%\"}"
    line="${line#\'}"
    line="${line%\'}"
    printf '%s' "${line}"
}

host_blob_has() {
    local blob="$1" word
    shift
    for word in "$@"; do
        case "${blob}" in
            *" ${word} "*) return 0 ;;
        esac
    done
    return 1
}

host_prepend_path_dir() {
    local dir="$1"
    [ -d "${dir}" ] || return 0
    case ":${PATH}:" in
        *":${dir}:"*) return 0 ;;
    esac
    PATH="${dir}:${PATH}"
    export PATH
}

host_pkg_manager_ready() {
    case "$1" in
        apt) command -v apt-get >/dev/null 2>&1 || command -v apt >/dev/null 2>&1 ;;
        dnf) command -v dnf >/dev/null 2>&1 ;;
        yum) command -v yum >/dev/null 2>&1 ;;
        microdnf) command -v microdnf >/dev/null 2>&1 ;;
        tdnf) command -v tdnf >/dev/null 2>&1 ;;
        pacman) command -v pacman >/dev/null 2>&1 ;;
        zypper) command -v zypper >/dev/null 2>&1 ;;
        apk) command -v apk >/dev/null 2>&1 ;;
        xbps) command -v xbps-install >/dev/null 2>&1 ;;
        emerge) command -v emerge >/dev/null 2>&1 ;;
        eopkg) command -v eopkg >/dev/null 2>&1 ;;
        slackpkg) command -v slackpkg >/dev/null 2>&1 ;;
        swupd) command -v swupd >/dev/null 2>&1 ;;
        urpmi) command -v urpmi >/dev/null 2>&1 ;;
        guix) command -v guix >/dev/null 2>&1 ;;
        nixos) command -v nix-env >/dev/null 2>&1 || command -v nix >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

host_first_ready_pm() {
    local name
    for name in "$@"; do
        if host_pkg_manager_ready "${name}"; then
            printf '%s\n' "${name}"
            return 0
        fi
    done
    return 1
}

host_is_nixos() {
    [ -f /etc/os-release ] && grep -q '^ID=nixos$' /etc/os-release
}

# Prints one id: apt, dnf, pacman, zypper, apk, nixos, ...
host_linux_pkg_manager() {
    local id like blob picked=""
    id="$(host_os_release_value ID || true)"
    like="$(host_os_release_value ID_LIKE || true)"
    blob=" $(printf '%s %s' "${id}" "${like}" | tr '[:upper:]' '[:lower:]') "

    if host_blob_has "${blob}" nixos || host_is_nixos; then
        printf '%s\n' nixos
        return 0
    fi

    if host_blob_has "${blob}" \
        debian ubuntu linuxmint pop elementary zorin kali raspbian \
        devuan parrot deepin neon mx pureos trisquel linuxlite tails
    then
        picked=apt
    elif host_blob_has "${blob}" photon; then
        picked=tdnf
    elif host_blob_has "${blob}" \
        fedora rhel centos rocky almalinux ol amzn nobara scientific \
        eurolinux openeuler anolis openmandriva
    then
        picked="$(host_first_ready_pm dnf microdnf yum || true)"
    elif host_blob_has "${blob}" mageia mandriva; then
        picked="$(host_first_ready_pm dnf urpmi || true)"
    elif host_blob_has "${blob}" \
        arch manjaro endeavouros garuda artix arcolinux cachyos parabola
    then
        picked=pacman
    elif host_blob_has "${blob}" \
        suse opensuse opensuse-leap opensuse-tumbleweed sles sled
    then
        picked=zypper
    elif host_blob_has "${blob}" alpine postmarketos; then
        picked=apk
    elif host_blob_has "${blob}" void; then
        picked=xbps
    elif host_blob_has "${blob}" gentoo funtoo; then
        picked=emerge
    elif host_blob_has "${blob}" solus; then
        picked=eopkg
    elif host_blob_has "${blob}" slackware; then
        picked=slackpkg
    elif host_blob_has "${blob}" clear-linux-os; then
        picked=swupd
    elif host_blob_has "${blob}" guix; then
        picked=guix
    fi

    if [ -n "${picked}" ] && host_pkg_manager_ready "${picked}"; then
        printf '%s\n' "${picked}"
        return 0
    fi

    if picked="$(host_first_ready_pm \
        apt dnf microdnf yum tdnf pacman zypper apk xbps emerge \
        eopkg urpmi slackpkg swupd guix nixos)"; then
        printf '%s\n' "${picked}"
        return 0
    fi
    return 1
}

host_as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        error "Need root to run: $*"
        return 1
    fi
}

host_install_nixos_packages() {
    local p
    local -a attrs=() flake=()
    for p in "$@"; do
        attrs+=("nixos.${p}")
        flake+=("nixpkgs#${p}")
    done
    if command -v nix-env >/dev/null 2>&1; then
        if nix-env -iA "${attrs[@]}"; then
            return 0
        fi
        warn "nix-env could not install $*. Trying nix profile."
    fi
    if command -v nix >/dev/null 2>&1; then
        nix --extra-experimental-features "nix-command flakes" profile install "${flake[@]}" \
            || return 1
        return 0
    fi
    error "This NixOS system cannot install $* with nix-env or nix profile."
    error "Add $* to configuration.nix, then run: sudo nixos-rebuild switch"
    return 1
}

host_install_host_packages() {
    local pm="$1"
    shift
    local p aptbin
    local -a pkgs=("$@") atoms=()
    [ ${#pkgs[@]} -gt 0 ] || return 0

    case "${pm}" in
        apt)
            aptbin=apt-get
            if ! command -v apt-get >/dev/null 2>&1; then
                aptbin=apt
            fi
            host_as_root env DEBIAN_FRONTEND=noninteractive "${aptbin}" update || return 1
            host_as_root env DEBIAN_FRONTEND=noninteractive "${aptbin}" install -y "${pkgs[@]}" || return 1
            ;;
        dnf) host_as_root dnf install -y "${pkgs[@]}" || return 1 ;;
        yum) host_as_root yum install -y "${pkgs[@]}" || return 1 ;;
        microdnf) host_as_root microdnf install -y "${pkgs[@]}" || return 1 ;;
        tdnf) host_as_root tdnf install -y "${pkgs[@]}" || return 1 ;;
        pacman) host_as_root pacman -Sy --needed --noconfirm "${pkgs[@]}" || return 1 ;;
        zypper) host_as_root zypper --non-interactive install "${pkgs[@]}" || return 1 ;;
        apk) host_as_root apk add --no-cache "${pkgs[@]}" || return 1 ;;
        xbps) host_as_root xbps-install -Sy "${pkgs[@]}" || return 1 ;;
        emerge)
            for p in "${pkgs[@]}"; do
                case "${p}" in
                    git) atoms+=(dev-vcs/git) ;;
                    curl) atoms+=(net-misc/curl) ;;
                    *) atoms+=("${p}") ;;
                esac
            done
            host_as_root emerge --ask=n --noreplace "${atoms[@]}" || return 1
            ;;
        eopkg)
            host_as_root eopkg update-repo || return 1
            host_as_root eopkg install -y "${pkgs[@]}" || return 1
            ;;
        urpmi) host_as_root urpmi --auto "${pkgs[@]}" || return 1 ;;
        slackpkg)
            host_as_root slackpkg -batch=on -default_answer=y update || return 1
            host_as_root slackpkg -batch=on -default_answer=y install "${pkgs[@]}" || return 1
            ;;
        swupd) host_as_root swupd bundle-add --assume=yes "${pkgs[@]}" || return 1 ;;
        guix) guix install "${pkgs[@]}" || return 1 ;;
        nixos) host_install_nixos_packages "${pkgs[@]}" || return 1 ;;
        *)
            error "No install command for package manager '${pm}'."
            return 1
            ;;
    esac
}

host_install_darwin_packages() {
    local prefix
    if ! command -v brew >/dev/null 2>&1; then
        warn "Homebrew is not installed, so git and curl were not installed automatically."
        return 1
    fi
    info "Installing $* with Homebrew."
    brew install "$@" || return 1
    prefix="$(brew --prefix 2>/dev/null || true)"
    if [ -n "${prefix}" ]; then
        host_prepend_path_dir "${prefix}/bin"
    fi
    return 0
}

host_refresh_tool_path() {
    host_prepend_path_dir /usr/local/sbin
    host_prepend_path_dir /usr/local/bin
    host_prepend_path_dir /usr/sbin
    host_prepend_path_dir /usr/bin
    host_prepend_path_dir /bin
    host_prepend_path_dir /opt/homebrew/bin
    if [ -n "${HOME:-}" ]; then
        host_prepend_path_dir "${HOME}/.nix-profile/bin"
        host_prepend_path_dir "${HOME}/.guix-profile/bin"
        host_prepend_path_dir "${HOME}/.local/bin"
    fi
    host_prepend_path_dir /run/current-system/sw/bin
    hash -r 2>/dev/null || true
}

# Install whichever of git and curl is missing. Returns 1 when the package
# manager cannot do it; the caller may still fetch a static curl.
host_ensure_git_and_curl() {
    local tool pm pretty
    local -a missing=() still=()
    for tool in git curl; do
        if ! command -v "${tool}" >/dev/null 2>&1; then
            missing+=("${tool}")
        fi
    done
    if [ ${#missing[@]} -eq 0 ]; then
        return 0
    fi

    if [ "$(uname -s)" = Darwin ]; then
        host_install_darwin_packages "${missing[@]}" || return 1
    else
        if [ "$(uname -s)" != Linux ]; then
            warn "No package install for $(uname -s). Install ${missing[*]} and retry."
            return 1
        fi
        if ! pm="$(host_linux_pkg_manager)"; then
            warn "Need ${missing[*]} before Nix is installed."
            warn "No package manager found. Install ${missing[*]} and retry."
            return 1
        fi
        pretty="$(host_os_release_value PRETTY_NAME || true)"
        if [ -n "${pretty}" ]; then
            info "Installing missing ${missing[*]} with ${pm} (${pretty})."
        else
            info "Installing missing ${missing[*]} with ${pm}."
        fi
        case "${pm}" in
            nixos|guix) ;;
            *)
                if [ "$(id -u)" -ne 0 ]; then
                    info "This may ask for your sudo password."
                fi
                ;;
        esac
        if ! host_install_host_packages "${pm}" "${missing[@]}"; then
            warn "Could not install ${missing[*]} with ${pm}."
            return 1
        fi
    fi

    host_refresh_tool_path
    for tool in "${missing[@]}"; do
        if ! command -v "${tool}" >/dev/null 2>&1; then
            still+=("${tool}")
        fi
    done
    if [ ${#still[@]} -ne 0 ]; then
        warn "Package install finished, but still missing: ${still[*]}"
        return 1
    fi
    info "Installed ${missing[*]}."
    return 0
}

# curl, else wget, else python3. Used for the Nix installer and static curl.
host_http_get() {
    local url="$1" dest="$2" dir
    dir="$(dirname "${dest}")"
    mkdir -p "${dir}"
    rm -f "${dest}"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 -o "${dest}" "${url}" \
            || { rm -f "${dest}"; return 1; }
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "${dest}" "${url}" || { rm -f "${dest}"; return 1; }
    elif command -v python3 >/dev/null 2>&1; then
        python3 - "${url}" "${dest}" <<'PY' || { rm -f "${dest}"; return 1; }
import sys
from urllib.request import urlopen
url, dest = sys.argv[1], sys.argv[2]
with urlopen(url, timeout=180) as src, open(dest, "wb") as out:
    while True:
        chunk = src.read(1024 * 1024)
        if not chunk:
            break
        out.write(chunk)
PY
    else
        error "Cannot download ${url}: need curl, wget, or python3."
        return 1
    fi
    if [ ! -s "${dest}" ]; then
        rm -f "${dest}"
        error "Download was empty: ${url}"
        return 1
    fi
}

# --- git work tree ------------------------------------------------------

host_git_supports_safe_directory() {
    command -v git >/dev/null 2>&1 || return 1
    git -c safe.directory=/ --version >/dev/null 2>&1
}

# Process-local safe.directory so Nix's git can read a WSL / mismatched-owner checkout.
host_export_git_safe_directory() {
    local dir="$1" n
    host_git_supports_safe_directory || return 0
    [ -n "${dir}" ] || return 0
    n="${GIT_CONFIG_COUNT:-0}"
    case "${n}" in
        ""|*[!0-9]*) n=0 ;;
    esac
    export GIT_CONFIG_COUNT=$((n + 1))
    export "GIT_CONFIG_KEY_${n}=safe.directory"
    export "GIT_CONFIG_VALUE_${n}=${dir}"
}

host_configure_git_worktree() {
    local dir="$1" cur
    command -v git >/dev/null 2>&1 || return 0
    git -C "${dir}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
    cur="$(git -C "${dir}" config --local --get core.autocrlf 2>/dev/null || true)"
    if [ "${cur}" != "false" ]; then
        git -C "${dir}" config --local core.autocrlf false \
            || warn "Could not set core.autocrlf=false in ${dir}"
    fi
    cur="$(git -C "${dir}" config --local --get core.eol 2>/dev/null || true)"
    if [ "${cur}" != "lf" ]; then
        git -C "${dir}" config --local core.eol lf \
            || warn "Could not set core.eol=lf in ${dir}"
    fi
}

host_restore_tracked_file() {
    local dir="$1" rel="$2" required="$3"
    local f="${dir}/${rel}"
    if [ -e "${f}" ]; then
        return 0
    fi
    if ! command -v git >/dev/null 2>&1 \
        || ! git -C "${dir}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        if [ "${required}" = true ]; then
            error "Missing ${rel}."
            return 1
        fi
        return 0
    fi
    if git -C "${dir}" cat-file -e "HEAD:${rel}" 2>/dev/null; then
        if ! git -C "${dir}" checkout HEAD -- "${rel}"; then
            error "Could not restore ${rel} from git."
            return 1
        fi
        info "Restored ${rel} from git"
        return 0
    fi
    if [ "${required}" = true ]; then
        error "Missing ${rel}, and that path is not in HEAD."
        return 1
    fi
    return 0
}

# warn_dirty=false for dev_run.sh, where a dirty source tree is expected.
host_prepare_git() {
    local dir="$1" warn_dirty="${2:-true}" rel
    host_export_git_safe_directory "${dir}"
    host_configure_git_worktree "${dir}"
    for rel in \
        devops/flake.nix \
        devops/web-shell.nix \
        database/ensure_db.sql \
        database/ensure_schema.sql \
        backend/manage.py \
        frontend/package.json \
        run.sh
    do
        host_restore_tracked_file "${dir}" "${rel}" true || return 1
    done
    host_restore_tracked_file "${dir}" devops/flake.lock false || return 1
    if [ "${warn_dirty}" = true ]; then
        host_warn_if_git_dirty "${dir}"
    fi
    return 0
}

host_warn_if_git_dirty() {
    local dir="$1" dirty
    command -v git >/dev/null 2>&1 || return 0
    git -C "${dir}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
    dirty="$(git -C "${dir}" --no-optional-locks status --porcelain --untracked-files=normal 2>/dev/null || true)"
    if [ -n "${dirty}" ]; then
        warn "Git working tree has uncommitted changes. Nix may copy the checkout into the store and stall."
        warn "Commit or stash those edits, or run ./dev_run.sh so Nix sees a copy without .git."
    fi
}

# --- Nix version --------------------------------------------------------
# flake.lock version 7 needs Nix >= 2.18. Several installs can sit on one
# machine; pick the newest binary that actually runs.

host_nix_parse_version() {
    local line
    line="$("$1" --version 2>/dev/null | head -n 1 || true)"
    if [[ "${line}" =~ ([0-9]+)\.([0-9]+)(\.([0-9]+))? ]]; then
        printf '%s %s %s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[4]:-0}"
        return 0
    fi
    return 1
}

host_nix_ver_ge() {
    local maj="$1" min="$2" pat="$3"
    local need_maj="$4" need_min="$5" need_pat="$6"
    if [ "${maj}" -gt "${need_maj}" ]; then return 0; fi
    if [ "${maj}" -lt "${need_maj}" ]; then return 1; fi
    if [ "${min}" -gt "${need_min}" ]; then return 0; fi
    if [ "${min}" -lt "${need_min}" ]; then return 1; fi
    [ "${pat}" -ge "${need_pat}" ]
}

# Newest Nix binary among PATH and the usual install locations.
# Prints one path. Returns 1 when none of them run.
host_nix_select_bin() {
    local cand resolved seen best best_key key maj min pat
    local -a cands
    seen="|"
    best=""
    best_key=-1
    cands=()
    if command -v nix >/dev/null 2>&1; then
        cands+=("$(command -v nix)")
    fi
    cands+=(
        /nix/var/nix/profiles/default/bin/nix
        "${HOME:-}/.nix-profile/bin/nix"
        "/nix/var/nix/profiles/per-user/${USER:-}/profile/bin/nix"
        /run/current-system/sw/bin/nix
        /usr/bin/nix
    )
    for cand in "${cands[@]}"; do
        [ -n "${cand}" ] && [ -x "${cand}" ] || continue
        resolved="$(readlink -f "${cand}" 2>/dev/null || printf '%s' "${cand}")"
        case "${seen}" in
            *"|${resolved}|"*) continue ;;
        esac
        seen="${seen}${resolved}|"
        if ! read -r maj min pat < <(host_nix_parse_version "${cand}"); then
            continue
        fi
        key=$((maj * 1000000 + min * 1000 + pat))
        if [ "${key}" -gt "${best_key}" ]; then
            best_key="${key}"
            best="${cand}"
        fi
    done
    [ -n "${best}" ] || return 1
    printf '%s\n' "${best}"
}
