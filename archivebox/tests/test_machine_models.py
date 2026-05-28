"""
Unit tests for machine module models: Machine, NetworkInterface, Binary, Process.

Tests cover:
1. Machine model creation and current() method
2. NetworkInterface model and network detection
3. Binary model lifecycle and state machine
4. Process model lifecycle, hierarchy, and state machine
5. JSONL serialization/deserialization
6. Manager methods
7. Process tracking methods (replacing pid_utils)
"""

import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from django.test import TestCase
from django.utils import timezone

from archivebox.machine.models import (
    BinaryManager,
    Machine,
    NetworkInterface,
    Binary,
    Process,
    BinaryMachine,
    ProcessMachine,
    MACHINE_RECHECK_INTERVAL,
    PID_REUSE_WINDOW,
    PROCESS_TIMEOUT_GRACE,
)


class TestMachineModel(TestCase):
    """Test the Machine model."""

    def setUp(self):
        """Reset cached machine between tests."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None

    def test_machine_current_creates_machine(self):
        """Machine.current() should create a machine if none exists."""
        machine = Machine.current()

        self.assertIsNotNone(machine)
        self.assertIsNotNone(machine.id)
        self.assertIsNotNone(machine.guid)
        self.assertEqual(machine.hostname, os.uname().nodename)
        self.assertIn(machine.os_family, ["linux", "darwin", "windows", "freebsd"])

    def test_machine_current_returns_cached(self):
        """Machine.current() should return cached machine within recheck interval."""
        machine1 = Machine.current()
        machine2 = Machine.current()

        self.assertEqual(machine1.id, machine2.id)

    def test_machine_current_refreshes_after_interval(self):
        """Machine.current() should refresh after recheck interval."""
        import archivebox.machine.models as models

        machine1 = Machine.current()

        # Manually expire the cache by modifying modified_at
        machine1.modified_at = timezone.now() - timedelta(seconds=MACHINE_RECHECK_INTERVAL + 1)
        machine1.save()
        models._CURRENT_MACHINE = machine1

        machine2 = Machine.current()

        # Should have fetched/updated the machine (same GUID)
        self.assertEqual(machine1.guid, machine2.guid)

    def test_machine_current_recreates_stale_cached_row(self):
        """Machine.current() should recreate the cached machine if the row was deleted."""
        import archivebox.machine.models as models

        machine1 = Machine.current()
        machine1_id = machine1.id
        machine1_guid = machine1.guid

        machine1.delete()
        models._CURRENT_MACHINE = machine1

        machine2 = Machine.current()

        self.assertNotEqual(machine1_id, machine2.id)
        self.assertEqual(machine1_guid, machine2.guid)

    def test_machine_from_jsonl_update(self):
        """Machine.from_json() should update machine config."""
        from archivebox.config.constants import CONSTANTS

        Machine.current()  # Ensure machine exists
        wget_path = CONSTANTS.DEFAULT_LIB_DIR / "wget"
        wget_path.parent.mkdir(parents=True, exist_ok=True)
        wget_path.write_text("#!/bin/sh\n")
        self.addCleanup(lambda: wget_path.exists() and wget_path.unlink())
        record = {
            "config": {
                "WGET_BINARY": str(wget_path),
            },
        }

        result = Machine.from_json(record)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.config.get("WGET_BINARY"), str(wget_path))

    def test_machine_from_jsonl_keeps_only_valid_binary_paths(self):
        """Machine.from_json() should persist only valid LIB_DIR binary paths."""
        from archivebox.config.constants import CONSTANTS

        Machine.current()  # Ensure machine exists
        wget_path = CONSTANTS.DEFAULT_LIB_DIR / "wget"
        wget_path.parent.mkdir(parents=True, exist_ok=True)
        wget_path.write_text("#!/bin/sh\n")
        self.addCleanup(lambda: wget_path.exists() and wget_path.unlink())
        record = {
            "config": {
                "WGET_BINARY": str(wget_path),
                "CHROMIUM_VERSION": "123.4.5",
                "YTDLP_BINARY": "/tmp/archivebox-test-missing-yt-dlp",
            },
        }

        result = Machine.from_json(record)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.config.get("WGET_BINARY"), str(wget_path))
        self.assertNotIn("CHROMIUM_VERSION", result.config)
        self.assertNotIn("YTDLP_BINARY", result.config)

    def test_machine_from_jsonl_invalid(self):
        """Machine.from_json() should return None for invalid records."""
        result = Machine.from_json({"invalid": "record"})
        self.assertIsNone(result)

    def test_machine_current_keeps_only_derived_runtime_cache(self):
        """Machine.current() should keep derived cache entries, not runtime config."""
        import archivebox.machine.models as models
        from archivebox.config.constants import CONSTANTS

        active_lib_dir = CONSTANTS.DEFAULT_LIB_DIR
        active_lib_dir.mkdir(parents=True, exist_ok=True)
        chrome_path = active_lib_dir / "chromium"
        node_path = active_lib_dir / "node"
        chrome_path.write_text("#!/bin/sh\n")
        node_path.write_text("#!/bin/sh\n")
        external_path = "/tmp/archivebox-test-external-node"
        open(external_path, "a").close()
        self.addCleanup(lambda: chrome_path.exists() and chrome_path.unlink())
        self.addCleanup(lambda: node_path.exists() and node_path.unlink())
        self.addCleanup(lambda: os.path.exists(external_path) and os.remove(external_path))
        machine = Machine.current()
        machine.config = {
            "CHROME_BINARY": str(chrome_path),
            "NODE_BINARY": str(node_path),
            "ABX_INSTALL_CACHE": {"wget": "2026-03-24T00:00:00+00:00"},
            "CHROME_ISOLATION": "snapshot",
            "CHROME_USER_DATA_DIR": "/tmp/profile",
            "CHROMIUM_VERSION": "123.4.5",
            "YTDLP_BINARY": external_path,
            "WGET_BINARY": "/tmp/archivebox-test-missing-wget",
        }
        machine.save(update_fields=["config"])
        models._CURRENT_MACHINE = machine

        refreshed = Machine.current()

        self.assertEqual(refreshed.config.get("CHROME_BINARY"), str(chrome_path))
        self.assertEqual(refreshed.config.get("NODE_BINARY"), str(node_path))
        self.assertNotIn("ABX_INSTALL_CACHE", refreshed.config)
        self.assertNotIn("CHROME_ISOLATION", refreshed.config)
        self.assertNotIn("CHROME_USER_DATA_DIR", refreshed.config)
        self.assertNotIn("CHROMIUM_VERSION", refreshed.config)
        self.assertNotIn("YTDLP_BINARY", refreshed.config)
        self.assertNotIn("WGET_BINARY", refreshed.config)

    def test_get_config_auto_applies_current_machine_config(self):
        """get_config() should include sanitized Machine.current() config by default."""
        import archivebox.machine.models as models
        from archivebox.config.common import get_config

        lib_dir = get_config(include_machine=False).LIB_DIR
        chrome_path = lib_dir / "chromium"
        chrome_path.parent.mkdir(parents=True, exist_ok=True)
        chrome_path.write_text("#!/bin/sh\n")
        self.addCleanup(lambda: chrome_path.exists() and chrome_path.unlink())
        machine = Machine.current()
        machine.config = {
            "CHROME_BINARY": str(chrome_path),
            "ABX_INSTALL_CACHE": {"chrome": "2026-03-24T00:00:00+00:00"},
            "CHROME_ISOLATION": "snapshot",
        }
        machine.save(update_fields=["config"])
        models._CURRENT_MACHINE = machine

        config = get_config()

        self.assertEqual(config.CHROME_BINARY, str(chrome_path))
        self.assertEqual(config.CHROME_ISOLATION, "crawl")

    def test_machine_manager_current(self):
        """Machine.objects.current() should return current machine."""
        machine = Machine.current()
        self.assertIsNotNone(machine)
        self.assertEqual(machine.id, Machine.current().id)


class TestNetworkInterfaceModel(TestCase):
    """Test the NetworkInterface model."""

    def setUp(self):
        """Reset cached interface between tests."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        models._CURRENT_INTERFACE = None

    def test_networkinterface_current_creates_interface(self):
        """NetworkInterface.current() should create an interface if none exists."""
        interface = NetworkInterface.current()

        self.assertIsNotNone(interface)
        self.assertIsNotNone(interface.id)
        self.assertIsNotNone(interface.machine)
        self.assertIsNotNone(interface.ip_local)

    def test_networkinterface_current_returns_cached(self):
        """NetworkInterface.current() should return cached interface within recheck interval."""
        interface1 = NetworkInterface.current()
        interface2 = NetworkInterface.current()

        self.assertEqual(interface1.id, interface2.id)

    def test_networkinterface_manager_current(self):
        """NetworkInterface.objects.current() should return current interface."""
        interface = NetworkInterface.current()
        self.assertIsNotNone(interface)


