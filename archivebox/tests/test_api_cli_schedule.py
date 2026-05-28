import os
from io import StringIO

import pytest
import requests
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from archivebox.api.v1_cli import ScheduleCommandSchema, cli_schedule
from archivebox.crawls.models import CrawlSchedule
from .test_server_helpers import (
    build_test_env,
    create_admin_and_token,
    get_free_port,
    init_archive,
    start_server,
    stop_server,
    wait_for_http,
)

User = get_user_model()


class CLIScheduleAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="api-user",
            password="testpass123",
            email="api@example.com",
        )

    def test_schedule_api_creates_schedule(self):
        request = RequestFactory().post("/api/v1/cli/schedule")
        request.user = self.user
        setattr(request, "stdout", StringIO())
        setattr(request, "stderr", StringIO())
        args = ScheduleCommandSchema(
            every="daily",
            import_path="https://example.com/feed.xml",
            quiet=True,
        )

        response = cli_schedule(request, args)

        self.assertTrue(response["success"])
        self.assertEqual(response["result_format"], "json")
        self.assertEqual(CrawlSchedule.objects.count(), 1)
        self.assertEqual(len(response["result"]["created_schedule_ids"]), 1)


@pytest.mark.django_db(transaction=True)
@pytest.mark.timeout(180)
def test_api_v1_cli_schedule_creates_schedule_over_server(tmp_path, recursive_test_site):
    os.chdir(tmp_path)
    init_archive(tmp_path)

    port = get_free_port()
    env = build_test_env(port)
    api_token = create_admin_and_token(tmp_path)

    try:
        start_server(tmp_path, env=env, port=port)
        wait_for_http(port, host=f"api.archivebox.localhost:{port}", path="/api/v1/docs")

        response = requests.post(
            f"http://127.0.0.1:{port}/api/v1/cli/schedule",
            headers={
                "Host": f"api.archivebox.localhost:{port}",
                "X-ArchiveBox-API-Key": api_token,
            },
            json={
                "every": "daily",
                "import_path": recursive_test_site["root_url"],
                "quiet": True,
            },
            timeout=10,
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["success"] is True
        assert payload["result_format"] == "json"
        assert len(payload["result"]["created_schedule_ids"]) == 1
    finally:
        stop_server(tmp_path)
