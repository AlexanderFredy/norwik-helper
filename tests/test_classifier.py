import unittest

from src.email_tool.classifier import MailKind, classify, parse_signature


class ClassifyTest(unittest.TestCase):
    def test_price_by_subject(self) -> None:
        self.assertIs(classify("Прайс-лист июнь 2026", []), MailKind.PRICE)
        self.assertIs(classify("Новые цены с 01.07", []), MailKind.PRICE)
        self.assertIs(classify("Price list June", []), MailKind.PRICE)

    def test_stock_by_subject(self) -> None:
        self.assertIs(classify("Остатки на складе", []), MailKind.STOCK)
        self.assertIs(classify("Наличие товара", []), MailKind.STOCK)

    def test_by_attachment_name(self) -> None:
        self.assertIs(classify("Добрый день", ["price_list.xlsx"]), MailKind.PRICE)
        self.assertIs(classify("Документы", ["остатки_июнь.xlsx"]), MailKind.STOCK)

    def test_by_sheet_names(self) -> None:
        self.assertIs(
            classify("Файл", ["data.xlsx"], excel_sheet_names=["Прайс"]),
            MailKind.PRICE,
        )
        self.assertIs(
            classify("Файл", ["data.xlsx"], excel_sheet_names=["Склад Москва"]),
            MailKind.STOCK,
        )

    def test_subject_has_priority_over_attachment(self) -> None:
        self.assertIs(classify("Прайс-лист", ["остатки.xlsx"]), MailKind.PRICE)

    def test_ambiguous_falls_through(self) -> None:
        # В теме и прайс, и остатки — неоднозначно, смотрим вложение
        self.assertIs(
            classify("Прайс и остатки", ["stock_msk.xlsx"]), MailKind.STOCK
        )

    def test_unknown(self) -> None:
        self.assertIs(classify("Добрый день", ["letter.pdf"]), MailKind.UNKNOWN)


class ParseSignatureTest(unittest.TestCase):
    def test_standard_signature(self) -> None:
        body = (
            "Добрый день!\nВо вложении актуальный прайс.\n\n"
            "С уважением,\nИван Петров\nООО Паркет-Опт\n"
            "тел. +7 (495) 123-45-67"
        )
        contact = parse_signature(body)
        self.assertEqual(contact.name, "Иван Петров")
        self.assertEqual(contact.phone, "+7 (495) 123-45-67")

    def test_dash_marker(self) -> None:
        body = "Текст письма\n--\nМария Сидорова\n8 912 345-67-89"
        contact = parse_signature(body)
        self.assertEqual(contact.name, "Мария Сидорова")
        self.assertEqual(contact.phone, "8 912 345-67-89")

    def test_no_signature(self) -> None:
        contact = parse_signature("Привет, лови файл")
        self.assertIsNone(contact.name)
        self.assertIsNone(contact.phone)

    def test_phone_without_name(self) -> None:
        contact = parse_signature("Заказ принят\n+79161234567")
        self.assertEqual(contact.phone, "+79161234567")
        self.assertIsNone(contact.name)


if __name__ == "__main__":
    unittest.main()
