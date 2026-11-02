from __future__ import annotations

import base64
import io
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile
import zlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


APP_NAME = "打印快捷工具"
DEFAULT_PRINTER = ""
RAIL_KEYWORDS = ("铁路电子客票", "铁路客票", "电子客票", "中国铁路", "12306")
SPECIAL_VAT_TITLES = ("增值税专用发票", "增值税电子专用发票")
REGULAR_VAT_TITLES = ("增值税普通发票", "增值税电子普通发票", "电子发票（普通发票）")
NUMERIC_TICKET_NAME = re.compile(r"^\d{20}$")
AMOUNT_NUMBER = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
ProgressCallback = Callable[[str], None]
ScanRecordCallback = Callable[[dict[str, Any], int, int], None]
_print_context = threading.local()


class PrintController:
    """Own one worker's cancellation state and any running print subprocess."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> PrintController:
        _print_context.controller = self
        return self

    def __exit__(self, *_args: object) -> None:
        del _print_context.controller

    def check(self) -> None:
        if self.cancelled.is_set():
            raise RuntimeError("任务已取消，停止继续提交打印作业。")

    def start_process(self, *args, **kwargs) -> subprocess.Popen:
        with self._lock:
            self.check()
            self.process = subprocess.Popen(*args, **kwargs)
            return self.process

    def release(self, process: subprocess.Popen) -> None:
        with self._lock:
            if self.process is process:
                self.process = None

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            process = self.process
        if process is not None and process.poll() is None:
            process.terminate()


def _check_cancelled() -> None:
    controller = getattr(_print_context, "controller", None)
    if controller is not None:
        controller.check()


ZIP_MAX_MEMBER_BYTES = 100 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 500 * 1024 * 1024
ZIP_MAX_PDF_MEMBERS = 200
ZIP_MAX_NESTING_DEPTH = 8
ZIP_MAX_NESTED_ARCHIVES = 200
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"})
SUPPORTED_FILE_EXTENSIONS = frozenset({".pdf", ".zip", *IMAGE_EXTENSIONS})
PAPER_SIZES = ("A4", "A5")
ORIENTATION_MODES = ("auto", "landscape", "portrait")
SCALE_MODES = ("fit", "actual", "smart")
STAMP_MODES = ("normal", "seam")
DEFAULT_PRINT_PRESET = "标准"
PRINT_PRESETS: dict[str, dict[str, Any]] = {
    "智能": {
        "dpi": 600,
        "duplex": False,
        "high_quality": False,
        "orientation_mode": "auto",
        "scale_mode": "smart",
        "description": "单面 · 600 DPI · 自动方向 · 自动选择实际大小或适合边距 · 居中",
    },
    "标准": {
        "dpi": 600,
        "duplex": False,
        "high_quality": False,
        "orientation_mode": "auto",
        "scale_mode": "fit",
        "description": "单面 · 600 DPI · 自动横纵向 · 适合边距 · 居中",
    },
    "高清": {
        "dpi": 720,
        "duplex": False,
        "high_quality": True,
        "orientation_mode": "auto",
        "scale_mode": "fit",
        "description": "单面 · 720 DPI · 高质量缩放 · 自动横纵向 · 适合边距 · 居中",
    },
    "快速": {
        "dpi": 300,
        "duplex": False,
        "high_quality": False,
        "orientation_mode": "auto",
        "scale_mode": "fit",
        "description": "单面 · 300 DPI · 自动横纵向 · 适合边距 · 居中",
    },
    "双面": {
        "dpi": 600,
        "duplex": True,
        "high_quality": False,
        "orientation_mode": "auto",
        "scale_mode": "fit",
        "description": "双面长边 · 600 DPI · 自动横纵向 · 适合边距 · 居中",
    },
    "横向实际": {
        "dpi": 600,
        "duplex": False,
        "high_quality": False,
        "orientation_mode": "landscape",
        "scale_mode": "actual",
        "description": "单面 · 600 DPI · 自动横向 · 实际大小 · 居中",
    },
    "纵向实际": {
        "dpi": 600,
        "duplex": False,
        "high_quality": False,
        "orientation_mode": "portrait",
        "scale_mode": "actual",
        "description": "单面 · 600 DPI · 自动纵向 · 实际大小 · 居中",
    },
    "横向适配": {
        "dpi": 600,
        "duplex": False,
        "high_quality": False,
        "orientation_mode": "landscape",
        "scale_mode": "fit",
        "description": "单面 · 600 DPI · 自动横向 · 适合打印边距大小 · 居中",
    },
    "纵向适配": {
        "dpi": 600,
        "duplex": False,
        "high_quality": False,
        "orientation_mode": "portrait",
        "scale_mode": "fit",
        "description": "单面 · 600 DPI · 自动纵向 · 适合打印边距大小 · 居中",
    },
}


def _normalize_paper_size(paper_size: str) -> str:
    normalized = str(paper_size).strip().upper()
    if normalized not in PAPER_SIZES:
        raise ValueError("纸张规格必须为 A4 或 A5。")
    return normalized


def _normalize_orientation_mode(orientation_mode: str) -> str:
    normalized = str(orientation_mode).strip().lower()
    if normalized not in ORIENTATION_MODES:
        raise ValueError("方向模式必须为 auto、landscape 或 portrait。")
    return normalized


def _normalize_scale_mode(scale_mode: str) -> str:
    normalized = str(scale_mode).strip().lower()
    if normalized not in SCALE_MODES:
        raise ValueError("缩放模式必须为 fit、actual 或 smart。")
    return normalized


def _normalize_margin_mm(margin_mm: Any) -> float:
    """Validate the extra user-controlled print margin in millimetres."""

    try:
        value = float(margin_mm)
    except (TypeError, ValueError):
        raise ValueError("打印边距必须是 0 到 50 毫米之间的数字。") from None
    if not math.isfinite(value) or value < 0 or value > 50:
        raise ValueError("打印边距必须是 0 到 50 毫米之间的数字。")
    return round(value, 2)


def normalize_margin_mm(margin_mm: Any) -> float:
    """Validate and normalize a user-controlled print margin in millimetres."""

    return _normalize_margin_mm(margin_mm)


def get_print_preset(name: str) -> dict[str, Any]:
    preset_name = str(name).strip()
    if preset_name not in PRINT_PRESETS:
        preset_name = DEFAULT_PRINT_PRESET
    return {"name": preset_name, **PRINT_PRESETS[preset_name]}


def _normalize_stamp_mode(mode: str) -> str:
    normalized = str(mode).strip().lower()
    aliases = {
        "普通盖章": "normal",
        "普通": "normal",
        "normal": "normal",
        "齐缝章": "seam",
        "齐缝": "seam",
        "seam": "seam",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in STAMP_MODES:
        raise ValueError("盖章模式必须为普通盖章或齐缝章。")
    return normalized


def parse_page_selection(
    value: str | None, total_pages: int, all_pages: bool = False
) -> list[int]:
    """Parse a human page expression such as ``1,3-5`` into sorted page numbers."""

    total_pages = int(total_pages)
    if total_pages < 1:
        raise ValueError("PDF 没有可盖章的页面。")
    if all_pages:
        return list(range(1, total_pages + 1))

    expression = str(value or "").strip().replace("，", ",").replace("；", ",")
    if not expression:
        raise ValueError("请输入盖章页数，例如 1,3-5，或勾选全部页数。")
    selected: set[int] = set()
    for token in re.split(r"[,\s]+", expression):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            parts = token.split("-")
            if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
                raise ValueError(f"页码范围无效：{token}")
            start, end = (int(part.strip()) for part in parts)
            if start > end:
                start, end = end, start
            if start < 1 or end > total_pages:
                raise ValueError(f"页码超出范围：{token}，PDF 共 {total_pages} 页。")
            selected.update(range(start, end + 1))
        elif token.isdigit():
            selected.add(int(token))
        else:
            raise ValueError(f"页码无效：{token}")
    if not selected:
        raise ValueError("没有解析出有效页码。")
    invalid = sorted(page for page in selected if page < 1 or page > total_pages)
    if invalid:
        raise ValueError(
            f"页码超出范围：{','.join(str(page) for page in invalid)}，PDF 共 {total_pages} 页。"
        )
    return sorted(selected)


def _stamp_position(
    page_width: float,
    page_height: float,
    stamp_width: float,
    stamp_height: float,
    location: str = "bottom-right",
) -> tuple[float, float]:
    margin = max(18.0, min(page_width, page_height) * 0.04)
    locations = {
        "top-left": (margin, page_height - margin - stamp_height),
        "top-right": (page_width - margin - stamp_width, page_height - margin - stamp_height),
        "bottom-left": (margin, margin),
        "bottom-right": (page_width - margin - stamp_width, margin),
    }
    if location not in locations:
        raise ValueError("盖章位置必须为左上角、右上角、左下角或右下角。")
    x, y = locations[location]
    return max(0.0, x), max(0.0, y)


def _stamp_overlay_page(
    image: Image.Image,
    page_width: float,
    page_height: float,
    mode: str,
    slice_index: int,
    slice_count: int,
    stamp_size_pt: float,
    location: str,
) -> Any:
    """Build one-page PDF overlay for a normal or edge seal."""

    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=(page_width, page_height))
    prepared = image.convert("RGBA")
    stamp_width = stamp_size_pt
    stamp_height = stamp_size_pt * prepared.height / max(1, prepared.width)
    if stamp_width > page_width or stamp_height > page_height:
        prepared.close()
        raise ValueError("印章尺寸超过页面大小，请减小印章。")
    if mode == "seam" and slice_count > 1:
        # Clip consecutive vertical strips from the full seal. Keep all of the
        # image so the selected page edges can be assembled into one seal.
        strip_width = stamp_width / slice_count
        x = page_width - strip_width
        y = (page_height - stamp_height) / 2.0
        canvas.saveState()
        clip = canvas.beginPath()
        clip.rect(x, y, strip_width, stamp_height)
        canvas.clipPath(clip, stroke=0, fill=0)
        canvas.drawImage(
            ImageReader(prepared),
            x - slice_index * strip_width,
            y,
            width=stamp_width,
            height=stamp_height,
            preserveAspectRatio=False,
            mask="auto",
        )
        canvas.restoreState()
    else:
        x, y = _stamp_position(page_width, page_height, stamp_width, stamp_height, location)
        canvas.drawImage(
            ImageReader(prepared),
            x,
            y,
            width=stamp_width,
            height=stamp_height,
            preserveAspectRatio=False,
            mask="auto",
        )
    prepared.close()
    canvas.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def stamp_pdf(
    source_path: str | Path,
    stamp_image_path: str | Path,
    output_path: str | Path,
    page_numbers: list[int] | tuple[int, ...] | None = None,
    all_pages: bool = False,
    mode: str = "normal",
    stamp_size_mm: float = 28.0,
    location: str = "bottom-right",
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Create a stamped PDF without modifying the source file."""

    source = Path(source_path).expanduser().resolve()
    stamp_path = Path(stamp_image_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"源 PDF 不存在：{source}")
    if not stamp_path.is_file():
        raise FileNotFoundError(f"印章图片不存在：{stamp_path}")
    if source == output:
        raise ValueError("盖章输出文件不能覆盖原 PDF。")
    if not math.isfinite(stamp_size_mm) or not 5 <= stamp_size_mm <= 100:
        raise ValueError("印章大小必须在 5 到 100 mm 之间。")
    normalized_mode = _normalize_stamp_mode(mode)
    reader = PdfReader(str(source))
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise ValueError("加密 PDF 无法盖章，请先使用可打开的 PDF。")
    selected = parse_page_selection(
        ",".join(str(page) for page in page_numbers or ()),
        len(reader.pages),
        all_pages=all_pages,
    )
    selected_set = set(selected)
    if normalized_mode == "seam" and len(selected) < 2:
        raise ValueError("齐缝章至少需要选择两页。")
    selected_indices = {page: index for index, page in enumerate(selected)}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        stamp_image = Image.open(stamp_path)
        if stamp_image.width < 2 or stamp_image.height < 2:
            raise ValueError("印章图片尺寸过小。")
        writer = PdfWriter()
        for page_index, page in enumerate(reader.pages, 1):
            page = writer.add_page(page)
            if page_index in selected_set:
                if page.rotation:
                    page.transfer_rotation_to_content()
                page_width = float(page.cropbox.width)
                page_height = float(page.cropbox.height)
                overlay = _stamp_overlay_page(
                    stamp_image,
                    page_width,
                    page_height,
                    normalized_mode,
                    selected_indices[page_index],
                    len(selected),
                    stamp_size_mm * 72.0 / 25.4,
                    location,
                )
                page.merge_translated_page(overlay, float(page.cropbox.left), float(page.cropbox.bottom))
                _progress(
                    progress,
                    f"已盖章 {selected_indices[page_index] + 1}/{len(selected)} 页：第 {page_index} 页",
                )
        if reader.metadata:
            writer.add_metadata({str(key): str(value) for key, value in reader.metadata.items() if value is not None})
        with output.open("wb") as handle:
            writer.write(handle)
    finally:
        try:
            stamp_image.close()
        except UnboundLocalError:
            pass
    return {
        "source_path": str(source),
        "output_path": str(output),
        "pages": selected,
        "mode": normalized_mode,
        "all_pages": bool(all_pages),
        "page_count": len(reader.pages),
    }


