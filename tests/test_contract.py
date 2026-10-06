"""Исполняемые спеки пунктов SPEC.md на маленьких примерах, без книги и без Chrome."""

import base64
import hashlib
import sys
from pathlib import Path

import pytest
from PIL import Image

from book_translate import compress, inplace, page_json, pages, reader, settings, tools

EXAMPLE = Path(__file__).resolve().parent.parent / "conf" / "book.example.yaml"
MINIMAL = (
    "slug: x\nocr_lang: eng\nsource_lower: a-z\nsource_upper: A-Z\n"
    "target_letters: а-яА-Я\ntarget_lang: ru\nnotes_headings: [Notes]\n"
)


@pytest.fixture
def book():
    b = settings.load(EXAMPLE)
    inplace.configure(b)
    return b


def config(tmp_path, text):
    path = tmp_path / "book.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.contract("BT-1.1")
def test_относительные_пути_книги_от_каталога_конфига(book):
    """Given пример конфига, When загружаем, Then рабочий каталог и выходы — абсолютные пути рядом с ним."""
    assert book.workdir == EXAMPLE.parent
    assert book.out_inplace == EXAMPLE.parent / "example-inplace.pdf"
    assert book.pages == EXAMPLE.parent / "pages"


@pytest.mark.contract("BT-1.2")
def test_неизвестное_поле_конфига_не_проглатывается(tmp_path):
    """Given опечатка в имени поля, When загружаем, Then ошибка с именем поля, а не молчаливый дефолт."""
    with pytest.raises(ValueError, match="ocr_lnag"):
        settings.load(config(tmp_path, MINIMAL + "ocr_lnag: eng\n"))


@pytest.mark.contract("BT-1.3")
def test_язык_книги_не_подставляется_молча(tmp_path):
    """Given конфиг без языка книги, When загружаем, Then ошибка с именами полей, а не язык по умолчанию."""
    with pytest.raises(ValueError, match="ocr_lang.*source_lower"):
        settings.load(config(tmp_path, "slug: x\n"))


@pytest.mark.contract("BT-1.4")
@pytest.mark.parametrize(
    "old, new, field",
    [
        ("notes_headings: [Notes]", "notes_headings: Notes", "notes_headings"),
        ("slug: x", "slug: x\npage_width_mm: -5", "page_width_mm"),
        ("slug: x", "slug: x\nspreads: sometimes", "spreads"),
        ("slug: x", "slug: x\nlabels: {pgae: p.}", "pgae"),
    ],
)
def test_неверный_тип_поля_ловится_при_загрузке(tmp_path, old, new, field):
    """Given поле неверного типа или значения, When загружаем, Then ошибка с именем поля."""
    with pytest.raises(ValueError, match=field):
        settings.load(config(tmp_path, MINIMAL.replace(old, new)))


@pytest.mark.contract("BT-2.1")
def test_цитата_в_несколько_строф_с_подписью(book):
    """Given песня в две строфы и подпись после закрывающей кавычки, When ищем цитаты, Then одна пара целиком."""
    t = {
        "source": '"Row the boat,\nrow it slow,\n\nhome is where the rivers go!"\nSong from the coast',
        "translation": "«Греби, греби,\nне спеши,\n\nдом там, где реки!»\nПесня с побережья",
    }
    found = inplace.quotes(t)
    assert len(found) == 1
    assert "rivers" in found[0]["source"] and "реки" in found[0]["translation"]


@pytest.mark.contract("BT-2.2")
def test_абзац_со_словом_в_кавычках_не_цитата(book):
    """Given абзац начинается со слова в кавычках, When ищем цитаты, Then цитат нет."""
    t = {
        "source": '"Sunday dress" was kept in a chest.',
        "translation": "«Воскресное платье» хранили в сундуке.",
    }
    assert inplace.quotes(t) == []


@pytest.mark.contract("BT-2.3")
def test_страница_песен_набирается_заново(book):
    """Given страница с тремя куплетами и порог 3, When решаем раскладку, Then страница набирается заново; при пороге 0 — нет."""
    verse = "a\nb\n\nc\nd\n\nprose\n\ne\nf"
    t = {"source": verse, "translation": verse, "kind": "text"}
    inplace.RETYPESET_VERSE_PARAGRAPHS = 3
    assert inplace.retypeset("p-005", t)
    inplace.RETYPESET_VERSE_PARAGRAPHS = 4
    assert not inplace.retypeset("p-005", t)
    inplace.RETYPESET_VERSE_PARAGRAPHS = 0
    assert not inplace.retypeset("p-005", t)


@pytest.mark.contract("BT-3.1")
def test_сноски_в_строку_разводятся_по_номерам(book):
    """Given сноски одной строкой, When разбираем, Then у каждой свой номер и текст, авторы — в форме перевода."""
    body, notes = inplace.split_notes("Текст.\n\nПримечания: 34 Smith 1901, 18. 35 Brown 1930, 27.")
    assert body.strip() == "Текст."
    assert [n for n, _ in notes] == ["34", "35"]
    assert "Смит" in inplace.notes_html(notes)


