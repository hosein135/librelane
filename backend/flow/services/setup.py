"""PDK setup — mirrors the 'Get LibreLane' cell in notebook.ipynb."""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml
from django.conf import settings


def pdk_version_hash(pdk_family: str) -> str:
    librelane_root = _find_librelane_root()
    hashes_path = librelane_root / "librelane" / "pdk_hashes.yaml"
    if not hashes_path.is_file():
        raise FileNotFoundError(
            f"Cannot find pdk_hashes.yaml at {hashes_path}. "
            "Run this inside the Nix shell (nix develop) so LibreLane is installed."
        )

    with open(hashes_path, encoding="utf-8") as f:
        pdk_hashes = yaml.safe_load(f)

    if pdk_family not in pdk_hashes:
        raise KeyError(
            f"PDK '{pdk_family}' not in pdk_hashes.yaml. "
            f"Available: {', '.join(sorted(pdk_hashes))}"
        )
    return pdk_hashes[pdk_family]


def pdk_is_ready(pdk_root: str, pdk_family: str) -> bool:
    """True when the pinned PDK version is installed and enabled under pdk_root."""
    import ciel
    from ciel.common import Version

    pdk_root_expanded = os.path.expanduser(pdk_root)
    version = Version(pdk_version_hash(pdk_family), pdk_family)
    ciel_home = ciel.get_ciel_home(pdk_root_expanded)
    return version.is_installed(ciel_home) and version.is_current(ciel_home)


def ensure_pdk(
    pdk_root: str,
    pdk_family: str,
    log_buffer: io.StringIO | None = None,
    *,
    verbose: bool = False,
) -> str:
    """
    Enable the sky130 PDK via ciel (same as the Colab notebook).
    Returns captured log text (empty when verbose=True).
    """
    if pdk_is_ready(pdk_root, pdk_family):
        message = f"PDK «{pdk_family}» already enabled under {os.path.expanduser(pdk_root)}.\n"
        if verbose:
            print(message, end="", flush=True)
        elif log_buffer is not None:
            log_buffer.write(message)
        return message

    out = io.StringIO()
    err = io.StringIO()
    pdk_root_expanded = os.path.expanduser(pdk_root)
    os.environ["PDK_ROOT"] = pdk_root_expanded

    import ciel
    from ciel.source import StaticWebDataSource

    pdk_hash = pdk_version_hash(pdk_family)
    enable_kwargs = {
        "data_source": StaticWebDataSource(
            "https://fossi-foundation.github.io/ciel-releases"
        ),
    }
    if verbose:
        enable_kwargs["output"] = sys.stdout
        ciel.enable(
            ciel.get_ciel_home(pdk_root_expanded),
            pdk_family,
            pdk_hash,
            **enable_kwargs,
        )
        combined = ""
    else:
        with redirect_stdout(out), redirect_stderr(err):
            ciel.enable(
                ciel.get_ciel_home(pdk_root_expanded),
                pdk_family,
                pdk_hash,
                **enable_kwargs,
            )
        combined = out.getvalue() + err.getvalue()
        if log_buffer is not None:
            log_buffer.write(combined)
    return combined


def _find_librelane_root() -> Path:
    import librelane

    return Path(librelane.__file__).resolve().parent.parent


def librelane_version() -> str:
    import librelane

    return str(librelane.__version__)


def check_tkinter() -> None:
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "tkinter is required for PDK configuration and is provided by the Nix shell "
            "(LibreLane's python3.pkgs.tkinter). Re-enter with: nix develop --accept-flake-config"
        ) from exc


def configure_interactive(
    design_name: str,
    *,
    pdk: str,
    clock_period: float,
) -> None:
    """Mirror Config.interactive() from the notebook."""
    from librelane.config import Config

    Config.interactive(
        design_name,
        PDK=pdk,
        CLOCK_PORT="clk",
        CLOCK_NET="clk",
        CLOCK_PERIOD=clock_period,
        PRIMARY_GDSII_STREAMOUT_TOOL="klayout",
    )
