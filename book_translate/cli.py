"""CLI: `book-translate <шаг> --book книга.yaml`. Логика — в модулях пакета, здесь только выбор шага."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from book_translate import settings, tools


def pids_arg(value: str | None) -> list[str] | None:
    return (
        tools.checked_page_ids([p.strip() for p in value.split(",") if p.strip()])
        if value
        else None
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="book-translate", description="Перевод отсканированных книг в PDF"
    )
    sub = parser.add_subparsers(dest="step", required=True)

    p = sub.add_parser("pages", help="PDF → pages/p-NNN.jpg, развороты режутся на a/b")
    p.add_argument("--book", required=True)
    p.add_argument("pdf")

    p = sub.add_parser("layout", help="блоки текста и маски фото по страницам (нужен scipy)")
    p.add_argument("--book", required=True)
    p.add_argument("--pages", help="p-007a,p-008b; без флага — все")
    p.add_argument("--jobs", type=int, default=2)

    p = sub.add_parser("bilingual", help="двуязычный PDF: скан слева, перевод справа")
    p.add_argument("--book", required=True)
    p.add_argument("--out")
    p.add_argument("--force", action="store_true", help="перезаписать существующий PDF")

    p = sub.add_parser("inplace", help="PDF с переводом прямо на страницах")
    p.add_argument("--book", required=True)
    p.add_argument("--pages", help="p-007a,p-008b; без флага — вся книга")
    p.add_argument("--out")
    p.add_argument("--force", action="store_true", help="перезаписать существующий PDF")

    p = sub.add_parser("reader", help="HTML-читалка: оригинал и перевод рядом, пары подсвечиваются")
    p.add_argument("--book", required=True)
    p.add_argument("--out")
    p.add_argument("--force", action="store_true", help="перезаписать существующий файл")

    p = sub.add_parser("check", help="проверка последней сборки inplace по итоговой вёрстке")
    p.add_argument("--book", required=True)

    p = sub.add_parser("compress", help="сжать PDF для отправки (сканы 150 dpi)")
    p.add_argument("pdf")
    p.add_argument("--out")

    args = parser.parse_args(argv)
    try:
        dispatch(args)
    except (tools.ToolError, FileExistsError, FileNotFoundError, ValueError) as e:
        print(f"book-translate: {e}", file=sys.stderr)
        sys.exit(1)


def dispatch(args: argparse.Namespace) -> None:
    if args.step == "compress":
        from book_translate import compress

        print(compress.run(Path(args.pdf), Path(args.out) if args.out else None))
        return

    book = settings.load(args.book)
    if args.step == "pages":
        from book_translate import pages

        if pages.has_text_layer(Path(args.pdf)):
            print("в PDF есть текстовый слой — текст можно брать оттуда")
        print(
            f"страниц разложено: {len(pages.run(Path(args.pdf), book.pages, spreads=book.spreads))}"
        )
    elif args.step == "layout":
        from book_translate import layout

        layout.run(book, pids_arg(args.pages), args.jobs)
    elif args.step == "bilingual":
        from book_translate import bilingual

        bilingual.configure(book)
        bilingual.run(args.out, force=args.force)
    elif args.step == "inplace":
        from book_translate import inplace

        inplace.configure(book)
        inplace.run(pids_arg(args.pages), args.out, force=args.force)
    elif args.step == "reader":
        from book_translate import reader

        reader.run(book, args.out, force=args.force)
    elif args.step == "check":
        from book_translate import check, inplace

        inplace.configure(book)
        check.run()


if __name__ == "__main__":
    main()