@pytest.mark.contract("BT-4.1")
def test_разворот_режется_на_две_страницы():
    """Given картинка шире своей высоты, When режем, Then две половины a и b; одиночная — одна."""
    spread = pages.split(Image.new("RGB", (2000, 1400)))
    single = pages.split(Image.new("RGB", (1000, 1400)))
    assert [s for s, _ in spread] == ["a", "b"] and spread[0][1].width == 1000
    assert [s for s, _ in single] == [""]


@pytest.mark.contract("BT-4.2")
def test_развороты_можно_задать_явно():
    """Given режим разворотов из конфига, When режем, Then never не режет широкую, always режет узкую."""
    assert [s for s, _ in pages.split(Image.new("RGB", (2000, 1400)), "never")] == [""]
    assert [s for s, _ in pages.split(Image.new("RGB", (1000, 1400)), "always")] == ["a", "b"]


@pytest.mark.contract("BT-5.1")
def test_алфавит_оригинала_из_конфига(tmp_path):
    """Given книга с кириллическим оригиналом, When берём буквы слова, Then кириллица не выбрасывается."""
    text = MINIMAL.replace("source_lower: a-z", 'source_lower: "а-яё"').replace(
        "source_upper: A-Z", 'source_upper: "А-ЯЁ"'
    )
    inplace.configure(settings.load(config(tmp_path, text)))
    assert inplace.letters("Сарафан!") == "сарафан"
    inplace.configure(settings.load(EXAMPLE))


@pytest.mark.contract("BT-5.2")
@pytest.mark.parametrize("word", ["العربية", "中文测试", "HELLO", "word"])
def test_слово_оригинала_без_строчных_букв_не_выбрасывается(book, word):
    """Given уверенно прочитанное слово без регистра или заглавными, When классифицируем, Then это слово оригинала."""
    assert inplace.is_source_word({"t": word, "c": 95})
    assert not inplace.is_source_word({"t": "wOrDs", "c": 95})


@pytest.mark.contract("BT-6.1")
def test_сжатие_не_перезаписывает_файл(tmp_path):
    """Given файл с выходным именем уже есть, When сжимаем, Then отказ, существующий файл цел."""
    src, out = tmp_path / "a.pdf", tmp_path / "a-compressed.pdf"
    src.write_bytes(b"%PDF-1.4")
    out.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        compress.run(src)
    assert out.read_bytes() == b"keep"


@pytest.mark.contract("BT-6.2")
def test_сборка_pdf_не_перезаписывает_без_force(tmp_path):
    """Given выходной PDF уже есть, When печатаем без force, Then отказ до запуска Chrome, файл цел."""
    out = tmp_path / "book.pdf"
    out.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        tools.print_pdf("chrome-is-not-called", tmp_path / "book.html", out)
    assert out.read_bytes() == b"keep"


@pytest.mark.contract("BT-7.1")
def test_перевод_страницы_в_старой_схеме_читается(tmp_path):
    """Given файл страницы с ключами fi/ru, When читаем, Then поля source/translation, и у подписей тоже."""
    path = tmp_path / "p-001.json"
    path.write_text(
        '{"page": "p-001", "fi": "Sleeve", "ru": "Рукав", "captions": [{"fi": "Front", "ru": "Перед"}]}',
        encoding="utf-8",
    )
    page = page_json.read(path)
    assert (page["source"], page["translation"]) == ("Sleeve", "Рукав")
    assert page["captions"] == [{"source": "Front", "translation": "Перед"}]


@pytest.mark.contract("BT-7.2")
def test_перевод_страницы_неверного_типа_ловится(tmp_path):
    """Given перевод не строкой, When читаем, Then ошибка с именем файла и поля."""
    path = tmp_path / "p-002.json"
    path.write_text('{"page": "p-002", "translation": 42}', encoding="utf-8")
    with pytest.raises(ValueError, match="p-002.json.*translation"):
        page_json.read(path)


@pytest.mark.contract("BT-8.1")
@pytest.mark.parametrize("pid", ["../etc/passwd", "/tmp/x", "p-001/../../x", 'p-1a"><b>'])
def test_идентификатор_страницы_не_становится_путём(pid):
    """Given идентификатор страницы с путём или разметкой, When проверяем, Then отказ."""
    with pytest.raises(ValueError):
        tools.checked_page_ids(["p-001", pid])


@pytest.mark.contract("BT-8.2")
def test_slug_не_выводит_за_каталог(tmp_path):
    """Given slug с переходом по каталогам, When загружаем конфиг, Then отказ."""
    with pytest.raises(ValueError, match="slug"):
        settings.load(config(tmp_path, MINIMAL.replace("slug: x", "slug: ../../home/x")))


