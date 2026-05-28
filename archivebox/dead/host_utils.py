# ruff: noqa
def get_archive_base_url(request=None, config: dict[str, Any] | None = None, **config_kwargs: Any) -> str:
    return get_web_base_url(request=request, config=config, **config_kwargs)


def build_api_url(path: str = "", request=None, config: dict[str, Any] | None = None, **config_kwargs: Any) -> str:
    return _build_url(get_api_base_url(request, config=config, **config_kwargs), path)


def build_archive_url(path: str = "", request=None, config: dict[str, Any] | None = None, **config_kwargs: Any) -> str:
    return _build_url(get_archive_base_url(request, config=config, **config_kwargs), path)
