import json
import os
import uuid
from pathlib import Path

import pytest

from archivebox.machine.models import Binary, Machine, Process
from archivebox.tests.conftest import parse_jsonl_output, run_archivebox_cmd
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def _write_fake_binary(bin_dir: Path, name: str, version: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / name
    binary.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "${1:-}" = "--version" ]; then',
                f'  echo "{name} {version}"',
                "else",
                f'  echo "{name} ran"',
                "fi",
                "",
            ],
        ),
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _binary_request(name: str, *, binproviders: str = "env") -> str:
    return json.dumps({"type": "BinaryRequest", "name": name, "binproviders": binproviders}) + "\n"


def _runtime_env(data_dir: Path, fake_bin_dir: Path | None = None) -> dict[str, str]:
    env = {"LIB_DIR": str(data_dir / "lib")}
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}{os.pathsep}{os.environ['PATH']}"
    return env


def test_binary_request_installs_env_binary_and_recovers_stale_cache(initialized_archive, tmp_path):
    name = f"abx-e2e-tool-{uuid.uuid4().hex[:8]}"
    fake_bin_dir = tmp_path / "fakebin"
    _write_fake_binary(fake_bin_dir, name, "2.4.6")

    stdout, stderr, returncode = run_archivebox_cmd(
        ["run"],
        data_dir=initialized_archive,
        stdin=_binary_request(name),
        timeout=120,
        env=_runtime_env(initialized_archive, fake_bin_dir),
    )

    assert returncode == 0, stderr
    output_records = parse_jsonl_output(stdout)
    assert any(record["type"] == "BinaryRequest" and record["name"] == name for record in output_records)

    with use_archivebox_db(initialized_archive):
        binary = Binary.objects.get(name=name)
        machine_id = str(binary.machine_id)
        first_binary_id = str(binary.id)
        first_abspath = Path(binary.abspath)
        binary_processes = list(Process.objects.filter(process_type=Process.TypeChoices.BINARY).order_by("created_at"))

    assert binary.status == Binary.StatusChoices.INSTALLED
    assert binary.version == "2.4.6"
    assert binary.binprovider == "env"
    assert binary.binproviders == "env"
    assert first_abspath.exists()
    assert first_abspath.is_relative_to(initialized_archive / "lib")
    assert (initialized_archive / "lib" / "env" / "bin" / name).exists()
    assert (initialized_archive / "machines" / machine_id / "binaries" / name / "index.jsonl").exists()
    assert binary_processes
    assert binary_processes[-1].status == Process.StatusChoices.EXITED
    assert binary_processes[-1].exit_code == 0
    assert any(f"--name={name}" in arg for arg in binary_processes[-1].cmd)

    version_stdout, version_stderr, version_code = run_archivebox_cmd(
        ["version"],
        data_dir=initialized_archive,
        timeout=60,
        env=_runtime_env(initialized_archive),
    )
    assert version_code == 0, version_stderr
    assert name in version_stdout
    assert "2.4.6" in version_stdout

    first_abspath.unlink()
    (initialized_archive / "lib" / "bin" / name).unlink(missing_ok=True)
    _write_fake_binary(fake_bin_dir, name, "2.4.7")

    rerun_stdout, rerun_stderr, rerun_code = run_archivebox_cmd(
        ["run", f"--binary-id={first_binary_id}"],
        data_dir=initialized_archive,
        timeout=120,
        env=_runtime_env(initialized_archive, fake_bin_dir),
    )

    assert rerun_code == 0, rerun_stdout + rerun_stderr
    with use_archivebox_db(initialized_archive):
        recovered = Binary.objects.get(pk=first_binary_id)
        process_count = Process.objects.filter(process_type=Process.TypeChoices.BINARY).count()

    assert recovered.status == Binary.StatusChoices.INSTALLED
    assert recovered.version == "2.4.7"
    assert Path(recovered.abspath).exists()
    assert process_count >= 2


def test_missing_binary_request_stays_queued_then_recovers_when_provider_can_resolve(initialized_archive, tmp_path):
    name = f"abx-missing-e2e-tool-{uuid.uuid4().hex[:8]}"

    stdout, stderr, returncode = run_archivebox_cmd(
        ["run"],
        data_dir=initialized_archive,
        stdin=_binary_request(name),
        timeout=120,
        env=_runtime_env(initialized_archive),
    )

    assert returncode == 0, stderr
    assert any(record["type"] == "BinaryRequest" and record["name"] == name for record in parse_jsonl_output(stdout))

    with use_archivebox_db(initialized_archive):
        queued = Binary.objects.get(name=name)
        queued_id = str(queued.id)
        failed_process = Process.objects.filter(process_type=Process.TypeChoices.BINARY).latest("created_at")
        machine_config = Machine.objects.get(pk=queued.machine_id).config or {}

    assert queued.status == Binary.StatusChoices.QUEUED
    assert queued.abspath == ""
    assert queued.retry_at is not None
    assert failed_process.status == Process.StatusChoices.EXITED
    assert failed_process.exit_code == 1
    assert f"{name.upper().replace('-', '_')}_BINARY" not in machine_config
    assert not (initialized_archive / "lib" / "env" / "bin" / name).exists()

    fake_bin_dir = tmp_path / "fakebin"
    _write_fake_binary(fake_bin_dir, name, "1.0.0")

    recover_stdout, recover_stderr, recover_code = run_archivebox_cmd(
        ["run", f"--binary-id={queued_id}"],
        data_dir=initialized_archive,
        timeout=120,
        env=_runtime_env(initialized_archive, fake_bin_dir),
    )

    assert recover_code == 0, recover_stdout + recover_stderr
    with use_archivebox_db(initialized_archive):
        recovered = Binary.objects.get(pk=queued_id)
        process_exit_codes = list(
            Process.objects.filter(process_type=Process.TypeChoices.BINARY).order_by("created_at").values_list("exit_code", flat=True),
        )

    assert recovered.status == Binary.StatusChoices.INSTALLED
    assert recovered.version == "1.0.0"
    assert Path(recovered.abspath).exists()
    assert process_exit_codes[-2:] == [1, 0]
