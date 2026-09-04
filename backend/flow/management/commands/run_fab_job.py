"""Run a shuttle-top fabrication GDS build (detached from the web process)."""

from django.core.management.base import BaseCommand
from django.db import close_old_connections


class Command(BaseCommand):
    help = "Build the shuttle top GDS for a FlowRun + fabrication target"

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=int)
        parser.add_argument("target_id", type=str)

    def handle(self, *args, **options):
        from flow.services.fabpack.integrate import run_integration_job

        close_old_connections()
        try:
            run_integration_job(int(options["run_id"]), str(options["target_id"]))
        finally:
            close_old_connections()
