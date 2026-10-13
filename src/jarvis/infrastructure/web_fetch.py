"""One exact, approved public-page GET with a pinned public destination."""

import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
import zlib
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import SplitResult, urlsplit

from jarvis.domain.pages import MAX_PAGE_TEXT_BYTES
from jarvis.domain.types import RequestFailure

_COMPRESSED_LIMIT = 256 * 1024
_DECODED_LIMIT = 512 * 1024
_TEXT_LIMIT = 10 * 1024
_DURATION = 10.0
_DNS_SLOTS = threading.BoundedSemaphore(2)
_HOST = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z",
    re.IGNORECASE,
)


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_reserved and not ip.is_multicast


def _parse(url: str) -> SplitResult:
    if len(url) > 2048 or any(ord(char) < 33 or ord(char) > 126 for char in url):
        raise RequestFailure("invalid_request", "URL contains controls or is too long")
    if "\\" in url:
        raise RequestFailure("invalid_request", "Backslashes are not allowed in URLs")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise RequestFailure("invalid_request", "Malformed URL") from exc
    if parsed.scheme not in {"http", "https"} or not host or not parsed.netloc:
        raise RequestFailure(
            "invalid_request", "Only absolute HTTP(S) URLs are allowed"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise RequestFailure("invalid_request", "URL credentials are forbidden")
    if parsed.fragment or "#" in url:
        raise RequestFailure("invalid_request", "URL fragments are unsupported")
    if port not in {None, 80 if parsed.scheme == "http" else 443}:
        raise RequestFailure(
            "invalid_request", "Only standard HTTP(S) ports are allowed"
        )
    if "%" in host or host.endswith("."):
        raise RequestFailure("invalid_request", "Ambiguous host is forbidden")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host):
            raise RequestFailure("invalid_request", "Invalid hostname") from None
    else:
        if not _public(str(ip)):
            raise RequestFailure("invalid_request", "Destination is not public")
    if parsed.path.startswith("//"):
        raise RequestFailure("invalid_request", "Ambiguous path is forbidden")
    return parsed


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript", "template", "head"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript", "template", "head"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


