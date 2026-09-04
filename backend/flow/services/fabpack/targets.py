"""Fabrication targets (shuttle services) selectable per PDK."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FabTarget:
    id: str
    label: str
    vendor: str
    description: str
    pdks: tuple[str, ...]
    submit_url: str
    docs_url: str
    template_repo: str = ""
    build_kind: str = "classic"
    eta: str = "30–90 minutes"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["pdks"] = list(self.pdks)
        return data


FAB_TARGETS: dict[str, FabTarget] = {
    "chipfoundry-caravel": FabTarget(
        id="chipfoundry-caravel",
        label="ChipFoundry (Caravel)",
        vendor="ChipFoundry",
        description=(
            "Macro-first ChipFoundry path: generates a LEF if needed, places your "
            "hardened macro in user_project_wrapper (2920×3520 µm), and produces "
            "gds/user_project_wrapper.gds (LibreLane Classic, typically 30–90 minutes)."
        ),
        pdks=("sky130A",),
        submit_url="https://platform.chipfoundry.io",
        docs_url="https://platform.chipfoundry.io/knowledge-base/article/submission-process",
        template_repo="https://github.com/chipfoundry/caravel_user_project",
        build_kind="classic",
        eta="30–90 minutes",
    ),
    "tinytapeout": FabTarget(
        id="tinytapeout",
        label="Tiny Tapeout",
        vendor="Tiny Tapeout",
        description=(
            "Re-hardens RTL into the Tiny Tapeout tile with official pins "
            "(LibreLane Classic, typically 30–90 minutes). Your previous macro GDS "
            "is kept as a reference — TT tiles are too small to drop an arbitrary macro into."
        ),
        pdks=("sky130A", "gf180mcuD", "ihp-sg13g2"),
        submit_url="https://app.tinytapeout.com",
        docs_url="https://tinytapeout.com/guides/",
        build_kind="classic",
        eta="30–90 minutes",
    ),
    "wafer-space": FabTarget(
        id="wafer-space",
        label="wafer.chip",
        vendor="wafer.space",
        description=(
            "Hardens chip_core for a 1×1 wafer.space slot (LibreLane Classic, typically "
            "30–90 minutes). This is not a full chip_top with pad ring — wafer.space "
            "still wraps chip_core."
        ),
        pdks=("gf180mcuD",),
        submit_url="https://platform.wafer.space",
        docs_url="https://github.com/wafer-space/gf180mcu-project-template",
        template_repo="https://github.com/wafer-space/gf180mcu-project-template",
        build_kind="core",
        eta="30–90 minutes",
    ),
    "ihp-openmpw": FabTarget(
        id="ihp-openmpw",
        label="IHP",
        vendor="IHP Microelectronics",
        description=(
            "Renames the GDS top cell to FMD_QNC_* (usually a few seconds) and lays "
            "out the IHP OpenMPW directory for a TO_<Month><Year> pull request."
        ),
        pdks=("ihp-sg13g2",),
        submit_url="https://github.com/IHP-GmbH",
        docs_url="https://ihp-open-ip.readthedocs.io/en/latest/submission.html",
        build_kind="rename",
        eta="a few seconds",
    ),
}

# Order shown in the UI, per PDK variant (LibreLane ``PDK=`` value).
TARGET_ORDER_BY_PDK: dict[str, tuple[str, ...]] = {
    "sky130A": ("chipfoundry-caravel", "tinytapeout"),
    "gf180mcuD": ("tinytapeout", "wafer-space"),
    "ihp-sg13g2": ("tinytapeout", "ihp-openmpw"),
}

# Family names and lowercase aliases accepted from the UI / run record.
_PDK_ALIASES: dict[str, str] = {
    "sky130": "sky130A",
    "sky130a": "sky130A",
    "gf180mcu": "gf180mcuD",
    "gf180mcud": "gf180mcuD",
    "ihp-sg13g2": "ihp-sg13g2",
    "ihp": "ihp-sg13g2",
}


def canonical_pdk(pdk: str) -> str:
    key = (pdk or "").strip()
    if key in TARGET_ORDER_BY_PDK:
        return key
    return _PDK_ALIASES.get(key.lower(), key)


def targets_for_pdk(pdk: str) -> list[FabTarget]:
    key = canonical_pdk(pdk)
    ordered = TARGET_ORDER_BY_PDK.get(key)
    if ordered is None:
        # Unknown variant: fall back to whatever declares support for it.
        return [t for t in FAB_TARGETS.values() if key in t.pdks]
    return [FAB_TARGETS[tid] for tid in ordered if key in FAB_TARGETS[tid].pdks]


def get_target(target_id: str, pdk: str) -> FabTarget:
    """Return the target or raise ValueError with a user-facing message."""
    target = FAB_TARGETS.get((target_id or "").strip())
    if target is None:
        known = ", ".join(sorted(FAB_TARGETS))
        raise ValueError(f"Unknown fabrication target {target_id!r}. Known targets: {known}.")
    key = canonical_pdk(pdk)
    if key not in target.pdks:
        allowed = ", ".join(t.label for t in targets_for_pdk(pdk)) or "(none)"
        raise ValueError(
            f"{target.label} does not accept designs on PDK {pdk!r}. "
            f"Targets for this PDK: {allowed}."
        )
    return target
