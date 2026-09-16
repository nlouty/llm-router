from django.core.management.base import BaseCommand

from router.config import APP_CONFIG
from router.repositories.requests import RequestRepository


class Command(BaseCommand):
    help = "Mark stale processing request records as incomplete."

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold",
            type=int,
            default=int(APP_CONFIG.get("proxy", {}).get("stale_processing_minutes", 66)),
            help="Stale threshold in minutes (defaults to proxy.stale_processing_minutes).",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        threshold = options["threshold"]
        if options["dry_run"]:
            self.stdout.write(f"Dry run: would cleanup processing records older than {threshold} minutes")
            return
        count = RequestRepository.cleanup_stale(threshold_minutes=threshold)
        self.stdout.write(self.style.SUCCESS(f"Updated {count} stale processing records"))
