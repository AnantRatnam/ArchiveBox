from __future__ import annotations

import time
from pathlib import Path

from django.utils import timezone
from rich import print


def runtime_stack_owner_types():
    from archivebox.machine.models import Process

    return (
        Process.TypeChoices.UPDATE,
        Process.TypeChoices.SERVER,
        Process.TypeChoices.ORCHESTRATOR,
        Process.TypeChoices.ADD,
    )


def current_command(process_type: str, *, data_dir: str | Path, url: str | None = None):
    from archivebox.machine.models import Process

    proc = Process.current()
    proc.mark_running(process_type=process_type, pwd=str(data_dir), url=url, timeout=0)
    return proc


def live_processes(*, process_type: str, data_dir: str | Path, url: str | None = None):
    from archivebox.machine.models import Machine, Process

    Process.cleanup_stale_running(machine=Machine.current())
    qs = Process.objects.filter(
        machine=Machine.current(),
        process_type=process_type,
        status=Process.StatusChoices.RUNNING,
        pwd=str(data_dir),
    )
    if url is not None:
        qs = qs.filter(url=url)
    return [proc for proc in qs.order_by("-created_at", "-modified_at").iterator(chunk_size=50) if proc.is_running]


def newest_live_process(*, process_type: str, data_dir: str | Path, url: str | None = None):
    processes = live_processes(process_type=process_type, data_dir=data_dir, url=url)
    return processes[0] if processes else None


def command_is_newest(command, *, process_type: str, data_dir: str | Path, url: str | None = None) -> bool:
    leader = newest_live_process(process_type=process_type, data_dir=data_dir, url=url)
    return bool(leader and leader.id == command.id)


def runtime_stack_owner(*, data_dir: str | Path):
    from archivebox.machine.models import Machine, Process

    Process.cleanup_stale_running(machine=Machine.current())
    base_qs = Process.objects.filter(
        machine=Machine.current(),
        status=Process.StatusChoices.RUNNING,
        pwd=str(data_dir),
        process_type__in=runtime_stack_owner_types(),
    )
    for process_types in (
        (Process.TypeChoices.UPDATE,),
        (Process.TypeChoices.SERVER, Process.TypeChoices.ADD),
        (Process.TypeChoices.ORCHESTRATOR,),
    ):
        qs = base_qs.filter(process_type__in=process_types)
        for proc in qs.order_by("-created_at", "-modified_at").iterator(chunk_size=50):
            if proc.is_running:
                return proc
    return None


def command_owns_runtime_stack(command, *, data_dir: str | Path) -> bool:
    owner = runtime_stack_owner(data_dir=data_dir)
    return bool(owner and owner.id == command.id)


def ensure_daemon_stack(*, reason: str = ""):
    from archivebox.config.common import get_config
    from archivebox.workers.supervisord_util import (
        get_existing_supervisord_process,
        get_or_create_supervisord_process,
        get_sonic_supervisord_worker_from_plugin,
        get_worker,
        start_worker,
    )

    config = get_config()
    sonic_worker = get_sonic_supervisord_worker_from_plugin(config)
    if sonic_worker is None:
        return None

    from abx_plugins.plugins.search_backend_sonic.daemon import is_port_listening, prepare_sonic_daemon

    sonic_event = prepare_sonic_daemon(config)
    if is_port_listening(sonic_event.host, sonic_event.port):
        return {
            "name": sonic_event.worker_name,
            "statename": "RUNNING",
            "description": f"existing Sonic daemon at {sonic_event.url}",
        }

    supervisor = get_existing_supervisord_process() or get_or_create_supervisord_process(daemonize=False)
    worker = get_worker(supervisor, sonic_worker["name"])
    if isinstance(worker, dict) and worker.get("statename") in ("STARTING", "RUNNING"):
        return worker

    if reason:
        print(f"[yellow][*] Starting daemon stack for {reason}...[/yellow]")
    return start_worker(supervisor, sonic_worker)


def healthy_orchestrator(*, data_dir: str | Path):
    from archivebox.machine.models import Machine, Process
    from archivebox.workers.supervisord_util import get_existing_supervisord_process, get_worker

    Process.cleanup_stale_running(machine=Machine.current())
    supervisor = get_existing_supervisord_process()
    worker = get_worker(supervisor, "worker_runner") if supervisor else None
    if isinstance(worker, dict) and worker.get("statename") in ("STARTING", "RUNNING"):
        return worker

    for proc in Process.objects.filter(
        machine=Machine.current(),
        process_type=Process.TypeChoices.ORCHESTRATOR,
        status=Process.StatusChoices.RUNNING,
        pwd=str(data_dir),
    ).order_by("-created_at"):
        if proc.is_running:
            return proc
    return None


def standby_until_leader_needed(command, *, process_type: str, data_dir: str | Path, url: str | None = None, interval: float = 2.0) -> None:
    from archivebox.workers.supervisord_util import reap_foreground_supervisord_process

    announced = False
    while not command_is_newest(command, process_type=process_type, data_dir=data_dir, url=url):
        reap_foreground_supervisord_process()
        if not announced:
            leader = newest_live_process(process_type=process_type, data_dir=data_dir, url=url)
            leader_pid = leader.pid if leader else "unknown"
            print(f"[yellow][*] Standing by; newer ArchiveBox parent pid={leader_pid} is running the orchestrator and server.[/yellow]")
            announced = True
        command.heartbeat()
        time.sleep(interval)
    command.modified_at = timezone.now()
    command.save(update_fields=["modified_at"])


def standby_until_runtime_stack_needed(command, *, data_dir: str | Path, interval: float = 2.0) -> None:
    from archivebox.workers.supervisord_util import reap_foreground_supervisord_process

    announced = False
    while not command_owns_runtime_stack(command, data_dir=data_dir):
        reap_foreground_supervisord_process()
        if not announced:
            owner = runtime_stack_owner(data_dir=data_dir)
            owner_pid = owner.pid if owner else "unknown"
            owner_type = owner.process_type if owner else "unknown"
            print(f"[yellow][*] Standing by; ArchiveBox {owner_type} pid={owner_pid} owns the runtime stack.[/yellow]")
            announced = True
        command.heartbeat()
        time.sleep(interval)
    command.modified_at = timezone.now()
    command.save(update_fields=["modified_at"])