class TestBinaryModel(TestCase):
    """Test the Binary model."""

    def setUp(self):
        """Reset cached binaries and create a machine."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        models._CURRENT_BINARIES = {}
        self.machine = Machine.current()

    def test_binary_creation(self):
        """Binary should be created with default values."""
        binary = Binary.objects.create(
            machine=self.machine,
            name="wget",
            binproviders="apt,brew,env",
        )

        self.assertIsNotNone(binary.id)
        self.assertEqual(binary.name, "wget")
        self.assertEqual(binary.status, Binary.StatusChoices.QUEUED)
        self.assertFalse(binary.is_valid)

    def test_binary_is_valid(self):
        """Binary.is_valid should be True for installed binaries with a resolved path."""
        binary = Binary.objects.create(
            machine=self.machine,
            name="wget",
            abspath="/usr/bin/wget",
            version="1.21",
            status=Binary.StatusChoices.INSTALLED,
        )

        self.assertTrue(binary.is_valid)

    def test_binary_manager_get_valid_binary(self):
        """BinaryManager.get_valid_binary() should find valid binaries."""
        # Create invalid binary (no abspath)
        Binary.objects.create(machine=self.machine, name="wget")

        # Create valid binary
        Binary.objects.create(
            machine=self.machine,
            name="wget",
            abspath="/usr/bin/wget",
            version="1.21",
            status=Binary.StatusChoices.INSTALLED,
        )

        result = cast(BinaryManager, Binary.objects).get_valid_binary("wget")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.abspath, "/usr/bin/wget")

    def test_binary_update_and_requeue(self):
        """Binary.update_and_requeue() should update fields and save."""
        binary = Binary.objects.create(machine=self.machine, name="test")
        old_modified = binary.modified_at

        binary.update_and_requeue(
            status=Binary.StatusChoices.QUEUED,
            retry_at=timezone.now() + timedelta(seconds=60),
        )

        binary.refresh_from_db()
        self.assertEqual(binary.status, Binary.StatusChoices.QUEUED)
        self.assertGreater(binary.modified_at, old_modified)

    def test_binary_from_json_preserves_provider_overrides(self):
        """Binary.from_json() should persist provider overrides unchanged."""
        overrides = {
            "apt": {"install_args": ["chromium"]},
            "npm": {"install_args": "puppeteer"},
            "custom": {"install": "bash -lc 'echo ok'"},
        }

        binary = Binary.from_json(
            {
                "name": "chrome",
                "binproviders": "apt,npm,custom",
                "overrides": overrides,
            },
        )

        self.assertIsNotNone(binary)
        assert binary is not None
        self.assertEqual(binary.overrides, overrides)

    def test_binary_from_json_canonicalizes_path_like_names(self):
        """Binary.from_json() should store command names, not path cache values."""
        binary = Binary.from_json(
            {
                "name": "/tmp/old-lib/pip/venv/bin/trafilatura",
                "binproviders": "env,pip",
                "overrides": {"pip": {"install_args": ["trafilatura"]}},
            },
        )

        self.assertIsNotNone(binary)
        assert binary is not None
        self.assertEqual(binary.name, "trafilatura")

    def test_binary_from_json_does_not_coerce_legacy_override_shapes(self):
        """Binary.from_json() should no longer translate legacy non-dict provider overrides."""
        overrides = {
            "apt": ["chromium"],
            "npm": "puppeteer",
        }

        binary = Binary.from_json(
            {
                "name": "chrome",
                "binproviders": "apt,npm",
                "overrides": overrides,
            },
        )

        self.assertIsNotNone(binary)
        assert binary is not None
        self.assertEqual(binary.overrides, overrides)

    def test_binary_from_json_prefers_published_readability_package(self):
        """Binary.from_json() should rewrite readability's npm git URL to the published package."""
        binary = Binary.from_json(
            {
                "name": "readability-extractor",
                "binproviders": "env,npm",
                "overrides": {
                    "npm": {
                        "install_args": ["https://github.com/ArchiveBox/readability-extractor"],
                    },
                },
            },
        )

        self.assertIsNotNone(binary)
        assert binary is not None
        self.assertEqual(
            binary.overrides,
            {
                "npm": {
                    "install_args": ["readability-extractor"],
                },
            },
        )


