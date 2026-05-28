"""
Database utility functions for ArchiveBox.
"""

__package__ = "archivebox.misc"

from io import StringIO
from pathlib import Path
from typing import TextIO
from typing import Any
import fcntl
import importlib
import time
from collections.abc import Callable
from contextlib import contextmanager
from sqlite3 import OperationalError as SQLiteOperationalError

from archivebox.config import DATA_DIR
from archivebox.misc.util import enforce_types


def compact_command(cmdline: list[str] | None, fallback: str = "") -> str:
    parts = [str(part) for part in (cmdline or []) if str(part)]
    if not parts:
        return fallback
    for marker in ("archivebox", "daphne", "gunicorn", "uvicorn", "supervisord", "sonic", "node"):
        for idx, part in enumerate(parts):
            if Path(part).name == marker or part == marker:
                return " ".join([Path(parts[idx]).name, *parts[idx + 1 :]])[:220]
    return " ".join([Path(parts[0]).name, *parts[1:]])[:220]


def sqlite_lock_holders(db_path: Path = DATA_DIR / "index.sqlite3") -> list[str]:
    import psutil

    db_path = db_path.resolve()
    holders: list[str] = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "status"]):
        try:
            open_files = proc.open_files()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        for open_file in open_files:
            try:
                open_path = Path(open_file.path).resolve()
            except (OSError, RuntimeError):
                continue
            if open_path == db_path or open_path.name in {f"{db_path.name}-wal", f"{db_path.name}-shm", f"{db_path.name}-journal"}:
                info = proc.info
                cmdline = compact_command(info.get("cmdline"), fallback=info.get("name") or "")
                holders.append(f"pid={info['pid']} ppid={info['ppid']} {info['status']} {cmdline}")
                break
    return holders


def sqlite_lock_error(error: BaseException) -> bool:
    return isinstance(error, SQLiteOperationalError) and "database is locked" in str(error).lower()


def retry_sqlite_locks(action: Callable[[], Any], *, label: str, stderr: TextIO | None = None) -> Any:
    from django.db import OperationalError, connections
    from rich.console import Console

    console = Console(file=stderr or None, stderr=stderr is None)
    attempts = 0
    while True:
        try:
            return action()
        except OperationalError as err:
            if "database is locked" not in str(err).lower():
                raise
        except SQLiteOperationalError as err:
            if not sqlite_lock_error(err):
                raise

        attempts += 1
        connections.close_all()
        holders = sqlite_lock_holders()
        console.print(f"[yellow][*] SQLite database is locked while {label}; retrying in 5s...[/yellow]")
        if holders:
            console.print("[yellow]    DB holders:[/yellow]")
            for holder in holders[:8]:
                console.print(f"[yellow]    - {holder}[/yellow]")
            if len(holders) > 8:
                console.print(f"[yellow]    ... {len(holders) - 8} more[/yellow]")
        else:
            console.print("[yellow]    No local process with index.sqlite3 open was visible to this user.[/yellow]")
        if attempts == 1:
            console.print(
                "[dim]    SQLite does not expose the active SQL statement from another process; only the owning local PIDs can be shown.[/dim]",
            )
        with console.status("[yellow]Waiting for SQLite database lock to clear...[/yellow]", spinner="dots"):
            time.sleep(5.0)


@contextmanager
def migration_lock(stdout: TextIO | None = None):
    from archivebox.config.paths import get_or_create_working_tmp_dir
    from rich.console import Console

    lock_path = get_or_create_working_tmp_dir(autofix=True, quiet=True) / "migrate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Migrations on large SQLite collections can run for hours. Use a
            # kernel lock with no timeout so parallel ArchiveBox commands queue
            # behind the active migrate process instead of racing it.
            console = Console(file=stdout or None, stderr=stdout is None)
            with console.status("[yellow]Waiting for migration lock...[/yellow]", spinner="dots"):
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(1.0)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@enforce_types
def pending_migrations(out_dir: Path = DATA_DIR) -> list[str]:
    """Cheaply compare migration files to django_migrations without invoking migrate."""
    from django.apps import apps
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    def applied_rows() -> set[tuple[str, str]]:
        with connection.cursor() as cursor:
            try:
                cursor.execute("SELECT app, name FROM django_migrations")
            except Exception as err:
                if "no such table" in str(err).lower():
                    return set()
                raise
            return {(str(app), str(name)) for app, name in cursor.fetchall()}

    applied = retry_sqlite_locks(applied_rows, label="checking applied migrations")
    disk_migrations: set[tuple[str, str]] = set()
    for app_config in apps.get_app_configs():
        module_name, explicit = MigrationLoader.migrations_module(app_config.label)
        if module_name is None:
            continue
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            if explicit:
                raise
            continue
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        for migration_file in Path(module_file).parent.glob("[0-9][0-9][0-9][0-9]_*.py"):
            disk_migrations.add((app_config.label, migration_file.stem))

    return [f"{app}.{name}" for app, name in sorted(disk_migrations - applied)]


@enforce_types
def apply_migrations(out_dir: Path = DATA_DIR, stdout: TextIO | None = None, stderr: TextIO | None = None, verbosity: int = 1) -> list[str]:
    """Apply pending Django migrations"""
    from django.core.management import call_command

    with migration_lock(stdout=stderr or stdout):
        if not pending_migrations():
            return []

        if stdout is not None:
            retry_sqlite_locks(
                lambda: call_command("migrate", interactive=False, database="default", stdout=stdout, stderr=stderr, verbosity=verbosity),
                label="applying migrations",
                stderr=stderr,
            )
            return []

        def migrate() -> StringIO:
            out1 = StringIO()
            call_command("migrate", interactive=False, database="default", stdout=out1, verbosity=verbosity)
            out1.seek(0)
            return out1

        out1 = retry_sqlite_locks(migrate, label="applying migrations")

        return [line.strip() for line in out1.readlines() if line.strip()]
