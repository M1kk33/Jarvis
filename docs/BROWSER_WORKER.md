# M14 browser worker

M14 renders one approved public HTML URL at a time. The model proposes only the URL; the local approval card shows its full path and query before the GET. The M13 fetcher resolves and pins a public IP, checks the connected peer, uses TLS hostname verification, and rejects redirects. The browser receives only the bounded fetched HTML, never the approved URL or conversation data.

Install the optional, pinned browser dependency and its headless shell:

```powershell
uv sync --python 3.12 --extra browser
uv run --extra browser python -m playwright install --only-shell chromium
```

Then add `--enable-network --enable-browser` before `web` in the model-backed command. Ordinary chat does not load Playwright or launch Chromium. The package is pinned to Playwright 1.63.0 and Chrome Headless Shell 153.0.8010.12 (Playwright revision 1243); the executable SHA-256 is `addfa79abb060e1e514e155ed745d4bf96140bca402735958bb4e223aea0b98c`. Jarvis checks this file before enabling the capability and again before each render.

The worker uses a fresh temporary browser profile and one tab, never a personal Chrome or Edge profile. It is born in a no-breakaway Windows Job Object with a 1.5 GiB job memory limit and verified tree cleanup. Admission also requires 1.5 GiB browser headroom plus the existing 3 GiB free-RAM reserve; browser rendering excludes concurrent heavy model requests. Cancellation and timeouts stop and verify the owned tree before freeing its slot. An unverified exit leaves the browser capability unavailable.

The browser starts at `about:blank` with the fetched HTML. Inline JavaScript can update the DOM, but external assets and page-initiated network requests are outside this exact-URL grant. A restrictive document CSP, offline browser context, blocked service workers, abort-all request route, disabled network APIs, disabled QUIC/non-proxied WebRTC UDP, failed DNS resolution for browser-owned destinations, and an app-owned deny proxy provide independent barriers. No browser request is continued or forwarded. Popups and downloads are discarded. The checked M13 fetcher is the only component that contacts the approved site. Local tests exercise HTTP, WebSocket, UDP/STUN, redirects, private destinations, navigation races, popups, downloads, and cancellation.

The result contains up to 9 KiB of rendered text, a source URL, a UTC retrieval time, an untrusted label, and a durable 1024×768 PNG screenshot. The screenshot is stored as a private artifact and included in verified backups. A site that needs external scripts, styles, API calls, cookies, or a service worker may render incompletely; Jarvis does not silently broaden the grant. To view such a site safely would require a separately approved request scope and a demonstrated network boundary.
