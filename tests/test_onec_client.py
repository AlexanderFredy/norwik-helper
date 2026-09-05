"""Разбор ответа by-tm клиентом (§8, §19.3).

Главное здесь — ответ с ОШИБКОЙ ПАРАМЕТРОВ. На боевом сервисе он приходил массивом, а не
структурой, из-за чего сериализация на стороне 1С падала в HTTP 500 с HTML-страницей IIS.
После правки `ПолучитьТоварыПоТМкВыгрузкеНаСайт` (specs/1c/by-tm.bsl) ошибка приходит в том
же конверте, что удачный ответ, и клиент обязан её разобрать, а не сломаться.
"""
import json
import unittest

import httpx

from src.onec.client import OnecClient


def _client(payload: dict, *, bom: bool = True) -> OnecClient:
    """Клиент, чей транспорт всегда отдаёт заданный JSON. 1С шлёт UTF-8 с BOM."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if bom:
        body = b"\xef\xbb\xbf" + body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body,
                              headers={"Content-Type": "application/json"})

    c = OnecClient("http://example.invalid/api", "token")
    c._client = httpx.Client(base_url="http://example.invalid/api",
                             transport=httpx.MockTransport(handler))
    return c


ERROR_ENVELOPE = {
    "tm": "", "total": 0, "offset": 1, "limit": 0, "items": [],
    "errors": [{"ref": "", "code": "tm_not_found",
                "message": "Не найдена торговая марка с кодом 000009999"}],
}

OK_ENVELOPE = {
    "tm": "CAMSAN", "total": 1, "offset": 1, "limit": 200,
    "items": [{
        "ref": "YO-00069316", "id": "143693", "name": "CAMSAN Platinum Plus Дуб Милас",
        "article": " 123 ", "unit": "м2", "size": "1380x190x10",
        "product_type": "Виниловый ламинат ", "collection": "Platinum Plus",
        "parent": {"code": "YO-00069287", "name": "PLATINUM+"},
        "alt_units": [{"упак": 1.84}],
        "prices": [{"purchase": {"value": 821, "date": "2025-03-01"}},
                   {"rrc": {"value": 1900, "date": "2023-11-16"}}],
    }],
    "errors": [],
}


class ErrorEnvelopeTest(unittest.TestCase):
    def test_error_response_parses_instead_of_raising(self):
        page = _client(ERROR_ENVELOPE).by_tm("000009999")
        self.assertEqual(page.items, [])
        self.assertEqual(page.total, 0)

    def test_error_reaches_caller_with_code(self):
        """Молча терять причину нельзя: сопоставление с прайсом окажется неполным."""
        page = _client(ERROR_ENVELOPE).by_tm("000009999")
        self.assertEqual(len(page.errors), 1)
        self.assertEqual(page.errors[0]["code"], "tm_not_found")


class OkEnvelopeTest(unittest.TestCase):
    def test_fields_are_mapped(self):
        page = _client(OK_ENVELOPE).by_tm("000000302")
        self.assertEqual(page.tm, "CAMSAN")
        self.assertEqual(page.total, 1)
        item = page.items[0]
        self.assertEqual(item.ref, "YO-00069316")
        self.assertEqual(item.article, "123")            # .strip() в клиенте
        self.assertEqual(item.collection_ref, "YO-00069287")
        self.assertEqual(item.parent, "PLATINUM+")
        self.assertEqual(item.alt_units, {"упак": 1.84})

    def test_prices_split_by_kind(self):
        item = _client(OK_ENVELOPE).by_tm("000000302").items[0]
        self.assertEqual(item.purchase.value, 821)
        self.assertEqual(item.rrc.date, "2023-11-16")
        self.assertIsNone(item.retail)                   # розницы в ответе нет

    def test_body_without_bom_also_parses(self):
        page = _client(OK_ENVELOPE, bom=False).by_tm("000000302")
        self.assertEqual(page.tm, "CAMSAN")


if __name__ == "__main__":
    unittest.main()
