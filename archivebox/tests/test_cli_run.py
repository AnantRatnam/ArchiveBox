"""
Tests for archivebox run CLI command.

Tests cover:
- run with stdin JSONL (Crawl, Snapshot, ArchiveResult)
- create-or-update behavior (records with/without id)
- pass-through output (for chaining)
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from archivebox.tests.conftest import (
    run_archivebox_cmd,
    parse_jsonl_output,
    create_test_url,
    create_test_crawl_json,
    create_test_snapshot_json,
)

RUN_TEST_ENV = {
    "PLUGINS": "favicon",
    "SAVE_FAVICON": "True",
}


class TestRunWithCrawl:
    """Tests for `archivebox run` with Crawl input."""

    def test_run_with_new_crawl(self, initialized_archive):
        """Run creates and processes a new Crawl (no id)."""
        crawl_record = create_test_crawl_json()

        stdout, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(crawl_record),
            data_dir=initialized_archive,
            timeout=120,
            env=RUN_TEST_ENV,
        )

        assert code == 0, f"Command failed: {stderr}"

        # Should output the created Crawl
        records = parse_jsonl_output(stdout)
        crawl_records = [r for r in records if r.get("type") == "Crawl"]
        assert len(crawl_records) >= 1
        assert crawl_records[0].get("id")  # Should have an id now

    def test_run_with_existing_crawl(self, initialized_archive):
        """Run re-queues an existing Crawl (with id)."""
        url = create_test_url()

        # First create a crawl
        stdout1, _, _ = run_archivebox_cmd(["crawl", "create", url], data_dir=initialized_archive, env=RUN_TEST_ENV)
        crawl = parse_jsonl_output(stdout1)[0]

        # Run with the existing crawl
        stdout2, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(crawl),
            data_dir=initialized_archive,
            timeout=120,
            env=RUN_TEST_ENV,
        )

        assert code == 0
        records = parse_jsonl_output(stdout2)
        assert len(records) >= 1


class TestRunWithSnapshot:
    """Tests for `archivebox run` with Snapshot input."""

    def test_run_with_new_snapshot(self, initialized_archive):
        """Run creates and processes a new Snapshot (no id, just url)."""
        snapshot_record = create_test_snapshot_json()

        stdout, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(snapshot_record),
            data_dir=initialized_archive,
            timeout=120,
            env=RUN_TEST_ENV,
        )

        assert code == 0, f"Command failed: {stderr}"

        records = parse_jsonl_output(stdout)
        snapshot_records = [r for r in records if r.get("type") == "Snapshot"]
        assert len(snapshot_records) >= 1
        assert snapshot_records[0].get("id")

    def test_run_with_existing_snapshot(self, initialized_archive):
        """Run re-queues an existing Snapshot (with id)."""
        url = create_test_url()

        # First create a snapshot
        stdout1, _, _ = run_archivebox_cmd(["snapshot", "create", url], data_dir=initialized_archive, env=RUN_TEST_ENV)
        snapshot = parse_jsonl_output(stdout1)[0]

        # Run with the existing snapshot
        stdout2, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(snapshot),
            data_dir=initialized_archive,
            timeout=120,
            env=RUN_TEST_ENV,
        )

        assert code == 0
        records = parse_jsonl_output(stdout2)
        assert len(records) >= 1

    def test_run_with_plain_url(self, initialized_archive):
        """Run accepts plain URL records (no type field)."""
        url = create_test_url()
        url_record = {"url": url}

        stdout, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(url_record),
            data_dir=initialized_archive,
            timeout=120,
            env=RUN_TEST_ENV,
        )

        assert code == 0
        records = parse_jsonl_output(stdout)
        assert len(records) >= 1


class TestRunWithArchiveResult:
    """Tests for `archivebox run` with ArchiveResult input."""

    def test_run_requeues_failed_archiveresult(self, initialized_archive):
        """Run re-queues a failed ArchiveResult."""
        url = create_test_url()

        # Create snapshot and archive result
        stdout1, _, _ = run_archivebox_cmd(["snapshot", "create", url], data_dir=initialized_archive, env=RUN_TEST_ENV)
        snapshot = parse_jsonl_output(stdout1)[0]

        stdout2, _, _ = run_archivebox_cmd(
            ["archiveresult", "create", "--plugin=favicon"],
            stdin=json.dumps(snapshot),
            data_dir=initialized_archive,
            env=RUN_TEST_ENV,
        )
        ar = next(r for r in parse_jsonl_output(stdout2) if r.get("type") == "ArchiveResult")

        # Update to failed
        ar["status"] = "failed"
        run_archivebox_cmd(
            ["archiveresult", "update", "--status=failed"],
            stdin=json.dumps(ar),
            data_dir=initialized_archive,
            env=RUN_TEST_ENV,
        )

        # Now run should re-queue it
        stdout3, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(ar),
            data_dir=initialized_archive,
            timeout=120,
            env=RUN_TEST_ENV,
        )

        assert code == 0
        records = parse_jsonl_output(stdout3)
        ar_records = [r for r in records if r.get("type") == "ArchiveResult"]
        assert len(ar_records) >= 1


class TestRunPassThrough:
    """Tests for pass-through behavior in `archivebox run`."""

    def test_run_passes_through_unknown_types(self, initialized_archive):
        """Run passes through records with unknown types."""
        unknown_record = {"type": "Unknown", "id": "fake-id", "data": "test"}

        stdout, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(unknown_record),
            data_dir=initialized_archive,
        )

        assert code == 0
        records = parse_jsonl_output(stdout)
        unknown_records = [r for r in records if r.get("type") == "Unknown"]
        assert len(unknown_records) == 1
        assert unknown_records[0]["data"] == "test"

    def test_run_outputs_all_processed_records(self, initialized_archive):
        """Run outputs all processed records for chaining."""
        url = create_test_url()
        crawl_record = create_test_crawl_json(urls=[url])

        stdout, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(crawl_record),
            data_dir=initialized_archive,
            timeout=120,
            env=RUN_TEST_ENV,
        )

        assert code == 0
        records = parse_jsonl_output(stdout)
        # Should have at least the Crawl in output
        assert len(records) >= 1


class TestRunMixedInput:
    """Tests for `archivebox run` with mixed record types."""

    def test_run_handles_mixed_types(self, initialized_archive):
        """Run handles mixed Crawl/Snapshot/ArchiveResult input."""
        crawl = create_test_crawl_json()
        snapshot = create_test_snapshot_json()
        unknown = {"type": "Tag", "id": "fake", "name": "test"}

        stdin = "\n".join(
            [
                json.dumps(crawl),
                json.dumps(snapshot),
                json.dumps(unknown),
            ],
        )

        stdout, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=stdin,
            data_dir=initialized_archive,
            timeout=120,
            env=RUN_TEST_ENV,
        )

        assert code == 0
        records = parse_jsonl_output(stdout)

        types = {r.get("type") for r in records}
        # Should have processed Crawl and Snapshot, passed through Tag
        assert "Crawl" in types or "Snapshot" in types or "Tag" in types


class TestRunEmpty:
    """Tests for `archivebox run` edge cases."""

    def test_run_empty_stdin(self, initialized_archive):
        """Run with empty stdin returns success."""
        stdout, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin="",
            data_dir=initialized_archive,
        )

        assert code == 0

    def test_run_no_records_to_process(self, initialized_archive):
        """Run with only pass-through records shows message."""
        unknown = {"type": "Unknown", "id": "fake"}

        stdout, stderr, code = run_archivebox_cmd(
            ["run"],
            stdin=json.dumps(unknown),
            data_dir=initialized_archive,
        )

        assert code == 0
        assert "No records to process" in stderr


class TestRunDaemonMode:
    @pytest.mark.parametrize("stdin_kind", ["malformed", "valid-snapshot"])
    def test_run_daemon_ignores_piped_stdin_and_starts_real_runner(self, initialized_archive, db, stdin_kind):
        from archivebox.machine.models import Process
        from archivebox.core.models import Snapshot
        from archivebox.tests.test_orm_helpers import use_archivebox_db

        snapshot_url = None
        if stdin_kind == "valid-snapshot":
            snapshot_url = create_test_url()
            piped_stdin = json.dumps(create_test_snapshot_json(url=snapshot_url)) + "\n"
        else:
            piped_stdin = "{this is not jsonl}\n"

        env = os.environ.copy()
        env.update(
            {
                "DATA_DIR": str(initialized_archive),
                "USE_COLOR": "False",
                "SHOW_PROGRESS": "False",
                "USE_INDEXING_BACKEND": "False",
            },
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "archivebox", "run", "--daemon"],
            cwd=initialized_archive,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        try:
            proc.stdin.write(piped_stdin)
            proc.stdin.close()

            deadline = time.monotonic() + 20
            started = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    stdout = proc.stdout.read()
                    stderr = proc.stderr.read()
                    pytest.fail(f"daemon exited before starting runner: code={proc.returncode}\nstdout={stdout}\nstderr={stderr}")
                with use_archivebox_db(initialized_archive):
                    started = Process.objects.filter(
                        process_type=Process.TypeChoices.ORCHESTRATOR,
                        status=Process.StatusChoices.RUNNING,
                        pid=proc.pid,
                    ).exists()
                if started:
                    break
                time.sleep(0.25)

            assert started is True
            if snapshot_url is not None:
                with use_archivebox_db(initialized_archive):
                    assert not Snapshot.objects.filter(url=snapshot_url).exists()
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)

        stdout = proc.stdout.read()
        stderr = proc.stderr.read()
        assert proc.returncode == 0, stdout + stderr
        assert "No records to process" not in stderr


@pytest.mark.django_db
class TestRecoverOrchestratorState:
    def test_recover_orchestrator_state_unlocks_started_crawl_with_pending_snapshot(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=None,
        )
        Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=None,
        )

        recovered = recover_orchestrator_state()

        crawl.refresh_from_db()
        assert recovered["unlocked_crawls"] == 1
        assert crawl.status == Crawl.StatusChoices.STARTED
        assert crawl.retry_at is not None

    def test_recover_orchestrator_state_seals_started_crawl_with_finished_snapshots(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=None,
        )
        Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
            retry_at=None,
        )

        recovered = recover_orchestrator_state()

        crawl.refresh_from_db()
        assert recovered["sealed_crawls"] == 1
        assert crawl.status == Crawl.StatusChoices.SEALED
        assert crawl.retry_at is None

    def test_recover_orchestrator_state_repairs_retry_at_status_invariants(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        user_id = get_or_create_system_user_pk()
        queued_crawl = Crawl.objects.create(
            urls="https://example.com/queued-crawl",
            created_by_id=user_id,
            status=Crawl.StatusChoices.QUEUED,
            retry_at=None,
        )
        sealed_crawl = Crawl.objects.create(
            urls="https://example.com/sealed-crawl",
            created_by_id=user_id,
            status=Crawl.StatusChoices.SEALED,
            retry_at=timezone.now(),
        )
        queued_snapshot = Snapshot.objects.create(
            url="https://example.com/queued-snapshot",
            crawl=queued_crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=None,
        )
        sealed_snapshot = Snapshot.objects.create(
            url="https://example.com/sealed-snapshot",
            crawl=sealed_crawl,
            status=Snapshot.StatusChoices.SEALED,
            retry_at=timezone.now(),
        )

        recovered = recover_orchestrator_state()

        queued_crawl.refresh_from_db()
        sealed_crawl.refresh_from_db()
        queued_snapshot.refresh_from_db()
        sealed_snapshot.refresh_from_db()

        assert recovered["queued_crawls_unlocked"] == 1
        assert recovered["queued_snapshots_unlocked"] == 1
        assert queued_crawl.status == Crawl.StatusChoices.QUEUED
        assert queued_crawl.retry_at is not None
        assert sealed_crawl.status == Crawl.StatusChoices.SEALED
        assert sealed_crawl.retry_at is not None
        assert queued_snapshot.status == Snapshot.StatusChoices.QUEUED
        assert queued_snapshot.retry_at is not None
        assert sealed_snapshot.status == Snapshot.StatusChoices.SEALED
        assert sealed_snapshot.retry_at is not None

    def test_recover_orchestrator_state_requeues_backoff_archiveresults(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.SEALED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
            retry_at=None,
        )
        result = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.BACKOFF,
        )

        recovered = recover_orchestrator_state()

        result.refresh_from_db()
        snapshot.refresh_from_db()
        crawl.refresh_from_db()

        assert recovered["requeued_archiveresults"] == 1
        assert recovered["requeued_snapshots"] == 1
        assert result.status == ArchiveResult.StatusChoices.QUEUED
        assert snapshot.status == Snapshot.StatusChoices.QUEUED
        assert crawl.status == Crawl.StatusChoices.SEALED

    def test_recover_orchestrator_state_leaves_due_queued_snapshot_for_runner_even_with_final_results(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.QUEUED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=None,
        )
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
        )

        recovered = recover_orchestrator_state()

        snapshot.refresh_from_db()
        crawl.refresh_from_db()

        assert recovered["sealed_queued_snapshots"] == 0
        assert recovered["sealed_queued_crawls"] == 0
        assert snapshot.status == Snapshot.StatusChoices.QUEUED
        assert snapshot.retry_at is not None
        assert snapshot.downloaded_at is None
        assert crawl.status == Crawl.StatusChoices.QUEUED
        assert crawl.retry_at is not None

    def test_recover_orchestrator_state_seals_stale_queued_snapshot_with_final_results(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        old = timezone.now() - timedelta(hours=13)
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.QUEUED,
            retry_at=old,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=old,
        )
        result = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
        )
        Crawl.objects.filter(pk=crawl.pk).update(modified_at=old)
        Snapshot.objects.filter(pk=snapshot.pk).update(modified_at=old)
        ArchiveResult.objects.filter(pk=result.pk).update(modified_at=old)

        recovered = recover_orchestrator_state()

        snapshot.refresh_from_db()
        crawl.refresh_from_db()

        assert recovered["sealed_queued_snapshots"] == 1
        assert recovered["sealed_queued_crawls"] == 1
        assert snapshot.status == Snapshot.StatusChoices.SEALED
        assert snapshot.retry_at is None
        assert snapshot.downloaded_at is not None
        assert crawl.status == Crawl.StatusChoices.SEALED
        assert crawl.retry_at is None

    def test_recover_orchestrator_state_raises_on_stale_active_crawl(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.services.runner import recover_orchestrator_state

        old = timezone.now() - timedelta(hours=13)
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.QUEUED,
            retry_at=old,
        )
        Crawl.objects.filter(id=crawl.id).update(modified_at=old, retry_at=old)

        with pytest.raises(RuntimeError, match="Stuck crawl invariant violated"):
            recover_orchestrator_state()

    def test_recover_orchestrator_state_unlocks_started_snapshot_without_running_result(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.SEALED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.STARTED,
            retry_at=None,
        )

        recovered = recover_orchestrator_state()

        snapshot.refresh_from_db()
        crawl.refresh_from_db()

        assert recovered["unlocked_snapshots"] == 1
        assert snapshot.status == Snapshot.StatusChoices.STARTED
        assert snapshot.retry_at is not None
        assert crawl.status == Crawl.StatusChoices.SEALED
        assert crawl.retry_at is None

    def test_recover_orchestrator_state_requeues_sealed_snapshot_with_queued_results(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.SEALED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
            retry_at=None,
        )
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.QUEUED,
        )

        recovered = recover_orchestrator_state()

        snapshot.refresh_from_db()
        crawl.refresh_from_db()

        assert recovered["requeued_snapshots"] == 1
        assert snapshot.status == Snapshot.StatusChoices.QUEUED
        assert snapshot.retry_at is not None
        assert crawl.status == Crawl.StatusChoices.SEALED
        assert crawl.retry_at is None

    def test_recover_orchestrator_state_ignores_sealed_downloaded_snapshot_without_results(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.SEALED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
            downloaded_at=timezone.now(),
            retry_at=None,
        )

        recovered = recover_orchestrator_state()

        snapshot.refresh_from_db()
        crawl.refresh_from_db()

        assert recovered["requeued_snapshots"] == 0
        assert recovered["unlocked_snapshots"] == 0
        assert snapshot.status == Snapshot.StatusChoices.SEALED
        assert snapshot.retry_at is None
        assert crawl.status == Crawl.StatusChoices.SEALED
        assert crawl.retry_at is None

    def test_recover_orchestrator_state_seals_started_snapshot_with_final_results(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.STARTED,
            retry_at=None,
        )
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
        )

        recovered = recover_orchestrator_state()

        snapshot.refresh_from_db()
        assert recovered["sealed_snapshots"] == 1
        assert snapshot.status == Snapshot.StatusChoices.SEALED
        assert snapshot.retry_at is None


@pytest.mark.django_db
class TestRunDueCrawlState:
    def test_maintenance_only_runner_does_not_start_regular_queued_crawls(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.services.runner import run_pending_crawls

        now = timezone.now()
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.QUEUED,
            retry_at=now,
        )

        assert run_pending_crawls(daemon=False, maintenance_only=True) == 0

        crawl.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.QUEUED
        assert crawl.retry_at == now
        assert crawl.snapshot_set.count() == 0

    def test_snapshot_start_writes_short_future_lease(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=timezone.now(),
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=timezone.now(),
        )

        snapshot.sm.tick()
        snapshot.refresh_from_db()

        assert snapshot.status == Snapshot.StatusChoices.STARTED
        assert snapshot.retry_at is not None
        assert snapshot.retry_at > timezone.now()

    def test_abandoned_started_snapshot_results_are_reset_locally_for_resume(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import reset_abandoned_snapshot_results

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=timezone.now(),
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.STARTED,
            retry_at=timezone.now(),
        )
        abandoned = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.STARTED,
            output_str="partial output should be cleared",
            output_files={"partial.txt": {"size": 12}},
            output_size=12,
            start_ts=timezone.now(),
        )
        queued = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="wget",
            hook_name="on_Snapshot__40_wget",
            status=ArchiveResult.StatusChoices.QUEUED,
        )
        finished = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="favicon",
            hook_name="on_Snapshot__01_favicon",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
            output_str="keep me",
            output_files={"favicon.ico": {"size": 1}},
            output_size=1,
        )

        reset_abandoned_snapshot_results(snapshot)

        abandoned.refresh_from_db()
        queued.refresh_from_db()
        finished.refresh_from_db()

        assert abandoned.status == ArchiveResult.StatusChoices.QUEUED
        assert abandoned.output_str == ""
        assert abandoned.output_files == {}
        assert abandoned.output_size == 0
        assert queued.status == ArchiveResult.StatusChoices.QUEUED
        assert finished.status == ArchiveResult.StatusChoices.SUCCEEDED
        assert finished.output_str == "keep me"
        assert finished.output_files == {"favicon.ico": {"size": 1}}

    def test_due_started_snapshot_with_live_child_extends_lease_without_reset(self):
        import os
        from datetime import datetime

        import psutil
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.machine.models import Machine, NetworkInterface, Process
        from archivebox.services.runner import run_due_snapshot

        now = timezone.now()
        os_proc = psutil.Process(os.getpid())
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=now,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.STARTED,
            retry_at=now,
        )
        process = Process.objects.create(
            machine=Machine.current(),
            iface=NetworkInterface.current(),
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=os.getpid(),
            started_at=datetime.fromtimestamp(os_proc.create_time(), tz=timezone.get_current_timezone()),
            cmd=os_proc.cmdline(),
            pwd=str(snapshot.output_dir / "title"),
        )
        result = ArchiveResult.objects.create(
            snapshot=snapshot,
            process=process,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.STARTED,
            output_str="live work should not be reset",
            output_files={"partial.txt": {"size": 12}},
            output_size=12,
        )

        assert run_due_snapshot(snapshot, lock_seconds=60) is True

        snapshot.refresh_from_db()
        result.refresh_from_db()
        assert snapshot.status == Snapshot.StatusChoices.STARTED
        assert snapshot.retry_at is not None
        assert snapshot.retry_at > now
        assert result.status == ArchiveResult.StatusChoices.STARTED
        assert result.output_str == "live work should not be reset"
        assert result.output_files == {"partial.txt": {"size": 12}}
        assert result.output_size == 12

    def test_run_due_crawl_seals_finished_started_crawl(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import run_due_crawl

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=timezone.now(),
        )
        Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
            retry_at=None,
        )

        assert run_due_crawl(crawl, lock_seconds=10) is True

        crawl.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.SEALED
        assert crawl.retry_at is None

    def test_run_due_crawl_preserves_next_future_snapshot_retry(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import run_due_crawl

        future = timezone.now() + timedelta(hours=1)
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=timezone.now(),
        )
        Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=future,
        )

        assert run_due_crawl(crawl, lock_seconds=10) is True

        crawl.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.STARTED
        assert crawl.retry_at == future

    def test_run_due_crawl_preserves_next_future_started_snapshot_lease(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import run_due_crawl

        future = timezone.now() + timedelta(minutes=5)
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=timezone.now(),
        )
        Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.STARTED,
            retry_at=future,
        )

        assert run_due_crawl(crawl, lock_seconds=10) is True

        crawl.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.STARTED
        assert crawl.retry_at == future

    def test_run_due_crawl_unlocks_null_retry_queued_snapshot(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import run_due_crawl

        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=timezone.now(),
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=None,
        )

        assert run_due_crawl(crawl, lock_seconds=10) is True

        crawl.refresh_from_db()
        snapshot.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.STARTED
        assert crawl.retry_at is not None
        assert snapshot.retry_at is not None


@pytest.mark.django_db
class TestRecoverOrchestratorStateRedFailureModes:
    def test_recovery_does_not_seal_queued_snapshot_waiting_for_future_retry_even_with_final_results(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        future = timezone.now() + timedelta(days=1)
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=future,
        )
        snapshot = Snapshot.objects.create(
            url="https://example.com",
            crawl=crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=future,
        )
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
        )

        recover_orchestrator_state()

        snapshot.refresh_from_db()
        assert snapshot.status == Snapshot.StatusChoices.QUEUED
        assert snapshot.retry_at == future

    def test_recovery_does_not_seal_queued_crawl_waiting_for_future_retry_even_with_finished_snapshots(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        future = timezone.now() + timedelta(days=1)
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.QUEUED,
            retry_at=future,
        )
        Snapshot.objects.create(url="https://example.com", crawl=crawl, status=Snapshot.StatusChoices.SEALED, retry_at=None)

        recover_orchestrator_state()

        crawl.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.QUEUED
        assert crawl.retry_at == future

    def test_recovery_keeps_sealed_parent_when_future_retry_child_is_scheduled(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        future = timezone.now() + timedelta(days=1)
        crawl = Crawl.objects.create(
            urls="https://blog.sweeting.me",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.SEALED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://blog.sweeting.me",
            crawl=crawl,
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=future,
        )

        recover_orchestrator_state()

        crawl.refresh_from_db()
        snapshot.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.SEALED
        assert crawl.retry_at is None
        assert snapshot.retry_at == future

    def test_recovery_unlocks_started_parent_to_future_retry_child_not_now(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        future = timezone.now() + timedelta(days=1)
        crawl = Crawl.objects.create(
            urls="https://www.mathjax.org/",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=None,
        )
        Snapshot.objects.create(url="https://www.mathjax.org/", crawl=crawl, status=Snapshot.StatusChoices.QUEUED, retry_at=future)

        recover_orchestrator_state()

        crawl.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.STARTED
        assert crawl.retry_at == future

    def test_recovery_requeues_started_archiveresult_without_process(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://www.mathjax.org/",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://www.mathjax.org/",
            crawl=crawl,
            status=Snapshot.StatusChoices.STARTED,
            retry_at=None,
        )
        result = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.STARTED,
        )

        recover_orchestrator_state()

        result.refresh_from_db()
        assert result.status == ArchiveResult.StatusChoices.QUEUED

    def test_recovery_requeues_started_archiveresult_with_exited_process(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.machine.models import Machine, NetworkInterface, Process
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://revealjs.com/",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(url="https://revealjs.com/", crawl=crawl, status=Snapshot.StatusChoices.STARTED, retry_at=None)
        process = Process.objects.create(
            machine=Machine.current(refresh=True),
            iface=NetworkInterface.current(refresh=True),
            process_type=Process.TypeChoices.HOOK,
            worker_type="archiveresult",
            pwd=str(snapshot.output_dir / "title"),
            cmd=["python", "--version"],
            status=Process.StatusChoices.EXITED,
            retry_at=None,
            exit_code=0,
            ended_at=timezone.now(),
        )
        result = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.STARTED,
            process=process,
        )

        recover_orchestrator_state()

        result.refresh_from_db()
        assert result.status == ArchiveResult.StatusChoices.QUEUED

    def test_recovery_requeues_sealed_snapshot_started_result_with_exited_process_result_too(self):
        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.machine.models import Machine, NetworkInterface, Process
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://pdfobject.com/pdf/sample-3pp.pdf",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.SEALED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://pdfobject.com/pdf/sample-3pp.pdf",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
            retry_at=None,
        )
        process = Process.objects.create(
            machine=Machine.current(refresh=True),
            iface=NetworkInterface.current(refresh=True),
            process_type=Process.TypeChoices.HOOK,
            worker_type="archiveresult",
            pwd=str(snapshot.output_dir / "pdf"),
            cmd=["python", "--version"],
            status=Process.StatusChoices.EXITED,
            retry_at=None,
            exit_code=0,
            ended_at=timezone.now(),
        )
        result = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="pdf",
            hook_name="on_Snapshot__50_pdf",
            status=ArchiveResult.StatusChoices.STARTED,
            process=process,
        )

        recover_orchestrator_state()

        snapshot.refresh_from_db()
        result.refresh_from_db()
        assert snapshot.status == Snapshot.StatusChoices.QUEUED
        assert result.status == ArchiveResult.StatusChoices.QUEUED

    def test_recovery_requeues_started_snapshot_result_before_unlocking_snapshot(self):
        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.services.runner import recover_orchestrator_state

        crawl = Crawl.objects.create(
            urls="https://mermaid-js.github.io/mermaid/",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=None,
        )
        snapshot = Snapshot.objects.create(
            url="https://mermaid-js.github.io/mermaid/",
            crawl=crawl,
            status=Snapshot.StatusChoices.STARTED,
            retry_at=None,
        )
        result = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            hook_name="on_Snapshot__01_title",
            status=ArchiveResult.StatusChoices.STARTED,
        )

        recover_orchestrator_state()

        snapshot.refresh_from_db()
        result.refresh_from_db()
        assert result.status == ArchiveResult.StatusChoices.QUEUED
        assert snapshot.retry_at is not None

    def test_crawl_runner_load_run_state_does_not_return_future_retry_snapshots(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import CrawlRunner

        future = timezone.now() + timedelta(days=1)
        crawl = Crawl.objects.create(
            urls="https://example.com",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=future,
        )
        Snapshot.objects.create(url="https://example.com", crawl=crawl, status=Snapshot.StatusChoices.QUEUED, retry_at=future)

        runner = CrawlRunner(crawl, selected_plugins=[])

        assert runner.load_run_state() == []

    def test_crawl_runner_finalize_run_state_preserves_next_future_snapshot_retry(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot
        from archivebox.services.runner import CrawlRunner

        future = timezone.now() + timedelta(days=1)
        crawl = Crawl.objects.create(
            urls="https://blog.sweeting.me",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.STARTED,
            retry_at=None,
        )
        Snapshot.objects.create(url="https://blog.sweeting.me", crawl=crawl, status=Snapshot.StatusChoices.QUEUED, retry_at=future)

        runner = CrawlRunner(crawl, selected_plugins=[])
        runner.finalize_run_state()

        crawl.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.STARTED
        assert crawl.retry_at == future

    def test_recovery_raises_stale_due_crawl_even_with_recent_unrelated_process_path_containing_crawl_id(self):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.base_models.models import get_or_create_system_user_pk
        from archivebox.crawls.models import Crawl
        from archivebox.machine.models import Machine, NetworkInterface, Process
        from archivebox.services.runner import recover_orchestrator_state

        old = timezone.now() - timedelta(hours=13)
        crawl = Crawl.objects.create(
            urls="https://github.com/nodeca/pica",
            created_by_id=get_or_create_system_user_pk(),
            status=Crawl.StatusChoices.QUEUED,
            retry_at=old,
        )
        Crawl.objects.filter(id=crawl.id).update(modified_at=old, retry_at=old)
        Process.objects.create(
            machine=Machine.current(refresh=True),
            iface=NetworkInterface.current(refresh=True),
            process_type=Process.TypeChoices.HOOK,
            worker_type="archiveresult",
            pwd=f"/tmp/not-an-archivebox-child/{crawl.id}/title",
            cmd=["python", "--version"],
            status=Process.StatusChoices.EXITED,
            retry_at=None,
            exit_code=0,
            ended_at=timezone.now(),
        )

        with pytest.raises(RuntimeError, match="Stuck crawl invariant violated"):
            recover_orchestrator_state()

    def test_recovery_does_not_crash_on_invalid_utf8_process_logs(self, tmp_path):
        from datetime import timedelta

        from django.utils import timezone

        from archivebox.machine.models import Machine, NetworkInterface, Process
        from archivebox.services.runner import recover_orchestrator_state

        runtime_dir = tmp_path / "https_example_com" / ".hooks" / "on_Snapshot__01_title.py"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "stdout.log").write_bytes(b"\\xff\\xfe\\xfa")
        process = Process.objects.create(
            machine=Machine.current(refresh=True),
            iface=NetworkInterface.current(refresh=True),
            process_type=Process.TypeChoices.HOOK,
            worker_type="archiveresult",
            pwd=str(tmp_path / "https_example_com"),
            cmd=["on_Snapshot__01_title.py"],
            status=Process.StatusChoices.RUNNING,
            retry_at=None,
            pid=999999,
            started_at=timezone.now() - timedelta(hours=1),
            timeout=1,
        )

        recover_orchestrator_state()

        process.refresh_from_db()
        assert process.status == Process.StatusChoices.EXITED
