#!/usr/bin/env python3
"""Integration tests for archivebox crawl command."""

import os
import subprocess

import pytest

from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def test_crawl_creates_crawl_object(tmp_path, process, disable_extractors_dict):
    """Test that crawl command creates a Crawl object."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "crawl", "--no-wait", "https://example.com"],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    with use_archivebox_db(tmp_path):
        crawl = Crawl.objects.order_by("-created_at").first()

    assert crawl is not None, "Crawl object should be created"


def test_crawl_depth_sets_max_depth_in_crawl(tmp_path, process, disable_extractors_dict):
    """Test that --depth option sets max_depth in the Crawl object."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "crawl", "--depth=2", "--no-wait", "https://example.com"],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    with use_archivebox_db(tmp_path):
        crawl = Crawl.objects.order_by("-created_at").first()

    assert crawl is not None
    assert crawl.max_depth == 2, "Crawl max_depth should match --depth=2"


def test_crawl_creates_snapshot_for_url(tmp_path, process, disable_extractors_dict):
    """Test that crawl creates a Snapshot for the input URL."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "crawl", "--no-wait", "https://example.com"],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    with use_archivebox_db(tmp_path):
        snapshot = Snapshot.objects.filter(url="https://example.com").first()

    assert snapshot is not None, "Snapshot should be created for input URL"


def test_crawl_links_snapshot_to_crawl(tmp_path, process, disable_extractors_dict):
    """Test that Snapshot is linked to Crawl via crawl_id."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "crawl", "--no-wait", "https://example.com"],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    with use_archivebox_db(tmp_path):
        crawl = Crawl.objects.order_by("-created_at").first()
        assert crawl is not None
        snapshot = Snapshot.objects.filter(url="https://example.com").first()

    assert snapshot is not None
    assert snapshot.crawl_id == crawl.id, "Snapshot should be linked to Crawl"


def test_crawl_multiple_urls_creates_multiple_snapshots(tmp_path, process, disable_extractors_dict):
    """Test that crawling multiple URLs creates multiple snapshots."""
    os.chdir(tmp_path)

    subprocess.run(
        [
            "archivebox",
            "crawl",
            "--no-wait",
            "https://example.com",
            "https://iana.org",
        ],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    with use_archivebox_db(tmp_path):
        urls = list(Snapshot.objects.order_by("url").values_list("url", flat=True))

    assert "https://example.com" in urls
    assert "https://iana.org" in urls


def test_crawl_from_file_creates_snapshot(tmp_path, process, disable_extractors_dict):
    """Test that crawl can create snapshots from a file of URLs."""
    os.chdir(tmp_path)

    # Write URLs to a file
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("https://example.com\n")

    subprocess.run(
        ["archivebox", "crawl", "--no-wait", str(urls_file)],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    with use_archivebox_db(tmp_path):
        snapshot = Snapshot.objects.first()

    # Should create at least one snapshot (the source file or the URL)
    assert snapshot is not None, "Should create at least one snapshot"


def test_crawl_persists_input_urls_on_crawl(tmp_path, process, disable_extractors_dict):
    """Test that crawl input URLs are stored on the Crawl record."""
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "crawl", "--no-wait", "https://example.com"],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    with use_archivebox_db(tmp_path):
        crawl = Crawl.objects.order_by("-created_at").first()

    assert crawl is not None, "Crawl should be created for crawl input"
    assert "https://example.com" in crawl.urls, "Crawl should persist input URLs"


class TestCrawlCLI:
    """Test the CLI interface for crawl command."""

    def test_cli_help(self, tmp_path, process):
        """Test that --help works for crawl command."""
        os.chdir(tmp_path)

        result = subprocess.run(
            ["archivebox", "crawl", "--help"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "create" in result.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
