__package__ = "archivebox.crawls"

from copy import copy
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpRequest, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import path, reverse
from django.utils.html import escape, format_html, format_html_join
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.contrib import admin, messages
from django.db.models import Case, CharField, Count, IntegerField, OuterRef, Prefetch, Q, Subquery, Value, When
from django.db.models.functions import Coalesce


from django_object_actions import action

from archivebox.base_models.admin import BaseModelAdmin, ConfigEditorMixin

from archivebox.config.common import get_config
from archivebox.core.models import Snapshot
from archivebox.core.permissions import PERMISSIONS_PRIVATE, PERMISSIONS_PUBLIC, PERMISSIONS_UNLISTED
from archivebox.core.widgets import TagEditorWidget, URLFiltersWidget
from archivebox.crawls.models import Crawl, CrawlSchedule
from archivebox.personas.models import Persona
from archivebox.workers.models import RETRY_AT_MAX


class MaxDepthListFilter(admin.SimpleListFilter):
    title = "max depth"
    parameter_name = "max_depth"

    def lookups(self, request, model_admin):
        return [(str(depth), str(depth)) for depth in range(5)]

    def queryset(self, request, queryset):
        value = self.value()
        if value is not None and value.isdigit():
            return queryset.filter(max_depth=int(value))
        return queryset


