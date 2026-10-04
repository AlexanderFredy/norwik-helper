"""Детекция строк бренда тремя способами: колонка, разделитель, картинка (04.10.2026).

**ГЛАВНЫЕ ТЕСТЫ ЗДЕСЬ ОТРИЦАТЕЛЬНЫЕ.** Правило «одинокая ячейка = раздел» на боевых мелких
форматах даёт мусор: у Most Floor так выглядят ТОВАРЫ (цены стоят одной строкой на
коллекцию), у Линдервуда верхним уровнем становится примечание «Важно: цены включают…».
Список «брендов», собранный из товаров, хуже отсутствующего — по нему РЕЖУТ файл. Поэтому
каждый порог проверяется отдельно, а раскладки взяты с боя.

Размеры листов в тестах синтетические, но пороги настоящие: лист меньше `SECTION_MIN_ROWS`
отсекается размером, поэтому ложные раскладки раздуваются до тысячи строк — иначе они
проходили бы проверку по причине, которую мы не тестируем.
"""
import struct
import unittest
from unittest.mock import patch

from pathlib import Path

from src.price_tool.brand_rows import (BY_COLUMN, BY_IMAGE, BY_MARK_SECTION,
                                       BY_SECTION, BY_SHEET_NAME, RULE_VERSION,
                                       SECTION_MIN_ROWS, brand_map, brand_per_raw_row,
                                       brands_in_rows, find_marked_sections, find_sections,
                                       from_sheet_name, mark_keys, only_brand_rows,
                                       rows_cost)
from src.price_tool.parser import Sheet

HEAD = [
    ["Оптовый отдел", "", "", "", "", "", "", "", 'ООО "Артисан-Проект"'],
    ["Прайс-лист на 26.08.2026"],
    ["Код", "Заводской код", "Вид", "Размер", "В упаковке", "Ед.изм.", "Наименование",
     "Розн", "Опт"],
]


def artisana(brands=5, colls=2, items=4) -> Sheet:
    """Раскладка Артисаны: бренд в колонке 0, коллекция в колонке 1, товар — густая строка."""
    rows = [list(r) for r in HEAD]
    for b in range(brands):
        rows.append([f"Бренд {b}"])
        for c in range(colls):
            rows.append(["", f"Коллекция {b}-{c}"])
            for i in range(items):
                rows.append([f"+{b}{c}{i}", f"A{b}{c}{i}", "Плитка", "20x20", "1",
                             "кв.м.", f"Декор {i}", "1140", "950"])
    # Доращиваем до порога размера: он отсекает мелкие форматы, и тестировать надо не его.
    while len(rows) < SECTION_MIN_ROWS:
        rows.append(["+9", "A9", "Плитка", "20x20", "1", "кв.м.", "Хвост", "1", "2"])
    return Sheet(name="Price", rows=rows)


class SectionTest(unittest.TestCase):
    """Горизонтальный разделитель — боевая раскладка Артисаны."""

    def setUp(self):
        self.sheet = artisana()
        self.spot = find_sections(self.sheet)

    def test_mode_and_brand_column(self):
        self.assertIsNotNone(self.spot)
        self.assertEqual(self.spot.mode, BY_SECTION)
        self.assertEqual(self.spot.column, 0)

    def test_brands_are_the_top_level_only(self):
        """Коллекции в список НЕ идут: детализация до них админу не нужна (его решение)."""
        self.assertEqual([b for b, _ in brands_in_rows(self.spot)],
                         [f"Бренд {n}" for n in range(5)])

    def test_rows_counted_are_items_not_separators(self):
        """Счёт строк — по товарам: завышенный обесценил бы оценку расхода."""
        self.assertEqual(dict(brands_in_rows(self.spot))["Бренд 0"], 8)

    def test_header_is_everything_above_the_first_brand(self):
        """У Артисаны две строки заголовков и контакты выше — модель должна видеть их все."""
        self.assertEqual(self.spot.header_rows, len(HEAD))

    def test_collection_rows_travel_with_the_brand(self):
        """Без строки «Коллекция» модель не узнает, к какой коллекции позиция: задачи
        адресуются парой (марка, коллекция)."""
        kept = only_brand_rows(self.sheet, self.spot, ["Бренд 1"]).rows
        self.assertEqual(kept[:len(HEAD)], [list(r) for r in HEAD])
        tail = kept[len(HEAD):]
        self.assertEqual(tail[0], ["Бренд 1"])
        self.assertIn(["", "Коллекция 1-0"], tail)
        self.assertNotIn(["", "Коллекция 0-0"], tail)
        # 1 разделитель бренда + 2 коллекции + 8 товаров
        self.assertEqual(len(tail), 11)

    def test_cost_counts_items_and_chars(self):
        lines, chars = rows_cost(self.sheet, self.spot, ["Бренд 2"])
        self.assertEqual(lines, 8)
        self.assertGreater(chars, 0)

    def test_nothing_wanted_gives_the_header_alone(self):
        self.assertEqual(len(only_brand_rows(self.sheet, self.spot, []).rows), len(HEAD))


