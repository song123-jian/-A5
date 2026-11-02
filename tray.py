from __future__ import annotations

import os
import threading
from collections.abc import Callable

from PIL import Image, ImageDraw

try:
    import pystray
except ImportError:  # Source-only environments can still run without a tray dependency.
    pystray = None


class SystemTray:
    """Small Windows tray wrapper that keeps Tk callbacks on the Tk thread."""

    def __init__(
        self,
        title: str,
        on_show: Callable[[], None],
        on_exit: Callable[[], None],
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self.title = title
        self._on_show = on_show
        self._on_exit = on_exit
        self._on_failure = on_failure or (lambda _error: None)
        self.icon = None
        self.thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()

    @property
    def started(self) -> bool:
        return self._ready.is_set() and self.thread is not None and self.thread.is_alive()

    @property
    def available(self) -> bool:
        return os.name == "nt" and pystray is not None

    def start(self) -> bool:
        if not self.available or self.thread is not None:
            return False
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", self._show_from_menu, default=True),
                pystray.MenuItem("退出程序", self._exit_from_menu),
            )
            self.icon = pystray.Icon(
                "a5-ticket-printer",
                self._make_image(),
                self.title,
                menu,
            )
            self.thread = threading.Thread(
                target=self._run_icon,
                name="system-tray",
                daemon=True,
            )
            self.thread.start()
            return True
        except Exception:
            self.stop()
            return False

    def stop(self) -> None:
        self._stopping.set()
        self._ready.clear()
        icon = self.icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        thread = self.thread
        self.thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self.icon = None

    def _run_icon(self) -> None:
        try:
            self.icon.run(setup=self._on_ready)
        except Exception as exc:
            if not self._stopping.is_set():
                self._on_failure(str(exc))
        finally:
            self._ready.clear()
            if not self._stopping.is_set():
                self._on_failure("托盘线程已停止")

    def _on_ready(self, icon: object) -> None:
        try:
            if not self._stopping.is_set():
                icon.visible = True
                self._ready.set()
        except Exception as exc:
            self._on_failure(str(exc))
            icon.stop()

    def _show_from_menu(self, _icon: object, _item: object) -> None:
        self._on_show()

    def _exit_from_menu(self, _icon: object, _item: object) -> None:
        self._on_exit()

    @staticmethod
    def _make_image() -> Image.Image:
        image = Image.new("RGBA", (64, 64), (30, 90, 232, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((10, 18, 54, 48), radius=6, fill=(255, 255, 255, 255))
        draw.rectangle((18, 10, 46, 29), fill=(255, 255, 255, 255))
        draw.rectangle((20, 14, 44, 23), fill=(30, 90, 232, 255))
        draw.rectangle((18, 38, 46, 54), fill=(30, 90, 232, 255))
        draw.rectangle((24, 41, 40, 49), fill=(255, 255, 255, 255))
        draw.ellipse((14, 28, 20, 34), fill=(30, 90, 232, 255))
        return image
