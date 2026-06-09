import pytest
import json
from pathlib import Path

from .conftest import (
    api_client_request,
    cli_env,
    get_free_port,
    init_archive,
    start_archivebox_server,
    stop_server,
)
from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


IMPORT_FORMAT_EXPECTATIONS = {
    "rss": {
        "url": "https://example.com/",
        "title": "RSS Example Import",
        "date": "2024-01-01",
        "tags": {"rss-tag", "metadata"},
    },
    "netscape": {
        "url": "https://www.iana.org/domains/reserved",
        "title": "IANA Reserved Domains",
        "date": "2024-01-02",
        "tags": {"netscape-tag", "metadata"},
    },
    "dom": {
        "url": "https://www.iana.org/help/example-domains",
    },
    "json": {
        "url": "https://example.com/?archivebox-json-import=1",
        "title": "JSON Import Example",
        "date": "2024-01-03",
        "tags": {"json-tag", "metadata"},
    },
    "jsonl": {
        "url": "https://example.com/?archivebox-jsonl-import=1",
        "title": "JSONL Import Example",
        "date": "2024-01-04",
        "tags": {"jsonl-tag", "metadata"},
    },
    "txt": {
        "url": "https://example.org/",
    },
}


def write_import_format_files(base_dir: Path) -> dict[str, Path]:
    files = {
        "rss": base_dir / "test_rss.xml",
        "netscape": base_dir / "test_netscape.html",
        "dom": base_dir / "test_dom.html",
        "json": base_dir / "test_bookmarks.json",
        "jsonl": base_dir / "test_bookmarks.jsonl",
        "txt": base_dir / "test_urls.txt",
    }
    files["rss"].write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>ArchiveBox RSS import fixture</title>
    <link>https://example.com/</link>
    <description>ArchiveBox RSS import fixture</description>
    <item>
      <title>RSS Example Import</title>
      <link>https://example.com/</link>
      <guid>https://example.com/</guid>
      <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
      <category>rss-tag</category>
      <category>metadata</category>
    </item>
  </channel>
</rss>
""",
        encoding="utf-8",
    )
    files["netscape"].write_text(
        """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
  <DT><A HREF="https://www.iana.org/domains/reserved" ADD_DATE="1704153600" TAGS="netscape-tag,metadata">IANA Reserved Domains</A>
</DL><p>
""",
        encoding="utf-8",
    )
    files["dom"].write_text(
        """<!doctype html>
<html>
  <head><title>DOM import fixture</title></head>
  <body>
    <a href="https://www.iana.org/help/example-domains">IANA Example Domains</a>
  </body>
