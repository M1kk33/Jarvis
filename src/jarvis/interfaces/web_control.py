"""UI-independent lifecycle signals for an application-owned web server."""

import asyncio
import socket
import threading
from queue import Queue
from typing import Protocol


class WebServer(Protocol):
    started: bool
    should_exit: bool

    async def serve(self, sockets: list[socket.socket] | None = None) -> None: ...


class WebControl:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.events: Queue[tuple[str, str]] = Queue()

    def error(self, message: str) -> None:
        self.events.put(("error", message))


async def serve_controlled(
    server: WebServer,
    control: WebControl,
    setup_url: str,
    sockets: list[socket.socket] | None = None,
) -> None:
    """Announce only after bind; cancellation does not bypass server cleanup."""

    async def monitor() -> None:
        announced = False
        while True:
            if control.stop.is_set():
                server.should_exit = True
                return
            if server.started and not announced:
                control.events.put(("ready", setup_url))
                announced = True
            await asyncio.sleep(0.25)

    watcher = asyncio.create_task(monitor())
    try:
        if sockets is None:
            await server.serve()
        else:
            await server.serve(sockets=sockets)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