def expected_print_jobs(scan: dict[str, Any], duplex: bool = False) -> int:
    if duplex:
        return sum(int(record["copies"]) for record in scan.get("files", []))
    return sum(
        int(record["pages"]) * int(record["copies"])
        for record in scan.get("files", [])
    )


def with_uniform_copies(scan: dict[str, Any], copies: int) -> dict[str, Any]:
    copies = int(copies)
    if copies < 1 or copies > 99:
        raise ValueError("复印份数必须在 1 到 99 之间。")
    overridden = dict(scan)
    overridden["files"] = [
        {**record, "copies": copies} for record in scan.get("files", [])
    ]
    return filter_scan_result(overridden, set())


def _calibration_page_stream(width_mm: float, height_mm: float) -> bytes:
    points_per_mm = 72.0 / 25.4
    square_size = 100.0
    square_x = (width_mm - square_size) / 2.0
    square_y = (height_mm - square_size) / 2.0
    x = square_x * points_per_mm
    y = square_y * points_per_mm
    side = square_size * points_per_mm
    commands = ["q", "0 G", "0.55 w"]

    def line(x1: float, y1: float, x2: float, y2: float) -> None:
        commands.append(
            f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S"
        )

    def label(x_mm: float, y_mm: float, size: int, value: str) -> None:
        escaped = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(
            f"BT /F1 {size} Tf 1 0 0 1 {x_mm * points_per_mm:.2f} "
            f"{y_mm * points_per_mm:.2f} Tm ({escaped}) Tj ET"
        )

    page_width_mm = width_mm
    page_height_mm = height_mm
    commands.append(
        f"{2 * points_per_mm:.2f} {2 * points_per_mm:.2f} "
        f"{(page_width_mm - 4) * points_per_mm:.2f} "
        f"{(page_height_mm - 4) * points_per_mm:.2f} re S"
    )
    label(5, page_height_mm - 7, 9, "PRINT CALIBRATION - 100 mm SQUARE")
    label(5, 4, 7, "Check the square with a ruler. Each minor tick is 1 mm.")
    commands.append(f"{x:.2f} {y:.2f} {side:.2f} {side:.2f} re S")

    for millimeter in range(101):
        tick = 3.0 if millimeter % 10 == 0 else 1.5
        offset = millimeter * points_per_mm
        line(x + offset, y, x + offset, y + tick * points_per_mm)
        line(x, y + offset, x + tick * points_per_mm, y + offset)

    center = 50.0 * points_per_mm
    cross = 3.0 * points_per_mm
    line(x + center - cross, y + center, x + center + cross, y + center)
    line(x + center, y + center - cross, x + center, y + center + cross)
    for millimeter in (0, 50, 100):
        offset = millimeter * points_per_mm
        label_x = square_x + millimeter - (4 if millimeter == 100 else 1)
        label(label_x, square_y + 4, 6, str(millimeter))
        label(square_x + 4, square_y + millimeter + 1, 6, str(millimeter))

    commands.append("Q")
    return "\n".join(commands).encode("ascii")


def create_calibration_pdf(output_path: Path, orientation: str = "portrait") -> Path:
    orientation = _normalize_orientation_mode(orientation)
    if orientation == "auto":
        orientation = "portrait"
    if orientation == "landscape":
        width_mm, height_mm = 160.0, 120.0
    else:
        width_mm, height_mm = 120.0, 160.0

    writer = PdfWriter()
    points_per_mm = 72.0 / 25.4
    page = writer.add_blank_page(
        width=width_mm * points_per_mm,
        height=height_mm * points_per_mm,
    )
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    page[NameObject("/Resources")] = resources
    stream = DecodedStreamObject()
    stream.set_data(_calibration_page_stream(width_mm, height_mm))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with Path(output_path).open("wb") as handle:
        writer.write(handle)
    return Path(output_path)


def _normalize_copy_count(copies: Any) -> int:
    raw = str(copies).strip()
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError("校准页份数必须是 1 到 99 之间的整数。")
    value = int(raw)
    if value < 1 or value > 99:
        raise ValueError("校准页份数必须是 1 到 99 之间的整数。")
    return value


