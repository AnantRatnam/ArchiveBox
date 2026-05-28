import os

import pytest
import requests

from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.test_orm_helpers import use_archivebox_db
from .test_server_helpers import (
    build_test_env,
    create_admin_and_token,
    get_free_port,
    init_archive,
    start_server,
    stop_server,
    wait_for_http,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.timeout(180)
def test_cli_api_add_search_update_remove_over_server(tmp_path, recursive_test_site):
    os.chdir(tmp_path)
    init_archive(tmp_path)

    port = get_free_port()
    env = build_test_env(port, PUBLIC_INDEX="True")
    api_token = create_admin_and_token(tmp_path)
    api_headers = {
        "Host": f"api.archivebox.localhost:{port}",
        "X-ArchiveBox-API-Key": api_token,
    }

    try:
        start_server(tmp_path, env=env, port=port)
        wait_for_http(port, host=f"api.archivebox.localhost:{port}", path="/api/v1/docs")

        add_response = requests.post(
            f"http://127.0.0.1:{port}/api/v1/cli/add",
            headers=api_headers,
            json={
                "urls": [recursive_test_site["root_url"]],
                "tag": "api-cli",
                "depth": 1,
                "parser": "url_list",
                "plugins": "wget",
                "update": True,
                "overwrite": False,
                "index_only": True,
            },
            timeout=10,
        )
        assert add_response.status_code == 200, add_response.text
        add_payload = add_response.json()
        assert add_payload["success"] is True
        assert add_payload["result_format"] == "json"
        assert add_payload["result"]["num_snapshots"] == 1
        crawl_id = add_payload["result"]["crawl_id"]
        snapshot_id = add_payload["result"]["snapshot_ids"][0]

        search_response = requests.post(
            f"http://127.0.0.1:{port}/api/v1/cli/search",
            headers=api_headers,
            json={
                "filter_patterns": [recursive_test_site["root_url"]],
                "filter_type": "exact",
                "status": "indexed",
                "sort": "bookmarked_at",
                "as_json": True,
                "as_html": False,
                "as_csv": "",
                "with_headers": False,
            },
            timeout=10,
        )
        assert search_response.status_code == 200, search_response.text
        search_payload = search_response.json()
        assert search_payload["success"] is True
        assert search_payload["result_format"] == "json"
        assert any(item["url"] == recursive_test_site["root_url"] for item in search_payload["result"])

        update_response = requests.post(
            f"http://127.0.0.1:{port}/api/v1/cli/update",
            headers=api_headers,
            json={
                "resume": None,
                "after": 0,
                "before": 4102444800,
                "filter_type": "exact",
                "filter_patterns": [recursive_test_site["root_url"]],
                "batch_size": 1,
                "continuous": False,
            },
            timeout=20,
        )
        assert update_response.status_code == 200, update_response.text
        assert update_response.json()["success"] is True

        with use_archivebox_db(tmp_path):
            crawl_obj = Crawl.objects.filter(pk=crawl_id).first()
            crawl = (crawl_obj.max_depth, crawl_obj.tags_str, crawl_obj.config) if crawl_obj else None

        assert crawl is not None
        assert crawl[0] == 1
        assert crawl[1] == "api-cli"
        assert crawl[2]["INDEX_ONLY"] is True

        remove_response = requests.post(
            f"http://127.0.0.1:{port}/api/v1/cli/remove",
            headers=api_headers,
            json={
                "delete": True,
                "after": 0,
                "before": 4102444800,
                "filter_type": "exact",
                "filter_patterns": [recursive_test_site["root_url"]],
            },
            timeout=20,
        )
        assert remove_response.status_code == 200, remove_response.text
        remove_payload = remove_response.json()
        assert remove_payload["success"] is True
        assert remove_payload["result"]["removed_count"] == 1
        assert snapshot_id in remove_payload["result"]["removed_snapshot_ids"]

        with use_archivebox_db(tmp_path):
            snapshot_count = Snapshot.objects.filter(pk=snapshot_id).count()

        assert snapshot_count == 0
    finally:
        stop_server(tmp_path)
