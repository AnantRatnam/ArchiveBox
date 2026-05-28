import os
import socket
import subprocess
import sys
import textwrap
import time
from datetime import timedelta
from pathlib import Path

import requests
from django.utils import timezone

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl, CrawlSchedule
from archivebox.tests.test_orm_helpers import use_archivebox_db
from .conftest import run_python_cwd


def init_archive(cwd: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "archivebox", "init", "--quick"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def build_test_env(port: int, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DATA_DIR", None)
    env.update(
        {
            "PLUGINS": "wget",
            "LISTEN_HOST": f"archivebox.localhost:{port}",
            "ALLOWED_HOSTS": "*",
            "CSRF_TRUSTED_ORIGINS": f"http://admin.archivebox.localhost:{port}",
            "PUBLIC_ADD_VIEW": "True",
            "USE_COLOR": "False",
            "SHOW_PROGRESS": "False",
            "TIMEOUT": "30",
            "URL_ALLOWLIST": r"127\.0\.0\.1[:/].*",
            "SAVE_ARCHIVEDOTORG": "False",
            "SAVE_TITLE": "False",
            "SAVE_FAVICON": "False",
            "SAVE_WARC": "False",
            "SAVE_PDF": "False",
            "SAVE_SCREENSHOT": "False",
            "SAVE_DOM": "False",
            "SAVE_SINGLEFILE": "False",
            "SAVE_READABILITY": "False",
            "SAVE_MERCURY": "False",
            "SAVE_GIT": "False",
            "SAVE_YTDLP": "False",
            "SAVE_HEADERS": "False",
            "SAVE_HTMLTOTEXT": "False",
            "SAVE_WGET": "True",
            "USE_CHROME": "False",
        },
    )
    env.update(extra)
    return env


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(cwd: Path, env: dict[str, str], port: int) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "archivebox", "server", "--daemonize", f"127.0.0.1:{port}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def stop_server(cwd: Path) -> None:
    script = textwrap.dedent(
        """
        import os
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.settings')
        import django
        django.setup()
        from archivebox.workers.supervisord_util import stop_existing_supervisord_process
        stop_existing_supervisord_process()
        print('stopped')
        """,
    )
    run_python_cwd(script, cwd=cwd, timeout=30)


def wait_for_http(port: int, host: str, path: str = "/", timeout: int = 30) -> requests.Response:
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            response = requests.get(
                f"http://127.0.0.1:{port}{path}",
                headers={"Host": host},
                timeout=2,
                allow_redirects=False,
            )
            if response.status_code < 500:
                return response
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for HTTP on {host}: {last_exc}")


def make_latest_schedule_due(cwd: Path) -> None:
    with use_archivebox_db(cwd):
        schedule = CrawlSchedule.objects.order_by("-created_at").select_related("template").first()
        assert schedule is not None
        Crawl.objects.filter(pk=schedule.template_id).update(
            created_at=timezone.now() - timedelta(days=2),
            modified_at=timezone.now() - timedelta(days=2),
        )


def get_snapshot_file_text(cwd: Path, url: str) -> str:
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.settings')
        import django
        django.setup()

        from archivebox.core.models import Snapshot

        snapshot = Snapshot.objects.filter(url={url!r}).order_by('-created_at').first()
        assert snapshot is not None, 'missing snapshot'
        assert snapshot.status == 'sealed', snapshot.status

        snapshot_dir = Path(snapshot.output_dir)
        candidates = []
        preferred_patterns = (
            'wget/**/index.html',
            'wget/**/*.html',
            'trafilatura/content.html',
            'trafilatura/content.txt',
            'defuddle/content.html',
            'defuddle/content.txt',
        )
        for pattern in preferred_patterns:
            for candidate in snapshot_dir.glob(pattern):
                if candidate.is_file():
                    candidates.append(candidate)

        if not candidates:
            for candidate in snapshot_dir.rglob('*'):
                if not candidate.is_file():
                    continue
                rel = candidate.relative_to(snapshot_dir)
                if rel.parts and rel.parts[0] == 'responses':
                    continue
                if len(rel.parts) == 1 and rel.name == 'index.html':
                    continue
                if candidate.suffix not in ('.html', '.htm', '.txt'):
                    continue
                if candidate.name in ('stdout.log', 'stderr.log'):
                    continue
                candidates.append(candidate)

        assert candidates, f'no captured html/txt files found in {{snapshot_dir}}'
        print(candidates[0].read_text(errors='ignore'))
        """,
    )
    stdout, stderr, code = run_python_cwd(script, cwd=cwd, timeout=60)
    assert code == 0, stderr
    return stdout


def wait_for_snapshot_capture(cwd: Path, url: str, timeout: int = 180) -> str:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return get_snapshot_file_text(cwd, url)
        except AssertionError as err:
            last_error = err
            time.sleep(2)
    raise AssertionError(f"timed out waiting for captured content for {url}: {last_error}")


def get_counts(cwd: Path, scheduled_url: str, one_shot_url: str) -> tuple[int, int, int]:
    with use_archivebox_db(cwd):
        scheduled_snapshots = Snapshot.objects.filter(url=scheduled_url).count()
        one_shot_snapshots = Snapshot.objects.filter(url=one_shot_url).count()
        scheduled_crawls = Crawl.objects.filter(schedule__isnull=False, urls=scheduled_url).count()
    return scheduled_snapshots, one_shot_snapshots, scheduled_crawls


def get_depth_counts(cwd: Path) -> dict[int, int]:
    with use_archivebox_db(cwd):
        return {depth: Snapshot.objects.filter(depth=depth).count() for depth in set(Snapshot.objects.values_list("depth", flat=True))}


def get_crawl_runtime_state(cwd: Path, crawl_id: str) -> dict[str, object]:
    from archivebox.workers.models import RETRY_AT_MAX

    with use_archivebox_db(cwd):
        crawl = Crawl.objects.get(id=crawl_id)
        snapshots = list(
            crawl.snapshot_set.order_by("created_at").values(
                "id",
                "url",
                "status",
                "retry_at",
            ),
        )
        results = list(
            ArchiveResult.objects.filter(snapshot__crawl=crawl)
            .order_by("snapshot_id", "plugin", "hook_name")
            .values(
                "snapshot_id",
                "plugin",
                "hook_name",
                "status",
                "retry_at",
                "output_files",
                "output_size",
            ),
        )

    return {
        "retry_at_max": RETRY_AT_MAX,
        "crawl_status": crawl.status,
        "crawl_retry_at": crawl.retry_at,
        "snapshots": snapshots,
        "results": results,
    }


def create_admin_and_token(cwd: Path) -> str:
    script = textwrap.dedent(
        """
        import os
        from datetime import timedelta
        from django.utils import timezone

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.settings')
        import django
        django.setup()

        from django.contrib.auth import get_user_model
        from archivebox.api.models import APIToken

        User = get_user_model()
        user, _ = User.objects.get_or_create(
            username='apitestadmin',
            defaults={
                'email': 'apitestadmin@example.com',
                'is_staff': True,
                'is_superuser': True,
            },
        )
        user.is_staff = True
        user.is_superuser = True
        user.set_password('testpass123')
        user.save()

        token = APIToken.objects.create(
            created_by=user,
            expires=timezone.now() + timedelta(days=1),
        )
        print(token.token)
        """,
    )
    stdout, stderr, code = run_python_cwd(script, cwd=cwd, timeout=60)
    assert code == 0, stderr
    return stdout.strip().splitlines()[-1]
