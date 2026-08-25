#!/usr/bin/env python3
"""Django manage — must run under the Nix-provided interpreter only."""
import os
import sys
from pathlib import Path


def _require_nix_python() -> None:
    exe = Path(sys.executable).resolve()
    if not str(exe).startswith("/nix/store/"):
        raise SystemExit(
            "Refusing to run with a non-Nix Python interpreter:\n"
            f"  {exe}\n\n"
            "Use the embedded environment instead:\n"
            "  ./run.sh\n"
            "  # or: nix develop devops && librelane-web\n"
        )
    expected = os.environ.get("LIBRELANE_NIX_PYTHON")
    if expected and str(exe) != str(Path(expected).resolve()):
        raise SystemExit(
            "Python interpreter does not match LIBRELANE_NIX_PYTHON from the Nix shell:\n"
            f"  running: {exe}\n"
            f"  expected: {expected}\n"
        )


def main() -> None:
    _require_nix_python()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "librelane_web.settings")
    os.environ.setdefault("PYTHONNOUSERSITE", "1")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
