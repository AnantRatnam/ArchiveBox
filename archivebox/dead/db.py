# ruff: noqa
def list_migrations(out_dir: Path = DATA_DIR) -> list[tuple[bool, str]]:
    """List all Django migrations and their status"""
    from django.core.management import call_command

    def showmigrations() -> StringIO:
        out = StringIO()
        call_command("showmigrations", list=True, stdout=out)
        out.seek(0)
        return out

    out = retry_sqlite_locks(showmigrations, label="checking migrations")

    migrations = []
    for line in out.readlines():
        if line.strip() and "]" in line:
            status_str, name_str = line.strip().split("]", 1)
            is_applied = "X" in status_str
            migration_name = name_str.strip()
            migrations.append((is_applied, migration_name))

    return migrations


def get_admins(out_dir: Path = DATA_DIR) -> list[Any]:
    """Get list of superuser accounts"""
    from django.contrib.auth.models import User

    return list(User.objects.filter(is_superuser=True).exclude(username="system"))
