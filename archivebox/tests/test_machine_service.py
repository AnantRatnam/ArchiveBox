import os
from pathlib import Path

import pytest

from archivebox.machine.models import Binary, Machine, Process
from archivebox.tests.conftest import run_archivebox_cmd_cwd
from archivebox.tests.test_orm_helpers import use_archivebox_db

pytestmark = pytest.mark.django_db(transaction=True)


def _write_fake_tool(bin_dir: Path, name: str, version: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / name
    binary.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "${1:-}" = "--version" ]; then',
                f'  echo "{name} {version}"',
                "else",
                f'  echo "fake {name}"',
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
        "LITEPARSE_ENABLED": "True",
    }
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}{os.pathsep}{os.environ['PATH']}"
    return env


def test_install_persists_machine_binary_config_and_recovers_stale_path(initialized_archive, tmp_path):
    fake_bin_dir = tmp_path / "fakebin"
    _write_fake_tool(fake_bin_dir, "lit", "9.8.7")
    _write_fake_tool(fake_bin_dir, "node", "26.0.0")

    stdout, stderr, returncode = run_archivebox_cmd_cwd(
        ["install", "--binproviders=env", "liteparse"],
        cwd=initialized_archive,
        timeout=120,
        env=_runtime_env(initialized_archive, fake_bin_dir),
    )

    assert returncode == 0, stdout + stderr
    assert "liteparse" in stdout

    with use_archivebox_db(initialized_archive):
        machine = Machine.current()
        binaries = list(Binary.objects.filter(status=Binary.StatusChoices.INSTALLED).order_by("name"))
        process = Process.objects.filter(process_type=Process.TypeChoices.BINARY).latest("created_at")

    assert "LITEPARSE_BINARY" in machine.config
    assert all(key.endswith("_BINARY") for key in machine.config)
    assert "ABX_INSTALL_CACHE" not in machine.config
    config_file_text = (initialized_archive / "ArchiveBox.conf").read_text(encoding="utf-8")
    assert "LITEPARSE_BINARY" not in config_file_text
    assert "NODE_BINARY" not in config_file_text
    assert binaries
    for config_path in machine.config.values():
        installed_path = Path(config_path)
        assert installed_path.exists()
        assert installed_path.is_relative_to(initialized_archive / "lib")
        assert (initialized_archive / "lib" / "bin" / installed_path.name).exists()
    assert process.status == Process.StatusChoices.EXITED
    assert process.exit_code == 0

    version_stdout, version_stderr, version_code = run_archivebox_cmd_cwd(
        ["version"],
        cwd=initialized_archive,
        timeout=60,
        env=_runtime_env(initialized_archive),
    )
    assert version_code == 0, version_stderr
    assert "lit" in version_stdout

    stale_path = Path(machine.config["LITEPARSE_BINARY"])
    stale_path.unlink()
    (initialized_archive / "lib" / "bin" / stale_path.name).unlink(missing_ok=True)

    cleanup_stdout, cleanup_stderr, cleanup_code = run_archivebox_cmd_cwd(
        ["version"],
        cwd=initialized_archive,
        timeout=60,
        env=_runtime_env(initialized_archive),
    )
    assert cleanup_code == 0, cleanup_stdout + cleanup_stderr

    with use_archivebox_db(initialized_archive):
        Machine.current().refresh_from_db()
        cleaned_machine_config = Machine.current().config or {}

    assert "LITEPARSE_BINARY" not in cleaned_machine_config

    _write_fake_tool(fake_bin_dir, "lit", "9.8.8")
    reinstall_stdout, reinstall_stderr, reinstall_code = run_archivebox_cmd_cwd(
        ["install", "--binproviders=env", "liteparse"],
        cwd=initialized_archive,
        timeout=120,
        env=_runtime_env(initialized_archive, fake_bin_dir),
    )

    assert reinstall_code == 0, reinstall_stdout + reinstall_stderr
    with use_archivebox_db(initialized_archive):
        restored_machine = Machine.current()
        restored_liteparse = Binary.objects.get(name=Path(restored_machine.config["LITEPARSE_BINARY"]).name)
        binary_exit_codes = list(
            Process.objects.filter(process_type=Process.TypeChoices.BINARY).order_by("created_at").values_list("exit_code", flat=True),
        )

    assert restored_liteparse.status == Binary.StatusChoices.INSTALLED
    assert Path(restored_liteparse.abspath).exists()
    assert Path(restored_machine.config["LITEPARSE_BINARY"]).exists()
    assert binary_exit_codes[-2:] == [0, 0]
