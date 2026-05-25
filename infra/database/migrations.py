from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

MIGRATION_TABLE = "infra_schema_migrations"
MIGRATION_FILE_RE = re.compile(r"^(?P<version>[0-9]{14})_(?P<name>[a-z0-9_]+)\.sql$")
MIGRATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SQL_QUOTE_CHARS = {"'", '"', "`"}


class MigrationExecutor(Protocol):
    async def execute_sql(
        self,
        sql: str,
        params: Any = None,
        commit: bool = True,
    ) -> int:
        raise NotImplementedError


@runtime_checkable
class MigrationDatabase(MigrationExecutor, Protocol):
    async def fetch_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        raise NotImplementedError


class MigrationLock(Protocol):
    async def acquire(self) -> None:
        raise NotImplementedError

    async def release(self) -> None:
        raise NotImplementedError


MigrationTransactionFactory: TypeAlias = Callable[
    [], AbstractAsyncContextManager[MigrationExecutor]
]


@dataclass(frozen=True)
class SqlMigration:
    version: str
    name: str
    path: Path
    checksum: str
    statements: tuple[str, ...]


@dataclass(frozen=True)
class MigrationRecord:
    version: str
    name: str
    checksum: str
    applied_at: str


class MigrationError(RuntimeError):
    pass


