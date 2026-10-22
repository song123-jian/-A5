from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import printer_engine as engine
import tkinter as tk
from _version import VERSION
from tray import SystemTray


class ReleaseAndRuntimeTests(unittest.TestCase):
    def test_version_comes_from_single_source(self) -> None:
        self.assertEqual(app.APP_VERSION, VERSION)
        self.assertRegex(app.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_atomic_json_writer_writes_complete_utf8_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="a5-json-") as directory:
            path = Path(directory) / "settings.json"
            payload = {"paper_size": "A4", "说明": "中文"}
            app._write_json(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertFalse(list(path.parent.glob(".settings.json.*.tmp")))

    def test_print_controller_cancellation_is_observable(self) -> None:
        controller = engine.PrintController()
        with controller:
            controller.cancel()
            with self.assertRaisesRegex(RuntimeError, "已取消"):
                controller.check()

    def test_tray_icon_asset_is_stable_without_starting_native_tray(self) -> None:
        tray = SystemTray("测试", lambda: None, lambda: None, lambda _error: None)
        self.assertEqual(tray._make_image().size, (64, 64))

    def test_default_printer_is_selected_from_system_at_runtime(self) -> None:
        self.assertEqual(engine.DEFAULT_PRINTER, "")
        self.assertTrue(callable(engine.get_default_printer))

    def test_tkdnd_cleanup_preserves_other_bindings_and_unregisters_target(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.calls: list[tuple] = []
                self.tk = self
                self._w = "."

            def dnd_bind(self, sequence) -> str:
                return "dnd-func-id %D\nother-callback %D"

            def call(self, *args):
                if args[0] == "info":
                    return ("dnd-func-id",)
                self.calls.append(args)

            def deletecommand(self, callback) -> None:
                self.calls.append(("delete", callback))

            def drop_target_unregister(self) -> None:
                self.calls.append(("unregister",))

        instance = object.__new__(app.TicketPrinterApp)
        instance.root = FakeRoot()
        instance._drop_dnd_enabled = True
        instance._drop_target_registered = True
        instance._drop_bind_id = "dnd-func-id"
        messages: list[str] = []
        instance.log = messages.append

        instance._disable_file_drop()

        self.assertEqual(
            instance.root.calls,
            [("bind", ".", "<<Drop>>", "other-callback %D"),
             ("delete", "dnd-func-id"), ("unregister",)],
        )
        self.assertFalse(instance._drop_dnd_enabled)
        self.assertFalse(instance._drop_target_registered)
        self.assertIsNone(instance._drop_bind_id)
        self.assertEqual(messages, [])

    def test_tkdnd_cleanup_continues_when_binding_is_already_gone(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def dnd_bind(self, *args) -> None:
                self.calls.append(("query", *args))
                raise tk.TclError("Tk interpreter already closed")

            def drop_target_unregister(self) -> None:
                self.calls.append(("unregister",))

        instance = object.__new__(app.TicketPrinterApp)
        instance.root = FakeRoot()
        instance._drop_dnd_enabled = True
        instance._drop_target_registered = True
        instance._drop_bind_id = "stale-func-id"
        messages: list[str] = []
        instance.log = messages.append

        instance._disable_file_drop()

        self.assertEqual(
            instance.root.calls,
            [("query", "<<Drop>>"), ("unregister",)],
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("事件解绑失败", messages[0])
        self.assertFalse(instance._drop_dnd_enabled)
        self.assertFalse(instance._drop_target_registered)
        self.assertIsNone(instance._drop_bind_id)

    def test_tkdnd_initialization_rolls_back_partial_registration(self) -> None:
        class FakeRoot:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def drop_target_register(self, *_args) -> None:
                self.calls.append(("register",))

            def dnd_bind(self, *_args) -> None:
                self.calls.append(("bind",))
                raise RuntimeError("binding unavailable")

            def drop_target_unregister(self) -> None:
                self.calls.append(("unregister",))

        instance = object.__new__(app.TicketPrinterApp)
        instance.root = FakeRoot()
        messages: list[str] = []
        instance.log = messages.append

        with patch.object(app, "DND_FILES", "DND_Files"):
            instance._enable_file_drop()

        self.assertEqual(
            instance.root.calls,
            [("register",), ("bind",), ("unregister",)],
        )
        self.assertFalse(instance._drop_dnd_enabled)
        self.assertFalse(instance._drop_target_registered)
        self.assertIsNone(instance._drop_bind_id)
        self.assertTrue(any("初始化失败" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
