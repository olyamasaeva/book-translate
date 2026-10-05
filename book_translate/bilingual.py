"""Двуязычный PDF: скан страницы слева, перевод справа.

Вход — `<workdir>/pages/p-*.jpg` и `translation/p-*.json` (схема — README). Сканы ужимаются до 1100 px, HTML
печатается headless Chrome, кегль подгоняет скрипт в браузере.
"""

import base64
import glob
import html
import io
import os
import re
from PIL import Image

from book_translate import page_json, tools

BOOK = OUT = CHROME = BUILD = LANG = None
TITLE = {}
LABELS = {}
EDITORIAL = ()
NOTE_BOILERPLATE = ()


def configure(book):
    global BOOK, OUT, CHROME, TITLE, BUILD, LANG, LABELS, EDITORIAL, NOTE_BOILERPLATE
    BOOK, OUT, TITLE, BUILD, LANG = (
        str(book.workdir),
        str(book.out_bilingual),
        book.title,
        book.build,
        book.target_lang,
    )
    CHROME = book.chrome
    LABELS = {key: book.label(key) for key in ("page", "missing", "note")}
    LABELS.update({kind: book.label("kind_" + kind) for kind in KINDS})
    EDITORIAL = tuple(re.compile(p) for p in book.editorial_patterns)
    NOTE_BOILERPLATE = book.note_boilerplate


KINDS = ("photo", "pattern", "mixed", "text", "blank", "cover", "toc")


FIT_JS = """
for (const side of document.querySelectorAll('.translation')) {
  let size = 9.8;
  while (side.scrollHeight > side.clientHeight + 1 && size > 7) { size -= 0.2; side.style.fontSize = size + 'pt'; }
  if (side.scrollHeight > side.clientHeight + 1) {
    side.dataset.overflow = '1';
    side.style.fontSize = '8.4pt';
    for (const box of [side, side.parentElement]) { box.style.height = 'auto'; box.style.overflow = 'visible'; }
  }
}
document.title = 'overflow:' + document.querySelectorAll('[data-overflow]').length;
"""


def img64(path, w=1100):
    im = Image.open(path).convert("RGB")
    if im.width > w:
        im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=72, optimize=True)
    return base64.b64encode(b.getvalue()).decode()


def md(text):
    """Абзацы через пустую строку, заголовки «# », переносы строк внутри абзаца (стихи, цитаты) сохраняются."""
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        while lines and lines[0].startswith("#"):
            h = lines.pop(0).lstrip("#").strip()
            out.append(f"<h2>{html.escape(h)}</h2>")
        if lines:
            out.append("<p>" + "<br>".join(html.escape(line) for line in lines) + "</p>")
    return "\n".join(out)


def clean_notes(text):
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(x for x in sentences if not any(w in x for w in NOTE_BOILERPLATE)).strip()


def strip_retelling_mark(text):
    """Убрать из перевода редакторские пометки из `editorial_patterns` конфига (например, «[пересказ]»)."""
    text = text or ""
    for pattern in EDITORIAL:
        text = pattern.sub("", text)
    return text


def headings(text):
    return [line.lstrip("#").strip() for line in (text or "").split("\n") if line.startswith("#")]