class _Article(HTMLParser):
    """Keep author-defined main/article content and structural text boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []
        self.parts: list[str] = []
        self.main: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        math_script = tag == "script" and str(attributes.get("type", "")).split(";")[
            0
        ].strip().lower() in {"math/tex", "application/x-tex"}
        hidden = (
            (self.stack[-1][1] if self.stack else False)
            or (
                tag in {"script", "style", "head", "noscript", "svg", "template", "nav"}
                and not math_script
            )
            or "hidden" in dict(attrs)
        )
        main = (self.stack[-1][2] if self.stack else False) or tag in {
            "main",
            "article",
        }
        if tag not in {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}:
            self.stack.append((tag, hidden, main))
        if not hidden:
            prefix = (
                "\n\n```\n\n"
                if tag == "pre"
                else "\n\n" + "#" * int(tag[1]) + " "
                if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
                else "\n\n"
                if tag
                in {
                    "p",
                    "div",
                    "section",
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                    "tr",
                    "br",
                }
                else "\t"
                if tag in {"td", "th"}
                else "\n- "
                if tag == "li"
                else ""
            )
            self.parts.append(prefix)
            if main:
                self.main.append(prefix)

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre" and self.stack and not self.stack[-1][1]:
            self.parts.append("\n\n```\n\n")
        if tag in {
            "p",
            "div",
            "section",
            "pre",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self.parts.append("\n\n")
            if self.stack and self.stack[-1][2]:
                self.main.append("\n\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        hidden = self.stack[-1][1] if self.stack else False
        main = self.stack[-1][2] if self.stack else False
        if not hidden:
            self.parts.append(data)
            if main:
                self.main.append(data)

    def text(self) -> str:
        # Retain visible notes/appendices outside <main>, too. Selecting only
        # <article> silently loses substantive source material on many sites.
        source = "".join(self.parts)
        return "\n\n".join(
            " ".join(part.split()) for part in source.split("\n\n") if part.strip()
        )


class _PinnedConnection:
    """Mixin that connects to the vetted numeric address, retaining host for TLS."""

    pinned_address: str

    def connect(self) -> None:
        self.sock = socket.create_connection(  # type: ignore[attr-defined]
            (self.pinned_address, self.port),  # type: ignore[attr-defined]
            self.timeout,  # type: ignore[attr-defined]
        )
        try:
            peer = self.sock.getpeername()[0]
            if ipaddress.ip_address(peer) != ipaddress.ip_address(self.pinned_address):
                raise RequestFailure("invalid_request", "Connected destination changed")
            if isinstance(self, http.client.HTTPSConnection):
                tls_socket = self._context.wrap_socket(  # type: ignore[attr-defined]
                    self.sock,
                    server_hostname=self.host,  # type: ignore[attr-defined]
                    do_handshake_on_connect=False,
                )
                self.sock = tls_socket
                tls_socket.do_handshake()
        except BaseException:
            self.sock.close()
            raise


class _HTTP(_PinnedConnection, http.client.HTTPConnection):
    pass


class _HTTPS(_PinnedConnection, http.client.HTTPSConnection):
    pass


class PublicPageFetcher:
    def __init__(
        self, resolver=None, connection_factory=None, *, full_text: bool = False
    ) -> None:
        self.resolver = resolver or socket.getaddrinfo
        self.connection_factory = connection_factory
        self.full_text = full_text

    def inspect(self, name: str, arguments: dict[str, object]) -> str:
        if name != "fetch_public_page" or type(arguments.get("url")) is not str:
            raise RequestFailure("invalid_request", "Invalid fetch request")
        url = arguments["url"]
        assert isinstance(url, str)
        _parse(url)
        return "GET " + url

    def _resolve(
        self, host: str, port: int, deadline: float, cancel: threading.Event
    ) -> list[str]:
        if not _DNS_SLOTS.acquire(blocking=False):
            raise RequestFailure("unavailable", "DNS resolver is busy")
        results: queue.Queue[object] = queue.Queue(maxsize=1)

        def lookup() -> None:
            try:
                records = self.resolver(host, port, type=socket.SOCK_STREAM)
                results.put_nowait([str(item[4][0]) for item in records])
            except Exception as exc:  # noqa: BLE001 - forwarded across DNS thread
                results.put_nowait(exc)
            finally:
                _DNS_SLOTS.release()

        try:
            threading.Thread(target=lookup, name="jarvis-web-dns", daemon=True).start()
        except RuntimeError:
            _DNS_SLOTS.release()
            raise
        dns_deadline = min(deadline, time.monotonic() + 3.0)
        while True:
            if cancel.is_set():
                raise RequestFailure("cancelled", "Page retrieval cancelled")
            remaining = dns_deadline - time.monotonic()
            if remaining <= 0:
                raise RequestFailure("timeout", "DNS resolution timed out")
            try:
                result = results.get(timeout=min(remaining, 0.05))
                break
            except queue.Empty:
                continue
        if isinstance(result, Exception):
            raise RequestFailure("unavailable", "DNS resolution failed") from result
        assert isinstance(result, list)
        return result

    def download(self, url: str) -> tuple[bytes, str, int]:
        return self.download_cancellable(url, threading.Event())

    def download_cancellable(
        self, url: str, cancel: threading.Event
    ) -> tuple[bytes, str, int]:
        parsed = _parse(url)
        host = parsed.hostname
        assert host is not None
        port = 443 if parsed.scheme == "https" else 80
        deadline = time.monotonic() + _DURATION
        addresses = self._resolve(host, port, deadline, cancel)
        if not addresses or any(not _public(address) for address in addresses):
            raise RequestFailure(
                "invalid_request", "DNS did not resolve exclusively to public addresses"
            )
        address = addresses[0]
        remaining = deadline - time.monotonic()

        def check_deadline() -> None:
            if cancel.is_set():
                raise RequestFailure("cancelled", "Page retrieval cancelled")
            if time.monotonic() >= deadline:
                raise RequestFailure("timeout", "Fetch timed out")

        check_deadline()
        if self.connection_factory is not None:
            conn = self.connection_factory(
                parsed.scheme, host, port, address, remaining
            )
        else:
            cls = _HTTPS if parsed.scheme == "https" else _HTTP
            conn = cls(host, port, timeout=min(remaining, 3.0))
            conn.pinned_address = address
        done = threading.Event()
        sockets: list[socket.socket] = []

        def interrupt() -> None:
            while not done.wait(0.01):
                current_socket = conn.sock
                if current_socket is not None and current_socket not in sockets:
                    sockets.append(current_socket)
                if cancel.is_set() or time.monotonic() >= deadline:
                    for active_socket in sockets:
                        try:
                            active_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

        watchdog = threading.Thread(
            target=interrupt, name="jarvis-web-deadline", daemon=True
        )
        watchdog.start()
        response = None
        try:
            target = (parsed.path or "/") + ("?" + parsed.query if "?" in url else "")
            conn.request(
                "GET",
                target,
                headers={
                    "Host": parsed.netloc,
                    "Accept": "text/html, text/plain",
                    "Accept-Encoding": "identity",
                    "User-Agent": "JarvisPageFetch/1",
                    "Connection": "close",
                },
            )
            if conn.sock is not None and conn.sock not in sockets:
                sockets.append(conn.sock)
            check_deadline()
            response = conn.getresponse()
            check_deadline()
            if 300 <= response.status < 400:
                raise RequestFailure(
                    "invalid_request", "Redirect needs a new approved URL"
                )
            if response.status != 200:
                raise RequestFailure(
                    "unavailable", "Page returned a non-success status"
                )
            content_type = (
                response.getheader("Content-Type", "").split(";", 1)[0].lower().strip()
            )
            if content_type not in {"text/html", "text/plain"}:
                raise RequestFailure(
                    "invalid_request", "Only HTML or plain text is supported"
                )
            encoding = (
                response.getheader("Content-Encoding", "identity").lower().strip()
            )
            if encoding not in {"identity", "gzip"}:
                raise RequestFailure("invalid_request", "Unsupported content encoding")
            declared = response.getheader("Content-Length")
            if declared and declared.isdecimal() and int(declared) > _COMPRESSED_LIMIT:
                raise RequestFailure(
                    "page_too_large" if self.full_text else "invalid_request",
                    "Page exceeds byte limit",
                )
            decompressor = (
                zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
            )
            chunks: list[bytes] = []
            compressed = decoded = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestFailure("timeout", "Fetch timed out")
                if conn.sock is not None:
                    conn.sock.settimeout(min(remaining, 2.0))
                chunk = response.read(min(8192, _COMPRESSED_LIMIT - compressed + 1))
                check_deadline()
                if not chunk:
                    break
                compressed += len(chunk)
                if compressed > _COMPRESSED_LIMIT:
                    raise RequestFailure(
                        "page_too_large" if self.full_text else "invalid_request",
                        "Page exceeds byte limit",
                    )
                data = (
                    decompressor.decompress(chunk, _DECODED_LIMIT - decoded + 1)
                    if decompressor
                    else chunk
                )
                decoded += len(data)
                if decoded > _DECODED_LIMIT or (
                    decompressor and decompressor.unconsumed_tail
                ):
                    raise RequestFailure(
                        "page_too_large" if self.full_text else "invalid_request",
                        "Decompressed page exceeds byte limit",
                    )
                chunks.append(data)
            if decompressor and not decompressor.eof:
                raise RequestFailure("invalid_request", "Incomplete compressed page")
            raw = b"".join(chunks)
            if b"\x00" in raw:
                raise RequestFailure(
                    "invalid_request", "Binary response is unsupported"
                )
            return raw, content_type, response.status
        except TimeoutError as exc:
            check_deadline()
            raise RequestFailure("timeout", "Page retrieval timed out") from exc
        except (OSError, ssl.SSLError, http.client.HTTPException, zlib.error) as exc:
            check_deadline()
            raise RequestFailure("unavailable", "Page retrieval failed") from exc
        finally:
            done.set()
            watchdog.join()
            if response is not None:
                response.close()
            conn.close()

    def execute(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return self.execute_cancellable(name, arguments, threading.Event())

    def execute_cancellable(
        self, name: str, arguments: dict[str, object], cancel: threading.Event
    ) -> dict[str, object]:
        self.inspect(name, arguments)
        url = arguments["url"]
        assert isinstance(url, str)
        raw, content_type, status = self.download_cancellable(url, cancel)
        text = raw.decode("utf-8", errors="replace")
        if content_type == "text/html":
            if self.full_text:
                article = _Article()
                article.feed(text)
                text = article.text()
            else:
                parser = _Text()
                parser.feed(text)
                text = " ".join("".join(parser.parts).split())
        encoded = text.encode("utf-8")
        if self.full_text and len(encoded) > MAX_PAGE_TEXT_BYTES:
            raise RequestFailure(
                "page_too_large", "Extracted page exceeds snapshot limit"
            )
        truncated = len(encoded) > _TEXT_LIMIT
        if truncated and not self.full_text:
            text = encoded[:_TEXT_LIMIT].decode("utf-8", errors="ignore")
        if self.full_text:
            truncated = False
        return {
            "url": url,
            "status": status,
            "content_type": content_type,
            "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "text": text,
            "truncated": truncated,
            "trust": "untrusted",
        }