class TestBinaryStateMachine(TestCase):
    """Test the BinaryMachine state machine."""

    def setUp(self):
        """Create a machine and binary for state machine tests."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        self.machine = Machine.current()
        self.binary = Binary.objects.create(
            machine=self.machine,
            name="test-binary",
            binproviders="env",
        )

    def test_binary_state_machine_initial_state(self):
        """BinaryMachine should start in queued state."""
        sm = BinaryMachine(self.binary)
        self.assertEqual(sm.current_state_value, Binary.StatusChoices.QUEUED)

    def test_binary_state_machine_can_start(self):
        """BinaryMachine.can_start() should check name and binproviders."""
        sm = BinaryMachine(self.binary)
        self.assertTrue(sm.can_install())

        self.binary.binproviders = ""
        self.binary.save()
        sm = BinaryMachine(self.binary)
        self.assertFalse(sm.can_install())


class TestProcessModel(TestCase):
    """Test the Process model."""

    def setUp(self):
        """Create a machine for process tests."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        models._CURRENT_PROCESS = None
        self.machine = Machine.current()

    def test_process_creation(self):
        """Process should be created with default values."""
        process = Process.objects.create(
            machine=self.machine,
            cmd=["echo", "hello"],
            pwd="/tmp",
        )

        self.assertIsNotNone(process.id)
        self.assertEqual(process.cmd, ["echo", "hello"])
        self.assertEqual(process.status, Process.StatusChoices.QUEUED)
        self.assertIsNone(process.pid)
        self.assertIsNone(process.exit_code)

    def test_process_to_jsonl(self):
        """Process.to_json() should serialize correctly."""
        process = Process.objects.create(
            machine=self.machine,
            cmd=["echo", "hello"],
            pwd="/tmp",
            timeout=60,
        )
        json_data = process.to_json()

        self.assertEqual(json_data["type"], "Process")
        self.assertEqual(json_data["cmd"], ["echo", "hello"])
        self.assertEqual(json_data["pwd"], "/tmp")
        self.assertEqual(json_data["timeout"], 60)

    def test_process_update_and_requeue(self):
        """Process.update_and_requeue() should update fields and save."""
        process = Process.objects.create(machine=self.machine, cmd=["test"])

        process.update_and_requeue(
            status=Process.StatusChoices.RUNNING,
            pid=12345,
            started_at=timezone.now(),
        )

        process.refresh_from_db()
        self.assertEqual(process.status, Process.StatusChoices.RUNNING)
        self.assertEqual(process.pid, 12345)
        self.assertIsNotNone(process.started_at)


