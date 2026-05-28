import os
import signal
import socket
import subprocess
import sys
import time


def test_real_fulltext_search_backends_survive_reindex_transition(tmp_path):
    data_dir = tmp_path / "archivebox_data"
    data_dir.mkdir()
    query = "documentation examples"

    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def archivebox(*args: str, env: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        merged_env.update(
            {
                "DATA_DIR": str(data_dir),
                "USE_COLOR": "False",
                "SHOW_PROGRESS": "False",
                "SAVE_WARC": "False",
                "WGET_WARC_ENABLED": "False",
                "WGET_TIMEOUT": "20",
                "USE_SEARCHING_BACKEND": "true",
                "USE_INDEXING_BACKEND": "true",
            },
        )
        if env:
            merged_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "archivebox", *args],
            cwd=data_dir,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    init_result = archivebox("init", "--quick", timeout=90)
    assert init_result.returncode == 0, init_result.stderr

    add_result = archivebox("add", "--depth=0", "--plugins=wget", "https://example.com", env={"SEARCH_BACKEND_ENGINE": "ripgrep"})
    assert add_result.returncode == 0, add_result.stderr

    rg_result = archivebox("list", "--search=contents", "--csv=url", query, env={"SEARCH_BACKEND_ENGINE": "ripgrep"})
    assert rg_result.returncode == 0, rg_result.stderr
    assert "https://example.com" in rg_result.stdout

    sqlite_update = archivebox("update", "--index-only", "--batch-size=10", env={"SEARCH_BACKEND_ENGINE": "sqlite"})
    assert sqlite_update.returncode == 0, sqlite_update.stderr
    sqlite_result = archivebox("list", "--search=contents", "--csv=url", query, env={"SEARCH_BACKEND_ENGINE": "sqlite"})
    assert sqlite_result.returncode == 0, sqlite_result.stderr
    assert "https://example.com" in sqlite_result.stdout

    http_port = free_port()
    sonic_port = free_port()
    sonic_env = os.environ.copy()
    sonic_env.update(
        {
            "DATA_DIR": str(data_dir),
            "USE_COLOR": "False",
            "SHOW_PROGRESS": "False",
            "SEARCH_BACKEND_ENGINE": "sonic",
            "USE_SEARCHING_BACKEND": "true",
            "USE_INDEXING_BACKEND": "true",
            "SEARCH_BACKEND_SONIC_PORT": str(sonic_port),
        },
    )
    server_log = data_dir / "server.log"
    with server_log.open("w", encoding="utf-8") as log_file:
        server = subprocess.Popen(
            [sys.executable, "-m", "archivebox", "server", f"127.0.0.1:{http_port}"],
            cwd=data_dir,
            env=sonic_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    try:
        for _ in range(80):
            if server.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", sonic_port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise AssertionError(f"Sonic did not start on port {sonic_port}:\n{server_log.read_text(encoding='utf-8', errors='replace')}")

        sonic_update = archivebox(
            "update",
            "--index-only",
            "--batch-size=10",
            env={
                "SEARCH_BACKEND_ENGINE": "sonic",
                "SEARCH_BACKEND_SONIC_PORT": str(sonic_port),
            },
        )
        assert sonic_update.returncode == 0, sonic_update.stderr
        sonic_result = archivebox(
            "list",
            "--search=contents",
            "--csv=url",
            query,
            env={
                "SEARCH_BACKEND_ENGINE": "sonic",
                "SEARCH_BACKEND_SONIC_PORT": str(sonic_port),
            },
        )
        assert sonic_result.returncode == 0, sonic_result.stderr
        assert "https://example.com" in sonic_result.stdout
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=10)

        for _ in range(20):
            leftovers = subprocess.run(
                ["pgrep", "-af", str(data_dir)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            remaining = [line for line in leftovers.stdout.splitlines() if "pgrep -af" not in line]
            if not remaining:
                break
            time.sleep(0.25)
        else:
            raise AssertionError(f"archivebox server left supervised worker processes running:\n{leftovers.stdout}")