def render_snapshots_list(snapshots_qs, request=None, crawl=None, page_size=50, prefix="snapshots"):
    """Render a nice inline list view of snapshots with status, title, URL, and progress."""

    query_param = f"{prefix}_q"
    status_param = f"{prefix}_status"
    page_param = f"{prefix}_page"
    query = (request.GET.get(query_param, "") if request is not None else "").strip()
    status_filter = (request.GET.get(status_param, "") if request is not None else "").strip()
    valid_statuses = {choice[0] for choice in Snapshot.StatusChoices.choices}

    filtered_qs = snapshots_qs
    if query:
        id_query = query.replace("-", "")
        filtered_qs = filtered_qs.filter(Q(id__icontains=id_query) | Q(url__icontains=query) | Q(title__icontains=query))
    if status_filter in valid_statuses:
        filtered_qs = filtered_qs.filter(status=status_filter)

    global_permissions = str(get_config(resolve_plugins=False).PERMISSIONS).strip().lower()
    persona_ids_by_permissions = {
        PERMISSIONS_PUBLIC: [],
        PERMISSIONS_UNLISTED: [],
        PERMISSIONS_PRIVATE: [],
    }
    for persona in Persona.objects.only("id", "permissions"):
        persona_ids_by_permissions[persona.permissions or global_permissions].append(str(persona.id))

    snapshots_qs = filtered_qs.order_by("-created_at").annotate(
        total_results=Count("archiveresult"),
        succeeded_results=Count("archiveresult", filter=Q(archiveresult__status="succeeded")),
        failed_results=Count("archiveresult", filter=Q(archiveresult__status="failed")),
        started_results=Count("archiveresult", filter=Q(archiveresult__status="started")),
        skipped_results=Count("archiveresult", filter=Q(archiveresult__status="skipped")),
        snapshot_permissions=Case(
            When(permissions=PERMISSIONS_PUBLIC, then=Value(PERMISSIONS_PUBLIC)),
            When(permissions=PERMISSIONS_UNLISTED, then=Value(PERMISSIONS_UNLISTED)),
            When(permissions=PERMISSIONS_PRIVATE, then=Value(PERMISSIONS_PRIVATE)),
            When(crawl__permissions=PERMISSIONS_PUBLIC, then=Value(PERMISSIONS_PUBLIC)),
            When(crawl__permissions=PERMISSIONS_UNLISTED, then=Value(PERMISSIONS_UNLISTED)),
            When(crawl__permissions=PERMISSIONS_PRIVATE, then=Value(PERMISSIONS_PRIVATE)),
            When(crawl__persona_id__in=persona_ids_by_permissions[PERMISSIONS_PUBLIC], then=Value(PERMISSIONS_PUBLIC)),
            When(crawl__persona_id__in=persona_ids_by_permissions[PERMISSIONS_UNLISTED], then=Value(PERMISSIONS_UNLISTED)),
            When(crawl__persona_id__in=persona_ids_by_permissions[PERMISSIONS_PRIVATE], then=Value(PERMISSIONS_PRIVATE)),
            default=Value(global_permissions),
            output_field=CharField(),
        ),
    )

    page_number = request.GET.get(page_param, 1) if request is not None else 1
    paginator = Paginator(snapshots_qs, page_size)
    page_obj = paginator.get_page(page_number)
    snapshots = page_obj.object_list
    total_count = paginator.count

    def querystring(**updates):
        if request is None:
            return "#"
        params = request.GET.copy()
        for key, value in updates.items():
            if value in (None, ""):
                params.pop(key, None)
            else:
                params[key] = str(value)
        return f"?{params.urlencode()}" if params else "?"

    preserved_inputs = ""
    if request is not None:
        managed_params = {query_param, status_param, page_param}
        preserved_inputs = "".join(
            f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
            for key, values in request.GET.lists()
            if key not in managed_params
            for value in values
        )

    status_options = "".join(
        f'<option value="{escape(value)}"{" selected" if status_filter == value else ""}>{escape(label)}</option>'
        for value, label in Snapshot.StatusChoices.choices
    )

    controls = f"""
        <div class="crawl-snapshots-toolbar" style="display: flex; gap: 10px; align-items: center; justify-content: space-between; flex-wrap: wrap; padding: 10px 12px; background: #f8fafc; border-bottom: 1px solid #e2e8f0;">
            <form method="get" style="display: flex; gap: 8px; align-items: center; flex: 1 1 540px; margin: 0;">
                {preserved_inputs}
                <input type="search" name="{query_param}" value="{escape(query)}" placeholder="Filter snapshots by title, URL, or ID"
                       style="min-width: 260px; flex: 1 1 360px; padding: 7px 10px; border: 1px solid #cbd5e1; border-radius: 6px;">
                <select name="{status_param}" style="max-width: 170px; padding: 7px 10px; border: 1px solid #cbd5e1; border-radius: 6px;">
                    <option value="">All statuses</option>
                    {status_options}
                </select>
                <input type="hidden" name="{page_param}" value="1">
                <button type="submit" class="button" style="padding: 7px 12px;">Filter</button>
                {f'<a href="{querystring(**{query_param: None, status_param: None, page_param: None})}" style="font-size: 12px; color: #64748b;">Clear</a>' if query or status_filter else ""}
            </form>
            <div style="font-size: 12px; color: #64748b; white-space: nowrap;">
                {page_obj.start_index() if total_count else 0}-{page_obj.end_index() if total_count else 0} of {total_count}
            </div>
        </div>
    """

    if not snapshots:
        return mark_safe(f"""
            <div data-crawl-snapshots-list style="border: 1px solid #ddd; border-radius: 6px; overflow: hidden; max-width: 100%;">
                {controls}
                <div style="color: #666; font-style: italic; padding: 12px;">No Snapshots found.</div>
            </div>
        """)

    # Status colors matching Django admin and progress monitor
    status_colors = {
        "queued": ("#6c757d", "#f8f9fa"),  # gray
        "started": ("#856404", "#fff3cd"),  # amber
        "paused": ("#1d4ed8", "#dbeafe"),  # blue
        "sealed": ("#155724", "#d4edda"),  # green
        "failed": ("#721c24", "#f8d7da"),  # red
    }

    rows = []
    for snapshot in snapshots:
        status = snapshot.status or "queued"
        color, bg = status_colors.get(status, ("#6c757d", "#f8f9fa"))
        permissions = snapshot.snapshot_permissions
        permission_icon = {
            PERMISSIONS_PUBLIC: "👁",
            PERMISSIONS_UNLISTED: "🔗",
            PERMISSIONS_PRIVATE: "🔒",
        }[permissions]
        permission_fg, permission_bg = {
            PERMISSIONS_PUBLIC: ("#047857", "#d1fae5"),
            PERMISSIONS_UNLISTED: ("#1d4ed8", "#dbeafe"),
            PERMISSIONS_PRIVATE: ("#991b1b", "#fee2e2"),
        }[permissions]

        # Calculate progress
        total = snapshot.total_results
        succeeded = snapshot.succeeded_results
        failed = snapshot.failed_results
        running = snapshot.started_results
        skipped = snapshot.skipped_results
        done = succeeded + failed + skipped
        pending = max(total - done - running, 0)
        progress_pct = int((done / total) * 100) if total > 0 else 0
        progress_text = f"{done}/{total}" if total > 0 else "-"
        progress_title = f"{succeeded} succeeded, {failed} failed, {running} running, {pending} pending, {skipped} skipped"
        progress_color = "#28a745"
        if failed:
            progress_color = "#dc3545"
        elif running:
            progress_color = "#17a2b8"
        elif pending:
            progress_color = "#ffc107"

        # Truncate title and URL
        snapshot_title = snapshot.title or "Untitled"
        title = snapshot_title[:60]
        if len(snapshot_title) > 60:
            title += "..."
        url_display = snapshot.url[:50]
        if len(snapshot.url) > 50:
            url_display += "..."
        delete_button = ""
        exclude_button = ""
        if crawl is not None:
            delete_url = reverse("admin:crawls_crawl_snapshot_delete", args=[crawl.pk, snapshot.pk])
            exclude_url = reverse("admin:crawls_crawl_snapshot_exclude_domain", args=[crawl.pk, snapshot.pk])
            delete_button = f'''
                <button type="button"
                        class="crawl-snapshots-action"
                        data-post-url="{escape(delete_url)}"
                        data-confirm="Delete this snapshot from the crawl?"
                        title="Delete this snapshot from the crawl and remove its URL from the crawl queue."
                        aria-label="Delete snapshot"
                        style="border: 1px solid #ddd; background: #fff; color: #666; border-radius: 4px; width: 28px; height: 28px; cursor: pointer;">🗑</button>
            '''
            exclude_button = f'''
                <button type="button"
                        class="crawl-snapshots-action"
                        data-post-url="{escape(exclude_url)}"
                        data-confirm="Exclude this domain from the crawl? This removes matching queued URLs, deletes pending matching snapshots, and blocks future matches."
                        title="Exclude this domain from this crawl. This removes matching URLs from the crawl queue, deletes pending matching snapshots, and blocks future matches."
                        aria-label="Exclude domain from crawl"
                        style="border: 1px solid #ddd; background: #fff; color: #666; border-radius: 4px; width: 28px; height: 28px; cursor: pointer;">⊘</button>
            '''

        # Format date
        date_str = snapshot.created_at.strftime("%Y-%m-%d %H:%M") if snapshot.created_at else "-"

        rows.append(f'''
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 6px 8px; white-space: nowrap;">
                    <span style="display: inline-block; padding: 2px 8px; border-radius: 10px;
                                 font-size: 11px; font-weight: 500; text-transform: uppercase;
                                 color: {color}; background: {bg};">{status}</span>
                </td>
                <td style="padding: 6px 8px; white-space: nowrap; text-align: center;">
                    <span title="{permissions}" style="display:inline-flex; align-items:center; justify-content:center; width:22px; height:22px; border-radius:999px; font-size:12px; color:{permission_fg}; background:{permission_bg};">{permission_icon}</span>
                </td>
                <td style="padding: 6px 8px; white-space: nowrap;">
                    <a href="/{snapshot.archive_path}/" style="text-decoration: none;">
                        <img src="/{snapshot.archive_path}/favicon.ico"
                             style="width: 16px; height: 16px; vertical-align: middle; margin-right: 4px;"
                             onerror="this.style.display='none'"/>
                    </a>
                </td>
                <td style="padding: 6px 8px; max-width: 300px;">
                    <a href="{snapshot.admin_change_url}" style="color: #417690; text-decoration: none; font-weight: 500;"
                       title="{escape(snapshot_title)}">{escape(title)}</a>
                </td>
                <td style="padding: 6px 8px; max-width: 250px;">
                    <a href="{escape(snapshot.url)}" target="_blank"
                       style="color: #666; text-decoration: none; font-family: monospace; font-size: 11px;"
                       title="{escape(snapshot.url)}">{escape(url_display)}</a>
                </td>
                <td style="padding: 6px 8px; white-space: nowrap; text-align: center;">
                    <div style="display: inline-flex; align-items: center; gap: 6px;" title="{escape(progress_title)}">
                        <div style="width: 60px; height: 6px; background: #eee; border-radius: 3px; overflow: hidden;">
                            <div style="width: {progress_pct}%; height: 100%;
                                        background: {progress_color};
                                        transition: width 0.3s;"></div>
                        </div>
                        <a href="/admin/core/archiveresult/?snapshot__id__exact={snapshot.id}"
                           style="font-size: 11px; color: #417690; min-width: 35px; text-decoration: none;"
                           title="View archive results">{progress_text}</a>
                    </div>
                </td>
                <td style="padding: 6px 8px; white-space: nowrap; color: #888; font-size: 11px;">
                    {date_str}
                </td>
                {f'<td style="padding: 6px 8px; white-space: nowrap; text-align: right;"><div style="display: inline-flex; gap: 6px;">{exclude_button}{delete_button}</div></td>' if crawl is not None else ""}
            </tr>
        ''')

    pagination = ""
    if paginator.num_pages > 1:
        pagination = f"""
            <div style="display: flex; gap: 10px; align-items: center; justify-content: center; padding: 10px 12px; background: #f8fafc; border-top: 1px solid #e2e8f0; font-size: 12px;">
                {"<a class='button' style='padding: 5px 10px;' href='" + querystring(**{page_param: page_obj.previous_page_number()}) + "'>Previous</a>" if page_obj.has_previous() else "<span style='color:#94a3b8;'>Previous</span>"}
                <span style="color: #64748b;">Page {page_obj.number} of {paginator.num_pages}</span>
                {"<a class='button' style='padding: 5px 10px;' href='" + querystring(**{page_param: page_obj.next_page_number()}) + "'>Next</a>" if page_obj.has_next() else "<span style='color:#94a3b8;'>Next</span>"}
            </div>
        """

    return mark_safe(f"""
        <div data-crawl-snapshots-list style="border: 1px solid #ddd; border-radius: 6px; overflow: hidden; max-width: 100%;">
            {controls}
            <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                <thead>
                    <tr style="background: #f5f5f5; border-bottom: 2px solid #ddd;">
                        <th style="padding: 8px; text-align: left; font-weight: 600; color: #333;">Status</th>
                        <th style="padding: 8px 4px; text-align: center; font-weight: 600; color: #333; width: 22px;">🔒</th>
                        <th style="padding: 8px; text-align: left; font-weight: 600; color: #333; width: 24px;"></th>
                        <th style="padding: 8px; text-align: left; font-weight: 600; color: #333;">Title</th>
                        <th style="padding: 8px; text-align: left; font-weight: 600; color: #333;">URL</th>
                        <th style="padding: 8px; text-align: center; font-weight: 600; color: #333;">Progress</th>
                        <th style="padding: 8px; text-align: left; font-weight: 600; color: #333;">Created</th>
                        {
        '<th style="padding: 8px; text-align: right; font-weight: 600; color: #333;">Actions</th>' if crawl is not None else ""
    }
                    </tr>
                </thead>
                <tbody>
                    {"".join(rows)}
                </tbody>
            </table>
            {pagination}
        </div>
        {
        '''
        <script>
        (function() {
            if (window.__archiveboxCrawlSnapshotActionsBound) {
                return;
            }
            window.__archiveboxCrawlSnapshotActionsBound = true;

            function getCookie(name) {
                var cookieValue = null;
                if (!document.cookie) {
                    return cookieValue;
                }
                var cookies = document.cookie.split(';');
                for (var i = 0; i < cookies.length; i++) {
                    var cookie = cookies[i].trim();
                    if (cookie.substring(0, name.length + 1) === (name + '=')) {
                        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                        break;
                    }
                }
                return cookieValue;
            }

            document.addEventListener('click', function(event) {
                var button = event.target.closest('.crawl-snapshots-action');
                if (!button) {
                    return;
                }
                event.preventDefault();

                var confirmMessage = button.getAttribute('data-confirm');
                if (confirmMessage && !window.confirm(confirmMessage)) {
                    return;
                }

                button.disabled = true;

                fetch(button.getAttribute('data-post-url'), {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'X-CSRFToken': getCookie('csrftoken') || '',
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                }).then(function(response) {
                    return response.json().then(function(data) {
                        if (!response.ok) {
                            throw new Error(data.error || 'Request failed');
                        }
                        return data;
                    });
                }).then(function() {
                    window.location.reload();
                }).catch(function(error) {
                    button.disabled = false;
                    window.alert(error.message || 'Request failed');
                });
            });
        })();
        </script>
        '''
        if crawl is not None
        else ""
    }
    """)


