"""Читалка: HTML-страница, где оригинал и перевод стоят рядом и подсвечиваются парами.

Единица пары — предложение (если в абзаце оригинала и перевода их поровну), строка стиха (если строк
поровну), иначе абзац целиком. Слова из поля `doubts` перевода страницы выделены в оригинале, при наведении
показывают догадку переводчика и, если рядом лежит `сомнения-astra.tsv`, второе мнение. Файл открывается
без сети: снимки страниц вшиты, шрифты системные.
"""

from __future__ import annotations

import base64
import csv
import glob
import html
import io
import os
import re
import tempfile
from pathlib import Path

from PIL import Image

from book_translate import page_json, tools

SCAN_WIDTH = 760
SCAN_QUALITY = 62
CONFIRMED = "да"
SECOND_OPINION = "сомнения-astra.tsv"
SENTENCE_END = re.compile(r'(?<=[.!?…])["»”)]?\s+(?=[—–«"„(A-ZÄÖÅŠŽČŃА-ЯЁ])')
ALTERNATIVES = re.compile(r" / |…")
MAX_DOUBT_WORD = 200


def sentences(text: str) -> list[str]:
    """Предложения абзаца; кавычка после точки остаётся в своём предложении."""
    parts, start = [], 0
    for m in SENTENCE_END.finditer(text):
        end = m.start() + len(m.group(0).rstrip())
        parts.append(text[start:end].strip())
        start = m.end()
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def units(source: str, translation: str) -> list[tuple[list[tuple[str, str]], bool]]:
    """Абзацы страницы: пары (оригинал, перевод) самого мелкого уровня, где число совпало, и признак стиха."""
    src = [p.strip() for p in re.split(r"\n\s*\n", (source or "").strip()) if p.strip()]
    tr = [p.strip() for p in re.split(r"\n\s*\n", (translation or "").strip()) if p.strip()]
    if len(src) != len(tr):
        return [([((source or "").strip(), (translation or "").strip())], False)]
    out = []
    for s, t in zip(src, tr):
        s_lines, t_lines = s.split("\n"), t.split("\n")
        if len(s_lines) > 1 and len(s_lines) == len(t_lines):
            out.append((list(zip(s_lines, t_lines)), True))
            continue
        s_sent, t_sent = sentences(s), sentences(t)
        out.append((list(zip(s_sent, t_sent)) if len(s_sent) == len(t_sent) else [(s, t)], False))
    return out


def second_opinion(workdir: Path) -> dict:
    path = workdir / SECOND_OPINION
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {
            (r.get("страница", ""), r.get("слово", "")): r
            for r in csv.DictReader(f, delimiter="\t")
        }


def doubt_tip(d: dict, opinion: dict | None) -> str:
    tip = f"{d.get('word', '')}\nдогадка: {d.get('guess', '?')}"
    if opinion:
        verdict = opinion.get("вердикт", "")
        variant = opinion.get("вариант_astra", "")
        note = opinion.get("пояснение", "")
        tip += (
            f"\nAstra: {verdict}"
            + (f" — {variant}" if variant else "")
            + (f" ({note})" if note else "")
        )
    if d.get("checked"):
        tip += f"\nсловарь: {d['checked']}" + (
            f" — {d['dictionary']}" if d.get("dictionary") else ""
        )
    if d.get("fix"):
        tip += f"\nправка: {d['fix']}"
    for lookup in d.get("lookups") or []:
        tip += f"\n{lookup.get('source', '')}: {lookup.get('verdict', '')}" + (
            f" — {lookup['note']}" if lookup.get("note") else ""
        )
    return tip


def doubt_class(d: dict) -> str:
    verdicts = [d.get("checked", "")] + [x.get("verdict", "") for x in d.get("lookups") or []]
    return "doubt ok" if any(str(v).startswith(CONFIRMED) for v in verdicts) else "doubt"


def marked(text: str, spans: list[tuple[int, int, str, str]]) -> str:
    out, pos = [], 0
    for a, b, tip, cls in sorted(spans):
        if a < pos:
            continue
        out.append(html.escape(text[pos:a]))
        out.append(
            f'<mark class="{cls}" data-tip="{html.escape(tip)}">{html.escape(text[a:b])}</mark>'
        )
        pos = b
    out.append(html.escape(text[pos:]))
    return "".join(out)


def doubt_spans(
    texts: list[str], doubts: list[dict], tips: list[str]
) -> list[list[tuple[int, int, str, str]]]:
    """Первое вхождение каждого сомнительного слова (или его частей через « / » и «…») в текстах страницы."""
    spans = [[] for _ in texts]
    for d, tip in zip(doubts, tips):
        word = d.get("word", "")
        if len(word) > MAX_DOUBT_WORD:
            continue
        for piece in ALTERNATIVES.split(word):
            piece = piece.strip(' «»"')
            if len(piece) < 2:
                continue
            pattern = re.compile(re.escape(piece), re.IGNORECASE)
            for i, text in enumerate(texts):
                m = pattern.search(text)
                if m:
                    spans[i].append((m.start(), m.end(), tip, doubt_class(d)))
                    break
    return spans


