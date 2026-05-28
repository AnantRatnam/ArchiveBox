"""
Search module for ArchiveBox.

Search indexing is handled by search backend hooks in plugins:
    abx_plugins/plugins/search_backend_*/on_Snapshot__*_index_*.py

This module provides the query interface that dynamically discovers
search backend plugins using the hooks system.

Search backends must provide a search.py module with:
    - search(query: str) -> List[str]  (returns snapshot IDs)
    - flush(snapshot_ids: Iterable[str]) -> None
"""

__package__ = "archivebox.search"

import os
from contextlib import contextmanager
from typing import Any

from django.db.models import Case, IntegerField, Q, QuerySet, Value, When

from archivebox.misc.util import enforce_types
from archivebox.misc.logging import stderr
from archivebox.config.common import get_config


# Cache discovered backends to avoid repeated filesystem scans
_search_backends_cache: dict | None = None
SEARCH_MODES = ("meta", "contents", "deep")
SEARCH_BACKEND_UI_NAMES = {
    "rg": "ripgrep",
    "sonic": "sonic",
    "fts": "sqlite",
}
MAX_SEARCH_RANK_IDS = 500


@contextmanager
def search_backend_env(config: dict[str, Any] | None = None, **config_kwargs: Any):
    """Expose ArchiveBox collection roots to in-process search backends."""
    config = config or get_config(**config_kwargs)
    updates = {}
    for key, value in config.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool, os.PathLike)):
            updates[str(key)] = str(value)
    updates["DATA_DIR"] = str(config.DATA_DIR)
    updates["SNAP_DIR"] = str(config.USERS_DIR)
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def normalize_search_backend_name(backend_name: str | None) -> str:
    return (backend_name or "").strip().lower().replace("-", "_")


def get_search_backend_display_name(backend_name: str) -> str:
    backend_name = normalize_search_backend_name(backend_name)
    return next((ui_name for ui_name, canonical_name in SEARCH_BACKEND_UI_NAMES.items() if canonical_name == backend_name), backend_name)


def get_default_search_mode(config: dict[str, Any] | None = None, **config_kwargs: Any) -> str:
    config = config or get_config(**config_kwargs)
    backend_name = normalize_search_backend_name(config.SEARCH_BACKEND_ENGINE)
    backends = get_available_backends()
    if backend_name in backends:
        return f"deep:{backend_name}"
    if "ripgrep" in backends:
        return "deep:ripgrep"
    return "contents"


def get_search_mode(search_mode: str | None, config: dict[str, Any] | None = None, **config_kwargs: Any) -> str:
    normalized = (search_mode or "").strip().lower().replace(" ", "")
    if normalized in SEARCH_MODES:
        return normalized
    if ":" in normalized:
        mode, backend_name = normalized.split(":", 1)
        backend_name = normalize_search_backend_name(backend_name)
        if mode == "deep" and backend_name in get_available_backends():
            return f"{mode}:{backend_name}"
    return get_default_search_mode(config=config, **config_kwargs)


def get_search_mode_base(search_mode: str | None, config: dict[str, Any] | None = None, **config_kwargs: Any) -> str:
    return get_search_mode(search_mode, config=config, **config_kwargs).split(":", 1)[0]


def get_search_mode_backend(search_mode: str | None, config: dict[str, Any] | None = None, **config_kwargs: Any) -> str | None:
    normalized = get_search_mode(search_mode, config=config, **config_kwargs)
    if ":" not in normalized:
        return None
    return normalized.split(":", 1)[1]


def get_search_mode_options(config: dict[str, Any] | None = None, **config_kwargs: Any) -> list[dict[str, str]]:
    config = config or get_config(**config_kwargs)
    backends = get_available_backends()
    configured_backend = normalize_search_backend_name(config.SEARCH_BACKEND_ENGINE)
    backend_names = [
        *([configured_backend] if configured_backend in backends else []),
        *(name for name in sorted(backends) if name != configured_backend),
    ]
    options = [
        {"value": "meta", "label": "meta"},
        {"value": "contents", "label": "contents"},
    ]
    if backend_names:
        options.extend(
            {
                "value": f"deep:{backend_name}",
                "label": f"deep: {get_search_backend_display_name(backend_name)}",
            }
            for backend_name in backend_names
        )
    else:
        options.append({"value": "deep", "label": "deep"})
    return options