class SectionGuardTest(unittest.TestCase):
    """Пороги доказательств. Каждый проверяется отдельно: сработает один — промолчит весь."""

    def test_small_sheet_is_not_touched(self):
        """Мелкий формат отсекается размером — именно он разделяет боевые случаи."""
        small = artisana()
        small = Sheet(name="Прайс", rows=small.rows[:50])
        self.assertIsNone(find_sections(small))

    def test_items_as_lonely_cells_are_not_brands(self):
        """РАСКЛАДКА MOST FLOOR: цены стоят одной строкой на коллекцию, и сами товары
        выглядят одинокими ячейками — 86 строк из 107. Признать их брендами значило бы
        нарезать файл по названиям декоров."""
        rows = [list(r) for r in HEAD]
        for n in range(SECTION_MIN_ROWS):
            rows.append([f"Коллекция {n} - 8 декоров"] if n % 9 == 0
                        else [f"{3310 + n} Декор"])
        self.assertIsNone(find_sections(Sheet(name="Ламинат", rows=rows)))

    def test_a_note_is_not_a_brand(self):
        """РАСКЛАДКА ЛИНДЕРВУДА: в колонке 0 всего две одинокие ячейки, и одна из них —
        «Важно: цены включают в себя стоимость доставки…»."""
        rows = [list(r) for r in HEAD]
        rows.append(["ПОДЛОЖКА ЛИСТОВАЯ 3 мм"])
        for n in range(SECTION_MIN_ROWS):
            rows.append(["", f"Ламинат — 8 мм 33 класс {n}"] if n % 12 == 0
                        else ["A1", "код", "Пол", "190x1290", "2", "кв.м.", "Дуб", "1", "2"])
        rows.append(["Важно:  цены включают в себя стоимость доставки"])
        self.assertIsNone(find_sections(Sheet(name="Прайс Москва", rows=rows)))

    def test_few_sections_are_not_a_list_of_brands(self):
        self.assertIsNone(find_sections(artisana(brands=3)))

    def test_section_without_items_is_dropped(self):
        """Раздел, под которым нет ни одной товарной строки, — оформление, а не бренд."""
        sheet = artisana()
        rows = list(sheet.rows)
        rows.insert(len(HEAD), ["ЦЕНА 1* — наша доставка по Москве"])
        spot = find_sections(Sheet(name="Price", rows=rows))
        self.assertIsNotNone(spot)
        self.assertNotIn("ЦЕНА 1* — наша доставка по Москве",
                         [b for b, _ in brands_in_rows(spot)])

    def test_junk_in_a_far_column_does_not_become_the_brand_level(self):
        """У FLOOR SERVICE одинокие ячейки встречаются в колонке 26 («0.35»). Уровнем
        бренда считается САМАЯ ЛЕВАЯ колонка разделов, и мусор справа её не подменяет."""
        sheet = artisana()
        rows = list(sheet.rows)
        rows.insert(len(HEAD) + 1, [""] * 26 + ["0.35"])
        spot = find_sections(Sheet(name="Price", rows=rows))
        self.assertIsNotNone(spot)
        self.assertEqual(spot.column, 0)


class ColumnModeTest(unittest.TestCase):
    """Колонка остаётся первым и главным способом: признак стоит в каждой строке."""

    SHEET = Sheet(name="TDSheet", rows=[
        ["Фабрика", "Бренд", "Артикул"],
        ["ABK", "ABK", "4938"],
        ["", "", "4939"],
        ["VitrA", "VitrA", "K9470"],
    ])

    def test_column_wins_and_carries_merged_cells(self):
        spot = brand_map(self.SHEET)
        self.assertEqual(spot.mode, BY_COLUMN)
        self.assertEqual(dict(brands_in_rows(spot)), {"ABK": 2, "VitrA": 1})

    def test_filter_keeps_the_header(self):
        spot = brand_map(self.SHEET)
        kept = only_brand_rows(self.SHEET, spot, ["VitrA"])
        self.assertEqual(kept.rows[0][1], "Бренд")
        self.assertEqual([r[1] for r in kept.rows[1:]], ["VitrA"])

    def test_sheet_without_any_sign_is_silent(self):
        plain = Sheet(name="КЛЕЙ", rows=[["Артикул", "Цена"], ["A1", "100"]])
        self.assertIsNone(brand_map(plain))