CSS = """
@page { size: A4 landscape; margin: 9mm 11mm; }
* { box-sizing: border-box; }
body { margin: 0; width: 275mm; font-family: 'PT Serif', 'Noto Serif', 'DejaVu Serif', serif; color: #1f1b16; font-size: 9.8pt; line-height: 1.38; }
.sheet { display: grid; grid-template-columns: 112mm 1fr; gap: 8mm; break-after: page; height: 190mm; overflow: hidden; }
.scan { break-inside: avoid; }
.scan img { width: 112mm; max-height: 186mm; object-fit: contain; object-position: top left; display: block;
            border: .3mm solid #d8d0c4; }
.translation { padding-top: 1mm; height: 190mm; overflow: hidden; }
.meta { font: 8pt 'Noto Sans', 'DejaVu Sans', sans-serif; color: #8a7d6b; letter-spacing: .04em; text-transform: uppercase;
        border-bottom: .3mm solid #b5452f; padding-bottom: 1.2mm; margin-bottom: 3mm; }
.meta b { color: #b5452f; font-weight: 600; }
h2 { font-size: 1.3em; margin: 2mm 0 2mm; color: #7a2716; font-weight: 700; }
p { margin: 0 0 2.2mm; text-align: left; hyphens: auto; }
.caps { margin-top: 3mm; border-top: .2mm dotted #b9ad9b; padding-top: 2mm; }
.cap { font-size: .9em; margin-bottom: 1.6mm; }
.cap .source { color: #8a7d6b; font-style: italic; }
.note { margin-top: 3mm; font: .82em/1.35 'Noto Sans', 'DejaVu Sans', sans-serif; color: #6b6255; background: #f4efe6;
        padding: 2mm 2.5mm; border-left: .8mm solid #c9b99f; }
.note b { font-weight: 600; }
.missing { color: #b5452f; font-style: italic; }
.title { break-after: page; height: 190mm; overflow: hidden; padding: 18mm 20mm 0; }
.title h1 { font-size: 30pt; margin: 0 0 3mm; color: #7a2716; line-height: 1.1; }
.title .sub { font-size: 14pt; margin-bottom: 12mm; }
.title .about { max-width: 170mm; font-size: 10.5pt; }
.toc { break-after: page; columns: 2; column-gap: 12mm; font-size: 10pt; padding: 4mm 6mm; }
.toc h1 { column-span: all; font-size: 16pt; color: #7a2716; margin: 0 0 4mm; }
.toc div { display: flex; gap: 2mm; break-inside: avoid; margin-bottom: 1mm; }
.toc span.t { flex: 1; } .toc span.n { color: #8a7d6b; }
"""


def run(out=None, force=False):
    """Собрать двуязычный PDF в `out` (по умолчанию — путь из настроек книги); существующий файл — только с `force`."""
    out = out or OUT
    pages = [
        f
        for f in sorted(glob.glob(os.path.join(BOOK, "pages", "p-*.jpg")))
        if tools.PAGE_ID.fullmatch(os.path.basename(f)[:-4])
    ]
    sheets, toc, missing = [], [], []
    for f in pages:
        pid = os.path.basename(f)[:-4]
        tj = os.path.join(BOOK, "translation", pid + ".json")
        t = page_json.read(tj) if os.path.exists(tj) else None
        if t is None:
            missing.append(pid)
        printed = (t or {}).get("printed")
        meta = (
            f"<b>{html.escape(LABELS['page'])} {html.escape(str(printed))}</b> · "
            if printed
            else ""
        ) + f"{html.escape(pid)} · {html.escape(LABELS.get((t or {}).get('kind'), ''))}"
        body = []
        if t is None:
            body.append(f'<p class="missing">{html.escape(LABELS["missing"])}</p>')
        else:
            for h in headings(t.get("translation")):
                toc.append((h, printed or pid))
            translation = strip_retelling_mark(t.get("translation"))
            body.append(f"<div>{md(translation)}</div>")
            caps = [c for c in t.get("captions") or [] if c.get("translation") or c.get("source")]
            if caps:
                body.append(
                    '<div class="caps">'
                    + "".join(
                        f'<div class="cap">{html.escape(c.get("translation") or "")}'
                        + (
                            f' <span class="source">({html.escape(c["source"])})</span>'
                            if c.get("source")
                            else ""
                        )
                        + "</div>"
                        for c in caps
                    )
                    + "</div>"
                )
            notes = clean_notes(t.get("notes"))
            if notes:
                body.append(
                    f'<div class="note"><b>{html.escape(LABELS["note"])}</b> {html.escape(notes)}</div>'
                )
        sheets.append(
            f'<section class="sheet"><div class="scan"><img src="data:image/jpeg;base64,{img64(f)}"></div>'
            f'<div class="translation"><div class="meta">{meta}</div>{"".join(body)}</div></section>'
        )
    about = "".join(f"<p>{p}</p>" for p in TITLE.get("about", []))
    title = (
        f'<section class="title"><h1>{TITLE.get("heading", "")}</h1>'
        f'<div class="sub">{TITLE.get("subtitle", "")}</div><div class="about">{about}</div></section>'
    )
    doc = (
        f'<!doctype html><html lang="{html.escape(LANG, quote=True)}"><head><meta charset="utf-8">'
        f"{tools.csp_meta(FIT_JS)}<style>{CSS}</style></head><body>"
        f"{title}{''.join(sheets)}<script>{FIT_JS}</script></body></html>"
    )
    path = tools.write_build_file(BUILD, "bilingual.html", doc)
    out = tools.print_pdf(tools.find_chrome(CHROME), path, out, force=force)
    print(
        f"{out}: страниц книги {len(pages)}, без перевода {len(missing)}"
        + (f" ({', '.join(missing[:10])}…)" if missing else "")
    )
