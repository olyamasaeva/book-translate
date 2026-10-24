---
name: book-translate
description: Translate a scanned book (PDF or page images without a text layer) from one language to another and build a bilingual PDF (scan left, translation right), an in-place PDF (the original text on each page replaced by the translation in the same layout, photos and drawings left uncovered) and an offline HTML reader with the original and the translation side by side. Use when the user asks to translate a book, a scanned PDF, a chapter or an old illustrated publication; "translate this book", "translate the scan", "in-place translation", "bilingual PDF", "переведи книгу", "перевод прямо на странице", "/book-translate".
---

# book-translate

You translate the pages by reading the page images. The `book-translate` CLI in this
repository does everything else: splitting the scan into pages, finding text blocks,
laying the translation out on the page, building PDFs and the reader, and checking the
result. OCR is used only to find where text sits on the page, never as the source of the
translation.

`$BT` below is the directory of this skill (usually `~/.claude/skills/book-translate`).
Run the CLI as `uv run --project $BT book-translate …`. Keep each book in its own folder
outside the skill directory, for example `~/books/<book>/`.

Before starting, ask the user whether they may translate this book: public domain, their
own text, the author's permission, or a translation for personal use. Never publish or
commit scans or translations of copyrighted books.

## 0. Setup (once)

```bash
cd $BT && uv sync
```

System programs: poppler (`pdftoppm`, `pdftotext`), Tesseract 5 with the source
language, Chrome or Chromium, Ghostscript. If one is missing, the CLI stops and names it.

## 1. Book config and pages

```bash
mkdir -p ~/books/<book> && cp $BT/conf/book.example.yaml ~/books/<book>/book.yaml
```

Set the language fields (`ocr_lang`, `source_lower`, `source_upper`, `target_letters`,
`target_lang`, `notes_headings`) and `spreads`. Phone photos and single-page scans need
`spreads: never`; two-page spreads need `auto` or `always`. All fields are described in
the repository README.

```bash
uv run --project $BT book-translate pages --book ~/books/<book>/book.yaml book.pdf
```

The command reports whether the PDF has a text layer. Without one, read the text from the
page images; Tesseract output from a compressed scan is not good enough to translate from.

Check the order of the pages against the printed page numbers. Phone photos are often out
of order. Fix the order by renaming the images, and delete `layout/` if it already exists.

## 2. Translation

1. Copy `$BT/docs/translation-instruction.md` into the book folder and fill in what the book
   is about, the translation mode and a glossary of key terms.
2. Settle the key terms before translating in bulk: a wrong term in the glossary repeats on
   every page. Look for definitions inside the book itself ("X, that is Y", "X, also called
   Y"); a later page often explains a word used earlier. Ask the user about doubtful terms.
3. Write one `translation/p-NNN.json` per page following the instruction: `source`,
   `translation`, `captions`, `notes`, `printed`, `kind`. Captions go into `captions`, not
   into `translation`. Every label on a drawing or pattern is a separate caption.
4. Record words you are unsure about in `doubts` (`word`, `context`, `guess`, `lang`). They
   are underlined in the reader so the user can check them against a dictionary.
5. After the last page, go through the terms once more across all pages, including `notes`.
6. Check the result: every page has a file, the JSON is valid, `printed` numbers run in
   order, `notes` contain no leftover working remarks.

Large books can be split into batches of about ten pages for parallel subagents, each with
the instruction and its page list.

## 3. Build

```bash
B=~/books/<book>/book.yaml
uv run --project $BT book-translate bilingual --book $B
nice -n 19 uv run --project $BT book-translate layout --book $B --jobs 2
uv run --project $BT book-translate inplace --book $B
uv run --project $BT book-translate check --book $B
uv run --project $BT book-translate reader --book $B
```

`layout` is slow (a few seconds per page) and caches its results. To rebuild only some
pages use `inplace --pages p-023a,p-041b --out sub.pdf`. Existing files are overwritten
only with `--force`.

Always build the reader. It shows which sentence matches which, and it is the quickest
way for the user to review the translation.

## 4. Check and fix

`check` counts defects in the final layout: boxes on photos, overlapping boxes, empty
boxes, text shrunk below 75%, text that did not fit, and words of the original that are
still visible. The goal is zero for the first three. Keep the numbers between runs so a
regression is visible at once.

To look at a page, render it next to the scan (`pdftoppm -r 70`) and compare. Fix a
defect as a rule for the whole class of pages (a config field or a code change with a test),
not by editing one page by hand. Pages with a grid layout, such as the table of contents,
glossaries and measurement tables, are better typeset from scratch with `retypeset_kinds`
or `retypeset_ranges`. Pages of songs set in two columns are better typeset from scratch
with `retypeset_verse_paragraphs`.

## 5. Hand over

Give the user three files: the bilingual PDF, the in-place PDF and the reader, and say what
did not work. For sharing over a messenger, `book-translate compress file.pdf` downsamples
the scans to 150 dpi; the text stays vector and sharp.

## Limits

- Text set with very wide letter or word spacing (common in books before 1920) breaks
  block detection: lines are split into single words. Typeset such pages from scratch.
- Right-to-left and vertical scripts are not supported.
- Scripts without spaces between words (Chinese, Japanese, Thai) cannot be shrunk word
  by word.
