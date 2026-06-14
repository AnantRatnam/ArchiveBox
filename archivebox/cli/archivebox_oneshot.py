#!/usr/bin/env python3

__package__ = "archivebox.cli"
__command__ = "archivebox oneshot"

import subprocess
from pathlib import Path

import rich_click as click

from archivebox.config import CONSTANTS


@click.command(add_help_option=False, context_settings=dict(ignore_unknown_options=True))
@click.argument("args", nargs=-1)
def main(args: tuple[str, ...] = ()) -> None:
    """Download URLs using abx-dl"""
    cwd = Path.cwd()
    if any((path / CONSTANTS.SQL_INDEX_FILENAME).exists() for path in (cwd, *cwd.parents)):
        raise click.ClickException(
            "Refusing to run `archivebox oneshot` inside an ArchiveBox DATA_DIR. Use `archivebox add` here, or run oneshot from another directory.",
        )
    raise SystemExit(subprocess.run(["abx-dl", *args]).returncode)


if __name__ == "__main__":
    main()
