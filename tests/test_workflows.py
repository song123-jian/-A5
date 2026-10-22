from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from PIL import Image
from pypdf import PdfReader, PdfWriter

import app
import printer_engine as engine


class TkWorkflowTests(unittest.TestCase):
    """Exercise real Tk callbacks with isolated files and a blocked print backend."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="a5-workflows-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.inputs = self.directory / "inputs"
        self.inputs.mkdir()
        self.single = self._pdf("single.pdf", [(340, 227)])
        self.multi = self._pdf("multi.pdf", [(420, 595), (595, 420), (420, 595)])
        self.image = self.inputs / "sample.png"
        Image.new("RGB", (240, 320), "white").save(self.image)
        self.stamp = self.directory / "stamp.png"
        Image.new("RGBA", (100, 100), (220, 30, 60, 180)).save(self.stamp)
        self.archive = self.inputs / "bundle.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.write(self.single, "nested/member.pdf")
            archive.writestr("readme.txt", "not printable")

        for name, value in (
            ("DATA_DIR", self.directory / "state"),
            ("SETTINGS_PATH", self.directory / "state" / "settings.json"),
            ("PRINT_HISTORY_PATH", self.directory / "state" / "print_history.json"),
            ("LOG_PATH", self.directory / "state" / "app.log"),
        ):
            self._patch(app, name, value)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.directory)})
        environment.start()
        self.addCleanup(environment.stop)
        self._patch(engine, "list_printers", return_value=[{"Name": "Test printer"}])
        self._patch(engine, "get_default_printer", return_value="Test printer")
        self._patch(app.SystemTray, "start", return_value=False)
        self.warning = self._patch(app.messagebox, "showwarning")
        self.error = self._patch(app.messagebox, "showerror")
        self.info = self._patch(app.messagebox, "showinfo")
        self.confirm = self._patch(app.messagebox, "askyesno", return_value=False)
        self.print_backend = self._patch(
            engine, "print_scan", side_effect=AssertionError("Real printing is forbidden")
        )
        self.calibration_backend = self._patch(
            engine, "print_calibration_page",
            side_effect=AssertionError("Real calibration printing is forbidden"),
        )
        self.validate_backend = self._patch(engine, "validate_print_scan")
        self.root = app.TkinterDnD.Tk() if app.TkinterDnD is not None else tk.Tk()
        self.callback_errors: list[str] = []
        self.root.report_callback_exception = lambda _kind, error, _trace: self.callback_errors.append(str(error))
        self.instance = app.TicketPrinterApp(self.root)
        self.root.withdraw()
        self.root.update()
        self.addCleanup(self._close)

    def _patch(self, target, attribute: str, *args, **kwargs):
        replacement = patch.object(target, attribute, *args, **kwargs)
        result = replacement.start()
        self.addCleanup(replacement.stop)
        return result

    def _pdf(self, name: str, dimensions: list[tuple[int, int]]) -> Path:
        output = self.inputs / name
        writer = PdfWriter()
        for width, height in dimensions:
            writer.add_blank_page(width=width, height=height)
        with output.open("wb") as handle:
            writer.write(handle)
        return output

    def _close(self) -> None:
        if self.root is None:
            return
        self.confirm.return_value = True
        self.instance.on_close()
        deadline = time.monotonic() + 5
        while self.instance.worker_thread and self.instance.worker_thread.is_alive():
            if time.monotonic() >= deadline:
                raise AssertionError("Worker did not stop during cleanup")
            self.root.update()
            time.sleep(0.01)
        try:
            self.root.update()
            if self.root.winfo_exists():
                self.instance._finish_close()
        except tk.TclError:
            pass
        self.root = None

    def _wait(self) -> None:
        deadline = time.monotonic() + 15
        while self.instance.busy:
            self.root.update()
            if self.callback_errors:
                self.fail(f"Unhandled Tk callback: {self.callback_errors}")
            if time.monotonic() >= deadline:
                self.fail(f"Worker timed out: {self.instance.status_var.get()}")
            time.sleep(0.01)
        self.root.update()
        self.assertEqual(self.callback_errors, [])

    def _import(self, *paths: Path) -> None:
        with patch.object(app.filedialog, "askopenfilenames", return_value=tuple(map(str, paths))):
            self.instance.import_button.invoke()
        self._wait()

    def _select(self, path: Path) -> None:
        item = next(item for item, value in self.instance.tree_record_paths.items() if value == str(path))
        self.instance.tree.selection_set(item)
        self.root.update()

    @staticmethod
    def _print_success(scan: dict, **_kwargs) -> dict:
        count = engine.expected_print_jobs(scan)
        return {"success": True, "expected_jobs": count, "submitted_jobs": count,
                "jobs_observed_in_queue": count, "queue_remaining_with_prefix": 0}

    def _select_latest_history(self) -> None:
        self.instance.on_print_history()
        self.root.update()
        tree = self.instance.history_tree
        tree.selection_set(tree.get_children()[0])

    def test_empty_state_and_cancelled_import_submit_nothing(self) -> None:
        for button in (self.instance.print_button, self.instance.selected_print_button,
                       self.instance.copy_button, self.instance.preview_button,
                       self.instance.export_pdf_button, self.instance.validate_button):
            self.assertTrue(button.instate(["disabled"]))
        with patch.object(app.filedialog, "askopenfilenames", return_value=()):
            self.instance.import_button.invoke()
        self.assertEqual(self.instance.tree.get_children(), ())
        self.assertFalse(self.instance.busy)
        self.print_backend.assert_not_called()

    def test_import_expands_zip_and_image_into_visible_records(self) -> None:
        self._import(self.single, self.multi, self.image, self.archive)
        scan = self.instance._active_scan_result()
        self.assertEqual(scan["pdf_count"], 3)
        self.assertEqual(scan["image_count"], 1)
        self.assertEqual(scan["zip_internal_count"], 1)
        names = [self.instance.tree.item(item, "text") for item in self.instance.tree.get_children()]
        self.assertNotIn("bundle.zip / nested/member.pdf", names)
        self.assertIn("single.pdf", names)
        self.assertEqual(scan["duplicate_skipped_count"], 1)
        self.assertIn("跳过重复 1", self.instance.summary_var.get())
        self.assertIn("sample.png", names)
        self.assertEqual(self.instance.preset_var.get(), "智能")
        self.assertFalse(self.instance.print_button.instate(["disabled"]))
        self.error.assert_not_called()

    def test_import_shows_first_detail_before_batch_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()
        read_pdf = engine.PdfReader

        def read_with_pause(path, *args, **kwargs):
            if Path(path) == self.single:
                started.set()
                if not release.wait(5):
                    raise TimeoutError("Second file was not released")
            return read_pdf(path, *args, **kwargs)

        with patch.object(engine, "PdfReader", side_effect=read_with_pause):
            try:
                with patch.object(app.filedialog, "askopenfilenames", return_value=(str(self.single), str(self.multi))):
                    self.instance.import_button.invoke()
                self.assertTrue(started.wait(3))
                deadline = time.monotonic() + 1
                while str(self.multi) not in self.instance.tree_record_paths.values() and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(0.01)
                self.assertIn(str(self.multi), self.instance.tree_record_paths.values(), "Completed file must appear before the whole batch finishes")
                item = next(item for item, path in self.instance.tree_record_paths.items() if path == str(self.multi))
                self.assertEqual(self.instance.tree.set(item, "pages"), "3")
                self.assertEqual(self.instance.table_frame.winfo_manager(), "pack")
                self.assertTrue(self.instance.busy)
                self.assertTrue(self.instance.print_button.instate(["disabled"]))
            finally:
                release.set()
                self._wait()

    def test_import_lists_source_names_while_first_file_is_reading(self) -> None:
        started = threading.Event()
        release = threading.Event()
        read_pdf = engine.PdfReader

        def read_with_pause(path, *args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("First file was not released")
            return read_pdf(path, *args, **kwargs)

        with patch.object(engine, "PdfReader", side_effect=read_with_pause):
            try:
                with patch.object(app.filedialog, "askopenfilenames", return_value=(str(self.single), str(self.multi))):
                    self.instance.import_button.invoke()
                self.assertTrue(started.wait(3))
                self.root.update()
                names = {self.instance.tree.item(item, "text") for item in self.instance.tree.get_children()}
                self.assertEqual(names, {self.single.name, self.multi.name})
                self.assertTrue(self.instance.busy)
                self.assertEqual(self.instance.table_frame.winfo_manager(), "pack")
            finally:
                release.set()
                self._wait()

    def test_failed_append_restores_previous_import_state(self) -> None:
        self._import(self.single, self.multi)
        self._select(self.single)
        self.instance.remove_button.invoke()
        previous_paths = self.instance.imported_paths
        previous_signature = self.instance.scan_signature
        invalid = self.inputs / "zzz-invalid.pdf"
        invalid.write_bytes(b"not a PDF")
        with patch.object(app.filedialog, "askopenfilenames", return_value=(str(invalid),)):
            self.instance.add_files_button.invoke()
        self._wait()
        self.error.assert_called_once()
        self.assertEqual(self.instance.imported_paths, previous_paths)
        self.assertEqual(self.instance.scan_signature, previous_signature)
        self.assertEqual(set(self.instance.tree_record_paths.values()), {str(self.multi)})
        self.assertNotIn(invalid.name, [self.instance.tree.item(item, "text") for item in self.instance.tree.get_children()])
        self.assertIsNotNone(self.instance._require_scan(require_printer=False))

    def test_scan_refresh_keeps_existing_detail_rows(self) -> None:
        self._import(self.single, self.multi)
        self._select(self.multi)
        previous_item = self.instance.tree.selection()[0]
        with patch.object(app.filedialog, "askopenfilenames", return_value=(str(self.image),)):
            self.instance.add_files_button.invoke()
        self._wait()
        self.assertTrue(self.instance.tree.exists(previous_item))
        self.assertEqual(self.instance.tree.selection(), (previous_item,))
        self.assertEqual(len(self.instance.tree.get_children()), 3)

    def test_upload_card_stays_hidden_before_import_and_after_removing_all(self) -> None:
        self.root.deiconify()
        self.root.geometry("980x800+20+20")
        self.root.update()
        self.assertFalse(self.instance.empty_drop_frame.winfo_ismapped())
        self.assertTrue(self.instance.table_frame.winfo_viewable())
        self.assertTrue(self.instance.import_button.winfo_viewable())
        self.assertEqual(self.instance.attachment_status_var.get(), "尚未导入附件")
        self._import(self.single, self.multi)
        self.instance.select_all_button.invoke()
        self.instance.remove_button.invoke()
        self.root.update()
        self.assertEqual(self.instance.tree.get_children(), ())
        self.assertFalse(self.instance.empty_drop_frame.winfo_ismapped())
        self.assertTrue(self.instance.table_frame.winfo_viewable())
        self.assertEqual(self.instance.attachment_status_var.get(), "当前附件列表为空")
        self.assertTrue(self.instance.add_files_button.winfo_viewable())
        self.assertTrue(self.instance.remove_button.instate(["disabled"]))
        x = self.instance.table_frame.winfo_rootx() + 10
        y = self.instance.table_frame.winfo_rooty() + 10
        self.assertTrue(self.instance._is_attachment_drop_point(x, y))
        self.print_backend.assert_not_called()

    def test_imported_details_are_visible_in_compact_window(self) -> None:
        self.root.deiconify()
        self.root.geometry("980x800+20+20")
        self._import(self.single)
        self.assertTrue(self.instance.tree.winfo_viewable())
        self.assertGreaterEqual(self.instance.tree.winfo_height(), 80)
        item = self.instance.tree.get_children()[0]
        self.assertTrue(self.instance.tree.bbox(item))
        self.instance.advanced_toggle_button.invoke()
        self.root.update()
        self.assertTrue(self.instance.stamp_target_combo.winfo_viewable())
        self.instance.advanced_toggle_button.invoke()
        self.root.update()
        self.assertTrue(self.instance.tree.winfo_viewable())
        self.assertGreaterEqual(self.instance.tree.winfo_height(), 80)

    def test_batch_poll_yields_with_messages_still_pending(self) -> None:
        self.root.after_cancel(self.instance._poll_after)
        for index in range(600):
            self.instance.events.put(("log", f"Batch progress {index}"))
        heartbeat = []
        self.root.after(0, lambda: heartbeat.append(self.instance.events.qsize()))
        self.instance._poll_events()
        try:
            self.assertFalse(self.instance.events.empty(), "One poll must leave time for window events")
        finally:
            self.root.update()
        self.assertTrue(heartbeat)
        self.assertGreater(heartbeat[0], 0)

    def test_browse_folder_automatically_scans_nested_files_once(self) -> None:
        (self.inputs / "部门" / "月份").mkdir(parents=True)
        nested = self._pdf("部门/月份/invoice.pdf", [(420, 595)])
        with patch.object(app.filedialog, "askdirectory", return_value=str(self.inputs)), \
             patch.object(engine, "analyze_folder", wraps=engine.analyze_folder) as scanner:
            self.instance.browse_button.invoke()
            self._wait()
        scanner.assert_called_once()
        self.assertEqual(self.instance.source_mode, "folder")
        self.assertEqual(self.instance.folder_var.get(), str(self.inputs))
        self.assertIn(str(nested), self.instance.tree_record_paths.values())
        names = [self.instance.tree.item(item, "text") for item in self.instance.tree.get_children()]
        self.assertIn("部门/月份/invoice.pdf", names)
        self.error.assert_not_called()
        self.print_backend.assert_not_called()

    def test_nested_identical_images_and_zips_have_separate_detail_rows(self) -> None:
        nested = self.inputs / "same"
        nested.mkdir()
        (nested / self.image.name).write_bytes(self.image.read_bytes())
        (nested / self.archive.name).write_bytes(self.archive.read_bytes())
        with patch.object(app.filedialog, "askdirectory", return_value=str(self.inputs)):
            self.instance.browse_button.invoke()
        self._wait()
        scan = self.instance._active_scan_result()
        self.assertIsNotNone(scan)
        self.assertEqual(scan["pdf_count"], 3)
        self.assertEqual(len(self.instance.tree.get_children()), 3)
        names = {self.instance.tree.item(item, "text") for item in self.instance.tree.get_children()}
        self.assertEqual(names, {"sample.png", "single.pdf", "multi.pdf"})
        self.assertEqual(scan["duplicate_skipped_count"], 3)
        self.error.assert_not_called()

    def test_scan_without_folder_selects_and_scans_once(self) -> None:
        self.instance.folder_var.set("")
        with patch.object(app.filedialog, "askdirectory", return_value=str(self.inputs)) as chooser, \
             patch.object(engine, "analyze_folder", wraps=engine.analyze_folder) as scanner:
            self.instance.scan_button.invoke()
            self._wait()
        chooser.assert_called_once()
        scanner.assert_called_once()
        self.assertEqual(self.instance.pdf_metric_var.get(), "3")
        self.error.assert_not_called()

    def test_cancelled_folder_selection_preserves_imported_results(self) -> None:
        self.instance.folder_var.set("")
        self._import(self.single)
        previous = self.instance._scan_previous_state()
        rows = self.instance.tree.get_children()
        with patch.object(app.filedialog, "askdirectory", return_value="") as chooser, \
             patch.object(engine, "analyze_folder", wraps=engine.analyze_folder) as scanner:
            self.instance.browse_button.invoke()
            self.instance.scan_button.invoke()
        self.assertEqual(chooser.call_count, 2)
        scanner.assert_not_called()
        self.assertFalse(self.instance.busy)
        self.assertEqual(self.instance._scan_previous_state(), previous)
        self.assertEqual(self.instance.tree.get_children(), rows)
        self.print_backend.assert_not_called()

    def test_folder_scan_uses_both_copy_counts(self) -> None:
        rail = self._pdf("26329116805001786023.pdf", [(300, 200)])
        self.instance.folder_var.set(str(self.inputs))
        self.instance.rail_copies_var.set(3)
        self.instance.other_copies_var.set(2)
        self.instance.scan_button.invoke()
        self._wait()
        self.error.assert_not_called()
        scan = self.instance._active_scan_result()
        self.assertIsNotNone(scan)
        self.assertEqual(next(record["copies"] for record in scan["files"] if record["path"] == str(rail)), 3)
        self.assertTrue(all(record["copies"] == 2 for record in scan["files"] if not record["rail"]))

    def test_append_deduplicates_and_preserves_removed_files(self) -> None:
        self._import(self.single, self.multi)
        self._select(self.single)
        self.instance.remove_button.invoke()
        with patch.object(app.filedialog, "askopenfilenames", return_value=(str(self.image), str(self.multi))):
            self.instance.add_files_button.invoke()
        self._wait()
        scan = self.instance._active_scan_result()
        self.assertEqual(scan["pdf_count"], 2)
        self.assertNotIn(str(self.single), self.instance.tree_record_paths.values())
        self.assertTrue(self.single.is_file())

    def test_remove_all_keeps_sources_and_disables_print_actions(self) -> None:
        self._import(self.single, self.multi)
        self.instance.tree.selection_set(self.instance.tree.get_children())
        self.root.update()
        self.instance.remove_button.invoke()
        self.assertEqual(self.instance.tree.get_children(), ())
        self.assertEqual(self.instance.pdf_metric_var.get(), "0")
        self.assertTrue(self.instance.print_button.instate(["disabled"]))
        self.assertTrue(self.single.is_file() and self.multi.is_file())

    def test_select_all_button_and_keyboard_support_batch_remove(self) -> None:
        self._import(self.single, self.multi, self.image)
        button = getattr(self.instance, "select_all_button", None)
        self.assertIsNotNone(button, "The list needs a visible select-all control")
        button.invoke()
        self.root.update()
        self.assertEqual(len(self.instance.tree.selection()), 3)
        self.assertIn("3", self.instance.remove_button.cget("text"))
        self.instance.tree.selection_remove(self.instance.tree.selection())
        self.root.deiconify()
        self.root.update()
        self.instance.tree.focus_force()
        self.instance.tree.event_generate("<Control-a>")
        self.root.update()
        self.assertEqual(len(self.instance.tree.selection()), 3)
        self.instance.remove_button.invoke()
        self.assertEqual(self.instance.tree.get_children(), ())
        self.assertEqual(self.instance.jobs_metric_var.get(), "0")
        self.assertTrue(button.instate(["disabled"]))
        self.assertTrue(all(path.is_file() for path in (self.single, self.multi, self.image)))

    def _show_drag_list(self, count: int = 5) -> tuple[Path, ...]:
        paths = tuple(self._pdf(f"drag-{index:02d}.pdf", [(300 + index, 400)])
                      for index in range(count))
        self._import(*paths)
        self.root.geometry("1240x1000")
        self.root.deiconify()
        self.root.update()
        self.instance.tree.focus_force()
        return paths

    def _row_point(self, item: str) -> tuple[int, int]:
        box = self.instance.tree.bbox(item, "#0")
        self.assertTrue(box, f"Expected a visible row: {item}")
        x, y, _width, height = box
        return x + 50, y + height // 2

    def test_mouse_drag_selects_range_and_removes_only_selected_sources(self) -> None:
        paths = self._show_drag_list()
        tree = self.instance.tree
        items = tree.get_children()
        x, y = self._row_point(items[0])
        tree.event_generate("<ButtonPress-1>", x=x, y=y)
        x, y = self._row_point(items[3])
        tree.event_generate("<B1-Motion>", x=x, y=y)
        self.root.update()
        self.assertEqual(tree.selection(), items[:4])
        x, y = self._row_point(items[2])
        tree.event_generate("<B1-Motion>", x=x, y=y)
        tree.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.root.update()
        self.assertEqual(tree.selection(), items[:3])
        self.assertEqual(self.instance.remove_button.cget("text"), "移除所选（3）")
        self.instance.remove_button.invoke()
        self.root.update()
        self.assertEqual(set(self.instance.tree_record_paths.values()), set(map(str, paths[3:])))
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertFalse(self.callback_errors)
        self.print_backend.assert_not_called()

    def test_drag_at_bottom_scrolls_until_release(self) -> None:
        self._show_drag_list(30)
        tree = self.instance.tree
        items = tree.get_children()
        x, y = self._row_point(items[0])
        tree.event_generate("<ButtonPress-1>", x=x, y=y)
        tree.event_generate("<B1-Motion>", x=x, y=tree.winfo_height() - 2)
        deadline = time.monotonic() + 5
        while items[-1] not in tree.selection() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.assertEqual(tree.selection(), items)
        tree.event_generate("<ButtonRelease-1>", x=x, y=tree.winfo_height() - 2)
        self.root.update()
        tree.yview_moveto(0)
        view = tree.yview()
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.assertEqual(tree.yview(), view, "Mouse release must cancel auto scrolling")
        self.assertFalse(self.callback_errors)

    def test_ctrl_and_shift_click_remain_available_with_drag_selection(self) -> None:
        self._show_drag_list()
        tree = self.instance.tree
        items = tree.get_children()
        for index, state in ((0, 0), (3, 4)):
            x, y = self._row_point(items[index])
            tree.event_generate("<ButtonPress-1>", x=x, y=y, state=state)
            tree.event_generate("<ButtonRelease-1>", x=x, y=y, state=state)
        self.root.update()
        self.assertEqual(tree.selection(), (items[0], items[3]))
        tree.selection_set(items[0])
        tree.focus(items[0])
        x, y = self._row_point(items[2])
        tree.event_generate("<ButtonPress-1>", x=x, y=y, state=1)
        tree.event_generate("<ButtonRelease-1>", x=x, y=y, state=1)
        self.root.update()
        self.assertEqual(tree.selection(), items[:3])
        self.assertFalse(self.callback_errors)

    def test_drag_upwards_and_heading_click_preserve_expected_selection(self) -> None:
        self._show_drag_list()
        tree = self.instance.tree
        items = tree.get_children()
        x, y = self._row_point(items[4])
        tree.event_generate("<ButtonPress-1>", x=x, y=y)
        x, y = self._row_point(items[1])
        tree.event_generate("<B1-Motion>", x=x, y=y)
        tree.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.root.update()
        self.assertEqual(tree.selection(), items[1:])
        tree.event_generate("<ButtonPress-1>", x=x, y=8)
        tree.event_generate("<B1-Motion>", x=x, y=y)
        tree.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.root.update()
        self.assertEqual(tree.selection(), items[1:])
        self.assertIsNone(self.instance._tree_drag_anchor)

    def test_busy_transition_cancels_pending_drag_scroll(self) -> None:
        self._show_drag_list(20)
        tree = self.instance.tree
        x, y = self._row_point(tree.get_children()[0])
        tree.event_generate("<ButtonPress-1>", x=x, y=y)
        tree.event_generate("<B1-Motion>", x=x, y=tree.winfo_height() - 2)
        self.assertIsNotNone(self.instance._tree_drag_after)
        self.instance._set_busy(True, "测试处理中")
        self.assertIsNone(self.instance._tree_drag_after)
        self.assertIsNone(self.instance._tree_drag_anchor)
        tree.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.instance._set_busy(False, "就绪")
        self.assertFalse(self.callback_errors)

    def test_zip_without_amount_is_visible_and_included_in_print_requests(self) -> None:
        archive = self.inputs / "mixed-no-amount.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(self.single, "01-plain.PDF")
            handle.writestr("02-bad.pdf", b"invalid PDF")
            handle.write(self.multi, "nested/03-details.pdf")
        self._import(archive)
        self.assertEqual(self.instance._active_scan_result()["pdf_count"], 2)
        self.assertEqual(self.instance._active_scan_result()["amount_unrecognized_count"], 2)
        self.assertEqual(len(self.instance.tree.get_children()), 2)
        self.assertIn("ZIP 内跳过 1", self.instance.summary_var.get())
        self.confirm.return_value = True
        self.print_backend.side_effect = self._print_success
        self.instance.print_button.invoke()
        self._wait()
        requested = self.print_backend.call_args.args[0]
        self.assertEqual(requested["pdf_count"], 2)
        self.assertTrue(all(record["amount"] is None for record in requested["files"]))
        self.assertIn("未识别金额的 PDF 也会打印", self.confirm.call_args.args[1])
        self.instance.tree.selection_set(self.instance.tree.get_children()[1])
        self.root.update()
        self.instance.selected_print_button.invoke()
        self._wait()
        self.assertEqual(self.print_backend.call_args.args[0]["pdf_count"], 1)
        self.assertIsNone(self.print_backend.call_args.args[0]["files"][0]["amount"])
        self.error.assert_not_called()
        self.assertTrue(archive.is_file() and self.single.is_file() and self.multi.is_file())

    def test_copy_starts_at_zero_even_when_old_settings_saved_positive_count(self) -> None:
        self.assertEqual(self.instance.copy_count_var.get(), 0)
        self._import(self.single)
        self._select(self.single)
        self.assertTrue(self.instance.copy_button.instate(["disabled"]))
        self.instance.copy_count_var.set(4)
        self.root.update()
        self.assertFalse(self.instance.copy_button.instate(["disabled"]))
        self.instance._save_settings()
        saved = json.loads(app.SETTINGS_PATH.read_text(encoding="utf-8"))
        saved["copy_count"] = 7
        app.SETTINGS_PATH.write_text(json.dumps(saved), encoding="utf-8")
        self.instance._load_settings(None)
        self.root.update()
        self.assertEqual(self.instance.copy_count_var.get(), 0)
        self.assertTrue(self.instance.copy_button.instate(["disabled"]))

    def test_copy_invalid_or_zero_count_never_opens_print_confirmation(self) -> None:
        self._import(self.single)
        self._select(self.single)
        for value in ("0", "", "abc", "-1", "1.5", "100"):
            self.instance.copy_spin.set(value)
            self.instance.on_copy_selected()
            self.assertTrue(self.instance.copy_button.instate(["disabled"]))
        self.confirm.assert_not_called()
        self.print_backend.assert_not_called()

    def test_validation_dialog_reports_skipped_duplicate_imports(self) -> None:
        self._import(self.single, self.archive)
        self.validate_backend.return_value = {
            "success": True, "checked_pages": 1, "failed_pages": 0,
            "duplicate_groups": [], "duplicate_skipped_count": 1,
            "duplicate_imports": self.instance.scan_result.get("duplicate_imports", []),
        }
        self.instance.validate_button.invoke()
        self._wait()
        self.assertIn("重复导入检查", self.info.call_args.args[1])
        self.assertIn("跳过重复 1", self.info.call_args.args[1])
        self.print_backend.assert_not_called()

    def test_invalid_scan_reports_error_and_recovers(self) -> None:
        previous_summary = self.instance.summary_var.get()
        invalid = self.inputs / "invalid.pdf"
        invalid.write_bytes(b"not a PDF")
        self._import(invalid)
        self.error.assert_called_once()
        self.assertEqual(self.instance.summary_var.get(), previous_summary)
        self.assertFalse(self.instance.import_button.instate(["disabled"]))
        self.error.reset_mock()
        self._import(self.single)
        self.error.assert_not_called()
        self.assertEqual(self.instance.pdf_metric_var.get(), "1")

    def test_drop_callback_handles_spaces_and_rejects_malformed_data(self) -> None:
        source = self._pdf("drop with spaces.pdf", [(340, 227)])
        with patch.object(self.instance, "_is_attachment_drop_point", return_value=True):
            result = self.instance._on_tkdnd_drop(SimpleNamespace(data=f"{{{source}}}", x_root=10, y_root=20))
            self.assertEqual(result, app.COPY)
            self.root.update()
            self._wait()
        self.assertIn(str(source), self.instance.tree_record_paths.values())
        self.assertEqual(self.instance._on_tkdnd_drop(SimpleNamespace(data="{broken", x_root=10, y_root=20)), app.REFUSE_DROP)
        self.assertIn("拖放失败", self.instance.status_var.get())
        self.assertEqual(self.callback_errors, [])

    def test_preset_rules_and_margin_are_persisted_with_debounce(self) -> None:
        self._import(self.multi)
        for name in app.MAIN_PRESET_NAMES:
            self.instance.preset_var.set(name)
            self.assertEqual(self.instance._current_preset()["name"], name)
        for name in app.ADVANCED_PRESET_NAMES:
            self.instance.advanced_preset_var.set(name)
            self.instance.advanced_preset_combo.event_generate("<<ComboboxSelected>>")
            self.root.update()
            self.assertEqual(self.instance.preset_var.get(), name)
        self.instance.paper_var.set("A4")
        self.instance.rail_copies_var.set(3)
        self.instance.other_copies_var.set(2)
        self.instance.copy_count_var.set(5)
        with patch.object(app, "_write_json", wraps=app._write_json) as write:
            for value in (1, 5, 12.5):
                self.instance.margin_mm_var.set(value)
                self.instance._on_margin_changed()
            self.assertEqual(write.call_count, 0)
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertEqual(write.call_count, 1)
        saved = json.loads(app.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["margin_mm"], 12.5)
        self.assertEqual(saved["paper_size"], "A4")
        self.instance.margin_mm_var.set(0)
        self.instance._load_settings(None)
        self.assertEqual(self.instance.margin_mm_var.get(), 12.5)
        self.assertEqual(self.instance.copy_count_var.get(), 0)
        self.assertEqual(self.instance.preset_var.get(), "纵向适配")

    def test_preview_fits_small_page_to_margin_without_printing(self) -> None:
        self._import(self.single)
        self.instance.paper_var.set("A4")
        self.instance.preset_var.set("标准")
        self.instance.margin_mm_var.set(10)
        self.instance.preview_button.invoke()
        self.root.deiconify()
        self.root.update()
        dialog = next(child for child in self.root.winfo_children()
                      if isinstance(child, tk.Toplevel) and child.title().startswith("打印预览"))
        canvas = next(child for child in dialog.winfo_children()[0].winfo_children() if isinstance(child, tk.Canvas))
        rectangles = [item for item in canvas.find_all() if canvas.type(item) == "rectangle"]
        left, top, right, bottom = canvas.coords(rectangles[2])
        aspect = 340 / 227
        expected_width = min(right - left, (bottom - top) * aspect)
        self.assertAlmostEqual(dialog._preview_photo.width(), expected_width, delta=2)
        self.print_backend.assert_not_called()
        self.error.assert_not_called()
        dialog.destroy()

    def test_pdf_export_requires_no_printer_and_preserves_inputs(self) -> None:
        self._import(self.single, self.multi, self.image)
        self.instance.printer_var.set("")
        original = self.single.read_bytes()
        output = self.directory / "merged.pdf"
        with patch.object(app.filedialog, "asksaveasfilename", return_value=str(output)):
            self.instance.export_pdf_button.invoke()
        self._wait()
        self.assertTrue(output.is_file())
        self.assertEqual(len(PdfReader(output).pages), 5)
        self.assertEqual(self.single.read_bytes(), original)
        self.print_backend.assert_not_called()

    def test_selected_export_and_source_overwrite_protection(self) -> None:
        self._import(self.single, self.multi)
        self._select(self.multi)
        output = self.directory / "selected.pdf"
        with patch.object(app.filedialog, "asksaveasfilename", return_value=str(output)):
            self.instance.export_pdf_button.invoke()
        self._wait()
        self.assertEqual(len(PdfReader(output).pages), 3)
        original = self.multi.read_bytes()
        with patch.object(app.filedialog, "asksaveasfilename", return_value=str(self.multi)):
            self.instance.export_pdf_button.invoke()
        self._wait()
        self.error.assert_called_once()
        self.assertEqual(self.multi.read_bytes(), original)

    def test_normal_and_seam_stamp_selected_pages_and_all_pages(self) -> None:
        original = self.multi.read_bytes()
        for mode, page_text, all_pages, expected in (
            ("普通盖章", "2", False, [2]),
            ("齐缝章", "1,3", False, [1, 3]),
            ("普通盖章", "", True, [1, 2, 3]),
            ("齐缝章", "", True, [1, 2, 3]),
        ):
            with self.subTest(mode=mode, all_pages=all_pages):
                self._import(self.multi)
                self._select(self.multi)
                self.instance.stamp_image_var.set(str(self.stamp))
                self.instance.stamp_mode_var.set(mode)
                self.instance.stamp_pages_var.set(page_text)
                self.instance.stamp_all_pages_var.set(all_pages)
                self.confirm.return_value = True
                self.instance.stamp_button.invoke()
                self._wait()
                record = self.instance._active_scan_result()["files"][0]
                self.assertTrue(record["stamped"])
                self.assertEqual(record["stamp_pages"], expected)
                self.assertEqual(len(PdfReader(record["path"]).pages), 3)
                self.assertEqual(self.multi.read_bytes(), original)
                self.assertEqual(self.instance.scan_signature, self.instance._current_signature())
        self.error.assert_not_called()

    def test_stamp_rejects_invalid_pages_size_and_single_page_seam(self) -> None:
        self._import(self.multi)
        self.instance.stamp_image_var.set(str(self.stamp))
        for size, pages, mode in (("NaN", "1", "普通盖章"),
                                  ("28", "4", "普通盖章"),
                                  ("28", "1", "齐缝章")):
            self.instance.stamp_size_var.set(size)
            self.instance.stamp_pages_var.set(pages)
            self.instance.stamp_mode_var.set(mode)
            self.instance.stamp_button.invoke()
            self.assertFalse(self.instance.busy)
        self.assertEqual(self.warning.call_count, 3)
        self.confirm.assert_not_called()

    def test_all_print_confirmations_can_be_cancelled(self) -> None:
        self._import(self.multi)
        self._select(self.multi)
        self.instance.copy_count_var.set(2)
        for button in (self.instance.print_button, self.instance.selected_print_button,
                       self.instance.copy_button):
            button.invoke()
            self.assertFalse(self.instance.busy)
        self.instance.calibration_button.invoke()
        self.instance.calibration_copies_var.set("0")
        self.instance.calibration_submit_button.invoke()
        self.warning.assert_called_once()
        self.instance.calibration_copies_var.set("2")
        self.instance.calibration_orientation_var.set("横向")
        self.instance.calibration_submit_button.invoke()
        self.assertEqual(self.confirm.call_count, 4)
        self.print_backend.assert_not_called()
        self.calibration_backend.assert_not_called()
        self.instance.calibration_dialog.destroy()

    def test_print_failure_records_reason_and_recovers_controls(self) -> None:
        self._import(self.single)
        self.confirm.return_value = True
        self.print_backend.side_effect = RuntimeError("Test printer offline")
        self.instance.print_button.invoke()
        self._wait()
        self.assertEqual(self.instance.print_history[-1]["failure_reason"], "Test printer offline")
        self.assertTrue(self.instance.print_history[-1]["source_paths"])
        self.assertFalse(self.instance.print_button.instate(["disabled"]))
        saved = json.loads(app.PRINT_HISTORY_PATH.read_text(encoding="utf-8"))
        self.assertFalse(saved[-1]["success"])

    def test_history_retry_requires_confirmation(self) -> None:
        self._import(self.single)
        context = self.instance._make_print_context(self.instance._active_scan_result(), "batch", "打印全部",
                                                    "Test printer", "A5", self.instance._current_preset(), 0)
        self.instance._append_print_history({}, context, error="Test failure")
        self._select_latest_history()
        self.instance.on_retry_history()
        self._wait()
        self.confirm.assert_called_once()
        self.print_backend.assert_not_called()
        self.assertEqual(len(self.instance.print_history), 1)

    def test_repeated_history_retry_preserves_uniform_copies(self) -> None:
        self._import(self.single)
        self.confirm.return_value = True
        self.print_backend.side_effect = self._print_success
        scan = engine.with_uniform_copies(self.instance._active_scan_result(), 3)
        context = self.instance._make_print_context(scan, "copy", "按份数打印所选", "Test printer", "A5",
                                                    self.instance._current_preset(), 0)
        context["copies"] = 3
        self.instance._append_print_history({}, context, error="Test failure")
        for _ in range(2):
            self._select_latest_history()
            self.instance.on_retry_history()
            self._wait()
            self.assertEqual(self.instance.print_history[-1]["copies"], 3)
            self.assertEqual(self.print_backend.call_args.args[0]["files"][0]["copies"], 3)

    def test_history_missing_sources_blocks_retry(self) -> None:
        self.instance._append_print_history({}, {"source_paths": ["missing.pdf"]}, error="Test failure")
        self._select_latest_history()
        self.instance.on_retry_history()
        self.warning.assert_called_once()
        self.print_backend.assert_not_called()

    def test_validation_callback_never_calls_print_backend(self) -> None:
        self._import(self.single)
        self.validate_backend.return_value = {"success": True, "checked_pages": 1, "failed_pages": 0}
        self.assertEqual(self.instance.scan_signature, self.instance._current_signature())
        self.assertTrue(self.instance.printer_var.get())
        self.assertFalse(self.instance.validate_button.instate(["disabled"]))
        self.instance.validate_button.invoke()
        self._wait()
        self.validate_backend.assert_called_once()
        self.print_backend.assert_not_called()
        self.assertIn("规则校验", self.instance.status_var.get())

    def test_browse_printer_refresh_and_log_report_errors(self) -> None:
        with patch.object(app.filedialog, "askdirectory", return_value=str(self.inputs)):
            self.instance.browse_button.invoke()
        self._wait()
        self.assertEqual(self.instance.folder_var.get(), str(self.inputs))
        with patch.object(engine, "list_printers", side_effect=RuntimeError("Test refresh failure")):
            self.instance.refresh_button.invoke()
        self.error.assert_called_once()
        self.assertIn("Test refresh failure", self.instance.log_text.get("1.0", tk.END))
        self.instance.refresh_button.invoke()
        self.assertEqual(tuple(self.instance.printer_combo["values"]), ("Test printer",))

    def test_selected_print_and_copy_forward_selected_files_and_rules(self) -> None:
        self._import(self.single, self.multi)
        self._select(self.single)
        self.instance.paper_var.set("A4")
        self.instance.preset_var.set("横向实际")
        self.instance.margin_mm_var.set(6)
        self.confirm.return_value = True
        self.print_backend.side_effect = self._print_success
        self.instance.selected_print_button.invoke()
        self._wait()
        parameters = self.print_backend.call_args.kwargs
        self.assertEqual(parameters["paper_size"], "A4")
        self.assertEqual(parameters["orientation_mode"], "landscape")
        self.assertEqual(parameters["scale_mode"], "actual")
        self.assertEqual(parameters["margin_mm"], 6)
        self.assertEqual([record["path"] for record in self.print_backend.call_args.args[0]["files"]], [str(self.single)])
        self.instance.copy_count_var.set(4)
        self.instance.copy_button.invoke()
        self._wait()
        self.assertEqual(self.print_backend.call_args.args[0]["files"][0]["copies"], 4)
        self.assertEqual(self.instance._active_scan_result()["files"][0]["copies"], 1)
        self.assertEqual(self.instance.print_history[-1]["copies"], 4)

    def test_history_retry_restores_per_file_counts_after_new_scan(self) -> None:
        self._import(self.single)
        context = self.instance._make_print_context(self.instance._active_scan_result(), "batch", "打印全部",
                                                    "Test printer", "A5", self.instance._current_preset(), 0)
        self.instance._append_print_history({}, context, error="Test failure")
        self.instance.other_copies_var.set(5)
        self._import(self.single)
        self.confirm.return_value = True
        self.print_backend.side_effect = self._print_success
        self._select_latest_history()
        self.instance.on_retry_history()
        self._wait()
        self.assertEqual(self.print_backend.call_args.args[0]["files"][0]["copies"], 1)
        self.assertEqual(self.instance._active_scan_result()["files"][0]["copies"], 5)

    def test_tray_event_callbacks_hide_restore_and_exit(self) -> None:
        with patch.object(app.SystemTray, "started", new_callable=PropertyMock, return_value=True):
            self.instance._show_from_tray()
            self.assertEqual(self.root.state(), "normal")
            self.instance._hide_to_tray()
            self.assertEqual(self.root.state(), "withdrawn")
            self.assertTrue(self.instance._tray_hidden)
            self.instance.events.put(("tray_show",))
            self.root.after_cancel(self.instance._poll_after)
            self.instance._poll_events()
            self.assertEqual(self.root.state(), "normal")
            self.assertFalse(self.instance._tray_hidden)
            self.instance.events.put(("tray_exit",))
            self.root.after_cancel(self.instance._poll_after)
            self.instance._poll_events()
            self.assertTrue(self.instance.closing)
            self.root = None

    def test_window_close_hides_to_tray_and_preserves_imported_list(self) -> None:
        self._import(self.single)
        scan = self.instance.scan_result
        with patch.object(app.SystemTray, "started", new_callable=PropertyMock, return_value=True):
            self.instance._show_from_tray()
            self.instance.on_window_close()
            self.assertEqual(self.root.state(), "withdrawn")
            self.assertFalse(self.instance.closing)
            self.assertIs(self.instance.scan_result, scan)
            self.assertEqual(len(self.instance.tree.get_children()), 1)
            self.instance._show_from_tray()
            self.assertEqual(self.root.state(), "normal")

    def test_window_close_exits_if_tray_is_not_ready(self) -> None:
        with patch.object(app.SystemTray, "started", new_callable=PropertyMock, return_value=False):
            self.instance.on_window_close()
        self.assertTrue(self.instance.closing)
        self.root = None

    def test_close_to_tray_preference_persists_and_can_be_disabled(self) -> None:
        self.assertTrue(self.instance.close_to_tray_var.get())
        self.instance.close_to_tray_var.set(False)
        self.instance._save_settings()
        self.instance.close_to_tray_var.set(True)
        self.instance._load_settings(None)
        self.assertFalse(self.instance.close_to_tray_var.get())
        self.assertFalse(json.loads(app.SETTINGS_PATH.read_text(encoding="utf-8"))["close_to_tray"])
        with patch.object(app.SystemTray, "started", new_callable=PropertyMock, return_value=True):
            self.instance.on_window_close()
        self.assertTrue(self.instance.closing)
        self.root = None

    def test_tkdnd_cleanup_removes_real_tcl_binding_and_is_idempotent(self) -> None:
        self.assertTrue(self.instance._drop_dnd_enabled)
        callback = self.instance._drop_bind_id
        self.assertIn(callback, self.root.bind("<<Drop>>"))
        self.instance._disable_file_drop()
        self.assertNotIn(callback, self.root.bind("<<Drop>>"))
        self.assertEqual(self.root.tk.call("info", "commands", callback), "")
        self.instance._disable_file_drop()
        self.assertFalse(self.instance._drop_target_registered)

    def test_busy_close_cancels_worker_and_cleans_stamp_temporary_file(self) -> None:
        ready = threading.Event()

        def worker(_progress):
            ready.set()
            while True:
                engine._check_cancelled()
                time.sleep(0.01)

        temporary = self.directory / "generated-stamp.pdf"
        temporary.write_bytes(b"test temporary")
        self.instance.stamp_temp_paths.add(temporary)
        self.instance._start_worker("Test worker", worker, "scan_done", ())
        self.assertTrue(ready.wait(2))
        self.instance.on_close()
        self.assertFalse(self.instance.closing)
        self.confirm.return_value = True
        self.instance.on_close()
        deadline = time.monotonic() + 5
        while temporary.exists() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(temporary.exists())
        self.assertFalse(self.instance.worker_thread.is_alive())
        self.root = None


if __name__ == "__main__":
    unittest.main()
