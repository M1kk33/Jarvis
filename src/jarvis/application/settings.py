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
        active_network: bool = False,
        active_browser: bool = False,
        browser_available: bool = False,
        network_available: bool = True,
    ) -> None:
        self.store = store
        self.model_name = model_name
        self.active_output_tokens = active_output_tokens
        self.context_tokens = context_tokens
        self.active_network = active_network
        self.active_browser = active_browser
        self.browser_available = browser_available
        self.network_available = network_available

    def restart_required(self, preferences: Preferences) -> bool:
        return (
            preferences.output_tokens != self.active_output_tokens
            or preferences.network_options(self.active_network, self.active_browser)
            != (self.active_network, self.active_browser)
        )

    async def read(self) -> Preferences:
        return await self.store.run(self.store.get_preferences)

    async def save(self, preferences: Preferences) -> Preferences:
        network, browser = preferences.network_options(
            self.active_network, self.active_browser
        )
        if network and not self.network_available:
            raise ValueError("Для чтения сайтов нужна подключённая модель.")
        if browser and not self.browser_available:
            raise ValueError("Браузерный компонент недоступен.")
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
