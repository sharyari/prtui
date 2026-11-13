"""Launch prtui inside a native desktop window via textual-serve + pywebview."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path


_HOST = "127.0.0.1"
_WORKER_ENV = "_PRTUI_WINDOW_WORKER"
_GEOMETRY_PATH = Path(__file__).resolve().parent.parent / ".window-state.json"
_RELATIVE_RENDERERS = {"wkwebview", "gtkwebkit2", "qtwebengine", "qtwebkit"}


def _screen_bounds(screen) -> list[int]:
    return [screen.x, screen.y, screen.width, screen.height]


def _load_window_geometry(screens, renderer: str) -> dict:
    """Restore screen-relative geometry, keeping the window on an available screen."""
    try:
        state = json.loads(_GEOMETRY_PATH.read_text())
        values = [state[key] for key in ("x", "y", "width", "height")]
        bounds = state["screen"]
        if (not isinstance(bounds, list) or len(bounds) != 4
                or any(type(value) is not int for value in [*values, *bounds])
                or min(state["width"], state["height"], *bounds[2:]) <= 0):
            return {"maximized": True}
    except (OSError, ValueError, KeyError, TypeError):
        return {"maximized": True}

    if not screens:
        return {"maximized": True}
    screen = next((s for s in screens if _screen_bounds(s) == bounds), None)
    if screen is None:
        # A display may have moved in the desktop layout. Prefer a unique size
        # match before falling back to the primary screen.
        matches = [s for s in screens if [s.width, s.height] == bounds[2:]]
        screen = matches[0] if len(matches) == 1 else screens[0]
    width = min(max(200, state["width"]), screen.width)
    height = min(max(100, state["height"]), screen.height)
    x = min(max(0, state["x"]), screen.width - width)
    y = min(max(0, state["y"]), screen.height - height)
    if renderer not in _RELATIVE_RENDERERS:
        x += screen.x
        y += screen.y
    return dict(screen=screen, x=x, y=y, width=width, height=height, maximized=False)


def _window_geometry(window, screens, renderer: str) -> dict:
    """Capture position relative to the current screen's top-left corner."""
    if renderer == "wkwebview":
        # Cocoa's public x/y getters use the launch screen even after a move.
        # Match the native frame against screen bounds in Cocoa coordinates.
        frame = window.native.frame()
        x, y = int(frame.origin.x), int(frame.origin.y)
        width, height = int(frame.size.width), int(frame.size.height)
    else:
        x, y = int(window.x), int(window.y)
        width, height = int(window.width), int(window.height)

    def overlap(screen):
        return (max(0, min(x + width, screen.x + screen.width) - max(x, screen.x))
                * max(0, min(y + height, screen.y + screen.height) - max(y, screen.y)))

    screen = max(screens, key=overlap)
    bounds = _screen_bounds(screen)
    x -= screen.x
    # Cocoa uses a bottom-left origin; create_window expects a top-left offset.
    y = screen.y + screen.height - y - height if renderer == "wkwebview" else y - screen.y
    return dict(x=x, y=y, width=width, height=height, screen=bounds)


def _remember_window_geometry(window, webview) -> None:
    """Save while the native window exists, including when the TUI quits it."""
    lock = threading.Lock()

    def remember(*_):
        # pywebview dispatches these callbacks on separate threads.
        with lock:
            try:
                state = _window_geometry(window, webview.screens, webview.renderer)
                if state["width"] > 0 and state["height"] > 0:
                    _GEOMETRY_PATH.write_text(json.dumps(state) + "\n")
            except (OSError, AttributeError, TypeError, ValueError):
                # Saving must not prevent launch/exit; late events may arrive
                # after the native window has already been destroyed.
                pass

    window.events.shown += remember
    window.events.moved += remember
    window.events.resized += remember


def run_windowed() -> None:
    """Open prtui in a detached native window, restoring its last geometry.

    First call respawns the script as a new session and returns so the shell
    regains its prompt. The respawned worker (``_WORKER_ENV=1``) opens the
    window and blocks until the TUI exits.
    """
    if os.environ.get(_WORKER_ENV) != "1":
        missing = []
        try:
            import webview  # noqa: F401
        except ImportError:
            missing.append("pywebview")
        try:
            import textual_serve  # noqa: F401
        except ImportError:
            missing.append("textual-serve")
        if missing:
            print(
                f"Error: missing package(s): {', '.join(missing)}\n"
                f"Install with: pip install textual-serve pywebview",
                file=sys.stderr,
            )
            return
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

    # screens is a lazy proxy: materialize it to initialize the GUI backend
    # before reading renderer (otherwise it is still None on first launch).
    screens = list(webview.screens)
    geometry = _load_window_geometry(screens, webview.renderer)
    window = webview.create_window("prtui", f"http://{_HOST}:{port}", **geometry)
    _remember_window_geometry(window, webview)

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
