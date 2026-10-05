"""Настройки одной книги из `book.yaml`: пути, язык оригинала и перевода, кавычки, подписи к цитатам, сноски.

Пример — `conf/book.example.yaml`. Модули пакета получают настройки через `configure(book)`.

Конфиг книги — доверенный файл: в нём указываются программы, которые будут запущены (`chrome`, `tesseract`),
и пути, куда пишутся PDF. Чужой конфиг стоит прочитать перед запуском, как чужой скрипт.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from book_translate.tools import checked_slug, find_executable

DEFAULT_LABELS = {
    "page": "p.",
    "missing": "This page has not been translated yet.",
    "note": "Translator's note.",
    "kind_photo": "photo",
    "kind_pattern": "pattern",
    "kind_mixed": "text and illustrations",
    "kind_text": "text",
    "kind_blank": "blank",
    "kind_cover": "cover",
    "kind_toc": "contents",
}
SPREAD_MODES = ("auto", "always", "never")


@dataclass
class Book:
    slug: str
    workdir: Path
    out_bilingual: Path
    out_inplace: Path
    ocr_lang: str
    source_lower: str
    source_upper: str
    target_letters: str
    target_lang: str
    notes_headings: tuple
    page_width_mm: float = 148
    page_height_mm: float | None = None
    spreads: str = "auto"
    quote_open: str = "\"«”“'"
    quote_close: str = '"»”'
    attribution_source: tuple = ()
    attribution_target: tuple = ()
    source_names: dict = field(default_factory=dict)
    service_words: tuple = ("cm", "mm")
    credit_words: tuple = ()
    retypeset_kinds: tuple = ("toc",)
    retypeset_ranges: tuple = ()
    editorial_patterns: tuple = ()
    note_boilerplate: tuple = ()
    labels: dict = field(default_factory=dict)
    title: dict = field(default_factory=dict)
    tesseract: str = "system"
    chrome: str | None = None

    @property
    def pages(self) -> Path:
        return self.workdir / "pages"

    @property
    def translation(self) -> Path:
        return self.workdir / "translation"

    @property
    def blocks(self) -> Path:
        return self.workdir / "layout" / "blocks"

    @property
    def rotated(self) -> Path:
        return self.workdir / "layout" / "rotated"

    @property
    def build(self) -> Path:
        return self.workdir / "build"

    def label(self, key: str) -> str:
        return {**DEFAULT_LABELS, **self.labels}[key]

    def tesseract_cmd(self, threads: int = 1) -> tuple[str, dict]:
        """Tesseract и его окружение.

        `system` — из PATH; путь к файлу — этот исполняемый файл; путь к каталогу — распакованный пакет
        (`usr/bin/tesseract`, библиотеки `usr/lib/*`, языки `usr/share/tesseract-ocr/*/tessdata`).
        """
        env = {**os.environ, "OMP_THREAD_LIMIT": str(threads)}
        if self.tesseract == "system":
            return find_executable("tesseract"), env
        root = Path(os.path.expanduser(self.tesseract))
        if not root.is_dir():
            return find_executable(str(root)), env
        libs = sorted(glob.glob(str(root / "usr/lib/*-linux-gnu"))) + [str(root / "usr/lib")]
        tessdata = sorted(glob.glob(str(root / "usr/share/tesseract-ocr/*/tessdata")))
        env["LD_LIBRARY_PATH"] = os.pathsep.join(libs + [os.environ.get("LD_LIBRARY_PATH", "")])
        if tessdata:
            env["TESSDATA_PREFIX"] = tessdata[-1]
        return find_executable(str(root / "usr/bin/tesseract")), env


LANGUAGE_FIELDS = (
    "ocr_lang",
    "source_lower",
    "source_upper",
    "target_letters",
    "target_lang",
    "notes_headings",
)
TUPLES = (
    "attribution_source",
    "attribution_target",
    "notes_headings",
    "service_words",
    "credit_words",
    "retypeset_kinds",
    "editorial_patterns",
    "note_boilerplate",
)
ALPHABETS = ("source_lower", "source_upper", "target_letters")


def validate(raw: dict, name: str) -> None:
    for key in TUPLES:
        if key in raw and not isinstance(raw[key], (list, tuple)):
            raise ValueError(f"{name}: поле {key} должно быть списком")
    for key in ("source_names", "labels", "title"):
        if key in raw and not isinstance(raw[key], dict):
            raise ValueError(f"{name}: поле {key} должно быть словарём")
    for key in ("page_width_mm", "page_height_mm"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, (int, float)) or value <= 0):
            raise ValueError(f"{name}: поле {key} должно быть положительным числом")
    for key in ALPHABETS:
        try:
            re.compile("[" + str(raw[key]) + "]")
        except re.error as e:
            raise ValueError(
                f"{name}: поле {key} — не набор букв для регулярного выражения: {e}"
            ) from e
    for pattern in raw.get("editorial_patterns") or ():
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"{name}: editorial_patterns, {pattern!r}: {e}") from e
    if raw.get("spreads", "auto") not in SPREAD_MODES:
        raise ValueError(f"{name}: spreads — одно из {', '.join(SPREAD_MODES)}")
    unknown_labels = set(raw.get("labels") or {}) - set(DEFAULT_LABELS)
    if unknown_labels:
        raise ValueError(f"{name}: неизвестные labels: {', '.join(sorted(unknown_labels))}")


def load(path: str | Path) -> Book:
    """Прочитать `book.yaml`; относительные пути считаются от каталога файла."""
    path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: ожидается словарь полей")
    base = path.parent

    def resolve(value: str) -> Path:
        p = Path(os.path.expanduser(str(value)))
        return p if p.is_absolute() else (base / p).resolve()

    raw["workdir"] = resolve(raw.get("workdir", "."))
    output = raw.pop("output", {}) or {}
    slug = checked_slug(raw.get("slug") or path.parent.name)
    raw["slug"] = slug
    raw["out_bilingual"] = resolve(output.get("bilingual", f"{slug}-bilingual.pdf"))
    raw["out_inplace"] = resolve(output.get("inplace", f"{slug}-inplace.pdf"))
    unknown = set(raw) - set(Book.__dataclass_fields__)
    if unknown:
        raise ValueError(f"неизвестные поля в {path.name}: {', '.join(sorted(unknown))}")
    missing = [name for name in LANGUAGE_FIELDS if not raw.get(name)]
    if missing:
        raise ValueError(f"в {path.name} не задан язык книги: {', '.join(missing)}")
    validate(raw, path.name)
    for key in TUPLES:
        if key in raw:
            raw[key] = tuple(raw[key] or ())
    if "retypeset_ranges" in raw:
        raw["retypeset_ranges"] = tuple(tuple(r) for r in raw["retypeset_ranges"] or ())
    return Book(**raw)
