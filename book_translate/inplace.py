"""Перевод прямо на странице: текст оригинала на скане заменён переводом в той же вёрстке.

Блоки и маски — из `layout`. Блок, чьи буквенные триграммы входят в подпись, заголовок, цитату или колонтитул из
`translation/p-*.json`, получает их перевод; крупные блоки без пары получают перевод страницы, перетекающий по
колонкам; сноски ставятся построчно в зону сносок. Плашки не ложатся на фото и не пересекаются, растут только в
пустое место; что не влезло — в свободное белое место той же страницы со звёздочкой, иначе мельче. На выкройках
плашки прозрачные: буквы оригинала стираются со скана, линии сетки и чертежа сохраняются.
"""

import base64
import csv
import glob
import html
import io
import json
import os
import re
import statistics
import numpy as np
from PIL import Image, ImageDraw, ImageOps

from book_translate import bilingual, page_json, tools
from book_translate.bilingual import strip_retelling_mark, md

BOOK = BLOCKS = OUT = ROTATED = TESS = None
TESS_ENV = {}
OCR_LANG = None
CHROME = BUILD = LANG = PAGE_H_MM = TESSERACT = None
PAGE_W_MM = 148
SOURCE_LOWER = None
SOURCE_UPPER = None
TARGET_LETTERS = None
SOURCE_NAMES = ()
CREDIT_WORDS = ()
MASK_CELL = 4
RETYPESET_RANGES = ()
RETYPESET_KINDS = ("toc",)
WORD_CONF = 50
MATCH_CONTAINMENT = 0.6
MATCH_MIN_GRAMS = 3
SHORT_GRAMS = 6
SHORT_CONTAINMENT = 0.85
PHOTO_TOLERANCE = 0.03
PHOTO_HALO_CELLS = 3
BODY_MIN_WORDS = 8
FONT_PER_WORD_HEIGHT = 1.15
EDGE_MARGIN = 12
QUOTE_OPEN = "\"«”“'"
ATTRIBUTION_SOURCE = ()
ATTRIBUTION_TARGET = ()


def configure(book):
    """Перенести настройки книги в модуль (пути, язык, кавычки, подписи к цитатам, сноски)."""
    global \
        BOOK, \
        BLOCKS, \
        OUT, \
        ROTATED, \
        TESS, \
        TESS_ENV, \
        OCR_LANG, \
        CHROME, \
        BUILD, \
        LANG, \
        PAGE_W_MM, \
        PAGE_H_MM, \
        TESSERACT, \
        SOURCE_LOWER, \
        SOURCE_UPPER
    global \
        TARGET_LETTERS, \
        SOURCE_NAMES, \
        CREDIT_WORDS, \
        RETYPESET_RANGES, \
        RETYPESET_KINDS, \
        QUOTE_OPEN, \
        QUOTE_CLOSE
    global ATTRIBUTION_SOURCE, ATTRIBUTION_TARGET, UNIT_WORDS, NOTES_HEAD
    BOOK, BLOCKS, OUT, ROTATED = (
        str(book.workdir),
        str(book.blocks),
        str(book.out_inplace),
        str(book.rotated),
    )
    bilingual.configure(book)
    TESSERACT, TESS, TESS_ENV = book, None, {}
    OCR_LANG, CHROME, BUILD, LANG = book.ocr_lang, book.chrome, book.build, book.target_lang
    PAGE_W_MM, PAGE_H_MM = book.page_width_mm, book.page_height_mm
    SOURCE_LOWER, SOURCE_UPPER, TARGET_LETTERS = (
        book.source_lower,
        book.source_upper,
        book.target_letters,
    )
    SOURCE_NAMES = tuple(book.source_names.items())
    CREDIT_WORDS = book.credit_words
    RETYPESET_RANGES, RETYPESET_KINDS = book.retypeset_ranges, book.retypeset_kinds
    QUOTE_OPEN, QUOTE_CLOSE = book.quote_open, book.quote_close
    ATTRIBUTION_SOURCE, ATTRIBUTION_TARGET = book.attribution_source, book.attribution_target
    UNIT_WORDS = {w.lower() for w in book.service_words}
    NOTES_HEAD = re.compile(
        r"\n\s*(?:-{3,}\s*\n\s*)?(?:"
        + "|".join(map(re.escape, book.notes_headings))
        + r")[^:\n]*:\s*"
    )


def retypeset(pid, t):
    return t.get("kind") in RETYPESET_KINDS or any(a <= pid <= b for a, b in RETYPESET_RANGES)


def retypeset_html(t):
    caps = "".join(
        f'<p class="cap">{html.escape(c["translation"])}</p>'
        for c in t.get("captions") or []
        if c.get("translation")
    )
    num = html.escape(str(t.get("printed") or ""))
    return (
        f'<section class="page retype"><div class="flow">{md(strip_retelling_mark(t.get("translation")))}{caps}</div>'
        f'<div class="folio">{num}</div></section>'
    )


def without_source_glosses(s):
    s = re.sub(r"\s*\((?![^)]*[" + TARGET_LETTERS + r"])[" + SOURCE_UPPER + r"][^)]*\)", "", s)
    return s


def letters(s):
    return re.sub("[^" + SOURCE_LOWER + "]", "", (s or "").lower())


def grams(s):
    s = letters(s)
    return {s[i : i + 3] for i in range(len(s) - 2)}


def is_word(w):
    t = w["t"]
    return w["c"] >= WORD_CONF and sum(c.isalnum() for c in t) >= max(1, 0.6 * len(t))


def is_faint_word(w):
    t = w["t"]
    return sum(c.isalpha() for c in t) >= FAINT_MIN_LETTERS and sum(
        c.isalnum() for c in t
    ) >= 0.6 * len(t)


def mixed_case(text):
    return any(c.isupper() for c in text) and any(c.islower() for c in text)


def is_source_word(w):
    t = w["t"].strip("\"«»“”().,:;!?'")
    return is_word(w) and len(t) >= 2 and t[:1].isalpha() and (not mixed_case(t[1:]) or len(t) <= 3)


OUTLIER_WORDS = 2
BAND_LINES = 2.5
FAINT_MIN_LETTERS = 4
QUOTE_GAP_LINES = 3
NESTED_SHARE = 0.5
ATTRIBUTION_MAX_CHARS = 50
QUOTE_MAX_PARAS = 8


def main_cluster(ws, rotated):
    if len(ws) < 3:
        return ws
    lo, hi = ("x0", "x1") if rotated else ("y0", "y1")
    h = statistics.median(w[hi] - w[lo] for w in ws)
    ws = sorted(ws, key=lambda w: w[lo] + w[hi])
    groups = [[ws[0]]]
    for w in ws[1:]:
        prev = groups[-1][-1]
        if (w[lo] + w[hi]) / 2 - (prev[lo] + prev[hi]) / 2 > 2.5 * h:
            groups.append([w])
        else:
            groups[-1].append(w)
    return [w for g in groups if len(g) > OUTLIER_WORDS for w in g] or max(groups, key=len)


def box_of(ws):
    return [
        min(w["x0"] for w in ws),
        min(w["y0"] for w in ws),
        max(w["x1"] for w in ws),
        max(w["y1"] for w in ws),
    ]


def prepare(blocks, core=None):
    out = []
    for b in blocks:
        source_words = main_cluster(
            [w for w in b.get("words", []) if is_source_word(w)], b["rotated"]
        )
        if not source_words:
            continue
        lo, hi = min(w["y0"] for w in source_words), max(w["y1"] for w in source_words)
        h = statistics.median(
            w["y1"] - w["y0"] if not b["rotated"] else w["x1"] - w["x0"] for w in source_words
        )
        keep = [
            w
            for w in b["words"]
            if (is_word(w) or (is_faint_word(w) and (core is None or not on_photo(core, w))))
            and lo - BAND_LINES * h <= (w["y0"] + w["y1"]) / 2 <= hi + BAND_LINES * h
        ]
        out.append(
            {
                **b,
                "src": source_words,
                "box": box_of(keep),
                "wh": h,
                "text": " ".join(w["t"] for w in source_words),
            }
        )
    kept = []
    for b in sorted(out, key=lambda b: -len(b["src"])):
        host = next((o for o in kept if inside_share(b["box"], o["box"]) > NESTED_SHARE), None)
        if host is None:
            kept.append(b)
        else:
            host["box"] = [
                min(host["box"][0], b["box"][0]),
                min(host["box"][1], b["box"][1]),
                max(host["box"][2], b["box"][2]),
                max(host["box"][3], b["box"][3]),
            ]
    return [b for b in out if any(b is k for k in kept)]


NOTES_HEAD = None
NOTE_SPLIT = re.compile(r"(?<=[.;!?»)…])\s+(?=\d{1,3}(?:[–-]\d{1,3})?\s+(?!\d))")
NOTE_ITEM = re.compile(r"(\d{1,3}(?:[–-]\d{1,3})?)\s+(.*)", re.S)
NOTES_ZONE_TOP = 0.6
NOTES_DIGIT_SHARE = 0.15
NOTES_MIN_WORDS = 5


def split_notes(translation):
    heads = list(NOTES_HEAD.finditer(translation))
    if not heads:
        return translation, []
    body, text = translation[: heads[-1].start()], translation[heads[-1].end() :].strip()
    parts = [line.strip() for line in text.split("\n") if line.strip()]
    if len(parts) == 1:
        parts = NOTE_SPLIT.split(parts[0])
    notes = []
    for part in parts:
        m = NOTE_ITEM.match(part)
        if m:
            notes.append((m.group(1), m.group(2).strip()))
        elif notes:
            notes[-1] = (notes[-1][0], notes[-1][1] + " " + part)
        else:
            notes.append(("", part))
    return body, notes


def target_source_names(text):
    for source, translation in SOURCE_NAMES:
        text = re.sub(r"(?<![(\w])" + re.escape(source) + r"(?!\w)", translation, text)
    return text


