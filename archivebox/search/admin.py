__package__ = "archivebox.search"

import hashlib
import json

from django.contrib import admin
from django.contrib.admin.views.main import ChangeList
from django.core.cache import cache

from archivebox.search import (
    get_search_backend_display_name,
    get_default_search_mode,
    get_search_mode,
    get_search_mode_backend,
    get_search_mode_base,
    get_search_mode_options,
    query_search_index,
)


SEARCH_RESULT_CACHE_TTL = 60


def get_admin_search_cache_key(request, url: str | None = None) -> str:
    # Search streams publish IDs for one exact changelist URL. Keeping the URL
    # whole makes sidebar filters, ordering, and user scope part of the key.
    payload = json.dumps(
        {
            "user": str(request.user.pk or "anon"),
            "url": url or request.get_full_path(),
        },
        sort_keys=True,
    )
    return f"abx:admin-search:{hashlib.sha256(payload.encode()).hexdigest()}"


def get_cached_admin_search_ids(request) -> list[str] | None:
    cached = cache.get(get_admin_search_cache_key(request))
    if isinstance(cached, dict):
        return cached.get("ids") or []
    return None


class SearchResultsChangeList(ChangeList):
    def __init__(self, request, *args, **kwargs):
        self.search_mode = get_search_mode(request.GET.get("search_mode"), config=getattr(request, "archivebox_config", None))
        self.search_mode_backend = get_search_mode_backend(self.search_mode, config=getattr(request, "archivebox_config", None))
        self.search_backend_label = get_search_backend_display_name(self.search_mode_backend) if self.search_mode_backend else ""
        super().__init__(request, *args, **kwargs)
        self.embedded_changelist = request.GET.get("_embedded") == "crawl"

    def get_results(self, request):
        super().get_results(request)
        self.show_search_index_hint = bool(
            self.opts.model_name == "snapshot"
            and self.query
            and self.result_count == 0
            and get_search_mode_base(self.search_mode, config=getattr(request, "archivebox_config", None)) == "deep"
            and self.search_mode_backend
        )

    def get_filters_params(self, params=None):
        lookup_params = super().get_filters_params(params)
        lookup_params.pop("search_mode", None)
        lookup_params.pop("_embedded", None)
        lookup_params.pop("per_page", None)
        return lookup_params


class SearchResultsAdminMixin(admin.ModelAdmin):
    show_search_mode_selector = True

    def get_changelist(self, request, **kwargs):
        return SearchResultsChangeList

    def get_default_search_mode(self):
        request = getattr(self, "request", None)
        return get_default_search_mode(config=getattr(request, "archivebox_config", None))

    def get_search_mode_options(self):
        request = getattr(self, "request", None)
        return get_search_mode_options(config=getattr(request, "archivebox_config", None))

    def get_search_results(self, request, queryset, search_term: str):
        """Enhances the search queryset with results from the search backend"""

        search_term = search_term.strip()
        if not search_term:
            return super().get_search_results(request, queryset, search_term)
        search_mode = get_search_mode(request.GET.get("search_mode"), config=getattr(request, "archivebox_config", None))
        if queryset.model._meta.label_lower == "core.snapshot" and request.GET.get("_embedded") != "crawl":
            cached_ids = get_cached_admin_search_ids(request)
            if cached_ids is not None:
                return queryset.filter(pk__in=cached_ids) if cached_ids else queryset.none(), False
            return queryset.none(), False

        if get_search_mode_base(search_mode, config=getattr(request, "archivebox_config", None)) == "meta":
            qs, use_distinct = super().get_search_results(request, queryset, search_term)
            return qs, use_distinct
        if request.GET.get("_embedded") == "crawl":
            try:
                return queryset.filter(
                    pk__in=query_search_index(
                        search_term,
                        search_mode=search_mode,
                        config=getattr(request, "archivebox_config", None),
                    ).values("pk"),
                ), False
            except Exception as err:
                print(f"[!] Error while using search backend: {err.__class__.__name__} {err}")
                return queryset.none(), False
        return queryset.none(), False
