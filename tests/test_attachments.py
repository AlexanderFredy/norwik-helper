import io
import unittest

from openpyxl import Workbook

from src.email_tool.attachments import excel_sheet_names, extract_text


def _make_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Прайс"
    ws.append(["Артикул", "Наименование", "Цена"])
    ws.append(["8633", "Ламинат Kronospan Castello 8633", 1250.50])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class AttachmentsTest(unittest.TestCase):
    def test_xlsx_extract(self) -> None:
        text = extract_text("price.xlsx", _make_xlsx())
        self.assertIn("Лист: Прайс", text)
        self.assertIn("8633", text)
        self.assertIn("1250.5", text)

    def test_xlsx_sheet_names(self) -> None:
        self.assertEqual(excel_sheet_names(_make_xlsx()), ["Прайс"])

    def test_csv_extract(self) -> None:
        content = "Артикул;Цена\n8633;1250,50\n".encode("cp1251")
        text = extract_text("price.csv", content)
        self.assertIn("8633", text)
        self.assertIn("Артикул", text)

    def test_txt_extract(self) -> None:
        self.assertEqual(extract_text("note.txt", "остатки: 10 уп".encode()), "остатки: 10 уп")

    def test_legacy_xls_message(self) -> None:
        text = extract_text("old.xls", b"\xd0\xcf\x11\xe0")
        self.assertIn(".xls", text)
        self.assertIn(".xlsx", text)

    def test_broken_file_no_crash(self) -> None:
        text = extract_text("broken.xlsx", b"not a real xlsx")
        self.assertIn("Ошибка чтения", text)

    def test_unknown_extension(self) -> None:
        text = extract_text("archive.zip", b"PK")
        self.assertIn("не поддерживается", text)


if __name__ == "__main__":
    unittest.main()
