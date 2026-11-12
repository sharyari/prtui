"""Window geometry tests without starting a GUI or contacting GitHub."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py"))
import window


def screen(x=0, y=0, width=1920, height=1080):
    return SimpleNamespace(x=x, y=y, width=width, height=height)


def frame(x, y, width, height):
    return SimpleNamespace(origin=SimpleNamespace(x=x, y=y),
                           size=SimpleNamespace(width=width, height=height))


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in self.handlers:
            handler(*args)


class WindowGeometryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        patcher = patch.object(window, "_GEOMETRY_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.primary = screen()
        self.secondary = screen(-2560, 300, 2560, 1440)

    def save(self, **overrides):
        state = dict(x=100, y=150, width=1200, height=800,
                     screen=window._screen_bounds(self.secondary))
        state.update(overrides)
        self.path.write_text(json.dumps(state))

    def test_first_launch_is_maximized(self):
        self.assertEqual(window._load_window_geometry([self.primary], "wkwebview"),
                         {"maximized": True})

    def test_invalid_state_falls_back_to_first_launch(self):
        for state in ("broken json", "null", "[]", "{}",
                      '{"x": 0, "y": 0, "width": -1, "height": 800, "screen": [0, 0, 1920, 1080]}',
                      '{"x": true, "y": 0, "width": 800, "height": 600, "screen": [0, 0, 1920, 1080]}',
                      '{"x": 0, "y": 0, "width": 800, "height": 600, "screen": "bad"}'):
            with self.subTest(state=state):
                self.path.write_text(state)
                self.assertEqual(window._load_window_geometry([self.primary], "wkwebview"),
                                 {"maximized": True})

    def test_restore_secondary_screen_with_relative_coordinates(self):
        self.save()
        screens = [self.primary, self.secondary]
        for renderer in window._RELATIVE_RENDERERS:
            with self.subTest(renderer=renderer):
                options = window._load_window_geometry(screens, renderer)
                self.assertIs(options["screen"], self.secondary)
                self.assertEqual((options["x"], options["y"]), (100, 150))
                self.assertEqual((options["width"], options["height"]), (1200, 800))
                self.assertFalse(options["maximized"])

    def test_restore_windows_with_absolute_coordinates(self):
        self.save()
        options = window._load_window_geometry([self.primary, self.secondary], "edgechromium")
        self.assertEqual((options["x"], options["y"]), (-2460, 450))

    def test_disconnected_screen_fits_window_onto_primary(self):
        self.save(x=2300, y=1200, width=2400, height=1400)
        options = window._load_window_geometry([self.primary], "wkwebview")
        self.assertIs(options["screen"], self.primary)
        self.assertEqual((options["x"], options["y"]), (0, 0))
        self.assertEqual((options["width"], options["height"]), (1920, 1080))

    def test_rearranged_screen_matches_unique_size(self):
        self.save()
        moved = screen(1920, -1440, 2560, 1440)
        options = window._load_window_geometry([self.primary, moved], "wkwebview")
        self.assertIs(options["screen"], moved)

    def test_ambiguous_screen_size_falls_back_to_primary(self):
        self.save()
        options = window._load_window_geometry(
            [self.primary, screen(1920, 0, 2560, 1440), screen(4480, 0, 2560, 1440)],
            "wkwebview")
        self.assertIs(options["screen"], self.primary)

    def test_clamps_negative_offsets_and_small_sizes(self):
        self.save(x=-10, y=-20, width=10, height=20)
        options = window._load_window_geometry([self.secondary], "wkwebview")
        self.assertEqual((options["x"], options["y"], options["width"], options["height"]),
                         (0, 0, 200, 100))

    def test_cocoa_capture_after_moving_between_screens(self):
        # Public x/y and native screen() intentionally point to the old screen.
        # Native frame coordinates also need a vertical flip.
        native = SimpleNamespace(
            frame=lambda: frame(-2460, 790, 1200, 800),
            screen=lambda: SimpleNamespace(frame=lambda: frame(0, 0, 1920, 1080)))
        state = window._window_geometry(SimpleNamespace(native=native, x=1, y=2),
                                        [self.primary, self.secondary], "wkwebview")
        self.assertEqual(state, dict(x=100, y=150, width=1200, height=800,
                                     screen=[-2560, 300, 2560, 1440]))
        self.path.write_text(json.dumps(state))
        options = window._load_window_geometry([self.primary, self.secondary], "wkwebview")
        self.assertIs(options["screen"], self.secondary)
        self.assertEqual((options["x"], options["y"]), (100, 150))

    def test_capture_chooses_screen_with_largest_overlap(self):
        desktop_window = SimpleNamespace(x=-1100, y=400, width=1200, height=600)
        state = window._window_geometry(desktop_window, [self.primary, self.secondary], "qtwebengine")
        self.assertEqual(state, dict(x=1460, y=100, width=1200, height=600,
                                     screen=[-2560, 300, 2560, 1440]))

    def test_geometry_events_persist_before_window_is_destroyed(self):
        events = SimpleNamespace(shown=Event(), moved=Event(), resized=Event())
        desktop_window = SimpleNamespace(events=events, x=100, y=150, width=1200, height=800)
        webview = SimpleNamespace(screens=[self.primary], renderer="qtwebengine")
        window._remember_window_geometry(desktop_window, webview)
        events.shown.fire()
        self.assertEqual(json.loads(self.path.read_text())["width"], 1200)
        desktop_window.x = 200
        events.moved.fire(200, 150)
        self.assertEqual(json.loads(self.path.read_text())["x"], 200)
        desktop_window.width = 1400
        events.resized.fire(1400, 800)
        saved = self.path.read_text()
        self.assertEqual(json.loads(saved)["width"], 1400)
        desktop_window.width = None  # A queued event after window.destroy().
        events.resized.fire(1400, 800)
        self.assertEqual(self.path.read_text(), saved)

    def test_unwritable_state_does_not_interrupt_events(self):
        desktop_window = SimpleNamespace(
            events=SimpleNamespace(shown=Event(), moved=Event(), resized=Event()),
            x=100, y=150, width=1200, height=800)
        webview = SimpleNamespace(screens=[self.primary], renderer="qtwebengine")
        window._remember_window_geometry(desktop_window, webview)
        with patch.object(Path, "write_text", side_effect=OSError("read-only")):
            desktop_window.events.shown.fire()


if __name__ == "__main__":
    unittest.main()
