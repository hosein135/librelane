"""Download and enable LibreLane PDKs via ciel (used by run.sh on first startup)."""

import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from flow.pdk_catalog import (
    format_all_pdk_families_with_sizes,
    format_pdk_family_with_size,
    unique_pdk_families,
)
from flow.services.setup import all_pdks_ready, ensure_all_pdks, ensure_pdk, pdk_is_ready


class Command(BaseCommand):
    help = (
        "Download and enable supported LibreLane PDK families via ciel "
        f"({format_all_pdk_families_with_sizes()} by default)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="Exit 0 if required PDKs are ready, 1 if a download is still required",
        )
        parser.add_argument(
            "--pdk-root",
            default=settings.PDK_ROOT,
            help=f"PDK root directory (default: {settings.PDK_ROOT})",
        )
        parser.add_argument(
            "--pdk-family",
            default="",
            help="Download only this Ciel family (default: all supported families)",
        )

    def handle(self, *args, **options):
        pdk_root = options["pdk_root"]
        family = (options["pdk_family"] or "").strip()

        if family:
            ready = pdk_is_ready(pdk_root, family)
            label = format_pdk_family_with_size(family)
        else:
            ready = all_pdks_ready(pdk_root)
            label = format_all_pdk_families_with_sizes()

        if ready:
            self.stdout.write(
                self.style.SUCCESS(f"PDK(s) already ready under {pdk_root}: {label}")
            )
            return

        if options["check_only"]:
            self.stderr.write(f"PDK(s) not fully installed under {pdk_root}: {label}")
            sys.exit(1)

        self.stdout.write(f"Downloading into {pdk_root}:")
        if family:
            self.stdout.write(f"  - {format_pdk_family_with_size(family)}")
            ensure_pdk(pdk_root, family, verbose=True)
        else:
            for name in unique_pdk_families():
                self.stdout.write(f"  - {format_pdk_family_with_size(name)}")
            ensure_all_pdks(pdk_root, verbose=True)
        self.stdout.write(self.style.SUCCESS("All requested PDKs ready."))
