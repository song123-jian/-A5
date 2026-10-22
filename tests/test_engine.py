from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import printer_engine as engine


class PrinterEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="a5-ticket-tests-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def _blank_pdf(self, name: str, width: float, height: float) -> Path:
        path = self.temp / name
        writer = PdfWriter()
        writer.add_blank_page(width=width, height=height)
        with path.open("wb") as handle:
            writer.write(handle)
        return path

    def test_analyze_folder_rules_and_duplicates(self) -> None:
        self._blank_pdf("26329116805001786023.pdf", 600, 400)
        invoice = self._blank_pdf("invoice.pdf", 612, 792)
        shutil.copy2(invoice, self.temp / "invoice_copy.pdf")
        (self.temp / "archive.zip").write_bytes(b"not-a-real-zip")

        result = engine.analyze_folder(self.temp, rail_copies=2, other_copies=1)

        self.assertEqual(result["pdf_count"], 2)
        self.assertEqual(result["zip_excluded_count"], 1)
        self.assertEqual(result["rail_count"], 1)
        self.assertEqual(result["regular_count"], 1)
        self.assertEqual(result["source_pages"], 2)
        self.assertEqual(result["expected_jobs"], 3)
        self.assertEqual(result["total_amount"], "0.00")
        self.assertEqual(result["amount_recognized_count"], 0)
        self.assertEqual(result["amount_unrecognized_count"], 2)
        self.assertEqual(result["duplicate_groups"], [])
        self.assertEqual(result["duplicate_skipped_count"], 1)
        self.assertEqual(result["duplicate_imports"][0]["name"], "invoice_copy.pdf")

    def test_analyze_folder_recurses_and_preserves_relative_source_names(self) -> None:
        nested = self.temp / "部门" / "月份"
        nested.mkdir(parents=True)
        self._blank_pdf("invoice.pdf", 612, 792)
        self._blank_pdf("部门/invoice.pdf", 420, 595)
        self._blank_pdf("部门/月份/invoice.pdf", 340, 227)
        Image.new("RGB", (240, 320), "white").save(nested / "receipt.PNG")
        with zipfile.ZipFile(nested / "bundle.zip", "w") as archive:
            archive.write(self.temp / "invoice.pdf", "内部/member.pdf")
            archive.writestr("readme.txt", "ignored")
        (nested / "invalid.zip").write_bytes(b"not a ZIP")
        (nested / "notes.txt").write_text("ignored", encoding="utf-8")
        emitted = []

        result = engine.analyze_folder(
            self.temp, on_record=lambda record, _index, _total: emitted.append(record["name"])
        )

        expected = {"invoice.pdf", "部门/invoice.pdf", "部门/月份/invoice.pdf",
                    "部门/月份/receipt.PNG"}
        self.assertEqual(result["pdf_count"], 4)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["zip_count"], 2)
        self.assertEqual(result["zip_internal_count"], 1)
        self.assertEqual(result["source_pages"], 4)
        self.assertEqual({record["name"] for record in result["files"]}, expected)
        self.assertEqual(set(emitted), expected)
        self.assertEqual(result["zip_excluded"], ["部门/月份/invalid.zip"])
        self.assertTrue(all(Path(record["path"]).is_file() for record in result["files"]))
        self.assertEqual(result["duplicate_groups"], [])
        self.assertEqual(result["duplicate_skipped_count"], 1)
        self.assertEqual(result["duplicate_imports"][0]["kept_name"], "invoice.pdf")

    def test_nested_identical_images_and_zips_keep_distinct_sources(self) -> None:
        first = self.temp / "first"
        second = self.temp / "second"
        first.mkdir()
        second.mkdir()
        image = first / "receipt.png"
        Image.new("RGB", (240, 320), "white").save(image)
        shutil.copy2(image, second / image.name)
        member = self._blank_pdf("member.pdf", 612, 792)
        archive = first / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(member, "member.pdf")
        shutil.copy2(archive, second / archive.name)

        result = engine.analyze_folder(self.temp)

        self.assertEqual(result["pdf_count"], 2)
        self.assertEqual(len({record["path"] for record in result["files"]}), 2)
        self.assertEqual({record["name"] for record in result["files"]},
                         {"member.pdf", "first/receipt.png"})
        self.assertEqual(result["duplicate_skipped_count"], 3)
        self.assertTrue(all(Path(item["path"]).is_file() for item in result["duplicate_imports"]))

    def test_zip_duplicate_prefers_outside_pdf_and_reports_skipped_source(self) -> None:
        invoice = self._blank_pdf("invoice.pdf", 612, 792)
        archive = self.temp / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(invoice, "renamed.pdf")
        emitted = []
        result = engine.analyze_files(
            [archive, invoice], on_record=lambda record, *_: emitted.append(record["path"])
        )
        self.assertEqual(result["pdf_count"], 1)
        self.assertEqual(emitted, [str(invoice)])
        self.assertEqual(result["files"][0]["path"], str(invoice))
        self.assertEqual(result["duplicate_skipped_count"], 1)
        self.assertEqual(result["duplicate_imports"][0]["name"], "bundle.zip / renamed.pdf")
        self.assertEqual(set(result["imported_paths"]), {str(invoice), str(archive)})
        self.assertTrue(invoice.is_file() and archive.is_file())

    def test_same_filename_with_different_content_is_not_removed(self) -> None:
        (self.temp / "other").mkdir()
        first = self._blank_pdf("invoice.pdf", 612, 792)
        second = self._blank_pdf("other/invoice.pdf", 420, 595)
        result = engine.analyze_files([first, second])
        self.assertEqual(result["pdf_count"], 2)
        self.assertEqual(result["duplicate_skipped_count"], 0)

    def test_zip_only_keeps_unique_pdf_members(self) -> None:
        first = self._blank_pdf("first.pdf", 612, 792)
        second = self._blank_pdf("second.pdf", 420, 595)
        archive = self.temp / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(first, "one.pdf")
            handle.write(first, "copy.pdf")
            handle.write(second, "two.pdf")
        result = engine.analyze_files([archive])
        self.assertEqual(result["pdf_count"], 2)
        self.assertEqual(result["duplicate_skipped_count"], 1)
        self.assertTrue(all(record.get("source_archive") == str(archive) for record in result["files"]))

    def test_validation_rehashes_files_and_rejects_new_duplicates(self) -> None:
        first = self._blank_pdf("first.pdf", 612, 792)
        second = self._blank_pdf("second.pdf", 420, 595)
        scan = engine.analyze_files([first, second])
        second.write_bytes(first.read_bytes())
        with patch.object(engine, "_execute_manifest", return_value={"success": True}) as execute:
            result = engine.validate_print_scan(scan, "Test printer")
        self.assertFalse(result["success"])
        self.assertEqual(result["duplicate_groups"], [["first.pdf", "second.pdf"]])
        self.assertTrue(any("重复" in message for message in result["errors"]))
        execute.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows directory junction regression")
    def test_analyze_folder_skips_directory_junctions(self) -> None:
        self._blank_pdf("invoice.pdf", 612, 792)
        link = self.temp / "loop"
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(self.temp)],
                       capture_output=True, check=True)
        self.assertTrue(link.is_junction())
        messages = []

        result = engine.analyze_folder(self.temp, progress=messages.append)

        self.assertEqual(result["pdf_count"], 1)
        self.assertTrue(any("跳过目录链接" in message and "loop" in message for message in messages))

    def test_analyze_folder_reports_unreadable_subdirectory(self) -> None:
        self._blank_pdf("invoice.pdf", 612, 792)
        blocked = self.temp / "blocked"
        blocked.mkdir()
        original_scandir = os.scandir

        def restricted_scandir(path):
            if Path(path) == blocked:
                raise PermissionError(13, "Access denied", str(blocked))
            return original_scandir(path)

        with patch.object(engine.os, "scandir", side_effect=restricted_scandir):
            with self.assertRaisesRegex(OSError, "blocked"):
                engine.analyze_folder(self.temp)

    def test_analyze_folder_can_cancel_before_directory_enumeration(self) -> None:
        self._blank_pdf("invoice.pdf", 612, 792)
        with engine.PrintController() as controller:
            def cancel_scan(message):
                if "正在扫描文件夹及子文件夹" in message:
                    controller.cancel()

            with patch.object(engine.os, "scandir", wraps=os.scandir) as enumerator:
                with self.assertRaisesRegex(RuntimeError, "任务已取消"):
                    engine.analyze_folder(self.temp, progress=cancel_scan)
                enumerator.assert_not_called()

    def test_analyze_folder_empty_nested_directories_report_no_documents(self) -> None:
        (self.temp / "a" / "b").mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "没有 PDF 文件"):
            engine.analyze_folder(self.temp)

    def test_analyze_files_deduplicates_paths_and_excludes_zip(self) -> None:
        invoice = self._blank_pdf("invoice.pdf", 612, 792)
        archive = self.temp / "archive.zip"
        archive.write_bytes(b"not-a-real-zip")
        ignored = self.temp / "notes.txt"
        ignored.write_text("ignored", encoding="utf-8")

        result = engine.analyze_files(
            [invoice, archive, invoice, ignored, self.temp / "missing.pdf"]
        )

        self.assertEqual(result["source_type"], "files")
        self.assertEqual(result["folder"], "导入文件")
        self.assertEqual(result["pdf_count"], 1)
        self.assertEqual(result["zip_excluded_count"], 1)
        self.assertEqual(result["zip_excluded"], ["archive.zip"])
        self.assertEqual(
            set(result["imported_paths"]), {str(invoice.resolve()), str(archive.resolve())}
        )

    def test_analyze_files_requires_at_least_one_pdf(self) -> None:
        archive = self.temp / "archive.zip"
        archive.write_bytes(b"not-a-real-zip")

        with self.assertRaisesRegex(RuntimeError, "导入文件中没有 PDF"):
            engine.analyze_files([archive])

    def test_analyze_files_reads_pdf_members_from_zip(self) -> None:
        member_pdf = self._blank_pdf("member.pdf", 612, 792)
        archive = self.temp / "tickets.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.write(member_pdf, "nested/member.pdf")
            handle.writestr("nested/readme.txt", "ignored")

        result = engine.analyze_files([archive])

        self.assertEqual(result["pdf_count"], 1)
        self.assertEqual(result["zip_count"], 1)
        self.assertEqual(result["zip_internal_count"], 1)
        self.assertEqual(result["zip_excluded_count"], 0)
        record = result["files"][0]
        self.assertEqual(record["archive_member"], "nested/member.pdf")
        self.assertIn("tickets.zip / nested/member.pdf", record["name"])
        self.assertTrue(Path(record["path"]).is_file())
        self.assertEqual(result["imported_paths"], [str(archive.resolve())])

    def _zip_mixed_amount_documents(self) -> Path:
        paid = self.temp / "paid.pdf"
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=400)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 20 350 Td (\24512.50) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        with paid.open("wb") as handle:
            writer.write(handle)
        no_amount = self._blank_pdf("no-amount.pdf", 310, 400)
        archive = self.temp / "mixed.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(paid, "01-paid.pdf")
            handle.write(no_amount, "sub/02-no-amount.PDF")
        return archive

    def test_nested_zips_keep_all_pdf_sources_and_render_without_amounts(self) -> None:
        first = self._blank_pdf("first.pdf", 300, 400)
        second = self._blank_pdf("second.pdf", 310, 400)
        third = self._blank_pdf("third.pdf", 320, 400)
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as handle:
            handle.write(third, "ticket.pdf")
        archive = self.temp / "outer.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(first, "direct.pdf")
            for name, pdf in (("a.zip", second), ("b.zip", third)):
                bundle = io.BytesIO()
                with zipfile.ZipFile(bundle, "w") as nested:
                    nested.write(pdf, "ticket.pdf")
                    nested.writestr("ticket.ofd", b"not a supported document")
                    if name == "a.zip":
                        nested.writestr("deeper.zip", inner.getvalue())
                handle.writestr(name, bundle.getvalue())
        original_hash = engine._sha256(archive)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}):
            scan = engine.analyze_files([archive])
            manifest = engine._render_manifest(scan, self.temp / "jobs", False, 150, None)
        self.assertEqual(scan["pdf_count"], 3)
        self.assertEqual(scan["zip_count"], 1)
        self.assertEqual(scan["zip_internal_count"], 4)
        self.assertEqual(scan["zip_excluded_count"], 0)
        self.assertEqual(scan["duplicate_skipped_count"], 1)
        self.assertEqual(scan["amount_unrecognized_count"], 3)
        self.assertEqual({item["archive_member"] for item in scan["files"]},
                         {"direct.pdf", "a.zip / ticket.pdf", "b.zip / ticket.pdf"})
        self.assertEqual({item["source_archive"] for item in scan["files"]}, {str(archive)})
        self.assertEqual(manifest["expected_jobs"], 3)
        self.assertEqual(engine._sha256(archive), original_hash)

    def test_bad_nested_zip_does_not_hide_later_nested_pdf(self) -> None:
        pdf = self._blank_pdf("ticket.pdf", 310, 400)
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as nested:
            nested.write(pdf, "ticket.pdf")
        archive = self.temp / "outer.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("01-bad.zip", b"not a zip")
            handle.writestr("02-good.zip", bundle.getvalue())
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}):
            scan = engine.analyze_files([archive])
        self.assertEqual(scan["pdf_count"], 1)
        self.assertEqual(scan["zip_member_excluded_count"], 1)
        self.assertEqual(scan["zip_member_excluded"][0]["archive_member"], "01-bad.zip")
        self.assertEqual(scan["files"][0]["archive_member"], "02-good.zip / ticket.pdf")

    def test_nested_zip_limits_are_shared_and_report_skipped_members(self) -> None:
        pdf = self._blank_pdf("ticket.pdf", 310, 400)
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as nested:
            nested.write(pdf, "ticket.pdf")
            nested.writestr("too-deep.zip", bundle.getvalue())
        archive = self.temp / "outer.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(pdf, "direct.pdf")
            handle.writestr("nested.zip", bundle.getvalue())
            handle.write(pdf, "later.pdf")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}), \
                patch.object(engine, "ZIP_MAX_NESTING_DEPTH", 1):
            scan = engine.analyze_files([archive])
        self.assertTrue(any("嵌套" in item["reason"] for item in scan["zip_member_excluded"]))
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}), \
                patch.object(engine, "ZIP_MAX_PDF_MEMBERS", 1):
            scan = engine.analyze_files([archive])
        self.assertEqual(scan["pdf_count"], 1)
        self.assertEqual(scan["zip_internal_count"], 1)
        self.assertTrue(any("数量" in item["reason"] for item in scan["zip_member_excluded"]))
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}), \
                patch.object(engine, "ZIP_MAX_TOTAL_BYTES", pdf.stat().st_size):
            scan = engine.analyze_files([archive])
        self.assertEqual(scan["zip_internal_count"], 1)
        self.assertTrue(any("已读取内容" in item["reason"] for item in scan["zip_member_excluded"]))

    def test_zip_gbk_member_names_are_readable_and_utf8_names_are_preserved(self) -> None:
        pdf = self._blank_pdf("ticket.pdf", 310, 400)
        archive = self.temp / "names.zip"

        class LegacyZipInfo(zipfile.ZipInfo):
            def _encodeFilenameFlags(self):
                return self.filename.encode("gbk"), self.flag_bits

        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(LegacyZipInfo("滴滴电子发票.pdf"), pdf.read_bytes())
            handle.writestr("铁路票据.pdf", self._blank_pdf("other.pdf", 320, 400).read_bytes())
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}):
            scan = engine.analyze_files([archive])
        self.assertEqual({item["archive_member"] for item in scan["files"]},
                         {"滴滴电子发票.pdf", "铁路票据.pdf"})

    def test_zip_no_amount_pdfs_are_included_in_print_manifest(self) -> None:
        archive = self._zip_mixed_amount_documents()
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}):
            scan = engine.analyze_files([archive])
            manifest = engine._render_manifest(scan, self.temp / "jobs", False, 150, None)
        self.assertEqual(scan["pdf_count"], 2)
        self.assertEqual(scan["files"][0]["amount"], "12.50")
        self.assertIsNone(scan["files"][1]["amount"])
        self.assertEqual({job["file"] for job in manifest["jobs"]},
                         {record["name"] for record in scan["files"]})
        self.assertEqual(manifest["expected_jobs"], 2)

    def test_zip_unreadable_member_does_not_hide_later_no_amount_pdf(self) -> None:
        archive = self._zip_mixed_amount_documents()
        with zipfile.ZipFile(archive, "a") as handle:
            handle.writestr("03-bad.pdf", b"not a PDF")
            handle.write(self._blank_pdf("later.pdf", 320, 400), "sub/04-later.pdf")
        original = zipfile.ZipFile.read

        def read_member(handle, member, *args, **kwargs):
            if getattr(member, "filename", member) == "sub/02-no-amount.PDF":
                raise zipfile.BadZipFile("Bad CRC-32 for member")
            return original(handle, member, *args, **kwargs)

        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}), \
                patch.object(zipfile.ZipFile, "read", read_member):
            scan = engine.analyze_files([archive])
        self.assertEqual({record["archive_member"] for record in scan["files"]},
                         {"01-paid.pdf", "sub/04-later.pdf"})
        self.assertEqual(scan["zip_member_excluded_count"], 2)
        self.assertEqual({item["archive_member"] for item in scan["zip_member_excluded"]},
                         {"sub/02-no-amount.PDF", "03-bad.pdf"})

    def test_zip_invalid_pdf_keeps_remaining_no_amount_documents(self) -> None:
        archive = self._zip_mixed_amount_documents()
        with zipfile.ZipFile(archive, "a") as handle:
            handle.writestr("03-bad.pdf", b"not a PDF")
            handle.write(self._blank_pdf("later.pdf", 320, 400), "04-later.pdf")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}):
            scan = engine.analyze_files([archive])
            manifest = engine._render_manifest(scan, self.temp / "jobs", False, 150, None)
        self.assertEqual(scan["pdf_count"], 3)
        self.assertEqual(scan["amount_recognized_count"], 1)
        self.assertEqual(scan["amount_unrecognized_count"], 2)
        self.assertEqual(scan["zip_member_excluded_count"], 1)
        self.assertEqual(scan["zip_member_excluded"][0]["archive_member"], "03-bad.pdf")
        self.assertTrue(scan["zip_member_excluded"][0]["reason"])
        self.assertEqual(len(manifest["jobs"]), 3)

    def test_zip_crc_failure_does_not_stop_later_documents(self) -> None:
        archive = self._zip_mixed_amount_documents()
        later = self._blank_pdf("later.pdf", 320, 400)
        with zipfile.ZipFile(archive, "a") as handle:
            handle.write(later, "04-later.pdf")
        with zipfile.ZipFile(archive) as handle:
            info = handle.getinfo("sub/02-no-amount.PDF")
        payload = bytearray(archive.read_bytes())
        start = info.header_offset
        name_length = int.from_bytes(payload[start + 26:start + 28], "little")
        extra_length = int.from_bytes(payload[start + 28:start + 30], "little")
        payload[start + 30 + name_length + extra_length] ^= 1
        archive.write_bytes(payload)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}):
            scan = engine.analyze_files([archive])
        self.assertEqual(scan["pdf_count"], 2)
        self.assertEqual(scan["zip_member_excluded_count"], 1)
        self.assertIn("CRC", scan["zip_member_excluded"][0]["reason"])
        self.assertIn("04-later.pdf", [record["archive_member"] for record in scan["files"]])

    def test_zip_deflate_failure_does_not_stop_later_documents(self) -> None:
        archive = self._zip_mixed_amount_documents()
        compressed = self.temp / "deflated.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(
                compressed, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            for info in source.infolist():
                handle.writestr(info.filename, source.read(info))
            handle.write(self._blank_pdf("later.pdf", 320, 400), "04-later.pdf")
        with zipfile.ZipFile(compressed) as handle:
            info = handle.getinfo("sub/02-no-amount.PDF")
        payload = bytearray(compressed.read_bytes())
        start = info.header_offset
        name_length = int.from_bytes(payload[start + 26:start + 28], "little")
        extra_length = int.from_bytes(payload[start + 28:start + 30], "little")
        payload[start + 30 + name_length + extra_length] = 7  # Invalid DEFLATE block type.
        compressed.write_bytes(payload)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}):
            scan = engine.analyze_files([compressed])
        self.assertEqual(scan["pdf_count"], 2)
        self.assertEqual(scan["zip_member_excluded_count"], 1)
        self.assertIn("04-later.pdf", [record["archive_member"] for record in scan["files"]])

    def test_zip_with_only_invalid_pdfs_reports_failure(self) -> None:
        archive = self.temp / "invalid-only.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("invalid.pdf", b"invalid PDF")
        messages = []
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}), \
                self.assertRaisesRegex(RuntimeError, "没有可读取的 PDF"):
            engine.analyze_files([archive], progress=messages.append)
        self.assertTrue(any("ZIP 内 PDF 已跳过" in message for message in messages))

    def test_images_are_cached_for_printing_and_exported_to_pdf(self) -> None:
        image = self.temp / "receipt.png"
        Image.new("RGB", (320, 240), (240, 240, 240)).save(image)

        result = engine.analyze_files([image])

        self.assertEqual(result["pdf_count"], 1)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["imported_paths"], [str(image.resolve())])
        record = result["files"][0]
        self.assertEqual(record["name"], "receipt.png")
        self.assertEqual(record["source_image"], str(image.resolve()))
        self.assertTrue(Path(record["path"]).is_file())
        self.assertEqual(Path(record["path"]).suffix.lower(), ".pdf")

        exported = self.temp / "exported.pdf"
        export_result = engine.export_scan_pdf(result, exported)
        self.assertTrue(export_result["success"])
        self.assertEqual(export_result["files"], 1)
        self.assertEqual(export_result["pages"], 1)
        self.assertEqual(len(engine.PdfReader(exported).pages), 1)

    def test_parse_page_selection_supports_ranges_and_all_pages(self) -> None:
        self.assertEqual(engine.parse_page_selection("1, 3-5", 6), [1, 3, 4, 5])
        self.assertEqual(engine.parse_page_selection("", 3, all_pages=True), [1, 2, 3])
        with self.assertRaisesRegex(ValueError, "超出范围"):
            engine.parse_page_selection("1,7", 6)

    def test_stamp_pdf_creates_normal_and_seam_copies_without_mutating_source(self) -> None:
        source = self.temp / "source.pdf"
        normal = self.temp / "normal.pdf"
        seam = self.temp / "seam.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_blank_page(width=612, height=792)
        with source.open("wb") as handle:
            writer.write(handle)
        stamp = self.temp / "stamp.png"
        Image.new("RGBA", (64, 64), (220, 30, 60, 180)).save(stamp)
        source_bytes = source.read_bytes()

        normal_result = engine.stamp_pdf(
            source, stamp, normal, page_numbers=[2], mode="normal"
        )
        seam_result = engine.stamp_pdf(source, stamp, seam, all_pages=True, mode="seam")

        self.assertEqual(normal_result["pages"], [2])
        self.assertEqual(seam_result["pages"], [1, 2])
        self.assertEqual(len(engine.PdfReader(normal).pages), 2)
        self.assertEqual(len(engine.PdfReader(seam).pages), 2)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertTrue(normal.is_file())
        self.assertTrue(seam.is_file())

    def test_stamp_pdf_preserves_unselected_pages_and_reports_selected_order(self) -> None:
        source = self.temp / "three-pages.pdf"
        output = self.temp / "selected-pages.pdf"
        writer = PdfWriter()
        for size in ((612, 792), (500, 700), (612, 792)):
            writer.add_blank_page(width=size[0], height=size[1])
        with source.open("wb") as handle:
            writer.write(handle)
        stamp = self.temp / "stamp-wide.png"
        Image.new("RGBA", (160, 80), (20, 90, 200, 180)).save(stamp)

        result = engine.stamp_pdf(source, stamp, output, page_numbers=[3, 1], mode="seam")

        self.assertEqual(result["pages"], [1, 3])
        stamped_reader = engine.PdfReader(output)
        self.assertEqual(len(stamped_reader.pages), 3)
        self.assertEqual(float(stamped_reader.pages[1].mediabox.width), 500.0)
        self.assertEqual(float(stamped_reader.pages[1].mediabox.height), 700.0)

    def test_extract_amount_prefers_labeled_total(self) -> None:
        amount, evidence = engine._extract_amount(
            "金额 ¥1,100.00 税额 ¥134.50 价税合计（小写） ¥1,234.50"
        )

        self.assertEqual(amount, "1234.50")
        self.assertIn("价税合计", evidence or "")

    def test_extract_amount_from_rail_ticket(self) -> None:
        amount, evidence = engine._extract_amount(
            "铁路电子客票\n票价：￥553.00元", rail=True
        )

        self.assertEqual(amount, "553.00")
        self.assertIn("票价", evidence or "")

    def test_extract_amount_uses_largest_currency_candidate(self) -> None:
        amount, _ = engine._extract_amount("¥100.00 ¥6.00 ¥106.00")

        self.assertEqual(amount, "106.00")

    def test_extract_amount_returns_none_without_evidence(self) -> None:
        amount, evidence = engine._extract_amount("订单编号 1234567890")

        self.assertIsNone(amount)
        self.assertIsNone(evidence)

    def test_filter_scan_result_removes_only_selected_records(self) -> None:
        rail_path = self._blank_pdf("26329116805001786023.pdf", 600, 400)
        regular_path = self._blank_pdf("invoice.pdf", 612, 792)
        scan = engine.analyze_folder(self.temp, rail_copies=2, other_copies=1)
        for record in scan["files"]:
            record["amount"] = "553.00" if record["rail"] else "123.45"

        filtered = engine.filter_scan_result(scan, {str(rail_path)})

        self.assertEqual(scan["pdf_count"], 2)
        self.assertEqual(len(scan["files"]), 2)
        self.assertEqual(filtered["pdf_count"], 1)
        self.assertEqual(filtered["rail_count"], 0)
        self.assertEqual(filtered["regular_count"], 1)
        self.assertEqual(filtered["expected_jobs"], 1)
        self.assertEqual(filtered["total_amount"], "123.45")
        self.assertEqual(filtered["amount_recognized_count"], 1)
        self.assertEqual(filtered["files"][0]["path"], str(regular_path))
        self.assertTrue(rail_path.exists())
        self.assertTrue(regular_path.exists())

    def test_filter_scan_result_allows_empty_print_list(self) -> None:
        path = self._blank_pdf("invoice.pdf", 612, 792)
        scan = engine.analyze_folder(self.temp)

        filtered = engine.filter_scan_result(scan, {str(path)})

        self.assertEqual(filtered["pdf_count"], 0)
        self.assertEqual(filtered["expected_jobs"], 0)
        self.assertEqual(filtered["total_amount"], "0.00")
        self.assertEqual(filtered["files"], [])
        self.assertTrue(path.exists())

    def test_safe_stub_is_ascii_and_bounded(self) -> None:
        value = engine._safe_stub("花小猪打车行程单_2026.pdf")
        self.assertLessEqual(len(value), 24)
        self.assertRegex(value, r"^[0-9A-Za-z_-]+$")

    def test_sample_manifest_is_one_job(self) -> None:
        self._blank_pdf("26329116805001786023.pdf", 600, 400)
        self._blank_pdf("invoice.pdf", 612, 792)
        scan = engine.analyze_folder(self.temp)
        job_dir = self.temp / "job"
        job_dir.mkdir()

        manifest = engine._render_manifest(
            scan,
            temp_dir=job_dir,
            sample_first_rail=True,
            dpi=150,
            progress=None,
        )

        self.assertEqual(manifest["expected_jobs"], 1)
        self.assertEqual(manifest["paper_size"], "A5")
        self.assertFalse(manifest["duplex"])
        self.assertEqual(manifest["orientation_mode"], "auto")
        self.assertEqual(manifest["scale_mode"], "fit")
        self.assertEqual(manifest["render_dpi"], 150)
        self.assertEqual(len(manifest["jobs"]), 1)
        self.assertEqual(len(manifest["documents"]), 1)
        self.assertTrue(manifest["jobs"][0]["rail"])
        self.assertEqual(manifest["jobs"][0]["copies"], 1)

    def test_manifest_accepts_a4_and_normalizes_case(self) -> None:
        self._blank_pdf("invoice.pdf", 612, 792)
        scan = engine.analyze_folder(self.temp)
        job_dir = self.temp / "a4-job"
        job_dir.mkdir()

        manifest = engine._render_manifest(
            scan,
            temp_dir=job_dir,
            sample_first_rail=False,
            dpi=72,
            progress=None,
            paper_size="a4",
        )

        self.assertEqual(manifest["paper_size"], "A4")
        self.assertEqual(manifest["expected_jobs"], 1)

    def test_invalid_paper_size_is_rejected_before_rendering(self) -> None:
        self._blank_pdf("invoice.pdf", 612, 792)
        scan = engine.analyze_folder(self.temp)

        with self.assertRaisesRegex(ValueError, "A4 或 A5"):
            engine._render_manifest(
                scan,
                temp_dir=self.temp / "invalid-job",
                sample_first_rail=False,
                dpi=72,
                progress=None,
                paper_size="Letter",
            )

    def test_print_presets_have_expected_quality_and_duplex_rules(self) -> None:
        smart = engine.get_print_preset("智能")
        self.assertEqual(smart["dpi"], 600)
        self.assertEqual(smart["orientation_mode"], "auto")
        self.assertEqual(smart["scale_mode"], "smart")
        self.assertEqual(engine.get_print_preset("标准")["dpi"], 600)
        self.assertEqual(engine.get_print_preset("高清")["dpi"], 720)
        self.assertTrue(engine.get_print_preset("高清")["high_quality"])
        self.assertEqual(engine.get_print_preset("快速")["dpi"], 300)
        self.assertTrue(engine.get_print_preset("双面")["duplex"])
        expected_modes = {
            "横向实际": ("landscape", "actual"),
            "纵向实际": ("portrait", "actual"),
            "横向适配": ("landscape", "fit"),
            "纵向适配": ("portrait", "fit"),
        }
        for name, (orientation_mode, scale_mode) in expected_modes.items():
            preset = engine.get_print_preset(name)
            self.assertEqual(preset["dpi"], 600)
            self.assertFalse(preset["duplex"])
            self.assertEqual(preset["orientation_mode"], orientation_mode)
            self.assertEqual(preset["scale_mode"], scale_mode)
        self.assertEqual(
            engine.get_print_preset("unknown")["name"], engine.DEFAULT_PRINT_PRESET
        )

    def test_calibration_pdf_has_100_mm_square_in_both_orientations(self) -> None:
        points_per_mm = 72.0 / 25.4
        for orientation, expected_size in (
            ("portrait", (120.0, 160.0)),
            ("landscape", (160.0, 120.0)),
        ):
            with self.subTest(orientation=orientation):
                path = engine.create_calibration_pdf(
                    self.temp / f"calibration-{orientation}.pdf", orientation
                )
                reader = engine.PdfReader(path)
                page = reader.pages[0]
                self.assertAlmostEqual(
                    float(page.mediabox.width), expected_size[0] * points_per_mm, places=2
                )
                self.assertAlmostEqual(
                    float(page.mediabox.height), expected_size[1] * points_per_mm, places=2
                )
                content = page.get_contents().get_data()
                self.assertIn(b"283.46 283.46 re S", content)
                self.assertEqual(content.count(b" l S"), 204)
                text = page.extract_text()
                self.assertIn("PRINT CALIBRATION - 100 mm SQUARE", text)
                self.assertIn("Each minor tick is 1 mm", text)

    def test_calibration_print_uses_fixed_rules_and_cleans_generated_pdf(self) -> None:
        source_paths: list[Path] = []

        def submit(scan: dict, **settings: object) -> dict:
            record = scan["files"][0]
            source = Path(record["path"])
            source_paths.append(source)
            self.assertTrue(source.is_file())
            self.assertEqual(record["copies"], 4)
            self.assertEqual(record["pages"], 1)
            return {"success": True, "expected_jobs": 4}

        with patch.object(engine, "print_scan", side_effect=submit) as print_scan:
            result = engine.print_calibration_page(
                printer="Test printer",
                paper_size="A4",
                copies=4,
                orientation_mode="landscape",
            )

        settings = print_scan.call_args.kwargs
        self.assertEqual(settings["printer"], "Test printer")
        self.assertEqual(settings["paper_size"], "A4")
        self.assertEqual(settings["dpi"], 600)
        self.assertFalse(settings["duplex"])
        self.assertFalse(settings["high_quality"])
        self.assertEqual(settings["orientation_mode"], "landscape")
        self.assertEqual(settings["scale_mode"], "actual")
        self.assertEqual(result["mode"], "calibration")
        self.assertEqual(result["calibration_copies"], 4)
        self.assertFalse(source_paths[0].exists())

    def test_calibration_print_rejects_invalid_copy_counts(self) -> None:
        for copies in (0, 100, "abc", 1.5):
            with self.subTest(copies=copies), self.assertRaisesRegex(
                ValueError, "1 到 99"
            ):
                engine.print_calibration_page("Test printer", copies=copies)

    def test_manifest_accepts_forced_orientation_and_actual_size(self) -> None:
        self._blank_pdf("invoice.pdf", 612, 792)
        scan = engine.analyze_folder(self.temp)

        manifest = engine._render_manifest(
            scan,
            temp_dir=self.temp / "actual-job",
            sample_first_rail=False,
            dpi=600,
            progress=None,
            orientation_mode="LANDSCAPE",
            scale_mode="ACTUAL",
            margin_mm=12.5,
        )

        self.assertEqual(manifest["orientation_mode"], "landscape")
        self.assertEqual(manifest["scale_mode"], "actual")
        self.assertEqual(manifest["margin_mm"], 12.5)
        self.assertEqual(manifest["render_dpi"], 600)
        with self.assertRaisesRegex(ValueError, "方向模式"):
            engine._render_manifest(
                scan,
                temp_dir=self.temp / "bad-orientation",
                sample_first_rail=False,
                dpi=600,
                progress=None,
                orientation_mode="diagonal",
            )
        with self.assertRaisesRegex(ValueError, "缩放模式"):
            engine._render_manifest(
                scan,
                temp_dir=self.temp / "bad-scale",
                sample_first_rail=False,
                dpi=600,
                progress=None,
                scale_mode="stretch",
            )

    def test_duplex_manifest_groups_pages_into_one_job_per_copy(self) -> None:
        path = self.temp / "two-pages.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_blank_page(width=792, height=612)
        with path.open("wb") as handle:
            writer.write(handle)
        scan = engine.analyze_folder(self.temp, other_copies=2)
        manifest = engine._render_manifest(
            scan,
            temp_dir=self.temp / "duplex-job",
            sample_first_rail=False,
            dpi=150,
            progress=None,
            paper_size="A4",
            duplex=True,
        )

        self.assertTrue(manifest["duplex"])
        self.assertEqual(manifest["expected_jobs"], 2)
        self.assertEqual(len(manifest["jobs"]), 2)
        self.assertEqual(len(manifest["documents"]), 1)
        self.assertEqual(
            [page["page"] for page in manifest["documents"][0]["pages"]], [1, 2]
        )
        self.assertEqual(
            engine.expected_print_jobs(scan, duplex=False), 4
        )
        self.assertEqual(
            engine.expected_print_jobs(scan, duplex=True), 2
        )

    def test_uniform_copy_override_does_not_mutate_original_scan(self) -> None:
        self._blank_pdf("invoice.pdf", 612, 792)
        scan = engine.analyze_folder(self.temp, other_copies=1)

        copied = engine.with_uniform_copies(scan, 6)

        self.assertEqual(scan["files"][0]["copies"], 1)
        self.assertEqual(copied["files"][0]["copies"], 6)
        self.assertEqual(copied["expected_jobs"], 6)
        with self.assertRaisesRegex(ValueError, "1 到 99"):
            engine.with_uniform_copies(scan, 100)

    def test_print_script_selects_and_verifies_a4_or_a5(self) -> None:
        self.assertIn("[System.Drawing.Printing.PaperKind]::A4", engine.POWERSHELL_PRINT)
        self.assertIn("[System.Drawing.Printing.PaperKind]::A5", engine.POWERSHELL_PRINT)
        self.assertIn("Driver did not apply $PaperName", engine.POWERSHELL_PRINT)
        self.assertNotIn("$Document.DefaultPageSettings.PaperSize = $A5", engine.POWERSHELL_PRINT)
        self.assertIn("$Probe.CanDuplex", engine.POWERSHELL_PRINT)
        self.assertIn("[System.Drawing.Printing.Duplex]::Vertical", engine.POWERSHELL_PRINT)
        self.assertIn("add_QueryPageSettings", engine.POWERSHELL_PRINT)
        self.assertIn("HighQualityBicubic", engine.POWERSHELL_PRINT)
        self.assertIn("Resolve-Landscape", engine.POWERSHELL_PRINT)
        self.assertIn("Get-CorrectedPrintableArea", engine.POWERSHELL_PRINT)
        self.assertIn("Get-PrintRectangle", engine.POWERSHELL_PRINT)
        self.assertIn("$MarginMm", engine.POWERSHELL_PRINT)
        self.assertIn("margin_mm", engine.POWERSHELL_PRINT)
        self.assertIn("Actual-size image exceeds printable area", engine.POWERSHELL_PRINT)
        self.assertLess(
            engine.POWERSHELL_PRINT.index("Print preflight failed"),
            engine.POWERSHELL_PRINT.index("$BeforeJobs ="),
        )

    @unittest.skipUnless(os.name == "nt", "PowerShell layout test requires Windows")
    def test_print_layout_supports_fit_actual_centering_and_overflow_rejection(self) -> None:
        command = engine.POWERSHELL_LAYOUT + r'''
$Fit = Get-PrintRectangle 0 0 800 1000 3600 2400 600 "fit"
$Actual = Get-PrintRectangle 0 0 800 1000 3600 2400 600 "actual"
$SmartActual = Get-PrintRectangle 0 0 800 1000 3600 2400 600 "smart"
$SmartFit = Get-PrintRectangle 0 0 800 1000 7200 4800 600 "smart"
$Overflow = $null
try { Get-PrintRectangle 0 0 500 300 3600 2400 600 "actual" | Out-Null }
catch { $Overflow = $_.Exception.Message }
[ordered]@{
    fit = @($Fit.Left, $Fit.Top, $Fit.Width, $Fit.Height)
    actual = @($Actual.Left, $Actual.Top, $Actual.Width, $Actual.Height)
    smart_actual = @($SmartActual.ScaleMode, $SmartActual.Left, $SmartActual.Top, $SmartActual.Width, $SmartActual.Height)
    smart_fit = @($SmartFit.ScaleMode, $SmartFit.Left, $SmartFit.Top, $SmartFit.Width, $SmartFit.Height)
    overflow = $Overflow
} | ConvertTo-Json -Compress
'''
        layout = json.loads(engine._run_powershell(command))
        self.assertAlmostEqual(layout["fit"][0], 0.0, places=2)
        self.assertAlmostEqual(layout["fit"][2], 800.0, places=2)
        self.assertAlmostEqual(layout["actual"][0], 100.0, places=2)
        self.assertAlmostEqual(layout["actual"][1], 300.0, places=2)
        self.assertAlmostEqual(layout["actual"][2], 600.0, places=2)
        self.assertAlmostEqual(layout["actual"][3], 400.0, places=2)
        self.assertEqual(layout["smart_actual"][0], "actual")
        self.assertAlmostEqual(layout["smart_actual"][3], 600.0, places=2)
        self.assertEqual(layout["smart_fit"][0], "fit")
        self.assertAlmostEqual(layout["smart_fit"][3], 800.0, places=2)
        self.assertIn("exceeds printable area", layout["overflow"])

    @unittest.skipUnless(os.name == "nt", "validation workflow requires Windows")
    def test_validate_print_scan_runs_preflight_without_print_submission(self) -> None:
        self._blank_pdf("invoice.pdf", 612, 792)
        scan = engine.analyze_folder(self.temp)
        result = {
            "success": True,
            "mode": "validate",
            "printer": "Test printer",
            "paper_size": "A4",
            "checked_pages": 1,
            "failed_pages": 0,
            "errors": [],
            "results": [{"file": "invoice.pdf", "page": 1, "valid": True}],
        }

        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp)}), patch.object(
            engine, "_execute_manifest", return_value=result
        ) as execute_manifest, patch.object(engine, "print_scan") as print_scan:
            validated = engine.validate_print_scan(
                scan,
                printer="Test printer",
                paper_size="A4",
                dpi=600,
                orientation_mode="landscape",
                scale_mode="actual",
            )

        self.assertTrue(validated["success"])
        self.assertEqual(validated["expected_jobs"], 1)
        self.assertFalse(validated["temp_retained"])
        self.assertEqual(
            list((self.temp / "A5TicketPrinter" / "jobs").iterdir()), []
        )
        self.assertTrue(execute_manifest.call_args.kwargs["validate_only"])
        self.assertEqual(execute_manifest.call_args.args[0]["orientation_mode"], "landscape")
        self.assertEqual(execute_manifest.call_args.args[0]["scale_mode"], "actual")
        print_scan.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "PowerShell parser test requires Windows")
    def test_embedded_powershell_parses(self) -> None:
        script = self.temp / "print.ps1"
        script.write_text(engine.POWERSHELL_PRINT, encoding="utf-8-sig")
        escaped = str(script).replace("'", "''")
        output = engine._run_powershell(
            "$tokens=$null;$errors=$null;"
            f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',"
            "[ref]$tokens,[ref]$errors)|Out-Null;$errors.Count"
        )
        self.assertEqual(output.strip(), "0")


if __name__ == "__main__":
    unittest.main()