class URLFiltersField(forms.Field):
    widget = URLFiltersWidget(source_selector="#id_urls")

    def to_python(self, value):
        if isinstance(value, dict):
            return value
        return {"allowlist": "", "denylist": "", "same_domain_only": False, "subpaths_only": False}


class CrawlAdminForm(forms.ModelForm):
    """Custom form for Crawl admin to render urls field as textarea."""

    tags_editor = forms.CharField(
        label="Tags",
        required=False,
        widget=TagEditorWidget(),
        help_text="Type tag names and press Enter or Space to add. Click × to remove.",
    )
    url_filters = URLFiltersField(
        label="URL Filters",
        required=False,
        help_text="Set URL_ALLOWLIST / URL_DENYLIST for this crawl.",
    )

    class Meta:
        model = Crawl
        fields = "__all__"
        widgets = {
            "urls": forms.Textarea(
                attrs={
                    "rows": 8,
                    "style": "width: 100%; font-family: monospace; font-size: 13px;",
                    "placeholder": "https://example.com\nhttps://example2.com\n# Comments start with #",
                },
            ),
            "notes": forms.Textarea(
                attrs={
                    "rows": 1,
                    "style": "width: 100%; min-height: 0; resize: vertical;",
                },
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        config = dict(self.instance.config or {}) if self.instance and self.instance.pk else {}
        if self.instance and self.instance.pk:
            self.initial["tags_editor"] = self.instance.tags_str
        self.initial["url_filters"] = {
            "allowlist": config.get("URL_ALLOWLIST", ""),
            "denylist": config.get("URL_DENYLIST", ""),
            "same_domain_only": False,
            "subpaths_only": False,
        }

    def clean_tags_editor(self):
        tags_str = self.cleaned_data.get("tags_editor", "")
        tag_names = []
        seen = set()
        for raw_name in tags_str.split(","):
            name = raw_name.strip()
            if not name:
                continue
            lowered = name.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            tag_names.append(name)
        return ",".join(tag_names)

    def clean_url_filters(self):
        value = self.cleaned_data.get("url_filters") or {}
        return {
            "allowlist": "\n".join(Crawl.split_filter_patterns(value.get("allowlist", ""))),
            "denylist": "\n".join(Crawl.split_filter_patterns(value.get("denylist", ""))),
            "same_domain_only": bool(value.get("same_domain_only")),
            "subpaths_only": bool(value.get("subpaths_only")),
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.tags_str = self.cleaned_data.get("tags_editor", "")
        if f"{self.add_prefix('url_filters')}_allowlist" in self.data or f"{self.add_prefix('url_filters')}_denylist" in self.data:
            url_filters = self.cleaned_data.get("url_filters") or {}
            instance.set_url_filters(
                url_filters.get("allowlist", ""),
                url_filters.get("denylist", ""),
            )
        if commit:
            instance.save()
            instance.apply_crawl_config_filters()
            save_m2m = getattr(self, "_save_m2m", None)
            if callable(save_m2m):
                save_m2m()
        return instance


class CrawlAdmin(ConfigEditorMixin, BaseModelAdmin):
    form = CrawlAdminForm
    change_form_template = "admin/crawls/crawl/change_form.html"
    list_select_related = ()
    list_display = (
        "id",
        "created_at",
        "created_by",
        "max_depth",
        "stop_reason_badge",
        "pause_control",
        "resume_control",
        "label",
        "notes",
        "urls_preview",
        "schedule_str",
        "status",
        "retry_at",
        "health_display",
        "num_snapshots",
    )
    sort_fields = (
        "id",
        "created_at",
        "created_by",
        "max_depth",
        "label",
        "notes",
        "schedule_str",
        "status",
        "retry_at",
    )
    search_fields = (
        "id",
        "created_by__username",
        "max_depth",
        "label",
        "notes",
        "schedule_id",
        "status",
        "urls",
    )

    readonly_fields = ("created_at", "modified_at", "stop_reason_display")

    fieldsets = (
        (
            "URLs",
            {
                "fields": ("urls", "url_filters"),
                "classes": ("card", "wide"),
            },
        ),
        (
            "Overview",
            {
                "fields": (
                    ("label", "status", "retry_at", "schedule", "created_by", "created_at", "modified_at"),
                    ("max_depth",),
                    ("stop_reason_display",),
                    ("notes", "tags_editor"),
                ),
                "classes": ("card", "wide", "crawl-admin-overview"),
            },
        ),
        (
            "Config",
            {
                "fields": ("config",),
                "classes": ("card", "wide", "crawl-admin-config"),
            },
        ),
    )
    add_fieldsets = (
        (
            "URLs",
            {
                "fields": ("urls", "url_filters"),
                "classes": ("card", "wide"),
            },
        ),
        (
            "Overview",
            {
                "fields": (
                    ("label", "status", "retry_at", "schedule", "created_by"),
                    ("max_depth",),
                    ("notes", "tags_editor"),
                ),
                "classes": ("card", "wide", "crawl-admin-overview"),
            },
        ),
        (
            "Config",
            {
                "fields": ("config",),
                "classes": ("card", "wide", "crawl-admin-config"),
            },
        ),
    )

    list_filter = (MaxDepthListFilter, "schedule", "created_by", "status", "retry_at")
    ordering = ["-created_at", "-retry_at"]
    list_per_page = 50
    actions = ["pause_selected_crawls", "resume_selected_crawls", "delete_selected_batched"]
    change_actions = ["recrawl"]

    class Media:
        css = {"all": ("admin/crawls/crawl_change.css",)}
        js = ("admin/crawls/crawl_admin.js",)

    def get_queryset(self, request):
        """Keep joins page-local while computing per-row snapshot counts in the page query."""
        snapshot_count = (
            Snapshot.objects.filter(crawl_id=OuterRef("pk")).order_by().values("crawl_id").annotate(count=Count("pk")).values("count")
        )
        return (
            super()
            .get_queryset(request)
            .prefetch_related(
                "created_by",
                Prefetch("schedule", queryset=CrawlSchedule.objects.select_related("template")),
            )
            .annotate(
                num_snapshots_cached=Coalesce(
                    Subquery(snapshot_count, output_field=IntegerField()),
                    Value(0),
                ),
            )
        )

    def change_view(self, request, object_id, form_url="", extra_context=None):
        self.request = request
        crawl = self.get_object(request, object_id)
        extra_context = {
            **(extra_context or {}),
            "crawl_stop_reason": crawl.limit_stop_reason() if crawl else "",
            "crawl_snapshots_changelist": self.snapshots_changelist(crawl) if crawl else "",
        }
        return super().change_view(request, object_id, form_url, extra_context)

    def add_view(self, request, form_url="", extra_context=None):
        self.request = request
        return super().add_view(request, form_url, extra_context)

    def get_fieldsets(self, request, obj=None):
        return self.fieldsets if obj else self.add_fieldsets

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/snapshot/<path:snapshot_id>/delete/",
                self.admin_site.admin_view(self.delete_snapshot_view),
                name="crawls_crawl_snapshot_delete",
            ),
            path(
                "<path:object_id>/snapshot/<path:snapshot_id>/exclude-domain/",
                self.admin_site.admin_view(self.exclude_domain_view),
                name="crawls_crawl_snapshot_exclude_domain",
            ),
        ]
        return custom_urls + urls

    @admin.action(description="Delete selected crawls")
    def delete_selected_batched(self, request, queryset):
        """Delete crawls in a single transaction to avoid SQLite concurrency issues."""
        from django.db import transaction

        total = queryset.count()

        # Get list of IDs to delete first (outside transaction)
        ids_to_delete = list(queryset.values_list("pk", flat=True))

        # Delete everything in a single atomic transaction
        with transaction.atomic():
            deleted_count, _ = Crawl.objects.filter(pk__in=ids_to_delete).delete()

        messages.success(request, f"Successfully deleted {total} crawls ({deleted_count} total objects including related records).")

    @admin.action(description="Pause selected crawls")
    def pause_selected_crawls(self, request, queryset):
        paused = 0
        for crawl in queryset.exclude(status=Crawl.StatusChoices.SEALED).iterator(chunk_size=100):
            paused += int(crawl.pause())
        if paused:
            messages.success(request, f"Paused {paused} crawl(s). The runner will stop scheduling new work on the next sweep.")
        else:
            messages.warning(request, "No active crawls were selected to pause.")

    @admin.action(description="Resume selected crawls")
    def resume_selected_crawls(self, request, queryset):
        resumed = 0
        for crawl in queryset.iterator(chunk_size=100):
            if crawl.status == Crawl.StatusChoices.SEALED:
                paused_at = timezone.now()
                # Resume-from-sealed is an admin scheduler edit, not a full
                # Crawl.save() operation. Guard the iterator-read row with
                # modified_at so a stale changelist page cannot reopen a crawl
                # that the runner/admin changed after this action started.
                updated = crawl.safe_update(
                    {
                        "status": Crawl.StatusChoices.PAUSED,
                        "retry_at": RETRY_AT_MAX,
                        "modified_at": paused_at,
                    },
                    extra_filter={"status": Crawl.StatusChoices.SEALED},
                )
                if not updated:
                    continue
            resumed += int(crawl.resume())
        if resumed:
            messages.success(request, f"Resumed {resumed} crawl(s). The runner will pick them up on the next sweep.")
        else:
            messages.warning(request, "No paused or sealed crawls were selected to resume.")

    @action(label="Recrawl", description="Create a new crawl with the same settings")
    def recrawl(self, request, obj):
        """Duplicate this crawl as a new crawl with the same URLs and settings."""

        # Validate URLs (required for crawl to start)
        if not obj.urls:
            messages.error(request, "Cannot recrawl: original crawl has no URLs.")
            return redirect("admin:crawls_crawl_change", obj.id)

        new_crawl = Crawl.objects.create(
            urls=obj.urls,
            max_depth=obj.max_depth,
            tags_str=obj.tags_str,
            config=obj.config,
            schedule=obj.schedule,
            label=f"{obj.label} (recrawl)" if obj.label else "",
            notes=obj.notes,
            created_by=request.user,
            status=Crawl.StatusChoices.QUEUED,
            retry_at=timezone.now(),
        )

        messages.success(request, f"Created new crawl {new_crawl.id} with the same settings. It will start processing shortly.")

        return redirect("admin:crawls_crawl_change", new_crawl.id)

    @admin.display(description="Stop Reason")
    def stop_reason_display(self, obj):
        reason = obj.limit_stop_reason() if obj else ""
        if not reason:
            return mark_safe('<span class="crawl-stop-reason crawl-stop-reason--empty">None</span>')
        return format_html('<span class="crawl-stop-reason">{}</span>', reason)

    @admin.display(description="Stop Reason")
    def stop_reason_badge(self, obj):
        return self.stop_reason_display(obj)

    @admin.display(description="Resume")
    def resume_control(self, obj):
        if obj.status != Crawl.StatusChoices.SEALED and not obj.is_paused:
            return mark_safe('<span class="crawl-resume-muted">-</span>')
        reason = "paused" if obj.is_paused else (obj.limit_stop_reason() or "sealed")
        return format_html(
            '<button type="button" class="button crawl-resume-row" data-crawl-id="{}" title="Resume crawl. Stop reason: {}">Resume</button>',
            obj.pk,
            reason,
        )

    @admin.display(description="Pause")
    def pause_control(self, obj):
        if obj.status == Crawl.StatusChoices.SEALED:
            return mark_safe('<span class="crawl-resume-muted">-</span>')
        if obj.is_paused:
            return mark_safe('<span class="crawl-resume-muted">Paused</span>')
        return format_html(
            '<button type="button" class="button crawl-pause-row" data-crawl-id="{}" title="Pause crawl">Pause</button>',
            obj.pk,
        )

    def num_snapshots(self, obj):
        # Use cached annotation from get_queryset to avoid N+1
        count = getattr(obj, "num_snapshots_cached", None)
        if count is None:
            count = obj.snapshot_set.count()
        return count

    @admin.display(description="Snapshots")
    def snapshots_changelist(self, obj):
        request = getattr(self, "request", None)
        snapshot_changelist = reverse("admin:core_snapshot_changelist")
        scoped_params = {"crawl_id": str(obj.pk)}
        full_url = f"{snapshot_changelist}?{urlencode(scoped_params)}"
        if request is None:
            return format_html('<a class="button" href="{}">Open snapshots changelist</a>', full_url)

        snapshot_admin = self.admin_site._registry[Snapshot]
        changelist_request = copy(request)
        changelist_request.method = "GET"
        changelist_request.path = snapshot_changelist
        changelist_request.GET = request.GET.copy()
        changelist_request.GET.update(
            {
                **scoped_params,
                "_embedded": "crawl",
                "per_page": "200",
            },
        )
        changelist_request.POST = request.POST.copy()
        changelist_request.POST.clear()

        response = snapshot_admin.changelist_view(
            changelist_request,
            extra_context={"embedded_changelist": True},
        )
        context = {
            **response.context_data,
            "snapshot_changelist_url": full_url,
            "crawl": obj,
        }
        return mark_safe(render_to_string("admin/crawls/crawl/snapshots_changelist.html", context, request=request))

    def delete_snapshot_view(self, request: HttpRequest, object_id: str, snapshot_id: str):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        crawl = get_object_or_404(Crawl, pk=object_id)
        snapshot = get_object_or_404(Snapshot, pk=snapshot_id, crawl=crawl)

        if snapshot.status == Snapshot.StatusChoices.STARTED:
            snapshot.cancel_running_hooks()

        removed_urls = crawl.prune_url(snapshot.url)
        snapshot.delete()
        return JsonResponse(
            {
                "ok": True,
                "snapshot_id": str(snapshot.id),
                "removed_urls": removed_urls,
            },
        )

    def exclude_domain_view(self, request: HttpRequest, object_id: str, snapshot_id: str):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        crawl = get_object_or_404(Crawl, pk=object_id)
        snapshot = get_object_or_404(Snapshot, pk=snapshot_id, crawl=crawl)
        result = crawl.exclude_domain(snapshot.url)
        return JsonResponse(
            {
                "ok": True,
                **result,
            },
        )

    @admin.display(description="Schedule", ordering="schedule")
    def schedule_str(self, obj):
        if not obj.schedule:
            return mark_safe("<i>None</i>")
        return format_html('<a href="{}">{}</a>', obj.schedule.admin_change_url, obj.schedule)

    @admin.display(description="URLs", ordering="urls")
    def urls_preview(self, obj):
        first_url = next((line.strip() for line in (obj.urls or "").splitlines() if line.strip() and not line.strip().startswith("#")), "")
        return first_url[:80] + "..." if len(first_url) > 80 else first_url

    @admin.display(description="Health", ordering="health")
    def health_display(self, obj):
        h = obj.health
        color = "green" if h >= 80 else "orange" if h >= 50 else "red"
        return format_html('<span style="color: {};">{}</span>', color, h)

    @admin.display(description="URLs")
    def urls_editor(self, obj):
        """Editor for crawl URLs."""
        widget_id = f"crawl_urls_{obj.pk}"

        # Escape for safe HTML embedding
        escaped_urls = (obj.urls or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        # Count lines for auto-expand logic
        line_count = len((obj.urls or "").split("\n"))
        uri_rows = min(max(3, line_count), 10)

        html = f'''
        <div id="{widget_id}_container" style="max-width: 900px;">
            <!-- URLs input -->
            <div style="margin-bottom: 12px;">
                <label style="font-weight: bold; display: block; margin-bottom: 4px;">URLs (one per line):</label>
                <textarea id="{widget_id}_urls"
                          style="width: 100%; font-family: monospace; font-size: 13px;
                                 padding: 8px; border: 1px solid #ccc; border-radius: 4px;
                                 resize: vertical;"
                          rows="{uri_rows}"
                          placeholder="https://example.com&#10;https://example2.com&#10;# Comments start with #"
                          readonly>{escaped_urls}</textarea>
                <p style="color: #666; font-size: 12px; margin: 4px 0 0 0;">
                    {line_count} URL{"s" if line_count != 1 else ""} · Note: URLs displayed here for reference only
                </p>
            </div>
        </div>
        '''
        return mark_safe(html)


class CrawlScheduleAdmin(BaseModelAdmin):
    list_display = ("id", "created_at", "created_by", "label", "notes", "template_str", "crawls", "num_crawls", "num_snapshots")
    sort_fields = ("id", "created_at", "created_by", "label", "notes", "template_str")
    search_fields = ("id", "created_by__username", "label", "notes", "schedule_id", "template_id", "template__urls")

    readonly_fields = ("created_at", "modified_at", "crawls", "snapshots")

    fieldsets = (
        (
            "Schedule Info",
            {
                "fields": ("label", "notes"),
                "classes": ("card",),
            },
        ),
        (
            "Configuration",
            {
                "fields": ("schedule", "template"),
                "classes": ("card",),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_by", "created_at", "modified_at"),
                "classes": ("card",),
            },
        ),
        (
            "Crawls",
            {
                "fields": ("crawls",),
                "classes": ("card", "wide"),
            },
        ),
        (
            "Snapshots",
            {
                "fields": ("snapshots",),
                "classes": ("card", "wide"),
            },
        ),
    )

    list_filter = ("created_by",)
    ordering = ["-created_at"]
    list_per_page = 100
    actions = ["delete_selected"]

    def get_queryset(self, request):
        self.request = request
        return (
            super()
            .get_queryset(request)
            .select_related("created_by", "template")
            .annotate(
                crawl_count=Count("crawl", distinct=True),
                snapshot_count=Count("crawl__snapshot_set", distinct=True),
            )
        )

    def change_view(self, request, object_id, form_url="", extra_context=None):
        self.request = request
        return super().change_view(request, object_id, form_url, extra_context)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return tuple(fieldset for fieldset in self.fieldsets if fieldset[0] not in {"Crawls", "Snapshots"})
        return self.fieldsets

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id and getattr(request, "user", None) and request.user.is_authenticated:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Template", ordering="template")
    def template_str(self, obj):
        return format_html('<a href="{}">{}</a>', obj.template.admin_change_url, obj.template)

    @admin.display(description="# Crawls", ordering="crawl_count")
    def num_crawls(self, obj):
        count = getattr(obj, "crawl_count", None)
        if count is None:
            count = obj.crawl_set.count()
        return count

    @admin.display(description="# Snapshots", ordering="snapshot_count")
    def num_snapshots(self, obj):
        count = getattr(obj, "snapshot_count", None)
        if count is None:
            count = Snapshot.objects.filter(crawl__schedule=obj).count()
        return count

    def crawls(self, obj):
        return format_html_join(
            "<br/>",
            ' - <a href="{}">{}</a>',
            ((crawl.admin_change_url, crawl) for crawl in obj.crawl_set.all().order_by("-created_at")[:20]),
        ) or mark_safe("<i>No Crawls yet...</i>")

    def snapshots(self, obj):
        crawl_ids = obj.crawl_set.values_list("pk", flat=True)
        return render_snapshots_list(
            Snapshot.objects.filter(crawl_id__in=crawl_ids),
            request=getattr(self, "request", None),
            prefix="schedule_snapshots",
        )


def register_admin(admin_site):
    admin_site.register(Crawl, CrawlAdmin)
    admin_site.register(CrawlSchedule, CrawlScheduleAdmin)
