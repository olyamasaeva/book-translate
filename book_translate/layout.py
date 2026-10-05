"""Раскладка страницы скана: блоки текста по форме букв, каждый блок распознан отдельно.

Чернила — пиксели заметно темнее локального фона. Фото — участки, где светлого фона мало. Пятна крупнее нескольких
высот буквы (линии чертежей, рамки) отбрасываются, буквы склеиваются в строки и блоки. Каждый блок распознаётся
отдельно (вырезка ×2, автоконтраст, tesseract `--psm 6`, повёрнутый — после поворота): целой страницей с чертежами
tesseract читает текст пятнами, а вырезку — чисто. Итог — `layout/blocks/<page>.json` и маски `.ink.png`, `.photo.png`.
"""

import json
import os
import tempfile
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

from book_translate import tools

PAGES = BLOCKS = TESS = OCR_LANG = None
TESS_ENV = {}


def configure(book):
    global PAGES, BLOCKS, TESS, TESS_ENV, OCR_LANG
    PAGES, BLOCKS, OCR_LANG = str(book.pages), str(book.blocks), book.ocr_lang
    TESS, TESS_ENV = book.tesseract_cmd(1)


OCR_PAD = 6
MASK_CELL = 4
INK_CONTRAST = 28
PAPER = 190
PHOTO_DENSITY = 0.55
PHOTO_WINDOW = 41
CHAR_MAX_H = 2.2
CHAR_MAX_W = 4.0
CHAR_MIN_AREA = 6
CHAR_MIN_FILL = 0.18
CHAR_MIN_STROKE = 3
BLOCK_MIN_CHARS = 3
ROTATED_ASPECT = 2.5
ROTATED_LETTER_RATIO = 0.9
BIG_CHAR_MAX = 6.0
BIG_CHAR_MIN = 2.2
BIG_CHAR_FILL = 0.25
BIG_MIN_LETTERS = 3
JOIN_X = 1.3
JOIN_Y = 1.7


def ink_and_photo(g):
    local = ndi.uniform_filter(g, 41)
    ink = (g < local - INK_CONTRAST) & (g < PAPER)
    dark_share = ndi.uniform_filter((g < PAPER).astype(np.float32), PHOTO_WINDOW)
    photo = ndi.binary_dilation(dark_share > PHOTO_DENSITY, iterations=6)
    return ink & ~photo, photo


def char_height(heights):
    h = heights[(heights >= 8) & (heights <= 60)]
    if not len(h):
        return 18.0
    counts = np.bincount(h.astype(int))
    smooth = np.convolve(counts, np.ones(5) / 5, mode="same")
    return float(np.argmax(smooth))


def rows_in(mask):
    prof = mask.any(axis=1)
    edges = np.flatnonzero(np.diff(np.concatenate([[0], prof.astype(int), [0]])))
    starts, ends = edges[::2], edges[1::2]
    return list(zip(starts, ends))


