#!/usr/bin/env python3
"""CLI-specific tests for archivebox schedule."""

import os
import subprocess

import pytest

from archivebox.crawls.models import Crawl
from archivebox.tests.orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def test_schedule_run_all_enqueues_scheduled_crawl(tmp_path, process, disable_extractors_dict):
    os.chdir(tmp_path)

    subprocess.run(
        ["archivebox", "schedule", "--every=daily", "--depth=0", "https://example.com"],
        capture_output=True,
        text=True,
        check=True,
    )

    result = subprocess.run(
        ["archivebox", "schedule", "--run-all"],
        capture_output=True,
        text=True,
        env=disable_extractors_dict,
    )

    assert result.returncode == 0
    assert "Enqueued 1 scheduled crawl" in result.stdout

    with use_archivebox_db(tmp_path):
        crawl_count = Crawl.objects.count()
        queued_count = Crawl.objects.filter(status="queued").count()

    assert crawl_count >= 2
    assert queued_count >= 1


def test_schedule_without_import_path_creates_maintenance_schedule(tmp_path, process):
    os.chdir(tmp_path)

    result = subprocess.run(
        ["archivebox", "schedule", "--every=day"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Created scheduled maintenance update" in result.stdout

    with use_archivebox_db(tmp_path):
        row = Crawl.objects.order_by("-created_at").values_list("urls", "status").first()

    assert row == ("archivebox://update", "sealed")