POWERSHELL_LAYOUT = r'''
function Resolve-Landscape {
    param([object]$Item, [string]$OrientationMode)
    switch ($OrientationMode) {
        "auto" { return [bool]$Item.landscape }
        "landscape" { return $true }
        "portrait" { return $false }
        default { throw "Unsupported orientation mode: $OrientationMode" }
    }
}

function Get-CorrectedPrintableArea {
    param([object]$PageSettings)
    $Raw = $PageSettings.PrintableArea
    if ($PageSettings.Landscape -and $Raw.Height -gt $Raw.Width) {
        return [pscustomobject]@{
            Raw = $Raw
            X = [single]$Raw.Y
            Y = [single]$Raw.X
            Width = [single]$Raw.Height
            Height = [single]$Raw.Width
        }
    }
    return [pscustomobject]@{
        Raw = $Raw
        X = [single]$Raw.X
        Y = [single]$Raw.Y
        Width = [single]$Raw.Width
        Height = [single]$Raw.Height
    }
}

function Get-PrintRectangle {
    param(
        [double]$AreaX,
        [double]$AreaY,
        [double]$AreaWidth,
        [double]$AreaHeight,
        [int]$ImageWidth,
        [int]$ImageHeight,
        [int]$RenderDpi,
        [string]$ScaleMode,
        [double]$MarginMm = 0
    )
    if ($AreaWidth -le 0 -or $AreaHeight -le 0) { throw "Printable area is invalid" }
    if ($ImageWidth -le 0 -or $ImageHeight -le 0) { throw "Image dimensions are invalid" }
    if ($RenderDpi -le 0) { throw "Render DPI is invalid" }
    if ($MarginMm -lt 0 -or $MarginMm -gt 50) { throw "Print margin is invalid" }

    # PrintDocument coordinates use 1/100 inch. Keep the user margin inside
    # the driver's printable area so fit/actual modes share one layout path.
    $MarginUnits = $MarginMm * 100.0 / 25.4
    $EffectiveAreaX = $AreaX + $MarginUnits
    $EffectiveAreaY = $AreaY + $MarginUnits
    $EffectiveAreaWidth = $AreaWidth - (2.0 * $MarginUnits)
    $EffectiveAreaHeight = $AreaHeight - (2.0 * $MarginUnits)
    if ($EffectiveAreaWidth -le 0 -or $EffectiveAreaHeight -le 0) { throw "Print margin leaves no printable area" }

    # PrintDocument coordinates use 1/100 inch.
    $ActualWidth = [double]$ImageWidth / [double]$RenderDpi * 100.0
    $ActualHeight = [double]$ImageHeight / [double]$RenderDpi * 100.0
    $EffectiveScaleMode = $ScaleMode
    if ($ScaleMode -eq "smart") {
        if ($ActualWidth -le ($EffectiveAreaWidth + 0.1) -and $ActualHeight -le ($EffectiveAreaHeight + 0.1)) {
            $EffectiveScaleMode = "actual"
        } else {
            $EffectiveScaleMode = "fit"
        }
    }

    if ($EffectiveScaleMode -eq "actual") {
        $DrawWidth = $ActualWidth
        $DrawHeight = $ActualHeight
    } elseif ($EffectiveScaleMode -eq "fit") {
        $Scale = [Math]::Min(($EffectiveAreaWidth / $ImageWidth), ($EffectiveAreaHeight / $ImageHeight))
        $DrawWidth = [double]$ImageWidth * $Scale
        $DrawHeight = [double]$ImageHeight * $Scale
    } else {
        throw "Unsupported scale mode: $ScaleMode"
    }

    $Left = $EffectiveAreaX + (($EffectiveAreaWidth - $DrawWidth) / 2.0)
    $Top = $EffectiveAreaY + (($EffectiveAreaHeight - $DrawHeight) / 2.0)
    if ($Left -lt ($EffectiveAreaX - 0.1) -or $Top -lt ($EffectiveAreaY - 0.1) -or ($Left + $DrawWidth) -gt ($EffectiveAreaX + $EffectiveAreaWidth + 0.1) -or ($Top + $DrawHeight) -gt ($EffectiveAreaY + $EffectiveAreaHeight + 0.1)) {
        if ($EffectiveScaleMode -eq "actual") {
            throw "Actual-size image exceeds printable area"
        }
        throw "Image rectangle exceeds printable area"
    }

    return [pscustomobject]@{
        Left = [single]$Left
        Top = [single]$Top
        Width = [single]$DrawWidth
        Height = [single]$DrawHeight
        ScaleMode = $EffectiveScaleMode
    }
}
'''


POWERSHELL_PRINT = r'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
''' + POWERSHELL_LAYOUT + r'''

$PrinterName = $env:A5_PRINT_PRINTER
$ManifestPath = $env:A5_PRINT_MANIFEST
$ResultPath = $env:A5_PRINT_RESULT
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

$Probe = New-Object System.Drawing.Printing.PrinterSettings
$Probe.PrinterName = $PrinterName
if (-not $Probe.IsValid) { throw "Printer is invalid: $PrinterName" }
$PaperName = ([string]$Manifest.paper_size).ToUpperInvariant()
$PaperKind = switch ($PaperName) {
    "A4" { [System.Drawing.Printing.PaperKind]::A4 }
    "A5" { [System.Drawing.Printing.PaperKind]::A5 }
    default { throw "Unsupported paper size: $PaperName" }
}
$Paper = @($Probe.PaperSizes | Where-Object { $_.Kind -eq $PaperKind } | Select-Object -First 1)
if ($Paper.Count -ne 1) { throw "Printer driver has no $PaperName paper" }
$UseDuplex = [bool]$Manifest.duplex
$HighQuality = [bool]$Manifest.high_quality
$OrientationMode = ([string]$Manifest.orientation_mode).ToLowerInvariant()
$ScaleMode = ([string]$Manifest.scale_mode).ToLowerInvariant()
$RenderDpi = [int]$Manifest.render_dpi
$MarginMm = [double]$Manifest.margin_mm
$ValidateOnly = ([string]$env:A5_PRINT_VALIDATE_ONLY -eq "1")
if ($OrientationMode -notin @("auto", "landscape", "portrait")) { throw "Unsupported orientation mode: $OrientationMode" }
if ($ScaleMode -notin @("fit", "actual", "smart")) { throw "Unsupported scale mode: $ScaleMode" }
if ($RenderDpi -le 0) { throw "Render DPI is invalid" }
if ($MarginMm -lt 0 -or $MarginMm -gt 50) { throw "Print margin is invalid" }
if ($UseDuplex -and -not $Probe.CanDuplex) { throw "Printer driver does not support duplex printing" }
$DuplexMode = if ($UseDuplex) {
    [System.Drawing.Printing.Duplex]::Vertical
} else {
    [System.Drawing.Printing.Duplex]::Simplex
}
$PrintUnits = @()
if ($UseDuplex) {
    foreach ($DocumentItem in @($Manifest.documents)) {
        $PrintUnits += $DocumentItem
    }
} else {
    foreach ($Item in @($Manifest.jobs)) {
        $PrintUnits += [pscustomobject]@{
            file = [string]$Item.file
            copies = [int]$Item.copies
            rail = [bool]$Item.rail
            document_stub = [string]$Item.document_stub
            pages = @($Item)
        }
    }
}

# Validate every rendered page against the selected printer before submitting any job.
$PreflightResults = @()
$PreflightErrors = @()
foreach ($PreflightItem in @($Manifest.jobs)) {
    $PageSettings = $null
    $Image = $null
    try {
        $PageSettings = $Probe.DefaultPageSettings.Clone()
        $PageSettings.PaperSize = $Paper[0]
        $PageSettings.Landscape = Resolve-Landscape $PreflightItem $OrientationMode
        $PageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)
        if ($PageSettings.PaperSize.Kind -ne $PaperKind) {
            throw "Driver did not apply $PaperName during preflight"
        }
        $PrintArea = Get-CorrectedPrintableArea $PageSettings
        $Image = [System.Drawing.Image]::FromFile([string]$PreflightItem.image)
        $Destination = Get-PrintRectangle $PrintArea.X $PrintArea.Y $PrintArea.Width $PrintArea.Height $Image.Width $Image.Height $RenderDpi $ScaleMode $MarginMm
        $PreflightResults += [pscustomobject]@{
            file = [string]$PreflightItem.file
            page = [int]$PreflightItem.page
            valid = $true
            landscape = [bool]$PageSettings.Landscape
            printable_area = @([Math]::Round($PrintArea.Width, 2), [Math]::Round($PrintArea.Height, 2))
            destination = @([Math]::Round($Destination.Width, 2), [Math]::Round($Destination.Height, 2))
            scale_mode = [string]$Destination.ScaleMode
            margin_mm = $MarginMm
            error = $null
        }
    } catch {
        $PreflightError = "{0} 第 {1} 页：{2}" -f [string]$PreflightItem.file, [int]$PreflightItem.page, $_.Exception.Message
        $PreflightErrors += $PreflightError
        $PreflightResults += [pscustomobject]@{
            file = [string]$PreflightItem.file
            page = [int]$PreflightItem.page
            valid = $false
            landscape = if ($null -ne $PageSettings) { [bool]$PageSettings.Landscape } else { $null }
            printable_area = @()
            destination = @()
            scale_mode = $null
            error = $PreflightError
        }
        if (-not $ValidateOnly) { throw ("Print preflight failed: {0}" -f $PreflightError) }
    } finally {
        if ($null -ne $Image) { $Image.Dispose() }
    }
}

if ($ValidateOnly) {
    $Output = [ordered]@{
        success = ($PreflightErrors.Count -eq 0)
        mode = "validate"
        printer = $PrinterName
        paper_size = $PaperName
        orientation_mode = $OrientationMode
        scale_mode = $ScaleMode
        margin_mm = $MarginMm
        render_dpi = $RenderDpi
        checked_pages = $PreflightResults.Count
        failed_pages = $PreflightErrors.Count
        errors = $PreflightErrors
        results = $PreflightResults
    }
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($ResultPath, ($Output | ConvertTo-Json -Depth 10), $Utf8NoBom)
    Write-Output ("VALIDATION_RESULT=" + (([pscustomobject]$Output | Select-Object success,checked_pages,failed_pages) | ConvertTo-Json -Compress))
    exit 0
}

