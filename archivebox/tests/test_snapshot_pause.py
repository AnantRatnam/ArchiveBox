import json
import os
from pathlib import Path

import pytest
from django.utils import timezone

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.test_orm_helpers import use_archivebox_db
from archivebox.workers.models import RETRY_AT_MAX

from .test_server_helpers import create_admin_and_token, init_archive

pytestmark = pytest.mark.django_db(transaction=True)

API_HOST = "api.archivebox.localhost:8000"


def _api_headers(token: str) -> dict[str, str]:
    return {
        "HTTP_HOST": API_HOST,
        "HTTP_X_ARCHIVEBOX_API_KEY": token,
    }


def _json_response(response):
    return json.loads(response.content.decode())


def _post_json(client, path: str, token: str, payload: dict):
    return client.post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        **_api_headers(token),
    )


def _patch_json(client, path: str, token: str, payload: dict):
    return client.patch(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        **_api_headers(token),
    )


def _seed_archiveresult(
    snapshot: Snapshot,
    *,
    plugin: str,
    hook_name: str,
    status: str,
    output_text: str = "",
    output_path: str | None = None,
) -> ArchiveResult:
    output_files = {}
    output_size = 0
    output_mimetypes = ""
    if output_path is not None:
        output_bytes = output_text.encode()
        absolute_path = Path(snapshot.output_dir) / output_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(output_bytes)
        output_size = len(output_bytes)
        output_mimetypes = "text/plain"
        output_files[output_path] = {
            "extension": Path(output_path).suffix.lstrip("."),
            "mimetype": "text/plain",
            "size": output_size,
        }

    now = timezone.now()
    return ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin=plugin,
        hook_name=hook_name,
        status=status,
        output_str=output_path or output_text,
        output_files=output_files,
        output_size=output_size,
        output_mimetypes=output_mimetypes,
        start_ts=now if status != ArchiveResult.StatusChoices.QUEUED else None,
        end_ts=now if status in ArchiveResult.FINAL_STATES else None,
    )


def test_snapshot_pause_resume_api_cascades_active_archiveresults_and_preserves_finished_rows(
    tmp_path,
    client,
    recursive_test_site,
):
    os.chdir(tmp_path)
    init_archive(tmp_path)
    api_token = create_admin_and_token(tmp_path)

    with use_archivebox_db(tmp_path):
        create_response = _post_json(
            client,
            "/api/v1/core/snapshots",
            api_token,
            {
                "url": recursive_test_site["root_url"],
                "depth": 0,
                "title": "Snapshot pause target",
                "tags": ["snapshot-pause-e2e"],
                "status": "queued",
            },
        )
        assert create_response.status_code == 200, create_response.content.decode()
        snapshot_id = _json_response(create_response)["id"]
        snapshot = Snapshot.objects.get(id=snapshot_id)

        queued_result = _seed_archiveresult(
            snapshot,
            plugin="manualqueue",
            hook_name="on_Snapshot__manual_queue",
            status=ArchiveResult.StatusChoices.QUEUED,
        )
        started_result = _seed_archiveresult(
            snapshot,
            plugin="manualstart",
            hook_name="on_Snapshot__manual_start",
            status=ArchiveResult.StatusChoices.STARTED,
        )
        succeeded_result = _seed_archiveresult(
            snapshot,
            plugin="manualdone",
            hook_name="on_Snapshot__manual_done",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
            output_text="finished result should stay finished",
            output_path="manualdone/final.txt",
        )
        failed_result = _seed_archiveresult(
            snapshot,
            plugin="manualfail",
            hook_name="on_Snapshot__manual_fail",
            status=ArchiveResult.StatusChoices.FAILED,
            output_text="failed result should stay failed",
        )

        invalid_response = _patch_json(
            client,
            f"/api/v1/core/snapshot/{snapshot_id}",
            api_token,
            {"action": "hold"},
        )
        assert invalid_response.status_code == 400
        snapshot = Snapshot.objects.get(id=snapshot_id)
        assert snapshot.status == Snapshot.StatusChoices.QUEUED

        pause_response = _patch_json(
            client,
            f"/api/v1/core/snapshot/{snapshot_id}",
            api_token,
            {"action": "pause"},
        )
        assert pause_response.status_code == 200, pause_response.content.decode()
        assert _json_response(pause_response)["status"] == Snapshot.StatusChoices.PAUSED

        snapshot.refresh_from_db()
        crawl = Crawl.objects.get(id=snapshot.crawl_id)
        assert snapshot.status == Snapshot.StatusChoices.PAUSED
        assert snapshot.retry_at == RETRY_AT_MAX
        assert crawl.status == Crawl.StatusChoices.QUEUED

        active_rows = {
            row.plugin: (row.status, row.retry_at) for row in ArchiveResult.objects.filter(id__in=[queued_result.id, started_result.id])
        }
        assert active_rows == {
            "manualqueue": (ArchiveResult.StatusChoices.PAUSED, RETRY_AT_MAX),
            "manualstart": (ArchiveResult.StatusChoices.PAUSED, RETRY_AT_MAX),
        }

        finished_rows = {
            row.plugin: (row.status, row.retry_at, row.output_size)
            for row in ArchiveResult.objects.filter(id__in=[succeeded_result.id, failed_result.id])
        }
        assert finished_rows["manualdone"][0] == ArchiveResult.StatusChoices.SUCCEEDED
        assert finished_rows["manualdone"][1] is None
        assert finished_rows["manualdone"][2] == len("finished result should stay finished")
        assert finished_rows["manualfail"] == (ArchiveResult.StatusChoices.FAILED, None, 0)

        succeeded_row = ArchiveResult.objects.get(id=succeeded_result.id)
        output_path = Path(snapshot.output_dir) / next(iter(succeeded_row.output_files))
        assert output_path.read_text() == "finished result should stay finished"

        resume_response = _patch_json(
            client,
            f"/api/v1/core/snapshot/{snapshot_id}",
            api_token,
            {"action": "resume"},
        )
        assert resume_response.status_code == 200, resume_response.content.decode()
        assert _json_response(resume_response)["status"] == Snapshot.StatusChoices.QUEUED

        snapshot.refresh_from_db()
        crawl.refresh_from_db()
        assert snapshot.status == Snapshot.StatusChoices.QUEUED
        assert snapshot.retry_at is not None
        assert snapshot.retry_at != RETRY_AT_MAX
        assert crawl.status == Crawl.StatusChoices.QUEUED
        assert crawl.retry_at is not None
        assert crawl.retry_at != RETRY_AT_MAX

        resumed_rows = {
            row.plugin: (row.status, row.retry_at) for row in ArchiveResult.objects.filter(id__in=[queued_result.id, started_result.id])
        }
        assert resumed_rows["manualqueue"][0] == ArchiveResult.StatusChoices.QUEUED
        assert resumed_rows["manualqueue"][1] is not None
        assert resumed_rows["manualqueue"][1] != RETRY_AT_MAX
        assert resumed_rows["manualstart"][0] == ArchiveResult.StatusChoices.QUEUED
        assert resumed_rows["manualstart"][1] is not None
        assert resumed_rows["manualstart"][1] != RETRY_AT_MAX

        assert ArchiveResult.objects.get(id=succeeded_result.id).status == ArchiveResult.StatusChoices.SUCCEEDED
        assert ArchiveResult.objects.get(id=failed_result.id).status == ArchiveResult.StatusChoices.FAILED
        assert output_path.read_text() == "finished result should stay finished"
