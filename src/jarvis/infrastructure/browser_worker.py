"""Disposable, offline Playwright renderer for one pre-fetched HTML document."""

import json
import socketserver
import sys
import threading
from pathlib import Path
from typing import cast

from playwright.sync_api import sync_playwright

_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'none'; img-src 'none'; font-src 'none'; media-src 'none'; "
    "object-src 'none'; frame-src 'none'; worker-src 'none'; form-action 'none'; "
    "base-uri 'none'"
)


class _DenyProxy(socketserver.TCPServer):
    allow_reuse_address = False
    hits = 0


class _Drop(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        cast(_DenyProxy, self.server).hits += 1
        self.request.settimeout(0.5)
        try:
            self.request.recv(4096)
        except OSError:
            pass


def render(request: dict[str, object]) -> dict[str, object]:
    if request.get("version") != 1 or set(request) != {
        "version",
        "html",
        "screenshot",
        "browser",
    }:
        raise ValueError("Invalid browser request")
    source = Path(str(request["html"]))
    screenshot = Path(str(request["screenshot"]))
    executable = Path(str(request["browser"]))
    if source.parent != screenshot.parent or not executable.is_file():
        raise ValueError("Browser paths are invalid")
    if source.stat().st_size > 512 * 1024:
        raise ValueError("HTML input exceeds limit")
    html = source.read_bytes().decode("utf-8", errors="replace")
    # The browser receives no approved URL or local conversation data. The first
    # policy in the document blocks page-initiated network paths.
    safe_html = (
        '<meta http-equiv="Content-Security-Policy" content="' + _CSP + '">' + html
    )
    counters = {"requests": 0, "popups": 0, "downloads": 0}
    with _DenyProxy(("127.0.0.1", 0), _Drop) as proxy:
        server = threading.Thread(target=proxy.serve_forever, daemon=True)
        server.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=str(executable),
                    timeout=10_000,
                    proxy={
                        "server": f"http://127.0.0.1:{proxy.server_address[1]}",
                        "bypass": "<-loopback>",
                    },
                    args=[
                        "--disable-background-networking",
                        "--disable-quic",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                    ],
                )
                try:
                    context = browser.new_context(
                        offline=True,
                        service_workers="block",
                        accept_downloads=False,
                        permissions=[],
                        viewport={"width": 1024, "height": 768},
                    )
                    try:
                        context.add_init_script(
                            "for (const name of ['RTCPeerConnection', "
                            "'webkitRTCPeerConnection', 'WebTransport', "
                            "'EventSource', 'WebSocket']) "
                            "Object.defineProperty(globalThis, name, "
                            "{value: undefined, configurable: false});"
                        )

                        def block(route) -> None:
                            counters["requests"] += 1
                            route.abort("blockedbyclient")

                        context.route("**/*", block)
                        page = context.new_page()

                        def close_popup(other) -> None:
                            if other != page:
                                counters["popups"] += 1
                                other.close()

                        def cancel_download(download) -> None:
                            counters["downloads"] += 1
                            download.cancel()

                        context.on("page", close_popup)
                        page.on("download", cancel_download)
                        page.on("dialog", lambda dialog: dialog.dismiss())
                        page.set_content(
                            safe_html, wait_until="domcontentloaded", timeout=5_000
                        )
                        page.wait_for_timeout(250)
                        if page.url != "about:blank" or len(context.pages) != 1:
                            raise ValueError("Browser navigation escaped the document")
                        if context.service_workers:
                            raise ValueError("Service worker was created")
                        text = page.evaluate(
                            "() => (document.body?.innerText || '').slice(0, 10241)"
                        )
                        if type(text) is not str:
                            raise ValueError("Browser text is invalid")
                        truncated = len(text) > 10240
                        text = text[:10240]
                        page.screenshot(path=str(screenshot), type="png", timeout=5_000)
                        return {
                            "version": 1,
                            "text": text,
                            "truncated": truncated,
                            "blocked": {**counters, "proxy_hits": proxy.hits},
                        }
                    finally:
                        context.close()
                finally:
                    browser.close()
        finally:
            proxy.shutdown()
            server.join(timeout=1)


def main() -> None:
    try:
        raw = sys.stdin.buffer.readline(4097)
        if len(raw) > 4096:
            raise ValueError("Browser request is oversized")
        result = render(json.loads(raw))
    except Exception as exc:  # noqa: BLE001 - worker returns only the error type
        result = {"version": 1, "error": type(exc).__name__}
    while True:
        payload = json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n"
        if len(payload) <= 60_000:
            break
        text = result.get("text")
        if type(text) is not str or not text:
            raise ValueError("Browser result exceeds protocol limit")
        result["text"] = text[: len(text) // 2]
        result["truncated"] = True
    sys.stdout.buffer.write(payload.encode("ascii"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