@pytest.mark.contract("BT-8.3")
def test_csp_разрешает_только_скрипт_подгонки():
    """Given скрипт подгонки, When строим политику, Then скрипт разрешён только по хешу, сеть и файлы закрыты."""
    meta = tools.csp_meta(inplace.FIT_JS)
    digest = base64.b64encode(hashlib.sha256(inplace.FIT_JS.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}';" in meta
    assert "default-src 'none'" in meta and "img-src data:;" in meta
    assert "unsafe-inline" not in meta.split("script-src")[1]


@pytest.mark.contract("BT-9.1")
def test_упавший_tesseract_не_даёт_пустой_результат():
    """Given программа распознавания завершилась с ошибкой, When распознаём, Then исключение, а не пустой TSV."""
    with pytest.raises(tools.ToolError):
        tools.ocr_tsv(sys.executable, {}, Image.new("L", (10, 10)), "eng", 6)


@pytest.mark.contract("BT-9.2")
def test_нет_программы_понятная_ошибка():
    """Given программы нет в системе, When ищем, Then ToolError с её именем."""
    with pytest.raises(tools.ToolError, match="no-such-binary-xyz"):
        tools.find_executable("no-such-binary-xyz")


@pytest.mark.contract("BT-10.1")
def test_читалка_сопоставляет_строки_предложения_и_абзацы():
    """Given стих, абзац с равным числом предложений и абзац с разным, When сопоставляем, Then строки, фразы, целый абзац."""
    source = "Lenti kokko,\nkoilta ilmoin.\n\nHän tuli. Hän meni.\n\nSe oli hyvä. Ja kaunis."
    translation = "Летел орёл,\nс востока.\n\nОн пришёл. Он ушёл.\n\nЭто было хорошо и красиво."
    paras = reader.units(source, translation)
    assert paras[0] == ([("Lenti kokko,", "Летел орёл,"), ("koilta ilmoin.", "с востока.")], True)
    assert paras[1] == ([("Hän tuli.", "Он пришёл."), ("Hän meni.", "Он ушёл.")], False)
    assert paras[2] == ([("Se oli hyvä. Ja kaunis.", "Это было хорошо и красиво.")], False)
    assert reader.units("a\n\nb", "только один") == [([("a\n\nb", "только один")], False)]


@pytest.mark.contract("BT-10.2")
def test_читалка_выделяет_сомнительное_слово_с_подсказкой():
    """Given сомнение «kešu repońi / x» и текст, When размечаем, Then первое вхождение в mark с догадкой."""
    texts = ["reunašša kešu repońi, vierellä", "kešu repońi ещё раз"]
    doubts = [{"word": "kešu repońi / zzz", "guess": "ручная лисонька"}]
    tips = [reader.doubt_tip(doubts[0], {"вердикт": "верно"})]
    spans = reader.doubt_spans(texts, doubts, tips)
    assert spans[1] == []
    html_text = reader.marked(texts[0], spans[0])
    assert '<mark class="doubt"' in html_text and "kešu repońi</mark>" in html_text
    assert "ручная лисонька" in html_text and "Astra: верно" in html_text


def test_читалка_показывает_вердикт_словаря():
    """Given сомнение, подтверждённое словарём, и сомнение с правкой, When размечаем, Then вердикт в подсказке, подтверждённое — бледным."""
    texts = ["hasie kannata, kosinolla istu"]
    doubts = [
        {
            "word": "hasie",
            "guess": "спорить",
            "checked": "нет — расходится",
            "dictionary": "хвалить",
            "fix": "хвастаться",
        },
        {"word": "kosinolla", "guess": "голбец", "checked": "да — совпадает"},
    ]
    tips = [reader.doubt_tip(d, None) for d in doubts]
    html_text = reader.marked(texts[0], reader.doubt_spans(texts, doubts, tips)[0])
    assert "словарь: нет — расходится — хвалить" in html_text and "правка: хвастаться" in html_text
    assert '<mark class="doubt ok"' in html_text and '<mark class="doubt" ' in html_text


def test_читалка_не_заменяет_файл_без_force(tmp_path):
    """Given готовый файл читалки, When публикуем без force, Then отказ и файл цел; с force — заменён, временных нет."""
    target = tmp_path / "r.html"
    target.write_text("старое", encoding="utf-8")
    with pytest.raises(FileExistsError):
        reader.publish(target, "новое", force=False)
    assert target.read_text(encoding="utf-8") == "старое"
    reader.publish(target, "новое", force=True)
    assert target.read_text(encoding="utf-8") == "новое"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r.html"]


def test_читалка_делит_длинное_сомнение_без_зависания():
    """Given слово из тысяч пробелов без разделителей, When делим на части, Then одна часть и линейное время."""
    word = "a" + " " * 50000 + "b"
    assert reader.ALTERNATIVES.split(word) == [word]
    assert reader.doubt_spans(["a b"], [{"word": word}], ["tip"]) == [[]]
