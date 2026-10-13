"""Translate marked interface labels only; never conversation or model content."""

import json
from html import escape
from html.parser import HTMLParser
from importlib import resources


class _Page(HTMLParser):
    def __init__(self, language: str, translations: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.language = language
        self.translations = translations
        self.parts: list[str] = []
        self.stack: list[dict[str, str | None]] = []

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            values["lang"] = self.language
        if self.language == "en":
            for attribute in ("title", "aria-label", "placeholder", "alt"):
                key = values.get("data-i18n-" + attribute)
                if key is not None:
                    values[attribute] = self.translations.get(key, key)
        attributes = "".join(
            f" {key}" if value is None else f' {key}="{escape(value, quote=True)}"'
            for key, value in values.items()
        )
        self.parts.append(f"<{tag}{attributes}>")
        if tag not in {"meta", "link", "input", "img", "br", "hr"}:
            self.stack.append(values)

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        key = self.stack[-1].get("data-i18n") if self.stack else None
        value = (
            self.translations.get(key, key)
            if self.language == "en" and key is not None
            else data
        )
        self.parts.append(escape(value, quote=False))

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")


def render_page(filename: str, language: str) -> str:
    static = resources.files("jarvis").joinpath("interfaces", "static")
    translations = json.loads(
        static.joinpath("translations.json").read_text(encoding="utf-8")
    )
    page = _Page(language, translations)
    page.feed(static.joinpath(filename).read_text(encoding="utf-8"))
    encoded = json.dumps(translations, ensure_ascii=True).replace("<", "\\u003c")
    dictionary = f'<script id="translations" type="application/json">{encoded}</script>'
    return "".join(page.parts).replace("</head>", dictionary + "</head>")
