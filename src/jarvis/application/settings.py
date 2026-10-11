"""Durable preferences, separate from active inference configuration."""

import asyncio

from jarvis.domain.ports import PreferencesStore
from jarvis.domain.preferences import Preferences


class SettingsService:
    def __init__(
        self,
        store: PreferencesStore,
        *,
        model_name: str,
        active_output_tokens: int,
        context_tokens: int,
    ) -> None:
        self.store = store
        self.model_name = model_name
        self.active_output_tokens = active_output_tokens
        self.context_tokens = context_tokens

    async def read(self) -> Preferences:
        return await self.store.run(self.store.get_preferences)

    async def save(self, preferences: Preferences) -> Preferences:
        try:
            return await self.store.run(lambda: self.store.set_preferences(preferences))
        except asyncio.CancelledError:
            # An owner-thread commit may have completed despite caller cancellation.
            verification = asyncio.create_task(self.read())
            while not verification.done():
                try:
                    await asyncio.shield(verification)
                except asyncio.CancelledError:
                    continue
            if verification.result() == preferences:
                return preferences
            raise
