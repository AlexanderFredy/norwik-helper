"""Картинки из PDF-прайса (§6.7 для PDF)."""
import unittest
from pathlib import Path

from src.price_tool.parser import pdf_images

PRICE = Path(r"C:\Data\ClodeCodeProjects\shop-helper\.claude\test-prices"
             r"\Прайс-лист LINDERWOOD - Москва.pdf")


class PdfImagesTest(unittest.TestCase):
    def test_broken_content_returns_empty(self):
        """Разбор прайса не должен падать из-за картинок — как и весь parser."""
        self.assertEqual(pdf_images(b"not a pdf at all"), [])
        self.assertEqual(pdf_images(b""), [])

    @unittest.skipUnless(PRICE.exists(), "нет боевого прайса LINDERWOOD")
    def test_real_price_list_has_logos(self):
        found = pdf_images(PRICE.read_bytes())
        self.assertGreater(len(found), 10)
        page, data, media = found[0]
        self.assertEqual(page, 1)
        self.assertTrue(data)
        self.assertIn(media, ("image/png", "image/jpeg"))

    @unittest.skipUnless(PRICE.exists(), "нет боевого прайса LINDERWOOD")
    def test_limit_respected(self):
        """Лимит нужен: страница этого прайса несёт 40 логотипов (§9.6.3)."""
        self.assertEqual(len(pdf_images(PRICE.read_bytes(), limit=5)), 5)


if __name__ == "__main__":
    unittest.main()