class ImageModeTest(unittest.TestCase):
    """Баннер картинкой: код знает строку, имя приносит `logo_intake`.

    Раскладка как в жизни: баннер висит над ПУСТОЙ строкой — картинка плавает над листом и
    своей ячейки не занимает. Отсюда и главная ловушка этого режима: якоря приходят в сырых
    номерах строк книги, а фильтр считает непустые.
    """

    SHEET = Sheet(name="Ламинат", rows=[
        ["Прайс"],                       # сырая 1, непустая 1
        ["Артикул", "Цена", "Упаковка"],  # сырая 2, непустая 2 — шапка
        [],                              # сырая 3 — под ней баннер Kronotex
        ["A1", "100", "2"],              # сырая 4, непустая 3
        ["A2", "110", "2"],              # сырая 5, непустая 4
        [],                              # сырая 6 — под ней баннер Classen
        ["B1", "200", "2"],              # сырая 7, непустая 5
    ])
    ANCHORS = {"Ламинат": {3: "Kronotex", 6: "Classen"}}

    def test_two_anchors_split_the_sheet(self):
        spot = brand_map(self.SHEET, self.ANCHORS)
        self.assertEqual(spot.mode, BY_IMAGE)
        self.assertEqual(dict(brands_in_rows(spot)), {"Kronotex": 2, "Classen": 1})
        self.assertEqual(spot.header_rows, 2)

    def test_rows_are_numbered_as_the_filter_counts_them(self):
        """РЕГРЕССИЯ. Якоря — сырые номера строк книги (их же использует `mark_images`), а
        `rows` обязаны быть в нумерации непустых: иначе пустая строка под баннером сдвигает
        бренды вверх на число пустых строк, и позиции достаются ЧУЖОМУ бренду."""
        spot = brand_map(self.SHEET, self.ANCHORS)
        self.assertEqual(spot.rows, ((3, "Kronotex"), (4, "Kronotex"), (5, "Classen")))

    def test_filter_keeps_the_header_and_only_the_brand(self):
        spot = brand_map(self.SHEET, self.ANCHORS)
        kept = only_brand_rows(self.SHEET, spot, ["Classen"])
        self.assertEqual(kept.rows, [["Прайс"], ["Артикул", "Цена", "Упаковка"],
                                     ["B1", "200", "2"]])

    def test_an_anchor_row_with_data_is_not_lost(self):
        """Якорь может попасть и на обычную строку прайса. Выбросив её как «разделитель», мы
        потеряли бы позицию; бренд начинается С НЕЁ."""
        spot = brand_map(self.SHEET, {"Ламинат": {4: "Kronotex", 7: "Classen"}})
        self.assertEqual(dict(brands_in_rows(spot)), {"Kronotex": 2, "Classen": 1})

    def test_one_anchor_separates_nothing(self):
        """У Монарха 93 картинки привязаны ВСЕ к строке 1, у Most Floor и FLOOR SERVICE по
        одной на лист — это шапка. Один якорь дал бы список из одной записи и ложное
        чувство, что выбор работает (замер 04.10.2026)."""
        self.assertIsNone(brand_map(self.SHEET, {"Ламинат": {1: "Most Floor"}}))

    def test_anchors_of_another_sheet_are_ignored(self):
        self.assertIsNone(brand_map(self.SHEET, {"SPC": {3: "Kronotex", 6: "Classen"}}))


class RawRowTest(unittest.TestCase):
    """Бренд строки по ПОЛОЖЕНИЮ — так его спрашивают цены (`prices_from_rows`)."""

    def test_every_raw_row_gets_its_owner(self):
        sheet = artisana(brands=5, colls=1, items=2)
        spot = find_sections(sheet)
        owners = brand_per_raw_row(sheet, spot)
        self.assertEqual(len(owners), len(sheet.rows))
        self.assertEqual(owners[:len(HEAD)], [""] * len(HEAD))
        self.assertEqual(owners[len(HEAD)], "Бренд 0")
        self.assertEqual(owners[len(HEAD) + 2], "Бренд 0")

    def test_empty_rows_belong_to_nobody(self):
        sheet = artisana(brands=5, colls=1, items=2)
        rows = list(sheet.rows)
        rows.insert(len(HEAD) + 1, [])
        sheet = Sheet(name="Price", rows=rows)
        owners = brand_per_raw_row(sheet, find_sections(sheet))
        self.assertEqual(owners[len(HEAD) + 1], "")


