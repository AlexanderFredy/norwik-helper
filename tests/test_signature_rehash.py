"""Пересчёт хешей форматов после смены правила скелета (правка 02.10.2026).

Правило подсчёта сигнатуры изменилось: скелет перестал тащить в хеш данные файла. Формат
прежний, значение хеша другое — и всё, что на нём держится, осиротело бы молча: выбор листов
к разбору, запомненные колонки цен, журнал встреч артикулов, опознание владельца формата.

Проверяем ровно то, ради чего пересчёт и сделан: решение админа о листах переживает смену
правила, а повторный запуск ничего не делает.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from src.model.signature_rehash import rehash_signatures
from src.price_tool.signature import price_signature
from src.storage.suppliers import SupplierStore


class Sheet:
    """Лист с нормальной шапкой: по нему и считается хеш нового правила."""

    def __init__(self, name="Прайс"):
        self.name = name
        self.rows = [["Замок", "Размер мм", "Класс"], ["5G", "1290x190x8", "33"]]


SHEETS = [Sheet()]
FRESH = price_signature(SHEETS)


class Pricing:
    """Память колонок: нужен только перевес хеша."""

    def __init__(self):
        self.moves = []

    async def rehash_signature(self, old, new):
        self.moves.append((old, new))
        return 1


class Broken(Pricing):
    async def rehash_signature(self, old, new):
        raise RuntimeError("хранилище недоступно")


class RehashTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.store = SupplierStore(root / "db.sqlite")
        await self.store.init()

        self.file = root / "price.xlsx"
        self.file.write_bytes(b"not a real book")

        self.supplier = await self.store.add_supplier("Стройиндустрия (Лиля)")
        self.sig = await self.store.add_signature(
            self.supplier.id, "старый-хеш", sample_name="price.xlsx")
        await self.store.add_price_file(self.sig.id, "price.xlsx", str(self.file))
        await self.store.set_signature_sheets(self.sig.id, "Прайс")

        # Разбор книги подменяем: файл на диске — заглушка, а правило хеша проверяется
        # отдельными тестами. Здесь важен перевес, а не чтение xlsx.
        import src.price_tool.parser as parser
        self.parse = parser.parse_price_table
        parser.parse_price_table = lambda content, name: SHEETS

    async def asyncTearDown(self):
        import src.price_tool.parser as parser
        parser.parse_price_table = self.parse
        self.dir.cleanup()

    async def test_hash_is_recalculated_and_sheets_survive(self):
        pricing = Pricing()
        done = await rehash_signatures(self.store, pricing=pricing)

        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["old"], "старый-хеш")
        self.assertEqual(done[0]["new"], FRESH)
        self.assertEqual(pricing.moves, [("старый-хеш", FRESH)])

        # Главное: указание админа о листах нашлось ПО НОВОМУ хешу — именно им адресуется
        # и форма 1С, и приём следующего прайса.
        self.assertEqual(await self.store.sheets_for(FRESH), "Прайс")
        self.assertEqual(await self.store.sheets_for("старый-хеш"), "")

    async def test_owner_is_found_by_the_new_hash(self):
        """Из-за этого всё и затевалось: следующий прайс обязан найти владельца формата, а
        не завести второго поставщика с именем из имени файла."""
        await rehash_signatures(self.store)
        owners = await self.store.find_signatures(FRESH)
        self.assertEqual([o.supplier_id for o in owners], [self.supplier.id])

    async def test_second_run_does_nothing(self):
        await rehash_signatures(self.store)
        pricing = Pricing()
        self.assertEqual(await rehash_signatures(self.store, pricing=pricing), [])
        self.assertEqual(pricing.moves, [])

    async def test_one_broken_store_does_not_stop_the_others(self):
        """Хуже всего — половина записей под старым хешом, половина под новым."""
        good = Pricing()
        done = await rehash_signatures(self.store, pricing=Broken(), sightings=good)
        self.assertEqual(len(done), 1)
        self.assertEqual(good.moves, [("старый-хеш", FRESH)])
        self.assertEqual(await self.store.sheets_for(FRESH), "Прайс")

    async def test_missing_file_is_left_alone(self):
        """Пересчитать нечем — не гадаем: формат сохраняет своё значение."""
        self.file.unlink()
        self.assertEqual(await rehash_signatures(self.store), [])
        self.assertEqual(await self.store.sheets_for("старый-хеш"), "Прайс")

    async def test_content_hash_is_not_touched(self):
        """`без-разбора:…` — это не скелет, пересчитывать там нечего."""
        raw = await self.store.add_signature(self.supplier.id, "без-разбора:abc",
                                             sample_name="price.xlsx")
        await self.store.add_price_file(raw.id, "price.xlsx", str(self.file))
        done = await rehash_signatures(self.store)
        self.assertEqual([e["old"] for e in done], ["старый-хеш"])

    async def test_two_formats_that_became_one_are_merged(self):
        """Два старых формата, различавшихся только данными, — это ОДИН формат. Пара
        (поставщик, хеш) обязана остаться одной, а решение о листах не потеряться."""
        other = self.file.with_name("price-2.xlsx")
        other.write_bytes(b"not a real book either")
        twin = await self.store.add_signature(self.supplier.id, "второй-хеш",
                                              sample_name="price-2.xlsx")
        await self.store.add_price_file(twin.id, "price-2.xlsx", str(other))

        await rehash_signatures(self.store)

        rows = await self.store.list_signatures(self.supplier.id)
        self.assertEqual([r.signature for r in rows], [FRESH])
        self.assertEqual(await self.store.sheets_for(FRESH), "Прайс")


if __name__ == "__main__":
    unittest.main()
