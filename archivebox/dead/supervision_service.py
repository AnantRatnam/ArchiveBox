# ruff: noqa
def ensure_single_orchestrator(*, data_dir: str | Path, takeover: bool, reason: str = ""):
    from archivebox.machine.models import Machine, Process
    from archivebox.workers.supervisord_util import (
        RUNNER_WORKER,
        get_existing_supervisord_process,
        get_or_create_supervisord_process,
        start_worker,
        stop_worker,
    )

    existing = healthy_orchestrator(data_dir=data_dir)
    if existing and not takeover:
        if reason:
            pid = existing.get("pid") if isinstance(existing, dict) else existing.pid
            print(f"[green][*] {reason}; existing orchestrator pid={pid} will process it.[/green]")
        return existing

    supervisor = get_existing_supervisord_process() or get_or_create_supervisord_process(daemonize=False)
    if existing and takeover:
        print("[yellow][*] Taking over existing ArchiveBox orchestrator...[/yellow]")
        try:
            stop_worker(supervisor, RUNNER_WORKER["name"])
        except Exception:
            pass
        for proc in Process.objects.filter(
            machine=Machine.current(),
            process_type=Process.TypeChoices.ORCHESTRATOR,
            status=Process.StatusChoices.RUNNING,
            pwd=str(data_dir),
        ).order_by("created_at"):
            if proc.is_running:
                proc.terminate(graceful_timeout=2.0)

    return start_worker(supervisor, RUNNER_WORKER)


def wait_until_replaced_or_signal(command, *, process_type: str, data_dir: str | Path, url: str | None = None, interval: float = 2.0) -> None:
    while command_is_newest(command, process_type=process_type, data_dir=data_dir, url=url):
        command.heartbeat()
        time.sleep(interval)
