from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from abx_dl.events import CrawlCleanupEvent, CrawlCompletedEvent, CrawlSetupEvent, CrawlStartEvent
from abx_dl.services.base import BaseService
from archivebox.workers.models import ACTIVE_STATE_LEASE_SECONDS


class CrawlService(BaseService):
    LISTENS_TO = [CrawlSetupEvent, CrawlStartEvent, CrawlCleanupEvent, CrawlCompletedEvent]
    EMITS = []

    def __init__(self, bus, *, crawl_id: str):
        self.crawl_id = crawl_id
        super().__init__(bus)
        self.bus.on(CrawlSetupEvent, self.on_CrawlSetupEvent__save_to_db)
        self.bus.on(CrawlStartEvent, self.on_CrawlStartEvent__save_to_db)
        self.bus.on(CrawlCleanupEvent, self.on_CrawlCleanupEvent__save_to_db)
        self.bus.on(CrawlCompletedEvent, self.on_CrawlCompletedEvent__save_to_db)

    async def on_CrawlSetupEvent__save_to_db(self, event: CrawlSetupEvent) -> None:
        from archivebox.crawls.models import Crawl

        crawl = await Crawl.objects.aget(id=self.crawl_id)
        if crawl.is_paused:
            return
        if crawl.status == Crawl.StatusChoices.SEALED:
            if crawl.retry_at is not None:
                # Setup/start events can be emitted during targeted maintenance
                # on sealed snapshots. The snapshot retry_at owns that work; the
                # sealed parent crawl must stay invisible to crawl selection
                # unless an explicit resume/requeue path changes its status first.
                crawl.retry_at = None
                await crawl.asave(update_fields=["retry_at", "modified_at"])
            return
        crawl.status = Crawl.StatusChoices.STARTED
        crawl.retry_at = timezone.now() + timedelta(seconds=ACTIVE_STATE_LEASE_SECONDS)
        await crawl.asave(update_fields=["status", "retry_at", "modified_at"])

    async def on_CrawlStartEvent__save_to_db(self, event: CrawlStartEvent) -> None:
        from archivebox.crawls.models import Crawl

        crawl = await Crawl.objects.aget(id=self.crawl_id)
        if crawl.is_paused:
            return
        if crawl.status == Crawl.StatusChoices.SEALED:
            if crawl.retry_at is not None:
                # CrawlStart is also emitted by sealed-snapshot maintenance
                # runs. Do not turn a sealed crawl back into schedulable work
                # unless an explicit resume/requeue path changes its status first.
                crawl.retry_at = None
                await crawl.asave(update_fields=["retry_at", "modified_at"])
            return
        crawl.status = Crawl.StatusChoices.STARTED
        crawl.retry_at = timezone.now() + timedelta(seconds=ACTIVE_STATE_LEASE_SECONDS)
        await crawl.asave(update_fields=["status", "retry_at", "modified_at"])

    async def on_CrawlCleanupEvent__save_to_db(self, event: CrawlCleanupEvent) -> None:
        from archivebox.crawls.models import Crawl

        crawl = await Crawl.objects.aget(id=self.crawl_id)
        if crawl.is_paused:
            return
        # Cleanup is still inside the active crawl lifecycle. Snapshot hooks may
        # have just written discovery output that the runner consumes before the
        # completion phase, so only CrawlCompleted/finalize_run_state makes the
        # final sealed-vs-requeue decision.
        if crawl.status != Crawl.StatusChoices.SEALED:
            crawl.status = Crawl.StatusChoices.STARTED
            crawl.retry_at = timezone.now()
        else:
            crawl.retry_at = None
        await crawl.asave(update_fields=["status", "retry_at", "modified_at"])

    async def on_CrawlCompletedEvent__save_to_db(self, event: CrawlCompletedEvent) -> None:
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot

        crawl = await Crawl.objects.aget(id=self.crawl_id)
        if crawl.is_paused:
            return
        is_finished = not await crawl.snapshot_set.filter(
            status__in=[Snapshot.StatusChoices.QUEUED, Snapshot.StatusChoices.STARTED, Snapshot.StatusChoices.PAUSED],
        ).aexists()
        if not is_finished:
            if crawl.status != Crawl.StatusChoices.SEALED:
                crawl.status = Crawl.StatusChoices.STARTED
                crawl.retry_at = timezone.now()
            else:
                crawl.retry_at = None
            await crawl.asave(update_fields=["status", "retry_at", "modified_at"])
            return

        crawl.status = Crawl.StatusChoices.SEALED
        crawl.retry_at = None
        await crawl.asave(update_fields=["status", "retry_at", "modified_at"])
