import pytest
from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import connection

from router.models import RequestRecord, Server

postgres_only = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL (uses DROP CONSTRAINT / pg_index)",
)


@pytest.fixture(scope="module", autouse=True)
def all_router_tables():
    models = list(apps.get_app_config("router").get_models())
    existing_tables = connection.introspection.table_names()
    with connection.schema_editor() as schema_editor:
        for model in models:
            if model._meta.db_table not in existing_tables:
                schema_editor.create_model(model)
    yield


@pytest.fixture(autouse=True)
def remove_metrics_port():
    drop_metrics_port()
    yield
    drop_metrics_port()


def drop_metrics_port():
    if has_column("servers", "metrics_port"):
        if connection.vendor == "sqlite":
            recreate_sqlite_table_without_extra_column(Server, "metrics_port")
            return
        with connection.schema_editor() as schema_editor:
            schema_editor.execute('ALTER TABLE "servers" DROP COLUMN "metrics_port";')


def add_metrics_port():
    with connection.cursor() as cursor:
        cursor.execute('ALTER TABLE "servers" ADD COLUMN "metrics_port" INTEGER;')


def drop_index(index_name):
    with connection.schema_editor() as schema_editor:
        schema_editor.execute(f'DROP INDEX IF EXISTS "{index_name}";')


def create_request_index(index_name):
    index = next(index for index in RequestRecord._meta.indexes if index.name == index_name)
    with connection.schema_editor() as schema_editor:
        schema_editor.execute(str(index.create_sql(RequestRecord, schema_editor)))


