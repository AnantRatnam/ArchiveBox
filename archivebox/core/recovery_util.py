from __future__ import annotations

from django.utils import timezone
from rich.console import Console


def recover_orchestrator_state(*, include_chrome: bool = False) -> dict[str, int]:
    from archivebox.crawls.models import Crawl
    from archivebox.core.models import ArchiveResult, Snapshot
    from archivebox.machine.models import Process
    from django.db.models import Exists, OuterRef, Q, Subquery, Value
    from django.db.models.functions import Coalesce

    now = timezone.now()
    recovery_console = Console(stderr=True, highlight=False, soft_wrap=True)
    cleaned = {
        "stale_processes": Process.cleanup_stale_running(),
        "orphaned_processes": Process.cleanup_orphaned_workers(),
        "orphaned_chrome": Process.cleanup_orphaned_chrome() if include_chrome else 0,
        "queued_crawls_unlocked": 0,
        "unlocked_snapshots": 0,
        "queued_snapshots_unlocked": 0,
        "requeued_archiveresults": 0,
        "queued_snapshot_maintenance_scheduled": 0,
        "unlocked_crawls": 0,
    }

    running_archiveresults = ArchiveResult.objects.filter(
        snapshot_id=OuterRef("pk"),
        status=ArchiveResult.StatusChoices.STARTED,
        process__status=Process.StatusChoices.RUNNING,
    )
    active_child_snapshots = Snapshot.objects.filter(
        crawl_id=OuterRef("pk"),
        status__in=[Snapshot.StatusChoices.QUEUED, Snapshot.StatusChoices.STARTED, Snapshot.StatusChoices.PAUSED],
    )
    due_child_snapshots = active_child_snapshots.exclude(status=Snapshot.StatusChoices.PAUSED).filter(
        Q(retry_at__isnull=True) | Q(retry_at__lte=now),
    )
    next_future_child_retry = Subquery(
        active_child_snapshots.filter(retry_at__gt=now).order_by("retry_at").values("retry_at")[:1],
    )

    # Broken lock repair: QUEUED rows with retry_at=NULL are invisible to the
    # queue. Set only the scheduling field so the runner owns the next tick.
    cleaned["queued_crawls_unlocked"] = Crawl.objects.filter(
        status=Crawl.StatusChoices.QUEUED,
        retry_at__isnull=True,
    ).update(retry_at=now, modified_at=now)
    cleaned["queued_snapshots_unlocked"] = Snapshot.objects.filter(
        status=Snapshot.StatusChoices.QUEUED,
        retry_at__isnull=True,
    ).update(retry_at=now, modified_at=now)
    # ArchiveResult has no retry_at scheduler; BACKOFF is a legacy/impossible
    # persisted state here, so move it back to QUEUED for the snapshot runner.
    cleaned["requeued_archiveresults"] = ArchiveResult.objects.filter(
        status=ArchiveResult.StatusChoices.BACKOFF,
    ).update(status=ArchiveResult.StatusChoices.QUEUED, modified_at=now)
    # Impossible state repair: STARTED ArchiveResults without a live Process
    # have no owner left to emit completion. Requeue only the result row; the
    # snapshot/crawl schedulers will pick up normal retry processing.
    cleaned["requeued_archiveresults"] += (
        ArchiveResult.objects.filter(status=ArchiveResult.StatusChoices.STARTED)
        .exclude(process__status=Process.StatusChoices.RUNNING)
        .update(status=ArchiveResult.StatusChoices.QUEUED, process=None, modified_at=now)
    )
    # SEALED snapshots can legitimately carry targeted maintenance work
    # (`update --index-only`, filesystem migrations). Status stays SEALED; this
    # only makes existing queued ArchiveResult rows visible to the sealed
    # snapshot branch of the runner.
    queued_maintenance_snapshot_ids = tuple(
        Snapshot.objects.filter(
            status=Snapshot.StatusChoices.SEALED,
            retry_at__isnull=True,
            archiveresult__status=ArchiveResult.StatusChoices.QUEUED,
        )
        .order_by("modified_at", "id")
        .values_list("id", flat=True)[:1000],
    )
    cleaned["queued_snapshot_maintenance_scheduled"] = Snapshot.objects.filter(id__in=queued_maintenance_snapshot_ids).update(
        retry_at=now,
        modified_at=now,
    )

    started_snapshots = Snapshot.objects.filter(status=Snapshot.StatusChoices.STARTED).filter(
        Q(retry_at__isnull=True) | Q(retry_at__gt=now),
    )

    # Broken lock repair: STARTED + retry_at=NULL or retry_at in the future
    # means "owned by an active runner". Recovery only runs from the current
    # elected runner after Process cleanup has proven old owners are gone, so
    # STARTED rows with no live ArchiveResult process should not wait out the
    # previous runner's full lease before the new runner can resume them.
    # We only unlock scheduling; normal Snapshot runner code owns the next
    # transition and side effects.
    cleaned["unlocked_snapshots"] = (
        started_snapshots.annotate(has_running_results=Exists(running_archiveresults))
        .filter(has_running_results=False)
        .update(
            retry_at=now,
            modified_at=now,
        )
    )

    # Broken lock repair: STARTED + retry_at=NULL is an orphaned ownership
    # lease. Recovery only unlocks scheduling; the runner owns any subsequent
    # state-machine transition, including sealing rows whose children/results
    # are already final.
    recoverable_started_crawls = Crawl.objects.filter(status=Crawl.StatusChoices.STARTED).filter(
        Q(retry_at__isnull=True) | Q(retry_at__gt=now),
    )

    due_started_crawls = recoverable_started_crawls.annotate(has_due_child=Exists(due_child_snapshots)).filter(has_due_child=True)
    cleaned["unlocked_crawls"] = due_started_crawls.update(retry_at=now, modified_at=now)
    future_started_crawls = recoverable_started_crawls.annotate(
        has_active_child=Exists(active_child_snapshots),
        has_due_child=Exists(due_child_snapshots),
        next_child_retry=next_future_child_retry,
    ).filter(has_active_child=True, has_due_child=False)
    cleaned["unlocked_crawls"] += future_started_crawls.update(retry_at=Coalesce("next_child_retry", Value(now)), modified_at=now)
    finished_started_crawls = recoverable_started_crawls.annotate(has_active_child=Exists(active_child_snapshots)).filter(
        has_active_child=False,
    )
    cleaned["unlocked_crawls"] += finished_started_crawls.update(retry_at=now, modified_at=now)

    warning_recoveries = {
        "stale_processes": "marked stale running Process row(s) exited",
        "orphaned_processes": "marked orphaned worker/hook Process row(s) exited",
        "orphaned_chrome": "terminated orphaned Chrome process(es)",
        "unlocked_snapshots": "unlocked started Snapshot row(s) whose owner process was gone",
        "queued_snapshot_maintenance_scheduled": "scheduled sealed Snapshot row(s) with queued maintenance ArchiveResults",
    }
    scheduler_repairs = {
        "queued_crawls_unlocked": "made queued Crawl row(s) with retry_at=NULL visible to the runner",
        "queued_snapshots_unlocked": "made queued Snapshot row(s) with retry_at=NULL visible to the runner",
        "requeued_archiveresults": "requeued ArchiveResult row(s) whose owner process was gone",
    }
    for key, message in warning_recoveries.items():
        if cleaned[key]:
            recovery_console.print(f"[yellow]⚠️ Orchestrator recovery: {cleaned[key]} {message}.[/yellow]")
    if cleaned["unlocked_crawls"]:
        recovery_console.print(
            f"[yellow]⚠️ Repairing: Rescheduled {cleaned['unlocked_crawls']} Crawl row(s) "
            "that were left unfinished by a previous runner[/yellow]",
        )
    for key, message in scheduler_repairs.items():
        if cleaned[key]:
            recovery_console.print(f"[yellow]⚠️ Orchestrator scheduler repair: {cleaned[key]} {message}.[/yellow]")

    return cleaned
