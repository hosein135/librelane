"""Run a flow from the CLI (useful outside the web UI)."""

from django.core.management.base import BaseCommand

from flow.models import FlowRun
from flow.services.runner import FlowRunner


class Command(BaseCommand):
    help = "Run setup and all LibreLane steps for a FlowRun id"

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=int)
        parser.add_argument(
            "--setup-only",
            action="store_true",
            help="Only download/configure PDK",
        )

    def handle(self, *args, **options):
        run = FlowRun.objects.get(pk=options["run_id"])
        runner = FlowRunner(run)
        runner.setup()
        self.stdout.write(self.style.SUCCESS(f"Setup complete for run {run.pk}"))
        if not options["setup_only"]:
            runner.run_all()
            self.stdout.write(self.style.SUCCESS(f"Flow complete for run {run.pk}"))
