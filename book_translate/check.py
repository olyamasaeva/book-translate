"""Проверка PDF «перевод на странице» по итоговой вёрстке после подгонки в браузере.

Chrome отдаёт DOM после скрипта подгонки (`--dump-dom`). По каждой плашке: доля фото под ней (по настоящему контуру
для наклонных), пустая ли, ужата ли сильнее 75%; по странице — пересечения плашек и сколько уверенно распознанных
слов оригинала осталось видно.
"""

import json
import math
import os
import re
from book_translate import inplace as V
from book_translate import tools

VISIBLE_SOURCE_CONF = 75
SHRUNK = 0.75


DOM = []


def final_dom(path):
    return tools.dump_dom(tools.find_chrome(V.CHROME), path)


def boxes_by_page(dom):
    pages = {}
    for sec in re.finditer(
        r'<section class="page" [^>]*data-pid="([^"]+)"[^>]*>(.*?)</section>', dom, re.S
    ):
        pid, inner = sec.group(1), sec.group(2)
        out = []
        for m in re.finditer(
            r'<div class="((?:ov [^"]*)|(?:spare used))" (?:data-k="\d+" )?style="([^"]*)"[^>]*>(.*?)</div>',
            inner,
            re.S,
        ):
            st = dict(
                re.findall(
                    r"(?<![-\w])(left|top|width|height|font-size|--f):\s*([\d.]+)mm", m.group(2)
                )
            )
            if all(k in st for k in ("left", "top", "width", "height")):
                vals = {k: float(v) for k, v in st.items()}
                angle = re.search(r"rotate\((-?[\d.]+)deg\)", m.group(2))
                if angle and "skew" in m.group(1):
                    vals["angle"] = float(angle.group(1))
                out.append((m.group(1), vals, re.sub("<[^>]+>", "", m.group(3)).strip()))
        pages[pid] = out
    return pages


def tilted_corners(r, angle):
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    w, h = r[2] - r[0], r[3] - r[1]
    return [
        (r[0] + u * c - v * s, r[1] + u * s + v * c) for u, v in ((0, 0), (w, 0), (w, h), (0, h))
    ]


def check(pid, boxes):
    from PIL import Image

    data = json.load(open(os.path.join(V.BLOCKS, pid + ".json"), encoding="utf-8"))
    W, H = data["size"]
    px = W / V.PAGE_W_MM
    grid = (H // V.MASK_CELL, W // V.MASK_CELL)
    im = Image.open(os.path.join(V.BOOK, "pages", pid + ".jpg")).convert("RGB")
    _, core = V.photo_masks(pid, im, grid, data["blocks"])
    rects, notes = [], []
    for cls, st, text in boxes:
        r = [
            st["left"] * px,
            st["top"] * px,
            (st["left"] + st["width"]) * px,
            (st["top"] + st["height"]) * px,
        ]
        if "angle" in st:
            corners = tilted_corners(r, st["angle"])
            xs, ys = [x for x, _ in corners], [y for _, y in corners]
            share = V.polygon_share(core, corners)
            r = [min(xs), min(ys), max(xs), max(ys)]
        else:
            share = V.photo_share(core, r)
        rects.append(r)
        if share > V.PHOTO_TOLERANCE:
            notes.append(f"{cls.split()[0]} на фото {share:.0%}")
        if not text.strip("*") and not cls.startswith(("body", "ov body", "ov blank")) and not text:
            notes.append(f"пустая {cls.split()[0]}")
        if "--f" in st and "font-size" in st and st["font-size"] < SHRUNK * st["--f"]:
            notes.append(f"{cls.split()[0]} ужата до {st['font-size'] / st['--f']:.0%}")
        if "в пояснениях" in text:
            notes.append(f"{cls.split()[0]} урезана")
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            ix = min(a[2], b[2]) - max(a[0], b[0])
            iy = min(a[3], b[3]) - max(a[1], b[1])
            if ix > 3 and iy > 3:
                notes.append("плашки пересекаются")
    visible = 0
    for b in V.prepare(data["blocks"]):
        for w in b["src"]:
            if w["c"] < VISIBLE_SOURCE_CONF:
                continue
            cx, cy = (w["x0"] + w["x1"]) / 2, (w["y0"] + w["y1"]) / 2
            if not any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in rects):
                visible += 1
    return notes, visible


def run(path=None):
    """Проверить собранный HTML (по умолчанию — `build/inplace.html` книги) и напечатать замечания."""
    path = path or V.BUILD / "inplace.html"
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} не найден — сначала соберите книгу командой inplace")
    DOM.clear()
    DOM.append(final_dom(path))
    pages = boxes_by_page(DOM[0])
    if not pages:
        raise tools.ToolError(f"в {path} нет ни одной проверяемой страницы")
    flagged = []
    for pid, boxes in sorted(pages.items()):
        notes, visible = check(pid, boxes)
        if notes or visible >= 3:
            flagged.append((pid, visible, notes))
    for pid, visible, notes in flagged:
        print(f"{pid}\tслов оригинала видно: {visible}\t" + "; ".join(notes))
    dropped = re.search(r"<title>dropped:(\d+)</title>", DOM[0])
    print(
        f"страниц проверено {len(pages)}, с замечаниями {len(flagged)}, не уместилось целиком {dropped.group(1) if dropped else '?'}"
    )