class TestProcessCurrent(TestCase):
    """Test Process.current() method."""

    def setUp(self):
        """Reset caches."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        models._CURRENT_PROCESS = None

    def test_process_current_creates_record(self):
        """Process.current() should create a Process for current PID."""
        proc = Process.current()

        self.assertIsNotNone(proc)
        self.assertEqual(proc.pid, os.getpid())
        self.assertEqual(proc.status, Process.StatusChoices.RUNNING)
        self.assertIsNotNone(proc.machine)
        self.assertIsNotNone(proc.iface)
        self.assertEqual(proc.iface.machine_id, proc.machine_id)
        self.assertIsNotNone(proc.started_at)

    def test_process_current_caches(self):
        """Process.current() should cache the result."""
        proc1 = Process.current()
        proc2 = Process.current()

        self.assertEqual(proc1.id, proc2.id)

    def test_process_detect_type_runner(self):
        """_detect_process_type should detect the background runner command."""
        old_argv = sys.argv
        try:
            sys.argv = ["archivebox", "run", "--daemon"]
            result = Process._detect_process_type()
            self.assertEqual(result, Process.TypeChoices.ORCHESTRATOR)
        finally:
            sys.argv = old_argv

    def test_process_detect_type_runner_watch(self):
        """runner_watch should be classified as a worker, not the orchestrator itself."""
        old_argv = sys.argv
        try:
            sys.argv = ["archivebox", "manage", "runner_watch", "--bind-url=http://127.0.0.1:8000"]
            result = Process._detect_process_type()
            self.assertEqual(result, Process.TypeChoices.WORKER)
        finally:
            sys.argv = old_argv

    def test_process_detect_type_cli(self):
        """_detect_process_type should detect CLI commands."""
        old_argv = sys.argv
        try:
            sys.argv = ["archivebox", "add", "http://example.com"]
            result = Process._detect_process_type()
            self.assertEqual(result, Process.TypeChoices.CLI)
        finally:
            sys.argv = old_argv

    def test_process_detect_type_binary(self):
        """_detect_process_type should detect non-ArchiveBox subprocesses as binary processes."""
        old_argv = sys.argv
        try:
            sys.argv = ["/usr/bin/wget", "https://example.com"]
            result = Process._detect_process_type()
            self.assertEqual(result, Process.TypeChoices.BINARY)
        finally:
            sys.argv = old_argv

    def test_process_proc_allows_interpreter_wrapped_script(self):
        """Process.proc should accept a script recorded in DB when wrapped by an interpreter in psutil."""
        import psutil

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        script = Path(temp_dir.name) / "on_CrawlSetup__90_chrome_launch.daemon.bg.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(script), "--url=https://example.com/"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def cleanup_process():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        self.addCleanup(cleanup_process)
        os_proc = psutil.Process(process.pid)
        proc = Process.objects.create(
            machine=Machine.current(),
            cmd=[str(script), "--url=https://example.com/"],
            pid=process.pid,
            status=Process.StatusChoices.RUNNING,
            started_at=timezone.datetime.fromtimestamp(os_proc.create_time(), tz=timezone.get_current_timezone()),
        )

        resolved_proc = proc.proc
        self.assertIsNotNone(resolved_proc)
        assert resolved_proc is not None
        self.assertEqual(resolved_proc.pid, process.pid)


class TestProcessHierarchy(TestCase):
    """Test Process parent/child relationships."""

    def setUp(self):
        """Create machine."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        self.machine = Machine.current()

    def test_process_parent_child(self):
        """Process should track parent/child relationships."""
        parent = Process.objects.create(
            machine=self.machine,
            process_type=Process.TypeChoices.CLI,
            status=Process.StatusChoices.RUNNING,
            pid=1,
            started_at=timezone.now(),
        )

        child = Process.objects.create(
            machine=self.machine,
            parent=parent,
            process_type=Process.TypeChoices.WORKER,
            status=Process.StatusChoices.RUNNING,
            pid=2,
            started_at=timezone.now(),
        )

        self.assertEqual(child.parent, parent)
        self.assertIn(child, parent.children.all())

    def test_process_root(self):
        """Process.root should return the root of the hierarchy."""
        root = Process.objects.create(
            machine=self.machine,
            process_type=Process.TypeChoices.CLI,
            status=Process.StatusChoices.RUNNING,
            started_at=timezone.now(),
        )
        child = Process.objects.create(
            machine=self.machine,
            parent=root,
            status=Process.StatusChoices.RUNNING,
            started_at=timezone.now(),
        )
        grandchild = Process.objects.create(
            machine=self.machine,
            parent=child,
            status=Process.StatusChoices.RUNNING,
            started_at=timezone.now(),
        )

        self.assertEqual(grandchild.root, root)
        self.assertEqual(child.root, root)
        self.assertEqual(root.root, root)

    def test_process_depth(self):
        """Process.depth should return depth in tree."""
        root = Process.objects.create(
            machine=self.machine,
            status=Process.StatusChoices.RUNNING,
            started_at=timezone.now(),
        )
        child = Process.objects.create(
            machine=self.machine,
            parent=root,
            status=Process.StatusChoices.RUNNING,
            started_at=timezone.now(),
        )

        self.assertEqual(root.depth, 0)
        self.assertEqual(child.depth, 1)


