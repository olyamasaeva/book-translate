"""Сжать готовый PDF для отправки (мессенджеры, почта): сканы до 150 dpi, текст остаётся векторным."""

from __future__ import annotations

from pathlib import Path

from book_translate import tools

GHOSTSCRIPT = ("gs", "gswin64c", "gswin32c")
DPI = 150
JPEG_QUALITY = 70


def ghostscript():
    for name in GHOSTSCRIPT:
        try:
            return tools.find_executable(name)
        except tools.ToolError:
            continue
    raise tools.ToolError("Ghostscript не найден")


def run(src: Path, out: Path | None = None, dpi: int = DPI) -> Path:
    """Пережать `src` ghostscript-ом в `out` (по умолчанию `<имя>-compressed.pdf` рядом); исходник не трогается."""
    src = Path(src).resolve()
    out = Path(out).resolve() if out else src.with_name(src.stem + "-compressed.pdf")
    if out.exists():
        raise FileExistsError(f"{out} уже есть — удалить или выбрать другое имя")
    tools.run_tool(
        [
            ghostscript(),
            "-q",
            "-dNOPAUSE",
            "-dBATCH",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.5",
            "-dPDFSETTINGS=/ebook",
            f"-dColorImageResolution={dpi}",
            f"-dGrayImageResolution={dpi}",
            "-dDownsampleColorImages=true",
            "-dDownsampleGrayImages=true",
            "-dColorImageDownsampleType=/Bicubic",
            f"-dJPEGQ={JPEG_QUALITY}",
            f"-sOutputFile={out}",
            str(src),
        ]
    )
    return out