class SqlMigrationRunner:
    def __init__(
        self,
        database: MigrationDatabase,
        migrations_path: str | Path,
        *,
        table_name: str = MIGRATION_TABLE,
        lock: MigrationLock | None = None,
        transaction_factory: MigrationTransactionFactory | None = None,
    ) -> None:
        self.database = database
        self.migrations_path = Path(migrations_path)
        self.table_name = table_name
        self.lock = lock
        self.transaction_factory = transaction_factory

    async def ensure_table(self) -> None:
        await self.database.execute_sql(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                version VARCHAR(14) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                checksum CHAR(64) NOT NULL,
                applied_at VARCHAR(32) NOT NULL
            )
            """)

    async def applied(self) -> dict[str, MigrationRecord]:
        await self.ensure_table()
        rows = await self.database.fetch_all(
            f"SELECT version, name, checksum, applied_at FROM {self.table_name} ORDER BY version"
        )
        records: dict[str, MigrationRecord] = {}
        for row in rows:
            version = str(row["version"])
            records[version] = MigrationRecord(
                version=version,
                name=str(row["name"]),
                checksum=str(row["checksum"]),
                applied_at=str(row["applied_at"]),
            )
        return records

    async def pending(self) -> list[SqlMigration]:
        migrations = load_sql_migrations(self.migrations_path)
        applied = await self.applied()
        pending: list[SqlMigration] = []
        for migration in migrations:
            record = applied.get(migration.version)
            if record is None:
                pending.append(migration)
                continue
            if record.checksum != migration.checksum:
                raise MigrationError(
                    f"migration {migration.version}_{migration.name} checksum changed after apply"
                )
        return pending

    async def migrate(self) -> list[SqlMigration]:
        if self.lock is not None:
            await self.lock.acquire()
        try:
            return await self._migrate_locked()
        finally:
            if self.lock is not None:
                await self.lock.release()

    async def _migrate_locked(self) -> list[SqlMigration]:
        applied_now: list[SqlMigration] = []
        for migration in await self.pending():
            if self.transaction_factory is None:
                await self._apply_migration(migration, self.database, commit=True)
            else:
                async with self.transaction_factory() as executor:
                    await self._apply_migration(migration, executor, commit=False)
            applied_now.append(migration)
        return applied_now

    async def _apply_migration(
        self,
        migration: SqlMigration,
        executor: MigrationExecutor,
        *,
        commit: bool,
    ) -> None:
        for statement in migration.statements:
            await executor.execute_sql(statement, commit=commit)
        await executor.execute_sql(
            f"""
            INSERT INTO {self.table_name} (version, name, checksum, applied_at)
            VALUES (%s, %s, %s, %s)
            """,
            (
                migration.version,
                migration.name,
                migration.checksum,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
            commit=commit,
        )


def create_sql_migration(
    migrations_path: str | Path,
    name: str,
    *,
    now: datetime | None = None,
) -> Path:
    normalized = normalize_migration_name(name)
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    path = Path(migrations_path) / f"{timestamp}_{normalized}.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"migration already exists: {path}")
    path.write_text("-- Write migration SQL here.\n", encoding="utf-8")
    return path


def load_sql_migrations(
    migrations_path: str | Path,
    *,
    allow_empty: bool = False,
) -> list[SqlMigration]:
    root = Path(migrations_path)
    if not root.exists():
        return []
    if not root.is_dir():
        raise MigrationError(f"migrations path is not a directory: {root}")

    migrations: list[SqlMigration] = []
    seen_versions: set[str] = set()
    for path in sorted(root.glob("*.sql")):
        match = MIGRATION_FILE_RE.fullmatch(path.name)
        if match is None:
            raise MigrationError(
                f"invalid migration filename {path.name}; expected YYYYMMDDHHMMSS_name.sql"
            )
        version = match.group("version")
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version: {version}")
        seen_versions.add(version)

        sql = path.read_text(encoding="utf-8")
        statements = split_sql_statements(sql)
        if not statements and not allow_empty:
            raise MigrationError(f"migration has no SQL statements: {path}")
        migrations.append(
            SqlMigration(
                version=version,
                name=match.group("name"),
                path=path,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                statements=tuple(statements),
            )
        )
    return migrations


def normalize_migration_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if not normalized or not MIGRATION_NAME_RE.fullmatch(normalized):
        raise ValueError("migration name must contain lowercase letters or numbers")
    return normalized


def split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    index = 0

    while index < len(sql):
        char = sql[index]
        next_char = _next_sql_char(sql, index)

        if in_line_comment:
            index, in_line_comment = _consume_sql_line_comment(sql, index, buffer)
            continue

        if in_block_comment:
            index, in_block_comment = _consume_sql_block_comment(sql, index)
            continue

        if quote is None and _starts_sql_line_comment(char, next_char):
            in_line_comment = True
            index += 2
            continue
        if quote is None and _starts_sql_block_comment(char, next_char):
            in_block_comment = True
            index += 2
            continue

        if char in SQL_QUOTE_CHARS:
            quote = _updated_sql_quote(sql, index, quote)
            buffer.append(char)
            index += 1
            continue

        if char == ";" and quote is None:
            _append_sql_statement(statements, buffer)
            buffer.clear()
            index += 1
            continue

        buffer.append(char)
        index += 1

    _append_sql_statement(statements, buffer)
    return statements


def _next_sql_char(sql: str, index: int) -> str:
    return sql[index + 1] if index + 1 < len(sql) else ""


def _starts_sql_line_comment(char: str, next_char: str) -> bool:
    return char == "-" and next_char == "-"


def _starts_sql_block_comment(char: str, next_char: str) -> bool:
    return char == "/" and next_char == "*"


def _consume_sql_line_comment(
    sql: str,
    index: int,
    buffer: list[str],
) -> tuple[int, bool]:
    char = sql[index]
    if char == "\n":
        buffer.append(char)
        return index + 1, False
    return index + 1, True


def _consume_sql_block_comment(sql: str, index: int) -> tuple[int, bool]:
    char = sql[index]
    next_char = _next_sql_char(sql, index)
    if char == "*" and next_char == "/":
        return index + 2, False
    return index + 1, True


def _updated_sql_quote(sql: str, index: int, quote: str | None) -> str | None:
    char = sql[index]
    if quote is None:
        return char
    if quote == char and not _is_escaped_sql_quote(sql, index):
        return None
    return quote


def _is_escaped_sql_quote(sql: str, index: int) -> bool:
    return index > 0 and sql[index - 1] == "\\"


def _append_sql_statement(statements: list[str], buffer: list[str]) -> None:
    statement = "".join(buffer).strip()
    if statement:
        statements.append(statement)