def text_blocks(im):
    g = np.asarray(im.convert("L")).astype(np.float32)
    ink, photo = ink_and_photo(g)
    text_blocks.ink = ink
    lab, n = ndi.label(ink, structure=np.ones((3, 3)))
    if not n:
        return [], photo
    sl = ndi.find_objects(lab)
    hs = np.array([s[0].stop - s[0].start for s in sl])
    ws = np.array([s[1].stop - s[1].start for s in sl])
    area = ndi.sum_labels(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    ch = char_height(hs[(area >= CHAR_MIN_AREA) & (ws < 60)])
    fill = area / np.maximum(1, hs * ws)
    keep = (
        (np.maximum(hs, ws) <= CHAR_MAX_W * ch)
        & (np.minimum(hs, ws) <= CHAR_MAX_H * ch)
        & (area >= CHAR_MIN_AREA)
        & (np.maximum(hs, ws) >= 0.25 * ch)
        & (fill >= CHAR_MIN_FILL)
        & (np.minimum(hs, ws) >= CHAR_MIN_STROKE)
    )
    chars = np.zeros(g.shape, bool)
    shape_of = np.zeros(g.shape, np.float32)
    for k in np.flatnonzero(keep):
        chars[sl[k]] = True
        cy, cx = (sl[k][0].start + sl[k][0].stop) // 2, (sl[k][1].start + sl[k][1].stop) // 2
        shape_of[cy, cx] = hs[k] / max(1, ws[k])
    joined = ndi.binary_dilation(
        chars, structure=np.ones((max(1, int(JOIN_Y * ch)), max(1, int(JOIN_X * ch))))
    )
    blab, bn = ndi.label(joined)
    blocks = []
    for k, s in enumerate(ndi.find_objects(blab), 1):
        inside = chars[s] & (blab[s] == k)
        nchars = ndi.label(inside)[1]
        if nchars < BLOCK_MIN_CHARS:
            continue
        ys, xs = np.nonzero(inside)
        y0, y1, x0, x1 = (
            s[0].start + ys.min(),
            s[0].start + ys.max() + 1,
            s[1].start + xs.min(),
            s[1].start + xs.max() + 1,
        )
        ratios = shape_of[y0:y1, x0:x1]
        ratios = ratios[ratios > 0]
        sideways = len(ratios) >= 6 and float(np.median(ratios)) < ROTATED_LETTER_RATIO
        rotated = (y1 - y0) > ROTATED_ASPECT * (x1 - x0) and sideways
        rows = rows_in(chars[y0:y1, x0:x1].T if rotated else chars[y0:y1, x0:x1])
        tall = [(a, b) for a, b in rows if b - a >= 0.5 * ch]
        centers = [(a + b) / 2 for a, b in tall]
        pitch = float(np.median(np.diff(centers))) if len(centers) > 1 else 1.6 * ch
        blocks.append(
            {
                "x0": int(x0),
                "y0": int(y0),
                "x1": int(x1),
                "y1": int(y1),
                "rows": int(max(1, len(tall))),
                "h": float(ch),
                "pitch": float(pitch),
                "chars": int(nchars),
                "rotated": bool(rotated),
            }
        )
    big = big_text_blocks(hs, ws, fill, area, sl, ch, g.shape, blocks)

    def inside(b, o, pad):
        return (
            o["x0"] - pad <= b["x0"]
            and b["x1"] <= o["x1"] + pad
            and o["y0"] - pad <= b["y0"]
            and b["y1"] <= o["y1"] + pad
        )

    def covered(o):
        a = (o["x1"] - o["x0"]) * (o["y1"] - o["y0"])
        for b in blocks:
            ix = max(0, min(o["x1"], b["x1"]) - max(o["x0"], b["x0"]))
            iy = max(0, min(o["y1"], b["y1"]) - max(o["y0"], b["y0"]))
            if b["chars"] > o["chars"] and ix * iy > 0.5 * a:
                return True
        return False

    big = [o for o in big if not covered(o)]
    blocks = [b for b in blocks if not any(inside(b, o, 0.4 * o["h"]) for o in big)] + big
    return blocks, photo


def big_text_blocks(hs, ws, fill, area, sl, ch, shape, small_blocks):
    aspect = hs / np.maximum(1, ws)
    big = (
        (hs > BIG_CHAR_MIN * ch)
        & (hs <= BIG_CHAR_MAX * ch)
        & (ws <= BIG_CHAR_MAX * ch)
        & (fill >= BIG_CHAR_FILL)
        & (aspect >= 0.4)
        & (aspect <= 5)
        & (np.minimum(hs, ws) >= CHAR_MIN_STROKE)
    )
    idx = np.flatnonzero(big)
    if len(idx) < BIG_MIN_LETTERS:
        return []
    bh = float(np.median(hs[idx]))
    mask = np.zeros(shape, bool)
    for k in idx:
        mask[sl[k]] = True
    joined = ndi.binary_dilation(
        mask, structure=np.ones((max(1, int(0.5 * bh)), max(1, int(1.4 * bh))))
    )
    lab, n = ndi.label(joined)
    out = []
    for k, s in enumerate(ndi.find_objects(lab), 1):
        inside = mask[s] & (lab[s] == k)
        letters_n = ndi.label(inside)[1]
        if letters_n < BIG_MIN_LETTERS:
            continue
        ys, xs = np.nonzero(inside)
        y0, y1, x0, x1 = (
            s[0].start + ys.min(),
            s[0].start + ys.max() + 1,
            s[1].start + xs.min(),
            s[1].start + xs.max() + 1,
        )
        if (x1 - x0) < 1.5 * (y1 - y0):
            continue
        out.append(
            {
                "x0": int(x0),
                "y0": int(y0),
                "x1": int(x1),
                "y1": int(y1),
                "rows": 1,
                "h": bh,
                "pitch": 1.6 * bh,
                "chars": int(letters_n),
                "rotated": False,
            }
        )
    return out


def read_block(im, b):
    import csv
    import io
    from PIL import ImageOps

    ox, oy = max(0, b["x0"] - OCR_PAD), max(0, b["y0"] - OCR_PAD)
    crop = im.crop((ox, oy, b["x1"] + OCR_PAD, b["y1"] + OCR_PAD)).convert("L")
    cw, chh = crop.size
    if b["rotated"]:
        crop = crop.rotate(-90, expand=True)
    crop = ImageOps.autocontrast(
        crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS), cutoff=1
    )
    tsv = tools.ocr_tsv(TESS, TESS_ENV, crop, OCR_LANG, 6)
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        t = (row.get("text") or "").strip()
        if row.get("level") != "5" or not t:
            continue
        u0, v0 = int(row["left"]) / 2, int(row["top"]) / 2
        u1, v1 = u0 + int(row["width"]) / 2, v0 + int(row["height"]) / 2
        if b["rotated"]:
            x0, x1, y0, y1 = v0, v1, chh - u1, chh - u0
        else:
            x0, x1, y0, y1 = u0, u1, v0, v1
        words.append(
            {
                "t": t,
                "c": round(float(row["conf"]), 1),
                "x0": int(ox + x0),
                "y0": int(oy + y0),
                "x1": int(ox + x1),
                "y1": int(oy + y1),
            }
        )
    return words


