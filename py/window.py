"""Launch prtui inside a native desktop window via textual-serve + pywebview."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path


_HOST = "127.0.0.1"
_WORKER_ENV = "_PRTUI_WINDOW_WORKER"


def run_windowed() -> None:
    """Open prtui in a detached, maximized native window.

    First call respawns the script as a new session and returns so the shell
    regains its prompt. The respawned worker (``_WORKER_ENV=1``) opens the
    window and blocks until the TUI exits.
    """
    if os.environ.get(_WORKER_ENV) != "1":
        subprocess.Popen(
            [sys.executable, *sys.argv],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
            env={**os.environ, _WORKER_ENV: "1"},
        )
        return

    import webview
    from aiohttp import web
    from textual_serve.app_service import AppService
    from textual_serve.server import Server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_HOST, 0))
        port = s.getsockname()[1]
    script = Path(__file__).with_name("prtui.py")
    server = Server(f'"{sys.executable}" "{script}"', host=_HOST, port=port, title="prtui")

    ready = threading.Event()
    error: list[BaseException] = []

    def serve() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            runner = web.AppRunner(loop.run_until_complete(server._make_app()), handle_signals=False)
            loop.run_until_complete(runner.setup())
            loop.run_until_complete(web.TCPSite(runner, _HOST, port).start())
            ready.set()
            loop.run_forever()
        except BaseException as exc:
            error.append(exc)
            ready.set()

    threading.Thread(target=serve, name="textual-serve", daemon=True).start()
    if not ready.wait(timeout=10.0):
        raise RuntimeError("textual-serve did not start within 10s")
    if error:
        raise error[0]

    window = webview.create_window("prtui", f"http://{_HOST}:{port}", maximized=True)

    original_stop = AppService.stop

    async def stop_and_close(self: AppService) -> None:
        try:
            await original_stop(self)
        finally:
            try:
                window.destroy()
            except Exception:
                pass

    AppService.stop = stop_and_close
    webview.start()
