"""Инвентаризация свойства «Коллекция» по каталогу (`/empty_collections`).

Сверка по прайсу считает то же самое, но только там, куда дотянулся прайс. Здесь — обход
каталога целиком, и считает его КОД: решать нечего, свойство либо заполнено, либо нет.
"""
import unittest

from src.model import collection_audit as audit
from src.onec.client import NomItem


def item(ref="T1", collection="", parent="Adventure WR 1290x193x8", size="1290x193x8",
         product_type="Ламинат", name="Ламинат Classen Adventure WR Дуб Авола",
         not_exported=False):
    return NomItem(
        ref=ref, id="", name=name, article="", unit="м2", size=size,
        product_type=product_type, collection=collection, parent=parent,
        collection_ref="F1", alt_units={}, purchase=None, retail=None, rrc=None,
        not_exported=not_exported)


class ScanTest(unittest.TestCase):

    def test_filled_property_is_not_a_gap(self):
        gaps = audit.scan("Classen", [item(collection="Adventure WR")])
        self.assertEqual(gaps, [])

    def test_empty_property_is_counted(self):
        gaps = audit.scan("Classen", [item(ref="A"), item(ref="B")])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].count, 2)
        self.assertEqual(gaps[0].tm, "Classen")

    def test_whitespace_is_empty(self):
        """«Пробел» в свойстве — это незаполненное поле, а не значение."""
        self.assertEqual(audit.scan("Classen", [item(collection="   ")])[0].count, 1)

    def test_collection_comes_from_the_folder_without_the_size(self):
        """Имя папки несёт размер по §19.5 — в адрес он не идёт, иначе адрес не совпадёт
        с тем, которым адресуются задачи."""
        gap = audit.scan("Classen", [item()])[0]
        self.assertEqual(gap.collection, "Adventure WR")
        self.assertEqual(gap.where, "Classen / Adventure WR")

    def test_discontinued_are_not_counted(self):
        """Снятые не правят: работа по ним — перенос в папки снятых, а не свойства
        (решение админа 21.09.2026). Показав их, предложили бы работу, которой нет."""
        gaps = audit.scan("Classen", [item(ref="A", not_exported=True), item(ref="B")])
        self.assertEqual(gaps[0].count, 1)
        self.assertEqual(gaps[0].sample, ("B",))

    def test_no_folder_no_name(self):
        """Ни свойства, ни папки — заполнять нечем, и это отдельная строка, а не общая
        куча: по имени коллекции такую позицию не проставить."""
        gap = audit.scan("Classen", [item(parent="", size="")])[0]
        self.assertEqual(gap.collection, audit.UNKNOWN)

    def test_same_name_different_product_type_is_two_rows(self):
        """Одноимённые коллекции разных видов — разные коллекции, заполняют их порознь."""
        rows = [item(ref="A", parent="Alpha", size="", product_type="Ламинат"),
                item(ref="B", parent="Alpha", size="", product_type="Плитка")]
        self.assertEqual(len(audit.scan("Classen", rows)), 2)

    def test_biggest_gap_comes_first(self):
        rows = [item(ref="A", parent="Alpha", size="")]
        rows += [item(ref=f"B{i}", parent="Beta", size="") for i in range(3)]
        gaps = audit.scan("Classen", rows)
        self.assertEqual([g.collection for g in gaps], ["Beta", "Alpha"])

    def test_sample_is_bounded(self):
        """Образец кодов, а не перечень: полный список в чате не читают."""
        rows = [item(ref=f"R{i}") for i in range(10)]
        self.assertEqual(len(audit.scan("Classen", rows)[0].sample), audit.SAMPLE)


class RenderTest(unittest.TestCase):

    def gaps(self, **kw):
        return audit.scan("Classen", [item(**kw)])

    def test_empty_report_is_still_printed(self):
        """Админ сам спросил — «дыр нет» это ответ, а тишина читалась бы как несработавшая
        команда. (В сводке менеджерам правило обратное, и это не противоречие: там никто
        не спрашивал.)"""
        text = audit.render([], marks=12, live=3418)
        self.assertIn("проставлено везде", text)
        self.assertIn("12", text)

    def test_counts_are_in_the_header(self):
        text = audit.render(self.gaps(), marks=1, live=9)
        self.assertIn("не проставлено: 1 поз.", text)
        self.assertIn("Classen / Adventure WR", text)

    def test_lost_positions_are_named(self):
        """Отчёт по неполной выгрузке выглядит как полный — молчать об этом нельзя."""
        text = audit.render([], marks=2, live=10, lost=7)
        self.assertIn("не отдала 7", text)

    def test_failed_marks_are_named(self):
        text = audit.render([], marks=1, live=10, failed=["Peli"])
        self.assertIn("Peli", text)

    def test_scope_is_shown(self):
        self.assertIn("«Peli»", audit.render([], marks=1, live=5, scope="Peli"))

    def test_fix_is_suggested_only_when_there_is_work(self):
        self.assertIn("изменение свойств", audit.render(self.gaps(), marks=1, live=1))
        self.assertNotIn("изменение свойств", audit.render([], marks=1, live=1))


