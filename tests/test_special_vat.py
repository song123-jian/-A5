from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

import printer_engine as engine


def invoice_pdf(path: Path, title: str, *lines: str) -> Path:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    document = canvas.Canvas(str(path), pagesize=(595, 420))
    document.setFont("STSong-Light", 16)
    for index, line in enumerate("\n".join((title, *lines)).splitlines()):
        document.drawString(35, 380 - index * 24, line)
    document.save()
    return path


class SpecialVatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="special-vat-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.directory / "state")})
        environment.start()
        self.addCleanup(environment.stop)

    def test_special_titles_default_to_two_and_override_numeric_filename(self) -> None:
        for index, title in enumerate((
            "增值税专用发票", "增值税电子专用发票",
            "电子发票（增值税专用发票）",
            "电子发票(增 值 税 专 用 发 票)",
            "广东增值税专用发票",
            "增 值 税\n专 用 发 票",
        )):
            with self.subTest(title=title):
                path = invoice_pdf(self.directory / f"{12345678901234567890 + index}.pdf", title)
                record = engine.analyze_files([path])["files"][0]
                self.assertTrue(record.get("special_vat"))
                self.assertFalse(record["rail"])
                self.assertEqual(record["copies"], 2)
                self.assertIn("专用发票", record["classification"])

    def test_regular_titles_and_filename_do_not_make_special_invoice(self) -> None:
        for index, title in enumerate((
            "增值税普通发票", "增值税电子普通发票", "电子发票（普通发票）",
        )):
            with self.subTest(title=title):
                path = invoice_pdf(self.directory / f"专票{index}.pdf", title, "如需增值税专用发票请联系商家")
                record = engine.analyze_files([path])["files"][0]
                self.assertFalse(record.get("special_vat", False))
                self.assertEqual(record["copies"], 1)

    def test_folder_and_zip_use_independent_custom_counts(self) -> None:
        special = invoice_pdf(self.directory / "special.pdf", "增值税专用发票")
        regular = invoice_pdf(self.directory / "regular.pdf", "电子发票（普通发票）")
        rail = invoice_pdf(self.directory / "rail.pdf", "铁路电子客票")
        with zipfile.ZipFile(self.directory / "bundle.zip", "w") as archive:
            archive.write(special, "内部/专票.pdf")
        special.unlink()
        for copies in (1, 5, 99):
            with self.subTest(copies=copies):
                scan = engine.analyze_folder(self.directory, rail_copies=3, other_copies=4, special_copies=copies)
                by_name = {record["name"]: record for record in scan["files"]}
                self.assertEqual(by_name["bundle.zip / 内部/专票.pdf"]["copies"], copies)
                self.assertEqual(by_name[regular.name]["copies"], 4)
                self.assertEqual(by_name[rail.name]["copies"], 3)
                self.assertEqual((scan["special_vat_count"], scan["regular_count"], scan["rail_count"]), (1, 1, 1))
                self.assertEqual(scan["expected_jobs"], copies + 7)
                selected = engine.filter_scan_result(scan, {str(regular), str(rail)})
                self.assertEqual((selected["special_vat_count"], selected["regular_count"]), (1, 0))

    def test_special_copies_reject_invalid_values(self) -> None:
        path = invoice_pdf(self.directory / "special.pdf", "增值税专用发票")
        for copies in (0, -1, 100, 1.5, "2.5", "abc", True):
            with self.subTest(copies=copies), self.assertRaises(ValueError):
                engine.analyze_files([path], special_copies=copies)

    def test_special_print_manifest_uses_custom_copies(self) -> None:
        path = invoice_pdf(self.directory / "special.pdf", "增值税专用发票")
        scan = engine.analyze_files([path], special_copies=6)
        manifest = engine._render_manifest(
            scan, self.directory / "job", sample_first_rail=False, dpi=96, progress=None,
        )
        self.assertEqual(manifest["expected_jobs"], 6)
        self.assertEqual(manifest["jobs"][0]["copies"], 6)
        self.assertEqual(manifest["documents"][0]["copies"], 6)