class LogoNamingTest(unittest.IsolatedAsyncioTestCase):
    """Имя на логотипе спрашивается один раз и помнится по хешу картинки."""

    class Orchestrator:
        def __init__(self, answer):
            self.answer = answer
            self.calls = 0

        async def handle_turn(self, messages, **kw):
            self.calls += 1
            self.seen = messages
            return self.answer, messages

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    async def store(self):
        from src.storage.suppliers import SupplierStore

        store = SupplierStore(self.root / "t.db")
        await store.init()
        supplier = await store.add_supplier("Артисан")
        await store.add_signature(supplier.id, "hash-1")
        return store

    @staticmethod
    def images(**sheets):
        return lambda content: {name: [(row, data, "image/png")
                                       for row, data in rows.items()]
                                for name, rows in sheets.items()}

    async def test_names_are_read_once_and_remembered(self):
        from src.model.logo_intake import image_hash, name_logos

        store = await self.store()
        agent = self.Orchestrator('[{"n": 1, "brand": "Kronotex"}]')
        images = self.images(Ламинат={3: b"logo-bytes"})

        with patch("src.price_tool.parser.extract_images", images):
            got = await name_logos(agent, store, "hash-1", b"x", "прайс.xlsx")
            self.assertEqual(got, {"Ламинат": {3: "Kronotex"}})
            self.assertEqual(agent.calls, 1)

            again = await name_logos(agent, store, "hash-1", b"x", "прайс.xlsx")

        self.assertEqual(again, {"Ламинат": {3: "Kronotex"}})
        self.assertEqual(agent.calls, 1, "второй файл того же формата обязан быть бесплатным")
        self.assertEqual(await store.logos_for("hash-1"),
                         {image_hash(b"logo-bytes"): "Kronotex"})

    async def test_not_a_logo_is_remembered_too(self):
        """Фото товара и рамки встречаются чаще логотипов; не запомнив ответ «это не
        логотип», мы спрашивали бы о них в каждом прогоне."""
        from src.model.logo_intake import name_logos

        store = await self.store()
        agent = self.Orchestrator('[{"n": 1, "brand": ""}]')
        images = self.images(Ламинат={3: b"frame"})

        with patch("src.price_tool.parser.extract_images", images):
            self.assertEqual(await name_logos(agent, store, "hash-1", b"x", "п.xlsx"), {})
            await name_logos(agent, store, "hash-1", b"x", "п.xlsx")

        self.assertEqual(agent.calls, 1)
        self.assertEqual(list((await store.logos_for("hash-1")).values()), [""])

    async def test_broken_answer_names_nothing(self):
        from src.model.logo_intake import name_logos

        store = await self.store()
        agent = self.Orchestrator("не могу разобрать картинки")
        with patch("src.price_tool.parser.extract_images",
                   self.images(Ламинат={3: b"logo"})):
            self.assertEqual(await name_logos(agent, store, "hash-1", b"x", "п.xlsx"), {})

    async def test_a_crowd_of_pictures_is_not_asked_about(self):
        """У Монарха 93 картинки на листе — это фото товаров, а не разделители. Платить за
        вопрос о них незачем."""
        from src.model.logo_intake import MAX_LOGOS, name_logos

        store = await self.store()
        agent = self.Orchestrator("[]")
        many = {row: f"pic-{row}".encode() for row in range(1, MAX_LOGOS + 5)}
        with patch("src.price_tool.parser.extract_images", self.images(Лист=many)):
            self.assertEqual(await name_logos(agent, store, "hash-1", b"x", "п.xlsx"), {})
        self.assertEqual(agent.calls, 0)

    async def test_logo_names_survive_a_hash_recompute(self):
        from src.model.logo_intake import image_hash
        from src.storage.suppliers import SupplierStore

        store = SupplierStore(self.root / "r.db")
        await store.init()
        supplier = await store.add_supplier("Артисан")
        sig = await store.add_signature(supplier.id, "старый")
        await store.remember_logos("старый", [(image_hash(b"logo"), "Kronotex")])

        await store.rehash_signature(sig.id, "новый")
        self.assertEqual(await store.logos_for("новый"),
                         {image_hash(b"logo"): "Kronotex"})
        self.assertEqual(await store.logos_for("старый"), {})


