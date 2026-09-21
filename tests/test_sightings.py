"""Журнал встреч артикулов в прайсах (`src/storage/sightings.py`).

Смысл журнала один: отличить «позиции нет в ЭТОМ прайсе» от «позиция снята с
производства». Всё остальное — следствия этого различия.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.storage.sightings import SIGHTING_TTL_DAYS, SightingStore


class Base(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SightingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()


class RememberTest(Base):

    async def test_sighting_of_another_supplier_is_visible(self):
        await self.store.remember(7, "sig", {"3309": "Миллениум Про"},
                                  supplier="Паркет-Холл", price_date="2026-09-15")

        seen = await self.store.elsewhere(exclude_supplier_id=1)
        self.assertIn("3309", seen)
        self.assertEqual(seen["3309"].supplier, "Паркет-Холл")
        self.assertIn("Паркет-Холл", seen["3309"].label())
        self.assertIn("2026-09-15", seen["3309"].label())

    async def test_own_sightings_are_excluded(self):
        """Вопрос всегда один: возит ли это КТО-ТО ЕЩЁ."""
        await self.store.remember(7, "sig", {"3309": "Миллениум Про"}, supplier="Свой")
        self.assertEqual(await self.store.elsewhere(exclude_supplier_id=7), {})

    async def test_new_price_replaces_the_old_one(self):
        """«Есть у другого» обязано значить «есть в его ПОСЛЕДНЕМ прайсе»: иначе позиция,
        выкинутая поставщиком, подтверждала бы себя вечно."""
        await self.store.remember(7, "sig", {"3309": "Про", "3310": "Про"},
                                  supplier="Паркет-Холл")
        await self.store.remember(7, "sig", {"3309": "Про"}, supplier="Паркет-Холл")

        seen = await self.store.elsewhere(exclude_supplier_id=1)
        self.assertIn("3309", seen)
        self.assertNotIn("3310", seen)

    async def test_another_section_of_the_same_supplier_survives(self):
        """Поставщик шлёт прайс частями: ламинат отдельно, плитка отдельно. Замена по
        одному лишь поставщику стёрла бы соседний раздел, которого в файле и не было."""
        await self.store.remember(7, "ламинат", {"3309": "Про"}, supplier="Х")
        await self.store.remember(7, "плитка", {"A001": "Кармен"}, supplier="Х")
        await self.store.remember(7, "ламинат", {"3311": "Про"}, supplier="Х")

        seen = await self.store.elsewhere(exclude_supplier_id=1)
        self.assertEqual(set(seen), {"A001", "3311"})

    async def test_silent_supplier_stops_confirming(self):
        """Год без прайсов — поставщика больше нет, и держать снятие он не должен."""
        await self.store.remember(7, "sig", {"3309": "Про"}, supplier="Х")
        later = datetime.now(timezone.utc) + timedelta(days=SIGHTING_TTL_DAYS + 1)
        self.assertEqual(await self.store.elsewhere(1, today=later), {})

    async def test_forget_supplier(self):
        await self.store.remember(7, "sig", {"3309": "Про"}, supplier="Х")
        self.assertEqual(await self.store.forget_supplier(7), 1)
        self.assertEqual(await self.store.elsewhere(1), {})

    async def test_supplierless_price_is_not_recorded(self):
        """Без поставщика строка бессмысленна: вопрос журнала — «у КОГО видели»."""
        self.assertEqual(await self.store.remember(0, "sig", {"3309": "Про"}), 0)


if __name__ == "__main__":
    unittest.main()
