# ruff: noqa
def short_ts(ts: Any) -> str | None:
    parsed = parse_date(ts)
    return None if parsed is None else str(parsed.timestamp()).split(".")[0]


def ts_to_iso(ts: Any) -> str | None:
    parsed = parse_date(ts)
    return None if parsed is None else parsed.isoformat()


def is_static_file(url: str):
    # TODO: the proper way is with MIME type detection + ext, not only extension
    return extension(url).lower() in CONSTANTS.STATICFILE_EXTENSIONS


@enforce_types
def str_between(string: str, start: str, end: str | None = None) -> str:
    """(<abc>12345</def>, <abc>, </def>)  ->  12345"""

    content = string.split(start, 1)[-1]
    if end is not None:
        content = content.rsplit(end, 1)[0]

    return content


@enforce_types
def get_headers(url: str, timeout: int | None = None, config=None, **config_kwargs) -> str:
    """Download the contents of a remote url and return the headers"""
    # TODO: get rid of this and use an abx pluggy hook instead

    from archivebox.config.common import get_config

    config = config or get_config(**config_kwargs)
    timeout = timeout or config.TIMEOUT

    try:
        response = requests.head(
            url,
            headers={"User-Agent": config.USER_AGENT},
            verify=config.CHECK_SSL_VALIDITY,
            timeout=timeout,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise RequestException
    except ReadTimeout:
        raise
    except RequestException:
        response = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT},
            verify=config.CHECK_SSL_VALIDITY,
            timeout=timeout,
            stream=True,
        )

    return pyjson.dumps(
        {
            "URL": url,
            "Status-Code": response.status_code,
            "Elapsed": response.elapsed.total_seconds() * 1000,
            "Encoding": str(response.encoding),
            "Apparent-Encoding": response.apparent_encoding,
            **dict(response.headers),
        },
        indent=4,
    )


def chrome_cleanup(config=None, **config_kwargs):
    """
    Cleans up any state or runtime files that Chrome leaves behind when killed by
    a timeout or other error. Handles:
    - All persona chrome_profile directories (via Persona.cleanup_chrome_all())
    - Explicit CHROME_USER_DATA_DIR from config
    - Legacy Docker chromium path
    """
    import os
    from pathlib import Path
    from archivebox.config.permissions import IN_DOCKER

    # Clean up all persona chrome directories using Persona class
    try:
        from archivebox.personas.models import Persona

        # Clean up all personas
        Persona.cleanup_chrome_all()

        # Also clean up the active persona's explicit CHROME_USER_DATA_DIR if set
        # (in case it's a custom path not under PERSONAS_DIR)
        from archivebox.config.common import get_config

        config = config or get_config(**config_kwargs)
        chrome_user_data_dir = config.get("CHROME_USER_DATA_DIR")
        if chrome_user_data_dir:
            singleton_lock = Path(chrome_user_data_dir) / "SingletonLock"
            if os.path.lexists(singleton_lock):
                try:
                    singleton_lock.unlink()
                except OSError:
                    pass
    except Exception:
        pass  # Persona/config not available during early startup

    # Legacy Docker cleanup (for backwards compatibility)
    if IN_DOCKER:
        singleton_lock = "/home/archivebox/.config/chromium/SingletonLock"
        if os.path.lexists(singleton_lock):
            try:
                os.remove(singleton_lock)
            except OSError:
                pass