class BudgetTest(unittest.TestCase):
    """Предохранитель: отмеченное сверх предела не уезжает модели молча."""

    def tools(self, marks, sheet):
        from src.model.task_builder import TaskBuilderTools

        t = TaskBuilderTools(b"x", "Price.xls", only_marks=marks)
        t._sheets = t._narrow([sheet])
        return t

    def test_too_much_refuses_with_an_estimate(self):
        from src.model.task_builder import BIG_BRAND_ROWS

        sheet = artisana(brands=5, colls=2, items=(BIG_BRAND_ROWS // 2) + 1)
        t = self.tools(["Бренд 0"], sheet)
        self.assertEqual(t.sheets, [])
        self.assertIn("отмечено слишком много", t.pick_problem)
        self.assertIn("токенов", t.pick_problem)
        self.assertGreater(t.kept_rows, BIG_BRAND_ROWS)

    def test_a_modest_choice_goes_through(self):
        t = self.tools(["Бренд 0"], artisana())
        self.assertEqual([s.name for s in t.sheets], ["Price"])
        self.assertEqual(t.kept_rows, 8)
        self.assertEqual(t.pick_problem, "")
        self.assertEqual(t.brand_modes, {"Price": BY_SECTION})

    def test_unticked_brands_are_named_for_the_report(self):
        t = self.tools(["Бренд 0"], artisana())
        self.assertEqual(t.skipped_marks, [f"Бренд {n}" for n in range(1, 5)])

MARKS = ["Classen / Классен", "Ultrafloor / Ультрафлор", "Westerhof / Вестерхоф",
         "Kronotex", "Peli", "AGT"]
KEYS = mark_keys(MARKS)


def stroyindustria() -> Sheet:
    """Боевая раскладка Стройиндустрии: 44 строки, два бренда разделителями.

    Строка «ULTRAFLOOR» несёт ещё «цена 1» и «цена 2» в далёких колонках — именно поэтому
    правило «одинокая ячейка» её не видит, а сверка со справочником видит.
    """
    return Sheet(name="Прайс от 01.10.2026", rows=[
        ['ООО "СТРОЙИНДУСТРИЯ"'],
        [],
        ["8-495-740-99-59"],
        ["", "", "ПРАЙС-ЛИСТ"],
        ["", "Замок", "Размер мм", "Класс", "Фаска", "м2 в уп", "ОПТ/м2", "РРЦ/м2"],
        ["CLASSEN"],
        ["POOL WR 832 PROMO", "Megaloc", "1285*192*8", "32/АС4", "да", "1.974", "1285",
         "1800"],
        ["ELEGANT 4V", "Clic it", "1292*193*8", "33/АС5", "да", "1.995", "1180", "1715"],
        ["ULTRAFLOOR", "", "", "", "", "", "", "", "", "цена 1", "цена 2"],
        ["CASTELLO", "Twin Clic", "1285*192*8", "32/АС4", "нет", "2.22", "532", "691",
         "", "506", "497"],
        ["FORTE VARIO", "Twin Clic", "1285*192*8", "33/АС5", "да", "2.22", "727", "1017"],
        ["Акссеуары"],
        ["Подложка НПЕ 3мм", "", "50000*1005*3мм", "", "", "52.5", "21"],
    ])


class MarkedSectionTest(unittest.TestCase):
    """Разделитель, опознанный ПО СПРАВОЧНИКУ МАРОК 1С — лист любого размера."""

    def setUp(self):
        self.sheet = stroyindustria()
        self.spot = find_marked_sections(self.sheet, KEYS)

    def test_both_brands_are_found_in_a_small_sheet(self):
        """44 строки — порог размера такой лист не пройдёт никогда, а справочник его
        размечает: совпадение с именем НАСТОЯЩЕЙ марки это не догадка."""
        self.assertIsNotNone(self.spot)
        self.assertEqual(self.spot.mode, BY_MARK_SECTION)
        self.assertEqual([b for b, _ in brands_in_rows(self.spot)],
                         ["CLASSEN", "ULTRAFLOOR"])

    def test_a_separator_with_labels_in_far_columns_is_still_a_separator(self):
        """РЕГРЕССИЯ. У «ULTRAFLOOR» заполнены три ячейки («цена 1», «цена 2» в колонках 9 и
        10), и правило «одинокая ячейка» его не видит. Разделитель от товара отличает пустота
        в КОЛОНКАХ ДАННЫХ — тех, что заняты в строке заголовков."""
        rows = dict(brands_in_rows(self.spot))
        self.assertEqual(rows["ULTRAFLOOR"], 4)   # 2 товара + «Акссеуары» + подложка

    def test_header_is_everything_above_the_first_brand(self):
        self.assertEqual(self.spot.header_rows, 4)
        kept = only_brand_rows(self.sheet, self.spot, ["CLASSEN"]).rows
        self.assertEqual(kept[0], ['ООО "СТРОЙИНДУСТРИЯ"'])
        self.assertEqual(kept[4], ["CLASSEN"])
        self.assertEqual(len(kept), 4 + 1 + 2)

    def test_a_section_outside_the_catalogue_is_not_a_brand(self):
        """«Акссеуары» — раздел подложек, а не марка; его строки достаются текущему бренду, и
        это осознанно: потерять их молча хуже, а чужие виды товара отсекают категории."""
        self.assertNotIn("Акссеуары", [b for b, _ in brands_in_rows(self.spot)])

    def test_an_item_named_like_a_mark_stays_an_item(self):
        """Строка с данными в колонках заголовков — товар, даже если её первая ячейка совпала
        с именем марки: иначе мы потеряли бы позицию."""
        rows = list(self.sheet.rows)
        rows.append(["Kronotex", "Megaloc", "1285*192*8", "33/АС5", "да", "2.2", "900",
                     "1200"])
        spot = find_marked_sections(Sheet(name="Прайс", rows=rows), KEYS)
        self.assertNotIn("Kronotex", [b for b, _ in brands_in_rows(spot)])

    def test_a_factory_note_is_not_a_brand(self):
        """РАСКЛАДКА ВЕСТЕРХОФА: «Завод PELI Турция» — завод, а не марка, и на их смешении мы
        уже обожглись (18 ложных задач). Сравнение ТОЧНОЕ, поэтому не совпадает."""
        rows = [["", "Подложка", "Упаковка", "Цена"],
                ["Завод PELI Турция"],
                ["WESTERHOF COSMO 33 КЛАСС", "3 мм", "2.1", "1500"],
                ["Завод AGT Турция"],
                ["Westerhof Effect 33 класс", "3 мм", "2.1", "1600"]]
        self.assertIsNone(find_marked_sections(Sheet(name="ламинат Турция", rows=rows),
                                               KEYS))

    def test_collections_are_not_brands(self):
        """РАСКЛАДКА MOST FLOOR: разделы там — коллекции, в справочнике марок их нет."""
        rows = [["Артикул товара", "Описание коллекции", "Дилерская цена"],
                ["Коллекция Миллениум Про - 8 декоров"],
                ["3310 Штраус"],
                ["3311 Бетховен"]]
        self.assertIsNone(find_marked_sections(Sheet(name="Ламинат", rows=rows), KEYS))

    def test_without_the_catalogue_it_is_silent(self):
        """Выдумывать бренды, не сверяясь ни с чем, нельзя: по ним режут файл."""
        self.assertIsNone(find_marked_sections(self.sheet, set()))

    def test_brand_map_picks_this_way_for_the_real_sheet(self):
        spot = brand_map(self.sheet, None, MARKS)
        self.assertEqual(spot.mode, BY_MARK_SECTION)


class SheetNameTest(unittest.TestCase):
    """Бренд в ИМЕНИ ЛИСТА: «Ассортимент CLASSEN» — весь лист про эту марку."""

    @staticmethod
    def assortment(name="Ассортимент CLASSEN") -> Sheet:
        return Sheet(name=name, rows=[
            ["Наименование", "Стендовая программа", "Складская позиция"],
            ["POOL WR 832 PROMO"],
            ["68278 Ламинат Pool PROMO 832-4", "", "склад"],
            ["Elegant 4V"],
            ["1872070 Ламинат Classen Elegant", "", "склад"],
        ])

    def test_the_whole_sheet_belongs_to_the_mark(self):
        spot = from_sheet_name(self.assortment(), KEYS)
        self.assertIsNotNone(spot)
        self.assertEqual(spot.mode, BY_SHEET_NAME)
        self.assertEqual(brands_in_rows(spot), [("CLASSEN", 4)])

    def test_name_is_shown_as_written_in_the_sheet(self):
        spot = from_sheet_name(self.assortment("Ассортимент Classen"), KEYS)
        self.assertEqual([b for b, _ in brands_in_rows(spot)], ["Classen"])

    def test_collections_inside_do_not_become_brands(self):
        """На таком листе одинокими ячейками размечены КОЛЛЕКЦИИ («Elegant 4V»), и брендами
        они не становятся именно потому, что имя листа сильнее."""
        spot = brand_map(self.assortment(), None, MARKS)
        self.assertEqual(spot.mode, BY_SHEET_NAME)
        self.assertNotIn("Elegant 4V", [b for b, _ in brands_in_rows(spot)])

    def test_a_name_without_a_mark_gives_nothing(self):
        self.assertIsNone(from_sheet_name(self.assortment("Прайс от 01.10.2026"), KEYS))
        self.assertIsNone(from_sheet_name(self.assortment("ИЗМЕНЕНИЯ"), KEYS))

    def test_two_marks_in_the_name_mean_none(self):
        """Выбор наугад однажды припишет лист чужой марке, а по нему отбираются строки и
        пишутся цены."""
        self.assertIsNone(from_sheet_name(self.assortment("Classen и Ultrafloor"), KEYS))

    def test_a_mark_as_part_of_a_word_does_not_count(self):
        self.assertIsNone(from_sheet_name(self.assortment("Классенька"), KEYS))

    def test_without_the_catalogue_it_is_silent(self):
        self.assertIsNone(from_sheet_name(self.assortment(), set()))

    def test_in_file_markup_wins_over_the_sheet_name(self):
        """Лист, названный по бренду, внутри может быть размечен по другим — и эта разметка
        главнее: иначе всё свалилось бы в одну марку."""
        sheet = stroyindustria()
        named = Sheet(name="Ассортимент CLASSEN", rows=sheet.rows)
        spot = brand_map(named, None, MARKS)
        self.assertEqual(spot.mode, BY_MARK_SECTION)
        self.assertEqual([b for b, _ in brands_in_rows(spot)], ["CLASSEN", "ULTRAFLOOR"])
class RuleVersionTest(unittest.IsolatedAsyncioTestCase):
    """Отметка «смотрели» обязана говорить, ЧЕМ смотрели (`RULE_VERSION`, 04.10.2026).

    Детекция за один день выросла дважды, и оба раза уже просмотренные форматы остались бы с
    прежним выводом: сперва у FLOOR SERVICE висел пустой способ, потом Стройиндустрия не
    перечиталась новыми детекторами, потому что способ у неё уже стоял. Сравнение с версией
    закрывает это раз и навсегда.
    """

    async def asyncSetUp(self):
        import tempfile

        from src.storage.suppliers import SupplierStore

        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.store = SupplierStore(self.root / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("Стройиндустрия")
        self.sig = await self.store.add_signature(supplier.id, "hash-1")

    async def asyncTearDown(self):
        self._dir.cleanup()

    def workbook(self, rows):
        import openpyxl

        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(row)
        path = self.root / "прайс.xlsx"
        wb.save(path)
        return path

    async def add_file(self):
        path = self.workbook([
            ["", "Замок", "Размер мм", "Класс"],
            ["CLASSEN"],
            ["POOL WR 832", "Megaloc", "1285*192*8", "32/АС4"],
            ["ULTRAFLOOR", "", "", "", "", "", "", "", "", "цена 1"],
            ["CASTELLO", "Twin Clic", "1285*192*8", "32/АС4"],
        ])
        await self.store.add_price_file(self.sig.id, path.name, str(path),
                                        received_at="2026-10-01T00:00:00")

    async def run_fill(self):
        from src.model.brand_backfill import fill_brand_lists

        return await fill_brand_lists(self.store, ["Classen / Классен",
                                                   "Ultrafloor / Ультрафлор"])

    async def test_an_older_rule_is_read_again_and_stamped(self):
        await self.add_file()
        await self.store.set_signature_brand_col("hash-1", None, "нет", 1)

        self.assertEqual(await self.run_fill(), 1)
        self.assertEqual(await self.store.brand_rule_for("hash-1"), RULE_VERSION)
        self.assertEqual(await self.store.brand_mode_for("hash-1"),
                         "разделитель по справочнику")
        self.assertEqual([m.brand for m in await self.store.marks_for("hash-1")],
                         ["CLASSEN", "ULTRAFLOOR"])

    async def test_the_current_rule_is_not_read_again(self):
        await self.add_file()
        self.assertEqual(await self.run_fill(), 1)
        self.assertEqual(await self.run_fill(), 0)

    async def test_a_mode_without_a_column_still_counts_as_looked_at(self):
        """РЕГРЕССИЯ. «Смотрели» определялось по номеру колонки, а у разделителей, имени
        листа и баннеров колонки нет вовсе — такой формат перечитывался бы при КАЖДОМ
        старте бота."""
        await self.add_file()
        await self.run_fill()
        self.assertIsNone(await self.store.brand_col_for("hash-1"))
        self.assertTrue(await self.store.brand_scanned("hash-1"))

    async def test_the_admin_choice_survives_the_rescan(self):
        await self.add_file()
        await self.run_fill()
        await self.store.set_marks_by_signature(
            "hash-1", [{"brand": "CLASSEN", "parse": True, "tm_code": "000000104",
                        "tm_name": "Classen / Классен", "discount": 12.5}])
        # Поднялась версия правила — перечитываем, но решение админа не трогаем.
        await self.store.set_signature_brand_col("hash-1", None,
                                                 "разделитель по справочнику", 1)
        self.assertEqual(await self.run_fill(), 1)

        rows = {m.brand: (m.parse, m.discount) for m in await self.store.marks_for("hash-1")}
        self.assertEqual(rows["CLASSEN"], (True, 12.5))
        self.assertEqual(rows["ULTRAFLOOR"], (False, None))
class XlsImagesTest(unittest.TestCase):
    """Картинки из СТАРОГО `.xls` — то, чего не умеет openpyxl.

    Боевой случай: прайс Линдервуда, логотип «Peli» на строке 5 и «LINDERWOOD» на 63. Файл
    в тесты не кладём (он 4 МБ и лежит у админа), поэтому проверяется разбор — на собранных
    вручную байтах формата.
    """

    @staticmethod
    def anchor(row: int, column: int = 0) -> bytes:
        """Запись ClientAnchor: тип 0xF010, длина 18, строка в четвёртом поле."""
        return (b"\x00\x00\x10\xf0\x12\x00\x00\x00"
                + struct.pack("<9H", 0, column, 0, row - 1, 0, column + 1, 0, row, 0))

    @staticmethod
    def opt(pib: int) -> bytes:
        """Запись OPT с единственным свойством — ссылкой на картинку."""
        body = struct.pack("<HI", 0x0104, pib)
        return struct.pack("<HHI", (1 << 4) | 3, 0xF00B, len(body)) + body

    def test_a_shape_takes_the_picture_named_above_it(self):
        from src.price_tool.xls_images import _scan_shapes

        glued = self.opt(2) + self.anchor(5) + self.opt(7) + self.anchor(63)
        self.assertEqual(_scan_shapes(glued), [(5, 2), (63, 7)])

    def test_an_anchor_without_a_picture_keeps_its_row(self):
        from src.price_tool.xls_images import _scan_shapes

        self.assertEqual(_scan_shapes(self.anchor(9)), [(9, 0)])

    def test_scanning_survives_broken_nesting(self):
        """РЕГРЕССИЯ. Обход контейнеров ломался на первой же неожиданной длине и терял
        остаток буфера: на боевом файле так пропали 19 якорей из 27, и среди них обе марки.
        Сканирование по заголовкам к этому безразлично."""
        from src.price_tool.xls_images import _scan_shapes

        broken = struct.pack("<HHI", 0x000F, 0xF003, 1 << 30)   # контейнер с дикой длиной
        glued = broken + self.opt(3) + self.anchor(12)
        self.assertEqual(_scan_shapes(glued), [(12, 3)])

    def test_picture_is_cut_by_its_signature(self):
        from src.price_tool.xls_images import _picture

        png = b"\x89PNG\r\n\x1a\n" + b"nice"
        self.assertEqual(_picture(b"\x00" * 17 + png), (png, "image/png"))
        self.assertEqual(_picture(b"\x11" * 17 + b"\xff\xd8\xff" + b"x")[1], "image/jpeg")

    def test_a_signature_deep_inside_is_not_a_header(self):
        """Подпись формата встречается и в данных; заголовок блипа короткий, и дальше него
        совпадение не считается."""
        from src.price_tool.xls_images import _picture

        self.assertEqual(_picture(b"\x00" * 500 + b"\x89PNG\r\n\x1a\n"), (b"", ""))

    def test_other_formats_are_left_alone(self):
        from src.price_tool.xls_images import is_xls, xls_images

        self.assertFalse(is_xls(b"PK\x03\x04" + b"0" * 10))      # это .xlsx
        self.assertFalse(is_xls(b"<html><body>"))                 # а это «эксель из HTML»
        self.assertEqual(xls_images(b"PK\x03\x04" + b"0" * 10), {})

    def test_a_broken_file_is_not_an_error(self):
        """Отсутствие баннеров — самый обычный прайс, а не сбой."""
        from src.price_tool.xls_images import xls_images

        self.assertEqual(xls_images(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64), {})


class HeaderLogoTest(unittest.TestCase):
    """Логотип выше строки заголовков — шапка, а не разделитель (прайс Линдервуда)."""

    SHEET = Sheet(name="Прайс Москва", rows=[
        [],                                     # сырая 1 — над ней логотип поставщика
        ["Коллекция", "Артикул", "Название"],   # сырая 2 — заголовки
        [],                                     # сырая 3 — под ней логотип Peli
        ["VN-511", "Ван Браун", "1 290"],
        ["VN-512", "Ван Грей", "1 290"],
        [],                                     # сырая 6 — под ней логотип LINDERWOOD
        ["LQ-01", "Адана", "1 100"],
    ])

    def test_supplier_logo_does_not_eat_the_header(self):
        """У Линдервуда логотип поставщика стоит выше заголовков. Приняв его за начало
        блока, мы отдали бы ему строку с названиями колонок — и выбор другого бренда оставил
        бы модель без заголовков."""
        spot = brand_map(self.SHEET, {"Прайс Москва": {1: "LINDERWOOD", 3: "Peli",
                                                       6: "LINDERWOOD"}})
        self.assertEqual(spot.mode, BY_IMAGE)
        # Шапка считается в НЕПУСТЫХ строках: пустые, над которыми висят картинки, в неё
        # не входят — их не видит и `render_preview`.
        self.assertEqual(spot.header_rows, 1)
        self.assertEqual(dict(brands_in_rows(spot)), {"Peli": 2, "LINDERWOOD": 1})

        kept = only_brand_rows(self.SHEET, spot, ["Peli"])
        self.assertEqual(kept.rows[0], ["Коллекция", "Артикул", "Название"])
        self.assertEqual(len(kept.rows), 3)

    def test_a_single_logo_in_the_header_separates_nothing(self):
        """У FLOOR SERVICE и Most Floor по одной картинке на лист — это шапка."""
        self.assertIsNone(brand_map(self.SHEET, {"Прайс Москва": {1: "Most Floor"}}))

if __name__ == "__main__":
    unittest.main()
