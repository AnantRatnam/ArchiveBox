from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from itertools import tee
import re

from django.db.backends.sqlite3.base import DatabaseWrapper as DjangoSQLiteDatabaseWrapper
from django.db.backends.sqlite3.base import SQLiteCursorWrapper as DjangoSQLiteCursorWrapper


def _is_locked_error(error: BaseException) -> bool:
    from django.db import OperationalError

    return isinstance(error, (sqlite3.OperationalError, OperationalError)) and "database is locked" in str(error).lower()


def _format_sql(query: str, params=None) -> str:
    compact = " ".join(str(query).split())
    match = re.match(r'^(INSERT INTO|UPDATE|DELETE FROM|SELECT) "?([A-Za-z0-9_]+)"?', compact, flags=re.IGNORECASE)
    if match:
        compact = f"{match.group(1).upper()} {match.group(2)}"
    if params is not None:
        if isinstance(params, str):
            params_summary = params
        elif isinstance(params, (tuple, list)):
            preview = ", ".join(repr(param)[:60] for param in params[:4])
            params_summary = f"{len(params)} params: {preview}"
        elif isinstance(params, Mapping):
            preview = ", ".join(f"{key}={repr(value)[:60]}" for key, value in list(params.items())[:4])
            params_summary = f"{len(params)} params: {preview}"
        else:
            params_summary = repr(params)[:120]
        compact = f"{compact} ({params_summary})"
    return compact[:260]


def _log_locked_database(query: str, params=None, *, attempt: int, elapsed: float) -> None:
    from rich.console import Console

    from archivebox.misc.db import sqlite_lock_holders

    console = Console(stderr=True)
    console.print(f"[yellow][*] SQLite database is locked for {elapsed:.0f}s; retrying in 5s... attempt={attempt}[/yellow]")
    console.print(f"[yellow]    Query: {_format_sql(query, params)}[/yellow]")
    holders = sqlite_lock_holders()
    if holders:
        console.print("[yellow]    DB holders:[/yellow]")
        for holder in holders[:8]:
            console.print(f"[yellow]    - {holder}[/yellow]")
        if len(holders) > 8:
            console.print(f"[yellow]    ... {len(holders) - 8} more[/yellow]")
    else:
        console.print("[yellow]    No local process with index.sqlite3 open was visible to this user.[/yellow]")
    if attempt == 1:
        console.print(
            "[dim]    SQLite does not expose the active SQL statement from another process; only local PIDs with the DB open can be shown.[/dim]",
        )


def _retry_locked_database(action, query: str, params=None):
    attempt = 0
    started_at = time.monotonic()
    while True:
        try:
            return action()
        except (sqlite3.OperationalError, Exception) as err:
            if not _is_locked_error(err):
                raise
            attempt += 1
            _log_locked_database(query, params, attempt=attempt, elapsed=time.monotonic() - started_at)
            time.sleep(5.0)


class SQLiteCursorWrapper(DjangoSQLiteCursorWrapper):
    def execute(self, query, params=None):
        if params is None:
            return _retry_locked_database(lambda: super(SQLiteCursorWrapper, self).execute(query), query)
        param_names = list(params) if isinstance(params, Mapping) else None
        converted_query = self.convert_query(query, param_names=param_names)
        return _retry_locked_database(
            lambda: super(DjangoSQLiteCursorWrapper, self).execute(converted_query, params),
            converted_query,
            params,
        )

    def executemany(self, query, param_list):
        peekable, param_list = tee(iter(param_list))
        if (params := next(peekable, None)) and isinstance(params, Mapping):
            param_names = list(params)
        else:
            param_names = None
        converted_query = self.convert_query(query, param_names=param_names)
        param_list = tuple(param_list)
        return _retry_locked_database(
            lambda: super(DjangoSQLiteCursorWrapper, self).executemany(converted_query, param_list),
            converted_query,
            f"{len(param_list)} parameter sets",
        )


class DatabaseWrapper(DjangoSQLiteDatabaseWrapper):
    def create_cursor(self, name=None):
        return self.connection.cursor(factory=SQLiteCursorWrapper)

    def _commit(self):
        return _retry_locked_database(lambda: super(DatabaseWrapper, self)._commit(), "COMMIT")

    def _rollback(self):
        return _retry_locked_database(lambda: super(DatabaseWrapper, self)._rollback(), "ROLLBACK")
