#!/usr/bin/env python3
"""
Tests for archivebox list command.
Verify list emits snapshot JSONL and applies the documented filters.
"""

import json
import os
import subprocess

import pytest

from archivebox.core.models import Snapshot
from archivebox.tests.conftest import create_test_url, parse_jsonl_output, run_archivebox_cmd, run_queued_crawls
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def _parse_jsonl(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip().startswith("{")]


def test_list_outputs_existing_snapshots_as_jsonl(tmp_path, process, disable_extractors_dict):
    """Test that list prints one JSON object per stored snapshot."""
    os.chdir(tmp_path)
    for url in ["https://example.com", "https://iana.org"]:
        subprocess.run(
            ["archivebox", "add", "--index-only", "--depth=0", url],
            capture_output=True,
            env=disable_extractors_dict,
            check=True,
        )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    result = subprocess.run(
        ["archivebox", "list"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    rows = _parse_jsonl(result.stdout)
    urls = {row["url"] for row in rows}

    assert result.returncode == 0, result.stderr
    assert "https://example.com" in urls
    assert "https://iana.org" in urls


def test_list_filters_by_url_icontains(tmp_path, process, disable_extractors_dict):
    """Test that list --url__icontains returns only matching snapshots."""
    os.chdir(tmp_path)
    for url in ["https://example.com", "https://iana.org"]:
        subprocess.run(
            ["archivebox", "add", "--index-only", "--depth=0", url],
            capture_output=True,
            env=disable_extractors_dict,
            check=True,
        )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    result = subprocess.run(
        ["archivebox", "list", "--url__icontains", "example.com"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    rows = _parse_jsonl(result.stdout)
    assert result.returncode == 0, result.stderr
    assert len(rows) == 1
    assert rows[0]["url"] == "https://example.com"


def test_list_filters_by_crawl_id_and_limit(tmp_path, process, disable_extractors_dict):
    """Test that crawl-id and limit filters constrain the result set."""
    os.chdir(tmp_path)
    for url in ["https://example.com", "https://iana.org"]:
        subprocess.run(
            ["archivebox", "add", "--index-only", "--depth=0", url],
            capture_output=True,
            env=disable_extractors_dict,
            check=True,
        )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    with use_archivebox_db(tmp_path):
        crawl_id = str(Snapshot.objects.values_list("crawl_id", flat=True).get(url="https://example.com"))

    result = subprocess.run(
        ["archivebox", "list", "--crawl-id", crawl_id, "--limit", "1"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    rows = _parse_jsonl(result.stdout)
    assert result.returncode == 0, result.stderr
    assert len(rows) == 1
    assert rows[0]["crawl_id"].replace("-", "") == crawl_id.replace("-", "")
    assert rows[0]["url"] == "https://example.com"


def test_list_filters_by_status(tmp_path, process, disable_extractors_dict):
    """Test that list can filter using the current snapshot status."""
    os.chdir(tmp_path)
    subprocess.run(
        ["archivebox", "add", "--index-only", "--depth=0", "https://example.com"],
        capture_output=True,
        env=disable_extractors_dict,
        check=True,
    )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    with use_archivebox_db(tmp_path):
        status = Snapshot.objects.values_list("status", flat=True).get()

    result = subprocess.run(
        ["archivebox", "list", "--status", status],
        capture_output=True,
        text=True,
        timeout=30,
    )

    rows = _parse_jsonl(result.stdout)
    assert result.returncode == 0, result.stderr
    assert len(rows) == 1
    assert rows[0]["status"] == status


def test_list_help_lists_filter_options(tmp_path, process):
    """Test that list --help documents the supported filter flags."""
    os.chdir(tmp_path)

    result = subprocess.run(
        ["archivebox", "list", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--url__icontains" in result.stdout
    assert "--crawl-id" in result.stdout
    assert "--limit" in result.stdout
    assert "--search" in result.stdout


def test_list_allows_sort_with_limit(tmp_path, process, disable_extractors_dict):
    """Test that list can sort and then apply limit without queryset slicing errors."""
    os.chdir(tmp_path)
    for url in ["https://example.com", "https://iana.org", "https://example.net"]:
        subprocess.run(
            ["archivebox", "add", "--index-only", "--depth=0", url],
            capture_output=True,
            env=disable_extractors_dict,
            check=True,
        )
    run_queued_crawls(tmp_path, disable_extractors_dict)

    result = subprocess.run(
        ["archivebox", "list", "--limit", "2", "--sort", "-created_at"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    rows = _parse_jsonl(result.stdout)
    assert result.returncode == 0, result.stderr
    assert len(rows) == 2


def test_snapshot_list_search_meta(initialized_archive):
    """snapshot list should support metadata search mode."""
    url = create_test_url(domain="meta-search-example.com")
    run_archivebox_cmd(["snapshot", "create", url], data_dir=initialized_archive)

    stdout, stderr, code = run_archivebox_cmd(
        ["snapshot", "list", "--search=meta", "meta-search-example.com"],
        data_dir=initialized_archive,
    )

    assert code == 0, f"Command failed: {stderr}"
    records = parse_jsonl_output(stdout)
    assert len(records) == 1
    assert "meta-search-example.com" in records[0]["url"]


def test_list_search_meta_matches_metadata(initialized_archive):
    """top-level list --search=meta should apply metadata search to the queryset."""
    url = create_test_url(domain="top-level-meta-search-example.com")
    run_archivebox_cmd(["snapshot", "create", url], data_dir=initialized_archive)

    stdout, stderr, code = run_archivebox_cmd(
        ["list", "--search=meta", "top-level-meta-search-example.com"],
        data_dir=initialized_archive,
    )

    assert code == 0, f"Command failed: {stderr}"
    records = parse_jsonl_output(stdout)
    assert len(records) == 1
    assert "top-level-meta-search-example.com" in records[0]["url"]


def test_search_command_finds_snapshots(initialized_archive):
    run_archivebox_cmd(["snapshot", "create", "https://example.com"], data_dir=initialized_archive)

    stdout, stderr, code = run_archivebox_cmd(["search", "example"], data_dir=initialized_archive)

    assert code == 0, stderr
    assert "example" in stdout


def test_search_command_returns_no_results_for_missing_term(initialized_archive):
    run_archivebox_cmd(["snapshot", "create", "https://example.com"], data_dir=initialized_archive)

    _stdout, _stderr, code = run_archivebox_cmd(["search", "nonexistentterm12345"], data_dir=initialized_archive)

    assert code in [0, 1]


def test_search_command_on_empty_archive(initialized_archive):
    _stdout, _stderr, code = run_archivebox_cmd(["search", "anything"], data_dir=initialized_archive)

    assert code in [0, 1]


def test_search_command_json_outputs_matching_snapshots(initialized_archive):
    run_archivebox_cmd(["snapshot", "create", "https://example.com"], data_dir=initialized_archive)

    stdout, stderr, code = run_archivebox_cmd(["search", "--json"], data_dir=initialized_archive)

    assert code == 0, stderr
    payload = json.loads(stdout)
    assert any("example.com" in row.get("url", "") for row in payload)


def test_search_command_json_with_headers_wraps_links_payload(initialized_archive):
    run_archivebox_cmd(["snapshot", "create", "https://example.com"], data_dir=initialized_archive)

    stdout, stderr, code = run_archivebox_cmd(["search", "--json", "--with-headers"], data_dir=initialized_archive)

    assert code == 0, stderr
    payload = json.loads(stdout)
    links = payload.get("links", payload)
    assert any("example.com" in row.get("url", "") for row in links)


def test_search_command_html_outputs_markup(initialized_archive):
    run_archivebox_cmd(["snapshot", "create", "https://example.com"], data_dir=initialized_archive)

    stdout, stderr, code = run_archivebox_cmd(["search", "--html"], data_dir=initialized_archive)

    assert code == 0, stderr
    assert "<" in stdout


def test_search_command_csv_outputs_requested_column(initialized_archive):
    run_archivebox_cmd(["snapshot", "create", "https://example.com"], data_dir=initialized_archive)

    stdout, stderr, code = run_archivebox_cmd(["search", "--csv", "url", "--with-headers"], data_dir=initialized_archive)

    assert code == 0, stderr
    assert "url" in stdout
    assert "example.com" in stdout


def test_search_command_with_headers_requires_structured_output_format(initialized_archive):
    _stdout, stderr, code = run_archivebox_cmd(["search", "--with-headers"], data_dir=initialized_archive)

    assert code != 0
    assert "requires" in stderr.lower() or "json" in stderr.lower()


def test_search_command_sort_option_runs_successfully(initialized_archive):
    for url in ["https://iana.org", "https://example.com"]:
        run_archivebox_cmd(["snapshot", "create", url], data_dir=initialized_archive)

    stdout, stderr, code = run_archivebox_cmd(["search", "--csv", "url", "--sort=url"], data_dir=initialized_archive)

    assert code == 0, stderr
    assert "example.com" in stdout or "iana.org" in stdout


def test_search_command_help_lists_supported_filters(initialized_archive):
    stdout, _stderr, code = run_archivebox_cmd(["search", "--help"], data_dir=initialized_archive)

    assert code == 0
    assert "--filter-type" in stdout or "-f" in stdout
    assert "--status" in stdout
    assert "--sort" in stdout