$BeforeJobs = @(Get-PrintJob -PrinterName $PrinterName -ErrorAction SilentlyContinue)
$WpsBefore = @(Get-Process -Name wps,wpp,et -ErrorAction SilentlyContinue | Select-Object ProcessName,Id,StartTime)
$Results = @()
$JobCounter = 0
$FatalError = $null

:PrintLoop foreach ($Unit in $PrintUnits) {
    $Pages = @($Unit.pages)
    if ($Pages.Count -eq 0) { continue }
    for ($Copy = 1; $Copy -le [int]$Unit.copies; $Copy++) {
        $JobCounter++
        $PageLabel = if ($UseDuplex) { "pages" } else { "p" + [string]$Pages[0].page }
        $DocumentName = $PaperName + "TicketApp_" + $Manifest.batch_id + "_" + $JobCounter.ToString("D3") + "_" + [string]$Unit.document_stub + "_" + $PageLabel + "_c" + $Copy
        $State = [hashtable]::Synchronized(@{ Seen = $false; JobIds = @(); Metrics = @(); PageIndex = 0 })

        $Document = New-Object System.Drawing.Printing.PrintDocument
        $Document.PrinterSettings.PrinterName = $PrinterName
        $Document.PrinterSettings.Copies = 1
        $Document.PrinterSettings.Duplex = $DuplexMode
        $Document.PrintController = New-Object System.Drawing.Printing.StandardPrintController
        $Document.DocumentName = $DocumentName
        $Document.OriginAtMargins = $false
        $Document.DefaultPageSettings.PaperSize = $Paper[0]
        $Document.DefaultPageSettings.Landscape = Resolve-Landscape $Pages[0] $OrientationMode
        $Document.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)

        $QueryBlock = {
            param($Sender, $Event)
            $CurrentPage = $Pages[$State.PageIndex]
            $Event.PageSettings.PaperSize = $Paper[0]
            $Event.PageSettings.Landscape = Resolve-Landscape $CurrentPage $OrientationMode
            $Event.PageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)
        }.GetNewClosure()
        $HandlerBlock = {
            param($Sender, $Event)
            if ($Event.PageSettings.PaperSize.Kind -ne $PaperKind) {
                throw "Driver did not apply $PaperName"
            }
            $Item = $Pages[$State.PageIndex]
            $ImagePath = [string]$Item.image
            $Image = [System.Drawing.Image]::FromFile($ImagePath)
            try {
                $PrintArea = Get-CorrectedPrintableArea $Event.PageSettings
                $Raw = $PrintArea.Raw
                $AreaX = $PrintArea.X
                $AreaY = $PrintArea.Y
                $AreaWidth = $PrintArea.Width
                $AreaHeight = $PrintArea.Height

                $Destination = Get-PrintRectangle $AreaX $AreaY $AreaWidth $AreaHeight $Image.Width $Image.Height $RenderDpi $ScaleMode $MarginMm
                $Rectangle = New-Object System.Drawing.RectangleF($Destination.Left, $Destination.Top, $Destination.Width, $Destination.Height)
                if ($HighQuality) {
                    $Event.Graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
                    $Event.Graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                    $Event.Graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                    $Event.Graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
                }
                $Event.Graphics.DrawImage($Image, $Rectangle)
                $State.Metrics += [pscustomobject]@{
                    page = [int]$Item.page
                    paper = $Event.PageSettings.PaperSize.PaperName
                    landscape = $Event.PageSettings.Landscape
                    driver_area_raw = @([Math]::Round($Raw.X, 2), [Math]::Round($Raw.Y, 2), [Math]::Round($Raw.Width, 2), [Math]::Round($Raw.Height, 2))
                    corrected_area = @([Math]::Round($AreaX, 2), [Math]::Round($AreaY, 2), [Math]::Round($AreaWidth, 2), [Math]::Round($AreaHeight, 2))
                    source_pixels = @($Image.Width, $Image.Height)
                    orientation_mode = $OrientationMode
                    scale_mode = [string]$Destination.ScaleMode
                    margin_mm = $MarginMm
                    render_dpi = $RenderDpi
                    destination = @([Math]::Round($Destination.Left, 2), [Math]::Round($Destination.Top, 2), [Math]::Round($Destination.Width, 2), [Math]::Round($Destination.Height, 2))
                }
                $Observed = @(Get-PrintJob -PrinterName $PrinterName -ErrorAction SilentlyContinue | Where-Object { $_.DocumentName -eq $DocumentName })
                if ($Observed.Count -gt 0) {
                    $State.Seen = $true
                    $State.JobIds = @($Observed | Select-Object -ExpandProperty ID)
                }
                $State.PageIndex++
                $Event.HasMorePages = ($State.PageIndex -lt $Pages.Count)
            } finally {
                $Image.Dispose()
            }
        }.GetNewClosure()

        $QueryHandler = [System.Drawing.Printing.QueryPageSettingsEventHandler]$QueryBlock
        $Handler = [System.Drawing.Printing.PrintPageEventHandler]$HandlerBlock
        $Document.add_QueryPageSettings($QueryHandler)
        $Document.add_PrintPage($Handler)
        try {
            $Document.Print()
            Start-Sleep -Milliseconds 100
            $AfterOne = @(Get-PrintJob -PrinterName $PrinterName -ErrorAction SilentlyContinue | Where-Object { $_.DocumentName -eq $DocumentName })
            if ($AfterOne.Count -gt 0) {
                $State.Seen = $true
                $State.JobIds = @($AfterOne | Select-Object -ExpandProperty ID)
            }
            $Results += [pscustomobject]@{
                document = $DocumentName
                file = [string]$Unit.file
                page = [int]$Pages[0].page
                pages = @($Pages | ForEach-Object { [int]$_.page })
                copy = $Copy
                rail = [bool]$Unit.rail
                duplex = $UseDuplex
                submitted = $true
                queue_observed = [bool]$State.Seen
                job_ids = @($State.JobIds)
                metrics = $State.Metrics
                error = $null
            }
            Write-Output ("SUBMIT {0}/{1}: {2}" -f $JobCounter, $Manifest.expected_jobs, $DocumentName)
        } catch {
            $FatalError = $_.Exception.Message
            $Results += [pscustomobject]@{
                document = $DocumentName
                file = [string]$Unit.file
                page = [int]$Pages[0].page
                pages = @($Pages | ForEach-Object { [int]$_.page })
                copy = $Copy
                rail = [bool]$Unit.rail
                duplex = $UseDuplex
                submitted = $false
                queue_observed = [bool]$State.Seen
                job_ids = @($State.JobIds)
                metrics = $State.Metrics
                error = $FatalError
            }
        } finally {
            $Document.remove_QueryPageSettings($QueryHandler)
            $Document.remove_PrintPage($Handler)
            $Document.Dispose()
        }
        if ($FatalError) { break PrintLoop }
    }
}