</html>
""",
        encoding="utf-8",
    )
    files["json"].write_text(
        json.dumps(
            {
                "url": "https://example.com/?archivebox-json-import=1",
                "title": "JSON Import Example",
                "tags": ["json-tag", "metadata"],
                "bookmarked_at": "2024-01-03T00:00:00+00:00",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    files["jsonl"].write_text(
        json.dumps(
            {
                "url": "https://example.com/?archivebox-jsonl-import=1",
                "title": "JSONL Import Example",
                "tags": "jsonl-tag,metadata",
                "bookmarked_at": "2024-01-04T00:00:00+00:00",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    files["txt"].write_text(
        "Plain text import fixture containing https://example.org/ as a real live URL.\n",
        encoding="utf-8",
    )
    return files


IMPORT_FORMAT_ENV = {
    "USE_COLOR": "False",
    "SHOW_PROGRESS": "False",
    "PLUGINS": "wget,headers",
    "SAVE_WGET": "True",
    "SAVE_HEADERS": "True",
    "USE_CHROME": "False",
    "URL_ALLOWLIST": r"example\.com|example\.org|iana\.org|www\.iana\.org",
}


def wait_for_import_processing(cwd: Path, expected_urls: set[str], *, timeout: float = 120.0) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with use_archivebox_db(cwd):
            crawl_started = Crawl.objects.filter(status__in=[Crawl.StatusChoices.STARTED, Crawl.StatusChoices.SEALED]).exists()
            snapshot_started = Snapshot.objects.filter(url__in=expected_urls).exists()
        if crawl_started or snapshot_started:
            return
        time.sleep(1)
    raise AssertionError("timed out waiting for import crawl processing to start")


def wait_for_expected_import_snapshots(cwd: Path, expected_urls: set[str], *, timeout: float = 180.0) -> None:
    import time

    allowed_statuses = {Snapshot.StatusChoices.QUEUED, Snapshot.StatusChoices.STARTED, Snapshot.StatusChoices.SEALED}
    deadline = time.time() + timeout
    while time.time() < deadline:
        with use_archivebox_db(cwd):
            rows = list(Snapshot.objects.filter(url__in=expected_urls).values_list("url", "status"))
        counts = {url: 0 for url in expected_urls}
        bad_statuses = []
        for url, status in rows:
            counts[url] += 1
            if status not in allowed_statuses:
                bad_statuses.append((url, status))
        if all(count == 1 for count in counts.values()) and not bad_statuses:
            return
        time.sleep(1)
    raise AssertionError(f"timed out waiting for one queued/started/sealed snapshot per URL, got counts={counts}, bad_statuses={bad_statuses}")


def malicious_add_inputs(tmp_path: Path, *, safe_url: str) -> tuple[list[str], Path]:
    other_crawl_source = tmp_path / "sources" / "other_crawl_source.txt"
    other_crawl_source.parent.mkdir(parents=True, exist_ok=True)
    other_crawl_source.write_text("https://example.com/not-owned-by-this-crawl\n", encoding="utf-8")
    canary = tmp_path / "archivebox_shell_injection_canary"
    return (
        [
            safe_url,
            "file:///etc/hosts",
            "/etc/hosts",
            "../../../../etc/passwd",
            f"file://{other_crawl_source}",
            str(other_crawl_source),
            f"'; touch {canary}; #",
            f'" && touch {canary} && echo "',
            f"$(touch {canary})",
            f"`touch {canary}`",
        ],
        canary,
    )


def assert_no_file_or_shell_payload_snapshots(cwd: Path, *, canary: Path) -> None:
    with use_archivebox_db(cwd):
        urls = list(Snapshot.objects.values_list("url", flat=True))
    assert not canary.exists()
    assert not [url for url in urls if str(url).startswith("file:")]
    for forbidden in ("/etc/hosts", "/etc/passwd", "other_crawl_source", "archivebox_shell_injection_canary"):
        assert not [url for url in urls if forbidden in str(url)]


def test_basic_success_case_request(client, tmp_path, api_headers):
    init_archive(tmp_path)

    response = api_client_request(
        client,
        "post",
        "/api/v1/cli/add",
        payload={
            "urls": ["https://example.com/api-cli-add-basic"],
            "depth": 0,
            "parser": "url_list",
            "plugins": "__archivebox_test_no_plugins__",
            "index_only": True,
        },
        headers=api_headers,
    )

    assert response.status_code == 200, response.content
    assert response.json()["success"] is True


@pytest.mark.timeout(360)
def test_api_cli_add_import_text_formats_preserve_metadata_and_crawl_inner_urls(client, tmp_path, api_headers):
    """REST API add should accept rich import text and queue real inner URLs with metadata preserved."""
    init_archive(tmp_path)
    import_files = write_import_format_files(tmp_path)
    expected_urls = {case["url"] for case in IMPORT_FORMAT_EXPECTATIONS.values()}
    port = get_free_port()
    env = cli_env(port=port, server=True, **IMPORT_FORMAT_ENV)

    for import_name, import_path in import_files.items():
        response = api_client_request(
            client,
            "post",
            "/api/v1/cli/add",
            payload={
                "urls": [import_path.read_text(encoding="utf-8")],
                "depth": 0,
                "tag": "api-import",
                "plugins": "wget,headers",
                "index_only": False,
            },
            headers=api_headers,
        )
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["success"] is True
        assert body["result"]["crawl_id"]

    try:
        start_archivebox_server(tmp_path, env=env, port=port)
        wait_for_import_processing(tmp_path, expected_urls)
        stop_server(tmp_path)
        start_archivebox_server(tmp_path, env=env, port=port)
        wait_for_expected_import_snapshots(tmp_path, expected_urls)
    finally:
        stop_server(tmp_path)

    for import_name, expected in IMPORT_FORMAT_EXPECTATIONS.items():
        with use_archivebox_db(tmp_path):
            snapshot = Snapshot.objects.filter(url=expected["url"]).order_by("-created_at").first()
            assert snapshot is not None, f"{import_name} did not create Snapshot for {expected['url']}"
            snapshot_id = str(snapshot.id)

        snapshot_response = api_client_request(
            client,
            "get",
            f"/api/v1/core/snapshot/{snapshot_id}",
            headers=api_headers,
        )
        assert snapshot_response.status_code == 200, snapshot_response.content
        assert snapshot_response.json()["url"] == expected["url"]

    with use_archivebox_db(tmp_path):
        crawls = list(Crawl.objects.order_by("created_at"))
        snapshots_by_url = {
            snapshot.url: snapshot
            for snapshot in Snapshot.objects.prefetch_related("tags").filter(url__in=expected_urls)
        }

    assert len(crawls) == len(import_files)
    assert all(crawl.tags_str == "api-import" for crawl in crawls)
    assert all(crawl.status in {Crawl.StatusChoices.STARTED, Crawl.StatusChoices.SEALED} for crawl in crawls)
    assert len(snapshots_by_url) == len(expected_urls)

    for import_name, expected in IMPORT_FORMAT_EXPECTATIONS.items():
        snapshot = snapshots_by_url.get(expected["url"])
        assert snapshot is not None, f"{import_name} did not create Snapshot for {expected['url']}"
        assert snapshot.status in {Snapshot.StatusChoices.QUEUED, Snapshot.StatusChoices.STARTED, Snapshot.StatusChoices.SEALED}
        if expected.get("title"):
            assert snapshot.title == expected["title"]
        if expected.get("date"):
            assert snapshot.bookmarked_at.date().isoformat() == expected["date"]
        if expected.get("tags"):
            assert expected["tags"] | {"api-import"} <= set(snapshot.tags.values_list("name", flat=True))


@pytest.mark.timeout(240)
def test_api_cli_add_rejects_file_path_and_shell_injection_payloads(client, tmp_path, api_headers):
    """REST add must not let path, file://, traversal, or shell strings become archiveable URLs."""
    init_archive(tmp_path)
    safe_url = "https://example.com/?archivebox-api-security=1"
    inputs, canary = malicious_add_inputs(tmp_path, safe_url=safe_url)
    port = get_free_port()
    env = cli_env(port=port, server=True, **IMPORT_FORMAT_ENV)

    response = api_client_request(
        client,
        "post",
        "/api/v1/cli/add",
        payload={
            "urls": inputs,
            "depth": 0,
            "tag": "api-security",
            "plugins": "wget,headers",
            "index_only": False,
        },
        headers=api_headers,
    )
    assert response.status_code == 200, response.content
    assert response.json()["success"] is True

    try:
        start_archivebox_server(tmp_path, env=env, port=port)
        wait_for_expected_import_snapshots(tmp_path, {safe_url}, timeout=120)
    finally:
        stop_server(tmp_path)

    assert_no_file_or_shell_payload_snapshots(tmp_path, canary=canary)
    with use_archivebox_db(tmp_path):
        snapshot = Snapshot.objects.get(url=safe_url)
        crawl = Crawl.objects.get()
    assert crawl.status in {Crawl.StatusChoices.STARTED, Crawl.StatusChoices.SEALED}
    assert snapshot.status in {Snapshot.StatusChoices.QUEUED, Snapshot.StatusChoices.STARTED, Snapshot.StatusChoices.SEALED}
    assert "api-security" in set(snapshot.tags.values_list("name", flat=True))
