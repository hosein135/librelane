"""Prune expired on-disk run workdirs per STORAGE_WORKDIR_RETENTION_DAYS."""

from django.core.management.base import BaseCommand

from flow.services.storage import prune_expired_workdirs


class Command(BaseCommand):
    help = (
        "Remove finished-run workdirs older than LIBRELANE_WORKDIR_RETENTION_DAYS "
        "(Postgres artifacts are kept). No-op when retention is 0."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List run ids that would be pruned without deleting",
        )

    def handle(self, *args, **options):
        pruned = prune_expired_workdirs(dry_run=options["dry_run"])
        if not pruned:
            self.stdout.write(self.style.SUCCESS("Nothing to prune."))
            return
        verb = "Would prune" if options["dry_run"] else "Pruned"
        self.stdout.write(self.style.SUCCESS(f"{verb} workdirs for runs: {pruned}"))