class SplitTest(unittest.TestCase):

    def test_short_text_stays_one_message(self):
        self.assertEqual(audit.split("одна строка"), ["одна строка"])

    def test_split_happens_on_line_boundaries(self):
        text = "\n".join(f"— марка / коллекция {i}: 5" for i in range(400))
        parts = audit.split(text)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 3900)
            # ни один обрывок не начинается и не кончается посреди строки
            self.assertTrue(part.startswith("—"))
            self.assertTrue(part.rstrip().endswith("5"))

    def test_one_endless_line_is_cut_anyway(self):
        parts = audit.split("x" * 9000)
        self.assertEqual(len(parts), 3)
        self.assertEqual("".join(parts), "x" * 9000)

    def test_nothing_is_lost(self):
        text = "\n".join(str(i) for i in range(2000))
        self.assertEqual("\n".join(audit.split(text)), text)


class FakeMessage:
    def __init__(self):
        self.sent: list[str] = []
        self.status: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)
        return self

    async def edit_text(self, text):
        self.status.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.sent)


class Args:
    def __init__(self, args=""):
        self.args = args


class FakeMark:
    def __init__(self, name, code):
        self.name, self.code = name, code


class FakeNom:
    def __init__(self, items, errors=()):
        self.items, self.errors = list(items), list(errors)


class FakeOnec:
    """1С, которая на одной из марок падает — так ведёт себя живой канал."""

    def __init__(self, dumps, marks=None, broken=()):
        self._dumps, self._broken = dumps, set(broken)
        self.marks = marks or [FakeMark("Classen", "1"), FakeMark("Peli", "2")]
        self.asked: list[str] = []
        self.all_marks = None

    def selling_tm(self, all_marks=False):
        self.all_marks = all_marks
        return list(self.marks)

    def by_tm_all(self, code):
        self.asked.append(code)
        if code in self._broken:
            raise RuntimeError("1С не ответила")
        return self._dumps.get(code, FakeNom([]))


class CommandTest(unittest.IsolatedAsyncioTestCase):
    """Обход по маркам: другого входа в номенклатуру нет — `by-tm` требует марку."""

    async def run_cmd(self, onec, args="", is_admin=True):
        from src.bot.catalog_handlers import cmd_empty_collections
        msg = FakeMessage()
        await cmd_empty_collections(msg, Args(args), onec, is_admin=is_admin)
        return msg

    async def test_gaps_of_all_marks_land_in_one_report(self):
        onec = FakeOnec({"1": FakeNom([item(ref="A"), item(ref="B")]),
                         "2": FakeNom([item(ref="C", parent="Vintage", size="")])})
        msg = await self.run_cmd(onec)
        self.assertIn("Classen / Adventure WR", msg.text)
        self.assertIn("Peli / Vintage", msg.text)
        self.assertEqual(onec.asked, ["1", "2"])

    async def test_broken_mark_does_not_bury_the_walk(self):
        """Канал теряет запросы: отчёт по остальным маркам полезнее, чем ошибка вместо
        всего. Но упавшую марку обязаны назвать — иначе её дыры сойдут за отсутствие дыр."""
        onec = FakeOnec({"2": FakeNom([item(ref="C")])}, broken={"1"})
        msg = await self.run_cmd(onec)
        self.assertIn("Peli / Adventure WR", msg.text)
        self.assertIn("Не удалось выгрузить марки: Classen", msg.text)
        self.assertIn("марок 1", msg.text, "упавшая в число просмотренных не идёт")

    async def test_lost_positions_reach_the_report(self):
        onec = FakeOnec({"1": FakeNom([item()], errors=[{"ref": "X"}])})
        self.assertIn("не отдала 1", (await self.run_cmd(onec)).text)

    async def test_named_mark_is_looked_up_even_if_not_selling(self):
        """Назвали марку — смотрим её, помечена она к выгрузке или нет."""
        onec = FakeOnec({"2": FakeNom([item()])})
        msg = await self.run_cmd(onec, args="peli")
        self.assertTrue(onec.all_marks, "спросили и непомеченные")
        self.assertEqual(onec.asked, ["2"], "чужие марки не обходим")
        self.assertIn("«peli»", msg.text)

    async def test_unknown_mark_is_said_out_loud(self):
        msg = await self.run_cmd(FakeOnec({}), args="нетакой")
        self.assertIn("не нашлось", msg.text)

    async def test_without_argument_only_selling_marks(self):
        onec = FakeOnec({})
        await self.run_cmd(onec)
        self.assertFalse(onec.all_marks)

    async def test_progress_is_shown(self):
        """Молчащая три минуты команда читается как зависшая."""
        msg = await self.run_cmd(FakeOnec({}))
        self.assertTrue(any("из 2" in s for s in msg.status))

    async def test_not_admin(self):
        msg = await self.run_cmd(FakeOnec({}), is_admin=False)
        self.assertIn("только администратору", msg.text)

    async def test_without_1c_says_so(self):
        msg = await self.run_cmd(None)
        self.assertIn("не настроена", msg.text)

    async def test_long_report_is_split(self):
        rows = [item(ref=f"R{i}", parent=f"Коллекция номер {i}", size="")
                for i in range(300)]
        msg = await self.run_cmd(FakeOnec({"1": FakeNom(rows)}))
        self.assertGreater(len(msg.sent), 2, "первое сообщение — статус обхода")


if __name__ == "__main__":
    unittest.main()
