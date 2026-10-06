"""App-owned ComfyUI launcher; runs only in the isolated image environment.

The pinned ComfyUI server has no native API-key setting. Install the guard in
its aiohttp application itself before any route is served, including /ws.
"""

import builtins
import hmac
import os
import runpy
import sys
from pathlib import Path


def authorized(headers: object, secret: str) -> bool:
    origin = headers.get("Origin")  # type: ignore[attr-defined]
    fetch_site = headers.get("Sec-Fetch-Site")  # type: ignore[attr-defined]
    value = headers.get("Authorization", "")  # type: ignore[attr-defined]
    return (
        not origin and not fetch_site and hmac.compare_digest(value, f"Bearer {secret}")
    )


def main() -> None:
    secret = os.environ.pop("JARVIS_COMFY_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("Missing per-launch ComfyUI secret")
    root = Path(sys.argv[1]).resolve(strict=True)
    arguments = sys.argv[2:]
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.argv = [str(root / "main.py"), *arguments]

    import comfy.options  # type: ignore[import-not-found]

    comfy.options.enable_args_parsing()
    from aiohttp import web  # type: ignore[import-not-found]

    @web.middleware
    async def require_secret(request, handler):
        if not authorized(request.headers, secret):
            return web.Response(status=403)
        return await handler(request)

    original_import = builtins.__import__
    installed = False

    def import_with_guard(name, globals=None, locals=None, fromlist=(), level=0):
        nonlocal installed
        module = original_import(name, globals, locals, fromlist, level)
        if name == "server" and level == 0 and not installed:
            original_init = module.PromptServer.__init__

            def secured_init(self, *args, **kwargs):
                original_init(self, *args, **kwargs)
                self.app.middlewares.insert(0, require_secret)

            module.PromptServer.__init__ = secured_init
            installed = True
            builtins.__import__ = original_import
        return module

    builtins.__import__ = import_with_guard
    try:
        runpy.run_path(str(root / "main.py"), run_name="__main__")
    finally:
        builtins.__import__ = original_import


if __name__ == "__main__":
    main()
