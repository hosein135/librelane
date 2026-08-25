"""Download and enable the sky130 PDK (used by run.sh on first startup)."""

import sys

from django.conf import settings
from django.core.management.base import BaseCommand

from flow.services.setup import ensure_pdk, pdk_is_ready


class Command(BaseCommand):
    help = "Download and enable the LibreLane PDK via ciel (sky130 by default)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="Exit 0 if the PDK is ready, 1 if a download is still required",
        )
        parser.add_argument(
            "--pdk-root",
            default=settings.PDK_ROOT,
            help=f"PDK root directory (default: {settings.PDK_ROOT})",
        )
        parser.add_argument(
            "--pdk-family",
            default=settings.LIBRELANE_PDK_FAMILY,
            help=f"PDK family name (default: {settings.LIBRELANE_PDK_FAMILY})",
        )

    def handle(self, *args, **options):
        pdk_root = options["pdk_root"]
        pdk_family = options["pdk_family"]

        if pdk_is_ready(pdk_root, pdk_family):
            self.stdout.write(
                self.style.SUCCESS(
                    f"PDK «{pdk_family}» already enabled under {pdk_root}"
                )
            )
            return

        if options["check_only"]:
            self.stderr.write(
                f"PDK «{pdk_family}» is not installed under {pdk_root}"
            )
            sys.exit(1)

        self.stdout.write(
            f"Downloading PDK «{pdk_family}» into {pdk_root} "
            "(~1 GB from FOSSi; first run can take 30+ minutes)…"
        )
        ensure_pdk(pdk_root, pdk_family, verbose=True)
        self.stdout.write(self.style.SUCCESS("PDK ready."))
