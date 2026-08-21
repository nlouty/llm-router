from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Check required table columns, constraints, and indexes against Django model definitions."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true", help="Drop extra columns and column defaults that are not defined by the models.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        models = list(apps.get_app_config("router").get_models())
        fix = options.get("fix")
        dry_run = options.get("dry_run")

        # Say exactly which database and which model definitions are being
        # compared. A "matches" verdict is only meaningful for the database
        # and code the app itself uses: running this command with a different
        # DB_* environment (or with USE_SQLITE_FOR_TESTS leaking in) or from
        # an older checkout compares the wrong pair and produces false
        # reassurance while the live app fails with UndefinedColumn.
        db_config = settings.DATABASES["default"]
        app_config = apps.get_app_config("router")
        self.stdout.write(
            "Validating schema of database "
            f"'{db_config.get('NAME')}' at {db_config.get('HOST') or 'localhost'}:{db_config.get('PORT') or 'default'} "
            f"(engine={db_config.get('ENGINE')}, user={db_config.get('USER')}) "
            f"against {len(models)} models defined in {app_config.models_module.__file__}"
        )

        pass_count = 0
        while True:
            drift = self._inspect_schema(models)
            if not any(drift.values()):
                break

            if pass_count == 0:
                self._write_drift(drift)

            if dry_run:
                # Preview the SQL without touching the database, even without
                # --fix, so `check_db_schema --dry-run` alone already shows
                # what would be applied.
                self._apply_fixes(drift, models, dry_run=True)
                raise SystemExit(1)

            if not fix:
                self.stderr.write(
                    "Run 'check_db_schema --fix --dry-run' to preview the SQL that would be "
                    "applied, then 'check_db_schema --fix' to apply it."
                )
                raise SystemExit(1)

            self._apply_fixes(drift, models, dry_run=False)
            pass_count += 1
            if pass_count > 5:  # Prevent infinite loop
                self.stderr.write("Still have drift after 5 passes, stopping.")
                raise SystemExit(1)
            self.stdout.write("Applied fixes, re-checking schema...")

        self.stdout.write(self.style.SUCCESS("Database schema matches Django model definitions"))

    def _apply_fixes(self, drift, models, dry_run):
        for table in drift["missing_tables"]:
            model = {m._meta.db_table: m for m in models}[table]
            sql = self._create_table_sql(model)
            self.stdout.write(sql)
            if not dry_run:
                with connection.schema_editor() as schema_editor:
                    schema_editor.execute(sql)

        for table, columns in drift["missing_columns"].items():
            model = {m._meta.db_table: m for m in models}[table]
            for column in columns:
                sql = self._add_column_sql(model, column)
                self.stdout.write(sql)
                if not dry_run:
                    with connection.schema_editor() as schema_editor:
                        schema_editor.execute(sql)

        for table, columns in drift["extra_columns"].items():
            for column in columns:
                sql = self._drop_column_sql(table, column)
                self.stdout.write(sql)
                if not dry_run:
                    with connection.schema_editor() as schema_editor:
                        schema_editor.execute(sql)

        for table, columns in drift["extra_defaults"].items():
            for column in columns:
                sql = self._drop_default_sql(table, column)
                self.stdout.write(sql)
                if not dry_run:
                    with connection.schema_editor() as schema_editor:
                        schema_editor.execute(sql)

        for table, columns in drift["missing_auto_increment"].items():
            for column in columns:
                sql = self._add_auto_increment_sql(table, column)
                self.stdout.write(sql)
                if not dry_run:
                    with connection.schema_editor() as schema_editor:
                        schema_editor.execute(sql)

        for table, mismatches in drift["nullable_mismatches"].items():
            for column, expected_null in mismatches.items():
                sql = self._alter_nullable_sql(table, column, expected_null)
                self.stdout.write(sql)
                if not dry_run:
                    with connection.schema_editor() as schema_editor:
                        schema_editor.execute(sql)

        for table, mismatches in drift["type_mismatches"].items():
            for column, info in mismatches.items():
                sql = self._alter_type_sql(table, column, info["expected"])
                self.stdout.write(sql)
                if not dry_run:
                    with connection.schema_editor() as schema_editor:
                        schema_editor.execute(sql)

        for table, mismatches in drift["unique_mismatches"].items():
            for column, status in mismatches.items():
                if status == "missing":
                    sql = self._add_unique_sql(table, column)
                else:
                    sql = self._drop_unique_sql(table, column)
                self.stdout.write(sql)
                if not dry_run:
                    with connection.schema_editor() as schema_editor:
                        schema_editor.execute(sql)

        for table, constraints in drift["missing_constraints"].items():
            model = {m._meta.db_table: m for m in models}[table]
            for constraint in constraints:
                sql = self._create_constraint_sql(model, constraint)
                self.stdout.write(sql)
                if not dry_run:
                    self._execute_schema_sql(sql)

        for table, indexes in drift["missing_indexes"].items():
            model = {m._meta.db_table: m for m in models}[table]
            for index in indexes:
                sql = self._create_index_sql(model, index)
                self.stdout.write(sql)
                if not dry_run:
                    self._execute_index_sql(sql)

    def _inspect_schema(self, models):
        drift = {
            "missing_tables": [],
            "missing_columns": {},
            "extra_columns": {},
            "extra_defaults": {},
            "missing_auto_increment": {},
            "nullable_mismatches": {},
            "type_mismatches": {},
            "unique_mismatches": {},
            "missing_constraints": {},
            "missing_indexes": {},
        }
        expected_tables = {model._meta.db_table: model for model in models}

        with connection.cursor() as cursor:
            existing_tables = set(connection.introspection.table_names(cursor))
            for table, model in expected_tables.items():
                if table not in existing_tables:
                    drift["missing_tables"].append(table)
                    continue

                expected_columns = self._expected_columns(model)
                description = connection.introspection.get_table_description(cursor, table)
                actual_columns = {column.name for column in description}
                actual_nullable = {column.name: column.null_ok for column in description}
                missing_columns = sorted(expected_columns - actual_columns)
                extra_columns = sorted(actual_columns - expected_columns)

                if missing_columns:
                    drift["missing_columns"][table] = missing_columns
                if extra_columns:
                    drift["extra_columns"][table] = extra_columns

                nullable_mismatches = self._nullable_mismatches(model, actual_columns, actual_nullable)
                if nullable_mismatches:
                    drift["nullable_mismatches"][table] = nullable_mismatches

                type_mismatches = self._type_mismatches(cursor, table, model, actual_columns)
                if type_mismatches:
                    drift["type_mismatches"][table] = type_mismatches

                extra_defaults = self._extra_defaults(cursor, table, model, actual_columns)
                if extra_defaults:
                    drift["extra_defaults"][table] = extra_defaults

                missing_auto = self._missing_auto_increment(cursor, table, model, actual_columns)
                if missing_auto:
                    drift["missing_auto_increment"][table] = missing_auto

                unique_mismatches = self._unique_mismatches(cursor, table, model, actual_columns)
                if unique_mismatches:
                    drift["unique_mismatches"][table] = unique_mismatches

                missing_constraints = self._missing_constraints(cursor, table, model)
                if missing_constraints:
                    drift["missing_constraints"][table] = missing_constraints

                missing_indexes = self._missing_indexes(cursor, table, model)
                if missing_indexes:
                    drift["missing_indexes"][table] = missing_indexes

        drift["missing_tables"].sort()
        return drift

    def _expected_columns(self, model):
        return {field.column for field in model._meta.local_concrete_fields}

    def _extra_defaults(self, cursor, table, model, actual_columns):
        if connection.vendor != "postgresql":
            return []

        fields_without_defaults = [
            field.column
            for field in model._meta.local_concrete_fields
            if field.column in actual_columns and not field.primary_key and not field.has_default()
        ]
        if not fields_without_defaults:
            return []

        placeholders = ", ".join(["%s"] * len(fields_without_defaults))
        cursor.execute(
            f"""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_name IN ({placeholders})
              AND column_default IS NOT NULL
            """,
            [table, *fields_without_defaults],
        )
        return sorted(row[0] for row in cursor.fetchall())

    def _missing_auto_increment(self, cursor, table, model, actual_columns):
        if connection.vendor != "postgresql":
            return []

        pk_field = model._meta.pk
        if pk_field.column not in actual_columns:
            return []

        from django.db.models import AutoField, BigAutoField
        if not isinstance(pk_field, (AutoField, BigAutoField)):
            return []

        cursor.execute(
            """
            SELECT column_default, is_identity
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_name = %s
            """,
            [table, pk_field.column],
        )
        row = cursor.fetchone()
        if row and not row[0] and row[1] == "NO":
            return [pk_field.column]
        return []

    def _add_auto_increment_sql(self, table, column):
        quote_name = connection.ops.quote_name
        return f"ALTER TABLE {quote_name(table)} ALTER COLUMN {quote_name(column)} ADD GENERATED BY DEFAULT AS IDENTITY;"

    def _nullable_mismatches(self, model, actual_columns, actual_nullable):
        mismatches = {}
        for field in model._meta.local_concrete_fields:
            if field.column not in actual_columns or field.primary_key:
                continue
            expected_null = field.null
            db_null = actual_nullable.get(field.column)
            if db_null is not None and expected_null != db_null:
                mismatches[field.column] = expected_null
        return mismatches

    def _type_mismatches(self, cursor, table, model, actual_columns):
        if connection.vendor != "postgresql":
            return {}
        field_map = {f.column: f for f in model._meta.local_concrete_fields}
        columns_to_check = [c for c in actual_columns if c in field_map and not field_map[c].primary_key]
        if not columns_to_check:
            return {}
        placeholders = ", ".join(["%s"] * len(columns_to_check))
        cursor.execute(
            f"""
            SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_name IN ({placeholders})
            """,
            [table, *columns_to_check],
        )
        mismatches = {}
        for row in cursor.fetchall():
            col_name, data_type, char_max_len, num_prec, num_scale = row
            field = field_map[col_name]
            expected_type = field.db_type(connection)
            if not expected_type:
                continue
            actual_type = self._reconstruct_type(data_type, char_max_len, num_prec, num_scale)
            if actual_type != expected_type:
                mismatches[col_name] = {"actual": actual_type, "expected": expected_type}
        return mismatches

    @staticmethod
    def _reconstruct_type(data_type, char_max_len, num_prec, num_scale):
        type_map = {
            "character varying": "varchar",
            "character": "char",
            "integer": "integer",
            "bigint": "bigint",
            "smallint": "smallint",
            "boolean": "boolean",
            "text": "text",
            "double precision": "double precision",
            "real": "real",
            "date": "date",
            "timestamp with time zone": "timestamp with time zone",
            "timestamp without time zone": "timestamp without time zone",
            "jsonb": "jsonb",
        }
        base = type_map.get(data_type, data_type)
        if base in ("varchar", "char") and char_max_len is not None:
            return f"{base}({char_max_len})"
        if base == "numeric" and num_prec is not None:
            if num_scale:
                return f"numeric({num_prec}, {num_scale})"
            return f"numeric({num_prec})"
        return base

    def _unique_mismatches(self, cursor, table, model, actual_columns):
        if connection.vendor != "postgresql":
            return {}
        # Get columns that have single-column unique constraints in the DB
        cursor.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s
              AND n.nspname = current_schema()
              AND i.indisunique = true
              AND i.indisprimary = false
              AND i.indpred IS NULL
              AND array_length(i.indkey, 1) = 1
            """,
            [table],
        )
        db_unique_columns = {row[0] for row in cursor.fetchall()}

        mismatches = {}
        for field in model._meta.local_concrete_fields:
            if field.column not in actual_columns or field.primary_key:
                continue
            if field.unique and field.column not in db_unique_columns:
                mismatches[field.column] = "missing"
            elif not field.unique and field.column in db_unique_columns:
                mismatches[field.column] = "extra"
        return mismatches

    def _add_unique_sql(self, table, column):
        quote_name = connection.ops.quote_name
        constraint_name = f"{table}_{column}_key"
        return f"ALTER TABLE {quote_name(table)} ADD CONSTRAINT {quote_name(constraint_name)} UNIQUE ({quote_name(column)});"

    def _drop_unique_sql(self, table, column):
        quote_name = connection.ops.quote_name
        constraint_name = f"{table}_{column}_key"
        return f"ALTER TABLE {quote_name(table)} DROP CONSTRAINT IF EXISTS {quote_name(constraint_name)};"

    def _create_table_sql(self, model):
        quote_name = connection.ops.quote_name
        table = model._meta.db_table
        col_defs = []
        for field in model._meta.local_concrete_fields:
            col_type = field.db_type(connection)
            if not col_type:
                continue
            parts = [quote_name(field.column), col_type]
            if field.primary_key:
                parts.append("PRIMARY KEY")
            else:
                if not field.null:
                    parts.append("NOT NULL")
                if field.unique:
                    parts.append("UNIQUE")
                if field.has_default():
                    default = field.get_default()
                    if isinstance(default, str):
                        parts.append(f"DEFAULT '{default}'")
                    elif isinstance(default, bool):
                        parts.append(f"DEFAULT {'true' if default else 'false'}")
                    elif default is None:
                        parts.append("DEFAULT NULL")
                    else:
                        parts.append(f"DEFAULT {default}")
            col_defs.append(" ".join(parts))

        for unique_together in model._meta.unique_together:
            columns = ", ".join([quote_name(model._meta.get_field(f).column) for f in unique_together])
            col_defs.append(f"UNIQUE ({columns})")

        return f"CREATE TABLE {quote_name(table)} ({', '.join(col_defs)});"

    def _add_column_sql(self, model, column_name):
        quote_name = connection.ops.quote_name
        field = next(f for f in model._meta.local_concrete_fields if f.column == column_name)
        col_type = field.db_type(connection)
        parts = [f"ALTER TABLE {quote_name(model._meta.db_table)} ADD COLUMN {quote_name(column_name)} {col_type}"]
        if not field.null:
            parts.append("NOT NULL")
            if field.has_default():
                default = field.get_default()
                if isinstance(default, str):
                    parts.append(f"DEFAULT '{default}'")
                elif isinstance(default, bool):
                    parts.append(f"DEFAULT {'true' if default else 'false'}")
                else:
                    parts.append(f"DEFAULT {default}")
        return " ".join(parts) + ";"

    def _drop_column_sql(self, table, column):
        quote_name = connection.ops.quote_name
        return f"ALTER TABLE {quote_name(table)} DROP COLUMN {quote_name(column)};"

    def _drop_default_sql(self, table, column):
        quote_name = connection.ops.quote_name
        return f"ALTER TABLE {quote_name(table)} ALTER COLUMN {quote_name(column)} DROP DEFAULT;"

    def _alter_nullable_sql(self, table, column, expected_null):
        quote_name = connection.ops.quote_name
        action = "DROP NOT NULL" if expected_null else "SET NOT NULL"
        return f"ALTER TABLE {quote_name(table)} ALTER COLUMN {quote_name(column)} {action};"

    def _alter_type_sql(self, table, column, expected_type):
        quote_name = connection.ops.quote_name
        return (
            f"ALTER TABLE {quote_name(table)} ALTER COLUMN {quote_name(column)} "
            f"TYPE {expected_type} USING {quote_name(column)}::{expected_type};"
        )

    def _create_index_sql(self, model, index):
        with connection.schema_editor() as schema_editor:
            sql = str(index.create_sql(model, schema_editor))
        return self._concurrent_index_sql(sql)

    def _create_constraint_sql(self, model, constraint):
        with connection.schema_editor() as schema_editor:
            sql = str(constraint.create_sql(model, schema_editor))
        return self._concurrent_index_sql(sql)

    @staticmethod
    def _concurrent_index_sql(sql):
        sql = sql.rstrip(";")
        if connection.vendor == "postgresql":
            if sql.startswith("CREATE INDEX "):
                sql = sql.replace("CREATE INDEX ", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ", 1)
            elif sql.startswith("CREATE UNIQUE INDEX "):
                sql = sql.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ", 1)
        return sql + ";"

    def _execute_schema_sql(self, sql):
        if connection.vendor != "postgresql" or " CONCURRENTLY " not in sql:
            with connection.schema_editor() as schema_editor:
                schema_editor.execute(sql)
            return

        old_autocommit = connection.get_autocommit()
        if not old_autocommit:
            connection.set_autocommit(True)
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql)
        finally:
            if not old_autocommit:
                connection.set_autocommit(False)

    def _execute_index_sql(self, sql):
        self._execute_schema_sql(sql)

    def _write_drift(self, drift):
        if drift["missing_tables"]:
            self.stderr.write("Missing tables: " + ", ".join(drift["missing_tables"]))

        for table, columns in drift["missing_columns"].items():
            self.stderr.write(f"Missing columns in {table}: {', '.join(columns)}")

        for table, columns in drift["extra_columns"].items():
            self.stderr.write(f"Extra columns in {table}: {', '.join(columns)}")

        for table, columns in drift["extra_defaults"].items():
            self.stderr.write(f"Extra defaults in {table}: {', '.join(columns)}")

        for table, columns in drift["missing_auto_increment"].items():
            self.stderr.write(f"Missing auto-increment in {table}: {', '.join(columns)}")

        for table, mismatches in drift["nullable_mismatches"].items():
            for column, expected_null in mismatches.items():
                actual = "NULL" if not expected_null else "NOT NULL"
                expected = "NULL" if expected_null else "NOT NULL"
                self.stderr.write(f"Nullable mismatch in {table}.{column}: db is {actual}, model expects {expected}")

        for table, mismatches in drift["type_mismatches"].items():
            for column, info in mismatches.items():
                self.stderr.write(f"Type mismatch in {table}.{column}: db is {info['actual']}, model expects {info['expected']}")

        for table, mismatches in drift["unique_mismatches"].items():
            for column, status in mismatches.items():
                if status == "missing":
                    self.stderr.write(f"Missing unique constraint in {table}.{column}")
                else:
                    self.stderr.write(f"Extra unique constraint in {table}.{column}")

        for table, constraints in drift["missing_constraints"].items():
            for constraint in constraints:
                self.stderr.write(f"Missing constraint in {table}: {constraint.name}")

        for table, indexes in drift["missing_indexes"].items():
            for index in indexes:
                self.stderr.write(f"Missing index in {table}: {index.name}")

    def _missing_constraints(self, cursor, table, model):
        if connection.vendor != "postgresql":
            return []

        existing = connection.introspection.get_constraints(cursor, table)
        return [constraint for constraint in model._meta.constraints if constraint.name not in existing]

    def _missing_indexes(self, cursor, table, model):
        if connection.vendor != "postgresql":
            return []

        cursor.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = %s
            """,
            [table],
        )
        db_indexes = {row[0] for row in cursor.fetchall()}

        missing = []
        for index in model._meta.indexes:
            if index.name not in db_indexes:
                missing.append(index)
        return missing
