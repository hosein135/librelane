# SPDX-License-Identifier: MIT
# Dev shell and CLI tools using only Nix-store Python (never OS python).
{
  pkgs,
  lib,
  projectRoot, # flake eval hint only; scripts resolve LIBRELANE_WEB_ROOT at runtime
}:
let
  # Verilog-only SPM flow: use nix-eda yosys plugins without ghdl (VHDL).
  librelane = pkgs.python3.pkgs.librelane.override (_old: {
    yosys-plugin-set = with pkgs; [
      yosys-sby
      yosys-eqy
      yosys-slang
    ];
  });

  webPython = pkgs.python3.withPackages (
    ps: with ps; [
      librelane
      django
      gunicorn
      whitenoise
    ]
  );

  webPythonBin = "${webPython}/bin/python";
  webSitePackages = "${webPython}/${webPython.sitePackages}";
  toolsPath = lib.makeBinPath librelane.includedTools;

  # Resolve repo root when the wrapper runs — never bake live source paths into /nix/store.
  resolveRoot = ''
    resolve_librelane_web_root() {
      if [ -n "''${LIBRELANE_WEB_ROOT:-}" ] && [ -f "''${LIBRELANE_WEB_ROOT}/backend/manage.py" ]; then
        printf '%s\n' "''${LIBRELANE_WEB_ROOT}"
        return 0
      fi
      local dir="''${PWD}"
      while [ -n "$dir" ] && [ "$dir" != "/" ]; do
        if [ -f "$dir/backend/manage.py" ]; then
          printf '%s\n' "$dir"
          return 0
        fi
        dir="$(dirname "$dir")"
      done
      echo "librelane-manage: could not find backend/manage.py" >&2
      echo "Set LIBRELANE_WEB_ROOT to the repo root or run from the project tree." >&2
      return 1
    }
  '';

  exportAppEnv = ''
    ${resolveRoot}
    LIBRELANE_WEB_ROOT="$(resolve_librelane_web_root)"
    export LIBRELANE_WEB_ROOT
    export DJANGO_SETTINGS_MODULE=librelane_web.settings
    export PYTHONNOUSERSITE=1
    export PYTHONPATH="''${LIBRELANE_WEB_ROOT}/backend:${webSitePackages}"
    export LIBRELANE_NIX_PYTHON="${webPythonBin}"
    if [ -z "''${LIBRELANE_DATA_DIR:-}" ]; then
      if [ -d "''${LIBRELANE_WEB_ROOT}/.librelane-data" ] || [ -w "''${LIBRELANE_WEB_ROOT}" ]; then
        export LIBRELANE_DATA_DIR="''${LIBRELANE_WEB_ROOT}/.librelane-data"
      else
        export LIBRELANE_DATA_DIR="$HOME/.local/share/librelane-web"
      fi
    fi
    export PATH="${webPython}/bin:${toolsPath}:$PATH"
  '';

  manage = pkgs.writeShellScriptBin "librelane-manage" ''
    set -euo pipefail
    ${exportAppEnv}
    exec "${webPythonBin}" "''${LIBRELANE_WEB_ROOT}/backend/manage.py" "$@"
  '';

  web = pkgs.writeShellScriptBin "librelane-web" ''
    set -euo pipefail
    export LIBRELANE_WEB_HOST="''${LIBRELANE_WEB_HOST:-0.0.0.0}"
    export LIBRELANE_WEB_PORT="''${LIBRELANE_WEB_PORT:-8000}"
    ${exportAppEnv}
    librelane-manage migrate --noinput
    exec librelane-manage runserver "''${LIBRELANE_WEB_HOST}:''${LIBRELANE_WEB_PORT}"
  '';

  shell = pkgs.mkShell {
    packages =
      [
        webPython
        manage
        web
      ]
      ++ librelane.includedTools
      ++ (with pkgs; [
        git
        gtkwave
        graphviz
        iverilog
        coreutils
      ]);

    shellHook = ''
      ${exportAppEnv}
      export NIX_PYTHONPATH="${webSitePackages}"
      export PATH="${webPython}/bin:${lib.makeBinPath librelane.includedTools}:$PATH"
      echo "LibreLane web shell - Python: $LIBRELANE_NIX_PYTHON"
      echo "  Repo root: $LIBRELANE_WEB_ROOT"
      echo "  Data dir: $LIBRELANE_DATA_DIR"
      echo "  librelane-manage - Django management"
      echo "  librelane-web - Start http://127.0.0.1:8000/"
    '';
  };
in
{
  inherit webPython manage web shell;
}
