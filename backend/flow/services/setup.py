"""PDK setup — mirrors the 'Get LibreLane' cell in notebook.ipynb."""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml
from django.conf import settings

from flow.pdk_catalog import (
    format_all_pdk_families_with_sizes,
    format_pdk_family_with_size,
    pdk_family_size_label,
    unique_pdk_families,
)


def pdk_version_hash(pdk_family: str) -> str:
    librelane_root = _find_librelane_root()
    hashes_path = librelane_root / "librelane" / "pdk_hashes.yaml"
    if not hashes_path.is_file():
        raise FileNotFoundError(
            f"Cannot find pdk_hashes.yaml at {hashes_path}. "
            "Run this inside the Nix shell (nix develop) so LibreLane is installed."
        )

    with open(hashes_path, encoding="utf-8") as f:
        pdk_hashes = yaml.safe_load(f) or {}

    if pdk_family not in pdk_hashes:
        available = ", ".join(sorted(pdk_hashes)) or "(none)"
        raise KeyError(
            f"PDK '{pdk_family}' not in pdk_hashes.yaml. "
            f"Available via Ciel/LibreLane: {available}. "
            "Remove it from the supported PDK list or upgrade LibreLane."
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


def all_pdks_ready(pdk_root: str | None = None) -> bool:
    root = pdk_root if pdk_root is not None else settings.PDK_ROOT
    return all(pdk_is_ready(root, family) for family in unique_pdk_families())


def ensure_pdk(
    pdk_root: str,
    pdk_family: str,
    log_buffer: io.StringIO | None = None,
    *,
    verbose: bool = False,
) -> str:
    """
    Enable a PDK family via ciel (same as the Colab notebook).
    Returns captured log text (empty when verbose=True).
    """
    if pdk_is_ready(pdk_root, pdk_family):
        message = (
            f"PDK «{pdk_family}» already enabled under {os.path.expanduser(pdk_root)} "
            f"(approx. download {pdk_family_size_label(pdk_family)}).\n"
        )
        if verbose:
            print(message, end="", flush=True)
        elif log_buffer is not None:
            log_buffer.write(message)
        return message

    size = pdk_family_size_label(pdk_family)
    start_msg = (
        f"Downloading PDK «{pdk_family}» ({size}) into {os.path.expanduser(pdk_root)}…\n"
    )
    if verbose:
        print(start_msg, end="", flush=True)
    elif log_buffer is not None:
        log_buffer.write(start_msg)

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
    done_msg = f"PDK «{pdk_family}» ready ({size}).\n"
    if verbose:
        print(done_msg, end="", flush=True)
    elif log_buffer is not None:
        log_buffer.write(done_msg)
    return start_msg + combined + done_msg


def ensure_all_pdks(
    pdk_root: str | None = None,
    log_buffer: io.StringIO | None = None,
    *,
    verbose: bool = False,
) -> str:
    """Download/enable every supported PDK family under the shared Ciel root."""
    root = pdk_root if pdk_root is not None else settings.PDK_ROOT
    summary = (
        f"Ensuring PDKs under {os.path.expanduser(root)}: "
        f"{format_all_pdk_families_with_sizes()}\n"
    )
    if verbose:
        print(summary, end="", flush=True)
    elif log_buffer is not None:
        log_buffer.write(summary)

    parts: list[str] = [summary]
    for family in unique_pdk_families():
        if verbose:
            print(
                f"\n=== {format_pdk_family_with_size(family)} ===\n",
                flush=True,
            )
        parts.append(ensure_pdk(root, family, log_buffer=log_buffer, verbose=verbose))
    return "".join(parts)


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
    top_module_name: str,
    *,
    pdk: str,
    clock_period: float,
) -> None:
    """Mirror Config.interactive() from the notebook (DESIGN_NAME = top module)."""
    from librelane.config import Config

    Config.interactive(
        top_module_name,
        PDK=pdk,
        CLOCK_PORT="clk",
        CLOCK_NET="clk",
        CLOCK_PERIOD=clock_period,
        PRIMARY_GDSII_STREAMOUT_TOOL="klayout",
    )