class TestProcessLifecycle(TestCase):
    """Test Process lifecycle methods."""

    def setUp(self):
        """Create machine."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        self.machine = Machine.current()

    def test_process_is_running_current_pid(self):
        """is_running should be True for current PID."""
        import psutil
        from datetime import datetime

        proc_start = datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), tz=timezone.get_current_timezone())
        proc = Process.objects.create(
            machine=self.machine,
            status=Process.StatusChoices.RUNNING,
            pid=os.getpid(),
            started_at=proc_start,
        )

        self.assertTrue(proc.is_running)

    def test_process_is_running_fake_pid(self):
        """is_running should be False for non-existent PID."""
        proc = Process.objects.create(
            machine=self.machine,
            status=Process.StatusChoices.RUNNING,
            pid=999999,
            started_at=timezone.now(),
        )

        self.assertFalse(proc.is_running)

    def test_process_poll_detects_exit(self):
        """poll() should detect exited process."""
        proc = Process.objects.create(
            machine=self.machine,
            status=Process.StatusChoices.RUNNING,
            pid=999999,
            started_at=timezone.now(),
        )

        exit_code = proc.poll()

        self.assertIsNotNone(exit_code)
        proc.refresh_from_db()
        self.assertEqual(proc.status, Process.StatusChoices.EXITED)

    def test_process_poll_normalizes_negative_exit_code(self):
        """poll() should normalize -1 exit codes to 137."""
        proc = Process.objects.create(
            machine=self.machine,
            status=Process.StatusChoices.EXITED,
            pid=999999,
            exit_code=-1,
            started_at=timezone.now(),
        )

        exit_code = proc.poll()

        self.assertEqual(exit_code, 137)
        proc.refresh_from_db()
        self.assertEqual(proc.exit_code, 137)

    def test_process_terminate_dead_process(self):
        """terminate() should handle already-dead process."""
        proc = Process.objects.create(
            machine=self.machine,
            status=Process.StatusChoices.RUNNING,
            pid=999999,
            started_at=timezone.now(),
        )

        result = proc.terminate()

        self.assertFalse(result)
        proc.refresh_from_db()
        self.assertEqual(proc.status, Process.StatusChoices.EXITED)


class TestProcessClassMethods(TestCase):
    """Test Process class methods for querying."""

    def setUp(self):
        """Create machine."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        self.machine = Machine.current()

    def test_get_running(self):
        """get_running should return running processes."""
        proc = Process.objects.create(
            machine=self.machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=99999,
            started_at=timezone.now(),
        )

        running = Process.get_running(process_type=Process.TypeChoices.HOOK)

        self.assertIn(proc, running)

    def test_get_running_count(self):
        """get_running_count should count running processes."""
        for i in range(3):
            Process.objects.create(
                machine=self.machine,
                process_type=Process.TypeChoices.HOOK,
                status=Process.StatusChoices.RUNNING,
                pid=99900 + i,
                started_at=timezone.now(),
            )

        count = Process.get_running_count(process_type=Process.TypeChoices.HOOK)
        self.assertGreaterEqual(count, 3)

    def test_cleanup_stale_running(self):
        """cleanup_stale_running should mark stale processes as exited."""
        stale = Process.objects.create(
            machine=self.machine,
            status=Process.StatusChoices.RUNNING,
            pid=999999,
            started_at=timezone.now() - PID_REUSE_WINDOW - timedelta(hours=1),
        )

        cleaned = Process.cleanup_stale_running()

        self.assertGreaterEqual(cleaned, 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, Process.StatusChoices.EXITED)

    def test_cleanup_stale_running_marks_timed_out_rows_exited(self):
        """cleanup_stale_running should retire RUNNING rows that exceed timeout + grace."""
        stale = Process.objects.create(
            machine=self.machine,
            status=Process.StatusChoices.RUNNING,
            pid=999998,
            timeout=5,
            started_at=timezone.now() - PROCESS_TIMEOUT_GRACE - timedelta(seconds=10),
        )

        cleaned = Process.cleanup_stale_running()

        self.assertGreaterEqual(cleaned, 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, Process.StatusChoices.EXITED)

    def test_cleanup_stale_running_marks_timed_out_live_hooks_exited(self):
        """Timed-out live hook rows should be retired in the DB without trying to kill the process."""
        stale = Process.objects.create(
            machine=self.machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=os.getpid(),
            timeout=5,
            started_at=timezone.now() - PROCESS_TIMEOUT_GRACE - timedelta(seconds=10),
        )

        cleaned = Process.cleanup_stale_running()

        self.assertGreaterEqual(cleaned, 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, Process.StatusChoices.EXITED)

    def test_cleanup_orphaned_workers_marks_dead_root_children_exited(self):
        """cleanup_orphaned_workers should retire rows whose CLI/orchestrator root is gone."""
        import psutil
        from datetime import datetime

        started_at = datetime.fromtimestamp(psutil.Process(os.getpid()).create_time(), tz=timezone.get_current_timezone())
        parent = Process.objects.create(
            machine=self.machine,
            process_type=Process.TypeChoices.CLI,
            status=Process.StatusChoices.RUNNING,
            pid=999997,
            started_at=timezone.now() - timedelta(minutes=5),
        )
        child = Process.objects.create(
            machine=self.machine,
            parent=parent,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=os.getpid(),
            started_at=started_at,
        )

        cleaned = Process.cleanup_orphaned_workers()

        self.assertEqual(cleaned, 1)
        child.refresh_from_db()
        self.assertEqual(child.status, Process.StatusChoices.EXITED)

    def test_cleanup_orphaned_workers_marks_non_running_children_exited(self):
        """cleanup_orphaned_workers should retire child rows whose OS process is already gone."""
        child = Process.objects.create(
            machine=self.machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=999997,
            started_at=timezone.now() - timedelta(minutes=5),
        )

        cleaned = Process.cleanup_orphaned_workers()

        self.assertEqual(cleaned, 1)
        child.refresh_from_db()
        self.assertEqual(child.status, Process.StatusChoices.EXITED)
        self.assertIsNotNone(child.ended_at)
        self.assertEqual(child.exit_code, 0)