def notes_html(notes):
    return "".join(
        f'<div class="fn"><b>{html.escape(n)}</b> {html.escape(target_source_names(x))}</div>'
        for n, x in notes
    )


def is_digit_word(w):
    return re.fullmatch(r"\d{1,3}[).]?", w["t"]) is not None


def notes_zone(raw_blocks, H):
    seeds, columns = [], []
    for b in raw_blocks:
        ws = b.get("words", [])
        if b["rotated"] or b["y0"] < NOTES_ZONE_TOP * H or not ws:
            continue
        digits = sum(bool(re.search(r"\d", w["t"])) for w in ws)
        number_column = (
            len(ws) >= 2
            and all(is_digit_word(w) for w in ws)
            and b["y1"] - b["y0"] > b["x1"] - b["x0"]
        )
        if number_column:
            columns.append(b)
        elif (
            len(ws) >= NOTES_MIN_WORDS
            and digits / len(ws) >= NOTES_DIGIT_SHARE
            and not any(w["t"].startswith(CREDIT_WORDS) for w in ws if CREDIT_WORDS)
        ):
            seeds.append(b)
    seeds = columns + seeds
    if not seeds:
        return None
    top, bottom = min(b["y0"] for b in seeds), max(b["y1"] for b in seeds)
    members = [
        b
        for b in raw_blocks
        if not b["rotated"] and top - 4 <= (b["y0"] + b["y1"]) / 2 <= bottom + 4
    ]
    return [min(b["x0"] for b in members), top, max(b["x1"] for b in members), bottom]


def inner_text(para):
    return re.sub(r"[!?.,…]+$", "", para.rstrip())[1:-1]


def without_attribution(text):
    lines = text.rstrip().split("\n")
    tail = lines[-1].strip()
    if (
        len(lines) > 1
        and len(tail) <= ATTRIBUTION_MAX_CHARS
        and not any(ch in QUOTE_OPEN + QUOTE_CLOSE for ch in tail)
        and lines[-2].rstrip().rstrip("!?.,…")[-1:] in QUOTE_CLOSE
    ):
        return "\n".join(lines[:-1])
    return text


def opens_quote(para):
    return para[:1] in QUOTE_OPEN and not any(
        ch in QUOTE_CLOSE for ch in inner_text(without_attribution(para.split("\n\n")[0]))
    )


def closes_quote(group):
    group = without_attribution(group).rstrip()
    return group.rstrip("!?.,…")[-1:] in QUOTE_CLOSE or group[-1:] in QUOTE_CLOSE


def is_attribution(para, attributions):
    if para.startswith(attributions):
        return True
    return (
        len(para) <= ATTRIBUTION_MAX_CHARS and "\n" not in para and para.rstrip()[-1:] not in ".:"
    )


def quotes(t):
    def groups(text, attributions):
        paras = [
            p.strip() for p in re.split(r"\n\s*\n", strip_retelling_mark(text or "")) if p.strip()
        ]
        out, i = [], 0
        while i < len(paras):
            if opens_quote(paras[i]):
                g, first = paras[i], i
                while not closes_quote(g) and i + 1 < len(paras) and i - first < QUOTE_MAX_PARAS:
                    i += 1
                    g += "\n\n" + paras[i]
                if not closes_quote(g) or any(
                    not opens_quote(p) and not p.startswith(attributions) and p[:1] in QUOTE_OPEN
                    for p in paras[first + 1 : i + 1]
                ):
                    out.append(None)
                    i += 1
                    continue
                if i + 1 < len(paras) and is_attribution(paras[i + 1], attributions):
                    g, i = g + "\n" + paras[i + 1], i + 1
                out.append(g)
            i += 1
        return out

    source, translation = (
        groups(t.get("source"), ATTRIBUTION_SOURCE),
        groups(t.get("translation"), ATTRIBUTION_TARGET),
    )
    return (
        [
            {"source": f, "translation": r, "quote": True}
            for f, r in zip(source, translation)
            if f and r
        ]
        if len(source) == len(translation)
        else []
    )


def running_head(t):
    source = [p.strip() for p in re.split(r"\n\s*\n", t.get("source") or "") if p.strip()]
    translation = [
        p.strip()
        for p in re.split(r"\n\s*\n", strip_retelling_mark(t.get("translation")))
        if p.strip()
    ]
    if not source or not translation:
        return []
    head_source, head_translation = source[0], translation[0]
    looks_like_head = (
        head_source[:1] not in QUOTE_OPEN
        and len(head_source) <= 90
        and len(head_translation) <= 120
        and not head_source.startswith("#")
        and not head_translation.startswith("#")
        and re.search(r"\s[–-]\s", head_source) is not None
    )
    return (
        [{"source": head_source, "translation": head_translation, "running": True}]
        if looks_like_head
        else []
    )


def headings(t):
    source = [
        line.lstrip("#").strip()
        for line in (t.get("source") or "").split("\n")
        if line.startswith("#")
    ]
    translation = [
        line.lstrip("#").strip()
        for line in strip_retelling_mark(t.get("translation")).split("\n")
        if line.startswith("#")
    ]
    return (
        [{"source": f, "translation": r, "heading": True} for f, r in zip(source, translation)]
        if len(source) == len(translation)
        else []
    )


UNIT_WORDS = {"cm", "mm"}
FIND_WORD_CONF = 15
FIND_CONTAINMENT = 0.5
FIND_WORD_SHARE = 0.5


def has_source_words(source):
    words = [
        w.lower() for w in re.findall("[" + SOURCE_LOWER + SOURCE_UPPER + "]{3,}", source or "")
    ]
    return any(w not in UNIT_WORDS for w in words)


def stretch_to_label(box, ws, source, rotated):
    lo, hi = (1, 3) if rotated else (0, 2)
    found = sum(len(re.sub(r"\W", "", w["t"])) for w in ws) + len(ws) - 1
    need = len(re.sub(r"[^\w ]", "", source).strip())
    span = box[hi] - box[lo]
    if not found or need <= found:
        return box
    extra = span * (need / found - 1)
    box = list(box)
    box[lo], box[hi] = box[lo] - extra, box[hi] + extra
    return box


WORD_CHAIN_GAP = 4


def contiguous(line, w0, along, gap):
    line = sorted(line, key=lambda p: p[0][along[0]])
    k = next(i for i, (w, _) in enumerate(line) if w is w0)
    lo = k
    while lo > 0 and line[lo][0][along[0]] - line[lo - 1][0][along[1]] <= gap:
        lo -= 1
    hi = k
    while hi + 1 < len(line) and line[hi + 1][0][along[0]] - line[hi][0][along[1]] <= gap:
        hi += 1
    return line[lo : hi + 1]


def find_words(raw_blocks, source, across_blocks=False):
    target = grams(source)
    if len(target) < 3:
        return None
    hits = []
    heights = [
        w["y1"] - w["y0"]
        for b in raw_blocks
        if not b["rotated"]
        for w in b.get("words", [])
        if w["c"] >= 60
    ]
    tall = 1.3 * statistics.median(heights) if heights and across_blocks else 0
    for b in raw_blocks:
        for w in b.get("words", []):
            g = grams(w["t"])
            if across_blocks and (b["rotated"] or w["y1"] - w["y0"] < tall):
                continue
            if (
                w["c"] >= FIND_WORD_CONF
                and len(letters(w["t"])) >= 2
                and g
                and len(g & target) / len(g) >= FIND_WORD_SHARE
            ):
                hits.append((w, b))
    best = None
    for w0, b0 in hits:
        h0 = (w0["y1"] - w0["y0"]) if not b0["rotated"] else (w0["x1"] - w0["x0"])
        across, along = (
            (("x0", "x1"), ("y0", "y1")) if b0["rotated"] else (("y0", "y1"), ("x0", "x1"))
        )
        line = [
            (w, b)
            for w, b in hits
            if b["rotated"] == b0["rotated"]
            and (across_blocks or b is b0)
            and abs((w[across[0]] + w[across[1]]) - (w0[across[0]] + w0[across[1]])) < 1.2 * h0
            and abs((w[along[0]] + w[along[1]]) - (w0[along[0]] + w0[along[1]])) < 30 * h0
        ]
        line = contiguous(line, w0, along, WORD_CHAIN_GAP * h0)
        cover = len(set().union(*(grams(w["t"]) for w, _ in line)) & target) / len(target)
        if cover >= FIND_CONTAINMENT * min(1, 12 / len(target)) and (
            best is None or cover > best[0]
        ):
            best = (cover, b0, [w for w, _ in line])
    if best is None:
        return None
    _, b, ws = best
    h = statistics.median(
        (w["y1"] - w["y0"]) if not b["rotated"] else (w["x1"] - w["x0"]) for w in ws
    )
    box = stretch_to_label(box_of(ws), ws, source, b["rotated"])
    return {
        **b,
        "src": ws,
        "box": box,
        "wh": h,
        "text": " ".join(w["t"] for w in ws),
        "x0": box[0],
        "y0": box[1],
        "x1": box[2],
        "y1": box[3],
    }


def tesseract():
    global TESS, TESS_ENV
    if TESS is None:
        TESS, TESS_ENV = TESSERACT.tesseract_cmd(2)
    return TESS, TESS_ENV


