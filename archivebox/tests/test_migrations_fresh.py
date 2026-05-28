#!/usr/bin/env python3
"""
Fresh install tests for ArchiveBox.

Tests that fresh installations work correctly with the current schema.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import pytest
from django.db.migrations.recorder import MigrationRecorder

from archivebox.core.models import ArchiveResult, Snapshot, Tag
from archivebox.crawls.models import Crawl
from archivebox.tests.test_orm_helpers import use_archivebox_db

from .migrations_helpers import run_archivebox

pytestmark = pytest.mark.django_db(transaction=True)


class TestFreshInstall(unittest.TestCase):
    """Test that fresh installs work correctly."""

    def test_init_creates_database(self):
        """Fresh init should create database and directories."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            # Verify database was created
            self.assertTrue((work_dir / "index.sqlite3").exists(), "Database not created")
            # Verify archive directory exists
            self.assertTrue((work_dir / "archive").is_dir(), "Archive dir not created")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_status_after_init(self):
        """Status command should work after init."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            result = run_archivebox(work_dir, ["status"])
            self.assertEqual(result.returncode, 0, f"Status failed: {result.stderr}")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_add_url_after_init(self):
        """Should be able to add URLs after init with --index-only."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            # Add a URL with --index-only for speed
            result = run_archivebox(work_dir, ["add", "--index-only", "https://example.com"])
            self.assertEqual(result.returncode, 0, f"Add command failed: {result.stderr}")

            with use_archivebox_db(work_dir):
                self.assertGreaterEqual(Crawl.objects.count(), 1, "No Crawl was created")
                self.assertGreaterEqual(Snapshot.objects.count(), 1, "No Snapshot was created")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_list_after_add(self):
        """List command should show added snapshots."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            result = run_archivebox(work_dir, ["add", "--index-only", "https://example.com"])
            self.assertEqual(result.returncode, 0, f"Add failed: {result.stderr}")

            result = run_archivebox(work_dir, ["list"])
            self.assertEqual(result.returncode, 0, f"List failed: {result.stderr}")

            # Verify the URL appears in output
            output = result.stdout + result.stderr
            self.assertIn("example.com", output, f"Added URL not in list output: {output[:500]}")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_migrations_table_populated(self):
        """Django migrations table should be populated after init."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            with use_archivebox_db(work_dir):
                count = MigrationRecorder.Migration.objects.count()

            # Should have many migrations applied
            self.assertGreater(count, 10, f"Expected >10 migrations, got {count}")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_core_migrations_applied(self):
        """Core app migrations should be applied."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            with use_archivebox_db(work_dir):
                migrations = list(
                    MigrationRecorder.Migration.objects.filter(app="core").order_by("name").values_list("name", flat=True),
                )

            self.assertIn("0001_initial", migrations)

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


class TestSchemaIntegrity(unittest.TestCase):
    """Test that the database schema is correct."""

    def test_snapshot_table_has_required_columns(self):
        """Snapshot table should have all required columns."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            columns = {field.column for field in Snapshot._meta.local_fields}

            required = {"id", "url", "timestamp", "title", "status", "created_at", "modified_at"}
            for col in required:
                self.assertIn(col, columns, f"Missing column: {col}")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_archiveresult_table_has_required_columns(self):
        """ArchiveResult table should have all required columns."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            columns = {field.column for field in ArchiveResult._meta.local_fields}

            required = {"id", "snapshot_id", "plugin", "status", "created_at", "modified_at"}
            for col in required:
                self.assertIn(col, columns, f"Missing column: {col}")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_tag_table_has_required_columns(self):
        """Tag table should have all required columns."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            columns = {field.column for field in Tag._meta.local_fields}

            required = {"id", "name"}
            for col in required:
                self.assertIn(col, columns, f"Missing column: {col}")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_crawl_table_has_required_columns(self):
        """Crawl table should have all required columns."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            columns = {field.column for field in Crawl._meta.local_fields}

            required = {"id", "urls", "status", "created_at", "created_by_id"}
            for col in required:
                self.assertIn(col, columns, f"Missing column: {col}")

            # seed_id should NOT exist (removed in 0.9.x)
            self.assertNotIn("seed_id", columns, "seed_id column should not exist in 0.9.x")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


class TestMultipleSnapshots(unittest.TestCase):
    """Test handling multiple snapshots."""

    def test_add_urls_separately(self):
        """Should be able to add multiple URLs one at a time."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            # Add URLs one at a time
            result = run_archivebox(work_dir, ["add", "--index-only", "https://example.com"])
            self.assertEqual(result.returncode, 0, f"Add 1 failed: {result.stderr}")

            result = run_archivebox(work_dir, ["add", "--index-only", "https://example.org"])
            self.assertEqual(result.returncode, 0, f"Add 2 failed: {result.stderr}")

            with use_archivebox_db(work_dir):
                snapshot_count = Snapshot.objects.count()
                crawl_count = Crawl.objects.count()
            self.assertEqual(snapshot_count, 2, f"Expected 2 snapshots, got {snapshot_count}")
            self.assertEqual(crawl_count, 2, f"Expected 2 Crawls, got {crawl_count}")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_snapshots_linked_to_crawls(self):
        """Each snapshot should be linked to a crawl."""
        work_dir = Path(tempfile.mkdtemp())

        try:
            result = run_archivebox(work_dir, ["init"])
            self.assertEqual(result.returncode, 0, f"Init failed: {result.stderr}")

            result = run_archivebox(work_dir, ["add", "--index-only", "https://example.com"])
            self.assertEqual(result.returncode, 0, f"Add failed: {result.stderr}")

            with use_archivebox_db(work_dir):
                row = Snapshot.objects.filter(url="https://example.com").values_list("crawl_id", flat=True).first()
            self.assertIsNotNone(row, "Snapshot not found")
            self.assertIsNotNone(row, "Snapshot should have a crawl_id")

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
