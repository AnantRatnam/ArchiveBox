#!/usr/bin/env python3
"""
Tests for archivebox server command.
Verify server can start (basic smoke tests only, no full server testing).
"""

import os
import asyncio
import builtins
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock


def test_sqlite_connections_use_explicit_30_second_busy_timeout():
    from archivebox.core.settings import SQLITE_CONNECTION_OPTIONS

    assert SQLITE_CONNECTION_OPTIONS["OPTIONS"]["timeout"] == 30
    assert "PRAGMA busy_timeout = 30000;" in SQLITE_CONNECTION_OPTIONS["OPTIONS"]["init_command"]
    assert "PRAGMA journal_mode = WAL;" in SQLITE_CONNECTION_OPTIONS["OPTIONS"]["init_command"]


def test_server_shows_usage_info(tmp_path, process):
    """Test that server command shows usage or starts."""
    os.chdir(tmp_path)

    # Just check that the command is recognized
    # We won't actually start a full server in tests
    result = subprocess.run(
        ["archivebox", "server", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "server" in result.stdout.lower() or "http" in result.stdout.lower()


def test_server_init_flag(tmp_path, process):
    """Test that --init flag runs init before starting server."""
    os.chdir(tmp_path)

    # Check init flag is recognized
    result = subprocess.run(
        ["archivebox", "server", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "--init" in result.stdout or "init" in result.stdout.lower()


def test_runner_worker_uses_current_interpreter():
    """The supervised runner should use the active Python environment, not PATH."""
    from archivebox.workers.supervisord_util import RUNNER_WORKER

    assert RUNNER_WORKER["command"] == f"{sys.executable} -m archivebox run --daemon"


def test_reload_workers_use_current_interpreter_and_supervisord_managed_runner():
    from archivebox.workers.supervisord_util import RUNNER_WATCH_WORKER, RUNSERVER_WORKER

    runserver = RUNSERVER_WORKER("127.0.0.1", "8000", reload=True)
    watcher = RUNNER_WATCH_WORKER("http://127.0.0.1:8000")

    assert runserver["name"] == "worker_runserver"
    assert runserver["command"] == f"{sys.executable} -m archivebox manage runserver 127.0.0.1:8000"
    assert 'ARCHIVEBOX_RUNSERVER="1"' in runserver["environment"]
    assert 'ARCHIVEBOX_AUTORELOAD="1"' in runserver["environment"]
    assert 'ARCHIVEBOX_RUNSERVER_BIND_URL="http://127.0.0.1:8000"' in runserver["environment"]

    assert watcher["name"] == "worker_runner_watch"
    assert watcher["command"] == f"{sys.executable} -m archivebox manage runner_watch --bind-url=http://127.0.0.1:8000"


def test_start_server_workers_starts_plugin_owned_sonic_worker(monkeypatch):
    from archivebox.workers import supervisord_util

    supervisor = Mock()
    supervisor.getPID.return_value = 123
    started_workers = []

    monkeypatch.setattr(supervisord_util, "get_or_create_supervisord_process", lambda daemonize=False: supervisor)
    monkeypatch.setattr(supervisord_util, "tail_multiple_worker_logs", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        supervisord_util,
        "get_sonic_supervisord_worker_from_plugin",
        lambda config: {
            "name": "worker_sonic",
            "command": "sonic -c /data/sonic/config.cfg",
            "stdout_logfile": "logs/worker_sonic.log",
        },
    )
    monkeypatch.setattr(
        supervisord_util,
        "start_worker",
        lambda _supervisor, worker, lazy=False: started_workers.append((worker["name"], lazy)) or {"name": worker["name"]},
    )
    monkeypatch.setattr("archivebox.config.common.get_config", lambda: SimpleNamespace(TMP_DIR="/tmp"))

    supervisord_util.start_server_workers(daemonize=True, debug=False)

    assert started_workers == [
        ("worker_daphne", False),
        ("worker_sonic", False),
        ("worker_runner", False),
    ]


def test_missing_plugin_owned_sonic_worker_is_optional(monkeypatch):
    from archivebox.workers.supervisord_util import get_sonic_supervisord_worker_from_plugin

    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "abx_plugins.plugins.search_backend_sonic.daemon":
            raise ModuleNotFoundError("No module named 'abx_plugins.plugins.search_backend_sonic.daemon'", name=name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert get_sonic_supervisord_worker_from_plugin(SimpleNamespace()) is None


def test_sonic_daemon_event_handler_requires_running_supervised_worker(monkeypatch):
    from abx_dl.events import ProcessStdoutEvent
    from abx_dl.orchestrator import create_bus
    from archivebox.search.sonic_daemon import register_sonic_daemon_event_handler

    supervisor = Mock()
    monkeypatch.setattr("archivebox.workers.supervisord_util.get_existing_supervisord_process", lambda: supervisor)
    monkeypatch.setattr(
        "archivebox.workers.supervisord_util.get_worker",
        lambda _supervisor, name: {"name": name, "statename": "RUNNING", "description": "pid 123"},
    )
    monkeypatch.setattr("abx_plugins.plugins.search_backend_sonic.daemon.is_port_listening", lambda host, port: True)

    async def run_test():
        bus = create_bus(name="test_sonic_daemon_event_handler_requires_running_supervised_worker")
        try:
            register_sonic_daemon_event_handler(bus)
            await bus.emit(
                ProcessStdoutEvent(
                    line=json.dumps(
                        {
                            "type": "SonicDaemonStartEvent",
                            "worker_name": "worker_sonic",
                            "url": "tcp://127.0.0.1:1491",
                            "host": "127.0.0.1",
                            "port": 1491,
                            "config_path": "/data/sonic/config.cfg",
                            "output_dir": "/data/sonic",
                        },
                    ),
                ),
            ).now()
        finally:
            await bus.destroy()

    asyncio.run(run_test())
