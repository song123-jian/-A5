from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class AppFeatureTests(unittest.TestCase):
    def _app_stub(self) -> app.TicketPrinterApp:
        instance = object.__new__(app.TicketPrinterApp)
        instance.print_history = []
        instance.printer_var = _Value("Test printer")
        instance.paper_var = _Value("A5")
        instance.preset_var = _Value("标准")
        instance.margin_mm_var = _Value(2)
        return instance

    def test_main_presets_are_four_common_choices(self) -> None:
        self.assertEqual(app.MAIN_PRESET_NAMES, ("智能", "标准", "高清", "快速"))
        self.assertEqual(
            app.ADVANCED_PRESET_NAMES,
            ("双面", "横向实际", "纵向实际", "横向适配", "纵向适配"),
        )

    def test_print_history_persists_success_and_failure_details(self) -> None:
        with tempfile.TemporaryDirectory(prefix="a5-history-") as directory:
            history_path = Path(directory) / "print_history.json"
            instance = self._app_stub()
            context = {
                "mode": "batch",
                "operation": "打印全部",
                "expected_jobs": 3,
                "source_names": ["sample.pdf"],
                "source_paths": ["C:/sample.pdf"],
            }
            result = {
                "success": True,
                "expected_jobs": 3,
                "submitted_jobs": 3,
                "jobs_observed_in_queue": 3,
                "queue_remaining_with_prefix": 0,
            }
            with patch.object(app, "PRINT_HISTORY_PATH", history_path):
                instance._append_print_history(result, context)
                instance._append_print_history({}, context, error="打印机离线")
                saved = json.loads(history_path.read_text(encoding="utf-8"))

            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0]["status"], "成功")
            self.assertEqual(saved[0]["submitted_jobs"], 3)
            self.assertEqual(saved[1]["status"], "失败")
            self.assertEqual(saved[1]["failure_reason"], "打印机离线")
            self.assertEqual(saved[1]["source_paths"], ["C:/sample.pdf"])

    def test_print_history_loader_discards_invalid_entries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="a5-history-") as directory:
            history_path = Path(directory) / "print_history.json"
            history_path.write_text(
                json.dumps([{"status": "成功"}, "invalid", 3], ensure_ascii=False),
                encoding="utf-8",
            )
            instance = object.__new__(app.TicketPrinterApp)
            with patch.object(app, "PRINT_HISTORY_PATH", history_path):
                loaded = instance._load_print_history()
            self.assertEqual(loaded, [{"status": "成功"}])


if __name__ == "__main__":
    unittest.main()
