"""Small, backend-neutral user preferences; runtime profiles stay pinned."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Preferences:
    theme: str = "dark"
    accent: str = "mint"
    text_size: int = 14
    send_shortcut: str = "enter"
    voice_volume: int = 80
    output_tokens: int = 2048
    language: str = "ru"
    # None preserves launch options for installations predating these controls.
    enable_network: bool | None = None
    enable_browser: bool | None = None

    def __post_init__(self) -> None:
        for option in (self.enable_network, self.enable_browser):
            if option is not None and type(option) is not bool:
                raise ValueError("Invalid network preference")
        if self.enable_browser is True and self.enable_network is not True:
            raise ValueError("Browser requires network access")
        for value, allowed in (
            (self.theme, {"dark", "light", "system"}),
            (self.accent, {"mint", "blue", "violet"}),
            (self.send_shortcut, {"enter", "ctrl_enter"}),
            (self.language, {"ru", "en"}),
        ):
            if type(value) is not str or value not in allowed:
                raise ValueError("Invalid preference choice")
        if type(self.text_size) is not int or self.text_size not in {14, 16, 18}:
            raise ValueError("Invalid text size")
        if type(self.voice_volume) is not int or not 0 <= self.voice_volume <= 100:
            raise ValueError("Invalid voice volume")
        if type(self.output_tokens) is not int or self.output_tokens not in {
            512,
            1024,
            2048,
        }:
            raise ValueError("Invalid output token limit")

    def network_options(self, network: bool, browser: bool) -> tuple[bool, bool]:
        network = network if self.enable_network is None else self.enable_network
        browser = browser if self.enable_browser is None else self.enable_browser
        return network, browser and network