def prioritize_metadata_matches(
    base_queryset: QuerySet,
    metadata_queryset: QuerySet,
    fulltext_queryset: QuerySet,
    *,
    deep_queryset: QuerySet | None = None,
    ordering: list[str] | tuple[str, ...] | None = None,
) -> QuerySet:
    metadata_ids = list(metadata_queryset.values_list("pk", flat=True).distinct()[: MAX_SEARCH_RANK_IDS + 1])
    metadata_id_set = set(metadata_ids)
    fulltext_ids = [
        pk for pk in fulltext_queryset.values_list("pk", flat=True).distinct()[: MAX_SEARCH_RANK_IDS + 1] if pk not in metadata_id_set
    ]
    fulltext_id_set = set(fulltext_ids)
    deep_ids = []
    if deep_queryset is not None:
        deep_ids = [
            pk
            for pk in deep_queryset.values_list("pk", flat=True).distinct()[: MAX_SEARCH_RANK_IDS + 1]
            if pk not in metadata_id_set and pk not in fulltext_id_set
        ]

    if not metadata_ids and not fulltext_ids and not deep_ids:
        return base_queryset.none()

    if any(len(ids) > MAX_SEARCH_RANK_IDS for ids in (metadata_ids, fulltext_ids, deep_ids)):
        search_filter = Q()
        if metadata_ids:
            search_filter |= Q(pk__in=metadata_queryset.values("pk").distinct())
        if fulltext_ids:
            search_filter |= Q(pk__in=fulltext_queryset.values("pk").distinct())
        if deep_queryset is not None and deep_ids:
            search_filter |= Q(pk__in=deep_queryset.values("pk").distinct())
        qs = base_queryset.filter(search_filter)
        if ordering is not None:
            qs = qs.order_by(*ordering)
        return qs.distinct()

    qs = base_queryset.filter(pk__in=[*metadata_ids, *fulltext_ids, *deep_ids]).annotate(
        search_rank=Case(
            When(pk__in=metadata_ids, then=Value(0)),
            When(pk__in=fulltext_ids, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        ),
    )

    if ordering is not None:
        qs = qs.order_by("search_rank", *ordering)

    return qs.distinct()


def get_available_backends() -> dict:
    """
    Discover all available search backend plugins.

    Uses the hooks system to find plugins with search.py modules.
    Results are cached after first call.
    """
    global _search_backends_cache

    if _search_backends_cache is None:
        from archivebox.hooks import get_search_backends

        _search_backends_cache = get_search_backends()

    return _search_backends_cache


def get_backend(config: dict[str, Any] | None = None, **config_kwargs: Any) -> Any:
    """
    Get the configured search backend module.

    Discovers available backends via the hooks system and returns
    the one matching SEARCH_BACKEND_ENGINE configuration.

    Falls back to 'ripgrep' if configured backend is not found.
    """
    config = config or get_config(**config_kwargs)
    backend_name = normalize_search_backend_name(config.SEARCH_BACKEND_ENGINE)
    backends = get_available_backends()

    if backend_name in backends:
        return backends[backend_name]

    # Fallback to ripgrep if available (no index needed)
    if "ripgrep" in backends:
        return backends["ripgrep"]

    # No backends found
    available = list(backends.keys())
    raise RuntimeError(
        f'Search backend "{backend_name}" not found. Available backends: {available or "none"}',
    )


@enforce_types
def query_search_index(
    query: str,
    search_mode: str | None = None,
    config: dict[str, Any] | None = None,
    max_results: int | None = None,
    **config_kwargs: Any,
) -> QuerySet:
    """
    Search for snapshots matching the query.

    Returns a QuerySet of Snapshot objects matching the search.
    """
    from archivebox.core.models import Snapshot

    config = config or get_config(**config_kwargs)
    if not config.USE_SEARCHING_BACKEND:
        return Snapshot.objects.none()

    search_mode = "contents" if search_mode is None else get_search_mode(search_mode, config=config)
    search_mode_base = get_search_mode_base(search_mode, config=config)
    if search_mode_base == "meta":
        return Snapshot.objects.none()

    snapshot_pks = list(iter_query_search_ids(query, search_mode=search_mode, config=config, max_results=max_results))
    return Snapshot.objects.filter(pk__in=list(dict.fromkeys(snapshot_pks)))


def iter_query_search_ids(
    query: str,
    search_mode: str | None = None,
    config: dict[str, Any] | None = None,
    max_results: int | None = None,
    **config_kwargs: Any,
):
    """Yield snapshot IDs from configured search backends as soon as each backend produces them."""
    config = config or get_config(**config_kwargs)
    if not config.USE_SEARCHING_BACKEND:
        return

    search_mode = "contents" if search_mode is None else get_search_mode(search_mode, config=config)
    search_mode_base = get_search_mode_base(search_mode, config=config)
    forced_backend = get_search_mode_backend(search_mode, config=config)
    if search_mode_base == "meta":
        return

    backends = get_available_backends()
    configured_backend = normalize_search_backend_name(config.SEARCH_BACKEND_ENGINE)
    if forced_backend:
        if forced_backend not in backends:
            raise RuntimeError(
                f'Search backend "{forced_backend}" not found. Available backends: {list(backends) or "none"}',
            )
        backend_names = [forced_backend]
    elif search_mode_base == "deep":
        backend_names = [
            *([configured_backend] if configured_backend in backends and configured_backend != "ripgrep" else []),
            *(name for name in backends if name not in {configured_backend, "ripgrep"}),
            *(["ripgrep"] if "ripgrep" in backends else []),
        ]
    elif configured_backend in backends:
        backend_names = [configured_backend]
    elif "ripgrep" in backends:
        backend_names = ["ripgrep"]
    else:
        get_backend()
        return

    if "sonic" in backend_names:
        from archivebox.services.supervision_service import ensure_daemon_stack

        ensure_daemon_stack(reason="search query")

    errors: list[Exception] = []
    successful_backends = 0
    seen: set[str] = set()
    try:
        for backend_name in backend_names:
            backend = backends[backend_name]
            try:
                with search_backend_env(config=config):
                    if hasattr(backend, "iter_search"):
                        ids = backend.iter_search(query, search_mode=search_mode_base)
                    elif backend_name == "ripgrep":
                        ids = backend.search(query, search_mode=search_mode_base)
                    else:
                        ids = backend.search(query)
                    for snapshot_id in ids:
                        if snapshot_id in seen:
                            continue
                        seen.add(snapshot_id)
                        yield snapshot_id
                        if max_results and len(seen) >= max_results:
                            return
                successful_backends += 1
            except Exception as err:
                errors.append(err)
                if search_mode_base != "deep" or forced_backend:
                    raise
    except Exception as err:
        stderr()
        stderr(
            f"[X] The search backend threw an exception={err}:",
            color="red",
        )
        raise
    else:
        if not successful_backends and errors and search_mode_base == "deep":
            raise errors[0]


@enforce_types
def flush_search_index(snapshots: QuerySet, config: dict[str, Any] | None = None, **config_kwargs: Any) -> None:
    """
    Remove snapshots from the search index.
    """
    config = config or get_config(**config_kwargs)
    if not config.USE_INDEXING_BACKEND or not snapshots:
        return

    backend = get_backend(config=config)
    snapshot_pks = [str(pk) for pk in snapshots.values_list("pk", flat=True)]

    try:
        with search_backend_env(config=config):
            backend.flush(snapshot_pks)
    except Exception as err:
        stderr()
        stderr(
            f"[X] The search backend threw an exception={err}:",
            color="red",
        )
