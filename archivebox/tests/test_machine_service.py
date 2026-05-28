import os
from pathlib import Path

import pytest

from archivebox.machine.models import Binary, Machine, Process
from archivebox.tests.conftest import run_archivebox_cmd
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def _write_fake_wget(bin_dir: Path, version: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "wget"
    binary.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "${1:-}" = "--version" ]; then',
                f'  echo "GNU Wget {version}"',
                "else",
                '  echo "fake wget"',
                "fi",
                "",
            ],
        ),
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _runtime_env(data_dir: Path, fake_bin_dir: Path | None = None) -> dict[str, str]:
    env = {
        "LIB_DIR": str(data_dir / "lib"),
        "SAVE_WGET": "True",
        "WGET_ENABLED": "True",
    }
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}{os.pathsep}{os.environ['PATH']}"
    return env


def test_install_persists_machine_binary_config_and_recovers_stale_path(initialized_archive, tmp_path):
    fake_bin_dir = tmp_path / "fakebin"
    _write_fake_wget(fake_bin_dir, "9.8.7")

    stdout, stderr, returncode = run_archivebox_cmd(
        ["install", "--binproviders=env", "wget"],
        data_dir=initialized_archive,
        timeout=120,
        env=_runtime_env(initialized_archive, fake_bin_dir),
    )

    assert returncode == 0, stdout + stderr
    assert "wget" in stdout

    with use_archivebox_db(initialized_archive):
        machine = Machine.current()
        binary = Binary.objects.get(name="wget")
        process = Process.objects.filter(process_type=Process.TypeChoices.BINARY).latest("created_at")

    installed_path = Path(binary.abspath)
    assert machine.config == {"WGET_BINARY": str(installed_path)}
    assert "WGET_BINARY" not in (initialized_archive / "ArchiveBox.conf").read_text(encoding="utf-8")
    assert "ABX_INSTALL_CACHE" not in machine.config
    assert binary.status == Binary.StatusChoices.INSTALLED
    assert binary.version == "9.8.7"
    assert binary.binprovider == "env"
    assert installed_path.exists()
    assert installed_path.is_relative_to(initialized_archive / "lib")
    assert (initialized_archive / "lib" / "bin" / "wget").exists()
    assert process.status == Process.StatusChoices.EXITED
    assert process.exit_code == 0

    version_stdout, version_stderr, version_code = run_archivebox_cmd(
        ["version"],
        data_dir=initialized_archive,
        timeout=60,
        env=_runtime_env(initialized_archive),
    )
    assert version_code == 0, version_stderr
    assert "Wget" not in version_stderr
    assert "wget" in version_stdout
    assert "9.8.7" in version_stdout

    installed_path.unlink()
    (initialized_archive / "lib" / "bin" / "wget").unlink(missing_ok=True)

    cleanup_stdout, cleanup_stderr, cleanup_code = run_archivebox_cmd(
        ["version"],
        data_dir=initialized_archive,
        timeout=60,
        env=_runtime_env(initialized_archive),
    )
    assert cleanup_code == 0, cleanup_stdout + cleanup_stderr

    with use_archivebox_db(initialized_archive):
        Machine.current().refresh_from_db()
        cleaned_machine_config = Machine.current().config or {}

    assert cleaned_machine_config == {}

    _write_fake_wget(fake_bin_dir, "9.8.8")
    reinstall_stdout, reinstall_stderr, reinstall_code = run_archivebox_cmd(
        ["install", "--binproviders=env", "wget"],
        data_dir=initialized_archive,
        timeout=120,
        env=_runtime_env(initialized_archive, fake_bin_dir),
    )

    assert reinstall_code == 0, reinstall_stdout + reinstall_stderr
    with use_archivebox_db(initialized_archive):
        restored_machine = Machine.current()
        restored_binary = Binary.objects.get(name="wget")
        binary_exit_codes = list(
            Process.objects.filter(process_type=Process.TypeChoices.BINARY).order_by("created_at").values_list("exit_code", flat=True),
        )

    assert restored_binary.status == Binary.StatusChoices.INSTALLED
    assert restored_binary.version == "9.8.8"
    assert Path(restored_binary.abspath).exists()
    assert restored_machine.config == {"WGET_BINARY": restored_binary.abspath}
    assert binary_exit_codes[-2:] == [0, 0]