class TestProcessStateMachine(TestCase):
    """Test the ProcessMachine state machine."""

    def setUp(self):
        """Create a machine and process for state machine tests."""
        import archivebox.machine.models as models

        models._CURRENT_MACHINE = None
        self.machine = Machine.current()
        self.process = Process.objects.create(
            machine=self.machine,
            cmd=["echo", "test"],
            pwd="/tmp",
        )

    def test_process_state_machine_initial_state(self):
        """ProcessMachine should start in queued state."""
        sm = ProcessMachine(self.process)
        self.assertEqual(sm.current_state_value, Process.StatusChoices.QUEUED)

    def test_process_state_machine_can_start(self):
        """ProcessMachine.can_start() should check cmd and machine."""
        sm = ProcessMachine(self.process)
        self.assertTrue(sm.can_start())

        self.process.cmd = []
        self.process.save()
        sm = ProcessMachine(self.process)
        self.assertFalse(sm.can_start())

    def test_process_state_machine_is_exited(self):
        """ProcessMachine.is_exited() should check exit_code."""
        sm = ProcessMachine(self.process)
        self.assertFalse(sm.is_exited())

        self.process.exit_code = 0
        self.process.save()
        sm = ProcessMachine(self.process)
        self.assertTrue(sm.is_exited())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
