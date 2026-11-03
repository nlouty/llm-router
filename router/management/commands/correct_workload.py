from django.core.management.base import BaseCommand

from router.repositories.servers import ServerRepository


class Command(BaseCommand):
    help = (
        "Reconcile servers.workload (and decoder active_tokens) against "
        "in-flight requests (task_status=processing/prefilling/decoding "
        "grouped by target_pod_ip)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Persist the corrected workload values.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the planned changes without writing.",
        )
        parser.add_argument(
            "--offline",
            action="store_true",
            help="Include offline servers (default: online servers only).",
        )

    def handle(self, *args, **options):
        fix = options["fix"]
        dry_run = options["dry_run"]
        include_offline = options["offline"]

        if fix and dry_run:
            self.stderr.write("--fix and --dry-run are mutually exclusive")
            raise SystemExit(1)

        changes, orphans = ServerRepository.recalculate_workload(
            include_offline=include_offline,
            apply=fix,
        )

        for change in changes:
            line = (
                f"server {change['server_id']} ({change['base_url']}): "
                f"workload {change['before']} -> {change['after']}"
            )
            if "tokens_before" in change:
                line += (
                    f", active_tokens {change['tokens_before']}"
                    f" -> {change['tokens_after']}"
                )
            self.stdout.write(line)

        for orphan in orphans:
            self.stdout.write(
                f"orphan {orphan['kind']} target '{orphan['target_pod_ip']}' "
                f"({orphan['count']} record(s)) matches no active server"
            )

        if changes:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'Corrected' if fix else 'Would correct'} {len(changes)} server(s)"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("All server workloads already match in-flight requests")
            )

        if not fix and changes:
            raise SystemExit(1)
