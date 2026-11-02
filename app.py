from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import sys
import threading
import tempfile
import time
import traceback
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import printer_engine as engine
from _version import BUILD_DATE, VERSION as APP_VERSION
from tray import SystemTray

try:
    from tkinterdnd2 import COPY, DND_FILES, REFUSE_DROP, TkinterDnD
except ImportError:  # Source-only fallback; the packaged build includes TkDnD.
    COPY = "copy"
    DND_FILES = None
    REFUSE_DROP = "refuse_drop"
    TkinterDnD = None


DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "A5TicketPrinter"
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "app.log"
PRINT_HISTORY_PATH = DATA_DIR / "print_history.json"
MAIN_PRESET_NAMES = ("智能", "标准", "高清", "快速")
ADVANCED_PRESET_NAMES = ("双面", "横向实际", "纵向实际", "横向适配", "纵向适配")
# === 90分美化配色系统：现代蓝 + 层次灰度 + 状态色体系 ===
APP_BG = "#EDF1F6"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#F8FAFC"
NAVY = "#0B2545"
PRIMARY = "#1E5AE8"
PRIMARY_DARK = "#1749C4"
PRIMARY_PRESS = "#123DA0"
PRIMARY_SOFT = "#EFF4FF"
TEXT = "#0F172A"
MUTED = "#64748B"
FAINT = "#94A3B8"
BORDER = "#E2E8F0"
BORDER_DARK = "#CBD5E1"
SUCCESS = "#059669"
SUCCESS_SOFT = "#ECFDF5"
WARNING = "#D97706"
WARNING_SOFT = "#FFFBEB"
ERROR = "#DC2626"
ERROR_SOFT = "#FEF2F2"
ZEBRA = "#F8FAFC"
SELECT_BG = "#DBEAFE"
SELECT_FG = "#1E3A8A"
HEADER_BAR = "#1E5AE8"
CARD_BORDER = "#E2E8F0"
DROP_CARD_BG = "#F5F9FF"
DROP_CARD_ACTIVE = "#E0ECFF"
DROP_BORDER = "#9FC5FF"
DROP_BORDER_ACTIVE = "#1E5AE8"
WM_DROPFILES = 0x0233
MAX_DROPPED_FILES = 1024
MAX_DROPPED_PATH_CHARS = 32767
STAMP_MODE_LABELS = {"普通盖章": "normal", "齐缝章": "seam"}
SUPPORTED_FILE_GLOB = "*.pdf *.zip *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp"
FONT_FAMILY = "Microsoft YaHei UI"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def run_headless_scan(folder: str, output: str | None) -> int:
    try:
        result = engine.analyze_folder(folder)
        payload = {
            "success": True,
            "version": APP_VERSION,
            "build_date": BUILD_DATE,
            "analysis": result,
            "printers": engine.list_printers(),
        }
        code = 0
    except Exception as exc:
        payload = {
            "success": False,
            "version": APP_VERSION,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        code = 1
    if output:
        _write_json(Path(output).expanduser().resolve(), payload)
    return code


class TicketPrinterApp:
    def __init__(self, root: tk.Tk, initial_folder: str | None = None) -> None:
        self.root = root
        self.root.title(f"{engine.APP_NAME}  v{APP_VERSION}")
        self.root.minsize(980, 800)
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

        self.events: queue.Queue[tuple] = queue.Queue()
        self.closing = False
        self._margin_save_after: str | None = None
        self._poll_after: str | None = None
        self.worker_thread: threading.Thread | None = None
        self.worker_controller: engine.PrintController | None = None
        self.tray: SystemTray | None = None
        self._tray_hidden = False
        self._setup_logging()
        self.scan_result: dict | None = None
        self.scan_signature: tuple | None = None
        self.removed_file_paths: set[str] = set()
        self.tree_record_paths: dict[str, str] = {}
        self._tree_drag_anchor: str | None = None
        self._tree_drag_base: tuple[str, ...] = ()
        self._tree_drag_after: str | None = None
        self._tree_drag_direction = 0
        self._scan_preview: dict | None = None
        self.source_mode = "folder"
        self.imported_paths: tuple[str, ...] = ()
        self.busy = False
        self.stamp_target_paths: dict[str, str] = {}
        self.stamp_temp_paths: set[Path] = set()
        self.print_history: list[dict] = self._load_print_history()
        self.active_print_context: dict | None = None
        self.history_window: tk.Toplevel | None = None
        self.history_tree: ttk.Treeview | None = None
        self.history_records_by_item: dict[str, dict] = {}

        self.folder_var = tk.StringVar()
        self.printer_var = tk.StringVar()
        self.paper_var = tk.StringVar(value="A5")
        self.preset_var = tk.StringVar(value=engine.DEFAULT_PRINT_PRESET)
        self.advanced_preset_var = tk.StringVar(value="选择高级规则")
        self.preset_description_var = tk.StringVar()
        self.rail_copies_var = tk.IntVar(value=2)
        self.special_copies_var = tk.StringVar(value="2")
        self._last_special_copies = 2
        self.other_copies_var = tk.IntVar(value=1)
        self.copy_count_var = tk.IntVar(value=0)
        self.close_to_tray_var = tk.BooleanVar(value=True)
        self.margin_mm_var = tk.DoubleVar(value=0.0)
        self.margin_display_var = tk.StringVar(value="0 mm")
        self.stamp_image_var = tk.StringVar()
        self.stamp_target_var = tk.StringVar()
        self.stamp_pages_var = tk.StringVar(value="1")
        self.stamp_all_pages_var = tk.BooleanVar(value=False)
        self.stamp_mode_var = tk.StringVar(value="普通盖章")
        self.stamp_size_var = tk.StringVar(value="28")
        self.summary_var = tk.StringVar(value="选择文件夹后自动扫描，包含各层子文件夹。")
        self.attachment_status_var = tk.StringVar(value="尚未导入附件")
        self.empty_drop_title_var = tk.StringVar(value="拖入 PDF 或 ZIP 到这里")
        self.drop_note_var = tk.StringVar(value="支持 PDF、图片和 ZIP；ZIP 会读取内部 PDF")
        self.drop_hover = False
        self.empty_drop_reset_state = False
        self.status_var = tk.StringVar(value="就绪")
        self.pdf_metric_var = tk.StringVar(value="0")
        self.rail_metric_var = tk.StringVar(value="0")
        self.amount_metric_var = tk.StringVar(value="¥0.00")
        self.jobs_metric_var = tk.StringVar(value="0")

        self._configure_style()
        self._build_ui()
        self._update_action_states()
        self._place_window()
        self._load_settings(initial_folder)
        self._refresh_margin_view()
        self._refresh_preset_view()
        self._refresh_printers(show_error=False)
        self._bind_invalidation()
        self._enable_file_drop()
        self._setup_tray()
        self._poll_after = self.root.after(20, self._poll_events)
        self.log("程序已启动。可选择文件夹、导入或拖入 PDF/ZIP；校准页可单独打印。")

    def _configure_style(self) -> None:
        self.root.configure(background=APP_BG)
        self.root.option_add("*Font", (FONT_FAMILY, 10))
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=APP_BG, foreground=TEXT)
        style.configure("App.TFrame", background=APP_BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("SurfaceAlt.TFrame", background=SURFACE_ALT)
        style.configure("Header.TFrame", background=NAVY)
        style.configure("HeaderBar.TFrame", background=HEADER_BAR)
        style.configure("HeaderTitle.TLabel", background=NAVY, foreground="#FFFFFF", font=(FONT_FAMILY, 19, "bold"))
        style.configure("HeaderSub.TLabel", background=NAVY, foreground="#9DB8D8", font=(FONT_FAMILY, 10))
        style.configure("HeaderVer.TLabel", background=NAVY, foreground="#6E8FB5", font=(FONT_FAMILY, 9))
        style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED, font=(FONT_FAMILY, 10))
        style.configure("Rule.TLabel", background=SURFACE, foreground=SUCCESS, font=(FONT_FAMILY, 9, "bold"))
        style.configure("Status.TLabel", background=SURFACE, foreground=FAINT, font=(FONT_FAMILY, 9))
        style.configure("Section.TLabelframe", background=SURFACE, bordercolor=CARD_BORDER, relief="solid", borderwidth=1)
        style.configure("Section.TLabelframe.Label", background=SURFACE, foreground=NAVY, font=(FONT_FAMILY, 10, "bold"))
        style.configure("TEntry", fieldbackground="#FFFFFF", bordercolor=BORDER_DARK, padding=4, borderwidth=1, foreground=TEXT)
        style.map("TEntry", bordercolor=[("focus", PRIMARY), ("!focus", BORDER_DARK)], lightcolor=[("focus", PRIMARY)], darkcolor=[("focus", PRIMARY)])
        style.configure("TCombobox", fieldbackground="#FFFFFF", bordercolor=BORDER_DARK, padding=3, foreground=TEXT, arrowcolor=PRIMARY)
        style.map("TCombobox", bordercolor=[("focus", PRIMARY), ("!focus", BORDER_DARK)], lightcolor=[("focus", PRIMARY)], darkcolor=[("focus", PRIMARY)])
        style.configure("TSpinbox", fieldbackground="#FFFFFF", bordercolor=BORDER_DARK, padding=3, foreground=TEXT, arrowcolor=PRIMARY)
        style.map("TSpinbox", bordercolor=[("focus", PRIMARY), ("!focus", BORDER_DARK)], lightcolor=[("focus", PRIMARY)], darkcolor=[("focus", PRIMARY)])
        style.configure("TButton", padding=(12, 4), background="#FFFFFF", foreground=TEXT, borderwidth=1, bordercolor=BORDER_DARK, font=(FONT_FAMILY, 10))
        style.map("TButton", background=[("active", PRIMARY_SOFT), ("pressed", "#E0E9FF")], bordercolor=[("active", PRIMARY), ("pressed", PRIMARY_DARK)], foreground=[("active", PRIMARY_DARK), ("disabled", FAINT)])
        style.configure("Primary.TButton", padding=(18, 6), background=PRIMARY, foreground="#FFFFFF", font=(FONT_FAMILY, 11, "bold"), borderwidth=0)
        style.map("Primary.TButton", background=[("active", PRIMARY_DARK), ("pressed", PRIMARY_PRESS), ("disabled", "#A9B9D1")], foreground=[("disabled", "#EEF2F7")])
        style.configure("Remove.TButton", padding=(10, 4), background="#FFFFFF", foreground=ERROR, borderwidth=1, bordercolor="#FCA5A5", font=(FONT_FAMILY, 10))
        style.map("Remove.TButton", background=[("active", ERROR_SOFT)], foreground=[("disabled", FAINT)], bordercolor=[("disabled", BORDER), ("active", ERROR)])
        style.configure("Treeview", rowheight=44, background=SURFACE, fieldbackground=SURFACE, foreground=TEXT, borderwidth=0, font=(FONT_FAMILY, 10))
        style.configure("Treeview.Heading", background=SURFACE_ALT, foreground=NAVY, font=(FONT_FAMILY, 9, "bold"), relief="flat", padding=(6, 4))
        style.map("Treeview.Heading", background=[("active", "#E2E8F0")])
        style.map("Treeview", background=[("selected", SELECT_BG)], foreground=[("selected", SELECT_FG)])
        style.configure("TNotebook", background=APP_BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 5), background="#DDE5EF", foreground=MUTED, font=(FONT_FAMILY, 10))
        style.map("TNotebook.Tab", background=[("selected", SURFACE)], foreground=[("selected", NAVY)])
        style.configure("Accent.Horizontal.TProgressbar", background=PRIMARY, troughcolor=BORDER, borderwidth=0)
        style.configure("Attachment.Vertical.TScrollbar", background=BORDER_DARK, troughcolor=SURFACE_ALT, bordercolor=BORDER, arrowcolor=NAVY, width=14, arrowsize=14)
        style.configure("Attachment.Horizontal.TScrollbar", background=BORDER_DARK, troughcolor=SURFACE_ALT, bordercolor=BORDER, arrowcolor=NAVY, arrowsize=12)

    def _place_window(self) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1240, max(980, screen_width - 48))
        height = min(1150, max(800, screen_height - 32))
        left = max(0, (screen_width - width) // 2)
        top = max(20, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{left}+{top}")

    @staticmethod
    def _rounded_points(
        left: float, top: float, right: float, bottom: float, radius: float
    ) -> list[float]:
        radius = min(radius, (right - left) / 2, (bottom - top) / 2)
        return [
            left + radius, top, right - radius, top, right, top + radius,
            right, bottom - radius, right - radius, bottom, left + radius,
            bottom, left, bottom - radius, left, top + radius,
        ]

    def _draw_drop_card(self) -> None:
        if not hasattr(self, "empty_drop_zone") or not self.empty_drop_zone.winfo_exists():
            return
        width = max(10, self.empty_drop_zone.winfo_width())
        height = max(10, self.empty_drop_zone.winfo_height())
        self.empty_drop_zone.delete("drop-card")
        fill = DROP_CARD_ACTIVE if self.drop_hover else DROP_CARD_BG
        border = DROP_BORDER_ACTIVE if self.drop_hover else DROP_BORDER
        points = self._rounded_points(3, 4, width - 3, height - 4, 16)
        self.empty_drop_zone.create_polygon(points, smooth=True, splinesteps=18, fill="#DCE9FA", outline="", tags="drop-card")
        points = self._rounded_points(3, 2, width - 3, height - 6, 16)
        self.empty_drop_zone.create_polygon(points, smooth=True, splinesteps=18, fill=fill, outline=border, width=2 if self.drop_hover else 1, tags="drop-card")
        self.empty_drop_zone.itemconfigure("drop-card", state=tk.NORMAL)
        self.empty_drop_zone.tag_lower("drop-card", self.empty_drop_window)
        self.empty_drop_zone.configure(cursor="hand2")
        self.empty_drop_zone.coords(self.empty_drop_window, width / 2, height / 2)
        self.empty_drop_zone.itemconfigure(
            self.empty_drop_window,
            width=max(520, width - 28),
            height=max(120, height - 24),
        )
        for widget in (self.empty_drop_zone, self.empty_drop_inner, self.empty_drop_icon_canvas, self.empty_drop_format_canvas):
            widget.configure(bg=fill)
        self.empty_drop_title.configure(bg=fill)
        self.drop_note_label.configure(bg=fill)

    def _draw_upload_icon(self) -> None:
        canvas = getattr(self, "empty_drop_icon_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        circle = "#B9D4FF" if self.drop_hover else "#D7E7FF"
        canvas.create_oval(2, 2, 54, 54, fill=circle, outline="")
        white = "#FFFFFF"
        canvas.create_oval(14, 25, 35, 43, fill=white, outline="")
        canvas.create_oval(23, 18, 44, 43, fill=white, outline="")
        canvas.create_rectangle(15, 31, 43, 43, fill=white, outline="")
        canvas.create_line(29, 25, 29, 38, fill=PRIMARY, width=2)
        canvas.create_polygon(24, 33, 29, 39, 34, 33, fill=PRIMARY, outline="")

    def _draw_format_tags(self) -> None:
        canvas = getattr(self, "empty_drop_format_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        tags = (("PDF", "#E0ECFF", PRIMARY), ("ZIP", "#EEF2FF", "#4F46E5"))
        left = 2
        for label, fill, foreground in tags:
            right = left + 54
            points = self._rounded_points(left, 2, right, 24, 11)
            canvas.create_polygon(points, smooth=True, splinesteps=12, fill=fill, outline="")
            canvas.create_text((left + right) / 2, 13, text=label, fill=foreground, font=(FONT_FAMILY, 9, "bold"))
            left = right + 10

    def _set_drop_hover(self, active: bool) -> None:
        if self.busy or self.drop_hover == active:
            return
        self.drop_hover = active
        self.empty_drop_title_var.set(
            "点击选择或拖入文件"
            if active
            else ("当前附件列表为空" if self.empty_drop_reset_state else "拖入 PDF 或 ZIP 到这里")
        )
        self._draw_upload_icon()
        self._draw_drop_card()

    def _schedule_drop_hover_reset(self) -> None:
        self.root.after(40, self._reset_drop_hover_if_outside)

    def _reset_drop_hover_if_outside(self) -> None:
        if not self.empty_drop_frame.winfo_ismapped():
            return
        x = self.root.winfo_pointerx()
        y = self.root.winfo_pointery()
        left = self.empty_drop_frame.winfo_rootx()
        top = self.empty_drop_frame.winfo_rooty()
        if not self._point_in_rectangle(x, y, left, top, self.empty_drop_frame.winfo_width(), self.empty_drop_frame.winfo_height()):
            self._set_drop_hover(False)

    def _build_ui(self) -> None:
        main_tab, log_tab = self._build_shell()
        self._build_source_section(main_tab)
        self._build_options_section(main_tab)
        self._build_metrics_and_actions(main_tab)
        self._build_advanced_section(main_tab)
        self._build_log_and_status(main_tab, log_tab)
        self._build_attachment_panel(main_tab)

    def _build_shell(self) -> tuple[ttk.Frame, ttk.Frame]:
        outer = ttk.Frame(self.root, padding=8, style="App.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer, padding=(18, 7), style="Header.TFrame")
        header.pack(fill=tk.X)
        header_text = ttk.Frame(header, style="Header.TFrame")
        header_text.pack(side=tk.LEFT)
        ttk.Label(header_text, text="打印快捷工具", style="HeaderTitle.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            header_text,
            text="PDF 票据识别 · A4/A5 智能打印 · 本地处理不打开 WPS",
            style="HeaderSub.TLabel",
        ).pack(side=tk.LEFT, padx=(18, 0), pady=(4, 0))
        ttk.Label(header, text=f"v{APP_VERSION}  ·  {BUILD_DATE}", style="HeaderSub.TLabel").pack(side=tk.RIGHT, pady=(5, 0))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        main_tab = ttk.Frame(notebook, padding=(8, 3), style="Surface.TFrame")
        log_tab = ttk.Frame(notebook, padding=12, style="Surface.TFrame")
        notebook.add(main_tab, text="打印任务")
        notebook.add(log_tab, text="运行日志")
        return main_tab, log_tab

    def _build_source_section(self, main_tab: ttk.Frame) -> None:
        source = ttk.LabelFrame(main_tab, text="1. 选择来源和打印机", padding=6, style="Section.TLabelframe")
        source.pack(fill=tk.X, pady=(4, 3))
        source.columnconfigure(1, weight=1)

        ttk.Label(source, text="文件夹：").grid(row=0, column=0, sticky=tk.W)
        self.folder_entry = ttk.Entry(source, textvariable=self.folder_var)
        self.folder_entry.grid(
            row=0, column=1, columnspan=3, sticky=tk.EW, padx=(4, 8)
        )
        self.browse_button = ttk.Button(source, text="选择文件夹", command=self.on_browse)
        self.browse_button.grid(row=0, column=4)
        self.import_button = ttk.Button(
            source, text="导入文件", command=self.on_import_files
        )
        self.import_button.grid(row=0, column=5, padx=(8, 0))

        ttk.Label(source, text="打印机：").grid(
            row=1, column=0, sticky=tk.W, pady=(4, 0)
        )
        self.printer_combo = ttk.Combobox(
            source, textvariable=self.printer_var, state="readonly"
        )
        self.printer_combo.grid(
            row=1,
            column=1,
            columnspan=3,
            sticky=tk.EW,
            padx=(4, 8),
            pady=(4, 0),
        )
        self.refresh_button = ttk.Button(
            source, text="刷新打印机", command=self.on_refresh_printers
        )
        self.refresh_button.grid(row=1, column=4, pady=(4, 0))

    def _build_options_section(self, main_tab: ttk.Frame) -> None:
        options = ttk.LabelFrame(
            main_tab,
            text="2. 打印规则",
            padding=6,
            style="Section.TLabelframe",
        )
        options.pack(fill=tk.X, pady=3)
        rule_row = ttk.Frame(options, style="Surface.TFrame")
        rule_row.pack(fill=tk.X)
        rule_row.columnconfigure(3, weight=1)
        ttk.Label(rule_row, text="预设：").grid(row=0, column=0, sticky=tk.W)
        self.preset_combo = ttk.Combobox(
            rule_row,
            textvariable=self.preset_var,
            values=tuple(engine.PRINT_PRESETS),
            state="readonly",
            width=12,
            justify=tk.CENTER,
            postcommand=self._prepare_preset_dropdown,
        )
        self.preset_combo.grid(row=0, column=1, padx=(4, 12), sticky=tk.W)
        ttk.Label(rule_row, text="高级规则：").grid(row=0, column=2, sticky=tk.W)
        self.advanced_preset_combo = ttk.Combobox(
            rule_row,
            textvariable=self.advanced_preset_var,
            values=("选择高级规则", *ADVANCED_PRESET_NAMES),
            state="readonly",
            width=24,
            justify=tk.CENTER,
        )
        self.advanced_preset_combo.grid(
            row=0, column=3, padx=(4, 0), sticky=tk.EW
        )
        self.advanced_preset_combo.bind(
            "<<ComboboxSelected>>", self.on_choose_advanced_preset
        )
        self.advanced_preset_button = self.advanced_preset_combo
        self.scan_button = ttk.Button(
            rule_row, text="一键扫描文件", command=self.on_scan, style="Primary.TButton"
        )
        self.scan_button.grid(row=0, column=4, padx=(12, 0), sticky=tk.E)
        description = ttk.Label(
            options, textvariable=self.preset_description_var, style="Muted.TLabel",
        )
        description.pack(fill=tk.X, pady=(3, 0))
        options.bind("<Configure>", lambda event: description.configure(
            wraplength=max(200, event.width - 24)
        ))

        counts_row = ttk.Frame(options, style="Surface.TFrame")
        counts_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(counts_row, text="纸张：").grid(row=0, column=0, sticky=tk.W)
        self.paper_combo = ttk.Combobox(
            counts_row,
            textvariable=self.paper_var,
            values=("A5", "A4"),
            state="readonly",
            width=5,
            justify=tk.CENTER,
        )
        self.paper_combo.grid(row=0, column=1, padx=(4, 12), sticky=tk.W)
        ttk.Label(counts_row, text="铁路票份数：").grid(
            row=0, column=2, sticky=tk.W
        )
        self.rail_spin = ttk.Spinbox(
            counts_row,
            from_=1,
            to=10,
            width=5,
            textvariable=self.rail_copies_var,
            justify=tk.CENTER,
        )
        self.rail_spin.grid(row=0, column=3, padx=(4, 12))
        ttk.Label(counts_row, text="专票份数：").grid(
            row=0, column=4, sticky=tk.W
        )
        self.special_spin = ttk.Spinbox(
            counts_row, from_=1, to=99, width=5,
            textvariable=self.special_copies_var, justify=tk.CENTER,
        )
        self.special_spin.grid(row=0, column=5, padx=(4, 12))
        ttk.Label(counts_row, text="其他份数：").grid(
            row=0, column=6, sticky=tk.W
        )
        self.other_spin = ttk.Spinbox(
            counts_row,
            from_=1,
            to=10,
            width=5,
            textvariable=self.other_copies_var,
            justify=tk.CENTER,
        )
        self.other_spin.grid(row=0, column=7, padx=(4, 12))
        ttk.Label(counts_row, text="复印份数：").grid(
            row=1, column=0, sticky=tk.W, pady=(6, 0)
        )
        self.copy_spin = ttk.Spinbox(
            counts_row,
            from_=0,
            to=99,
            width=5,
            textvariable=self.copy_count_var,
            justify=tk.CENTER,
        )
        self.copy_spin.grid(row=1, column=1, padx=(4, 8), pady=(6, 0), sticky=tk.W)
        ttk.Label(counts_row, text="0 = 关闭", style="Muted.TLabel").grid(
            row=1, column=2, columnspan=2, sticky=tk.W, pady=(6, 0)
        )
        margin_row = ttk.Frame(options, style="Surface.TFrame")
        margin_row.pack(fill=tk.X, pady=(6, 0))
        margin_row.columnconfigure(1, weight=1)
        ttk.Label(margin_row, text="打印边距：").grid(row=0, column=0, sticky=tk.W)
        self.margin_scale = ttk.Scale(
            margin_row,
            from_=0.0,
            to=50.0,
            orient=tk.HORIZONTAL,
            variable=self.margin_mm_var,
            command=self._on_margin_changed,
            length=220,
        )
        self.margin_scale.grid(
            row=0, column=1, sticky=tk.EW, padx=(4, 12)
        )
        ttk.Label(
            margin_row, textvariable=self.margin_display_var, style="Rule.TLabel"
        ).grid(row=0, column=2, sticky=tk.W)
        ttk.Label(
            margin_row,
            text="左右上下同时增加，拖动后自动保存",
            style="Muted.TLabel",
        ).grid(row=0, column=3, sticky=tk.W, padx=(12, 0))

    def _build_metrics_and_actions(self, main_tab: ttk.Frame) -> None:
        metrics = ttk.Frame(main_tab, style="Surface.TFrame")
        metrics.pack(fill=tk.X, pady=(1, 3))
        metric_items = (
            ("PDF 文件", self.pdf_metric_var, "#2563EB"),
            ("铁路票", self.rail_metric_var, "#D97706"),
            ("票据金额合计", self.amount_metric_var, "#0F766E"),
            ("预计打印作业", self.jobs_metric_var, "#7C3AED"),
        )
        for column, (label, variable, color) in enumerate(metric_items):
            metrics.columnconfigure(column, weight=1, uniform="metric")
            card = tk.Frame(metrics, bg=SURFACE, highlightbackground=CARD_BORDER, highlightthickness=1, bd=0)
            card.grid(row=0, column=column, sticky=tk.EW, padx=(0 if column == 0 else 0, 0 if column == 3 else 8), pady=(1, 3))
            bar = tk.Frame(card, bg=color, width=4)
            bar.pack(side=tk.LEFT, fill=tk.Y)
            inner = tk.Frame(card, bg=SURFACE)
            inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            tk.Label(inner, text=label, bg=SURFACE, fg=MUTED, font=(FONT_FAMILY, 9)).pack(side=tk.LEFT, padx=(10, 5), pady=4)
            tk.Label(inner, textvariable=variable, bg=SURFACE, fg=color, font=(FONT_FAMILY, 15, "bold")).pack(side=tk.RIGHT, padx=(5, 10), pady=2)

        actions = ttk.Frame(main_tab, style="Surface.TFrame")
        actions.pack(fill=tk.X, pady=(0, 5))
        self.selected_print_button = ttk.Button(
            actions, text="选择打印", command=self.on_print_selected
        )
        self.selected_print_button.pack(side=tk.LEFT)
        self.print_button = ttk.Button(
            actions,
            text="打印全部",
            command=self.on_print_all,
            style="Primary.TButton",
        )
        self.print_button.pack(side=tk.LEFT, padx=10)
        self.export_pdf_button = ttk.Button(
            actions,
            text="合并/转换 PDF",
            command=self.on_export_pdf,
        )
        self.export_pdf_button.pack(side=tk.LEFT, padx=(10, 0))
        self.preview_button = ttk.Button(
            actions, text="打印预览", command=self.on_print_preview
        )
        self.preview_button.pack(side=tk.LEFT, padx=(10, 0))
        self.history_button = ttk.Button(
            actions, text="打印记录", command=self.on_print_history
        )
        self.history_button.pack(side=tk.LEFT, padx=(10, 0))
        self.validate_button = ttk.Button(
            actions,
            text="校验规则",
            command=self.on_validate_print_rules,
        )
        self.validate_button.pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(
            actions,
            text="按住拖动多选",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=8)

    def _build_advanced_section(self, main_tab: ttk.Frame) -> None:
        advanced_holder = ttk.Frame(main_tab, style="Surface.TFrame")
        advanced_holder.pack(fill=tk.X)
        self.advanced_toggle_button = ttk.Button(
            self.import_button.master,
            text="展开高级功能",
            command=self._toggle_advanced_section,
        )
        self.advanced_toggle_button.grid(row=1, column=5, padx=(8, 0), pady=(5, 0))
        advanced_frame = ttk.LabelFrame(
            advanced_holder, text="高级功能（低频操作）", padding=6, style="Section.TLabelframe"
        )
        self.advanced_panel = advanced_frame
        advanced_actions = ttk.Frame(advanced_frame, style="Surface.TFrame")
        advanced_actions.pack(fill=tk.X, pady=(0, 5))
        self.copy_button = ttk.Button(
            advanced_actions,
            text="按份数打印所选",
            command=self.on_copy_selected,
        )
        self.copy_button.pack(side=tk.LEFT)
        self.stamp_button = ttk.Button(
            advanced_actions, text="盖章所选文件", command=self.on_stamp_selected
        )
        self.stamp_button.pack(side=tk.LEFT, padx=(10, 0))
        self.calibration_button = ttk.Button(
            advanced_actions,
            text="打印校准页",
            command=self.on_print_calibration_page,
        )
        self.calibration_button.pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(
            advanced_actions,
            text="高级预设、盖章、校准页和按份数打印集中在此处。",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=10)

        stamp_frame = ttk.LabelFrame(
            advanced_frame, text="盖章设置（先在明细中选择文件）", padding=6, style="Section.TLabelframe"
        )
        stamp_frame.pack(fill=tk.X, pady=(0, 0))
        stamp_frame.columnconfigure(1, weight=1)
        ttk.Label(stamp_frame, text="目标文件：").grid(row=0, column=0, sticky=tk.W)
        self.stamp_target_combo = ttk.Combobox(
            stamp_frame,
            textvariable=self.stamp_target_var,
            state="readonly",
            width=34,
        )
        self.stamp_target_combo.grid(row=0, column=1, columnspan=4, sticky=tk.EW, padx=(4, 8))
        ttk.Label(stamp_frame, text="仅可选择一个 PDF").grid(row=0, column=5, columnspan=2, sticky=tk.W)
        ttk.Label(stamp_frame, text="印章图片：").grid(row=1, column=0, sticky=tk.W, pady=(5, 0))
        ttk.Entry(stamp_frame, textvariable=self.stamp_image_var).grid(
            row=1, column=1, columnspan=4, sticky=tk.EW, padx=(4, 8), pady=(5, 0)
        )
        self.stamp_browse_button = ttk.Button(
            stamp_frame, text="选择印章", command=self.on_browse_stamp
        )
        self.stamp_browse_button.grid(row=1, column=5, columnspan=2, sticky=tk.W, pady=(5, 0))
        ttk.Label(stamp_frame, text="页数：").grid(row=2, column=0, sticky=tk.W, pady=(5, 0))
        self.stamp_pages_entry = ttk.Entry(
            stamp_frame, textvariable=self.stamp_pages_var, width=14
        )
        self.stamp_pages_entry.grid(row=2, column=1, sticky=tk.W, padx=(4, 12), pady=(5, 0))
        self.stamp_all_pages_check = ttk.Checkbutton(
            stamp_frame, text="全部页数", variable=self.stamp_all_pages_var
        )
        self.stamp_all_pages_check.grid(row=2, column=2, sticky=tk.W, pady=(5, 0))
        ttk.Label(stamp_frame, text="模式：").grid(row=2, column=3, sticky=tk.E, pady=(5, 0))
        self.stamp_mode_combo = ttk.Combobox(
            stamp_frame,
            textvariable=self.stamp_mode_var,
            values=tuple(STAMP_MODE_LABELS),
            state="readonly",
            width=10,
        )
        self.stamp_mode_combo.grid(row=2, column=4, sticky=tk.W, padx=(4, 12), pady=(5, 0))
        ttk.Label(stamp_frame, text="大小(mm)：").grid(row=2, column=5, sticky=tk.E, pady=(5, 0))
        self.stamp_size_entry = ttk.Entry(
            stamp_frame, textvariable=self.stamp_size_var, width=6
        )
        self.stamp_size_entry.grid(row=2, column=6, sticky=tk.W, padx=(4, 0), pady=(5, 0))
        ttk.Label(
            stamp_frame,
            text="页码示例：1,3-5；齐缝章按所选页序切分印章并沿右侧边缘连续拼接。",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=7, sticky=tk.W, pady=(5, 0))

    def _toggle_advanced_section(self) -> None:
        if self.advanced_panel.winfo_manager():
            self.advanced_panel.pack_forget()
            self.advanced_panel.master.configure(height=1)
            self.advanced_toggle_button.configure(
                text="展开高级功能"
            )
        else:
            self.advanced_panel.pack(fill=tk.X, pady=(4, 0))
            self.advanced_toggle_button.configure(text="收起高级功能")

    def _build_attachment_panel(self, main_tab: ttk.Frame) -> None:
        preview = ttk.LabelFrame(main_tab, text="3. 附件与打印明细", padding=8, style="Section.TLabelframe")
        preview.pack(fill=tk.BOTH, expand=True, pady=(3, 5))
        self.attachment_body = tk.Frame(preview, bg=SURFACE)
        self.attachment_body.pack(fill=tk.BOTH, expand=True)

        self.empty_drop_frame = tk.Frame(self.attachment_body, bg=SURFACE)
        self.empty_drop_zone = tk.Canvas(
            self.empty_drop_frame, bg=SURFACE, bd=0, highlightthickness=0, cursor="hand2"
        )
        self.empty_drop_zone.place(relx=0.5, rely=0.5, anchor=tk.CENTER, relwidth=0.96, height=108)
        empty_inner = tk.Frame(self.empty_drop_zone, bg=DROP_CARD_BG, padx=24, pady=18)
        empty_inner.pack_propagate(False)
        self.empty_drop_inner = empty_inner
        self.empty_drop_window = self.empty_drop_zone.create_window(0, 0, window=empty_inner, anchor=tk.CENTER)
        self.empty_drop_icon_canvas = tk.Canvas(empty_inner, width=56, height=56, bg=DROP_CARD_BG, bd=0, highlightthickness=0)
        self.empty_drop_icon_canvas.pack(side=tk.LEFT, padx=(18, 10))
        self.empty_drop_title = tk.Label(
            empty_inner, textvariable=self.empty_drop_title_var, bg=DROP_CARD_BG,
            fg=NAVY, font=(FONT_FAMILY, 14, "bold"), cursor="hand2"
        )
        self.empty_drop_title.pack(side=tk.LEFT, padx=(0, 16))
        self.drop_note_label = tk.Label(
            empty_inner, textvariable=self.drop_note_var, bg=DROP_CARD_BG,
            fg=MUTED, font=(FONT_FAMILY, 9), cursor="hand2"
        )
        self.drop_note_label.pack(side=tk.LEFT, padx=(0, 18))
        self.empty_drop_format_canvas = tk.Canvas(empty_inner, width=132, height=26, bg=DROP_CARD_BG, bd=0, highlightthickness=0)
        self.empty_drop_format_canvas.pack(side=tk.LEFT, padx=(0, 16))
        self.empty_drop_button = ttk.Button(
            empty_inner, text="选择附件", command=self.on_import_files,
            style="Primary.TButton", padding=(22, 4)
        )
        self.empty_drop_button.pack(side=tk.LEFT, padx=(0, 18))
        for widget in (
            self.empty_drop_zone, empty_inner, self.empty_drop_icon_canvas,
            self.empty_drop_title, self.drop_note_label, self.empty_drop_format_canvas,
        ):
            widget.bind("<Button-1>", lambda _event: self.on_import_files())
            widget.bind("<Enter>", lambda _event: self._set_drop_hover(True))
            widget.bind("<Leave>", lambda _event: self._schedule_drop_hover_reset())
        self.empty_drop_button.bind("<Enter>", lambda _event: self._set_drop_hover(True))
        self.empty_drop_button.bind("<Leave>", lambda _event: self._schedule_drop_hover_reset())
        self.empty_drop_zone.bind("<Configure>", lambda _event: self._draw_drop_card())
        self._draw_upload_icon()
        self._draw_format_tags()
        self._draw_drop_card()

        self.table_frame = ttk.Frame(self.attachment_body, style="Surface.TFrame")
        self.table_frame.pack(fill=tk.BOTH, expand=True)
        self.loaded_attachment_bar = tk.Frame(
            self.table_frame,
            bg="#EFF6FF",
            highlightbackground="#BFDBFE",
            highlightthickness=1,
            padx=10,
            pady=6,
        )
        self.loaded_attachment_bar.pack(fill=tk.X, pady=(0, 6))
        self.progress = ttk.Progressbar(
            self.loaded_attachment_bar, mode="indeterminate", length=120,
            style="Accent.Horizontal.TProgressbar",
        )
        tk.Label(
            self.loaded_attachment_bar,
            textvariable=self.attachment_status_var,
            bg="#EFF6FF",
            fg=NAVY,
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(side=tk.LEFT)
        self.select_all_button = ttk.Button(
            self.loaded_attachment_bar,
            text="全选",
            command=self.on_select_all,
        )
        self.select_all_button.pack(side=tk.RIGHT, padx=(8, 0))
        self.remove_button = ttk.Button(
            self.loaded_attachment_bar,
            text="移除所选",
            command=self.on_remove_selected,
            style="Remove.TButton",
        )
        self.remove_button.pack(side=tk.RIGHT, padx=(8, 0))
        self.add_files_button = ttk.Button(
            self.loaded_attachment_bar,
            text="继续添加附件",
            command=self.on_add_files,
        )
        self.add_files_button.pack(side=tk.RIGHT)

        tree_frame = tk.Frame(
            self.table_frame,
            bg=BORDER,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        tree_frame.pack(fill=tk.BOTH, expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        columns = ("type", "amount", "pages", "copies", "orientation", "reason", "warnings")
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="tree headings", selectmode="extended"
        )
        self.tree.heading("#0", text="文件名")
        self.tree.heading("type", text="类型")
        self.tree.heading("amount", text="金额")
        self.tree.heading("pages", text="页数")
        self.tree.heading("copies", text="份数")
        self.tree.heading("orientation", text="方向")
        self.tree.heading("reason", text="识别依据")
        self.tree.heading("warnings", text="提示")
        self.tree.column("#0", width=300, minwidth=220, stretch=True)
        self.tree.column("type", width=120, minwidth=110, anchor=tk.CENTER, stretch=False)
        self.tree.column("amount", width=96, minwidth=88, anchor=tk.E, stretch=False)
        self.tree.column("pages", width=58, minwidth=54, anchor=tk.CENTER, stretch=False)
        self.tree.column("copies", width=58, minwidth=54, anchor=tk.CENTER, stretch=False)
        self.tree.column("orientation", width=82, minwidth=72, anchor=tk.CENTER, stretch=False)
        self.tree.column("reason", width=210, minwidth=160, stretch=True)
        self.tree.column("warnings", width=180, minwidth=130, stretch=True)
        self.tree_scroll = ttk.Scrollbar(
            tree_frame,
            orient=tk.VERTICAL,
            command=self.tree.yview,
            style="Attachment.Vertical.TScrollbar",
        )
        self.tree_xscroll = ttk.Scrollbar(
            tree_frame,
            orient=tk.HORIZONTAL,
            command=self.tree.xview,
            style="Attachment.Horizontal.TScrollbar",
        )
        self.tree.configure(
            yscrollcommand=self.tree_scroll.set,
            xscrollcommand=self.tree_xscroll.set,
        )
        self.tree.grid(row=0, column=0, sticky=tk.NSEW)
        self.tree_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.tree_xscroll.grid(row=1, column=0, sticky=tk.EW)
        tk.Frame(tree_frame, bg="#F1F5F9", width=14, height=12).grid(
            row=1, column=1, sticky=tk.NSEW
        )
        self.tree.tag_configure("rail", background="#FFF7ED")
        self.tree.tag_configure("duplicate", background="#FEFCE8")
        self.tree.tag_configure("warning", foreground="#B45309")
        self.tree.tag_configure("zebra", background=ZEBRA)
        self.tree.tag_configure("pending", foreground=MUTED)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection_changed)
        self.tree.bind("<Delete>", self.on_remove_selected)
        self.tree.bind("<Control-a>", self.on_select_all)
        self.tree.bind("<ButtonPress-1>", self._on_tree_drag_start)
        self.tree.bind("<B1-Motion>", self._on_tree_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._cancel_tree_drag)
        self.tree.bind("<Escape>", self._cancel_tree_drag)
        self.tree.bind("<FocusOut>", self._cancel_tree_drag)
        self.tree.bind("<MouseWheel>", self._on_tree_mousewheel)
        self.tree.bind("<Shift-MouseWheel>", self._on_tree_shift_mousewheel)

    def _build_log_and_status(self, main_tab: ttk.Frame, log_tab: ttk.Frame) -> None:
        log_frame = ttk.LabelFrame(log_tab, text="运行日志", padding=8, style="Section.TLabelframe")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(
            log_frame,
            height=20,
            wrap=tk.WORD,
            state=tk.DISABLED,
            background="#F8FAFC",
            foreground=TEXT,
            relief=tk.FLAT,
            padx=10,
            pady=8,
        )
        log_scroll = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        status = ttk.Frame(main_tab, style="Surface.TFrame")
        status.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        ttk.Label(status, textvariable=self.status_var, width=24, style="Status.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Label(
            status,
            textvariable=self.summary_var, width=1, anchor=tk.E,
            style="Status.TLabel",
        ).pack(side=tk.RIGHT, fill=tk.X, expand=True)
        ttk.Separator(main_tab, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))

    def _bind_invalidation(self) -> None:
        self.folder_var.trace_add("write", lambda *_: self._on_folder_changed())
        self.rail_copies_var.trace_add("write", lambda *_: self._invalidate_scan())
        self.special_copies_var.trace_add("write", lambda *_: self._on_special_copies_changed())
        self.other_copies_var.trace_add("write", lambda *_: self._invalidate_scan())
        self.preset_var.trace_add("write", lambda *_: self._on_preset_changed())
        self.margin_mm_var.trace_add("write", lambda *_: self._refresh_margin_view())
        self.copy_count_var.trace_add(
            "write", lambda *_: self._update_action_states(
                copy_count_text=str(self.root.getvar(str(self.copy_count_var)))
            )
        )

    def _on_folder_changed(self) -> None:
        if self.source_mode == "files":
            self.source_mode = "folder"
            self.imported_paths = ()
        self._invalidate_scan()

    def _load_settings(self, initial_folder: str | None) -> None:
        settings: dict = {}
        try:
            if SETTINGS_PATH.exists():
                settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

        if not isinstance(settings, dict):
            settings = {}
        self.close_to_tray_var.set(settings.get("close_to_tray", True) is not False)
        folder = initial_folder or settings.get("last_folder", "")
        if folder and Path(folder).is_dir():
            self.folder_var.set(str(Path(folder)))
        self.printer_var.set(settings.get("printer", ""))
        paper_size = str(settings.get("paper_size", "A5")).upper()
        self.paper_var.set(paper_size if paper_size in {"A4", "A5"} else "A5")
        preset_name = str(settings.get("print_preset", engine.DEFAULT_PRINT_PRESET))
        self.preset_var.set(
            preset_name if preset_name in engine.PRINT_PRESETS else engine.DEFAULT_PRINT_PRESET
        )
        for variable, key, fallback in (
            (self.rail_copies_var, "rail_copies", 2),
            (self.other_copies_var, "other_copies", 1),
        ):
            try:
                variable.set(max(1, int(settings.get(key, fallback))))
            except (TypeError, ValueError):
                variable.set(fallback)
        try:
            self._last_special_copies = engine.normalize_special_copies(settings.get("special_copies", 2))
        except ValueError:
            self._last_special_copies = 2
        self.special_copies_var.set(str(self._last_special_copies))
        self.copy_count_var.set(0)
        try:
            margin_mm = engine.normalize_margin_mm(settings.get("margin_mm", 0.0))
        except (TypeError, ValueError):
            margin_mm = 0.0
        if hasattr(self, "margin_mm_var"):
            self.margin_mm_var.set(margin_mm)
            self._refresh_margin_view()
        if hasattr(self, "stamp_image_var"):
            self.stamp_image_var.set(str(settings.get("stamp_image", "")))
            self.stamp_pages_var.set(str(settings.get("stamp_pages", "1")))
            self.stamp_all_pages_var.set(bool(settings.get("stamp_all_pages", False)))
            stamp_mode = str(settings.get("stamp_mode", "普通盖章"))
            self.stamp_mode_var.set(
                stamp_mode if stamp_mode in STAMP_MODE_LABELS else "普通盖章"
            )
            self.stamp_size_var.set(str(settings.get("stamp_size_mm", "28")))

    def _save_settings(self) -> None:
        self._cancel_margin_save()
        stamp_image_var = getattr(self, "stamp_image_var", None)
        stamp_pages_var = getattr(self, "stamp_pages_var", None)
        stamp_all_pages_var = getattr(self, "stamp_all_pages_var", None)
        stamp_mode_var = getattr(self, "stamp_mode_var", None)
        stamp_size_var = getattr(self, "stamp_size_var", None)
        payload = {
            "last_folder": self.folder_var.get().strip(),
            "printer": self.printer_var.get().strip(),
            "paper_size": self.paper_var.get(),
            "print_preset": self.preset_var.get(),
            "rail_copies": self._copy_value(self.rail_copies_var, 2),
            "special_copies": self._special_copy_value() or self._last_special_copies,
            "other_copies": self._copy_value(self.other_copies_var, 1),
            "copy_count": self._validated_copy_count() or 0,
            "close_to_tray": self.close_to_tray_var.get(),
            "margin_mm": self._margin_value(),
            "stamp_image": stamp_image_var.get().strip() if stamp_image_var else "",
            "stamp_pages": stamp_pages_var.get().strip() if stamp_pages_var else "1",
            "stamp_all_pages": bool(stamp_all_pages_var.get()) if stamp_all_pages_var else False,
            "stamp_mode": stamp_mode_var.get().strip() if stamp_mode_var else "普通盖章",
            "stamp_size_mm": stamp_size_var.get().strip() if stamp_size_var else "28",
        }
        _write_json(SETTINGS_PATH, payload)

    def _load_print_history(self) -> list[dict]:
        try:
            payload = json.loads(PRINT_HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)][-100:]

    def _save_print_history(self) -> None:
        try:
            _write_json(PRINT_HISTORY_PATH, self.print_history[-100:])
        except OSError as exc:
            self.log(f"打印记录保存失败：{exc}")

    def _append_print_history(self, result: dict, context: dict | None, error: str = "") -> None:
        context = context or {}
        expected = int(result.get("expected_jobs", context.get("expected_jobs", 0)) or 0)
        submitted = int(result.get("submitted_jobs", 0) or 0)
        observed = int(result.get("jobs_observed_in_queue", 0) or 0)
        success = bool(result.get("success")) and not error
        failure_reason = error or str(
            result.get("fatal_error") or result.get("stderr") or ""
        )
        record = {
            "id": uuid.uuid4().hex,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "operation": context.get("operation", context.get("mode", "打印")),
            "mode": context.get("mode", "batch"),
            "printer": context.get("printer", self.printer_var.get().strip()),
            "paper_size": context.get("paper_size", self.paper_var.get()),
            "preset": context.get("preset", self._current_preset().get("name", "")),
            "margin_mm": float(context.get("margin_mm", self._margin_value()) or 0),
            "copies": int(context.get("copies", 0) or 0),
            "expected_jobs": expected,
            "submitted_jobs": submitted,
            "observed_queue_jobs": observed,
            "queue_remaining": int(result.get("queue_remaining_with_prefix", 0) or 0),
            "status": "成功" if success else "失败",
            "success": success,
            "failure_reason": failure_reason,
            "source_names": list(context.get("source_names", [])),
            "source_paths": list(context.get("source_paths", [])),
            "source_copies": dict(context.get("source_copies", {})),
            "temp_directory": str(result.get("temp_directory", "")),
        }
        self.print_history.append(record)
        self.print_history = self.print_history[-100:]
        self._save_print_history()

    def _make_print_context(
        self,
        scan: dict | None,
        mode: str,
        operation: str,
        printer: str,
        paper_size: str,
        preset: dict,
        margin_mm: float,
    ) -> dict:
        records = list((scan or {}).get("files", []))
        return {
            "mode": mode,
            "operation": operation,
            "printer": printer,
            "paper_size": paper_size,
            "preset": preset.get("name", ""),
            "margin_mm": margin_mm,
            "expected_jobs": engine.expected_print_jobs(scan, preset["duplex"]) if scan else 0,
            "source_names": [str(record.get("name", "")) for record in records],
            "source_paths": [str(record.get("path", "")) for record in records],
            "source_copies": {
                str(record["path"]): int(record.get("copies", 1) or 1)
                for record in records if record.get("path")
            },
        }

    def _margin_value(self) -> float:
        variable = getattr(self, "margin_mm_var", None)
        if variable is None:
            return 0.0
        try:
            return engine.normalize_margin_mm(variable.get())
        except (tk.TclError, TypeError, ValueError):
            return 0.0

    def _refresh_margin_view(self) -> None:
        display = getattr(self, "margin_display_var", None)
        if display is not None:
            display.set(f"{self._margin_value():g} mm")

    def _on_margin_changed(self, _value: str | None = None) -> None:
        self._refresh_margin_view()
        if hasattr(self, "folder_var") and not self.closing:
            self._cancel_margin_save()
            self._margin_save_after = self.root.after(300, self._flush_margin_save)

    def _cancel_margin_save(self) -> None:
        pending = getattr(self, "_margin_save_after", None)
        if pending is not None:
            self.root.after_cancel(pending)
            self._margin_save_after = None

    def _flush_margin_save(self) -> None:
        self._margin_save_after = None
        try:
            self._save_settings()
        except (OSError, tk.TclError, ValueError) as exc:
            self.log(f"边距设置保存失败：{exc}")

    def _copy_value(
        self, variable: tk.IntVar, fallback: int, maximum: int | None = None
    ) -> int:
        try:
            value = max(1, int(variable.get()))
            return min(maximum, value) if maximum is not None else value
        except (tk.TclError, ValueError):
            return fallback

    def _special_copy_value(self) -> int | None:
        try:
            return engine.normalize_special_copies(self.special_copies_var.get())
        except (ValueError, tk.TclError):
            return None

    def _on_special_copies_changed(self) -> None:
        if self.busy:
            return
        copies = self._special_copy_value()
        if copies is not None:
            self._last_special_copies = copies
            signature = self._current_signature()
            if self.scan_result and self.scan_signature and self.scan_signature[:4] == signature[:4]:
                updated = dict(self.scan_result)
                updated["files"] = [
                    {**record, "copies": copies} if record.get("special_vat") else record
                    for record in self.scan_result["files"]
                ]
                self.scan_result = engine.filter_scan_result(updated, set())
                self.scan_signature = signature
                self._refresh_scan_view()
            try:
                self._save_settings()
            except OSError as exc:
                self.log(f"专票份数保存失败：{exc}")
        self._update_action_states()

    def _validated_copy_count(self, raw_value: str | None = None) -> int | None:
        # Read the entry text: IntVar.get() can silently truncate decimals.
        value = (self.copy_spin.get() if raw_value is None else raw_value).strip()
        if not value.isascii() or not value.isdecimal():
            return None
        try:
            count = int(value)
        except ValueError:
            return None
        return count if 1 <= count <= 99 else None

    def _current_preset(self) -> dict:
        return engine.get_print_preset(self.preset_var.get())

    def _refresh_preset_view(self) -> None:
        preset = self._current_preset()
        if self.preset_var.get() != preset["name"]:
            self.preset_var.set(preset["name"])
        # Keep the complete value list for source/test compatibility. The
        # postcommand narrows the opened dropdown to the four common presets.
        self.preset_combo.configure(values=tuple(engine.PRINT_PRESETS))
        self.preset_description_var.set(preset["description"])
        if hasattr(self, "advanced_preset_var"):
            self.advanced_preset_var.set(
                preset["name"]
                if preset["name"] in ADVANCED_PRESET_NAMES
                else "选择高级规则"
            )

    def _prepare_preset_dropdown(self) -> None:
        current = self._current_preset()["name"]
        visible_values = list(MAIN_PRESET_NAMES)
        if current not in visible_values:
            visible_values.append(current)
        self.preset_combo.configure(values=tuple(visible_values))

    def on_choose_advanced_preset(self, _event: object | None = None) -> None:
        if self.busy:
            return
        preset_name = self.advanced_preset_var.get().strip()
        if preset_name not in ADVANCED_PRESET_NAMES:
            return
        self.preset_var.set(preset_name)
        self._save_settings()

    def _on_preset_changed(self) -> None:
        self._refresh_preset_view()
        result = self._active_scan_result()
        if result is not None:
            self.jobs_metric_var.set(
                str(engine.expected_print_jobs(result, self._current_preset()["duplex"]))
            )

    def _current_signature(self) -> tuple:
        if self.source_mode == "files":
            return (
                "files",
                self.imported_paths,
                self._copy_value(self.rail_copies_var, 2),
                self._copy_value(self.other_copies_var, 1),
                self._special_copy_value(),
            )
        return (
            "folder",
            str(Path(self.folder_var.get().strip()).expanduser()) if self.folder_var.get().strip() else "",
            self._copy_value(self.rail_copies_var, 2),
            self._copy_value(self.other_copies_var, 1),
            self._special_copy_value(),
        )

    def _invalidate_scan(self) -> None:
        if self.scan_signature and self.scan_signature != self._current_signature():
            self.scan_result = None
            self.scan_signature = None
            self.removed_file_paths.clear()
            self.summary_var.set("设置已变化，请重新扫描文件。")
            self._reset_metrics()
            self._clear_tree()
            self._show_attachment_state(None)
            self._update_action_states()

    def _reset_metrics(self) -> None:
        self.pdf_metric_var.set("0")
        self.rail_metric_var.set("0")
        self.amount_metric_var.set("¥0.00")
        self.jobs_metric_var.set("0")

    def _clear_tree(self) -> None:
        self._cancel_tree_drag()
        items = self.tree.get_children()
        if items:
            self.tree.delete(*items)
        self.tree_record_paths.clear()

    def _scan_previous_state(self) -> dict:
        return {
            "source_mode": self.source_mode,
            "imported_paths": self.imported_paths,
            "preset": self.preset_var.get(),
            "scan_result": self.scan_result,
            "scan_signature": self.scan_signature,
            "removed_paths": set(self.removed_file_paths),
            "summary": self.summary_var.get(),
        }

    def _begin_scan_preview(
        self, signature: tuple, paths: tuple[str, ...], removed_paths: set[str],
        previous: dict, append: bool = False,
    ) -> None:
        if not append:
            self._clear_tree()
            self._reset_metrics()
        existing_sources = {
            record.get("source_archive") or record.get("source_image") or record["path"]
            for record in (self.scan_result or {}).get("files", [])
        } if append else set()
        pending: dict[str, str] = {}
        for value in paths:
            if value in existing_sources or value in removed_paths:
                continue
            path = Path(value)
            kind = "ZIP 待展开" if path.suffix.lower() == ".zip" else (
                "图片" if path.suffix.lower() in engine.IMAGE_EXTENSIONS else "PDF"
            )
            pending[value] = self.tree.insert(
                "", tk.END, text=path.name,
                values=(kind, "…", "…", "…", "…", "等待识别", ""),
                tags=("pending",),
            )
        self._scan_preview = {
            "signature": signature, "removed_paths": removed_paths,
            "previous": previous, "pending": pending,
            "items": {path: item for item, path in self.tree_record_paths.items()},
        }
        self._set_drop_hover(False)
        self.empty_drop_frame.pack_forget()
        if not self.table_frame.winfo_manager():
            self.table_frame.pack(fill=tk.BOTH, expand=True)
        self.attachment_status_var.set(
            f"等待识别 · {len(paths)} 个源文件" if paths else "正在读取文件夹…"
        )
        self.summary_var.set("正在识别附件，文件明细会逐条更新…")

    def _handle_scan_record(self, record: dict, index: int, total: int, signature: tuple) -> None:
        preview = self._scan_preview
        if self.closing or preview is None or preview["signature"] != signature:
            return
        if record["path"] not in preview["removed_paths"]:
            source = record.get("source_archive") or record.get("source_image") or record["path"]
            pending_item = preview["pending"].pop(source, None)
            item = preview["items"].get(record["path"])
            if record.get("source_archive") and pending_item:
                self.tree.delete(pending_item)
                pending_item = None
            item = self._update_tree_record(record, item or pending_item, len(preview["items"]))
            preview["items"][record["path"]] = item
        self.attachment_status_var.set(f"正在识别 · {index}/{total} 份 PDF")
        self.status_var.set(f"正在识别 {index}/{total}：{record['name']}")

    def _restore_scan_preview(self) -> None:
        if self._scan_preview is None:
            return
        previous = self._scan_preview["previous"]
        self._scan_preview = None
        self.source_mode = previous["source_mode"]
        self.imported_paths = previous["imported_paths"]
        self.scan_result = previous["scan_result"]
        self.scan_signature = previous["scan_signature"]
        self.removed_file_paths = previous["removed_paths"]
        self.preset_var.set(previous["preset"])
        self._refresh_scan_view()
        self.summary_var.set(previous["summary"])

    def _active_scan_result(self) -> dict | None:
        if not self.scan_result:
            return None
        return engine.filter_scan_result(self.scan_result, self.removed_file_paths)

    def _show_attachment_state(self, result: dict | None) -> None:
        has_files = bool(result and result.get("files"))
        if has_files:
            self.empty_drop_reset_state = False
            self._set_drop_hover(False)
            self.empty_drop_frame.pack_forget()
            if not self.table_frame.winfo_manager():
                self.table_frame.pack(fill=tk.BOTH, expand=True)
            source_name = "导入附件" if result.get("source_type") == "files" else "文件夹扫描"
            zip_count = int(result.get("zip_count", 0))
            zip_internal_count = int(result.get("zip_internal_count", 0))
            zip_excluded_count = int(result.get("zip_excluded_count", 0))
            zip_note = (
                f"ZIP {zip_count} 个 · 内部 PDF {zip_internal_count} 个"
                + (f" · 排除 {zip_excluded_count} 个" if zip_excluded_count else "")
                if zip_count else ""
            )
            self.attachment_status_var.set(
                f"{source_name} · PDF {result['pdf_count']} 份"
                + (f" · 图片 {result.get('image_count', 0)} 个" if result.get("image_count") else "")
                + (f" · {zip_note}" if zip_note else "")
            )
            self.add_files_button.configure(
                text="继续添加附件" if result.get("source_type") == "files" else "导入其他附件"
            )
            return
        self.empty_drop_frame.pack_forget()
        self.empty_drop_reset_state = result is not None
        if not self.table_frame.winfo_manager():
            self.table_frame.pack(fill=tk.BOTH, expand=True)
        self.empty_drop_title_var.set(
            "当前附件列表为空" if result is not None else "拖入 PDF 或 ZIP 到这里"
        )
        self.attachment_status_var.set(
            "当前附件列表为空" if result is not None else "尚未导入附件"
        )

    def _refresh_scan_view(self, reset_scroll: bool = False) -> None:
        current_scroll = 0.0
        if not reset_scroll and self.tree.get_children():
            current_scroll = self.tree.yview()[0]
        result = self._active_scan_result()
        if result is None:
            self._reset_metrics()
            self._clear_tree()
            self._show_attachment_state(None)
            self._refresh_stamp_targets(None)
            self._update_action_states()
            return

        self._populate_tree(result)
        self._show_attachment_state(result)
        self._refresh_stamp_targets(result)
        if result["files"]:
            self.tree.yview_moveto(0.0 if reset_scroll else current_scroll)
        self.pdf_metric_var.set(str(result["pdf_count"]))
        self.rail_metric_var.set(str(result["rail_count"]))
        self.amount_metric_var.set(f"¥{result['total_amount']}")
        self.jobs_metric_var.set(
            str(engine.expected_print_jobs(result, self._current_preset()["duplex"]))
        )
        duplicate_count = len(result["duplicate_groups"])
        warning_count = len(result["warnings"])
        removed_count = len(self.scan_result["files"]) - result["pdf_count"]
        removed_note = (
            f"已从列表移除 {removed_count} 份（原文件保留） · "
            if removed_count
            else ""
        )
        self.summary_var.set(
            f"待打印 {result['pdf_count']} 份 · {removed_note}"
            f"金额识别 {result['amount_recognized_count']}/{result['pdf_count']} · "
            f"ZIP 排除 {result['zip_excluded_count']} · "
            f"ZIP 内跳过 {result.get('zip_member_excluded_count', 0)} · "
            f"跳过重复 {result.get('duplicate_skipped_count', 0)} · "
            f"列表重复 {duplicate_count} · 警告 {warning_count}"
        )
        self._update_action_states()

    def _on_tree_selection_changed(self, _event: object | None = None) -> None:
        selected_ids = self.tree.selection()
        if len(selected_ids) == 1 and not self.busy:
            selected_path = self.tree_record_paths.get(selected_ids[0])
            for label, path in self.stamp_target_paths.items():
                if path == selected_path:
                    self.stamp_target_var.set(label)
                    break
        self._update_action_states()

    def _cancel_tree_drag(self, _event: object | None = None) -> None:
        if self._tree_drag_after is not None:
            self.root.after_cancel(self._tree_drag_after)
            self._tree_drag_after = None
        self._tree_drag_anchor = None
        self._tree_drag_base = ()
        self._tree_drag_direction = 0

    def _on_tree_drag_start(self, event: tk.Event) -> None:
        self._cancel_tree_drag()
        if self.busy or self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        # Native bindings still handle ordinary, Ctrl and Shift clicks.
        self._tree_drag_anchor = (self.tree.focus() if event.state & 1 else item) or item
        self._tree_drag_base = self.tree.selection() if event.state & 4 else ()

    def _select_tree_drag_range(self, item: str) -> None:
        items = self.tree.get_children()
        if self._tree_drag_anchor not in items or item not in items:
            self._cancel_tree_drag()
            return
        first, last = sorted((items.index(self._tree_drag_anchor), items.index(item)))
        selected = tuple(dict.fromkeys((*self._tree_drag_base, *items[first:last + 1])))
        if set(selected) != set(self.tree.selection()):
            self.tree.selection_set(selected)

    def _tree_drag_edge_row(self, direction: int) -> str:
        height = self.tree.winfo_height()
        positions = range(height - 1, -1, -1) if direction > 0 else range(height)
        for y in positions:
            item = self.tree.identify_row(y)
            if item:
                return item
        return ""

    def _on_tree_drag_motion(self, event: tk.Event) -> str | None:
        if self._tree_drag_anchor is None:
            return None
        if self.busy:
            self._cancel_tree_drag()
            return "break"
        if self._tree_drag_after is not None:
            self.root.after_cancel(self._tree_drag_after)
            self._tree_drag_after = None
        top_item = self._tree_drag_edge_row(-1)
        top = self.tree.bbox(top_item)[1] if top_item else 0
        self._tree_drag_direction = (
            -1 if event.y <= top + 12 else
            1 if event.y >= self.tree.winfo_height() - 18 else 0
        )
        item = self.tree.identify_row(event.y)
        if not item and self._tree_drag_direction:
            item = self._tree_drag_edge_row(self._tree_drag_direction)
        if item:
            self._select_tree_drag_range(item)
        if self._tree_drag_direction and self._tree_drag_anchor is not None:
            self._tree_drag_after = self.root.after(80, self._scroll_tree_drag)
        return "break"

    def _scroll_tree_drag(self) -> None:
        self._tree_drag_after = None
        if self._tree_drag_anchor is None or self.busy or self.closing:
            self._cancel_tree_drag()
            return
        before = self.tree.yview()
        self.tree.yview_scroll(self._tree_drag_direction, "units")
        item = self._tree_drag_edge_row(self._tree_drag_direction)
        if item:
            self._select_tree_drag_range(item)
        if self.tree.yview() != before and self._tree_drag_anchor is not None:
            self._tree_drag_after = self.root.after(80, self._scroll_tree_drag)

    def on_select_all(self, _event: object | None = None) -> str:
        self._cancel_tree_drag()
        if not self.busy:
            self.tree.selection_set(self.tree.get_children())
            self._update_action_states()
        return "break"

    def _on_tree_mousewheel(self, event: tk.Event) -> str:
        if not event.delta:
            return "break"
        delta = -1 if event.delta > 0 else 1
        self.tree.yview_scroll(delta * 3, "units")
        return "break"

    def _on_tree_shift_mousewheel(self, event: tk.Event) -> str:
        if not event.delta:
            return "break"
        delta = -1 if event.delta > 0 else 1
        self.tree.xview_scroll(delta * 3, "units")
        return "break"

    def on_remove_selected(self, _event: object | None = None) -> str | None:
        self._cancel_tree_drag()
        if self.busy:
            return "break" if _event is not None else None
        selected = self.tree.selection()
        paths = {
            self.tree_record_paths[item_id]
            for item_id in selected
            if item_id in self.tree_record_paths
        }
        if not paths:
            self._update_action_states()
            return "break" if _event is not None else None

        self.removed_file_paths.update(paths)
        self.log(f"已从本次打印列表移除 {len(paths)} 份文件；磁盘原文件未删除。")
        self._refresh_scan_view()
        return "break" if _event is not None else None

    def on_browse(self) -> None:
        if self.busy:
            return
        initial = self.folder_var.get().strip()
        if not initial or not Path(initial).is_dir():
            initial = str(Path.home() / "Desktop")
        selected = filedialog.askdirectory(title="选择票据总文件夹（包含子文件夹）", initialdir=initial)
        if selected:
            self.source_mode = "folder"
            self.imported_paths = ()
            self.folder_var.set(selected)
            self.on_scan()

    def on_import_files(self) -> None:
        if self.busy:
            return
        selected = filedialog.askopenfilenames(
            title="导入 PDF、图片或 ZIP 文件",
            filetypes=(("PDF、图片和 ZIP", SUPPORTED_FILE_GLOB), ("所有文件", "*.*")),
        )
        if selected:
            self._start_file_scan(selected, append=False)

    def on_quick_scan(self) -> None:
        """Select files and immediately send them through the import scanner."""

        if self.busy:
            return
        selected = filedialog.askopenfilenames(
            title="一键扫描 PDF、图片或 ZIP 文件",
            filetypes=(("PDF、图片和 ZIP", SUPPORTED_FILE_GLOB), ("所有文件", "*.*")),
        )
        if selected:
            self._start_file_scan(selected, append=False)

    def on_add_files(self) -> None:
        if self.busy:
            return
        selected = filedialog.askopenfilenames(
            title="继续添加 PDF、图片或 ZIP 文件",
            filetypes=(("PDF、图片和 ZIP", SUPPORTED_FILE_GLOB), ("所有文件", "*.*")),
        )
        if selected:
            append = self.source_mode == "files" and bool(self.scan_result)
            self._start_file_scan(selected, append=append)

    def _normalize_imported_paths(self, paths: object) -> tuple[str, ...]:
        normalized: dict[str, str] = {}
        for value in paths if isinstance(paths, (list, tuple, set)) else ():
            path = Path(str(value)).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in engine.SUPPORTED_FILE_EXTENSIONS:
                continue
            normalized.setdefault(os.path.normcase(str(path)), str(path))
        return tuple(normalized.values())

    def _start_file_scan(self, paths: object, append: bool = False) -> None:
        if self.busy:
            return
        if self._special_copy_value() is None:
            messagebox.showwarning("专票份数无效", "专票份数必须是 1 到 99 之间的整数。", parent=self.root)
            return
        imported_paths = self._normalize_imported_paths(paths)
        if not imported_paths:
            messagebox.showwarning(
                "没有可导入文件",
                "请选择或拖入 PDF、图片或 ZIP 文件。图片会自动转换后进入打印明细。",
                parent=self.root,
            )
            return
        if append and self.source_mode == "files":
            imported_paths = self._normalize_imported_paths(
                (*self.imported_paths, *imported_paths)
            )
        previous = self._scan_previous_state()
        removed_paths = set(self.removed_file_paths) if append else set()
        self.source_mode = "files"
        self.imported_paths = imported_paths
        self.preset_var.set("智能")
        signature = self._current_signature()
        rail_copies = signature[2]
        other_copies = signature[3]
        self._begin_scan_preview(signature, imported_paths, removed_paths, previous, append)

        def analyze(progress):
            result = engine.analyze_files(
                imported_paths, rail_copies=rail_copies,
                other_copies=other_copies, progress=progress,
                special_copies=signature[4],
                on_record=lambda record, index, total: self.events.put(
                    ("scan_record", record, index, total, signature)
                ),
            )
            result["removed_paths"] = sorted(removed_paths)
            return result

        self._start_worker(
            "导入并识别文件",
            analyze,
            "scan_done",
            signature,
        )

    def _selected_stamp_record(self) -> dict | None:
        scan = self._active_scan_result()
        if not scan:
            messagebox.showwarning("请先扫描", "请先扫描或一键扫描文件。", parent=self.root)
            return None
        target_path = self.stamp_target_paths.get(self.stamp_target_var.get().strip())
        if not target_path:
            selected_ids = self.tree.selection()
            selected_paths = [
                self.tree_record_paths[item_id]
                for item_id in selected_ids
                if item_id in self.tree_record_paths
            ]
            target_path = selected_paths[0] if len(selected_paths) == 1 else None
        if not target_path:
            messagebox.showwarning(
                "请选择一个文件",
                "请选择目标 PDF；也可以在明细中只选择一个文件。",
                parent=self.root,
            )
            return None
        for record in scan["files"]:
            if record["path"] == target_path:
                return record
        messagebox.showwarning("文件不存在", "选中的文件已经不在当前列表中。", parent=self.root)
        return None

    def on_browse_stamp(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择印章图片",
            filetypes=(
                ("图片文件", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("所有文件", "*.*"),
            ),
        )
        if selected:
            self.stamp_image_var.set(selected)

    def on_stamp_selected(self) -> None:
        if self.busy:
            return
        record = self._selected_stamp_record()
        if not record:
            return
        stamp_path = self.stamp_image_var.get().strip()
        if not stamp_path or not Path(stamp_path).is_file():
            messagebox.showwarning("请选择印章", "请选择存在的 PNG/JPG/BMP/WEBP 印章图片。", parent=self.root)
            return
        try:
            size_mm = float(self.stamp_size_var.get().strip())
            if not math.isfinite(size_mm) or not 5 <= size_mm <= 100:
                raise ValueError("size out of range")
        except (TypeError, ValueError):
            messagebox.showwarning("大小无效", "印章大小请输入 5 到 100 之间的数字。", parent=self.root)
            return
        all_pages = bool(self.stamp_all_pages_var.get())
        page_text = self.stamp_pages_var.get().strip()
        try:
            page_numbers = engine.parse_page_selection(page_text, int(record["pages"]), all_pages)
        except ValueError as exc:
            messagebox.showwarning("页数无效", str(exc), parent=self.root)
            return
        mode_label = self.stamp_mode_var.get().strip()
        mode = STAMP_MODE_LABELS.get(mode_label)
        if mode is None:
            messagebox.showwarning("模式无效", "请选择普通盖章或齐缝章。", parent=self.root)
            return
        if mode == "seam" and len(page_numbers) < 2:
            messagebox.showwarning("页数无效", "齐缝章至少需要选择两页。", parent=self.root)
            return
        approved = messagebox.askyesno(
            "确认生成盖章文件",
            f"文件：{record['name']}\n"
            f"盖章页数：{'全部页数' if all_pages else page_text}\n"
            f"模式：{mode_label}\n大小：{size_mm:g} mm\n\n"
            "程序会生成新的盖章 PDF，原文件保留。是否继续？",
            parent=self.root,
        )
        if not approved:
            return
        rail_copies = self._copy_value(self.rail_copies_var, 2)
        other_copies = self._copy_value(self.other_copies_var, 1)
        special_copies = self._special_copy_value()

        def create_stamped_copy(progress):
            result = engine.stamp_pdf(
                record["path"], stamp_path, self._stamp_output_path(record["name"]),
                page_numbers=page_numbers, all_pages=all_pages, mode=mode,
                stamp_size_mm=size_mm, progress=progress,
            )
            scanned = engine.analyze_files(
                [result["output_path"]], rail_copies=rail_copies,
                other_copies=other_copies, progress=progress,
                special_copies=special_copies,
            )
            result["record"] = scanned["files"][0]
            # A generated filename must not change classification or copy counts.
            for key in ("rail", "copies", "classification"):
                result["record"][key] = record[key]
            result["record"]["special_vat"] = bool(record.get("special_vat"))
            return result

        self._start_worker(
            "生成盖章文件",
            create_stamped_copy,
            "stamp_done",
            {"record_path": record["path"], "mode_label": mode_label},
        )

    def _stamp_output_path(self, filename: str) -> Path:
        root = DATA_DIR / "stamped"
        root.mkdir(parents=True, exist_ok=True)
        stamp_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        return root / f"{Path(filename).stem}_盖章_{stamp_id}.pdf"

    @staticmethod
    def _point_in_rectangle(
        x: int, y: int, left: int, top: int, width: int, height: int
    ) -> bool:
        return left <= x < left + width and top <= y < top + height

    def _is_attachment_drop_point(self, screen_x: int, screen_y: int) -> bool:
        targets = (
            (self.empty_drop_frame,)
            if self.empty_drop_frame.winfo_manager()
            else (self.table_frame,)
        )
        for widget in targets:
            if not widget.winfo_exists() or not widget.winfo_ismapped():
                continue
            if self._point_in_rectangle(
                screen_x,
                screen_y,
                widget.winfo_rootx(),
                widget.winfo_rooty(),
                widget.winfo_width(),
                widget.winfo_height(),
            ):
                return True
        return False

    def _handle_file_drop(
        self, paths: tuple[str, ...], screen_x: int, screen_y: int
    ) -> None:
        if self.busy:
            return
        if not self._is_attachment_drop_point(screen_x, screen_y):
            self.status_var.set("请将附件拖入蓝色附件区域或文件明细列表。")
            self.log("已忽略附件区域外的文件拖放。")
            return
        self._set_drop_hover(False)
        append = self.source_mode == "files" and bool(self.scan_result)
        self._start_file_scan(paths, append=append)

    @staticmethod
    def _parse_dnd_paths(data: object, splitlist: Callable[[str], tuple[str, ...]]) -> tuple[str, ...]:
        raw_values = splitlist(str(data or ""))
        if len(raw_values) > MAX_DROPPED_FILES:
            raise ValueError(f"拖放文件数量异常：{len(raw_values)}")
        paths: list[str] = []
        for value in raw_values:
            path = str(value).strip()
            if not path or len(path) > MAX_DROPPED_PATH_CHARS:
                raise ValueError("拖放路径为空或长度异常。")
            paths.append(path)
        return tuple(paths)

    def _on_tkdnd_drop(self, event: object) -> str:
        try:
            if self.busy:
                return REFUSE_DROP
            data = getattr(event, "data", "")
            paths = self._parse_dnd_paths(data, self.root.tk.splitlist)
            screen_x = int(event.x_root)
            screen_y = int(event.y_root)
            if not paths or not self._is_attachment_drop_point(screen_x, screen_y):
                return REFUSE_DROP
            self.root.after_idle(self._dispatch_file_drop, paths, screen_x, screen_y)
            return COPY
        except Exception as exc:
            self.status_var.set("拖放失败，请点击“选择附件”重试。")
            self.log(f"TkDnD 文件拖放失败：{exc}")
            return REFUSE_DROP

    def _dispatch_file_drop(self, paths: tuple[str, ...], screen_x: int, screen_y: int) -> None:
        try:
            self._handle_file_drop(paths, screen_x, screen_y)
        except Exception as exc:
            self._set_busy(False, "拖放失败，请点击“选择附件”重试。")
            self.log(f"拖入文件处理失败：{exc}")

    # Kept for compatibility with older diagnostics; the live UI no longer registers WM_DROPFILES.
    @staticmethod
    def _read_dropped_paths(shell32: object, drop_handle: object) -> tuple[str, ...]:
        import ctypes

        count = int(shell32.DragQueryFileW(drop_handle, 0xFFFFFFFF, None, 0))
        if count < 0 or count > MAX_DROPPED_FILES:
            raise ValueError(f"拖放文件数量异常：{count}")
        dropped: list[str] = []
        for index in range(count):
            length = int(shell32.DragQueryFileW(drop_handle, index, None, 0))
            if length <= 0 or length > MAX_DROPPED_PATH_CHARS:
                raise ValueError(f"第 {index + 1} 个拖放路径长度异常：{length}")
            buffer = ctypes.create_unicode_buffer(length + 1)
            copied = int(shell32.DragQueryFileW(drop_handle, index, buffer, len(buffer)))
            if copied != length or not buffer.value:
                raise ValueError(f"第 {index + 1} 个拖放路径读取不完整")
            dropped.append(buffer.value)
        return tuple(dropped)

    def _handle_native_drop_error(self, message: str) -> None:
        self.status_var.set("拖放失败，请点击“选择附件”重试。")
        self.log(f"原生文件拖放失败：{message}")

    def _schedule_native_drop_error(self, message: str) -> None:
        try:
            self.root.after(0, self._handle_native_drop_error, message)
        except BaseException:
            pass

    def _process_native_drop(
        self, shell32: object, user32: object, window: int, wparam: int
    ) -> int:
        """Compatibility boundary for diagnostics; never installed as a window procedure."""

        import ctypes
        from ctypes import wintypes

        dropped: tuple[str, ...] = ()
        point = wintypes.POINT()
        drop_handle = wintypes.HANDLE(wparam)
        try:
            if shell32.DragQueryPoint(drop_handle, ctypes.byref(point)):
                if not user32.ClientToScreen(window, ctypes.byref(point)):
                    raise ctypes.WinError()
            elif not user32.GetCursorPos(ctypes.byref(point)):
                raise ctypes.WinError()
            dropped = self._read_dropped_paths(shell32, drop_handle)
        except BaseException as exc:
            self._schedule_native_drop_error(str(exc) or type(exc).__name__)
        finally:
            try:
                shell32.DragFinish(drop_handle)
            except BaseException as exc:
                self._schedule_native_drop_error(f"释放拖放句柄失败：{exc}")
        if dropped:
            try:
                self.root.after(0, self._handle_file_drop, dropped, int(point.x), int(point.y))
            except BaseException as exc:
                self._schedule_native_drop_error(f"提交拖放任务失败：{exc}")
        return 0

    def _enable_file_drop(self) -> None:
        self._drop_dnd_enabled = False
        self._drop_target_registered = False
        self._drop_bind_id: str | None = None
        if DND_FILES is None or not hasattr(self.root, "drop_target_register"):
            self.log("TkDnD 不可用，可继续使用“一键扫描文件”或“导入文件”。")
            return
        try:
            self.root.drop_target_register(DND_FILES)
            self._drop_target_registered = True
            self._drop_bind_id = self.root.dnd_bind("<<Drop>>", self._on_tkdnd_drop)
            self._drop_dnd_enabled = True
            self.log("已启用 TkDnD 文件拖放。")
        except Exception as exc:
            self.log(f"TkDnD 初始化失败，可继续使用“一键扫描文件”：{exc}")
            if self._drop_target_registered:
                try:
                    self.root.drop_target_unregister()
                except Exception as unregister_exc:
                    self.log(f"TkDnD 初始化回滚失败：{unregister_exc}")
            self._drop_target_registered = False
            self._drop_bind_id = None

    def _disable_file_drop(self) -> None:
        if not (
            getattr(self, "_drop_dnd_enabled", False)
            or getattr(self, "_drop_target_registered", False)
            or getattr(self, "_drop_bind_id", None)
        ):
            return
        bind_id = getattr(self, "_drop_bind_id", None)
        try:
            if bind_id:
                # TkDnD binds a bare Tcl callback rather than Tkinter's wrapper.
                binding = str(self.root.dnd_bind("<<Drop>>"))
                remaining = "\n".join(
                    line for line in binding.splitlines()
                    if line.split(maxsplit=1)[:1] != [bind_id]
                )
                self.root.tk.call("bind", self.root._w, "<<Drop>>", remaining)
                if self.root.tk.call("info", "commands", bind_id):
                    self.root.deletecommand(bind_id)
        except Exception as exc:
            self.log(f"TkDnD 事件解绑失败：{exc}")
        try:
            if getattr(self, "_drop_target_registered", False):
                self.root.drop_target_unregister()
        except Exception as exc:
            self.log(f"TkDnD 目标注销失败：{exc}")
        finally:
            self._drop_dnd_enabled = False
            self._drop_target_registered = False
            self._drop_bind_id = None

    def on_refresh_printers(self) -> None:
        self._refresh_printers(show_error=True)

    def _refresh_printers(self, show_error: bool) -> None:
        try:
            printers = engine.list_printers()
            names = [str(item.get("Name", "")) for item in printers if item.get("Name")]
            self.printer_combo["values"] = names
            current = self.printer_var.get().strip()
            if current not in names:
                default_printer = engine.get_default_printer()
                self.printer_var.set(default_printer if default_printer in names else "")
                if names and not self.printer_var.get():
                    self.status_var.set("请选择打印机")
                    self.log("未找到可用的系统默认打印机，请手动选择。")
            self.log(f"已读取 {len(names)} 台打印机。")
        except Exception as exc:
            self.log(f"读取打印机失败：{exc}")
            if show_error:
                messagebox.showerror("读取打印机失败", str(exc), parent=self.root)

    def on_scan(self) -> None:
        if self.busy:
            return
        if self._special_copy_value() is None:
            messagebox.showwarning("专票份数无效", "专票份数必须是 1 到 99 之间的整数。", parent=self.root)
            return
        folder = self.folder_var.get().strip()
        if not folder:
            self.on_browse()
            return
        previous = self._scan_previous_state()
        if self.source_mode != "folder":
            self.source_mode = "folder"
            self.imported_paths = ()
            self._invalidate_scan()
        signature = self._current_signature()
        rail_copies = signature[2]
        other_copies = signature[3]
        self._begin_scan_preview(signature, (), set(), previous)
        self._start_worker(
            "扫描文件",
            lambda progress: engine.analyze_folder(
                folder,
                rail_copies=rail_copies,
                other_copies=other_copies,
                special_copies=signature[4],
                progress=progress,
                on_record=lambda record, index, total: self.events.put(
                    ("scan_record", record, index, total, signature)
                ),
            ),
            "scan_done",
            signature,
        )

    def _require_scan(self, require_printer: bool = True) -> dict | None:
        if not self.scan_result or self.scan_signature != self._current_signature():
            messagebox.showwarning(
                "请先扫描",
                "请先点击“扫描文件”，确认文件分类和份数。",
                parent=self.root,
            )
            return None
        result = self._active_scan_result()
        if not result or not result["files"]:
            messagebox.showwarning(
                "待打印列表为空",
                "当前列表没有可打印文件。重新扫描可以恢复已移除的文件。",
                parent=self.root,
            )
            return None
        if require_printer and not self.printer_var.get().strip():
            messagebox.showwarning("请选择打印机", "当前没有选择打印机。", parent=self.root)
            return None
        return result

    def _selected_scan_result(self, scan: dict) -> dict | None:
        selected_paths = {
            self.tree_record_paths[item_id]
            for item_id in self.tree.selection()
            if item_id in self.tree_record_paths
        }
        selected_scan = engine.filter_scan_result(
            scan,
            {item["path"] for item in scan["files"] if item["path"] not in selected_paths},
        )
        if not selected_scan["files"]:
            messagebox.showwarning(
                "请选择文件",
                "请先在文件明细中选中需要打印或复印的文件，可按 Ctrl 或 Shift 多选。",
                parent=self.root,
            )
            return None
        return selected_scan

    def _print_settings(self) -> tuple[str, str, dict]:
        return (
            self.printer_var.get().strip(),
            self.paper_var.get(),
            self._current_preset(),
        )

    def on_print_selected(self) -> None:
        scan = self._require_scan()
        if not scan or self.busy:
            return
        scan = self._selected_scan_result(scan)
        if not scan:
            return
        printer, paper_size, preset = self._print_settings()
        expected_jobs = engine.expected_print_jobs(scan, preset["duplex"])
        approved = messagebox.askyesno(
            "确认选择打印",
            f"已选择 PDF：{scan['pdf_count']} 份\n"
            f"铁路票：{scan['rail_count']} 份；专票：{scan.get('special_vat_count', 0)} 份；其他 PDF：{scan['regular_count']} 份\n"
            f"识别金额：¥{scan['total_amount']}（{scan['amount_recognized_count']}/{scan['pdf_count']} 份已识别）\n"
            "未识别金额的 PDF 也会按当前选择打印。\n"
            f"预计打印作业：{expected_jobs} 个（按当前份数设置）\n"
            f"打印机：{printer}\n"
            f"纸张：{paper_size}\n"
            f"边距：{self._margin_value():g} mm（四边）\n"
            f"规则：{preset['name']}（{preset['description']}）\n\n"
            "是否打印？",
            parent=self.root,
        )
        if approved:
            self._save_settings()
            margin_mm = self._margin_value()
            context = self._make_print_context(
                scan, "selected", "选择打印", printer, paper_size, preset, margin_mm
            )
            self.active_print_context = context
            self._start_worker(
                "选择打印",
                lambda progress: engine.print_scan(
                    scan,
                    printer=printer,
                    sample_first_rail=False,
                    paper_size=paper_size,
                    dpi=preset["dpi"],
                    duplex=preset["duplex"],
                    high_quality=preset["high_quality"],
                    orientation_mode=preset["orientation_mode"],
                    scale_mode=preset["scale_mode"],
                    margin_mm=margin_mm,
                    progress=progress,
                ),
                "print_done",
                "selected",
            )

    def on_print_all(self) -> None:
        scan = self._require_scan()
        if not scan or self.busy:
            return
        printer, paper_size, preset = self._print_settings()
        expected_jobs = engine.expected_print_jobs(scan, preset["duplex"])
        duplicate_note = (
            f"\n已跳过重复导入：{scan['duplicate_skipped_count']} 份，相同内容只打印一份。"
            if scan.get("duplicate_skipped_count")
            else ""
        )
        zip_note = (
            f"ZIP：{scan.get('zip_count', 0)} 个；内部 PDF：{scan.get('zip_internal_count', 0)} 个；"
            f"排除：{scan.get('zip_excluded_count', 0)} 个\n"
            f"ZIP 内文件跳过：{scan.get('zip_member_excluded_count', 0)} 个（原因见日志）\n"
            if scan.get("zip_count")
            else ""
        )
        approved = messagebox.askyesno(
            "确认全部打印",
            f"文件夹：{scan['folder']}\n"
            f"PDF：{scan['pdf_count']} 份\n"
            f"{zip_note}"
            f"铁路票：{scan['rail_count']} 份；专票：{scan.get('special_vat_count', 0)} 份；其他 PDF：{scan['regular_count']} 份\n"
            f"识别金额：¥{scan['total_amount']}（{scan['amount_recognized_count']}/{scan['pdf_count']} 份已识别）\n"
            "未识别金额的 PDF 也会打印。\n"
            f"预计打印作业：{expected_jobs} 个\n"
            f"打印机：{printer}"
            f"{duplicate_note}\n\n"
            f"纸张：{paper_size}\n"
            f"边距：{self._margin_value():g} mm（四边）\n"
            f"规则：{preset['name']}（{preset['description']}）\n"
            "是否打印？",
            parent=self.root,
        )
        if approved:
            self._save_settings()
            margin_mm = self._margin_value()
            context = self._make_print_context(
                scan, "batch", "打印全部", printer, paper_size, preset, margin_mm
            )
            self.active_print_context = context
            self._start_worker(
                "批量打印",
                lambda progress: engine.print_scan(
                    scan,
                    printer=printer,
                    sample_first_rail=False,
                    paper_size=paper_size,
                    dpi=preset["dpi"],
                    duplex=preset["duplex"],
                    high_quality=preset["high_quality"],
                    orientation_mode=preset["orientation_mode"],
                    scale_mode=preset["scale_mode"],
                    margin_mm=margin_mm,
                    progress=progress,
                ),
                "print_done",
                "batch",
            )

    def on_copy_selected(self) -> None:
        copies = self._validated_copy_count()
        if copies is None:
            self._update_action_states()
            self.status_var.set("复印已关闭；请手动输入 1–99 的整数份数。")
            return
        scan = self._require_scan()
        if not scan or self.busy:
            return
        scan = self._selected_scan_result(scan)
        if not scan:
            return
        scan = engine.with_uniform_copies(scan, copies)
        printer, paper_size, preset = self._print_settings()
        expected_jobs = engine.expected_print_jobs(scan, preset["duplex"])
        approved = messagebox.askyesno(
            "确认复印",
            f"已选择 PDF：{scan['pdf_count']} 份\n"
            f"统一复印份数：{copies} 份\n"
            f"识别金额：¥{scan['total_amount']}（金额不乘复印份数）\n"
            f"预计打印作业：{expected_jobs} 个\n"
            f"打印机：{printer}\n"
            f"纸张：{paper_size}\n"
            f"边距：{self._margin_value():g} mm（四边）\n"
            f"规则：{preset['name']}（{preset['description']}）\n\n"
            "是否复印？",
            parent=self.root,
        )
        if approved:
            self._save_settings()
            margin_mm = self._margin_value()
            context = self._make_print_context(
                scan, "copy", "按份数打印所选", printer, paper_size, preset, margin_mm
            )
            context["copies"] = copies
            self.active_print_context = context
            self._start_worker(
                "复印所选",
                lambda progress: engine.print_scan(
                    scan,
                    printer=printer,
                    sample_first_rail=False,
                    paper_size=paper_size,
                    dpi=preset["dpi"],
                    duplex=preset["duplex"],
                    high_quality=preset["high_quality"],
                    orientation_mode=preset["orientation_mode"],
                    scale_mode=preset["scale_mode"],
                    margin_mm=margin_mm,
                    progress=progress,
                ),
                "print_done",
                "copy",
            )

    def on_print_preview(self) -> None:
        scan = self._require_scan(require_printer=False)
        if not scan or self.busy:
            return
        selected_ids = self.tree.selection()
        selected_paths = {
            self.tree_record_paths[item_id]
            for item_id in selected_ids
            if item_id in self.tree_record_paths
        }
        record = next(
            (item for item in scan["files"] if item["path"] in selected_paths),
            scan["files"][0],
        )
        preset = self._current_preset()
        paper_size = self.paper_var.get()
        margin_mm = self._margin_value()
        paper_dims = {"A4": (210.0, 297.0), "A5": (148.0, 210.0)}
        paper_w, paper_h = paper_dims.get(paper_size, paper_dims["A5"])
        source_path = Path(str(record["path"])).expanduser()
        try:
            document = engine.pdfium.PdfDocument(str(source_path))
            page = document[0]
            image = page.render(scale=1.35, rotation=0).to_pil().convert("RGB")
            try:
                page_size = page.get_size()
                source_w_mm = float(page_size[0]) * 25.4 / 72.0
                source_h_mm = float(page_size[1]) * 25.4 / 72.0
            except Exception:
                source_w_mm, source_h_mm = 210.0, 297.0
            document.close()
        except Exception as exc:
            messagebox.showerror("打印预览失败", str(exc), parent=self.root)
            return

        orientation_mode = preset.get("orientation_mode", "auto")
        if orientation_mode == "auto":
            orientation = "横向" if source_w_mm > source_h_mm else "纵向"
        else:
            orientation = "横向" if orientation_mode == "landscape" else "纵向"
        if orientation == "横向":
            paper_w, paper_h = paper_h, paper_w
        usable_w = max(1.0, paper_w - margin_mm * 2)
        usable_h = max(1.0, paper_h - margin_mm * 2)
        scale_mode = preset.get("scale_mode", "fit")
        actual_scale = min(usable_w / max(source_w_mm, 1.0), usable_h / max(source_h_mm, 1.0))
        layout_mode = scale_mode
        if scale_mode == "smart":
            layout_mode = "actual" if actual_scale >= 1.0 else "fit"
        if layout_mode == "fit":
            layout_scale = min(usable_w / max(source_w_mm, 1.0), usable_h / max(source_h_mm, 1.0))
        else:
            layout_scale = 1.0
        content_w = max(4.0, source_w_mm * layout_scale)
        content_h = max(4.0, source_h_mm * layout_scale)

        dialog = tk.Toplevel(self.root)
        dialog.title(f"打印预览 · {record['name']}")
        dialog.transient(self.root)
        dialog.geometry("760x700")
        body = ttk.Frame(dialog, padding=14, style="Surface.TFrame")
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text=f"示例文件：{record['name']}",
            style="Section.TLabelframe.Label",
        ).pack(anchor=tk.W)
        ttk.Label(
            body,
            text=(
                f"纸张 {paper_size} · 方向 {orientation} · "
                f"缩放 {'适合边距' if layout_mode == 'fit' else '实际大小'} · "
                f"边距 {margin_mm:g} mm · 预设 {preset['name']}"
            ),
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(3, 10))
        canvas = tk.Canvas(body, width=700, height=580, bg="#E8EEF7", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        def draw(_event: object | None = None) -> None:
            width = max(320, canvas.winfo_width())
            height = max(320, canvas.winfo_height())
            canvas.delete("all")
            paper_scale = min((width - 80) / paper_w, (height - 50) / paper_h)
            left = (width - paper_w * paper_scale) / 2
            top = (height - paper_h * paper_scale) / 2
            right = left + paper_w * paper_scale
            bottom = top + paper_h * paper_scale
            canvas.create_rectangle(left + 5, top + 7, right + 5, bottom + 7, fill="#CBD5E1", outline="")
            canvas.create_rectangle(left, top, right, bottom, fill="#FFFFFF", outline="#94A3B8", width=1)
            margin_px = margin_mm * paper_scale
            canvas.create_rectangle(
                left + margin_px, top + margin_px,
                right - margin_px, bottom - margin_px,
                outline="#93C5FD", dash=(4, 3), width=1,
            )
            display_w = max(8, int(content_w * paper_scale))
            display_h = max(8, int(content_h * paper_scale))
            preview_image = image.resize((display_w, display_h), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(preview_image)
            dialog._preview_photo = photo
            center_x = (left + right) / 2
            center_y = (top + bottom) / 2
            canvas.create_image(center_x, center_y, image=photo)
            canvas.create_text(
                left + 12, bottom - 14,
                text="蓝色虚线为打印边距，内容区域为页面实际版面示意",
                anchor=tk.W, fill=MUTED, font=(FONT_FAMILY, 9),
            )

        canvas.bind("<Configure>", draw)
        draw()
        ttk.Button(body, text="关闭", command=dialog.destroy).pack(anchor=tk.E, pady=(10, 0))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.focus_set()

    def on_export_pdf(self) -> None:
        scan = self._require_scan(require_printer=False)
        if not scan or self.busy:
            return
        selected_ids = self.tree.selection()
        selected_paths = {
            self.tree_record_paths[item_id]
            for item_id in selected_ids
            if item_id in self.tree_record_paths
        }
        records = [
            record for record in scan["files"]
            if not selected_paths or record["path"] in selected_paths
        ]
        output = filedialog.asksaveasfilename(
            title="合并/转换 PDF",
            defaultextension=".pdf",
            filetypes=(("PDF 文件", "*.pdf"), ("所有文件", "*.*")),
            initialfile="打印快捷工具合并.pdf",
        )
        if not output:
            return
        output_path = Path(output).expanduser()
        if output_path.suffix.lower() != ".pdf":
            output_path = output_path.with_suffix(".pdf")
        self._start_worker(
            "合并/转换 PDF",
            lambda progress: engine.export_scan_pdf(
                scan,
                output_path,
                records=records,
                progress=progress,
            ),
            "export_done",
            str(output_path),
        )

    def on_print_history(self) -> None:
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.deiconify()
            self.history_window.lift()
            self._refresh_history_view()
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("打印队列记录")
        dialog.transient(self.root)
        dialog.geometry("900x470")
        self.history_window = dialog
        body = ttk.Frame(dialog, padding=12, style="Surface.TFrame")
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text="查看提交结果、队列观察和失败原因；重试前请确认源文件仍在当前附件列表。",
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(0, 8))
        frame = ttk.Frame(body, style="Surface.TFrame")
        frame.pack(fill=tk.BOTH, expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = ("time", "operation", "printer", "paper", "preset", "jobs", "queue", "status", "failure")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "time": "时间", "operation": "操作", "printer": "打印机", "paper": "纸张",
            "preset": "规则", "jobs": "提交/预计", "queue": "队列观察", "status": "结果", "failure": "失败原因",
        }
        widths = {"time": 145, "operation": 110, "printer": 155, "paper": 55, "preset": 75, "jobs": 85, "queue": 75, "status": 60, "failure": 200}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], minwidth=45, stretch=column in {"printer", "failure"})
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self.history_tree = tree
        self.history_records_by_item = {}

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.pack(fill=tk.X, pady=(10, 0))
        retry_button = ttk.Button(buttons, text="重试选中记录", command=self.on_retry_history)
        retry_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="刷新", command=self._refresh_history_view).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self._refresh_history_view()
        dialog.focus_set()

    def _refresh_history_view(self) -> None:
        tree = self.history_tree
        if tree is None or not tree.winfo_exists():
            return
        for item in tree.get_children():
            tree.delete(item)
        self.history_records_by_item = {}
        for record in reversed(self.print_history):
            submitted = int(record.get("submitted_jobs", 0) or 0)
            expected = int(record.get("expected_jobs", 0) or 0)
            failure = str(record.get("failure_reason", ""))
            item_id = tree.insert(
                "", tk.END,
                values=(
                    str(record.get("timestamp", "")).replace("T", " "),
                    record.get("operation", "打印"),
                    record.get("printer", ""),
                    record.get("paper_size", ""),
                    record.get("preset", ""),
                    f"{submitted}/{expected}",
                    str(record.get("observed_queue_jobs", 0)),
                    record.get("status", "失败"),
                    failure[:160],
                ),
            )
            self.history_records_by_item[item_id] = record

    def on_retry_history(self) -> None:
        tree = self.history_tree
        if tree is None or not tree.winfo_exists():
            return
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("请选择记录", "请先选择一条打印记录。", parent=self.history_window or self.root)
            return
        record = self.history_records_by_item.get(selected[0], {})
        if record.get("mode") == "calibration":
            messagebox.showinfo("暂不支持重试校准页", "校准页参数需要重新确认，请从高级功能重新提交。", parent=self.history_window or self.root)
            return
        if self.busy:
            return
        scan = self._active_scan_result()
        expected_paths = {str(path) for path in record.get("source_paths", []) if path}
        current_paths = {str(item.get("path", "")) for item in (scan or {}).get("files", [])}
        if not scan or not expected_paths or not expected_paths.issubset(current_paths):
            messagebox.showwarning(
                "无法直接重试",
                "当前附件列表缺少历史记录中的源文件。请先重新导入同一批源文件，再重试。",
                parent=self.history_window or self.root,
            )
            return
        scan = engine.filter_scan_result(
            scan,
            current_paths - expected_paths,
        )
        printer = str(record.get("printer", "")).strip()
        if not printer:
            messagebox.showwarning("缺少打印机", "历史记录没有可用打印机，请重新选择打印机。", parent=self.history_window or self.root)
            return
        paper_size = str(record.get("paper_size", self.paper_var.get()))
        preset = engine.get_print_preset(str(record.get("preset", engine.DEFAULT_PRINT_PRESET)))
        margin_mm = float(record.get("margin_mm", 0.0) or 0.0)
        mode = str(record.get("mode", "batch"))
        copies = 0
        if mode == "copy":
            copies = int(record.get("copies", 0) or 0)
            if copies > 0:
                scan = engine.with_uniform_copies(scan, copies)
        else:
            source_copies = record.get("source_copies", {})
            if isinstance(source_copies, dict):
                restored_files = []
                for item in scan.get("files", []):
                    restored = dict(item)
                    try:
                        restored["copies"] = max(
                            1, int(source_copies.get(item.get("path"), restored.get("copies", 1)))
                        )
                    except (TypeError, ValueError):
                        pass
                    restored_files.append(restored)
                scan = dict(scan)
                scan["files"] = restored_files
        if not messagebox.askyesno(
            "确认重试打印",
            f"文件：{scan['pdf_count']} 份\n"
            f"预计打印作业：{engine.expected_print_jobs(scan, preset['duplex'])} 个\n"
            f"打印机：{printer}\n纸张：{paper_size}\n"
            f"规则：{preset['name']}\n边距：{margin_mm:g} mm（四边）\n\n"
            "重试会再次提交打印，原记录中已提交的作业可能重复出纸。是否继续？",
            parent=self.history_window or self.root,
        ):
            return
        operation = "重试" + str(record.get("operation", "打印"))
        context = self._make_print_context(scan, mode, operation, printer, paper_size, preset, margin_mm)
        context["copies"] = copies
        self.active_print_context = context
        self._start_worker(
            operation,
            lambda progress: engine.print_scan(
                scan,
                printer=printer,
                sample_first_rail=False,
                paper_size=paper_size,
                dpi=preset["dpi"],
                duplex=preset["duplex"],
                high_quality=preset["high_quality"],
                orientation_mode=preset["orientation_mode"],
                scale_mode=preset["scale_mode"],
                margin_mm=margin_mm,
                progress=progress,
            ),
            "print_done",
            context,
        )
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.destroy()
            self.history_window = None

    def on_validate_print_rules(self) -> None:
        scan = self._require_scan()
        if not scan or self.busy:
            return
        printer, paper_size, preset = self._print_settings()
        margin_mm = self._margin_value()
        self._start_worker(
            "校验打印规则",
            lambda progress: engine.validate_print_scan(
                scan,
                printer=printer,
                paper_size=paper_size,
                dpi=preset["dpi"],
                duplex=preset["duplex"],
                high_quality=preset["high_quality"],
                orientation_mode=preset["orientation_mode"],
                scale_mode=preset["scale_mode"],
                margin_mm=margin_mm,
                progress=progress,
            ),
            "validation_done",
            preset["name"],
        )

    def on_print_calibration_page(self) -> None:
        if self.busy:
            return
        printer = self.printer_var.get().strip()
        if not printer:
            messagebox.showwarning("请选择打印机", "当前没有选择打印机。", parent=self.root)
            return

        paper_size = self.paper_var.get()
        dialog = tk.Toplevel(self.root)
        dialog.title("打印校准页")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=16, style="Surface.TFrame")
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=f"打印机：{printer}").grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8)
        )
        ttk.Label(body, text=f"纸张：{paper_size}").grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=(0, 10)
        )

        copies_var = tk.StringVar(master=dialog, value="1")
        orientation_var = tk.StringVar(master=dialog, value="自动（按页面方向）")
        ttk.Label(body, text="份数：").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.calibration_copies_spin = ttk.Spinbox(
            body, from_=1, to=99, width=8, textvariable=copies_var, justify=tk.CENTER
        )
        self.calibration_copies_spin.grid(row=2, column=1, sticky=tk.W, pady=4)
        ttk.Label(body, text="方向：").grid(row=3, column=0, sticky=tk.W, pady=4)
        self.calibration_orientation_combo = ttk.Combobox(
            body,
            textvariable=orientation_var,
            values=("自动（按页面方向）", "横向", "纵向"),
            state="readonly",
            width=18,
        )
        self.calibration_orientation_combo.grid(row=3, column=1, sticky=tk.W, pady=4)
        ttk.Label(
            body,
            text="单面 · 600 DPI · 实际大小 · 居中\n用尺检查 100 × 100 mm 方框。",
            style="Muted.TLabel",
            justify=tk.LEFT,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 14))

        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.grid(row=5, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(
            side=tk.RIGHT, padx=(8, 0)
        )

        def submit() -> None:
            raw_copies = copies_var.get().strip()
            if not raw_copies.isascii() or not raw_copies.isdecimal():
                messagebox.showwarning(
                    "份数无效", "请输入 1 到 99 之间的整数。", parent=dialog
                )
                return
            copies = int(raw_copies)
            if copies < 1 or copies > 99:
                messagebox.showwarning(
                    "份数无效", "请输入 1 到 99 之间的整数。", parent=dialog
                )
                return

            orientation_modes = {
                "自动（按页面方向）": "auto",
                "横向": "landscape",
                "纵向": "portrait",
            }
            orientation_label = orientation_var.get()
            orientation_mode = orientation_modes.get(orientation_label)
            if orientation_mode is None:
                messagebox.showwarning(
                    "方向无效", "请选择自动、横向或纵向。", parent=dialog
                )
                return

            approved = messagebox.askyesno(
                "确认打印校准页",
                f"打印机：{printer}\n纸张：{paper_size}\n份数：{copies}\n"
                f"方向：{orientation_label}\n边距：{self._margin_value():g} mm（四边）\n"
                "规则：单面 · 600 DPI · 实际大小 · 居中\n\n"
                "校准页会提交到打印机队列，是否继续？",
                parent=dialog,
            )
            if not approved:
                return
            dialog.destroy()
            margin_mm = self._margin_value()
            self.active_print_context = {
                "mode": "calibration",
                "operation": "打印校准页",
                "printer": printer,
                "paper_size": paper_size,
                "preset": "校准页",
                "margin_mm": margin_mm,
                "expected_jobs": copies,
                "source_names": [],
                "source_paths": [],
                "calibration_copies": copies,
                "orientation_mode": orientation_mode,
            }
            self._start_worker(
                "打印校准页",
                lambda progress: engine.print_calibration_page(
                    printer=printer,
                    paper_size=paper_size,
                    copies=copies,
                    orientation_mode=orientation_mode,
                    margin_mm=margin_mm,
                    progress=progress,
                ),
                "print_done",
                "calibration",
            )

        ttk.Button(
            buttons,
            text="打印校准页",
            command=submit,
            style="Primary.TButton",
        ).pack(side=tk.RIGHT)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        self.calibration_copies_var = copies_var
        self.calibration_orientation_var = orientation_var
        self.calibration_dialog = dialog
        self.calibration_submit_button = buttons.winfo_children()[-1]
        dialog.focus_set()

    def _start_worker(self, title: str, function, event_name: str, context) -> None:
        if self.closing:
            return
        self._set_busy(True, title)
        controller = engine.PrintController()
        self.worker_controller = controller
        if event_name == "print_done" and isinstance(context, dict):
            self.active_print_context = dict(context)

        def worker() -> None:
            try:
                with controller:
                    result = function(lambda message: self.events.put(("log", message)))
                self.events.put((event_name, result, context))
            except Exception as exc:
                self.events.put(("error", title, str(exc), traceback.format_exc(), event_name, context))

        self.worker_thread = threading.Thread(target=worker, name="print-task", daemon=True)
        self.worker_thread.start()

    def _poll_events(self) -> None:
        self._poll_after = None
        if self.closing:
            return
        deadline = time.monotonic() + 0.008
        try:
            for _ in range(40):
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "tray_show":
                    self._show_from_tray()
                elif kind == "tray_exit":
                    self._show_from_tray()
                    self.on_close()
                    if self.closing:
                        return
                elif kind == "tray_failed":
                    if self._tray_hidden:
                        self._show_from_tray()
                    self.log(f"系统托盘不可用：{event[1]}。窗口保留在任务栏。")
                elif kind == "log":
                    self.log(event[1])
                elif kind == "scan_record":
                    self._handle_scan_record(*event[1:])
                elif kind == "scan_done":
                    self._handle_scan_done(event[1], event[2])
                elif kind == "stamp_done":
                    self._handle_stamp_done(event[1], event[2])
                elif kind == "print_done":
                    self._handle_print_done(event[1], event[2])
                elif kind == "validation_done":
                    self._handle_validation_done(event[1], event[2])
                elif kind == "export_done":
                    self._handle_export_done(event[1], event[2])
                elif kind == "error":
                    self._handle_error(event[1], event[2], event[3], event[4] if len(event) > 4 else "", event[5] if len(event) > 5 else None)
                if time.monotonic() >= deadline:
                    break
        except queue.Empty:
            pass
        self._poll_after = self.root.after(1 if not self.events.empty() else 20, self._poll_events)

    def _handle_scan_done(self, result: dict, signature: tuple) -> None:
        self._scan_preview = None
        self.scan_result = result
        self.scan_signature = signature
        self.removed_file_paths = set(result.pop("removed_paths", ()))
        self._refresh_scan_view(reset_scroll=True)
        self._save_settings()
        self._set_busy(False, "扫描完成")

    def _refresh_stamp_targets(self, result: dict | None = None) -> None:
        result = result or self._active_scan_result()
        if not hasattr(self, "stamp_target_combo"):
            return
        records = list((result or {}).get("files", []))
        self.stamp_target_paths = {}
        for record in records:
            label = str(record["name"])
            if label in self.stamp_target_paths:
                label = f"{label} · {Path(record['path']).parent.name}"
            suffix = 2
            base_label = label
            while label in self.stamp_target_paths:
                label = f"{base_label} ({suffix})"
                suffix += 1
            self.stamp_target_paths[label] = record["path"]
        values = tuple(self.stamp_target_paths)
        self.stamp_target_combo.configure(values=values)
        current = self.stamp_target_var.get()
        if current not in values:
            self.stamp_target_var.set(values[0] if values else "")

    def _handle_stamp_done(self, result: dict, context: dict) -> None:
        try:
            output_path = Path(result["output_path"]).resolve()
            new_record = dict(result["record"])
            new_record.update(
                {
                    "stamped": True,
                    "stamp_mode": result["mode"],
                    "stamp_pages": result["pages"],
                    "source_path": context["record_path"],
                }
            )
            base_scan = dict(self.scan_result or {})
            base_scan["files"] = [
                new_record
                if record["path"] == context["record_path"]
                else record
                for record in base_scan.get("files", [])
            ]
            self.scan_result = engine.filter_scan_result(base_scan, self.removed_file_paths)
            if self.source_mode == "files":
                self.imported_paths = tuple(
                    str(output_path) if path == context["record_path"] else path
                    for path in self.imported_paths
                )
                self.scan_signature = self._current_signature()
            self.stamp_temp_paths.add(output_path)
            self._refresh_scan_view(reset_scroll=False)
            for item_id, path in self.tree_record_paths.items():
                if path == str(output_path):
                    self.tree.selection_set(item_id)
                    self.tree.see(item_id)
                    break
            for label, path in self.stamp_target_paths.items():
                if path == str(output_path):
                    self.stamp_target_var.set(label)
                    break
            self._save_settings()
            self._set_busy(False, "盖章完成")
            self.log(
                f"已生成盖章文件：{output_path}；模式={result['mode']}；页数={','.join(map(str, result['pages']))}。原文件保留。"
            )
        except Exception as exc:
            self._set_busy(False, "盖章失败")
            self.log(f"盖章结果处理失败：{exc}")
            messagebox.showerror("盖章失败", str(exc), parent=self.root)

    def _populate_tree(self, result: dict) -> None:
        self._cancel_tree_drag()
        existing = {path: item for item, path in self.tree_record_paths.items()}
        duplicate_names = {
            name for group in result["duplicate_groups"] for name in group
        }
        ordered_items: list[str] = []
        for idx, record in enumerate(result["files"]):
            ordered_items.append(
                self._update_tree_record(
                    record, existing.get(record["path"]), idx, duplicate_names
                )
            )
        keep = set(ordered_items)
        discarded = [item for item in self.tree.get_children() if item not in keep]
        if discarded:
            self.tree.delete(*discarded)
            for item in discarded:
                self.tree_record_paths.pop(item, None)
        self.tree.set_children("", *ordered_items)

    def _update_tree_record(
        self,
        record: dict,
        item_id: str | None = None,
        index: int = 0,
        duplicate_names: set[str] | tuple = (),
    ) -> str:
        orientations = {
            "横向" if item["landscape"] else "纵向"
            for item in record["page_details"]
        }
        tags: list[str] = ["zebra"] if index % 2 == 1 else []
        if record["rail"]:
            tags.append("rail")
        if record["name"] in duplicate_names:
            tags.append("duplicate")
        if record["warnings"]:
            tags.append("warning")
        options = {
            "text": record["name"],
            "values": (
                ("ZIP 内专票" if record.get("source_archive") else "增值税专票")
                if record.get("special_vat") else
                "ZIP 内 PDF" if record.get("source_archive") else (
                    "图片" if record.get("source_image") else (
                        "铁路票" if record["rail"] else "其他 PDF"
                    )
                ),
                f"¥{record['amount']}" if record.get("amount") else "—",
                record["pages"],
                record["copies"],
                "/".join(sorted(orientations)),
                record["classification"],
                "；".join(record["warnings"]),
            ),
            "tags": tuple(tags),
        }
        if item_id is None:
            item_id = self.tree.insert("", tk.END, **options)
        else:
            self.tree.item(item_id, **options)
        self.tree_record_paths[item_id] = record["path"]
        return item_id

    def _handle_print_done(self, result: dict, mode: str) -> None:
        if isinstance(mode, dict):
            context = mode
            mode_name = str(context.get("mode", "batch"))
        else:
            context = self.active_print_context or {"mode": str(mode)}
            mode_name = str(mode)
        self._set_busy(False, "打印流程结束")
        submitted = int(result.get("submitted_jobs", 0))
        expected = int(result.get("expected_jobs", 0))
        observed = int(result.get("jobs_observed_in_queue", 0))
        remaining = int(result.get("queue_remaining_with_prefix", 0))
        status = result.get("printer_status", "Unknown")
        self.log(
            f"打印结果：提交 {submitted}/{expected}，队列观察 {observed}，"
            f"当前本批剩余 {remaining}，打印机状态 {status}。"
        )
        self._append_print_history(result, context)
        self.active_print_context = None
        if result.get("success"):
            title = {
                "selected": "所选作业已提交",
                "copy": "复印作业已提交",
                "calibration": "校准页作业已提交",
            }.get(mode_name, "批量作业已提交")
            calibration_note = ""
            if mode_name == "calibration":
                direction = {
                    "auto": "自动",
                    "landscape": "横向",
                    "portrait": "纵向",
                }.get(result.get("orientation_mode"), "自动")
                calibration_note = (
                    f"校准页：100 × 100 mm · {result.get('calibration_copies', 1)} 份 · "
                    f"{direction}\n\n"
                )
            messagebox.showinfo(
                title,
                f"{calibration_note}提交作业：{submitted}/{expected}\n"
                f"曾在队列中观察：{observed}\n"
                f"当前本批队列剩余：{remaining}\n"
                f"打印机状态：{status}\n\n"
                + (
                    "程序已完成提交；请用尺检查校准方框是否为 100 × 100 mm。"
                    if mode_name == "calibration"
                    else "程序已完成提交；请人工确认实际出纸和版面。"
                ),
                parent=self.root,
            )
        else:
            messagebox.showerror(
                "打印未完整完成",
                f"提交作业：{submitted}/{expected}\n"
                f"错误：{result.get('fatal_error') or result.get('stderr') or '未知错误'}\n"
                f"诊断目录：{result.get('temp_directory', '')}",
                parent=self.root,
            )

    def _handle_validation_done(self, result: dict, preset_name: str) -> None:
        self._set_busy(False, "规则校验完成")
        checked_pages = int(result.get("checked_pages", 0))
        failed_pages = int(result.get("failed_pages", 0))
        skipped = int(result.get("duplicate_skipped_count", 0))
        duplicate_groups = result.get("duplicate_groups", [])
        duplicate_summary = (
            f"重复导入检查：列表重复 {len(duplicate_groups)} 组；跳过重复 {skipped} 份。"
        )
        duplicate_details = "\n".join(
            f"跳过：{item['name']} → 保留：{item['kept_name']}"
            for item in result.get("duplicate_imports", [])[:5]
        )
        if skipped > 5:
            duplicate_details += f"\n其余 {skipped - 5} 份见运行日志。"
        duplicate_note = duplicate_summary + (
            f"\n{duplicate_details}" if duplicate_details else ""
        )
        self.log(
            f"打印规则校验：预设 {preset_name}，检查 {checked_pages} 页，"
            f"不通过 {failed_pages} 页；{duplicate_summary} 未提交打印作业。"
        )
        if result.get("success"):
            messagebox.showinfo(
                "打印规则校验通过",
                f"预设：{preset_name}\n"
                f"打印机：{result.get('printer', self.printer_var.get())}\n"
                f"纸张：{result.get('paper_size', self.paper_var.get())}\n"
                f"已检查页面：{checked_pages}\n{duplicate_note}\n\n"
                "当前规则适用于当前列表；未提交打印作业。",
                parent=self.root,
            )
            return

        errors = result.get("errors", [])
        details = "\n".join(str(error) for error in errors[:10])
        if len(errors) > 10:
            details += f"\n另有 {len(errors) - 10} 项未显示。"
        messagebox.showerror(
            "打印规则校验未通过",
            f"预设：{preset_name}\n"
            f"检查页面：{checked_pages}；不通过：{failed_pages}\n"
            f"{duplicate_note}\n"
            f"\n{details}\n\n未提交打印作业。",
            parent=self.root,
        )

    def _handle_export_done(self, result: dict, output_path: str) -> None:
        self._set_busy(False, "合并/转换 PDF 完成")
        self.log(
            f"合并/转换 PDF 完成：{result.get('files', 0)} 个文件、{result.get('pages', 0)} 页 → {output_path}"
        )
        messagebox.showinfo(
            "合并/转换 PDF 完成",
            f"文件：{output_path}\n"
            f"来源文件：{result.get('files', 0)} 个\n"
            f"总页数：{result.get('pages', 0)} 页",
            parent=self.root,
        )

    def _handle_error(
        self,
        title: str,
        error: str,
        trace: str,
        event_name: str = "",
        context: object | None = None,
    ) -> None:
        if event_name == "scan_done":
            self._restore_scan_preview()
        self._set_busy(False, "发生错误")
        self.log(f"{title}失败：{error}")
        self.log(trace)
        if event_name == "print_done" or self.active_print_context:
            print_context = context if isinstance(context, dict) else self.active_print_context
            self._append_print_history({}, print_context, error=error)
            self.active_print_context = None
        messagebox.showerror(f"{title}失败", error, parent=self.root)

    def _set_busy(self, busy: bool, status: str) -> None:
        if busy:
            self._cancel_tree_drag()
        self.busy = busy
        self.status_var.set(status)
        if busy:
            if not self.progress.winfo_manager():
                self.progress.pack(side=tk.RIGHT, padx=10, pady=6)
            self.progress.start(10)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        state = tk.DISABLED if busy else tk.NORMAL
        for widget in (
            self.browse_button,
            self.import_button,
            getattr(self, "quick_scan_button", None),
            self.empty_drop_button,
            self.add_files_button,
            self.refresh_button,
            self.scan_button,
            getattr(self, "advanced_preset_combo", None),
            self.selected_print_button,
            self.print_button,
            self.copy_button,
            getattr(self, "export_pdf_button", None),
            getattr(self, "preview_button", None),
            getattr(self, "history_button", None),
            getattr(self, "stamp_button", None),
            getattr(self, "stamp_browse_button", None),
            getattr(self, "stamp_target_combo", None),
            getattr(self, "stamp_pages_entry", None),
            getattr(self, "stamp_all_pages_check", None),
            getattr(self, "stamp_mode_combo", None),
            getattr(self, "stamp_size_entry", None),
            self.validate_button,
            self.calibration_button,
            self.remove_button,
            self.select_all_button,
            self.folder_entry,
            self.rail_spin,
            self.special_spin,
            self.other_spin,
            self.copy_spin,
        ):
            if widget is not None:
                widget.configure(state=state)
        self.printer_combo.configure(state="disabled" if busy else "readonly")
        self.paper_combo.configure(state="disabled" if busy else "readonly")
        self.preset_combo.configure(state="disabled" if busy else "readonly")
        if hasattr(self, "advanced_preset_combo"):
            self.advanced_preset_combo.configure(state="disabled" if busy else "readonly")
        if hasattr(self, "stamp_target_combo"):
            self.stamp_target_combo.configure(state="disabled" if busy else "readonly")
        if hasattr(self, "stamp_mode_combo"):
            self.stamp_mode_combo.configure(state="disabled" if busy else "readonly")
        if not busy:
            self._update_action_states()

    def _update_action_states(self, copy_count_text: str | None = None) -> None:
        if self.busy:
            self.selected_print_button.configure(state=tk.DISABLED)
            self.print_button.configure(state=tk.DISABLED)
            self.copy_button.configure(state=tk.DISABLED)
            if hasattr(self, "export_pdf_button"):
                self.export_pdf_button.configure(state=tk.DISABLED)
            if hasattr(self, "preview_button"):
                self.preview_button.configure(state=tk.DISABLED)
            if hasattr(self, "stamp_button"):
                self.stamp_button.configure(state=tk.DISABLED)
            self.validate_button.configure(state=tk.DISABLED)
            self.calibration_button.configure(state=tk.DISABLED)
            self.remove_button.configure(state=tk.DISABLED)
            self.select_all_button.configure(state=tk.DISABLED)
            return
        result = self._active_scan_result()
        has_files = bool(result and result["files"])
        valid_copies = self._special_copy_value() is not None
        self.calibration_button.configure(state=tk.NORMAL)
        if hasattr(self, "history_button"):
            self.history_button.configure(state=tk.NORMAL)
        self.print_button.configure(state=tk.NORMAL if has_files and valid_copies else tk.DISABLED)
        self.validate_button.configure(state=tk.NORMAL if has_files and valid_copies else tk.DISABLED)
        if hasattr(self, "export_pdf_button"):
            self.export_pdf_button.configure(state=tk.NORMAL if has_files else tk.DISABLED)
        if hasattr(self, "preview_button"):
            self.preview_button.configure(state=tk.NORMAL if has_files else tk.DISABLED)
        selected_count = len(self.tree.selection()) if has_files else 0
        has_selection = selected_count > 0
        self.selected_print_button.configure(
            state=tk.NORMAL if has_selection and valid_copies else tk.DISABLED
        )
        self.copy_button.configure(
            state=tk.NORMAL
            if has_selection and valid_copies and self._validated_copy_count(copy_count_text) is not None
            else tk.DISABLED
        )
        if hasattr(self, "stamp_button"):
            self.stamp_button.configure(
                state=tk.NORMAL if has_files and bool(self.stamp_target_var.get()) else tk.DISABLED
            )
        self.remove_button.configure(
            state=tk.NORMAL if has_selection else tk.DISABLED,
            text=f"移除所选（{selected_count}）" if selected_count else "移除所选",
        )
        self.select_all_button.configure(state=tk.NORMAL if has_files else tk.DISABLED)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.log_text.delete("1.0", f"{line_count - 2000 + 1}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        logger = getattr(self, "logger", None)
        if logger is not None:
            logger.info(message)

    def _setup_logging(self) -> None:
        self.logger = logging.Logger(f"ticket-printer.{id(self)}")
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
            self.logger.addHandler(handler)
        except OSError:
            self.logger = None

    def _setup_tray(self) -> None:
        self.tray = SystemTray(
            f"{engine.APP_NAME} v{APP_VERSION}",
            on_show=lambda: self.events.put(("tray_show",)),
            on_exit=lambda: self.events.put(("tray_exit",)),
            on_failure=lambda error: self.events.put(("tray_failed", error)),
        )
        menu = tk.Menu(self.root)
        window_menu = tk.Menu(menu, tearoff=False)
        window_menu.add_command(label="最小化到托盘", command=self._hide_to_tray)
        window_menu.add_checkbutton(
            label="关闭窗口时最小化到托盘",
            variable=self.close_to_tray_var,
            command=self._save_settings,
        )
        window_menu.add_separator()
        window_menu.add_command(label="退出程序", command=self.on_close)
        menu.add_cascade(label="窗口", menu=window_menu)
        self.root.configure(menu=menu)
        self.root.bind("<Unmap>", self._on_window_unmap, add="+")
        if not self.tray.start():
            self.log("系统托盘不可用，最小化时仍保留任务栏窗口。")

    def _on_window_unmap(self, event: tk.Event) -> None:
        if event.widget is self.root and not self.closing and self.root.state() == "iconic":
            self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        if self.closing:
            return
        if self.tray is not None and self.tray.started:
            self._tray_hidden = True
            self.root.withdraw()
        else:
            self.root.iconify()

    def _show_from_tray(self) -> None:
        if self.closing:
            return
        self._tray_hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_window_close(self) -> None:
        if self.closing:
            return
        if self.close_to_tray_var.get() and self.tray is not None and self.tray.started:
            self._hide_to_tray()
            self.log("窗口已收起到系统托盘。点击托盘图标可恢复，右键菜单可退出程序。")
            return
        self.on_close()

    def on_close(self) -> None:
        if self.closing:
            return
        if self.busy:
            if not messagebox.askyesno(
                "任务正在运行",
                "退出将停止当前处理及后续打印提交。已提交到 Windows 队列的作业仍可能打印，需在系统打印队列取消。是否退出？",
                parent=self.root,
            ):
                return
        self.closing = True
        self._cancel_tree_drag()
        self.status_var.set("正在停止任务并退出…")
        pending_poll = getattr(self, "_poll_after", None)
        if pending_poll is not None:
            self.root.after_cancel(pending_poll)
            self._poll_after = None
        self.progress.stop()
        try:
            self._disable_file_drop()
            self._save_settings()
        except (OSError, tk.TclError, ValueError) as exc:
            self.log(f"退出时保存设置失败：{exc}")
        if self.worker_controller is not None:
            try:
                self.worker_controller.cancel()
            except OSError as exc:
                self.closing = False
                self.log(f"停止打印子进程失败：{exc}")
                messagebox.showerror("无法退出", "打印子进程未停止，请稍后重试。", parent=self.root)
                self._poll_after = self.root.after(20, self._poll_events)
                return
        self._finish_close()

    def _finish_close(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.root.after(100, self._finish_close)
            return
        if self.active_print_context is not None:
            self._append_print_history({}, self.active_print_context, error="用户退出，已停止继续提交；已进入系统队列的作业可能仍会打印。")
            self.active_print_context = None
        for path in self.stamp_temp_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self.log(f"盖章临时文件清理失败：{exc}")
        if self.tray is not None:
            self.tray.stop()
        if self.logger is not None:
            for handler in self.logger.handlers:
                handler.close()
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=engine.APP_NAME)
    parser.add_argument("folder", nargs="?", help="启动时预填的文件夹")
    parser.add_argument("--scan-folder", help="无界面扫描指定文件夹")
    parser.add_argument("--output", help="无界面扫描结果 JSON 路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.scan_folder:
        return run_headless_scan(args.scan_folder, args.output)

    _enable_windows_dpi_awareness()
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    TicketPrinterApp(root, initial_folder=args.folder)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
