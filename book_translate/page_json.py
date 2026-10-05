"""Перевод одной страницы — `translation/p-NNN.json`.

Поля: `source` — текст оригинала, `translation` — перевод, `captions` — пары подписей с теми же полями.
Файлы со старыми ключами `fi`/`ru` при чтении переименовываются, переписывать их не нужно.
"""

from __future__ import annotations

import json
from pathlib import Path

LEGACY_KEYS = {"fi": "source", "ru": "translation"}
TEXT_FIELDS = ("source", "translation", "notes")


def renamed(fields: dict) -> dict:
    return {LEGACY_KEYS.get(k, k): v for k, v in fields.items()}


def checked_text(fields: dict, where: str) -> dict:
    for key in TEXT_FIELDS:
        if fields.get(key) is not None and not isinstance(fields[key], str):
            raise ValueError(f"{where}: поле {key} должно быть строкой")
    return fields


def read(path: str | Path) -> dict:
    """Прочитать перевод страницы в схеме `source`/`translation`, в каком бы виде он ни был записан."""
    name = Path(path).name
    page = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(page, dict):
        raise ValueError(f"{name}: ожидается объект JSON")
    page = checked_text(renamed(page), name)
    captions = page.get("captions") or []
    if not isinstance(captions, list) or not all(isinstance(c, dict) for c in captions):
        raise ValueError(f"{name}: captions должно быть списком объектов")
    page["captions"] = [checked_text(renamed(c), f"{name}, подпись") for c in captions]
    return page
