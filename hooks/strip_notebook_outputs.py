"""Pre-commit-хук: снимает выводы и номера ячеек с ноутбуков перед коммитом.

Зачем: ноутбук с выводами тащит в git картинки и блобы, а его дифф нечитаем.
"""

from __future__ import annotations

import json
import sys


def strip_notebook(path: str) -> bool:
    """Убрать outputs и execution_count из ноутбука. True, если файл изменён."""
    with open(path, encoding="utf-8") as f:
        try:
            notebook = json.load(f)
        except json.JSONDecodeError:
            print(f"WARNING: {path} — невалидный JSON, пропускаю")
            return False

    if notebook.get("nbformat", 0) < 4:
        return False

    modified = False
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        if cell.get("outputs"):
            cell["outputs"] = []
            modified = True
        if cell.get("execution_count") is not None:
            cell["execution_count"] = None
            modified = True

    if modified:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(notebook, f, indent=1, ensure_ascii=False)
            f.write("\n")

    return modified


def main() -> int:
    retval = 0
    for filename in sys.argv[1:]:
        if strip_notebook(filename):
            print(f"Сняты выводы: {filename}")
            retval = 1
    return retval


if __name__ == "__main__":
    sys.exit(main())