Start-Sleep -Milliseconds 300
$AfterJobs = @(Get-PrintJob -PrinterName $PrinterName -ErrorAction SilentlyContinue)
$Prefix = $PaperName + "TicketApp_" + $Manifest.batch_id + "_"
$Remaining = @($AfterJobs | Where-Object { $_.DocumentName -like ($Prefix + "*") })
$WpsAfter = @(Get-Process -Name wps,wpp,et -ErrorAction SilentlyContinue | Select-Object ProcessName,Id,StartTime)
$Submitted = @($Results | Where-Object { $_.submitted }).Count
$ObservedCount = @($Results | Where-Object { $_.queue_observed }).Count
$Output = [ordered]@{
    success = ($Submitted -eq [int]$Manifest.expected_jobs -and -not $FatalError)
    batch_id = $Manifest.batch_id
    printer = $PrinterName
    expected_jobs = [int]$Manifest.expected_jobs
    submitted_jobs = $Submitted
    jobs_observed_in_queue = $ObservedCount
    queue_before_count = $BeforeJobs.Count
    queue_remaining_with_prefix = $Remaining.Count
    printer_status = (Get-Printer -Name $PrinterName).PrinterStatus.ToString()
    wps_before = $WpsBefore
    wps_after = $WpsAfter
    fatal_error = $FatalError
    results = $Results
}
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($ResultPath, ($Output | ConvertTo-Json -Depth 10), $Utf8NoBom)
Write-Output ("PRINT_RESULT=" + (([pscustomobject]$Output | Select-Object success,expected_jobs,submitted_jobs,jobs_observed_in_queue,queue_before_count,queue_remaining_with_prefix,printer_status,fatal_error) | ConvertTo-Json -Compress))
if ($FatalError) { exit 1 }
'''


def _progress(callback: ProgressCallback | None, message: str) -> None:
    _check_cancelled()
    if callback:
        callback(message)


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _powershell_executable() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        raise RuntimeError("未找到 Windows PowerShell。")
    return executable


def _run_powershell(command: str) -> str:
    setup = (
        "$OutputEncoding=[Console]::OutputEncoding="
        "New-Object System.Text.UTF8Encoding($false);"
    )
    encoded = base64.b64encode((setup + command).encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        [_powershell_executable(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        creationflags=_creation_flags(),
        check=False,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        raise RuntimeError(stderr or stdout or f"PowerShell 失败，退出码 {completed.returncode}")
    return stdout


def list_printers() -> list[dict[str, Any]]:
    raw = _run_powershell(
        "@(Get-Printer | Select-Object Name,PrinterStatus,DriverName,PortName)"
        "|ConvertTo-Json -Compress -Depth 4"
    )
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return sorted(data, key=lambda item: str(item.get("Name", "")).lower())


def get_default_printer() -> str:
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    try:
        get_default = ctypes.WinDLL("winspool.drv", use_last_error=True).GetDefaultPrinterW
        get_default.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        get_default.restype = wintypes.BOOL
        size = wintypes.DWORD()
        get_default(None, ctypes.byref(size))
        if not size.value:
            return ""
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if get_default(buffer, ctypes.byref(size)) else ""
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        return ""


def queue_count(printer: str) -> int:
    escaped = printer.replace("'", "''")
    raw = _run_powershell(
        f"@((Get-PrintJob -PrinterName '{escaped}' -ErrorAction SilentlyContinue)).Count"
    )
    return int(raw or "0")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ink_ratio(image: Image.Image) -> float:
    probe = image.convert("L")
    probe.thumbnail((700, 700), Image.Resampling.LANCZOS)
    histogram = probe.histogram()
    return sum(histogram[:250]) / max(1, probe.width * probe.height)


def _safe_stub(filename: str) -> str:
    stem = Path(filename).stem
    value = re.sub(r"[^0-9A-Za-z_-]", "_", stem)[:24]
    return value or "document"


def _zip_cache_root() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "A5TicketPrinter" / "zip_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _image_cache_root() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "A5TicketPrinter" / "image_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _materialize_images(
    images: list[Path] | tuple[Path, ...],
    progress: ProgressCallback | None = None,
) -> tuple[list[Path], list[Path], dict[str, dict[str, str]]]:
    """Convert supported image files to cached one-page PDFs for the PDF pipeline."""

    pdfs: list[Path] = []
    excluded: list[Path] = []
    sources: dict[str, dict[str, str]] = {}
    for image_path in images:
        image_path = Path(image_path).expanduser().resolve()
        try:
            image_hash = _sha256(image_path)
            source_key = hashlib.sha256(os.path.normcase(str(image_path)).encode("utf-8")).hexdigest()[:16]
            cache_dir = _image_cache_root() / image_hash[:24] / source_key
            cache_dir.mkdir(parents=True, exist_ok=True)
            target = cache_dir / f"{_safe_stub(image_path.name)}.pdf"
            if not target.exists() or target.stat().st_size == 0:
                with Image.open(image_path) as source:
                    dpi = source.info.get("dpi", (96, 96))
                    try:
                        dpi_x = float(dpi[0] if isinstance(dpi, tuple) else dpi)
                    except (TypeError, ValueError, IndexError):
                        dpi_x = 96.0
                    if not math.isfinite(dpi_x) or dpi_x <= 0:
                        dpi_x = 96.0
                    source.convert("RGB").save(target, "PDF", resolution=dpi_x)
            if not target.is_file() or target.stat().st_size == 0:
                raise OSError("图片 PDF 缓存为空")
            pdfs.append(target)
            sources[str(target.resolve())] = {"source_image": str(image_path)}
            _progress(progress, f"已读取图片：{image_path.name}")
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            excluded.append(image_path)
            _progress(progress, f"图片无法读取，已排除：{image_path.name}（{exc}）")
    return pdfs, excluded, sources


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """Recover legacy Chinese names without changing UTF-8 or unreadable names."""
    if not info.flag_bits & 0x800:
        try:
            return info.filename.encode("cp437").decode("gbk")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return info.filename


def _expand_zip_pdfs(
    zips: list[Path] | tuple[Path, ...],
    progress: ProgressCallback | None = None,
) -> tuple[list[Path], list[Path], dict[str, dict[str, str]], list[dict[str, str]]]:
    """Read PDFs through nested ZIPs with shared limits and member error isolation."""

    pdfs: list[Path] = []
    excluded: list[Path] = []
    sources: dict[str, dict[str, str]] = {}
    member_excluded: list[dict[str, str]] = []
    for archive in zips:
        found = 0
        total_bytes = 0
        pdf_members = 0
        nested_archives = 0
        stop_at_member_limit = False

        def read_members(handle: zipfile.ZipFile, prefix: str = "", depth: int = 0) -> None:
            nonlocal found, total_bytes, pdf_members, nested_archives, stop_at_member_limit
            for info in handle.infolist():
                if stop_at_member_limit:
                    return
                _check_cancelled()
                is_pdf = info.filename.lower().endswith(".pdf")
                is_zip = info.filename.lower().endswith(".zip")
                if info.is_dir() or not (is_pdf or is_zip):
                    continue
                member_name = _zip_member_name(info)
                member_path = f"{prefix} / {member_name}" if prefix else member_name
                nested_handle = None
                try:
                    if is_pdf:
                        if pdf_members >= ZIP_MAX_PDF_MEMBERS:
                            raise ValueError(f"PDF 文件数量超过 {ZIP_MAX_PDF_MEMBERS} 个")
                        pdf_members += 1
                    else:
                        if depth >= ZIP_MAX_NESTING_DEPTH:
                            raise ValueError(f"ZIP 嵌套超过 {ZIP_MAX_NESTING_DEPTH} 层限制")
                        if nested_archives >= ZIP_MAX_NESTED_ARCHIVES:
                            raise ValueError(f"嵌套 ZIP 数量超过 {ZIP_MAX_NESTED_ARCHIVES} 个")
                        nested_archives += 1
                    if info.flag_bits & 0x1:
                        raise ValueError("ZIP 成员已加密，需要密码")
                    if info.file_size > ZIP_MAX_MEMBER_BYTES:
                        raise ValueError("ZIP 内文件大小超过 100 MB 限制")
                    if total_bytes + info.file_size > ZIP_MAX_TOTAL_BYTES:
                        raise ValueError("ZIP 已读取内容超过 500 MB 限制")
                    if is_pdf:
                        target = cache_dir / f"{pdf_members:04d}_{_safe_stub(member_name)}.pdf"
                        if not target.exists() or target.stat().st_size != info.file_size:
                            target.write_bytes(handle.read(info))
                        if not target.is_file() or target.stat().st_size != info.file_size:
                            raise OSError("PDF 缓存大小不完整")
                    else:
                        nested_handle = zipfile.ZipFile(io.BytesIO(handle.read(info)))
                    total_bytes += info.file_size
                except (OSError, zipfile.BadZipFile, ValueError, RuntimeError, EOFError, zlib.error) as exc:
                    member_excluded.append({"source_archive": str(archive),
                                            "archive_member": member_path, "reason": str(exc)})
                    _progress(progress, f"ZIP 内文件无法读取，已跳过：{archive.name} / {member_path}（{exc}）")
                    if "文件数量超过" in str(exc) or "嵌套 ZIP 数量超过" in str(exc):
                        stop_at_member_limit = True
                        return
                    continue
                if nested_handle is not None:
                    # Recurse outside the read-error handler so cancellation propagates.
                    with nested_handle:
                        read_members(nested_handle, member_path, depth + 1)
                    continue
                pdfs.append(target)
                sources[str(target.resolve())] = {
                    "source_archive": str(archive),
                    "archive_member": member_path,
                }
                found += 1
                _progress(progress, f"已读取 ZIP 内部 PDF：{archive.name} / {member_path}")

        try:
            archive_hash = _sha256(archive)
            source_key = hashlib.sha256(os.path.normcase(str(archive)).encode("utf-8")).hexdigest()[:16]
            cache_dir = _zip_cache_root() / archive_hash[:24] / source_key
            cache_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as handle:
                read_members(handle)
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            _progress(progress, f"ZIP 无法读取，已排除：{archive.name}（{exc}）")
            found = 0
        if found == 0:
            excluded.append(archive)
    return pdfs, excluded, sources, member_excluded


def _decimal_amount(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if value <= 0 or value >= Decimal("10000000"):
        return None
    return value


def _extract_amount(text: str, rail: bool = False) -> tuple[str | None, str | None]:
    if not text.strip():
        return None, None

    normalized = text.replace("\u00a0", " ").replace("，", ",")
    compact = re.sub(r"[ \t]+", " ", normalized)
    patterns: list[tuple[int, re.Pattern[str]]] = [
        (
            100,
            re.compile(
                rf"价税合计(?:[（(]?小写[）)]?)?[^\d￥¥]{{0,40}}[￥¥]?\s*({AMOUNT_NUMBER})\s*元?",
                re.IGNORECASE,
            ),
        ),
        (
            90,
            re.compile(
                rf"(?:实付金额|应付金额|支付金额|合计金额|金额合计|票价)"
                rf"[^\d￥¥]{{0,20}}[￥¥]?\s*({AMOUNT_NUMBER})\s*元?",
                re.IGNORECASE,
            ),
        ),
    ]
    if rail:
        patterns.append(
            (
                85,
                re.compile(
                    rf"(?:金额|票款)[^\d￥¥]{{0,12}}[￥¥]?\s*({AMOUNT_NUMBER})\s*元?",
                    re.IGNORECASE,
                ),
            )
        )
    patterns.extend(
        [
            (
                70,
                re.compile(
                    rf"[￥¥]\s*({AMOUNT_NUMBER})\s*元?",
                    re.IGNORECASE,
                ),
            ),
            (
                60,
                re.compile(
                    rf"(?<![\d.])({AMOUNT_NUMBER})\s*元",
                    re.IGNORECASE,
                ),
            ),
        ]
    )

    candidates: list[tuple[int, Decimal, str]] = []
    for score, pattern in patterns:
        for match in pattern.finditer(compact):
            value = _decimal_amount(match.group(1))
            if value is None:
                continue
            evidence = re.sub(r"\s+", " ", match.group(0)).strip()
            candidates.append((score, value, evidence[:80]))

    if not candidates:
        return None, None
    best_score = max(score for score, _, _ in candidates)
    _, value, evidence = max(
        (candidate for candidate in candidates if candidate[0] == best_score),
        key=lambda candidate: candidate[1],
    )
    return f"{value:.2f}", evidence


def normalize_special_copies(copies: Any) -> int:
    raw = str(copies).strip()
    if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= 99:
        raise ValueError("专票份数必须是 1 到 99 之间的整数。")
    return int(raw)


def _is_special_vat_invoice(page_texts: list[str]) -> bool:
    for text in page_texts:
        compact = "".join(text.split()).replace("(", "（").replace(")", "）")
        special_positions = [compact.find(title) for title in SPECIAL_VAT_TITLES if title in compact]
        regular_positions = [compact.find(title) for title in REGULAR_VAT_TITLES if title in compact]
        if special_positions and (not regular_positions or min(special_positions) < min(regular_positions)):
            return True
    return False


def _analyze_paths(
    pdfs: list[Path] | tuple[Path, ...],
    zips: list[Path] | tuple[Path, ...],
    source_label: str,
    rail_copies: int = 2,
    other_copies: int = 1,
    progress: ProgressCallback | None = None,
    source_type: str = "folder",
    images: list[Path] | tuple[Path, ...] = (),
    on_record: ScanRecordCallback | None = None,
    special_copies: int = 2,
) -> dict[str, Any]:
    special_copies = normalize_special_copies(special_copies)
    if rail_copies < 1 or other_copies < 1:
        raise ValueError("打印份数必须大于等于 1。")
    pdfs = sorted(
        (Path(path).expanduser().resolve() for path in pdfs),
        key=lambda item: (item.name.lower(), str(item).lower()),
    )
    zips = sorted(
        (Path(path).expanduser().resolve() for path in zips),
        key=lambda item: (item.name.lower(), str(item).lower()),
    )
    original_pdfs = list(pdfs)
    original_images = list(images)
    image_pdfs, image_excluded, image_sources = _materialize_images(original_images, progress)
    pdfs = [*original_pdfs, *image_pdfs]
    zip_pdfs, zip_excluded, zip_sources, zip_member_excluded = _expand_zip_pdfs(zips, progress)
    pdfs = [*pdfs, *zip_pdfs]
    if not pdfs:
        if source_type == "files":
            raise RuntimeError("导入文件中没有 PDF 文件。")
        raise RuntimeError("所选文件夹及其子文件夹中没有 PDF 文件或可识别的图片。")

    source_root = Path(source_label) if source_type == "folder" else None

    def source_name(value: str | Path) -> str:
        source = Path(value)
        if source_root is not None:
            return source.relative_to(source_root).as_posix()
        return source.name

    sources = {**image_sources, **zip_sources}

    def display_name_for(path: Path) -> str:
        source_meta = sources.get(str(path.resolve()), {})
        if source_meta.get("source_archive"):
            return f"{source_name(source_meta['source_archive'])} / {source_meta['archive_member']}"
        return source_name(source_meta.get("source_image", path))

    # Prefer documents outside ZIPs, then the shallowest visible source path.
    pdfs.sort(key=lambda path: (
        bool(sources.get(str(path.resolve()), {}).get("source_archive")),
        display_name_for(path).count("/"),
        display_name_for(path).casefold(),
        str(path).casefold(),
    ))
    records: list[dict[str, Any]] = []
    seen_pdfs: dict[str, dict[str, Any]] = {}
    seen_images: dict[str, dict[str, Any]] = {}
    duplicate_imports: list[dict[str, str]] = []
    for index, path in enumerate(pdfs, 1):
        source_meta = sources.get(str(path.resolve()), {})
        display_name = display_name_for(path)
        _progress(progress, f"正在分析 {index}/{len(pdfs)}：{display_name}")
        pdf_digest = _sha256(path)
        image_digest = (
            _sha256(Path(source_meta["source_image"]))
            if source_meta.get("source_image") else ""
        )
        kept = seen_pdfs.get(pdf_digest) or seen_images.get(image_digest)
        if kept is not None:
            duplicate_imports.append({
                "name": display_name,
                "path": str(path),
                "kept_name": kept["name"],
                "kept_path": kept["path"],
                **source_meta,
            })
            _progress(progress, f"跳过重复导入：{display_name}；保留：{kept['name']}")
            continue
        warnings: list[str] = []
        page_texts: list[str] = []
        extraction_error: str | None = None
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted and reader.decrypt("") == 0:
                raise RuntimeError("加密 PDF 无法打开")
            for page_number, page in enumerate(reader.pages, 1):
                try:
                    page_texts.append(page.extract_text() or "")
                except Exception as exc:
                    page_texts.append("")
                    warnings.append(f"第 {page_number} 页文字提取失败：{exc}")
        except Exception as exc:
            extraction_error = str(exc)
            warnings.append(f"文字提取失败：{exc}")

        compact = "".join("\n".join(page_texts).split())
        matched = [keyword for keyword in RAIL_KEYWORDS if keyword in compact]
        numeric_fallback = NUMERIC_TICKET_NAME.fullmatch(path.stem) is not None
        special_vat = _is_special_vat_invoice(page_texts)
        rail = not special_vat and (bool(matched) or numeric_fallback)
        reason = (
            "票面标题：增值税专用发票" if special_vat else
            "票面关键词：" + "、".join(matched)
            if matched
            else ("20 位票号文件名" if numeric_fallback else "未发现铁路票标志")
        )
        amount_details: list[dict[str, Any]] = []
        for page_number, page_text in enumerate(page_texts, 1):
            amount, source = _extract_amount(page_text, rail=rail)
            amount_details.append(
                {"page": page_number, "amount": amount, "source": source}
            )
        recognized_amounts = [
            Decimal(item["amount"]) for item in amount_details if item["amount"]
        ]
        document_amount = (
            sum(recognized_amounts, Decimal("0.00"))
            if recognized_amounts
            else None
        )

        document = None
        page_details: list[dict[str, Any]] = []
        try:
            document = pdfium.PdfDocument(str(path))
            if len(document) == 0:
                raise ValueError("PDF 没有可打印页面")
            for page_number in range(len(document)):
                image = (
                    document[page_number]
                    .render(scale=96 / 72.0, rotation=0)
                    .to_pil()
                    .convert("RGB")
                )
                ratio = round(_ink_ratio(image), 6)
                if ratio < 0.00005:
                    warnings.append(f"第 {page_number + 1} 页接近空白")
                page_details.append(
                    {
                        "page": page_number + 1,
                        "preview_pixels": [image.width, image.height],
                        "landscape": image.width > image.height,
                        "ink_ratio": ratio,
                    }
                )
                image.close()
        except (OSError, ValueError, pdfium.PdfiumError) as exc:
            if not source_meta.get("source_archive"):
                raise
            zip_member_excluded.append({**source_meta, "reason": f"PDF 无法读取或渲染：{exc}"})
            _progress(progress, f"ZIP 内 PDF 已跳过：{display_name}（{exc}）")
            continue
        finally:
            if document is not None:
                document.close()

        records.append(
            {
                "name": display_name,
                "path": str(path),
                "sha256": pdf_digest,
                "pages": len(page_details),
                "rail": rail,
                "special_vat": special_vat,
                "copies": special_copies if special_vat else (rail_copies if rail else other_copies),
                "classification": reason,
                "amount": f"{document_amount:.2f}" if document_amount is not None else None,
                "amount_details": amount_details,
                "text_extraction_error": extraction_error,
                "warnings": warnings,
                "page_details": page_details,
                **source_meta,
            }
        )
        seen_pdfs[pdf_digest] = records[-1]
        if image_digest:
            seen_images[image_digest] = records[-1]
        if on_record is not None:
            _check_cancelled()
            on_record(records[-1], index, len(pdfs))

    if not records:
        raise RuntimeError("没有可读取的 PDF 文件；请查看日志中的 ZIP 文件跳过原因。")
    hashes: dict[str, list[str]] = {}
    for record in records:
        hashes.setdefault(record["sha256"], []).append(record["name"])

    recognized_document_amounts = [
        Decimal(record["amount"]) for record in records if record["amount"]
    ]
    result = {
        "folder": source_label,
        "source_type": source_type,
        "imported_paths": [str(path) for path in (*original_pdfs, *original_images, *zips)]
        if source_type == "files"
        else [],
        "pdf_count": len(records),
        "image_count": sum(1 for record in records if record.get("source_image")),
        "image_excluded_count": len(image_excluded),
        "image_excluded": [source_name(path) for path in image_excluded],
        "zip_count": len(zips),
        "zip_internal_count": len(zip_pdfs),
        "zip_excluded_count": len(zip_excluded),
        "zip_excluded": [source_name(path) for path in zip_excluded],
        "zip_member_excluded_count": len(zip_member_excluded),
        "zip_member_excluded": zip_member_excluded,
        "rail_count": sum(1 for record in records if record["rail"]),
        "special_vat_count": sum(1 for record in records if record.get("special_vat")),
        "regular_count": sum(1 for record in records if not record["rail"] and not record.get("special_vat")),
        "source_pages": sum(record["pages"] for record in records),
        "expected_jobs": sum(record["pages"] * record["copies"] for record in records),
        "total_amount": f"{sum(recognized_document_amounts, Decimal('0.00')):.2f}",
        "amount_recognized_count": len(recognized_document_amounts),
        "amount_unrecognized_count": len(records) - len(recognized_document_amounts),
        "duplicate_groups": [names for names in hashes.values() if len(names) > 1],
        "duplicate_skipped_count": len(duplicate_imports),
        "duplicate_imports": duplicate_imports,
        "warnings": [
            {"file": record["name"], "warnings": record["warnings"]}
            for record in records
            if record["warnings"]
        ],
        "files": records,
    }
    _progress(
        progress,
        f"扫描完成：PDF {result['pdf_count']}，铁路票 {result['rail_count']}，"
        f"专票 {result['special_vat_count']}，"
        f"其他 {result['regular_count']}，识别金额 ¥{result['total_amount']}，"
        f"ZIP 内文件跳过 {result['zip_member_excluded_count']}，"
        f"跳过重复 {result['duplicate_skipped_count']}，预计作业 {result['expected_jobs']}。",
    )
    return result


def analyze_folder(
    folder: str | Path,
    rail_copies: int = 2,
    other_copies: int = 1,
    progress: ProgressCallback | None = None,
    on_record: ScanRecordCallback | None = None,
    special_copies: int = 2,
) -> dict[str, Any]:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"文件夹不存在：{root}")
    _progress(progress, f"正在扫描文件夹及子文件夹：{root}")
    _check_cancelled()
    pdfs: list[Path] = []
    images: list[Path] = []
    zips: list[Path] = []

    def raise_directory_error(error: OSError) -> None:
        raise OSError(f"无法读取文件夹：{error.filename or root}；{error}") from error

    directory_count = 0
    for directory, children, filenames in os.walk(root, onerror=raise_directory_error, followlinks=False):
        _check_cancelled()
        current = Path(directory)
        traversable = []
        for name in children:
            _check_cancelled()
            child = current / name
            if child.is_symlink() or child.is_junction():
                _progress(progress, f"跳过目录链接：{child.relative_to(root).as_posix()}")
            else:
                traversable.append(name)
        children[:] = sorted(traversable, key=str.casefold)
        for name in filenames:
            _check_cancelled()
            path = current / name
            extension = path.suffix.lower()
            if extension not in SUPPORTED_FILE_EXTENSIONS:
                continue
            if path.is_symlink():
                _progress(progress, f"跳过文件链接：{path.relative_to(root).as_posix()}")
                continue
            if extension == ".pdf":
                pdfs.append(path)
            elif extension == ".zip":
                zips.append(path)
            else:
                images.append(path)
        directory_count += 1
        if directory_count % 100 == 0:
            _progress(progress, f"已读取 {directory_count} 个文件夹，找到 {len(pdfs) + len(images) + len(zips)} 个源文件…")
    _progress(progress, f"已读取 {directory_count} 个文件夹：PDF {len(pdfs)}，图片 {len(images)}，ZIP {len(zips)}。")
    return _analyze_paths(
        pdfs,
        zips,
        str(root),
        rail_copies=rail_copies,
        other_copies=other_copies,
        progress=progress,
        source_type="folder",
        images=images,
        on_record=on_record,
        special_copies=special_copies,
    )


def analyze_files(
    paths: list[str | Path] | tuple[str | Path, ...],
    rail_copies: int = 2,
    other_copies: int = 1,
    progress: ProgressCallback | None = None,
    on_record: ScanRecordCallback | None = None,
    special_copies: int = 2,
) -> dict[str, Any]:
    supported: dict[str, Path] = {}
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_FILE_EXTENSIONS:
            continue
        supported.setdefault(os.path.normcase(str(path)), path)
    pdfs = [path for path in supported.values() if path.suffix.lower() == ".pdf"]
    zips = [path for path in supported.values() if path.suffix.lower() == ".zip"]
    images = [path for path in supported.values() if path.suffix.lower() in IMAGE_EXTENSIONS]
    return _analyze_paths(
        pdfs,
        zips,
        "导入文件",
        rail_copies=rail_copies,
        other_copies=other_copies,
        progress=progress,
        source_type="files",
        images=images,
        on_record=on_record,
        special_copies=special_copies,
    )


def export_scan_pdf(
    scan: dict[str, Any],
    output_path: str | Path,
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Merge scanned PDF/image documents into a new PDF without changing sources."""

    output = Path(output_path).expanduser().resolve()
    selected = list(records if records is not None else scan.get("files", []))
    if not selected:
        raise ValueError("没有可导出的文件。")
    source_paths = {Path(str(record["path"])).expanduser().resolve() for record in selected}
    if output in source_paths:
        raise ValueError("导出文件不能覆盖输入文件。")

    writer = PdfWriter()
    page_count = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    for index, record in enumerate(selected, 1):
        source = Path(str(record["path"])).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"导出源文件不存在：{source}")
        reader = PdfReader(str(source))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError(f"无法导出加密 PDF：{record.get('name', source.name)}")
        for page in reader.pages:
            writer.add_page(page)
            page_count += 1
        _progress(progress, f"正在导出 {index}/{len(selected)}：{record.get('name', source.name)}")
    writer.add_metadata({"/Title": "打印快捷工具导出文件", "/Producer": APP_NAME})
    with output.open("wb") as handle:
        writer.write(handle)
    return {"success": True, "output_path": str(output), "files": len(selected), "pages": page_count}


def filter_scan_result(
    scan: dict[str, Any], removed_paths: set[str] | list[str] | tuple[str, ...]
) -> dict[str, Any]:
    """Return a print-ready scan view without touching any source files."""

    def path_key(value: str) -> str:
        return os.path.normcase(os.path.abspath(value))

    removed_keys = {path_key(str(path)) for path in removed_paths}
    records = [
        record
        for record in scan.get("files", [])
        if path_key(str(record["path"])) not in removed_keys
    ]

    hashes: dict[str, list[str]] = {}
    for record in records:
        digest = str(record.get("sha256", ""))
        if digest:
            hashes.setdefault(digest, []).append(record["name"])

    recognized_amounts = [
        Decimal(record["amount"]) for record in records if record.get("amount")
    ]
    filtered = dict(scan)
    filtered.update(
        {
            "pdf_count": len(records),
            "image_count": sum(1 for record in records if record.get("source_image")),
            "rail_count": sum(1 for record in records if record["rail"]),
            "special_vat_count": sum(1 for record in records if record.get("special_vat")),
            "regular_count": sum(1 for record in records if not record["rail"] and not record.get("special_vat")),
            "source_pages": sum(record["pages"] for record in records),
            "expected_jobs": sum(
                record["pages"] * record["copies"] for record in records
            ),
            "total_amount": f"{sum(recognized_amounts, Decimal('0.00')):.2f}",
            "amount_recognized_count": len(recognized_amounts),
            "amount_unrecognized_count": len(records) - len(recognized_amounts),
            "duplicate_groups": [
                names for names in hashes.values() if len(names) > 1
            ],
            "warnings": [
                {"file": record["name"], "warnings": record["warnings"]}
                for record in records
                if record["warnings"]
            ],
            "files": records,
        }
    )
    return filtered


def _render_manifest(
    scan: dict[str, Any],
    temp_dir: Path,
    sample_first_rail: bool,
    dpi: int,
    progress: ProgressCallback | None,
    paper_size: str = "A5",
    duplex: bool = False,
    high_quality: bool = False,
    orientation_mode: str = "auto",
    scale_mode: str = "fit",
    margin_mm: float = 0.0,
) -> dict[str, Any]:
    paper_size = _normalize_paper_size(paper_size)
    orientation_mode = _normalize_orientation_mode(orientation_mode)
    scale_mode = _normalize_scale_mode(scale_mode)
    margin_mm = _normalize_margin_mm(margin_mm)
    records = scan["files"]
    if sample_first_rail:
        selected = next((record for record in records if record["rail"]), records[0])
        selections = [(selected, [1], 1)]
    else:
        selections = [
            (record, list(range(1, record["pages"] + 1)), record["copies"])
            for record in records
        ]

    rendered_dir = temp_dir / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    total_pages = sum(len(page_numbers) for _, page_numbers, _ in selections)
    rendered_count = 0
    for file_index, (record, page_numbers, copies) in enumerate(selections, 1):
        document_pages: list[dict[str, Any]] = []
        document = pdfium.PdfDocument(record["path"])
        try:
            for page_number in page_numbers:
                rendered_count += 1
                _progress(
                    progress,
                    f"正在生成 {dpi} DPI 打印页 {rendered_count}/{total_pages}："
                    f"{record['name']} 第 {page_number} 页",
                )
                image = (
                    document[page_number - 1]
                    .render(scale=dpi / 72.0, rotation=0)
                    .to_pil()
                    .convert("RGB")
                )
                output = (
                    rendered_dir
                    / f"{file_index:03d}_{_safe_stub(record['name'])}_p{page_number}.png"
                )
                image.save(output, "PNG", compress_level=3)
                page_job = {
                    "file": record["name"],
                    "page": page_number,
                    "copies": copies,
                    "rail": record["rail"],
                    "image": str(output),
                    "width": image.width,
                    "height": image.height,
                    "landscape": image.width > image.height,
                    "document_stub": _safe_stub(record["name"]),
                }
                jobs.append(page_job)
                document_pages.append(page_job)
                image.close()
        finally:
            document.close()
        documents.append(
            {
                "file": record["name"],
                "copies": copies,
                "rail": record["rail"],
                "document_stub": _safe_stub(record["name"]),
                "pages": document_pages,
            }
        )

    return {
        "batch_id": uuid.uuid4().hex[:8],
        "paper_size": paper_size,
        "duplex": bool(duplex),
        "high_quality": bool(high_quality),
        "orientation_mode": orientation_mode,
        "scale_mode": scale_mode,
        "margin_mm": margin_mm,
        "render_dpi": dpi,
        "expected_jobs": (
            sum(document["copies"] for document in documents)
            if duplex
            else sum(job["copies"] for job in jobs)
        ),
        "sample_first_rail": sample_first_rail,
        "jobs": jobs,
        "documents": documents,
    }


def _execute_manifest(
    manifest: dict[str, Any],
    printer: str,
    temp_dir: Path,
    progress: ProgressCallback | None,
    validate_only: bool = False,
) -> dict[str, Any]:
    manifest_path = temp_dir / "manifest.json"
    result_path = temp_dir / "result.json"
    script_path = temp_dir / "print.ps1"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    script_path.write_text(POWERSHELL_PRINT, encoding="utf-8-sig")

    environment = os.environ.copy()
    environment["A5_PRINT_PRINTER"] = printer
    environment["A5_PRINT_MANIFEST"] = str(manifest_path)
    environment["A5_PRINT_RESULT"] = str(result_path)
    environment["A5_PRINT_VALIDATE_ONLY"] = "1" if validate_only else "0"
    controller = getattr(_print_context, "controller", None)
    start_process = controller.start_process if controller is not None else subprocess.Popen
    process = start_process(
        [
            _powershell_executable(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creation_flags(),
    )
    stdout_lines: list[str] = []
    try:
        if process.stdout:
            for line in process.stdout:
                clean = line.strip()
                if clean:
                    stdout_lines.append(clean)
                    _progress(progress, clean)
        stderr = process.stderr.read().strip() if process.stderr else ""
        return_code = process.wait()
        _check_cancelled()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        if controller is not None:
            controller.release(process)
    if not result_path.exists():
        detail = stderr or "\n".join(stdout_lines) or f"退出码 {return_code}"
        raise RuntimeError(f"打印流程未生成结果：{detail}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["process_exit_code"] = return_code
    result["stdout"] = stdout_lines
    result["stderr"] = stderr
    return result


def validate_print_scan(
    scan: dict[str, Any],
    printer: str,
    paper_size: str = "A5",
    dpi: int = 600,
    progress: ProgressCallback | None = None,
    duplex: bool = False,
    high_quality: bool = False,
    orientation_mode: str = "auto",
    scale_mode: str = "fit",
    margin_mm: float = 0.0,
) -> dict[str, Any]:
    paper_size = _normalize_paper_size(paper_size)
    orientation_mode = _normalize_orientation_mode(orientation_mode)
    scale_mode = _normalize_scale_mode(scale_mode)
    margin_mm = _normalize_margin_mm(margin_mm)
    if os.name != "nt":
        raise RuntimeError("打印规则校验仅支持 Windows。")
    if not printer:
        raise ValueError("请选择打印机。")
    if dpi < 150 or dpi > 1200:
        raise ValueError("DPI 必须在 150 到 1200 之间。")

    hashes: dict[str, list[str]] = {}
    errors: list[str] = []
    for record in scan.get("files", []):
        _progress(progress, f"正在检查重复导入：{record['name']}")
        try:
            digest = _sha256(Path(record["path"]))
        except OSError as exc:
            errors.append(f"无法检查文件：{record['name']}（{exc}）")
            continue
        hashes.setdefault(digest, []).append(record["name"])
    duplicate_groups = [names for names in hashes.values() if len(names) > 1]
    errors.extend(f"列表中存在重复内容：{'、'.join(names)}；请重新扫描或移除重复项。"
                  for names in duplicate_groups)
    duplicate_report = {
        "duplicate_groups": duplicate_groups,
        "duplicate_checked_count": len(scan.get("files", [])),
        "duplicate_skipped_count": scan.get("duplicate_skipped_count", 0),
        "duplicate_imports": scan.get("duplicate_imports", []),
    }
    if errors:
        return {
            "success": False, "mode": "validate", "printer": printer,
            "paper_size": paper_size, "checked_pages": 0, "failed_pages": 0,
            "errors": errors, "results": [], "temp_retained": False,
            "expected_jobs": expected_print_jobs(scan, duplex),
            **duplicate_report,
        }

    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "A5TicketPrinter"
    jobs_root = base / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="validate-", dir=jobs_root))
    try:
        manifest = _render_manifest(
            scan,
            temp_dir,
            False,
            dpi,
            progress,
            paper_size,
            duplex,
            high_quality,
            orientation_mode,
            scale_mode,
            margin_mm,
        )
        _progress(progress, f"正在校验 {len(manifest['jobs'])} 个页面，不会提交打印作业。")
        result = _execute_manifest(manifest, printer, temp_dir, progress, validate_only=True)
        result["expected_jobs"] = manifest["expected_jobs"]
        result["temp_retained"] = False
        result.update(duplicate_report)
        return result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def print_scan(
    scan: dict[str, Any],
    printer: str,
    sample_first_rail: bool = False,
    dpi: int = 600,
    progress: ProgressCallback | None = None,
    paper_size: str = "A5",
    duplex: bool = False,
    high_quality: bool = False,
    orientation_mode: str = "auto",
    scale_mode: str = "fit",
    margin_mm: float = 0.0,
) -> dict[str, Any]:
    paper_size = _normalize_paper_size(paper_size)
    orientation_mode = _normalize_orientation_mode(orientation_mode)
    scale_mode = _normalize_scale_mode(scale_mode)
    margin_mm = _normalize_margin_mm(margin_mm)
    if os.name != "nt":
        raise RuntimeError("打印功能仅支持 Windows。")
    if not printer:
        raise ValueError("请选择打印机。")
    if dpi < 150 or dpi > 1200:
        raise ValueError("DPI 必须在 150 到 1200 之间。")

    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "A5TicketPrinter"
    jobs_root = base / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="job-", dir=jobs_root))
    success = False
    try:
        manifest = _render_manifest(
            scan,
            temp_dir,
            sample_first_rail,
            dpi,
            progress,
            paper_size,
            duplex,
            high_quality,
            orientation_mode,
            scale_mode,
            margin_mm,
        )
        _progress(
            progress,
            f"开始向 {printer} 提交 {manifest['expected_jobs']} 个 {paper_size} 作业。",
        )
        result = _execute_manifest(manifest, printer, temp_dir, progress)
        result["mode"] = "sample" if sample_first_rail else "batch"
        result["paper_size"] = paper_size
        result["duplex"] = bool(duplex)
        result["high_quality"] = bool(high_quality)
        result["orientation_mode"] = orientation_mode
        result["scale_mode"] = scale_mode
        result["margin_mm"] = margin_mm
        result["render_dpi"] = dpi
        result["rendered_pages"] = len(manifest["jobs"])
        result["temp_directory"] = str(temp_dir)
        success = bool(result.get("success"))
        if success:
            shutil.rmtree(temp_dir)
            result["temp_retained"] = False
        else:
            result["temp_retained"] = True
        return result
    except Exception:
        _progress(progress, f"打印失败，诊断文件保留在：{temp_dir}")
        raise
    finally:
        if success and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def print_calibration_page(
    printer: str,
    paper_size: str = "A5",
    copies: int = 1,
    orientation_mode: str = "auto",
    margin_mm: float = 0.0,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    paper_size = _normalize_paper_size(paper_size)
    orientation_mode = _normalize_orientation_mode(orientation_mode)
    copies = _normalize_copy_count(copies)
    if not printer:
        raise ValueError("请选择打印机。")
    if os.name != "nt":
        raise RuntimeError("打印校准页仅支持 Windows。")

    page_orientation = "landscape" if orientation_mode == "landscape" else "portrait"
    with tempfile.TemporaryDirectory(prefix="a5-calibration-") as temporary:
        pdf_path = Path(temporary) / "print-calibration.pdf"
        create_calibration_pdf(pdf_path, page_orientation)
        _progress(progress, "已生成带 100 mm 方框和毫米刻度的校准页。")
        scan = {
            "files": [
                {
                    "name": "print-calibration.pdf",
                    "path": str(pdf_path),
                    "pages": 1,
                    "rail": False,
                    "copies": copies,
                }
            ]
        }
        result = print_scan(
            scan,
            printer=printer,
            sample_first_rail=False,
            dpi=600,
            progress=progress,
            paper_size=paper_size,
            duplex=False,
            high_quality=False,
            orientation_mode=orientation_mode,
            scale_mode="actual",
            margin_mm=margin_mm,
        )
        result.update(
            {
                "mode": "calibration",
                "calibration_copies": copies,
                "orientation_mode": orientation_mode,
                "paper_size": paper_size,
                "render_dpi": 600,
                "scale_mode": "actual",
                "duplex": False,
            }
        )
        return result
