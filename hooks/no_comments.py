"""Pre-commit: в .py нет комментариев — код объясняют имена и докстринги.

Разрешены только прагмы для инструментов: noqa, type:, pragma, pylint, fmt:, ruff:,
isort:, mypy:, shebang, coding. Докстринги не комментарии и не проверяются.
Запуск: python hooks/no_comments.py <файлы>; выход 1, если нашлись комментарии.
"""

import io
import re
import sys
import tokenize

PRAGMA = re.compile(r"^#\s*(noqa|type:|pragma|pylint|fmt:|ruff:|isort:|mypy:|!|-\*-|coding[:=])")


def comments(path):
    try:
        src = open(path, encoding="utf-8").read()
        tokens = tokenize.generate_tokens(io.StringIO(src).readline)
        return [
            (t.start[0], t.string)
            for t in tokens
            if t.type == tokenize.COMMENT and not PRAGMA.match(t.string)
        ]
    except (SyntaxError, tokenize.TokenError, UnicodeDecodeError):
        return []


def main(paths):
    found = 0
    for path in paths:
        for line, text in comments(path):
            print(f"{path}:{line}: {text[:80]}")
            found += 1
    if found:
        print(
            f"\n{found} комментариев. Код должен быть понятен без них: переименуй, "
            f"вынеси в функцию, или напиши докстринг, если это контракт модуля."
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
