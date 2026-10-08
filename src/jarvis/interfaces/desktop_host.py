"""Window-only process. The parent owns server lifecycle and the Windows job."""

import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit


def main() -> None:
    # No page text, tokens or WebView diagnostics in logs.
    logging.getLogger("pywebview").disabled = True
    import webview

    config = json.load(sys.stdin)
    url = urlsplit(config["url"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.path != "/setup":
        raise ValueError("Expected local setup URL")
    cwd = Path.cwd()
    exiting = threading.Event()
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    window = webview.create_window(
        config["title"],
        config["url"],
        width=1240,
        height=820,
        min_size=(800, 600),
        background_color="#101216",
        text_select=True,
    )
    if window is None:
        raise RuntimeError("Cannot create desktop window")

    def closing() -> bool:
        if exiting.is_set():
            return True
        (cwd / "closing").touch()
        window.set_title("Jarvis — завершение работы…")
        if (cwd / "ready").exists():
            try:
                window.run_js(
                    "document.body.inert = true; document.getElementById('notice')?.replaceChildren('Завершаю работу и освобождаю модели…');"
                )
            except webview.WebViewException:
                pass  # A closing/crashed renderer must not bypass server cleanup.
        return False

    def constrain_navigation() -> None:
        from System.Drawing import Icon  # type: ignore[import-not-found]

        cast(Any, window.native).Icon = Icon(
            str(Path(__file__).parent / "static" / "jarvis.ico")
        )

        def navigating(_sender: object, event: Any) -> None:
            target = urlsplit(str(event.Uri))
            if (target.scheme, target.netloc) != (url.scheme, url.netloc):
                event.Cancel = True

        cast(Any, window.native).browser.webview.NavigationStarting += navigating

    def loaded() -> None:
        (cwd / "ready").touch()

    def watch() -> None:
        while not exiting.wait(0.25):
            if (cwd / "stop").exists():
                exiting.set()
                window.destroy()
                return

    window.events.closing += closing
    window.events.before_show += constrain_navigation
    window.events.loaded += loaded
    window.events.closed += exiting.set
    # No Python API is exposed to the page. Existing session/CSRF/CSP remain.
    webview.start(
        watch,
        gui="edgechromium",
        private_mode=True,
        storage_path=str(cwd / "webview"),
        debug=False,
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 -- report failure without logging secrets
        (Path.cwd() / "error").touch()
        raise SystemExit(1) from None