def scan64(path: str) -> str:
    im = Image.open(path).convert("RGB")
    if im.width > SCAN_WIDTH:
        im = im.resize((SCAN_WIDTH, round(im.height * SCAN_WIDTH / im.width)), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=SCAN_QUALITY, optimize=True)
    return base64.b64encode(b.getvalue()).decode()


def page_html(pid: str, t: dict, scan: str | None, opinions: dict, labels: dict) -> str:
    doubts = [d for d in t.get("doubts") or [] if isinstance(d, dict)]
    tips = [doubt_tip(d, opinions.get((pid, d.get("word", "")))) for d in doubts]
    paragraphs = units(t.get("source"), t.get("translation"))
    pairs = [
        (s.removeprefix("# ").strip(), tr.removeprefix("# ").strip())
        for para, _ in paragraphs
        for s, tr in para
    ]
    spans = doubt_spans([s for s, _ in pairs], doubts, tips)
    src_html, tr_html, k = [], [], 0
    for para, verse in paragraphs:
        src_parts, tr_parts = [], []
        for _ in para:
            s_text, t_text = pairs[k]
            key = f"{pid}-{k}"
            src_parts.append(f'<span class="u" data-u="{key}">{marked(s_text, spans[k])}</span>')
            tr_parts.append(f'<span class="u" data-u="{key}">{html.escape(t_text)}</span>')
            k += 1
        tag = "h2" if para[0][0].startswith("# ") else "p"
        cls = ' class="verse"' if verse else ""
        joiner = "<br>" if verse else " "
        src_html.append(f"<{tag}{cls}>{joiner.join(src_parts)}</{tag}>")
        tr_html.append(f"<{tag}{cls}>{joiner.join(tr_parts)}</{tag}>")
    for i, c in enumerate(t.get("captions") or []):
        key = f"{pid}-c{i}"
        src_html.append(
            f'<p class="cap"><span class="u" data-u="{key}">{html.escape(c.get("source", ""))}</span></p>'
        )
        tr_html.append(
            f'<p class="cap"><span class="u" data-u="{key}">{html.escape(c.get("translation", ""))}</span></p>'
        )
    printed = t.get("printed")
    label = (
        f"{html.escape(labels['page'])} {html.escape(str(printed))}"
        if printed
        else html.escape(pid)
    )
    scan_html = (
        f'<details class="scan"><summary>снимок страницы</summary>'
        f'<img loading="lazy" alt="{label}" src="data:image/jpeg;base64,{scan}"></details>'
        if scan
        else ""
    )
    notes = t.get("notes") or ""
    notes_html = (
        f'<aside class="note"><b>{html.escape(labels["note"])}</b> {html.escape(notes)}</aside>'
        if notes
        else ""
    )
    return (
        f'<section class="page" id="{pid}"><header><span class="num">{label}</span>{scan_html}</header>'
        f'<div class="src" lang="{labels["source_lang"]}">{"".join(src_html)}</div>'
        f'<div class="tr" lang="{labels["target_lang"]}">{"".join(tr_html)}</div>{notes_html}</section>'
    )


CSS = """
:root{--paper:#f2f4f2;--ink:#1d2627;--muted:#5d6a69;--rule:#c9d1cf;--pair:#e8b4b8;--pair-ink:#5a1520;--doubt:#b7791f}
*{box-sizing:border-box}
html{background:var(--paper);color:var(--ink)}
body{margin:0;font:17px/1.6 "Noto Serif","PT Serif",Georgia,serif}
.top{max-width:1180px;margin:0 auto;padding:56px 28px 24px;border-bottom:2px solid var(--ink)}
.top h1{font-size:44px;line-height:1.1;margin:0 0 10px;font-weight:700;letter-spacing:-.01em}
.top p{margin:4px 0;color:var(--muted);max-width:760px}
.legend{display:flex;gap:22px;flex-wrap:wrap;margin-top:18px;font:14px/1.4 "Noto Sans","PT Sans",sans-serif;color:var(--muted)}
.legend i{font-style:normal;padding:1px 6px;border-radius:3px}
.legend .l1{background:var(--pair);color:var(--pair-ink)}
.legend .l2{border-bottom:2px dotted var(--doubt)}
.legend .l3{border-bottom:1px dotted var(--rule)}
main{max-width:1180px;margin:0 auto;padding:0 28px 120px}
.page{display:grid;grid-template-columns:1fr 1fr;column-gap:44px;padding:34px 0 26px;border-bottom:1px solid var(--rule)}
.page header{grid-column:1/-1;display:flex;align-items:baseline;gap:18px;margin-bottom:8px}
.num{font:600 13px/1 "Noto Sans","PT Sans",sans-serif;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.scan summary{cursor:pointer;font:13px "Noto Sans","PT Sans",sans-serif;color:var(--muted)}
.scan img{display:block;max-width:100%;margin:10px 0;border:1px solid var(--rule)}
.src{color:#33403f;font-size:16px}
.src p,.tr p{margin:0 0 .75em}
.verse{padding-left:1.2em;border-left:2px solid var(--rule)}
h2{font-size:26px;margin:.2em 0 .5em}
.cap{font-size:14px;color:var(--muted);font-style:italic}
.u{border-radius:3px;transition:background .12s,color .12s;cursor:default}
.u.hl{background:var(--pair);color:var(--pair-ink)}
mark.doubt{background:none;color:inherit;border-bottom:2px dotted var(--doubt);cursor:help}
mark.doubt.ok{border-bottom:1px dotted var(--rule)}
.note{grid-column:1/-1;margin-top:10px;padding:10px 14px;border-left:3px solid var(--rule);font:14px/1.5 "Noto Sans","PT Sans",sans-serif;color:var(--muted)}
#tip{position:fixed;z-index:9;max-width:340px;padding:9px 12px;background:var(--ink);color:#f2f4f2;font:13px/1.45 "Noto Sans","PT Sans",sans-serif;white-space:pre-line;border-radius:4px;pointer-events:none;opacity:0;transition:opacity .1s}
#tip.on{opacity:1}
@media (max-width:760px){.page{grid-template-columns:1fr}.tr{margin-top:12px;padding-top:12px;border-top:1px dashed var(--rule)}.top h1{font-size:32px}}
@media (prefers-reduced-motion:reduce){.u,#tip{transition:none}}
"""

