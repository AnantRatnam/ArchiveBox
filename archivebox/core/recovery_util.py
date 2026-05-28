from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rich.console import Console


def recover_orchestrator_state(*, include_chrome: bool = False) -> dict[str, int]:
    from archivebox.crawls.models import Crawl
    from archivebox.core.models import ArchiveResult, Snapshot
    from archivebox.machine.models import Process
    from django.db.models import Exists, OuterRef, Q, Subquery, Value
    from django.db.models.functions import Coalesce

    now = timezone.now()
    stuck_cutoff = now - timedelta(hours=12)
    recovery_console = Console(stderr=True, highlight=False)
    cleaned = {
        "stale_processes": Process.cleanup_stale_running(),
        "orphaned_processes": Process.cleanup_orphaned_workers(),
        "orphaned_chrome": Process.cleanup_orphaned_chrome() if include_chrome else 0,
        "queued_crawls_unlocked": 0,
        "sealed_crawl_locks_cleared": 0,
        "sealed_snapshots": 0,
        "unlocked_snapshots": 0,
        "requeued_snapshots": 0,
        "queued_snapshots_unlocked": 0,
        "sealed_snapshot_locks_cleared": 0,
        "requeued_archiveresults": 0,
        "sealed_crawls": 0,
        "unlocked_crawls": 0,
        "requeued_crawls": 0,
        "sealed_queued_snapshots": 0,
        "sealed_queued_crawls": 0,
    }

    any_archiveresults = ArchiveResult.objects.filter(snapshot_id=OuterRef("pk"))
    unfinished_archiveresults = any_archiveresults.exclude(status__in=ArchiveResult.FINAL_STATES)
    recent_snapshots = Snapshot.objects.filter(crawl_id=OuterRef("pk"), modified_at__gt=stuck_cutoff)
    recent_archiveresults = ArchiveResult.objects.filter(snapshot__crawl_id=OuterRef("pk"), modified_at__gt=stuck_cutoff)
    recent_archiveresult_processes = Process.objects.filter(
        archiveresult__snapshot__crawl_id=OuterRef("pk"),
        modified_at__gt=stuck_cutoff,
    )
    recent_crawl_snapshots_for_snapshot = Snapshot.objects.filter(crawl_id=OuterRef("crawl_id"), modified_at__gt=stuck_cutoff)
    recent_crawl_archiveresults_for_snapshot = ArchiveResult.objects.filter(
        snapshot__crawl_id=OuterRef("crawl_id"),
        modified_at__gt=stuck_cutoff,
    )
    recent_crawl_archiveresult_processes_for_snapshot = Process.objects.filter(
        archiveresult__snapshot__crawl_id=OuterRef("crawl_id"),
        modified_at__gt=stuck_cutoff,
    )

    # Stale-only repair: if a queued snapshot/crawl already has only final
    # projected result rows and the whole crawl has been quiet for >12hr, it
    # was likely interrupted after hook completion but before state sealing.
    # Never run this on fresh rows: queued work is normal during direct
    # reindex/extract and while a daemon runner is active.
    stale_finished_snapshot_ids = (
        Snapshot.objects.filter(
            status=Snapshot.StatusChoices.QUEUED,
            modified_at__lte=stuck_cutoff,
            crawl__modified_at__lte=stuck_cutoff,
        )
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=stuck_cutoff))
        .annotate(
            has_results=Exists(any_archiveresults),
            has_unfinished_results=Exists(unfinished_archiveresults),
            has_recent_snapshot=Exists(recent_crawl_snapshots_for_snapshot),
            has_recent_archiveresult=Exists(recent_crawl_archiveresults_for_snapshot),
            has_recent_archiveresult_process=Exists(recent_crawl_archiveresult_processes_for_snapshot),
        )
        .filter(
            has_results=True,
            has_unfinished_results=False,
            has_recent_snapshot=False,
            has_recent_archiveresult=False,
            has_recent_archiveresult_process=False,
        )
        .values_list("id", flat=True)
    )
    cleaned["sealed_queued_snapshots"] = Snapshot.objects.filter(id__in=stale_finished_snapshot_ids).update(
        status=Snapshot.StatusChoices.SEALED,
        retry_at=None,
        downloaded_at=Coalesce("downloaded_at", Value(now)),
    )
    unrecoverable_active_child_snapshots = (
        Snapshot.objects.filter(
            crawl_id=OuterRef("pk"),
            status__in=[
                Snapshot.StatusChoices.QUEUED,
                Snapshot.StatusChoices.STARTED,
                Snapshot.StatusChoices.PAUSED,
            ],
        )
        .annotate(
            has_results=Exists(any_archiveresults),
            has_unfinished_results=Exists(unfinished_archiveresults),
        )
        .filter(
            Q(status=Snapshot.StatusChoices.STARTED)
            | Q(modified_at__gt=stuck_cutoff)
            | Q(retry_at__gt=stuck_cutoff)
            | Q(has_results=False)
            | Q(has_unfinished_results=True),
        )
    )
    cleaned["sealed_queued_crawls"] = (
        Crawl.objects.filter(
            status=Crawl.StatusChoices.QUEUED,
            snapshot_set__isnull=False,
            modified_at__lte=stuck_cutoff,
        )
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=stuck_cutoff))
        .annotate(
            has_unrecoverable_active_child=Exists(unrecoverable_active_child_snapshots),
            has_recent_snapshot=Exists(recent_snapshots),
            has_recent_archiveresult=Exists(recent_archiveresults),
            has_recent_archiveresult_process=Exists(recent_archiveresult_processes),
        )
        .filter(
            has_unrecoverable_active_child=False,
            has_recent_snapshot=False,
            has_recent_archiveresult=False,
            has_recent_archiveresult_process=False,
        )
        .update(
            status=Crawl.StatusChoices.SEALED,
            retry_at=None,
            modified_at=now,
        )
    )

    stale_crawls = (
        Crawl.objects.filter(
            status__in=[Crawl.StatusChoices.QUEUED, Crawl.StatusChoices.STARTED],
            modified_at__lte=stuck_cutoff,
        )
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=now))
        .annotate(
            has_recent_snapshot=Exists(recent_snapshots),
            has_recent_archiveresult=Exists(recent_archiveresults),
            has_recent_archiveresult_process=Exists(recent_archiveresult_processes),
        )
        .filter(has_recent_snapshot=False, has_recent_archiveresult=False, has_recent_archiveresult_process=False)
        .order_by("modified_at")[:10]
    )
    stale_crawl_messages = []
    for crawl in stale_crawls:
        if not Process.objects.filter(
            pwd__contains=str(crawl.id),
            status=Process.StatusChoices.RUNNING,
            modified_at__gt=stuck_cutoff,
        ).exists():
            stale_crawl_messages.append(
                f"{crawl.id} status={crawl.status} retry_at={crawl.retry_at} modified_at={crawl.modified_at}",
            )
    if stale_crawl_messages:
        recovery_console.print(
            "[red]❌ Orchestrator recovery found stuck active crawl invariant violation; refusing to continue.[/red]",
        )
        raise RuntimeError(
            "Stuck crawl invariant violated: active crawls had no crawl/snapshot/result/process changes for >12hr: "
            + "; ".join(stale_crawl_messages),
        )

    running_archiveresults = ArchiveResult.objects.filter(
        snapshot_id=OuterRef("pk"),
        status=ArchiveResult.StatusChoices.STARTED,
        process__status=Process.StatusChoices.RUNNING,
    )
    unfinished_archiveresult_statuses = [
        ArchiveResult.StatusChoices.QUEUED,
        ArchiveResult.StatusChoices.STARTED,
        ArchiveResult.StatusChoices.PAUSED,
        ArchiveResult.StatusChoices.BACKOFF,
    ]
    running_unfinished_archiveresults = ArchiveResult.objects.filter(
        snapshot_id=OuterRef("pk"),
        status__in=unfinished_archiveresult_statuses,
        process__status=Process.StatusChoices.RUNNING,
    )
    unfinished_without_running_archiveresults = ArchiveResult.objects.filter(
        snapshot_id=OuterRef("pk"),
        status__in=[
            ArchiveResult.StatusChoices.QUEUED,
            ArchiveResult.StatusChoices.STARTED,
            ArchiveResult.StatusChoices.BACKOFF,
        ],
    ).exclude(
        status=ArchiveResult.StatusChoices.PAUSED,
    ).exclude(
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

    started_snapshots = Snapshot.objects.filter(
        status=Snapshot.StatusChoices.STARTED,
        retry_at__isnull=True,
    )

    # Normal transition: the snapshot has finished all known extractor work,
    # but the process died before the state machine got to seal it.
    finished_snapshot_ids = (
        started_snapshots.annotate(
            has_results=Exists(any_archiveresults),
            has_unfinished_results=Exists(unfinished_archiveresults),
        )
        .filter(has_results=True, has_unfinished_results=False)
        .values_list("id", flat=True)
    )
    for snapshot in Snapshot.objects.filter(id__in=finished_snapshot_ids).select_related("crawl").iterator(chunk_size=100):
        snapshot.sm.seal()
        cleaned["sealed_snapshots"] += 1

    # Broken lock repair: STARTED + retry_at=NULL means "owned by an active
    # runner". If no ArchiveResult has a live process anymore, only unlock it.
    # The existing runner will pick the row up through the normal queue path.
    cleaned["unlocked_snapshots"] = (
        started_snapshots.annotate(has_running_results=Exists(running_archiveresults))
        .filter(has_running_results=False)
        .update(
            retry_at=now,
            modified_at=now,
        )
    )

    # Impossible state repair: a SEALED snapshot with a still-running child is
    # active, not final. Reflect that without starting duplicate work.
    cleaned["requeued_snapshots"] += (
        Snapshot.objects.filter(status=Snapshot.StatusChoices.SEALED)
        .annotate(has_running_unfinished_results=Exists(running_unfinished_archiveresults))
        .filter(has_running_unfinished_results=True)
        .update(
            status=Snapshot.StatusChoices.STARTED,
            retry_at=None,
            modified_at=now,
        )
    )

    # Impossible state repair: SEALED snapshots should not contain unfinished
    # ArchiveResults. There is no valid state-machine transition from final
    # back to queued, so repair only the fields needed for the runner to retry.
    cleaned["requeued_snapshots"] += (
        Snapshot.objects.filter(status=Snapshot.StatusChoices.SEALED)
        .annotate(has_unfinished_results_without_running=Exists(unfinished_without_running_archiveresults))
        .filter(has_unfinished_results_without_running=True)
        .update(
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=now,
            modified_at=now,
        )
    )

    # Normal transition: a started crawl has no active snapshots left.
    finished_crawl_ids = (
        Crawl.objects.filter(status=Crawl.StatusChoices.STARTED, retry_at__isnull=True)
        .exclude(
            snapshot_set__status__in=[
                Snapshot.StatusChoices.QUEUED,
                Snapshot.StatusChoices.STARTED,
                Snapshot.StatusChoices.PAUSED,
            ],
        )
        .values_list("id", flat=True)
    )
    for crawl in Crawl.objects.filter(id__in=finished_crawl_ids).iterator(chunk_size=100):
        crawl.sm.seal()
        cleaned["sealed_crawls"] += 1

    # Broken lock repair: STARTED + retry_at=NULL with unfinished snapshots is
    # recoverable by unlocking the crawl. Do not create snapshots or results.
    due_started_crawls = (
        Crawl.objects.filter(status=Crawl.StatusChoices.STARTED, retry_at__isnull=True)
        .annotate(has_due_child=Exists(due_child_snapshots))
        .filter(has_due_child=True)
    )
    cleaned["unlocked_crawls"] = due_started_crawls.update(retry_at=now, modified_at=now)
    future_started_crawls = (
        Crawl.objects.filter(status=Crawl.StatusChoices.STARTED, retry_at__isnull=True)
        .annotate(has_active_child=Exists(active_child_snapshots), has_due_child=Exists(due_child_snapshots), next_child_retry=next_future_child_retry)
        .filter(has_active_child=True, has_due_child=False)
    )
    cleaned["unlocked_crawls"] += future_started_crawls.update(retry_at=Coalesce("next_child_retry", Value(now)), modified_at=now)

    cleaned["requeued_crawls"] = 0

    warning_recoveries = {
        "stale_processes": "marked stale running Process row(s) exited",
        "orphaned_processes": "marked orphaned worker/hook Process row(s) exited",
        "orphaned_chrome": "terminated orphaned Chrome process(es)",
        "sealed_snapshots": "sealed started Snapshot row(s) whose ArchiveResults were already final",
        "unlocked_snapshots": "unlocked started Snapshot row(s) whose owner process was gone",
        "sealed_crawls": "sealed started Crawl row(s) with no active Snapshots",
        "unlocked_crawls": "unlocked started Crawl row(s) with pending child Snapshots",
    }
    error_recoveries = {
        "queued_crawls_unlocked": "repaired queued Crawl row(s) with retry_at=NULL",
        "queued_snapshots_unlocked": "repaired queued Snapshot row(s) with retry_at=NULL",
        "requeued_archiveresults": "requeued ArchiveResult row(s) left in BACKOFF",
        "requeued_snapshots": "reopened sealed Snapshot row(s) with unfinished ArchiveResults",
        "requeued_crawls": "reopened sealed Crawl row(s) with active child Snapshots",
        "sealed_queued_snapshots": "sealed stale queued Snapshot row(s) whose ArchiveResults were already final",
        "sealed_queued_crawls": "sealed stale queued Crawl row(s) whose Snapshots were already final",
    }
    for key, message in warning_recoveries.items():
        if cleaned[key]:
            recovery_console.print(f"[yellow]⚠️ Orchestrator recovery: {cleaned[key]} {message}.[/yellow]")
    for key, message in error_recoveries.items():
        if cleaned[key]:
            recovery_console.print(f"[red]❌ Orchestrator invariant repair: {cleaned[key]} {message}.[/red]")

    return cleaned