def rotated_words(pid, im, angles=(-90, 90)):
    """Слова, которых нет в блоках: подписи вдоль линий выкройки (страница повёрнута на ±90°) и подписи, слитые с фото (angles=(0,)). Кеш в layout/rotated."""
    path = os.path.join(
        ROTATED, pid + ("" if angles == (-90, 90) else "." + "_".join(map(str, angles))) + ".json"
    )
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    W, H = im.size
    words = []
    for angle in angles:
        turned = ImageOps.autocontrast(im.convert("L").rotate(angle, expand=True), cutoff=1)
        tsv = tools.ocr_tsv(*tesseract(), turned, OCR_LANG, 11)
        for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
            t = (row.get("text") or "").strip()
            if row.get("level") != "5" or not t:
                continue
            u0, v0 = int(row["left"]), int(row["top"])
            u1, v1 = u0 + int(row["width"]), v0 + int(row["height"])
            if angle == 0:
                x0, x1, y0, y1 = u0, u1, v0, v1
            elif angle == -90:
                x0, x1, y0, y1 = v0, v1, H - u1, H - u0
            else:
                x0, x1, y0, y1 = W - v1, W - v0, u0, u1
            words.append(
                {"t": t, "c": round(float(row["conf"]), 1), "x0": x0, "y0": y0, "x1": x1, "y1": y1}
            )
    os.makedirs(ROTATED, exist_ok=True)
    json.dump(words, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return words


def split_paired(captions):
    out = []
    for c in captions:
        source, translation = c["source"].split(" / "), c["translation"].split(" / ")
        if len(source) > 1 and len(source) == len(translation):
            out += [
                {**c, "source": f.strip(), "translation": r.strip()}
                for f, r in zip(source, translation)
            ]
        else:
            out.append(c)
    return out


CAPTION_LINE_GAP = 0.6


def text_lines(words, wh):
    lines = []
    for w in sorted(words, key=lambda w: (w["y0"] + w["y1"]) / 2):
        cy = (w["y0"] + w["y1"]) / 2
        if lines and abs(cy - lines[-1]["cy"]) < CAPTION_LINE_GAP * wh:
            lines[-1]["words"].append(w)
        else:
            lines.append({"cy": cy, "words": [w]})
    return [sorted(line["words"], key=lambda w: w["x0"]) for line in lines]


def split_caption_blocks(blocks, targets):
    caption_text = " ".join(c["source"] for c in targets)
    out = []
    for b in blocks:
        single = max((containment(b["text"], c["source"]) for c in targets), default=0)
        lines = text_lines(b["src"], b["wh"]) if not b["rotated"] else []
        if (
            len(lines) < 2
            or single >= MATCH_CONTAINMENT
            or containment(b["text"], caption_text) < CAPTION_BODY_SHARE
        ):
            out.append(b)
            continue
        for ws in lines:
            out.append(
                {
                    **b,
                    "src": ws,
                    "words": ws,
                    "box": box_of(ws),
                    "text": " ".join(w["t"] for w in ws),
                }
            )
    return out


def match(blocks, targets):
    tg = [grams(c["source"]) for c in targets]
    owner = {}
    for k, b in enumerate(blocks):
        bg = grams(b["text"])
        if len(bg) < MATCH_MIN_GRAMS:
            continue
        best = max(
            (
                (len(bg & g) / len(bg), 2 * len(bg & g) / (len(bg) + len(g)), i)
                for i, g in enumerate(tg)
                if g
            ),
            default=None,
        )
        if best and best[0] >= (MATCH_CONTAINMENT if len(bg) >= SHORT_GRAMS else SHORT_CONTAINMENT):
            owner[k] = best[2]
            blocks[k]["dice"] = best[1]
    for k, b in enumerate(blocks):
        word = letters(b["text"])
        on_other_line = any(on_same_line(blocks[j]["box"], b["box"], 2 * b["wh"]) for j in owner)
        if k not in owner and word and not on_other_line:
            same = [i for i, c in enumerate(targets) if letters(c["source"]) == word]
            if same:
                owner[k] = same[0]
                b["dice"] = 1.0
    return owner


QUOTE_LINE_CONTAINMENT = 0.5
CAPTION_BODY_SHARE = 0.8
PAIR_CONTAINMENT = 0.3


def containment(text, target):
    g = grams(text)
    return len(g & grams(target)) / len(g) if g else 0.0


def nearest_cluster(bs, gap_lines=2):
    seed = max(bs, key=lambda b: (b.get("dice", 0), len(b["src"])))
    cluster, box = [seed], list(seed["box"])
    grown = True
    while grown:
        grown = False
        for b in bs:
            if b not in cluster and touches(box, b["box"], gap_lines * seed["wh"]):
                cluster.append(b)
                box = [
                    min(box[0], b["box"][0]),
                    min(box[1], b["box"][1]),
                    max(box[2], b["box"][2]),
                    max(box[3], b["box"][3]),
                ]
                grown = True
    return cluster


COLOUR_CHROMA = 45
COLOUR_SHARE = 0.3


def colour_mask(im, grid):
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    chroma = a.max(axis=2) - a.min(axis=2)
    Hc, Wc = grid
    t = chroma[: Hc * MASK_CELL, : Wc * MASK_CELL].reshape(Hc, MASK_CELL, Wc, MASK_CELL)
    return (t > COLOUR_CHROMA).mean(axis=(1, 3)) > COLOUR_SHARE


TEXT_WORD_CONF = 85
TEXT_WORD_LETTERS = 4


def photo_masks(pid, im, grid, blocks=()):
    dark = load_mask(pid, "photo", grid)
    colour = colour_mask(im, grid)
    for b in blocks:
        for w in b.get("words", []):
            if w["c"] >= TEXT_WORD_CONF and len(letters(w["t"])) >= TEXT_WORD_LETTERS:
                dark[cells([w["x0"], w["y0"], w["x1"], w["y1"]])] = False
    return dark | colour, erode(dark, PHOTO_HALO_CELLS)


def load_mask(pid, kind, shape):
    p = os.path.join(BLOCKS, f"{pid}.{kind}.png")
    return np.asarray(Image.open(p)) > 0 if os.path.exists(p) else np.zeros(shape, bool)


def erode(mask, r):
    core = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            core &= np.roll(np.roll(mask, dy, 0), dx, 1)
    return core


def printed_numbers(pids):
    out, last = {}, None
    for pid in pids:
        path = os.path.join(BOOK, "translation", pid + ".json")
        t = page_json.read(path) if os.path.exists(path) else {}
        p = str(t.get("printed") or "").strip()
        if p.isdigit():
            last = int(p)
            out[pid] = p
        else:
            last = last + 1 if last is not None else None
            out[pid] = f"{last}" if last is not None else ""
    return out


def cells(box):
    x0, y0, x1, y1 = box
    return slice(max(0, int(y0) // MASK_CELL), int(y1) // MASK_CELL + 1), slice(
        max(0, int(x0) // MASK_CELL), int(x1) // MASK_CELL + 1
    )


def photo_share(photo, box):
    sub = photo[cells(box)]
    return sub.mean() if sub.size else 0.0


def free_right(obstacle, box, W):
    ys, xs = cells(box)
    right = xs.stop
    while (
        right < obstacle.shape[1]
        and right * MASK_CELL < W - EDGE_MARGIN
        and not obstacle[ys, right].any()
    ):
        right += 1
    return max(box[2], right * MASK_CELL)


def free_down(obstacle, box, H):
    ys, xs = cells(box)
    down = ys.stop
    while (
        down < obstacle.shape[0]
        and down * MASK_CELL < H - EDGE_MARGIN
        and not obstacle[down, xs].any()
    ):
        down += 1
    return max(box[3], down * MASK_CELL)


def paper_color(im, box, pad):
    x0, y0, x1, y1 = (int(v) for v in box)
    ring = []
    for b in (
        (x0, max(0, y0 - pad), x1, y0),
        (x0, y1, x1, min(im.height, y1 + pad)),
        (max(0, x0 - pad), y0, x0, y1),
        (x1, y0, min(im.width, x1 + pad), y1),
    ):
        if b[2] > b[0] and b[3] > b[1]:
            ring += list(im.crop(b).resize((8, 8)).getdata())
    ring = sorted(ring, key=sum)[len(ring) // 3 :] or [(245, 242, 235)]
    return "#%02x%02x%02x" % tuple(int(statistics.median(p[k] for p in ring)) for k in range(3))


def body_items(translation):
    items = []
    for block in re.split(r"\n\s*\n", translation.strip()):
        for ln in block.strip().split("\n"):
            if ln.startswith("#"):
                items.append(("h", html.escape(ln.lstrip("#").strip())))
            elif ln.strip() and not re.fullmatch(r"-{3,}", ln.strip()):
                items.append(("p", html.escape(ln.strip())))
    return items


def shrink_off_photo(photo, box, words, step=MASK_CELL, tolerance=PHOTO_TOLERANCE - 0.01):
    cx = [(w["x0"] + w["x1"]) / 2 for w in words]
    cy = [(w["y0"] + w["y1"]) / 2 for w in words]
    inner = [min(cx), min(cy), max(cx), max(cy)]
    box = list(box)
    while photo_share(photo, box) > tolerance:
        options = []
        for side, delta, limit in (
            (0, step, inner[0]),
            (1, step, inner[1]),
            (2, -step, inner[2]),
            (3, -step, inner[3]),
        ):
            trial = list(box)
            trial[side] += delta
            if (delta > 0 and trial[side] < limit) or (delta < 0 and trial[side] > limit):
                options.append((photo_share(photo, trial), trial))
        if not options:
            return None
        box = min(options, key=lambda o: o[0])[1]
    return box


def area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def inside_share(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / max(1, area(a))


def separate(boxes):
    boxes = [list(b) for b in boxes]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            ix = min(a[2], b[2]) - max(a[0], b[0])
            iy = min(a[3], b[3]) - max(a[1], b[1])
            if ix <= 0 or iy <= 0:
                continue
            small, big = (a, b) if area(a) <= area(b) else (b, a)
            if inside_share(small, big) > 0.5:
                above = (small[1] - big[1]) * (big[2] - big[0])
                below = (big[3] - small[3]) * (big[2] - big[0])
                if below >= above:
                    big[1] = small[3] + 1
                else:
                    big[3] = small[1] - 1
                continue
            if ix < iy:
                left, right = (a, b) if a[0] + a[2] <= b[0] + b[2] else (b, a)
                mid = (left[2] + right[0]) / 2
                left[2], right[0] = mid - 1, mid + 1
            else:
                top, bottom = (a, b) if a[1] + a[3] <= b[1] + b[3] else (b, a)
                mid = (top[3] + bottom[1]) / 2
                top[3], bottom[1] = mid - 1, mid + 1
    return boxes


def free_rect(obstacle, box, W, H, directions="lrud"):
    y0, x0 = max(0, int(box[1]) // MASK_CELL), max(0, int(box[0]) // MASK_CELL)
    y1, x1 = (
        min(obstacle.shape[0], int(box[3]) // MASK_CELL + 1),
        min(obstacle.shape[1], int(box[2]) // MASK_CELL + 1),
    )
    lim = (
        EDGE_MARGIN // MASK_CELL,
        EDGE_MARGIN // MASK_CELL,
        min(obstacle.shape[1], (W - EDGE_MARGIN) // MASK_CELL),
        min(obstacle.shape[0], (H - EDGE_MARGIN) // MASK_CELL),
    )
    grown = True
    while grown:
        grown = False
        if "r" in directions and x1 < lim[2] and not obstacle[y0:y1, x1].any():
            x1 += 1
            grown = True
        if "d" in directions and y1 < lim[3] and not obstacle[y1, x0:x1].any():
            y1 += 1
            grown = True
        if "l" in directions and x0 > lim[0] and not obstacle[y0:y1, x0 - 1].any():
            x0 -= 1
            grown = True
        if "u" in directions and y0 > lim[1] and not obstacle[y0 - 1, x0:x1].any():
            y0 -= 1
            grown = True
    return [
        min(box[0], (x0 + 1) * MASK_CELL),
        min(box[1], (y0 + 1) * MASK_CELL),
        max(box[2], (x1 - 1) * MASK_CELL),
        max(box[3], (y1 - 1) * MASK_CELL),
    ]


SPARE_MIN_MM = 18
SPARE_MIN_HEIGHT_MM = 6
SPARE_COUNT = 3


def largest_empty(obstacle):
    Hc, Wc = obstacle.shape
    heights = np.zeros(Wc, int)
    best = (0, None)
    for y in range(Hc):
        heights = np.where(obstacle[y], 0, heights + 1)
        stack = []
        for x in range(Wc + 1):
            h = heights[x] if x < Wc else 0
            start = x
            while stack and stack[-1][1] >= h:
                sx, sh = stack.pop()
                if sh * (x - sx) > best[0]:
                    best = (sh * (x - sx), (sx, y - sh + 1, x, y + 1))
                start = sx
            stack.append((start, h))
    return best[1]


def spare_rects(obstacle, W, H, mm):
    ob = obstacle.copy()
    m = EDGE_MARGIN // MASK_CELL + 1
    ob[:m, :] = ob[-m:, :] = True
    ob[:, :m] = ob[:, -m:] = True
    out = []
    for _ in range(SPARE_COUNT):
        r = largest_empty(ob)
        if r is None:
            break
        x0, y0, x1, y1 = (v * MASK_CELL for v in r)
        if (y1 - y0) * mm < SPARE_MIN_HEIGHT_MM or (x1 - x0) * mm < SPARE_MIN_MM:
            break
        out.append([x0 + 6, y0 + 6, x1 - 6, y1 - 6])
        ob[r[1] : r[3], r[0] : r[2]] = True
    return out


PAPER_LEVEL = 215
PAPER_DARK_SHARE = 0.01


def not_paper(im, grid):
    g = np.asarray(im.convert("L"))
    Hc, Wc = grid
    t = g[: Hc * MASK_CELL, : Wc * MASK_CELL].reshape(Hc, MASK_CELL, Wc, MASK_CELL)
    dark = (t < PAPER_LEVEL).mean(axis=(1, 3)) > PAPER_DARK_SHARE
    out = dark.copy()
    out[:-1] |= dark[1:]
    out[1:] |= dark[:-1]
    out[:, :-1] |= dark[:, 1:]
    out[:, 1:] |= dark[:, :-1]
    return out


GRID_LINE_SHARE = 0.7
GRID_PAPER_GAP = 25
GRID_MIN_LINES = 4
INK_ERASE_DEPTH = 90
LINE_RUN_LINES = 2.5
LETTER_ERASE_DEPTH = 45


def line_runs(flags):
    return int(np.sum(flags[1:] & ~flags[:-1]) + flags[0]) if flags.size else 0


def on_grid(gray, box, pad):
    x0, y0, x1, y1 = (int(v) for v in box)
    a = gray[max(0, y0 - pad) : y1 + pad, max(0, x0 - pad) : x1 + pad]
    if a.size == 0:
        return False
    dark = a < np.percentile(a, 90) - GRID_PAPER_GAP
    return (
        line_runs(dark.mean(axis=1) > GRID_LINE_SHARE)
        + line_runs(dark.mean(axis=0) > GRID_LINE_SHARE)
        >= GRID_MIN_LINES
    )


def long_runs(dark, length):
    keep = np.zeros_like(dark)
    for axis_dark, axis_keep in ((dark, keep), (dark.T, keep.T)):
        for i, row in enumerate(axis_dark):
            edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
            for start, stop in zip(edges[::2], edges[1::2]):
                if stop - start >= length:
                    axis_keep[i, start:stop] = True
    return keep


def long_strokes(dark, length):
    keep = long_runs(dark, length)
    dark = dark & ~keep
    label = np.zeros(dark.shape, np.int32)
    H, W = dark.shape
    current = 0
    for y0, x0 in zip(*np.nonzero(dark)):
        if label[y0, x0]:
            continue
        current += 1
        stack, ys, xs = [(y0, x0)], [], []
        label[y0, x0] = current
        while stack:
            y, x = stack.pop()
            ys.append(y)
            xs.append(x)
            for ny, nx in (
                (y - 1, x),
                (y + 1, x),
                (y, x - 1),
                (y, x + 1),
                (y - 1, x - 1),
                (y - 1, x + 1),
                (y + 1, x - 1),
                (y + 1, x + 1),
            ):
                if 0 <= ny < H and 0 <= nx < W and dark[ny, nx] and not label[ny, nx]:
                    label[ny, nx] = current
                    stack.append((ny, nx))
        if max(ys) - min(ys) >= length or max(xs) - min(xs) >= length:
            keep[label == current] = True
    return keep


def erase_letters(im, box, wh, photo=None):
    x0, y0, x1, y1 = (max(0, int(v)) for v in box)
    px = np.asarray(im).copy()
    sub = px[y0:y1, x0:x1]
    keep = np.zeros(sub.shape[:2], bool)
    if photo is not None:
        cover = np.repeat(np.repeat(photo, MASK_CELL, 0), MASK_CELL, 1)[y0:y1, x0:x1]
        keep[: cover.shape[0], : cover.shape[1]] = cover
    gray = sub.mean(axis=2)
    if gray.size == 0:
        return
    paper = np.percentile(gray, 90)
    lines = long_strokes(gray < paper - GRID_PAPER_GAP, int(LINE_RUN_LINES * wh))
    dark = (gray < paper - LETTER_ERASE_DEPTH) & ~lines
    dark = (
        dark
        | np.roll(dark, 1, 0)
        | np.roll(dark, -1, 0)
        | np.roll(dark, 1, 1)
        | np.roll(dark, -1, 1)
    )
    dark &= (gray < paper - GRID_PAPER_GAP) & ~lines
    bright = sub[gray >= paper - 10]
    colour = np.median(bright, axis=0) if len(bright) else np.array([245, 245, 245])
    sub[dark & ~is_colour(sub) & ~keep] = colour
    im.paste(Image.fromarray(px))


def is_colour(px):
    a = px.astype(np.int16)
    return a.max(axis=2) - a.min(axis=2) > COLOUR_CHROMA


def erase_words(im, words, box, depth=INK_ERASE_DEPTH):
    px = np.asarray(im).copy()
    gray = px.mean(axis=2)
    for w in words:
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["y0"] + w["y1"]) / 2
        if not (box[0] <= cx <= box[2] and box[1] <= cy <= box[3]):
            continue
        ys, xs = (
            slice(max(0, int(w["y0"]) - 2), int(w["y1"]) + 3),
            slice(max(0, int(w["x0"]) - 2), int(w["x1"]) + 3),
        )
        cell = gray[ys, xs]
        if cell.size == 0:
            continue
        paper = np.percentile(cell, 90)
        bright = px[ys, xs][cell >= paper - 10]
        colour = np.median(bright, axis=0) if len(bright) else np.array([245, 245, 245])
        sub = px[ys, xs]
        mask = cell < paper - depth
        mask = (
            mask
            | np.roll(mask, 1, 0)
            | np.roll(mask, -1, 0)
            | np.roll(mask, 1, 1)
            | np.roll(mask, -1, 1)
        )
        sub[mask & (cell < paper - GRID_PAPER_GAP) & ~is_colour(sub)] = colour
    im.paste(Image.fromarray(px))


LEFTOVER_CONTAINMENT = 0.6
RUNNING_HEAD_ZONE = 0.06
LEFTOVER_CONF = 75
LEFTOVER_MIN_LETTERS = 6


def is_leftover_text(b, source):
    letters_n = len(letters(b["text"]))
    if letters_n < LEFTOVER_MIN_LETTERS - 1:
        return False
    if containment(b["text"], source) >= LEFTOVER_CONTAINMENT:
        return True
    return (
        letters_n >= LEFTOVER_MIN_LETTERS
        and statistics.mean(w["c"] for w in b["src"]) >= LEFTOVER_CONF
    )


INK_GAP_LINES = 1.5
EXTEND_MAX_LINES = 8
FIRST_LINE_LINES = 1.2


def extend_along_ink(ink, box, wh):
    gap = max(1, int(INK_GAP_LINES * wh) // MASK_CELL)
    reach = int(EXTEND_MAX_LINES * wh) // MASK_CELL
    x0, y0, x1, _ = (int(v) // MASK_CELL for v in box)
    y1 = int(box[1] + FIRST_LINE_LINES * wh) // MASK_CELL
    band = slice(max(0, y0), min(ink.shape[0], y1 + 1))
    left = x0
    while left - 1 >= max(0, x0 - reach):
        hits = [x for x in range(max(0, left - gap), left) if ink[band, x].any()]
        if not hits:
            break
        left = min(hits)
    right = x1
    while right + 1 < min(ink.shape[1], x1 + reach):
        hits = [
            x for x in range(right + 1, min(ink.shape[1], right + 1 + gap)) if ink[band, x].any()
        ]
        if not hits:
            break
        right = max(hits)
    return [min(box[0], left * MASK_CELL), box[1], max(box[2], (right + 1) * MASK_CELL), box[3]]


SKEW_MIN_ANGLE = 0.003
SKEW_LINE_RESIDUAL = 0.6
SKEW_MIN_WORDS = 3


def polygon_share(mask, corners):
    canvas = Image.new("L", (mask.shape[1], mask.shape[0]), 0)
    ImageDraw.Draw(canvas).polygon([(x / MASK_CELL, y / MASK_CELL) for x, y in corners], fill=1)
    inside = np.asarray(canvas) > 0
    return mask[inside].mean() if inside.any() else 0.0


def skew_frame(words, wh, pad):
    words = [w for w in words if w["c"] >= WORD_CONF]
    if len(words) < SKEW_MIN_WORDS:
        return None
    cx = np.array([(w["x0"] + w["x1"]) / 2 for w in words])
    cy = np.array([(w["y0"] + w["y1"]) / 2 for w in words])
    slope, offset = np.polyfit(cx, cy, 1)
    if (
        np.abs(cy - (slope * cx + offset)).max() > SKEW_LINE_RESIDUAL * wh
        or abs(slope) < SKEW_MIN_ANGLE
    ):
        return None
    a = np.arctan(slope)
    c, s_ = np.cos(a), np.sin(a)
    px, py = cx.min(), slope * cx.min() + offset
    us, vs = [], []
    for w in words:
        for x, y in (
            (w["x0"], w["y0"]),
            (w["x1"], w["y0"]),
            (w["x0"], w["y1"]),
            (w["x1"], w["y1"]),
        ):
            us.append((x - px) * c + (y - py) * s_)
            vs.append(-(x - px) * s_ + (y - py) * c)
    u0, v0, u1, v1 = min(us) - pad, min(vs) - pad, max(us) + pad, max(vs) + pad
    corners = [
        (px + u * c - v * s_, py + u * s_ + v * c)
        for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))
    ]
    return {
        "angle": float(np.degrees(a)),
        "left": corners[0][0],
        "top": corners[0][1],
        "width": u1 - u0,
        "height": v1 - v0,
        "corners": corners,
    }


SKEW_GROW_LINES = 3
SKEW_GROW_STEP = 0.5


def tilted(frame, height):
    a = np.radians(frame["angle"])
    c, s_ = np.cos(a), np.sin(a)
    x, y = frame["left"], frame["top"]
    return [
        (x + u * c - v * s_, y + u * s_ + v * c)
        for u, v in ((0, 0), (frame["width"], 0), (frame["width"], height), (0, height))
    ]


def grow_skew_down(frame, blocked, wh):
    height = frame["height"]
    limit = frame["height"] + SKEW_GROW_LINES * wh
    while height + SKEW_GROW_STEP * wh <= limit:
        strip = tilted(frame, height + SKEW_GROW_STEP * wh)
        top = tilted(frame, height)
        strip = [top[3], top[2], strip[2], strip[3]]
        if polygon_share(blocked, strip) > 0:
            break
        height += SKEW_GROW_STEP * wh
    corners = tilted(frame, height)
    return {**frame, "height": height, "corners": corners}


ABSORB_CONF = 40
ABSORB_SHARE = 0.6
ABSORB_GAP_LINES = 2


def absorb_line_words(box, wh, source, words):
    target = grams(source)
    box = list(box)
    grown = True
    while grown:
        grown = False
        for w in words:
            g = grams(w["t"])
            if w["c"] < ABSORB_CONF or not g or len(g & target) / len(g) < ABSORB_SHARE:
                continue
            cx, cy = (w["x0"] + w["x1"]) / 2, (w["y0"] + w["y1"]) / 2
            inside = box[0] <= cx <= box[2] and box[1] <= cy <= box[3]
            near = (
                box[1] - 0.3 * wh <= cy <= box[3] + 1.3 * wh
                and w["x0"] <= box[2] + ABSORB_GAP_LINES * wh
                and w["x1"] >= box[0] - ABSORB_GAP_LINES * wh
            )
            if near and not inside:
                box = [
                    min(box[0], w["x0"]),
                    min(box[1], w["y0"]),
                    max(box[2], w["x1"]),
                    max(box[3], w["y1"]),
                ]
                grown = True
    return box


def on_photo(core, w):
    cx, cy = (w["x0"] + w["x1"]) / 2, (w["y0"] + w["y1"]) / 2
    return bool(core[cells([cx, cy, cx + 1, cy + 1])].any())


LEFTOVER_GAP_LINES = 3
LEFTOVER_MAX_HEIGHT = 1.5
HEAD_SIDE_LINES = 1
SIDE_GAP_LINES = 2


def on_same_line(a, b, wh):
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    gap = max(a[0], b[0]) - min(a[2], b[2])
    return overlap > 0.5 * min(a[3] - a[1], b[3] - b[1]) and gap < SIDE_GAP_LINES * wh


def touches(a, b, gap):
    return a[0] - gap <= b[2] and b[0] <= a[2] + gap and a[1] - gap <= b[3] and b[1] <= a[3] + gap


def reading_order(blocks, W):
    def wide(b):
        return b["box"][2] - b["box"][0] > 0.6 * W

    tops = [b["box"][1] for b in blocks if wide(b)]

    def key(b):
        x0, y0, x1, _ = b["box"]
        band = sum(t <= y0 for t in tops)
        if wide(b):
            return (band, 0, y0)
        return (band, 1 + int((x0 + x1) / 2 > W / 2), y0)

    return sorted(blocks, key=key)


def page_html(pid, folio=""):
    im = Image.open(os.path.join(BOOK, "pages", pid + ".jpg")).convert("RGB")
    W, H = im.size
    mm = PAGE_W_MM / W
    t = page_json.read(os.path.join(BOOK, "translation", pid + ".json"))
    if retypeset(pid, t):
        return retypeset_html(t)
    data = json.load(open(os.path.join(BLOCKS, pid + ".json"), encoding="utf-8"))
    grid = (H // MASK_CELL, W // MASK_CELL)
    photo, photo_core = photo_masks(pid, im, grid, data["blocks"])
    page_gray = np.asarray(im.convert("L")).astype(float)
    ink = load_mask(pid, "ink", grid)
    blocks = prepare(data["blocks"], photo_core)
    body_translation, notes = split_notes(strip_retelling_mark(t.get("translation")))
    zone = notes_zone(data["blocks"], H) if notes else None
    if zone:
        blocks = [b for b in blocks if inside_share(b["box"], zone) <= 0.5]
    targets = (
        split_paired([c for c in t.get("captions") or [] if c.get("translation")])
        + headings(t)
        + quotes(t)
        + running_head(t)
    )
    blocks = split_caption_blocks(blocks, targets)
    owner = match(blocks, targets)
    legend = []
    placed = {}
    for k, i in owner.items():
        placed.setdefault(i, []).append(blocks[k])
    for i, bs in list(placed.items()):
        keep = nearest_cluster(bs, QUOTE_GAP_LINES if targets[i].get("quote") else 2)
        placed[i] = keep
        for k in [k for k, j in owner.items() if j == i and blocks[k] not in keep]:
            del owner[k]
    taken = set(owner)
    for k, b in enumerate(blocks):
        if k in taken or b["rotated"]:
            continue
        for k2, i in list(owner.items()):
            c, m = targets[i], blocks[k2]
            same_line = (
                not c.get("quote")
                and len(b["src"]) < BODY_MIN_WORDS
                and abs((b["box"][1] + b["box"][3]) - (m["box"][1] + m["box"][3])) < 2 * m["wh"]
                and touches(m["box"], b["box"], 3 * m["wh"])
            )
            under_heading = (
                c.get("heading")
                and 0 <= b["box"][1] - m["box"][3] < 3 * m["wh"]
                and len(b["src"]) < BODY_MIN_WORDS
                and b["box"][0] < m["box"][2]
                and m["box"][0] < b["box"][2]
            )
            if same_line or under_heading:
                placed.setdefault(i, []).append(b)
                taken.add(k)
                break
    for i in [i for i in placed if targets[i].get("quote")]:
        grown = True
        while grown:
            grown = False
            bs = placed[i]
            box = [
                min(b["box"][0] for b in bs),
                min(b["box"][1] for b in bs),
                max(b["box"][2] for b in bs),
                max(b["box"][3] for b in bs),
            ]
            wh = statistics.median(b["wh"] for b in bs)
            for k, b in enumerate(blocks):
                if (
                    k not in taken
                    and not b["rotated"]
                    and touches(box, b["box"], QUOTE_GAP_LINES * wh)
                    and containment(b["text"], targets[i]["source"]) >= QUOTE_LINE_CONTAINMENT
                ):
                    bs.append(b)
                    taken.add(k)
                    grown = True
    free_blocks = [b for k, b in enumerate(blocks) if k not in taken]
    caption_text = " ".join(c["source"] for c in targets)
    body = [
        b
        for b in free_blocks
        if not b["rotated"]
        and len(b["src"]) >= BODY_MIN_WORDS
        and containment(b["text"], caption_text) < CAPTION_BODY_SHARE
    ]
    body = [
        b
        for b in body
        if not any(
            o is not b
            and area(o["box"]) > area(b["box"])
            and inside_share(b["box"], o["box"]) > 0.5
            for o in body
        )
    ]
    rest = [
        b
        for b in free_blocks
        if b not in body
        and not b["rotated"]
        and len(b["src"]) >= 2
        and not any(inside_share(b["box"], o["box"]) > 0.5 for o in body)
    ]
    merged_ids = set()
    for b in [b for b in free_blocks if b not in body and not b["rotated"]]:
        cx = (b["box"][0] + b["box"][2]) / 2
        near = [
            m
            for m in body
            if b["wh"] <= LEFTOVER_MAX_HEIGHT * m["wh"]
            and touches(m["box"], b["box"], LEFTOVER_GAP_LINES * min(b["wh"], m["wh"]))
            and m["box"][0] <= cx <= m["box"][2]
            and not any(
                o is not m
                and o["box"][0] <= cx <= o["box"][2]
                and touches(o["box"], b["box"], b["wh"])
                for o in body
            )
        ]
        near = near or [
            m
            for m in body
            if b["wh"] <= LEFTOVER_MAX_HEIGHT * m["wh"]
            and on_same_line(m["box"], b["box"], b["wh"])
        ]
        if near:
            merged_ids.add(id(b))
            m = near[0]
            m["box"] = [
                min(m["box"][0], b["box"][0]),
                min(m["box"][1], b["box"][1]),
                max(m["box"][2], b["box"][2]),
                max(m["box"][3], b["box"][3]),
            ]
            if b in rest:
                rest.remove(b)
    missing = [i for i in range(len(targets)) if i not in placed]
    if missing and len(missing) == len(rest):
        for i, b in zip(list(missing), sorted(rest, key=lambda b: (b["box"][1], b["box"][0]))):
            if containment(b["text"], targets[i]["source"]) >= PAIR_CONTAINMENT:
                placed[i] = [b]
                missing.remove(i)
    still = []
    for i in missing:
        if not has_source_words(targets[i]["source"]):
            continue
        found = find_words(
            data["blocks"], targets[i]["source"], across_blocks=bool(targets[i].get("heading"))
        )
        if found and any(
            inside_share(found["box"], b["box"]) > 0.5 for bs in placed.values() for b in bs
        ):
            found = None
        if found:
            placed[i] = [found]
        else:
            still.append(i)
    if still:
        placed_boxes = [b["box"] for bs in placed.values() for b in bs] + [b["box"] for b in blocks]
        loose = [
            w
            for w in rotated_words(pid, im, (0,))
            if not any(
                r[0] <= (w["x0"] + w["x1"]) / 2 <= r[2] and r[1] <= (w["y0"] + w["y1"]) / 2 <= r[3]
                for r in placed_boxes
            )
        ]
        upright = {"rotated": False, "words": loose, "x0": 0, "y0": 0, "x1": W, "y1": H}
        turned = [
            {"rotated": True, "words": rotated_words(pid, im), "x0": 0, "y0": 0, "x1": W, "y1": H}
        ]
        for i in list(still):
            found = find_words([upright], targets[i]["source"])
            if found and not any(
                inside_share(found["box"], b["box"]) > 0.5 for bs in placed.values() for b in bs
            ):
                placed[i] = [found]
                still.remove(i)
                upright["words"] = [
                    w for w in upright["words"] if all(w is not u for u in found["src"])
                ]
                continue
            found = find_words(turned, targets[i]["source"])
            if found and not any(
                inside_share(found["box"], b["box"]) > 0.5 for bs in placed.values() for b in bs
            ):
                placed[i] = [found]
                still.remove(i)
                turned[0]["words"] = [
                    w for w in turned[0]["words"] if all(w is not u for u in found["src"])
                ]
    for i in still:
        if not targets[i].get("heading"):
            legend.append(targets[i])
    translation = body_translation if zone else strip_retelling_mark(t.get("translation"))
    for i in placed:
        c = targets[i]
        if c.get("heading"):
            translation = "\n".join(
                line
                for line in translation.split("\n")
                if not (line.startswith("#") and line.lstrip("#").strip() == c["translation"])
            )
        if c.get("quote") or c.get("running"):
            translation = translation.replace(c["translation"].split("\n")[0], "", 1)
            for extra in c["translation"].split("\n")[1:]:
                translation = translation.replace(extra, "", 1)
    if t.get("kind") == "cover" and translation.strip():
        legend.append({"translation": translation.strip(), "source": ""})
        body = []
    if not translation.strip():
        body = []

    def pad(b):
        return max(3, int(b["wh"] * 0.3))

    def padded(b, side=0):
        p = pad(b)
        x0, y0, x1, y1 = b["box"]
        return [max(0, x0 - p - side), max(0, y0 - p), min(W, x1 + p + side), min(H, y1 + p)]

    colour = colour_mask(im, grid)

    def safe(box, b):
        box = off_dark_photo(box, b)
        if box is None or photo_share(colour, box) == 0:
            return box
        shrunk = shrink_off_photo(colour, box, b["src"], tolerance=0)
        if shrunk is None:
            return box
        b["erase"] = [
            w
            for w in b.get("words") or b["src"]
            if not (
                shrunk[0] <= w["x0"]
                and w["x1"] <= shrunk[2]
                and shrunk[1] <= w["y0"]
                and w["y1"] <= shrunk[3]
            )
            and inside_share([w["x0"], w["y0"], w["x1"], w["y1"]], box) > 0.5
        ]
        return shrunk

    def off_dark_photo(box, b):
        if photo_share(photo_core, box) <= PHOTO_TOLERANCE - 0.01:
            return box
        off_photo = [w for w in b["src"] if not on_photo(photo, w)] or [
            w for w in b["src"] if not on_photo(photo_core, w)
        ]
        if off_photo and len(off_photo) < len(b["src"]):
            b["src"] = off_photo
            b["box"] = box_of(off_photo)
            p = pad(b)
            box = [
                max(0, b["box"][0] - p),
                max(0, b["box"][1] - p),
                min(W, b["box"][2] + p),
                min(H, b["box"][3] + p),
            ]
            if photo_share(photo_core, box) <= PHOTO_TOLERANCE - 0.01:
                return box
        tight = b["box"]
        if photo_share(photo_core, tight) <= PHOTO_TOLERANCE - 0.01:
            return tight
        return shrink_off_photo(photo_core, tight, b["src"])

    shapes = []
    for i, bs in placed.items():
        bs = sorted(bs, key=lambda b: b["box"][1])
        union = [
            min(b["box"][0] for b in bs),
            min(b["box"][1] for b in bs),
            max(b["box"][2] for b in bs),
            max(b["box"][3] for b in bs),
        ]
        merged = {
            **bs[0],
            "box": union,
            "src": [w for b in bs for w in b["src"]],
            "words": [w for b in bs for w in b.get("words", [])],
            "x0": min(b.get("x0", union[0]) for b in bs),
            "y0": min(b.get("y0", union[1]) for b in bs),
            "x1": max(b.get("x1", union[2]) for b in bs),
            "y1": max(b.get("y1", union[3]) for b in bs),
        }
        if (
            not merged["rotated"]
            and not targets[i].get("quote")
            and t.get("kind") != "pattern"
            and not on_grid(page_gray, merged["box"], int(merged["wh"]))
        ):
            free_ink = ink & ~photo
            for o in blocks:
                if all(o is not b for b in bs):
                    free_ink[cells(o["box"])] = False
            merged["box"] = extend_along_ink(free_ink, merged["box"], merged["wh"])
        if t.get("kind") == "pattern" and not merged["rotated"] and not targets[i].get("heading"):
            merged["box"] = absorb_line_words(
                merged["box"], merged["wh"], targets[i]["source"], rotated_words(pid, im, (0,))
            )
        box = safe(
            padded(merged, HEAD_SIDE_LINES * merged["wh"] if targets[i].get("heading") else 0),
            merged,
        )
        if box is None and not merged["rotated"] and not targets[i].get("quote"):
            frame = skew_frame(
                [w for w in merged["src"] if not on_photo(photo, w)], merged["wh"], pad(merged)
            )
            if frame and polygon_share(photo_core, frame["corners"]) <= PHOTO_TOLERANCE - 0.01:
                frame = grow_skew_down(frame, ink | photo, merged["wh"])
                merged["skew"] = frame
                xs, ys = [x for x, _ in frame["corners"]], [y for _, y in frame["corners"]]
                box = [min(xs), min(ys), max(xs), max(ys)]
        if box is None:
            legend.append(targets[i])
            continue
        shapes.append(("head" if targets[i].get("heading") else "cap", box, merged, targets[i]))
    if zone:
        zone_words = [
            w
            for b in data["blocks"]
            if inside_share([b["x0"], b["y0"], b["x1"], b["y1"]], zone) > 0.5
            for w in b.get("words", [])
            if w["c"] >= 60
        ]
        wh = statistics.median([w["y1"] - w["y0"] for w in zone_words] or [16])
        zb = {
            "box": zone,
            "wh": wh,
            "rotated": False,
            "src": zone_words or [{"x0": zone[0], "y0": zone[1], "x1": zone[2], "y1": zone[3]}],
            "x0": zone[0],
            "y0": zone[1],
            "x1": zone[2],
            "y1": zone[3],
        }
        box = safe(padded(zb), zb)
        if box is not None:
            shapes.append(("notes", box, zb, {"translation": "", "source": "", "notes": notes}))
    regions = []
    for b in reading_order(body, W):
        box = safe(padded(b), b)
        if box is not None:
            regions.append((box, b))
    all_boxes = separate([box for _, box, _, _ in shapes] + [box for box, _ in regions])
    shapes = [
        (k, box if m.get("skew") else nb, m, c) for (k, box, m, c), nb in zip(shapes, all_boxes)
    ]
    regions = [(nb, b) for (_, b), nb in zip(regions, all_boxes[len(shapes) :])]
    obstacle = ink | photo | not_paper(im, grid)
    for box in all_boxes:
        obstacle[cells(box)] = True
    grown_regions = []
    for box, b in regions:
        own = obstacle.copy()
        own[cells(box)] = False
        g = free_rect(own, box, W, H, "rd")
        b["h0"] = (box[3] - g[1]) * mm
        grown_regions.append((g, b))
        obstacle[cells(g)] = True
    regions = grown_regions
    used = {id(b) for bs in placed.values() for b in bs} | {id(b) for b in body} | merged_ids
    taken_rects = all_boxes + [g for g, _ in regions]
    blanks = []
    for b in free_blocks:
        if (
            id(b) in used
            or b["rotated"]
            or b["box"][3] < RUNNING_HEAD_ZONE * H
            or not is_leftover_text(b, t.get("source") or "")
        ):
            continue
        box = padded(b)
        if photo_share(photo_core, box) > PHOTO_TOLERANCE - 0.01 or any(
            inside_share(box, r) > 0 for r in taken_rects + blanks
        ):
            continue
        blanks.append(box)
        obstacle[cells(box)] = True
    cap_font = (
        statistics.median([m["wh"] for k, _, m, _ in shapes if k == "cap"] or [18])
        * mm
        * FONT_PER_WORD_HEIGHT
    )
    out = []
    gray = np.asarray(im.convert("L")).astype(float)

    def div(cls, box, inner, extra="", paint=None, b=None):
        x0, y0, x1, y1 = box
        background = paper_color(im, paint or box, 10)
        if b is not None and b.get("erase"):
            erase_words(im, b["erase"], [0, 0, W, H], LETTER_ERASE_DEPTH)
        if (
            b is not None
            and not b.get("skew")
            and (t.get("kind") == "pattern" or on_grid(gray, box, int(b["wh"])))
        ):
            erase_words(im, b.get("words") or b["src"], box)
            erase_letters(im, box, b["wh"], photo)
            cls, background = cls + " ongrid", "transparent"
        return (
            f'<div class="ov {cls}" style="left:{x0 * mm:.2f}mm;top:{y0 * mm:.2f}mm;width:{(x1 - x0) * mm:.2f}mm;'
            f'height:{(y1 - y0) * mm:.2f}mm;background:{background};{extra}">{inner}</div>'
        )

    for kind, box, b, c in shapes:
        if b.get("skew"):
            sk = b["skew"]
            box = [sk["left"], sk["top"], sk["left"] + sk["width"], sk["top"] + sk["height"]]
        own = obstacle.copy()
        halo = int(0.6 * b["wh"])
        raw = [
            min(box[0], b.get("x0", box[0])),
            min(box[1], b.get("y0", box[1])),
            max(box[2], b.get("x1", box[2])),
            max(box[3], b.get("y1", box[3])),
        ]
        around = cells([raw[0] - halo, raw[1] - halo, raw[2] + halo, raw[3] + halo])
        own[around] = photo[around]
        fr = free_rect(own, box, W, H)
        f = (
            b["wh"] * mm * FONT_PER_WORD_HEIGHT
            if kind in ("head", "notes") or c.get("quote")
            else cap_font
        )
        extra = (
            f"--f:{f:.2f}mm;--fx0:{fr[0] * mm:.2f}mm;--fy0:{fr[1] * mm:.2f}mm;--fx1:{fr[2] * mm:.2f}mm;"
            f"--fy1:{fr[3] * mm:.2f}mm"
        )
        if b["rotated"]:
            extra += ";writing-mode:vertical-rl;transform:rotate(180deg)"
        if b.get("skew"):
            extra += f";transform:rotate({b['skew']['angle']:.3f}deg);transform-origin:0 0"
            kind += " skew"
        cls = (
            ("cap notes" if kind == "notes" else kind)
            + (" quote" if c.get("quote") else "")
            + (" rot" if b["rotated"] else "")
        )
        inner = (
            notes_html(c["notes"])
            if kind == "notes"
            else "<br>".join(
                html.escape(without_source_glosses(x)) for x in c["translation"].split("\n")
            )
        )
        short = without_source_glosses(c["translation"]).replace("\n", " ")
        out.append(
            div(
                cls,
                box,
                inner,
                extra
                + f'" data-source="{html.escape(c["source"], quote=True)}" data-translation="{html.escape(c["translation"], quote=True)}" data-short="{html.escape(short, quote=True)}',
                b=b,
            )
        )
    for box in blanks:
        out.append(div("blank", box, ""))
    for k, sp in enumerate(spare_rects(obstacle, W, H, mm)):
        out.append(
            f'<div class="spare" data-k="{k}" style="left:{sp[0] * mm:.2f}mm;top:{sp[1] * mm:.2f}mm;'
            f'width:{(sp[2] - sp[0]) * mm:.2f}mm;height:{(sp[3] - sp[1]) * mm:.2f}mm;background:{paper_color(im, sp, 10)}"></div>'
        )
    body_font = statistics.median([b["wh"] for _, b in regions] or [18]) * mm * FONT_PER_WORD_HEIGHT
    for k, (box, b) in enumerate(regions):
        out.append(
            div(
                "body",
                box,
                "",
                f'--f:{body_font:.2f}mm;--h0:{b.get("h0", 0):.2f}mm" data-region="{k}',
                b=b,
            )
        )
    items = json.dumps(body_items(translation) if regions else [], ensure_ascii=False)
    orphans = json.dumps(
        [html.escape(without_source_glosses(c["translation"])) for c in legend], ensure_ascii=False
    )
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=74, optimize=True)
    src = base64.b64encode(buf.getvalue()).decode()
    return (
        f"<section class=\"page\" data-items='{html.escape(items, quote=True)}' data-orphans='{html.escape(orphans, quote=True)}' "
        f'data-pid="{html.escape(pid, quote=True)}"><img src="data:image/jpeg;base64,{src}" style="width:{PAGE_W_MM}mm">{"".join(out)}</section>'
    )


PAGE_CSS = """
@page { size: %(w)smm %(h)smm; margin: 0; }
body { margin: 0; width: %(w)smm; }
.page { position: relative; width: %(w)smm; height: %(h)smm; overflow: hidden; break-after: page; background: #fff; }
"""

CSS = """
.page img { display: block; }
.ov { position: absolute; overflow: hidden; color: #1d1a17; font-family: 'PT Serif', 'Noto Serif', 'DejaVu Serif', serif;
      hyphens: auto; text-align: left; box-sizing: border-box; padding: .3mm .5mm; line-height: 1.22; }
.ov p { margin: 0; }
.ov p + p { text-indent: 1.2em; }
.ov p.cont { text-indent: 0; }
.ov h3 { margin: 0 0 .3em; font-size: 1.35em; line-height: 1.15; font-weight: 700; }
.cap { font-style: italic; }
.ongrid { text-shadow: 0 0 .4mm #fff, 0 0 .4mm #fff, 0 0 .8mm #fff; }
.notes { font-style: normal; line-height: 1.15; }
.notes .fn { padding-left: 1.5em; text-indent: -1.5em; }
.notes .fn b { display: inline-block; min-width: 1.5em; text-indent: 0; font-weight: 700; }
.spare { position: absolute; overflow: hidden; box-sizing: border-box; padding: .5mm .8mm; font-family: 'PT Serif', 'Noto Serif', serif;
         font-style: italic; color: #1d1a17; line-height: 1.22; visibility: hidden; }
.spare.used { visibility: visible; }
.spare p { margin: 0 0 .4em; }
.spare h3 { font-style: normal; margin: 0 0 .3em; }
.see { font-style: normal; color: #8a5a3c; font-size: .85em; }
.quote { font-style: italic; text-align: center; }
.head { font-weight: 700; white-space: nowrap; }
.legend { background: #fbf8f2; padding: 16mm 14mm; box-sizing: border-box; font: 3.4mm/1.4 'PT Serif', 'Noto Serif', serif; color: #1d1a17; }
.legend h2 { font-size: 5mm; margin: 0 0 2mm; }
.legend .why { color: #7a6f60; font-size: 3mm; margin: 0 0 4mm; }
.legend p { margin: 0 0 2.5mm; }
.retype { background: #fbf8f2; }
.retype .flow { position: absolute; left: 16mm; right: 14mm; top: 16mm; bottom: 20mm; overflow: hidden; column-gap: 6mm;
                font-family: 'PT Serif', 'Noto Serif', 'DejaVu Serif', serif; color: #1d1a17; hyphens: auto; }
.retype .flow p { margin: 0 0 .45em; }
.retype .flow h2 { font-size: 1.4em; margin: 0 0 .6em; column-span: all; }
.retype .folio { position: absolute; bottom: 10mm; left: 0; right: 0; text-align: center; font: 3mm 'PT Serif', serif; color: #555; }
"""

FIT_JS = r"""
function overflows(el) { return el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1; }
const css = (el, name) => parseFloat(getComputedStyle(el).getPropertyValue(name));
let mark = 0, dropped = 0;
function centre(el) { const r = el.getBoundingClientRect(); return [(r.left + r.right) / 2, (r.top + r.bottom) / 2]; }
function toSpare(page, html, f, near) {
  const spares = [...page.querySelectorAll('.spare')];
  if (near) { const [x, y] = centre(near); spares.sort((a, b) => { const [ax, ay] = centre(a), [bx, by] = centre(b); return Math.hypot(ax - x, ay - y) - Math.hypot(bx - x, by - y); }); }
  for (const sp of spares) {
    if (clashes(sp, page)) continue;
    const before = sp.innerHTML;
    sp.classList.add('used');
    sp.insertAdjacentHTML('beforeend', '<p>' + html + '</p>');
    for (let s = f; s >= 0.6 * f; s -= 0.05 * f) { sp.style.fontSize = s + 'mm'; if (!overflows(sp)) return true; }
    sp.innerHTML = before;
    if (!before) sp.classList.remove('used');
  }
  return false;
}
function fitWords(el, words, suffix) {
  let lo = 0, hi = words.length;
  while (lo < hi) { const m = Math.ceil((lo + hi) / 2); el.innerHTML = words.slice(0, m).join(' ') + (m < words.length ? '…' : '') + suffix; if (overflows(el)) hi = m - 1; else lo = m; }
  el.innerHTML = words.slice(0, lo).join(' ') + (lo < words.length ? '…' : '') + suffix;
  return lo;
}
function escapeHtml(text) { return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function fallback(el, page, best) {
  const full = escapeHtml(el.dataset.short || el.textContent);
  const f0 = css(el, '--f') || 2.5;
  const star = '*'.repeat(++mark % 4 || 4);
  const head = el.classList.contains('head') || el.classList.contains('notes');
  if (head) {
    best();
    for (let f = 0.45 * f0; f >= 0.3 * f0; f -= 0.03 * f0) { el.style.fontSize = f + 'mm'; if (!overflows(el)) return; }
  }
  if (!head && !el.classList.contains('rot') && !el.classList.contains('skew') && toSpare(page, '<sup>' + star + '</sup> ' + full, Math.min(f0, 2.6), el)) {
    el.style.fontSize = (0.8 * f0) + 'mm';
    fitWords(el, full.split(' '), ' <sup>' + star + '</sup>');
    return;
  }
  best();
  for (let f = 0.6 * f0; f >= 0.4 * f0; f -= 0.05 * f0) { el.style.fontSize = f + 'mm'; if (!overflows(el)) return; }
  const kept = fitWords(el, full.split(' '), '');
  if (kept < full.split(' ').length) dropped++;
  if (kept === 0 || clashes(el, page)) el.remove();
}
for (const flow of document.querySelectorAll('.retype .flow')) {
  let f = 3.6;
  flow.style.fontSize = f + 'mm';
  while (overflows(flow) && f > 2.4) { f -= 0.1; flow.style.fontSize = f + 'mm'; }
  if (overflows(flow)) { flow.style.columnCount = 2; f = 3.2; flow.style.fontSize = f + 'mm'; while (overflows(flow) && f > 1.6) { f -= 0.1; flow.style.fontSize = f + 'mm'; } }
}
function clashes(el, page) {
  const r = el.getBoundingClientRect();
  for (const o of page.querySelectorAll('.ov, .spare.used')) {
    if (o === el) continue;
    const q = o.getBoundingClientRect();
    if (Math.min(r.right, q.right) - Math.max(r.left, q.left) > 1 && Math.min(r.bottom, q.bottom) - Math.max(r.top, q.top) > 1) return true;
  }
  return false;
}
function fitLabel(el, page) {
  const f0 = css(el, '--f');
  const fx0 = css(el, '--fx0'), fy0 = css(el, '--fy0'), fx1 = css(el, '--fx1'), fy1 = css(el, '--fy1');
  const x0 = parseFloat(el.style.left), y0 = parseFloat(el.style.top), w0 = parseFloat(el.style.width), h0 = parseFloat(el.style.height);
  const rot = el.classList.contains('rot') || el.classList.contains('skew');
  const set = (l, t, w, h, wrap) => { el.style.left = l + 'mm'; el.style.top = t + 'mm'; el.style.width = w + 'mm'; el.style.height = h + 'mm';
    el.style.whiteSpace = el.classList.contains('head') && !wrap ? 'nowrap' : 'normal'; };
  const ok = () => !overflows(el) && !clashes(el, page);
  const layouts = rot ? [[x0, y0, w0, h0, 1]] : [
    [x0, y0, w0, h0, 0], [x0, y0, fx1 - x0, h0, 0], [x0, y0, fx1 - x0, fy1 - y0, 1], [fx0, y0, fx1 - fx0, fy1 - y0, 1],
    [fx0, fy0, fx1 - fx0, fy1 - fy0, 1], [x0, y0, w0, fy1 - y0, 1], [x0, fy0, fx1 - x0, fy1 - fy0, 1],
    [fx0, fy0, x0 + w0 - fx0, fy1 - fy0, 1]];
  const fmin = el.classList.contains('head') ? 0.45 * f0 : 0.6 * f0;
  for (let f = f0; f >= fmin; f -= 0.05 * f0) {
    el.style.fontSize = f + 'mm';
    for (const [l, t, w, h, wrap] of layouts) { set(l, t, w, h, wrap); if (ok()) return; }
  }
  const best = () => {
    for (const [l, t, w, h, wrap] of layouts.slice().reverse()) { set(l, t, w, h, 1); if (!clashes(el, page)) return; }
    set(x0, y0, w0, h0, 1);
  };
  el.style.fontSize = f0 + 'mm';
  set(x0, y0, w0, h0, 1);
  fallback(el, page, best);
}
function render(el, items, from, to) {
  let h = '', n = 0;
  for (let i = 0; i < items.length && n < to; i++) {
    const [kind, text] = items[i];
    const ws = text.split(' ');
    const start = Math.max(0, from - n), stop = Math.min(ws.length, to - n);
    if (stop > start) {
      const part = ws.slice(start, stop).join(' ');
      if (kind === 'h') h += '<h3>' + part + '</h3>';
      else h += '<p' + (start > 0 ? ' class="cont"' : '') + '>' + part + '</p>';
    }
    n += ws.length;
  }
  el.innerHTML = h;
}
function textFrom(items, from) {
  let out = [], n = 0;
  for (const [kind, text] of items) {
    const ws = text.split(' ');
    if (n + ws.length > from) out.push((kind === 'h' ? '<h3>' : '<p>') + ws.slice(Math.max(0, from - n)).join(' ') + (kind === 'h' ? '</h3>' : '</p>'));
    n += ws.length;
  }
  return out.join('');
}
for (const page of document.querySelectorAll('.page[data-pid]')) {
  for (const el of page.querySelectorAll('.head')) fitLabel(el, page);
  for (const el of page.querySelectorAll('.cap')) fitLabel(el, page);
  const items = JSON.parse(page.dataset.items);
  const regions = [...page.querySelectorAll('.body')].sort((a, b) => a.dataset.region - b.dataset.region);
  if (items.length && regions.length) {
    const total = items.reduce((s, [, t]) => s + t.split(' ').length, 0);
    const f0 = css(regions[0], '--f');
    const fill = f => {
      let pos = 0;
      for (const r of regions) {
        r.style.fontSize = f + 'mm';
        let lo = pos, hi = total;
        while (lo < hi) { const m = Math.ceil((lo + hi) / 2); render(r, items, pos, m); if (overflows(r)) hi = m - 1; else lo = m; }
        render(r, items, pos, lo);
        pos = lo;
      }
      return pos;
    };
    let lo = 0.6 * f0, hi = 1.5 * f0;
    if (fill(lo) >= total) {
      for (let k = 0; k < 12; k++) { const mid = (lo + hi) / 2; if (fill(mid) >= total) lo = mid; else hi = mid; }
    }
    let pos = fill(lo);
    const tighten = () => { for (const r of regions) { const h = r.style.height; r.style.height = 'auto'; const need = r.scrollHeight; r.style.height = h;
      const floor = css(r, '--h0') * 96 / 25.4;
      const want = Math.max(need, floor);
      if (want + 2 < r.clientHeight) r.style.height = (want * 25.4 / 96) + 'mm'; } };
    if (pos < total && !toSpare(page, textFrom(items, pos), lo, regions[regions.length - 1])) {
      let f = lo;
      while (pos < total && f > 0.45 * f0) { f -= 0.03 * f0; pos = fill(f); }
      if (pos < total) dropped++;
    }
    tighten();
  }
  for (const text of JSON.parse(page.dataset.orphans || '[]')) if (!toSpare(page, text, 2.4, null)) dropped++;
}
document.title = 'dropped:' + dropped;
"""


def page_ids():
    return [
        os.path.basename(f)[:-4]
        for f in sorted(glob.glob(os.path.join(BOOK, "pages", "p-*.jpg")))
        if tools.PAGE_ID.fullmatch(os.path.basename(f)[:-4])
    ]


def page_height_mm(pids):
    """Высота страницы: из конфига или по медианной пропорции сканов."""
    if PAGE_H_MM:
        return PAGE_H_MM
    ratios = []
    for pid in pids:
        with Image.open(os.path.join(BOOK, "pages", pid + ".jpg")) as im:
            ratios.append(im.height / im.width)
    return round(PAGE_W_MM * statistics.median(ratios), 1)


def build_html(pids=None):
    """Собрать HTML книги (все страницы или `pids`) с подгоночным скриптом в `build/inplace.html` книги."""
    every = page_ids()
    chosen = tools.checked_page_ids(pids) if pids else every
    folios = printed_numbers(every)
    geometry = PAGE_CSS % {"w": f"{PAGE_W_MM:g}", "h": f"{page_height_mm(chosen):g}"}
    doc = (
        f'<!doctype html><html lang="{html.escape(LANG, quote=True)}"><head><meta charset="utf-8">'
        f"{tools.csp_meta(FIT_JS)}<style>{geometry}{CSS}</style></head><body>"
        + "".join(page_html(p, folios.get(p, "")) for p in chosen)
        + f"<script>{FIT_JS}</script></body></html>"
    )
    return tools.write_build_file(BUILD, "inplace.html", doc)


def run(pids=None, out=None, force=False):
    """Собрать PDF «перевод на странице»: все страницы или `pids`, в `out` или путь из настроек.

    Существующий PDF перезаписывается только с `force`.
    """
    path = build_html(pids)
    out = tools.print_pdf(tools.find_chrome(CHROME), path, out or OUT, force=force)
    print(f"{out}: {len(pids) if pids else len(page_ids())} стр.")
    return path
