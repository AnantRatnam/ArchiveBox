from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.db import connections


def archivebox_db_path(path: str | Path = ".") -> Path:
    path = Path(path)
    return path if path.name == "index.sqlite3" else path / "index.sqlite3"


@contextmanager
def use_archivebox_db(path: str | Path = ".") -> Iterator[None]:
    connection = connections["default"]
    original_name = connection.settings_dict["NAME"]
    original_database_name = connections.databases["default"]["NAME"]
    original_setting_name = settings.DATABASES["default"]["NAME"]
    original_connection = getattr(connections._connections, "default", None)
    db_path = str(archivebox_db_path(path))

    connection.close()
    connection.settings_dict["NAME"] = db_path
    connections.databases["default"]["NAME"] = db_path
    settings.DATABASES["default"]["NAME"] = db_path
    if original_connection is not None:
        delattr(connections._connections, "default")
    try:
        yield
    finally:
        connections["default"].close()
        connections.databases["default"]["NAME"] = original_database_name
        settings.DATABASES["default"]["NAME"] = original_setting_name
        if hasattr(connections._connections, "default"):
            delattr(connections._connections, "default")
        if original_connection is not None:
            original_connection.settings_dict["NAME"] = original_name
            setattr(connections._connections, "default", original_connection)
