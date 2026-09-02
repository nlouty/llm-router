from django.core.management.base import BaseCommand
from django.utils import timezone
from router.services.cmdb import CMDBService
from router.repositories.ips import IPRepository
from router.repositories.user_ips import UserIPRepository
from router.config import APP_CONFIG


class Command(BaseCommand):
    help = "Refresh user_ips table from CMDB source. Supports dry-run to generate SQL."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Do not update DB, just print the SQL commands.")
        parser.add_argument(
            "--ip",
            nargs="?",
            const="all",
            help="Refresh only IP-backed rows: every active IP when bare, or one address with --ip <address>.",
        )
        parser.add_argument("--apikey", action="store_true", help="Refresh only API-key-backed rows.")

    def handle(self, *args, **options):
        if not APP_CONFIG.get("cmdb", {}).get("enabled", False):
            self.stdout.write(self.style.ERROR("CMDB is not enabled in config.yaml"))
            return

        dry_run = options.get("dry_run")
        ip_filter = options.get("ip")
        apikey_only = bool(options.get("apikey"))
        single_ip = bool(ip_filter) and ip_filter != "all"

        if single_ip:
            ip_rows = [ip for ip in IPRepository.all_active() if ip.ip == ip_filter]
            if not ip_rows:
                self.stdout.write(self.style.WARNING(f"IP {ip_filter} not found or inactive."))
                return
        elif ip_filter or not apikey_only:
            ip_rows = IPRepository.all_active()
        else:
            ip_rows = []

        service = CMDBService()
        now = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
        sql_commands = []

        # IP-backed rows: ip / ip_id fixed, apikey empty
        if not self._refresh_ip_backed(service, ip_rows, dry_run, now, sql_commands):
            return

        # API-key-backed rows: apikey / employee_no fixed, ip_id = 0
        # (full refresh with no --ip, or on explicit --apikey request)
        if apikey_only or not ip_filter:
            if not self._refresh_apikey_backed(service, dry_run, now, sql_commands):
                return

        if dry_run and sql_commands:
            self.stdout.write("\n-- GENERATED SQL COMMANDS --")
            for cmd in sql_commands:
                self.stdout.write(cmd)

            self.stdout.write("\n" + "=" * 40)
            self.stdout.write("To run these commands manually against the database:")
            self.stdout.write("1. Save the SQL to a file (e.g., updates.sql)")
            self.stdout.write("2. Run: psql -h <db_host> -p <db_port> -U <user> -d <db_name> -f updates.sql")
            self.stdout.write("=" * 40)
        elif dry_run:
            self.stdout.write("No updates needed or no data found to generate SQL.")

    def _refresh_ip_backed(self, service, ip_rows, dry_run, now_str, sql_commands) -> bool:
        for ip_row in ip_rows:
            try:
                user_data = service.fetch_user_data(ip_row.ip)
            except NotImplementedError:
                self.stdout.write(self.style.ERROR(
                    "CMDBService does not implement 'fetch_user_data(ip) -> dict'. "
                    "Implement it in 'router/services/cmdb.py' to refresh IP-backed rows."
                ))
                return False
            if not user_data:
                self.stdout.write(f"Skipping {ip_row.ip}: no data from CMDB")
                continue

            if dry_run:
                sql_commands.append(self._generate_ip_upsert_sql(ip_row.id, user_data, now_str))
            else:
                UserIPRepository.create_or_update(
                    ip_id=ip_row.id,
                    user_name=user_data.get("user_name", ""),
                    user_charge=user_data.get("user_charge", ""),
                    employee_no=user_data.get("employee_no", ""),
                    department_id=user_data.get("department_id"),
                    vip=bool(user_data.get("vip")),
                )
                self.stdout.write(f"Successfully refreshed {ip_row.ip}")
        return True

    def _refresh_apikey_backed(self, service, dry_run, now_str, sql_commands) -> bool:
        for row in UserIPRepository.all_active_apikeys():
            try:
                user_data = service.fetch_user_data_by_employee_no(row.employee_no)
            except NotImplementedError:
                self.stdout.write(self.style.ERROR(
                    "CMDBService does not implement 'fetch_user_data_by_employee_no(employee_no) -> dict'. "
                    "Implement it in 'router/services/cmdb.py' to refresh API-key-backed rows."
                ))
                return False
            if not user_data:
                self.stdout.write(f"Skipping apikey for {row.employee_no}: no data from CMDB")
                continue

            if dry_run:
                sql_commands.append(self._generate_apikey_upsert_sql(row, user_data, now_str))
            else:
                UserIPRepository.create_or_update_apikey(
                    apikey=row.apikey,
                    employee_no=row.employee_no,
                    user_name=user_data.get("user_name", ""),
                    user_charge=user_data.get("user_charge", ""),
                    department_id=user_data.get("department_id"),
                    vip=bool(user_data.get("vip")),
                )
                self.stdout.write(f"Successfully refreshed apikey for {row.employee_no}")
        return True

    def _generate_ip_upsert_sql(self, ip_id, user_data, now_str):
        user_name = user_data.get("user_name", "").replace("'", "''")
        user_charge = user_data.get("user_charge", "").replace("'", "''")
        employee_no = user_data.get("employee_no", "").replace("'", "''")
        dept_id = user_data.get("department_id")
        dept_val = str(dept_id) if dept_id is not None else "NULL"
        vip = "true" if user_data.get("vip") else "false"

        return (
            f"INSERT INTO user_ips (ip_id, user_name, user_charge, employee_no, department_id, vip, is_valid, created_at, updated_at) "
            f"VALUES ({ip_id}, '{user_name}', '{user_charge}', '{employee_no}', {dept_val}, {vip}, true, '{now_str}', '{now_str}') "
            f"ON CONFLICT (ip_id) WHERE ip_id > 0 DO UPDATE SET "
            f"user_name = EXCLUDED.user_name, "
            f"user_charge = EXCLUDED.user_charge, "
            f"employee_no = EXCLUDED.employee_no, "
            f"department_id = EXCLUDED.department_id, "
            f"vip = EXCLUDED.vip, "
            f"updated_at = EXCLUDED.updated_at;\n"
        )

    def _generate_apikey_upsert_sql(self, row, user_data, now_str):
        apikey = row.apikey.replace("'", "''")
        employee_no = row.employee_no.replace("'", "''")
        user_name = user_data.get("user_name", "").replace("'", "''")
        user_charge = user_data.get("user_charge", "").replace("'", "''")
        dept_id = user_data.get("department_id")
        dept_val = str(dept_id) if dept_id is not None else "NULL"
        vip = "true" if user_data.get("vip") else "false"

        return (
            f"INSERT INTO user_ips (ip_id, apikey, employee_no, user_name, user_charge, department_id, vip, is_valid, created_at, updated_at) "
            f"VALUES (0, '{apikey}', '{employee_no}', '{user_name}', '{user_charge}', {dept_val}, {vip}, true, '{now_str}', '{now_str}') "
            f"ON CONFLICT (apikey) WHERE apikey <> '' DO UPDATE SET "
            f"user_name = EXCLUDED.user_name, "
            f"user_charge = EXCLUDED.user_charge, "
            f"department_id = EXCLUDED.department_id, "
            f"vip = EXCLUDED.vip, "
            f"updated_at = EXCLUDED.updated_at;\n"
        )
