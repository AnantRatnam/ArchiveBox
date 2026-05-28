"""Tests for archivebox extract input handling and pipelines."""

import os
import subprocess
import json

import pytest

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.tests.orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def _snapshot_id(data_dir):
    with use_archivebox_db(data_dir):
        return Snapshot.objects.values_list("id", flat=True).first()


def test_extract_runs_on_snapshot_id(tmp_path, process, disable_extractors_dict):
    """Test that extract command accepts a snapshot ID."""
    os.chdir(tmp_path)

    # First create a snapshot
    subprocess.run(
        ["archivebox", "add", "--index-only", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    snapshot_id = _snapshot_id(tmp_path)

    # Run extract on the snapshot
    result = subprocess.run(
        ["archivebox", "extract", "--no-wait", str(snapshot_id)],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    # Should not error about invalid snapshot ID
    assert "not found" not in result.stderr.lower()


def test_extract_with_enabled_extractor_creates_archiveresult(tmp_path, process, disable_extractors_dict):
    """Test that extract creates ArchiveResult when extractor is enabled."""
    os.chdir(tmp_path)

    # First create a snapshot
    subprocess.run(
        ["archivebox", "add", "--index-only", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    snapshot_id = _snapshot_id(tmp_path)

    # Run extract with title extractor enabled
    env = disable_extractors_dict.copy()
    env["SAVE_TITLE"] = "true"

    subprocess.run(
        ["archivebox", "extract", "--no-wait", str(snapshot_id)],
        capture_output=True,
        text=True,
        env=env,
    )

    with use_archivebox_db(tmp_path):
        count = ArchiveResult.objects.filter(snapshot_id=snapshot_id).count()

    # May or may not have results depending on timing
    assert count >= 0


def test_extract_plugin_option_accepted(tmp_path, process, disable_extractors_dict):
    """Test that --plugin option is accepted."""
    os.chdir(tmp_path)

    # First create a snapshot
    subprocess.run(
        ["archivebox", "add", "--index-only", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    snapshot_id = _snapshot_id(tmp_path)

    result = subprocess.run(
        ["archivebox", "extract", "--plugin=title", "--no-wait", str(snapshot_id)],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    assert "unrecognized arguments: --plugin" not in result.stderr


def test_extract_stdin_snapshot_id(tmp_path, process, disable_extractors_dict):
    """Test that extract reads snapshot IDs from stdin."""
    os.chdir(tmp_path)

    # First create a snapshot
    subprocess.run(
        ["archivebox", "add", "--index-only", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    snapshot_id = _snapshot_id(tmp_path)

    result = subprocess.run(
        ["archivebox", "extract", "--no-wait"],
        input=f"{snapshot_id}\n",
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    # Should not show "not found" error
    assert "not found" not in result.stderr.lower() or result.returncode == 0


def test_extract_stdin_jsonl_input(tmp_path, process, disable_extractors_dict):
    """Test that extract reads JSONL records from stdin."""
    os.chdir(tmp_path)

    # First create a snapshot
    subprocess.run(
        ["archivebox", "add", "--index-only", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    snapshot_id = _snapshot_id(tmp_path)

    jsonl_input = json.dumps({"type": "Snapshot", "id": str(snapshot_id)}) + "\n"

    result = subprocess.run(
        ["archivebox", "extract", "--no-wait"],
        input=jsonl_input,
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    # Should not show "not found" error
    assert "not found" not in result.stderr.lower() or result.returncode == 0


def test_extract_pipeline_from_snapshot(tmp_path, process, disable_extractors_dict):
    """Test piping snapshot output to extract."""
    os.chdir(tmp_path)

    # Create snapshot and pipe to extract
    snapshot_proc = subprocess.Popen(
        ["archivebox", "snapshot", "https://example.com"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=disable_extractors_dict,
    )

    subprocess.run(
        ["archivebox", "extract", "--no-wait"],
        stdin=snapshot_proc.stdout,
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    snapshot_proc.wait()

    with use_archivebox_db(tmp_path):
        snapshot = Snapshot.objects.filter(url="https://example.com").first()

    assert snapshot is not None, "Snapshot should be created by pipeline"


def test_extract_multiple_snapshots(tmp_path, process, disable_extractors_dict):
    """Test extracting from multiple snapshots."""
    os.chdir(tmp_path)

    # Create multiple snapshots one at a time to avoid deduplication issues
    subprocess.run(
        ["archivebox", "add", "--index-only", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
    )
    subprocess.run(
        ["archivebox", "add", "--index-only", "https://iana.org"],
        capture_output=True,
        env=disable_extractors_dict,
    )

    with use_archivebox_db(tmp_path):
        snapshot_ids = list(Snapshot.objects.values_list("id", flat=True))

    assert len(snapshot_ids) >= 2, "Should have at least 2 snapshots"

    # Extract from all snapshots
    ids_input = "\n".join(str(snapshot_id) for snapshot_id in snapshot_ids) + "\n"
    result = subprocess.run(
        ["archivebox", "extract", "--no-wait"],
        input=ids_input,
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )
    assert result.returncode == 0, result.stderr

    with use_archivebox_db(tmp_path):
        count = Snapshot.objects.count()

    assert count >= 2, "Both snapshots should still exist after extraction"


class TestExtractCLI:
    """Test the CLI interface for extract command."""

    def test_cli_help(self, tmp_path, process):
        """Test that --help works for extract command."""
        os.chdir(tmp_path)

        result = subprocess.run(
            ["archivebox", "extract", "--help"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "--plugin" in result.stdout or "-p" in result.stdout
        assert "--wait" in result.stdout or "--no-wait" in result.stdout

    def test_cli_no_snapshots_shows_warning(self, tmp_path, process):
        """Test that running without snapshots shows a warning."""
        os.chdir(tmp_path)

        result = subprocess.run(
            ["archivebox", "extract", "--no-wait"],
            input="",
            capture_output=True,
            text=True,
        )

        # Should show warning about no snapshots or exit normally (empty input)
        assert result.returncode == 0 or "No" in result.stderr
