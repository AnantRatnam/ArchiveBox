import os

import pytest
import requests

from archivebox.core.models import Snapshot, Tag
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
def test_core_api_crud_uses_token_auth_and_persists_side_effects_over_server(tmp_path, recursive_test_site):
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
        docs = wait_for_http(port, host=f"api.archivebox.localhost:{port}", path="/api/v1/docs")
        assert docs.status_code == 200
        openapi = wait_for_http(port, host=f"api.archivebox.localhost:{port}", path="/api/v1/openapi.json")
        assert openapi.status_code == 200
        paths = openapi.json()["paths"]
        assert "/api/v1/core/snapshots" in paths
        assert "/api/v1/crawls/crawls" in paths

        unauth = requests.get(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawls",
            headers={"Host": f"api.archivebox.localhost:{port}"},
            timeout=10,
        )
        assert unauth.status_code in (401, 403)
        bad_auth = requests.get(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawls",
            headers={"Host": f"api.archivebox.localhost:{port}", "X-ArchiveBox-API-Key": "bad-token"},
            timeout=10,
        )
        assert bad_auth.status_code in (401, 403)

        crawl_response = requests.post(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawls",
            headers=api_headers,
            json={
                "urls": [recursive_test_site["root_url"]],
                "max_depth": 2,
                "max_urls": 7,
                "crawl_max_size": 0,
                "snapshot_max_size": 0,
                "tags": ["api-depth-two"],
                "label": "api crawl",
                "notes": "created through REST API",
                "config": {"PLUGINS": "wget,parse_html_urls", "URL_ALLOWLIST": r"127\.0\.0\.1[:/].*"},
            },
            timeout=10,
        )
        assert crawl_response.status_code == 200, crawl_response.text
        crawl_payload = crawl_response.json()
        crawl_id = crawl_payload["id"]
        assert crawl_payload["max_depth"] == 2
        assert crawl_payload["max_urls"] == 7
        assert crawl_payload["tags_str"] == "api-depth-two"
        assert crawl_payload["config"]["PLUGINS"] == "wget,parse_html_urls"

        snapshot_response = requests.post(
            f"http://127.0.0.1:{port}/api/v1/core/snapshots",
            headers=api_headers,
            json={
                "url": recursive_test_site["child_urls"][0],
                "crawl_id": crawl_id,
                "depth": 1,
                "title": "API child snapshot",
                "tags": ["api-child"],
                "status": "queued",
            },
            timeout=10,
        )
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot_payload = snapshot_response.json()
        snapshot_id = snapshot_payload["id"]
        assert snapshot_payload["url"] == recursive_test_site["child_urls"][0]
        assert snapshot_payload["tags"] == ["api-child"]

        patch_snapshot = requests.patch(
            f"http://127.0.0.1:{port}/api/v1/core/snapshot/{snapshot_id}",
            headers=api_headers,
            json={"status": "sealed", "tags": ["api-child", "api-patched"]},
            timeout=10,
        )
        assert patch_snapshot.status_code == 200, patch_snapshot.text
        assert patch_snapshot.json()["status"] == "sealed"
        assert set(patch_snapshot.json()["tags"]) == {"api-child", "api-patched"}

        tag_create = requests.post(
            f"http://127.0.0.1:{port}/api/v1/core/tags/create/",
            headers=api_headers,
            json={"name": "api-extra"},
            timeout=10,
        )
        assert tag_create.status_code == 200, tag_create.text
        tag_id = tag_create.json()["tag_id"]

        add_tag = requests.post(
            f"http://127.0.0.1:{port}/api/v1/core/tags/add-to-snapshot/",
            headers=api_headers,
            json={"snapshot_id": snapshot_id, "tag_id": tag_id},
            timeout=10,
        )
        assert add_tag.status_code == 200, add_tag.text
        remove_tag = requests.post(
            f"http://127.0.0.1:{port}/api/v1/core/tags/remove-from-snapshot/",
            headers=api_headers,
            json={"snapshot_id": snapshot_id, "tag_name": "api-extra"},
            timeout=10,
        )
        assert remove_tag.status_code == 200, remove_tag.text

        crawl_patch = requests.patch(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawl/{crawl_id}",
            headers=api_headers,
            json={"status": "sealed", "tags": ["api-sealed"]},
            timeout=10,
        )
        assert crawl_patch.status_code == 200, crawl_patch.text
        assert crawl_patch.json()["status"] == "sealed"
        assert crawl_patch.json()["tags_str"] == "api-sealed"

        snapshots_list = requests.get(
            f"http://127.0.0.1:{port}/api/v1/core/snapshots?tag=api-patched&with_archiveresults=true",
            headers=api_headers,
            timeout=10,
        )
        assert snapshots_list.status_code == 200, snapshots_list.text
        snapshot_items = snapshots_list.json()["items"]
        assert len(snapshot_items) == 1
        assert snapshot_items[0]["id"] == snapshot_id
        assert snapshot_items[0]["archiveresults"] == []

        bearer_response = requests.get(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawl/{crawl_id}",
            headers={"Host": f"api.archivebox.localhost:{port}", "Authorization": f"Bearer {api_token}"},
            timeout=10,
        )
        assert bearer_response.status_code == 200, bearer_response.text
        query_response = requests.get(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawl/{crawl_id}?api_key={api_token}",
            headers={"Host": f"api.archivebox.localhost:{port}"},
            timeout=10,
        )
        assert query_response.status_code == 200, query_response.text

        delete_snapshot = requests.delete(
            f"http://127.0.0.1:{port}/api/v1/core/snapshot/{snapshot_id}",
            headers=api_headers,
            timeout=10,
        )
        assert delete_snapshot.status_code == 200, delete_snapshot.text
        assert delete_snapshot.json()["success"] is True

        delete_crawl = requests.delete(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawl/{crawl_id}",
            headers=api_headers,
            timeout=10,
        )
        assert delete_crawl.status_code == 200, delete_crawl.text
        assert delete_crawl.json()["success"] is True

        with use_archivebox_db(tmp_path):
            assert Crawl.objects.filter(pk=crawl_id).count() == 0
            assert Snapshot.objects.filter(pk=snapshot_id).count() == 0
            assert Tag.objects.filter(name="api-extra").count() == 1
    finally:
        stop_server(tmp_path)