def save_mask(mask, path):
    H, W = mask.shape[0] // MASK_CELL, mask.shape[1] // MASK_CELL
    cells = (
        mask[: H * MASK_CELL, : W * MASK_CELL].reshape(H, MASK_CELL, W, MASK_CELL).any(axis=(1, 3))
    )
    Image.fromarray((cells * 255).astype(np.uint8)).save(path)


def page_blocks(pid):
    out = os.path.join(BLOCKS, pid + ".json")
    if os.path.exists(out) and os.path.exists(os.path.join(BLOCKS, pid + ".photo.png")):
        return json.load(open(out, encoding="utf-8"))
    im = Image.open(os.path.join(PAGES, pid + ".jpg")).convert("RGB")
    blocks, _ = text_blocks(im)
    for b in blocks:
        b["words"] = (
            read_block(im, b) if b["chars"] >= 4 or (b["x1"] - b["x0"]) >= 2 * b["h"] else []
        )
        b["text"] = " ".join(w["t"] for w in b["words"])
    os.makedirs(BLOCKS, exist_ok=True)
    save_mask(text_blocks.ink, os.path.join(BLOCKS, pid + ".ink.png"))
    save_mask(_, os.path.join(BLOCKS, pid + ".photo.png"))
    json.dump(
        {"size": im.size, "blocks": blocks}, open(out, "w", encoding="utf-8"), ensure_ascii=False
    )
    return json.load(open(out, encoding="utf-8"))


def debug(pid, out=None):
    out = out or tempfile.mkdtemp(prefix="book-translate-layout-")
    os.makedirs(out, exist_ok=True)
    im = Image.open(os.path.join(PAGES, pid + ".jpg")).convert("RGB")
    blocks, photo = text_blocks(im)
    d = ImageDraw.Draw(im)
    for b in blocks:
        d.rectangle((b["x0"], b["y0"], b["x1"], b["y1"]), outline=(220, 0, 0), width=3)
        d.text(
            (b["x0"], b["y0"] - 12), f"{b['rows']}r{'R' if b['rotated'] else ''}", fill=(220, 0, 0)
        )
    im.thumbnail((600, 800))
    im.save(os.path.join(out, pid + ".png"))
    return blocks


def init_worker(book):
    configure(book)


def run(book, pids=None, jobs=2):
    """Разложить страницы (все или `pids`) в `jobs` процессов; готовые берутся из кеша."""
    from concurrent.futures import ProcessPoolExecutor

    configure(book)
    pids = (
        tools.checked_page_ids(pids)
        if pids
        else sorted(
            f[:-4]
            for f in os.listdir(PAGES)
            if f.endswith(".jpg") and tools.PAGE_ID.fullmatch(f[:-4])
        )
    )
    with ProcessPoolExecutor(jobs, initializer=init_worker, initargs=(book,)) as ex:
        for pid, d in zip(pids, ex.map(page_blocks, pids)):
            print(pid, len(d["blocks"]), flush=True)
