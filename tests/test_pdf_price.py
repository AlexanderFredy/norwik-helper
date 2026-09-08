"""Разбор PDF-прайса и привязка логотипов к строкам (§19.10).

ПОЧЕМУ ЭТО ПОНАДОБИЛОСЬ. `read_price_file` на PDF возвращал НОЛЬ листов: разбирались
только xlsx, xls и csv. Прайс LINDERWOOD — PDF, и агент его просто не видел; разбор шёл
руками.

Тесты идут по НАСТОЯЩЕМУ файлу из `.claude/test-prices`. Синтетический PDF тут бесполезен:
проверять надо ровно то, на чём механизм ломался, — таблицу без явных линеек, склеенные
колонки и имя коллекции, которого в тексте нет вообще.
"""
import unittest
from pathlib import Path

from src.price_tool.parser import parse_price_table, pdf_image_anchors

NAME = "Прайс-лист LINDERWOOD - Москва.pdf"


def _find() -> Path | None:
    """Ищем прайс вверх по дереву: в worktree и в основном checkout он лежит на разной
    глубине, а тесты обязаны работать из обоих."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".claude" / "test-prices" / NAME
        if candidate.exists():
            return candidate
    return None


PDF = _find()


@unittest.skipUnless(PDF is not None, f"нет файла {NAME}")
class PdfPriceTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = PDF.read_bytes()
        cls.sheets = parse_price_table(cls.data, PDF.name)

    def test_parsed_at_all(self):
        self.assertTrue(self.sheets, "PDF-прайс не разобрался")

    def test_sheet_is_named_by_page(self):
        """Имя листа — часть ключа запомненного маппинга колонок (§6.4), оно обязано быть
        устойчивым. Номер страницы устойчив, порядковый номер таблицы — нет."""
        self.assertEqual(self.sheets[0].name, "стр. 1")

    def test_columns_survive(self):
        """Строки текста склеивали колонки; таблица их сохраняет."""
        header = next(r for r in self.sheets[0].rows if "Артикул" in " ".join(r))
        self.assertIn("Коллекция", header)
        self.assertIn("*РРЦ за м2", " ".join(header))
        self.assertGreater(len(header), 10)

    def test_prices_are_in_cells(self):
        row = next(r for r in self.sheets[0].rows if "LQ-05" in " ".join(r))
        joined = " ".join(row)
        self.assertIn("1 100", joined)      # закупка самовывозом
        self.assertIn("1 690", joined)      # РРЦ

    def test_collection_name_is_absent_from_text(self):
        """Ради этого всё и делается: «Quartz» в тексте прайса НЕТ, он только в логотипе."""
        text = "\n".join(" ".join(r) for s in self.sheets for r in s.rows).lower()
        self.assertNotIn("quartz", text)
        self.assertIn("spc", text)          # в тексте только технология

    def _row_with(self, article: str) -> list[str]:
        return next(r for r in self.sheets[0].rows if article in " ".join(r))

    def test_glued_positions_are_split(self):
        """PDF склеил LE-267 и LE-263 в одну строку таблицы — их надо развести.

        Признак «нет фаски» стоял в такой строке ОДИН раз, и по данным 1С он принадлежит
        LE-263: у LE-267 фаска четырёхсторонняя. Склеенная строка сделала бы признак
        неразложимым, а угадывание испортило бы верную карточку.
        """
        self.assertNotIn("LE-263", " ".join(self._row_with("LE-267")))
        self.assertIn("Нет", self._row_with("LE-263"))
        self.assertNotIn("Нет", self._row_with("LE-267"))
        self.assertIn("Нет", self._row_with("LE-269"))

    def test_wrapped_header_is_not_split(self):
        """Шапка тоже многострочна, но это перенос текста, а не несколько позиций."""
        header = next(r for r in self.sheets[0].rows if "Артикул" in " ".join(r))
        self.assertIn("Название оригинал", header)
        self.assertIn("Тип поверхности", header)

    def test_row_with_one_wrapped_cell_survives(self):
        """Метка класса «8 /32 MM» стоит в объединённой ячейке одна — на ней первая версия
        правила разрезала строку и теряла саму позицию."""
        row = self._row_with("LE-266")
        self.assertIn("8 /32 MM", row)
        self.assertIn("Medio", row)
        self.assertTrue(any("LF-700" in " ".join(r) for r in self.sheets[0].rows))

    def test_images_are_anchored_to_rows(self):
        anchors = pdf_image_anchors(self.data)
        self.assertIn("стр. 1", anchors)
        images = anchors["стр. 1"]
        self.assertGreaterEqual(len(images), 30)
        rows = [r for r, _data, _mt in images]
        self.assertTrue(all(1 <= r <= len(self.sheets[0].rows) for r in rows))

    def test_logo_lands_on_its_section(self):
        """Логотип коллекции должен стоять НА разделе, а не в конце файла."""
        sheet = self.sheets[0]
        spc_row = next(n for n, r in enumerate(sheet.rows, 1)
                       if "Кварцвинил (SPC)" in " ".join(r))
        rows = {r for r, _d, _m in pdf_image_anchors(self.data)["стр. 1"]}
        self.assertTrue(any(spc_row <= r <= spc_row + 8 for r in rows),
                        f"у раздела SPC (строка {spc_row}) нет ни одного логотипа")

    def test_images_are_cheap_enough_to_send_whole(self):
        """Замер, на котором держится решение «отдавать все» (§19.10): 40 штук ≈ 560
        токенов. Если прайс вдруг станет дороже — тест это покажет."""
        from PIL import Image
        import io as _io
        total = 0
        for _row, data, _mt in pdf_image_anchors(self.data)["стр. 1"]:
            with Image.open(_io.BytesIO(data)) as im:
                total += im.width * im.height
        self.assertLess(total / 750, 2000, "картинки прайса стали дороже бюджета")


if __name__ == "__main__":
    unittest.main()
