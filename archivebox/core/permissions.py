from __future__ import annotations

from django.db.models import Q, QuerySet
from django.http import HttpRequest

from archivebox.config.common import get_config

PERMISSIONS_PUBLIC = "public"
PERMISSIONS_UNLISTED = "unlisted"
PERMISSIONS_PRIVATE = "private"
PERMISSIONS_CHOICES = (
    (PERMISSIONS_PUBLIC, "Public"),
    (PERMISSIONS_UNLISTED, "Unlisted"),
    (PERMISSIONS_PRIVATE, "Private"),
)


def is_admin_user(request: HttpRequest) -> bool:
    user = request.user
    return bool(user.is_authenticated and user.is_active and user.is_staff)


def get_snapshot_permissions(snapshot) -> str:
    try:
        return str(get_config(snapshot=snapshot, resolve_plugins=False).PERMISSIONS).strip().lower()
    except Exception:
        return PERMISSIONS_PRIVATE


def can_view_snapshot(request: HttpRequest, snapshot) -> bool:
    permissions = get_snapshot_permissions(snapshot)
    return permissions in {PERMISSIONS_PUBLIC, PERMISSIONS_UNLISTED} or is_admin_user(request)


def _persona_ids_for_permissions(allowed_permissions: set[str]) -> list[str]:
    from archivebox.personas.models import Persona

    fallback_permissions = str(get_config(resolve_plugins=False).PERMISSIONS).strip().lower()
    personas = Persona.objects.only("id", "config")
    return [
        str(persona.id)
        for persona in personas
        if (persona.permissions or fallback_permissions) in allowed_permissions
    ]


def filter_personas_by_permissions(queryset: QuerySet, allowed_permissions: set[str]) -> QuerySet:
    return queryset.filter(id__in=_persona_ids_for_permissions(allowed_permissions))


def filter_snapshots_by_permissions(queryset: QuerySet, *, direct: bool = False, allowed_permissions: set[str] | None = None) -> QuerySet:
    from archivebox.crawls.models import Crawl
    from archivebox.personas.models import Persona

    allowed_permissions = allowed_permissions or ({PERMISSIONS_PUBLIC, PERMISSIONS_UNLISTED} if direct else {PERMISSIONS_PUBLIC})
    fallback_permissions = str(get_config(resolve_plugins=False).PERMISSIONS).strip().lower()
    has_overrides = (
        queryset.model.objects.filter(permissions__gt="").exists()
        or Crawl.objects.filter(permissions__gt="").exists()
        or Persona.objects.filter(permissions__gt="").exists()
    )
    if not has_overrides:
        return queryset if fallback_permissions in allowed_permissions else queryset.none()

    allowed_persona_ids = _persona_ids_for_permissions(allowed_permissions)
    valid_persona_ids = [str(persona_id) for persona_id in Persona.objects.values_list("id", flat=True)]
    fallback_query = Q(crawl__persona_id__in=allowed_persona_ids)
    if fallback_permissions in allowed_permissions:
        fallback_query |= Q(crawl__persona_id__isnull=True) | ~Q(crawl__persona_id__in=valid_persona_ids)
    inherited_query = Q(crawl__permissions__in=sorted(allowed_permissions)) | (Q(crawl__permissions__isnull=True) & fallback_query)
    return queryset.filter(
        Q(permissions__in=sorted(allowed_permissions)) | (Q(permissions__isnull=True) & inherited_query),
    )


def public_snapshots_queryset(queryset: QuerySet) -> QuerySet:
    return filter_snapshots_by_permissions(queryset, direct=False)


def direct_snapshots_queryset(request: HttpRequest, queryset: QuerySet) -> QuerySet:
    return queryset if is_admin_user(request) else filter_snapshots_by_permissions(queryset, direct=True)
