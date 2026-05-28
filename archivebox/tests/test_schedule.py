#!/usr/bin/env python3
"""Integration tests for the database-backed archivebox schedule command."""

import os
import subprocess

import pytest

from archivebox.crawls.models import Crawl, CrawlSchedule
from archivebox.tests.orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def test_schedule_creates_enabled_db_schedule(tmp_path, process):
    os.chdir(tmp_path)

    result = subprocess.run(
        ["archivebox", "schedule", "--every=daily", "--depth=1", "https://example.com/feed.xml"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0

    with use_archivebox_db(tmp_path):
        schedule_row = CrawlSchedule.objects.order_by("-created_at").values_list("schedule", "is_enabled", "label").first()
        crawl = Crawl.objects.order_by("-created_at").first()

    assert schedule_row == ("daily", True, "Scheduled import: https://example.com/feed.xml")
    assert crawl is not None
    assert crawl.urls == "https://example.com/feed.xml"
    assert crawl.status == "sealed"
    assert crawl.max_depth == 1


def test_schedule_show_lists_enabled_schedules(tmp_path, process):
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "schedule", "--every=weekly", "https://example.com/feed.xml"],
        capture_output=True,
        text=True,
        check=True,
    )

    result = subprocess.run(
        ["archivebox", "schedule", "--show"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Active scheduled crawls" in result.stdout
    assert "https://example.com/feed.xml" in result.stdout
    assert "weekly" in result.stdout


def test_schedule_clear_disables_existing_schedules(tmp_path, process):
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "schedule", "--every=daily", "https://example.com/feed.xml"],
        capture_output=True,
        text=True,
        check=True,
    )

    result = subprocess.run(
        ["archivebox", "schedule", "--clear"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Disabled 1 scheduled crawl" in result.stdout

    with use_archivebox_db(tmp_path):
        disabled_count = CrawlSchedule.objects.filter(is_enabled=False).count()
        enabled_count = CrawlSchedule.objects.filter(is_enabled=True).count()

    assert disabled_count == 1
    assert enabled_count == 0


def test_schedule_every_requires_valid_period(tmp_path, process):
    os.chdir(tmp_path)

    result = subprocess.run(
        ["archivebox", "schedule", "--every=invalid_period", "https://example.com/feed.xml"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Invalid schedule" in result.stderr or "Invalid schedule" in result.stdout


class TestScheduleCLI:
    def test_cli_help(self, tmp_path, process):
        os.chdir(tmp_path)

        result = subprocess.run(
            ["archivebox", "schedule", "--help"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "--every" in result.stdout
        assert "--show" in result.stdout
        assert "--clear" in result.stdout
        assert "--run-all" in result.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
