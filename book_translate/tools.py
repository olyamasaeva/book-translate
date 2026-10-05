"""Внешние программы пайплайна: tesseract, Chrome — и сборочные файлы книги.

Каждый вызов проверяет код возврата и падает с понятным сообщением: пустой вывод упавшего tesseract не должен
попасть в кеш как «на странице нет слов», а упавший Chrome — выдать «замечаний нет».

Chrome запускается с включённой песочницей и каждый раз с новым профилем во временном каталоге. HTML книги
содержит политику CSP: картинки только `data:`, сеть и файлы недоступны, выполняется лишь сам скрипт подгонки
(по хешу), поэтому разметка или обработчики событий, попавшие в текст перевода, не исполнятся.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
PAGE_ID = re.compile(r"p-\d+[ab]?")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
CHROME_TIMEOUT_S = 1800


class ToolError(RuntimeError):
    """Внешняя программа не найдена или завершилась с ошибкой."""


def checked_page_ids(pids):
    """Проверить идентификаторы страниц (`p-007`, `p-012a`): путь из них не собрать."""
    bad = [p for p in pids if not PAGE_ID.fullmatch(p)]
    if bad:
        raise ValueError(
            f"неверные идентификаторы страниц: {', '.join(bad)} (ожидается p-007 или p-012a)"
        )
    return list(pids)


def checked_slug(slug):
    if not SLUG.fullmatch(slug or ""):
        raise ValueError(f"slug {slug!r}: допустимы латинские буквы, цифры, «_» и «-»")
    return slug


def find_executable(name):
    found = shutil.which(name) or (
        name if os.path.isfile(name) and os.access(name, os.X_OK) else None
    )
    if not found:
        raise ToolError(f"программа не найдена: {name}")
    return found


def find_chrome(configured=None):
    """Chrome из настроек или первый найденный из обычных имён и путей."""
    if configured:
        return find_executable(configured)
    for name in CHROME_CANDIDATES:
        try:
            return find_executable(name)
        except ToolError:
            continue
    raise ToolError("Chrome или Chromium не найден — укажите путь в поле `chrome` конфига книги")


def run_tool(cmd, **kwargs):
    try:
        r = subprocess.run(cmd, capture_output=True, **kwargs)
    except FileNotFoundError as e:
        raise ToolError(f"программа не найдена: {cmd[0]}") from e
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace")
        raise ToolError(
            f"{Path(cmd[0]).name} завершился с кодом {r.returncode}: {err.strip()[-800:]}"
        )
    return r


def ocr_tsv(tesseract, env, image: Image.Image, lang, psm):
    """Распознать картинку tesseract-ом, вернуть TSV по словам."""
    with tempfile.TemporaryDirectory(prefix="book-translate-ocr-") as tmp:
        path = os.path.join(tmp, "image.png")
        image.save(path)
        return run_tool(
            [tesseract, path, "-", "-l", lang, "--psm", str(psm), "--dpi", "300", "tsv"],
            text=True,
            env=env,
        ).stdout


def csp_meta(script):
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    policy = (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
        f"script-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'"
    )
    return f'<meta http-equiv="Content-Security-Policy" content="{policy}">'


def write_build_file(build_dir: Path, name, text):
    """Записать сборочный файл в `build_dir` книги атомарно: сначала во временный, потом переименовать."""
    build_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=build_dir, prefix="." + name, suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    target = build_dir / name
    os.replace(tmp, target)
    return target


def chrome(binary, *args, timeout=CHROME_TIMEOUT_S):
    with tempfile.TemporaryDirectory(prefix="book-translate-chrome-") as profile:
        return run_tool(
            [binary, "--headless=new", f"--user-data-dir={profile}", "--disable-gpu", *args],
            timeout=timeout,
        )


def print_pdf(binary, html_path: Path, out: Path, force=False):
    """Напечатать HTML в PDF. Существующий `out` не перезаписывается без `force`; запись — через временный файл."""
    out = Path(out).expanduser().resolve()
    if out.exists() and not force:
        raise FileExistsError(f"{out} уже есть — выберите другой путь или добавьте --force")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix="." + out.stem, suffix=".pdf.part")
    os.close(fd)
    try:
        chrome(
            binary,
            "--no-pdf-header-footer",
            f"--print-to-pdf={tmp}",
            Path(html_path).resolve().as_uri(),
        )
        if os.path.getsize(tmp) == 0:
            raise ToolError("Chrome не записал PDF")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out


def dump_dom(binary, html_path: Path):
    """DOM страницы после выполнения скрипта подгонки."""
    r = chrome(
        binary, "--virtual-time-budget=600000", "--dump-dom", Path(html_path).resolve().as_uri()
    )
    dom = r.stdout.decode("utf-8", errors="replace")
    if "<html" not in dom:
        raise ToolError("Chrome не вернул DOM")
    return dom
