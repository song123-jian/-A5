"""Shared application icon artwork for the window, tray and release files."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def make_app_icon(size: int = 256) -> Image.Image:
    """Render at high resolution so small Windows icons have smooth edges."""
    if not isinstance(size, int) or not 16 <= size <= 256:
        raise ValueError("Icon size must be an integer from 16 to 256")
    scale = 4
    image = Image.new("RGBA", (256 * scale, 256 * scale))
    draw = ImageDraw.Draw(image)

    def rounded(box: tuple[int, int, int, int], radius: int, fill: str) -> None:
        draw.rounded_rectangle(tuple(value * scale for value in box), radius * scale, fill)

    # Blue tile, paper feed, printer body and the outgoing receipt.
    rounded((8, 8, 248, 248), 56, "#215FE7")
    rounded((78, 43, 178, 124), 10, "#FFFFFF")
    rounded((94, 61, 158, 68), 3, "#B6CEFC")
    rounded((94, 77, 143, 84), 3, "#B6CEFC")
    rounded((46, 98, 210, 181), 22, "#123EAB")
    rounded((46, 91, 210, 172), 22, "#EAF1FF")
    rounded((66, 137, 190, 161), 9, "#1748BA")
    rounded((79, 145, 177, 214), 8, "#FFFFFF")
    rounded((96, 162, 159, 170), 4, "#2563EB")
    rounded((96, 180, 145, 187), 3, "#B6CEFC")
    rounded((65, 110, 91, 118), 4, "#87A9F0")
    draw.ellipse(tuple(value * scale for value in (175, 108, 187, 120)), fill="#0EAD91")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_icon_assets(directory: Path) -> None:
    """Regenerate release assets using the same artwork as the running app."""
    image = make_app_icon()
    image.save(directory / "app_icon.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    image.save(directory / "app_icon.png")


if __name__ == "__main__":
    write_icon_assets(Path(__file__).resolve().parent)
