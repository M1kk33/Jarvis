"""Read-only totals and optional exact token checkpoints while a model is leased."""

import asyncio
from threading import Event

from jarvis.application.resource_manager import _settle
from jarvis.domain.ports import StatisticsStore, TextTokenCounter, TextTokenStore
from jarvis.domain.statistics import ChatStatistics
from jarvis.domain.types import RequestFailure


class StatisticsService:
    def __init__(self, store: StatisticsStore) -> None:
        self.store = store

    async def read(self, conversation_id: str | None = None) -> ChatStatistics:
        return await self.store.run(lambda: self.store.get_statistics(conversation_id))


async def checkpoint_text_tokens(
    store: TextTokenStore,
    provider: object,
    turn_id: str,
    text: str,
    role: str,
    cancel: Event,
    job_attempt: int | None,
) -> None:
    """Count saved text, not prompts/tools/images. Unsupported counts stay unknown.

    The caller holds the runtime lease until both tokenization and persistence
    settle. Optional tokenizer failure must not discard a valid model reply.
    """
    if not isinstance(provider, TextTokenCounter):
        return
    if cancel.is_set():
        raise RequestFailure("cancelled", "Token checkpoint cancelled")
    task = asyncio.create_task(asyncio.to_thread(provider.count_output_tokens, text))
    try:
        count = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await _settle(task)
        except Exception:  # noqa: BLE001, S110 -- optional telemetry; cleanup still follows
            pass
        raise
    except Exception:  # noqa: BLE001 -- unavailable telemetry remains unknown
        return
    if type(count) is not int or count < 0:
        return
    if cancel.is_set():
        raise RequestFailure("cancelled", "Token checkpoint cancelled")
    commit = asyncio.create_task(
        store.run(
            lambda: store.checkpoint_text_tokens(
                turn_id, text, count, role, job_attempt
            )
        )
    )
    try:
        await asyncio.shield(commit)
    except asyncio.CancelledError:
        # Obtain the owner-thread outcome by the same stable turn identity.
        await _settle(commit)
        raise