def has_index(index_name):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = %s
            """,
            [index_name],
        )
        return cursor.fetchone() is not None


def has_column(table, column):
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table)
    return column in {item.name for item in description}


def recreate_sqlite_table_without_extra_column(model, extra_column):
    table = model._meta.db_table
    temp_table = f"{table}_without_{extra_column}"
    quote_name = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {quote_name(temp_table)}")

    old_table = model._meta.db_table
    model._meta.db_table = temp_table
    try:
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(model)
    finally:
        model._meta.db_table = old_table

    columns = [
        field.column
        for field in model._meta.local_concrete_fields
        if has_column(table, field.column)
    ]
    quoted_columns = ", ".join(quote_name(column) for column in columns)
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {quote_name(temp_table)} ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM {quote_name(table)}"
        )
        cursor.execute(f"DROP TABLE {quote_name(table)}")
        cursor.execute(f"ALTER TABLE {quote_name(temp_table)} RENAME TO {quote_name(table)}")


def test_check_db_schema_succeeds_when_schema_matches(capsys):
    call_command("check_db_schema")

    output = capsys.readouterr()
    assert "Database schema matches Django model definitions" in output.out


def test_check_db_schema_reports_validated_database_target(capsys):
    call_command("check_db_schema")

    output = capsys.readouterr()
    # A "matches" verdict must be attributable: which database and which
    # model definitions were actually compared.
    assert "Validating schema of database" in output.out
    assert settings.DATABASES["default"]["NAME"] in output.out
    assert "models defined in" in output.out
    assert "router/models.py" in output.out


@postgres_only
def test_check_db_schema_reports_search_path_and_resolved_tables(capsys):
    call_command("check_db_schema")

    output = capsys.readouterr()
    # Table resolution is search_path-dependent; the report must show the
    # schema each model table actually resolves to, so a checker/app
    # search_path mismatch cannot hide behind a false "matches".
    assert "PostgreSQL search_path:" in output.out
    assert "Resolved table locations" in output.out
    assert "requests -> " in output.out
    assert "models -> " in output.out


@postgres_only
def test_check_db_schema_bare_dry_run_previews_fix_sql(capsys):
    # With drift present, plain `--dry-run` (no --fix) prints the SQL that
    # would be applied, without touching the database.
    index_name = "idx_requests_processing_target"
    drop_index(index_name)
    try:
        with pytest.raises(SystemExit):
            call_command("check_db_schema", "--dry-run")

        output = capsys.readouterr()
        assert f"Missing index in requests: {index_name}" in output.err
        assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in output.out
        assert not has_index(index_name)
    finally:
        if not has_index(index_name):
            create_request_index(index_name)


def test_user_visit_counts_is_not_required(capsys):
    assert "user_visit_counts" not in connection.introspection.table_names()

    call_command("check_db_schema")

    output = capsys.readouterr()
    assert "Database schema matches Django model definitions" in output.out


@postgres_only
def test_check_db_schema_reports_extra_column(capsys):
    add_metrics_port()

    with pytest.raises(SystemExit):
        call_command("check_db_schema")

    output = capsys.readouterr()
    assert "Extra columns in servers: metrics_port" in output.err
    assert has_column("servers", "metrics_port")


@postgres_only
def test_check_db_schema_dry_run_does_not_drop_extra_column(capsys):
    add_metrics_port()

    with pytest.raises(SystemExit):
        call_command("check_db_schema", "--fix", "--dry-run")

    output = capsys.readouterr()
    assert "Extra columns in servers: metrics_port" in output.err
    assert 'ALTER TABLE "servers" DROP COLUMN "metrics_port";' in output.out
    assert has_column("servers", "metrics_port")


@postgres_only
def test_check_db_schema_fix_drops_extra_column(capsys):
    add_metrics_port()

    call_command("check_db_schema", "--fix")

    output = capsys.readouterr()
    assert "Extra columns in servers: metrics_port" in output.err
    assert 'ALTER TABLE "servers" DROP COLUMN "metrics_port";' in output.out
    assert not has_column("servers", "metrics_port")


@postgres_only
def test_check_db_schema_reports_missing_unique_constraint(capsys):
    with connection.schema_editor() as schema_editor:
        schema_editor.execute('ALTER TABLE "servers" DROP CONSTRAINT IF EXISTS "servers_base_url_key";')

    with pytest.raises(SystemExit):
        call_command("check_db_schema")

    output = capsys.readouterr()
    assert "Missing unique constraint in servers.base_url" in output.err

    # Restore
    with connection.schema_editor() as schema_editor:
        schema_editor.execute('ALTER TABLE "servers" ADD CONSTRAINT "servers_base_url_key" UNIQUE ("base_url");')


@postgres_only
def test_check_db_schema_fix_adds_missing_unique_constraint(capsys):
    with connection.schema_editor() as schema_editor:
        schema_editor.execute('ALTER TABLE "servers" DROP CONSTRAINT IF EXISTS "servers_base_url_key";')

    call_command("check_db_schema", "--fix")

    output = capsys.readouterr()
    assert 'ADD CONSTRAINT "servers_base_url_key" UNIQUE ("base_url")' in output.out

    # Verify constraint was added
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            JOIN pg_class c ON c.oid = i.indrelid
            WHERE c.relname = 'servers' AND a.attname = 'base_url'
              AND i.indisunique = true AND i.indisprimary = false
            """
        )
        assert cursor.fetchone() is not None


@postgres_only
def test_check_db_schema_reports_missing_request_index(capsys):
    index_name = "idx_requests_processing_target"
    drop_index(index_name)

    try:
        with pytest.raises(SystemExit):
            call_command("check_db_schema", "--dry-run")

        output = capsys.readouterr()
        assert f"Missing index in requests: {index_name}" in output.err
        assert not has_index(index_name)
    finally:
        if not has_index(index_name):
            create_request_index(index_name)


@postgres_only
def test_check_db_schema_fix_dry_run_does_not_create_missing_request_index(capsys):
    index_name = "idx_requests_processing_target"
    drop_index(index_name)

    try:
        with pytest.raises(SystemExit):
            call_command("check_db_schema", "--fix", "--dry-run")

        output = capsys.readouterr()
        assert f"Missing index in requests: {index_name}" in output.err
        assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in output.out
        assert f'"{index_name}"' in output.out
        assert not has_index(index_name)
    finally:
        if not has_index(index_name):
            create_request_index(index_name)


@postgres_only
def test_check_db_schema_fix_creates_missing_request_index(capsys):
    index_name = "idx_requests_processing_target"
    drop_index(index_name)

    call_command("check_db_schema", "--fix")

    output = capsys.readouterr()
    assert f"Missing index in requests: {index_name}" in output.err
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in output.out
    assert f'"{index_name}"' in output.out
    assert has_index(index_name)
