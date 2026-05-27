#!/usr/bin/env python3
"""Real user-facing archive flows against live URLs."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from .conftest import _find_system_browser


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"PID {pid} is still alive")


def _cleanup_process_group(group_pid: int | None, *child_pids: int | None) -> None:
    if group_pid and _pid_is_alive(group_pid):
        try:
            os.killpg(group_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                os.kill(group_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for pid in child_pids:
        if pid and _pid_is_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.timeout(90)
def test_cli_run_signal_cleans_background_hook_process_group(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    plugins_root = tmp_path / "runtime_plugins"
    plugin_dir = plugins_root / "cancel_group"
    plugin_dir.mkdir(parents=True)
    daemon_hook = plugin_dir / "on_CrawlSetup__10_daemon.daemon.bg.sh"
    foreground_hook = plugin_dir / "on_CrawlSetup__20_foreground.sh"
    daemon_hook.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'test_dir="${LEAK_TEST_DIR:?}"',
                "sleep 600 &",
                'echo $$ > "$test_dir/daemon.pid"',
                'echo $! > "$test_dir/daemon-child.pid"',
                'echo ready > "$test_dir/daemon.ready"',
                "trap 'echo cleaned > \"$test_dir/daemon.cleaned\"; exit 0' TERM INT",
                "wait",
                "",
            ],
        ),
    )
    foreground_hook.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'test_dir="${LEAK_TEST_DIR:?}"',
                'echo $$ > "$test_dir/foreground.pid"',
                'echo ready > "$test_dir/foreground.ready"',
                "trap 'echo cleaned > \"$test_dir/foreground.cleaned\"; exit 0' TERM INT",
                "while true; do sleep 1; done",
                "",
            ],
        ),
    )
    daemon_hook.chmod(0o755)
    foreground_hook.chmod(0o755)

    leak_test_dir = tmp_path / "leak-check"
    leak_test_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "ABX_PLUGINS_DIR": str(plugins_root),
            "LEAK_TEST_DIR": str(leak_test_dir),
            "PLUGINS": "cancel_group",
            "TIMEOUT": "30",
            "USE_COLOR": "false",
            "SHOW_PROGRESS": "false",
        },
    )

    create_result = subprocess.run(
        [sys.executable, "-m", "archivebox", "crawl", "create", "https://example.com"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert create_result.returncode == 0, create_result.stderr or create_result.stdout
    crawl_records = [json.loads(line) for line in create_result.stdout.splitlines() if line.strip().startswith("{")]
    crawl_id = next(record["id"] for record in crawl_records if record.get("type") == "Crawl")

    daemon_pid: int | None = None
    daemon_child_pid: int | None = None
    foreground_pid: int | None = None
    run_process = subprocess.Popen(
        [sys.executable, "-m", "archivebox", "run", f"--crawl-id={crawl_id}"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if (leak_test_dir / "daemon.ready").exists() and (leak_test_dir / "foreground.ready").exists():
                break
            if run_process.poll() is not None:
                output = run_process.communicate(timeout=1)[0]
                raise AssertionError(f"archivebox run exited before hooks were ready:\n{output}")
            time.sleep(0.05)
        assert (leak_test_dir / "daemon.ready").exists()
        assert (leak_test_dir / "foreground.ready").exists()

        daemon_pid = int((leak_test_dir / "daemon.pid").read_text().strip())
        daemon_child_pid = int((leak_test_dir / "daemon-child.pid").read_text().strip())
        foreground_pid = int((leak_test_dir / "foreground.pid").read_text().strip())
        assert _pid_is_alive(daemon_pid)
        assert _pid_is_alive(daemon_child_pid)
        assert _pid_is_alive(foreground_pid)

        run_process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        if run_process.poll() is None:
            run_process.send_signal(signal.SIGTERM)
        output = run_process.communicate(timeout=20)[0]
        assert "Runner error" not in output

        _wait_for_pid_exit(daemon_pid)
        _wait_for_pid_exit(daemon_child_pid)
        _wait_for_pid_exit(foreground_pid)
        assert (leak_test_dir / "daemon.cleaned").read_text().strip() == "cleaned"
        assert (leak_test_dir / "foreground.cleaned").read_text().strip() == "cleaned"
    finally:
        if run_process.poll() is None:
            try:
                os.killpg(run_process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            run_process.communicate(timeout=5)
        _cleanup_process_group(daemon_pid, daemon_child_pid)
        _cleanup_process_group(foreground_pid)


@pytest.mark.timeout(180)
def test_cli_add_real_urls_with_options_writes_inspectable_outputs(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    wget_urls = [
        "https://example.com",
        "https://pirate.github.io/stress-tests/challenge.html",
    ]
    chrome_url = "https://example.com/?archivebox-chrome-flow=1"
    env = os.environ.copy()
    env.pop("CHROME_BINARY", None)
    env.update(
        {
            "USE_COLOR": "false",
            "SHOW_PROGRESS": "false",
            "TIMEOUT": "60",
            "SAVE_WGET": "true",
            "SAVE_HEADERS": "false",
            "SAVE_TITLE": "false",
            "SAVE_READABILITY": "false",
            "SAVE_SINGLEFILE": "false",
            "SAVE_MERCURY": "false",
            "SAVE_SCREENSHOT": "false",
            "SAVE_PDF": "false",
            "SAVE_DOM": "false",
            "SAVE_ARCHIVEDOTORG": "false",
            "SAVE_GIT": "false",
            "SAVE_YTDLP": "false",
            "SAVE_FAVICON": "false",
        },
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "archivebox",
            "add",
            "--depth=0",
            "--max-urls=2",
            "--crawl-max-size=10mb",
            "--tag=real-flow,challenge",
            "--parser=url_list",
            "--plugins=wget",
            *wget_urls,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    chrome_env = env | {
        "SAVE_WGET": "false",
        "SAVE_HEADERS": "true",
        "SAVE_TITLE": "true",
        "CHROME_HEADLESS": "true",
        "CHROME_SANDBOX": "false",
        "CHROME_ISOLATION": "snapshot",
    }
    system_browser = _find_system_browser()
    if system_browser:
        chrome_env["CHROME_BINARY"] = str(system_browser)
    else:
        install_result = subprocess.run(
            [sys.executable, "-m", "archivebox", "install", "chrome"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=chrome_env,
            timeout=600,
        )
        assert install_result.returncode == 0, install_result.stderr or install_result.stdout
    chrome_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "archivebox",
            "add",
            "--depth=0",
            "--max-urls=1",
            "--crawl-max-size=10mb",
            "--tag=chrome-flow",
            "--parser=url_list",
            "--plugins=chrome,wget,headers,title",
            chrome_url,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=chrome_env,
        timeout=180,
    )
    assert chrome_result.returncode == 0, chrome_result.stderr or chrome_result.stdout

    list_result = subprocess.run(
        [sys.executable, "-m", "archivebox", "list", "--tag=real-flow"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert list_result.returncode == 0, list_result.stderr or list_result.stdout
    listed = [json.loads(line) for line in list_result.stdout.splitlines() if line.strip()]
    assert {item["url"] for item in listed} >= set(wget_urls)

    conn = sqlite3.connect(tmp_path / "index.sqlite3")
    try:
        crawl = conn.execute(
            "SELECT max_depth, max_urls, crawl_max_size, snapshot_max_size, tags_str, config FROM crawls_crawl ORDER BY created_at DESC LIMIT 1",
        ).fetchone()
        real_flow_crawl = conn.execute(
            "SELECT max_depth, max_urls, crawl_max_size, snapshot_max_size, tags_str, config FROM crawls_crawl WHERE tags_str = 'real-flow,challenge'",
        ).fetchone()
        snapshots = conn.execute(
            "SELECT id, url, depth, status, title FROM core_snapshot ORDER BY url",
        ).fetchall()
        archive_results = conn.execute(
            "SELECT s.url, ar.plugin, ar.status, ar.output_files, ar.output_size, ar.output_str "
            "FROM core_archiveresult ar "
            "JOIN core_snapshot s ON s.id = ar.snapshot_id "
            "ORDER BY s.url, ar.plugin",
        ).fetchall()
        processes = conn.execute(
            "SELECT process_type, status, exit_code, pwd, cmd FROM machine_process WHERE process_type = 'hook'",
        ).fetchall()
    finally:
        conn.close()

    assert real_flow_crawl is not None
    assert real_flow_crawl[0] == 0
    assert real_flow_crawl[1] == 2
    assert real_flow_crawl[2] == 10 * 1024 * 1024
    assert real_flow_crawl[3] == 0
    assert real_flow_crawl[4] == "real-flow,challenge"
    assert "wget" in real_flow_crawl[5]
    assert crawl is not None
    assert crawl[4] == "chrome-flow"
    assert "wget,headers,title" in crawl[5]

    snapshot_urls = {url for _id, url, _depth, _status, _title in snapshots}
    assert snapshot_urls >= {*wget_urls, chrome_url}
    assert all(depth == 0 for _id, _url, depth, _status, _title in snapshots)

    by_url_plugin = {(url, plugin): status for url, plugin, status, _files, _size, _output in archive_results}
    assert by_url_plugin[("https://example.com", "wget")] == "succeeded"
    assert by_url_plugin[("https://pirate.github.io/stress-tests/challenge.html", "wget")] == "succeeded"
    assert (chrome_url, "headers") in by_url_plugin
    assert (chrome_url, "title") in by_url_plugin
    failed_results = [(url, plugin, output) for url, plugin, status, _files, _size, output in archive_results if status == "failed"]
    assert len(failed_results) <= 2, failed_results

    snapshot_root = tmp_path / "archive/users/system/snapshots"
    html_outputs = [path for path in snapshot_root.rglob("wget/**/*.html") if path.is_file()]
    header_outputs = [path for path in snapshot_root.rglob("headers/**/headers.json") if path.is_file() and path.stat().st_size > 0]
    title_outputs = [path for path in snapshot_root.rglob("title/title.txt") if path.is_file() and path.stat().st_size > 0]
    index_outputs = [path for path in snapshot_root.rglob("index.jsonl") if path.is_file()]
    assert html_outputs
    if by_url_plugin[(chrome_url, "headers")] == "succeeded":
        assert header_outputs
    if by_url_plugin[(chrome_url, "title")] == "succeeded":
        assert title_outputs
        assert any("Example Domain" in path.read_text(errors="ignore") for path in title_outputs)
    assert len(index_outputs) >= len(wget_urls) + 1

    combined_html = "\n".join(path.read_text(errors="ignore") for path in html_outputs)
    assert "Example Domain" in combined_html
    assert "Browser Agent Challenge for AI Browser Drivers" in combined_html

    assert processes
    assert any("wget" in (pwd or "") or "wget" in (cmd or "") for _type, _status, _exit, pwd, cmd in processes)
    assert any("headers" in (pwd or "") or "headers" in (cmd or "") for _type, _status, _exit, pwd, cmd in processes)


@pytest.mark.timeout(180)
def test_cli_recursive_crawl_processes_discovered_html_urls(tmp_path, process):
    os.chdir(tmp_path)
    assert process.returncode == 0, process.stderr

    env = os.environ.copy()
    env.update(
        {
            "USE_COLOR": "false",
            "SHOW_PROGRESS": "false",
            "TIMEOUT": "60",
            "SAVE_WGET": "true",
            "SAVE_HEADERS": "false",
            "SAVE_TITLE": "false",
            "SAVE_READABILITY": "false",
            "SAVE_SINGLEFILE": "false",
            "SAVE_MERCURY": "false",
            "SAVE_SCREENSHOT": "false",
            "SAVE_PDF": "false",
            "SAVE_DOM": "false",
            "SAVE_ARCHIVEDOTORG": "false",
            "SAVE_GIT": "false",
            "SAVE_YTDLP": "false",
            "SAVE_FAVICON": "false",
            "PARSE_HTML_URLS_ENABLED": "true",
            "PARSE_DOM_OUTLINKS_ENABLED": "false",
        },
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "archivebox",
            "add",
            "--depth=2",
            "--max-urls=2",
            "--crawl-max-size=50mb",
            "--tag=recursive-flow",
            "--parser=url_list",
            "--plugins=wget,parse_html_urls",
            "https://example.com",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    conn = sqlite3.connect(tmp_path / "index.sqlite3")
    try:
        crawl = conn.execute(
            "SELECT max_depth, max_urls, crawl_max_size, snapshot_max_size, tags_str FROM crawls_crawl ORDER BY created_at DESC LIMIT 1",
        ).fetchone()
        snapshots = conn.execute(
            "SELECT url, depth, status FROM core_snapshot ORDER BY depth, url",
        ).fetchall()
        archive_results = conn.execute(
            "SELECT s.url, ar.plugin, ar.status, ar.output_files "
            "FROM core_archiveresult ar "
            "JOIN core_snapshot s ON s.id = ar.snapshot_id "
            "ORDER BY s.depth, s.url, ar.plugin",
        ).fetchall()
    finally:
        conn.close()

    assert crawl == (2, 2, 50 * 1024 * 1024, 0, "recursive-flow")
    assert ("https://example.com", 0, "sealed") in snapshots
    assert any(url == "https://iana.org/domains/example" and depth == 1 and status == "sealed" for url, depth, status in snapshots)

    by_url_plugin = {(url, plugin): status for url, plugin, status, _files in archive_results}
    assert by_url_plugin[("https://example.com", "wget")] == "succeeded"
    assert by_url_plugin[("https://example.com", "parse_html_urls")] == "succeeded"
    assert by_url_plugin[("https://iana.org/domains/example", "wget")] == "succeeded"

    urls_outputs = list((tmp_path / "archive/users/system/snapshots").rglob("parse_html_urls/urls.jsonl"))
    assert urls_outputs
    assert any("https://iana.org/domains/example" in path.read_text() for path in urls_outputs)
