# ruff: noqa
def pid_is_running(pid: int) -> bool:
    """Return True when the OS still has a process for pid.

    This intentionally does not inspect ArchiveBox state. It is used only for
    foreground parent processes and stale pid files; orchestrator ownership
    still belongs to the database state machine and retry_at locks.
    """

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid_file(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def unlink_pid_file_if_owner(pid_file: Path, pid: int) -> None:
    """Remove a pid file only if it still points at the expected process."""

    try:
        if pid_file.read_text().strip() == str(pid):
            pid_file.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def wait_for_pid_exit(pid: int, *, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return True
        time.sleep(interval)
    return not pid_is_running(pid)


def stop_pidfile_owner(
    pid_file: Path,
    *,
    current_pid: int,
    description: str,
    graceful_timeout: float,
    log: Callable[[str], object],
    owner_matches: Callable[[int], bool] | None = None,
    on_stale_pid: Callable[[], object] | None = None,
    on_forced_stop: Callable[[], object] | None = None,
) -> int:
    """Stop a previous foreground owner recorded in pid_file.

    This is for command-parent takeover only. It deliberately does not claim
    Crawl/Snapshot work; crashed work is resumed by the existing retry_at/state
    machine path after the parent process is gone.
    """

    pid = read_pid_file(pid_file)
    if pid is None or pid == current_pid:
        return 0

    if not pid_is_running(pid):
        pid_file.unlink(missing_ok=True)
        if on_stale_pid is not None:
            on_stale_pid()
        return 0
    if owner_matches is not None and not owner_matches(pid):
        # PIDs can be reused after an unclean exit. A stale pidfile must never
        # let one ArchiveBox collection stop a process owned by another
        # collection or another app entirely.
        pid_file.unlink(missing_ok=True)
        if on_stale_pid is not None:
            on_stale_pid()
        return 0

    log(f"[yellow][*] Stopping existing {description} pid={pid}...[/yellow]")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        if on_stale_pid is not None:
            on_stale_pid()
        return 0

    if wait_for_pid_exit(pid, timeout=graceful_timeout):
        pid_file.unlink(missing_ok=True)
        return 1

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if on_forced_stop is not None:
        on_forced_stop()
    pid_file.unlink(missing_ok=True)
    return 1
