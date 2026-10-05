"""PDF книги → картинки страниц `pages/p-NNN.jpg`; разворот режется на `p-NNNa` (левая) и `p-NNNb` (правая)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PIL import Image

from book_translate import tools

SPREAD_ASPECT = 1.1
DPI = 200
JPEG_QUALITY = 88


def is_spread(im: Image.Image) -> bool:
    return im.width > SPREAD_ASPECT * im.height


def split(im: Image.Image, spreads: str = "auto") -> list[tuple[str, Image.Image]]:
    """Одиночная страница — как есть, разворот — две половины с суффиксами a и b.

    `spreads`: `auto` — разворот, если картинка шире своей высоты в 1,1 раза; `always` — каждая картинка
    разворот; `never` — ни одна.
    """
    if spreads == "never" or (spreads == "auto" and not is_spread(im)):
        return [("", im)]
    half = im.width // 2
    return [("a", im.crop((0, 0, half, im.height))), ("b", im.crop((half, 0, im.width, im.height)))]


def has_text_layer(pdf: Path) -> bool:
    """Есть ли в PDF текстовый слой: если нет, текст читается глазами по картинкам, а не из PDF."""
    out = tools.run_tool(["pdftotext", "-l", "5", str(Path(pdf).resolve()), "-"], text=True)
    return len(out.stdout.split()) > 20


def run(pdf: Path, pages_dir: Path, dpi: int = DPI, spreads: str = "auto") -> list[str]:
    """Растеризовать PDF и разложить страницы; существующие файлы не перезаписываются."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    made = []
    with tempfile.TemporaryDirectory() as tmp:
        tools.run_tool(
            ["pdftoppm", "-r", str(dpi), "-png", str(Path(pdf).resolve()), os.path.join(tmp, "p")]
        )
        for path in sorted(Path(tmp).glob("p-*.png")):
            number = int(path.stem.split("-")[-1])
            for suffix, part in split(Image.open(path).convert("RGB"), spreads):
                target = pages_dir / f"p-{number:03d}{suffix}.jpg"
                if target.exists():
                    continue
                part.save(target, "JPEG", quality=JPEG_QUALITY)
                made.append(target.stem)
    return made
