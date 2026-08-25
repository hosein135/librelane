"""Supported PDK variants for the web UI and startup downloads."""

from __future__ import annotations

# Variant (LibreLane PDK=) → Ciel / open_pdks family name.
# Families must exist in LibreLane's pdk_hashes.yaml (ciel-managed).
SUPPORTED_PDK_VARIANTS: dict[str, str] = {
    "sky130A": "sky130",
    "gf180mcuD": "gf180mcu",
    "ihp-sg13g2": "ihp-sg13g2",
}

# Approximate Ciel download size per family (compressed tarballs from FOSSi).
# Exact size varies by pinned open_pdks / IHP revision and which libraries are fetched.
PDK_FAMILY_DOWNLOAD_SIZE: dict[str, str] = {
    "sky130": "~1 GB",
    "gf180mcu": "~800 MB",
    "ihp-sg13g2": "~1.5 GB",
}

SUPPORTED_PDK_VARIANT_LIST: list[str] = list(SUPPORTED_PDK_VARIANTS.keys())


def family_for_variant(variant: str) -> str:
    key = (variant or "").strip()
    try:
        return SUPPORTED_PDK_VARIANTS[key]
    except KeyError as exc:
        allowed = ", ".join(SUPPORTED_PDK_VARIANT_LIST)
        raise ValueError(
            f"Unsupported PDK variant {key!r}. Choose one of: {allowed}."
        ) from exc


def unique_pdk_families() -> list[str]:
    seen: set[str] = set()
    families: list[str] = []
    for family in SUPPORTED_PDK_VARIANTS.values():
        if family not in seen:
            seen.add(family)
            families.append(family)
    return families


def pdk_family_size_label(family: str) -> str:
    return PDK_FAMILY_DOWNLOAD_SIZE.get(family, "size varies")


def format_pdk_family_with_size(family: str) -> str:
    return f"{family} ({pdk_family_size_label(family)})"


def format_all_pdk_families_with_sizes() -> str:
    return ", ".join(format_pdk_family_with_size(f) for f in unique_pdk_families())
