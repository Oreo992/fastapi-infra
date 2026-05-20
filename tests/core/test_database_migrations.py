from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from infra.database import (
    MigrationError,
    SqlMigrationRunner,
    create_sql_migration,
    load_sql_migrations,
    normalize_migration_name,
    split_sql_statements,
)


class FakeMigrationDatabase:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, object, bool, bool]] = []
        self.events: list[str] = []
        self.in_transaction = False
        self.fail_on: str | None = None

    async def execute_sql(self, sql: str, params=None, commit: bool = True) -> int:
        normalized_sql = " ".join(sql.split())
        self.executed.append((normalized_sql, params, commit, self.in_transaction))
        self.events.append(f"execute:{normalized_sql}")
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("forced migration failure")
        if "INSERT INTO infra_schema_migrations" in sql:
            version, name, checksum, applied_at = params
            self.rows.append(
                {
                    "version": version,
                    "name": name,
                    "checksum": checksum,
                    "applied_at": applied_at,
                }
            )
        return 1

    async def fetch_all(self, sql: str, params=None) -> list[dict[str, object]]:
        self.events.append(f"fetch:{' '.join(sql.split())}")
        return list(self.rows)

    def transaction(self) -> "FakeMigrationTransaction":
        return FakeMigrationTransaction(self)


class FakeMigrationTransaction:
    def __init__(self, database: FakeMigrationDatabase) -> None:
        self.database = database

    async def __aenter__(self) -> FakeMigrationDatabase:
        self.database.events.append("begin")
        self.database.in_transaction = True
        return self.database

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.database.events.append("rollback" if exc_type else "commit")
        self.database.in_transaction = False


class FakeMigrationLock:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def acquire(self) -> None:
        self.events.append("acquire-lock")

    async def release(self) -> None:
        self.events.append("release-lock")


def write_migration(root: Path, name: str, sql: str) -> Path:
    path = root / name
    path.write_text(sql, encoding="utf-8")
    return path


def test_split_sql_statements_handles_comments_and_quoted_semicolons():
    sql = """
    -- ignored comment;
    CREATE TABLE users (id VARCHAR(32), note VARCHAR(255));
    INSERT INTO users (id, note) VALUES ('1', 'semi;colon');
    /* ignored; block */
    """

    assert split_sql_statements(sql) == [
        "CREATE TABLE users (id VARCHAR(32), note VARCHAR(255))",
        "INSERT INTO users (id, note) VALUES ('1', 'semi;colon')",
    ]


def test_create_sql_migration_uses_timestamp_and_normalized_name(tmp_path):
    path = create_sql_migration(
        tmp_path,
        "Create Users",
        now=datetime(2026, 5, 12, 1, 2, 3, tzinfo=UTC),
    )

    assert path.name == "20260512010203_create_users.sql"
    assert path.read_text(encoding="utf-8").startswith("-- Write migration SQL")


def test_load_sql_migrations_orders_and_checksums(tmp_path):
    write_migration(tmp_path, "20260512010204_add_email.sql", "ALTER TABLE users ADD email TEXT;")
    write_migration(tmp_path, "20260512010203_create_users.sql", "CREATE TABLE users (id TEXT);")

    migrations = load_sql_migrations(tmp_path)

    assert [migration.name for migration in migrations] == ["create_users", "add_email"]
    assert all(len(migration.checksum) == 64 for migration in migrations)


def test_load_sql_migrations_rejects_bad_filename(tmp_path):
    write_migration(tmp_path, "bad.sql", "SELECT 1;")

    with pytest.raises(MigrationError, match="invalid migration filename"):
        load_sql_migrations(tmp_path)


def test_load_sql_migrations_rejects_empty_file_by_default(tmp_path):
    write_migration(tmp_path, "20260512010203_empty.sql", "-- comment only\n")

    with pytest.raises(MigrationError, match="no SQL statements"):
        load_sql_migrations(tmp_path)


@pytest.mark.asyncio
async def test_sql_migration_runner_applies_pending_migrations(tmp_path):
    write_migration(tmp_path, "20260512010203_create_users.sql", "CREATE TABLE users (id TEXT);")
    database = FakeMigrationDatabase()

    applied = await SqlMigrationRunner(database, tmp_path).migrate()

    assert [migration.name for migration in applied] == ["create_users"]
    assert any("CREATE TABLE users" in sql for sql, _params, _commit, _in_tx in database.executed)
    assert database.rows[0]["version"] == "20260512010203"


@pytest.mark.asyncio
async def test_sql_migration_runner_acquires_and_releases_lock_around_migrate(tmp_path):
    write_migration(tmp_path, "20260512010203_create_users.sql", "CREATE TABLE users (id TEXT);")
    database = FakeMigrationDatabase()
    lock = FakeMigrationLock(database.events)

    await SqlMigrationRunner(database, tmp_path, lock=lock).migrate()

    assert database.events[0] == "acquire-lock"
    assert database.events[-1] == "release-lock"


@pytest.mark.asyncio
async def test_sql_migration_runner_executes_migration_and_version_insert_in_transaction(tmp_path):
    write_migration(tmp_path, "20260512010203_create_users.sql", "CREATE TABLE users (id TEXT);")
    database = FakeMigrationDatabase()

    await SqlMigrationRunner(database, tmp_path, transaction_factory=database.transaction).migrate()

    create_table = next(event for event in database.executed if "CREATE TABLE users" in event[0])
    version_insert = next(
        event for event in database.executed if "INSERT INTO infra_schema_migrations" in event[0]
    )
    assert create_table[2:] == (False, True)
    assert version_insert[2:] == (False, True)
    assert "commit" in database.events


@pytest.mark.asyncio
async def test_sql_migration_runner_releases_lock_when_migration_fails(tmp_path):
    write_migration(tmp_path, "20260512010203_create_users.sql", "CREATE TABLE users (id TEXT);")
    database = FakeMigrationDatabase()
    database.fail_on = "CREATE TABLE users"
    lock = FakeMigrationLock(database.events)

    with pytest.raises(RuntimeError, match="forced migration failure"):
        await SqlMigrationRunner(database, tmp_path, lock=lock).migrate()

    assert database.events[0] == "acquire-lock"
    assert database.events[-1] == "release-lock"


@pytest.mark.asyncio
async def test_sql_migration_runner_rejects_changed_applied_checksum(tmp_path):
    path = write_migration(
        tmp_path,
        "20260512010203_create_users.sql",
        "CREATE TABLE users (id TEXT);",
    )
    migration = load_sql_migrations(tmp_path)[0]
    database = FakeMigrationDatabase(
        [
            {
                "version": migration.version,
                "name": migration.name,
                "checksum": "different",
                "applied_at": "2026-05-12T01:02:03+00:00",
            }
        ]
    )
    path.write_text("CREATE TABLE users (id BIGINT);", encoding="utf-8")

    with pytest.raises(MigrationError, match="checksum changed"):
        await SqlMigrationRunner(database, tmp_path).pending()


def test_normalize_migration_name_rejects_empty_name():
    with pytest.raises(ValueError):
        normalize_migration_name("!!!")