JS = """
const tip=document.getElementById('tip');
let lit=[];
function light(key){for(const e of lit)e.classList.remove('hl');lit=key?[...document.querySelectorAll('[data-u="'+key+'"]')]:[];for(const e of lit)e.classList.add('hl');}
document.addEventListener('mouseover',e=>{const u=e.target.closest('.u');light(u?u.dataset.u:null);
const d=e.target.closest('mark.doubt');if(d){tip.textContent=d.dataset.tip;tip.classList.add('on');}else tip.classList.remove('on');});
document.addEventListener('mousemove',e=>{if(!tip.classList.contains('on'))return;const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8);const y=e.clientY+18+tip.offsetHeight>innerHeight?e.clientY-tip.offsetHeight-12:e.clientY+18;tip.style.left=x+'px';tip.style.top=y+'px';});
document.addEventListener('click',e=>{const u=e.target.closest('.u');light(u?u.dataset.u:null);});
"""


def publish(target: Path, text: str, force: bool) -> None:
    """Записать файл атомарно через уникальный временный; без force существующий файл не заменяется."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix="." + target.name, suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if force:
            os.replace(tmp, target)
        else:
            os.link(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(book, out: str | None = None, force: bool = False) -> Path:
    workdir = Path(book.workdir)
    target = Path(out) if out else Path(book.out_reader)
    if target.exists() and not force:
        raise FileExistsError(f"{target} уже есть; перезаписать — --force")
    files = sorted(
        f
        for f in glob.glob(str(workdir / "translation" / "p-*.json"))
        if tools.PAGE_ID.fullmatch(Path(f).stem)
    )
    if not files:
        raise FileNotFoundError(f"нет переводов в {workdir / 'translation'}")
    opinions = second_opinion(workdir)
    labels = {
        "page": book.label("page"),
        "note": book.label("note"),
        "source_lang": html.escape(book.ocr_lang[:2]),
        "target_lang": html.escape(book.target_lang),
    }
    sections, pairs, fine = [], 0, 0
    for f in files:
        pid = Path(f).stem
        t = page_json.read(f)
        scan_path = workdir / "pages" / f"{pid}.jpg"
        scan = scan64(str(scan_path)) if scan_path.exists() else None
        for para, _ in units(t.get("source"), t.get("translation")):
            pairs += 1
            fine += len(para) > 1
        sections.append(page_html(pid, t, scan, opinions, labels))
    title = {
        k: (v.replace("<br>", " ") if isinstance(v, str) else v)
        for k, v in (book.title or {}).items()
    }
    about = "".join(f"<p>{html.escape(p)}</p>" for p in title.get("reader_about") or [])
    subtitle = f"<p>{html.escape(title['subtitle'])}</p>" if title.get("subtitle") else ""
    doc = (
        f'<!doctype html><html lang="{labels["target_lang"]}"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title.get('heading', book.slug))}</title>{tools.csp_meta(JS)}<style>{CSS}</style></head><body>"
        f'<div class="top"><h1>{html.escape(title.get("heading", book.slug))}</h1>'
        f"{subtitle}{about}"
        f'<div class="legend"><span><i class="l1">наведите</i> на фразу — подсветится её пара</span>'
        f'<span><i class="l2">пунктир</i> — слово, в переводе которого переводчик не уверен</span>'
        f'<span><i class="l3">бледный пунктир</i> — сомнение снято по словарю</span></div></div>'
        f'<main>{"".join(sections)}</main><div id="tip"></div><script>{JS}</script></body></html>'
    )
    publish(target, doc, force)
    print(
        f"{target}: страниц {len(files)}, абзацев {pairs}, разбиты на фразы {fine}, второе мнение {len(opinions)}"
    )
    return target
