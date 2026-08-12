"""Баннеры-логотипы из прайса доезжают до модели картинкой, а не теряются.

Кейс из боевого прогона: в прайсе Most Floor логотип второго бренда вставлен картинкой,
и раздел другой ТМ выглядел продолжением предыдущей — агент решил, что позиций этого
бренда в прайсе нет.
"""
import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage

from src.agent import pricing_tools as pt
from src.agent.pricing_tools import PricingTools
from src.storage.pricing import PricingStore

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg==")


def png(color: tuple[int, int, int]) -> bytes:
    """Разные картинки — разные байты (одинаковые схлопывает дедуп)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buf, format="PNG")
    return buf.getvalue()


def workbook(image_rows=(), images=None, rows=20) -> bytes:
    """images — байты картинок по позициям image_rows (по умолчанию одна и та же PNG)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Ламинат"
    ws.append(["Артикул", "Коллекция", "Цена"])
    for i in range(1, rows + 1):
        ws.append([f"77{i:02d}", "Миллениум", 1000 + i])
    for n, row in enumerate(image_rows):
        data = images[n] if images else PNG
        ws.add_image(XLImage(io.BytesIO(data)), f"A{row}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class PriceImagesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.tools = PricingTools(onec=None, store=self.store, user_id=1)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _read(self, content: bytes, name="price.xlsx"):
        self.tools.set_file(name, content)
        return await self.tools.execute("read_price_file", {})

    def _text(self, out) -> str:
        return out[0]["text"] if isinstance(out, list) else out

    async def test_image_attached_as_block_and_marked_in_text(self):
        out = await self._read(workbook(image_rows=[12]))
        self.assertIsInstance(out, list)
        images = [b for b in out if b["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(images[0]["source"]["data"]), PNG)
        self.assertIn("⟨ИЗОБРАЖЕНИЕ #1, привязано к строке 12", self._text(out))

    async def test_marker_sits_between_the_right_rows(self):
        lines = self._text(await self._read(workbook(image_rows=[12]))).splitlines()
        i = next(n for n, l in enumerate(lines) if "ИЗОБРАЖЕНИЕ" in l)
        self.assertIn("7710", lines[i - 1])       # строка 12 листа = артикул 7710
        self.assertIn("7711", lines[i + 1])

    async def test_no_images_returns_plain_text(self):
        """Прайсы без картинок не должны менять форму результата."""
        out = await self._read(workbook())
        self.assertIsInstance(out, str)
        self.assertNotIn("ИЗОБРАЖЕНИЕ", out)

    async def test_csv_is_not_probed_for_images(self):
        out = await self._read(b"a;b\n1;2\n", name="price.csv")
        self.assertIsInstance(out, str)

    async def test_oversized_image_marked_but_not_attached(self):
        with patch.object(pt, "MAX_IMAGE_BYTES", 10):
            out = await self._read(workbook(image_rows=[12]))
        self.assertIsInstance(out, str)                    # прикладывать нечего
        self.assertIn("слишком большое", out)

    async def test_image_count_capped(self):
        three = [png((255, 0, 0)), png((0, 255, 0)), png((0, 0, 255))]
        with patch.object(pt, "MAX_IMAGES", 2):
            out = await self._read(workbook(image_rows=[5, 10, 15], images=three))
        self.assertEqual(len([b for b in out if b["type"] == "image"]), 2)
        self.assertEqual(self._text(out).count("исчерпан лимит"), 1)

    async def test_total_budget_respected(self):
        two = [png((255, 0, 0)), png((0, 255, 0))]
        with patch.object(pt, "MAX_IMAGE_TOTAL_BYTES", len(two[0])):
            out = await self._read(workbook(image_rows=[5, 10], images=two))
        self.assertEqual(len([b for b in out if b["type"] == "image"]), 1)

    async def test_duplicate_logo_attached_once(self):
        """У Монарха один логотип вставлен 93 раза — лимит не должен уходить на копии."""
        out = await self._read(workbook(image_rows=[5, 10, 15]))     # одна и та же PNG
        self.assertEqual(len([b for b in out if b["type"] == "image"]), 1)
        text = self._text(out)
        self.assertEqual(text.count("ИЗОБРАЖЕНИЕ #1,"), 1)           # приложено один раз
        self.assertEqual(text.count("ИЗОБРАЖЕНИЕ #1 ещё раз"), 2)    # два повтора

    async def test_image_dropped_when_its_marker_is_truncated_away(self):
        """Картинка без маркера в тексте — это картинка без объяснения: не отправляем."""
        content = workbook(image_rows=[300], rows=400)
        with patch.object(pt, "MAX_TEXT_CHARS", 200):
            out = await self._read(content)
        self.assertIsInstance(out, str)
        self.assertNotIn("ИЗОБРАЖЕНИЕ", out)

    async def test_markers_do_not_break_remembered_mapping(self):
        """Сигнатура формата считается по исходному файлу — маркеры её сдвигать не должны."""
        content = workbook(image_rows=[12])
        await self._read(content)
        await self.tools.execute("save_price_mapping", {
            "supplier": "Most Floor", "purchase_column": "Цена"})

        later = PricingTools(onec=None, store=self.store, user_id=1)
        later.set_file("price-next-month.xlsx", content)
        out = await later.execute("read_price_file", {})
        self.assertIn("ЗАПОМНЕННЫЙ МАППИНГ", self._text(out))

    async def test_blocks_are_json_serializable(self):
        """История диалога уезжает в SQLite как JSON — bytes туда попасть не должны."""
        import json
        out = await self._read(workbook(image_rows=[12]))
        json.dumps(out)


if __name__ == "__main__":
    unittest.main()
