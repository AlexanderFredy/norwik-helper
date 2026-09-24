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


class PostEncodingTest(unittest.TestCase):
    """Тело записи уходит с явной кодировкой.

    Ценам это было безразлично — в их теле одни коды и числа. Но set-items повезёт
    кириллические наименования, а 1С читает тело через `ПолучитьТелоКакСтроку()`, которая
    без charset в заголовке выбирает кодировку сама. Молча созданный товар с испорченным
    именем откатывать дороже, чем указать кодировку.
    """

    def _capture(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["content_type"] = request.headers.get("content-type")
            seen["body"] = request.content
            return httpx.Response(200, content=b'{"date":"2026-09-08","updated":0,'
                                               b'"unchanged":0,"failed":0,'
                                               b'"results":[],"errors":[]}')

        c = OnecClient("http://example.invalid/api", "token")
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        return c, seen

    def test_charset_is_declared(self):
        c, seen = self._capture()
        c.set_prices([{"ref": "YO-1", "prices": {"purchase": 100}}])
        self.assertEqual(seen["content_type"], "application/json; charset=utf-8")

    def test_cyrillic_body_is_utf8_not_escaped(self):
        c, seen = self._capture()
        c.set_prices([{"ref": "YO-1", "name": "Дуб Милас"}])
        self.assertIn("Дуб Милас".encode("utf-8"), seen["body"])


class SellingTmTest(unittest.TestCase):
    """Все марки против только выгружаемых (§19.10)."""

    def _client(self, payload):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, content=json.dumps(payload).encode("utf-8"))

        c = OnecClient("http://example.invalid/api", "token")
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        return c, seen

    def test_default_asks_only_selling(self):
        c, seen = self._client([{"NameTM": "Peli", "Code": "000000298"}])
        c.selling_tm()
        self.assertNotIn("include_not_exported", seen["url"])

    def test_all_marks_sets_the_flag(self):
        c, seen = self._client([])
        c.selling_tm(all_marks=True)
        self.assertIn("include_not_exported=1", seen["url"])

    def test_selling_flag_parsed(self):
        c, _ = self._client([{"NameTM": "Peli", "Code": "000000298", "Selling": True},
                             {"NameTM": "Linderwood", "Code": "000000999",
                              "Selling": False}])
        tms = c.selling_tm(all_marks=True)
        self.assertTrue(tms[0].selling)
        self.assertFalse(tms[1].selling)

    def test_old_response_without_flag_counts_as_selling(self):
        """Старый обработчик поля не отдаёт — считаем марку выгружаемой, как раньше."""
        c, _ = self._client([{"NameTM": "Peli", "Code": "000000298"}])
        self.assertTrue(c.selling_tm()[0].selling)


# =====================================================================================
# §19.3 / §19.2.4 / §19.2.1 — чтения, нужные только для правки справочника.
# Формы ответов сняты с боевой базы 09.09.2026, не придуманы.
# =====================================================================================

ITEM_ENVELOPE = {
    "tm": "Linderwood", "total": 1, "offset": 1, "limit": 200,
    "items": [{
        "ref": "YO-00078955", "id": "108434",
        "name": "Виниловый ламинат Linderwood Quartz Адана LQ-01",
        "full_name": "Виниловый ламинат Linderwood Quartz Адана LQ-01",
        "site_name": "Адана", "article": "LQ-01", "unit": "м2",
        "size": "1219x228x4", "product_type": "Виниловый ламинат ",
        "product_type_ref": "000000002",
        "length_from": 1219, "length_to": 1219,
        "width_from": 228, "width_to": 228, "thickness": 4,
        "collection": "Quartz", "collection_code": "0004046",
        "parent": {"code": "YO-00078954", "name": "Quartz"},
        "not_exported": False,
        "properties": [
            {"property": "Класс", "code": "0000002",
             "value": "43 класс", "value_code": "0000017"},
            {"property": "Коллекция", "code": "0000003",
             "value": "Quartz", "value_code": "0004046"},
        ],
        "alt_units": [{"упак": 2.23}],
        "prices": [{"purchase": {"value": 1100, "date": "2026-09-08"}}],
    }],
    "errors": [],
}

FOLDERS_ENVELOPE = {
    "total": 3,
    "items": [
        {"ref": "00000000001", "name": "Товары и услуги", "parent_ref": "",
         "kind": "root", "level": 1, "not_exported": False, "deleted": False,
         "product_type_ref": "", "tm_ref": "", "tm_share": 0},
        {"ref": "YO-00002590", "name": "Водостойкий ламинат ", "parent_ref": "00000000001",
         "kind": "type", "level": 2, "not_exported": False, "deleted": False,
         "product_type_ref": "000000002", "tm_ref": "", "tm_share": 0},
        {"ref": "YO-00078954", "name": "Quartz", "parent_ref": "YO-00078953",
         "kind": "collection", "level": 4, "not_exported": False, "deleted": False,
         "product_type_ref": "000000002", "tm_ref": "000000325", "tm_share": 1},
    ],
    "errors": [],
}

PROPS_ENVELOPE = {
    "product_type": "Виниловый ламинат ", "product_type_ref": "000000002",
    "matched_folder": {"code": "0004045", "name": "Linderwood"}, "total": 2,
    "properties": [
        {"property": "Коллекция", "code": "0000003",
         "values": [{"value": "Quartz", "code": "0004046"}]},
        {"property": "Длина", "code": "0000045", "values": []},
    ],
    "errors": [],
}


class ItemFieldsTest(unittest.TestCase):
    """Поля §19.3: без них в товарных режимах нечего сверять с прайсом."""

    def setUp(self):
        self.item = _client(ITEM_ENVELOPE).by_tm("000000325").items[0]

    def test_names_and_site_name(self):
        self.assertEqual(self.item.site_name, "Адана")
        self.assertTrue(self.item.full_name.endswith("Адана LQ-01"))

    def test_dimensions_are_numbers(self):
        self.assertEqual((self.item.length_from, self.item.width_from,
                          self.item.thickness), (1219.0, 228.0, 4.0))

    def test_collection_code_is_value_not_folder(self):
        """`collection_code` — код ЗНАЧЕНИЯ свойства; код папки лежит в `collection_ref`."""
        self.assertEqual(self.item.collection_code, "0004046")
        self.assertEqual(self.item.collection_ref, "YO-00078954")

    def test_properties_parsed(self):
        codes = {p.code: p.value_code for p in self.item.properties}
        self.assertEqual(codes, {"0000002": "0000017", "0000003": "0004046"})

    def test_old_response_without_new_fields_still_parses(self):
        """Ответ прежней 1С обязан разбираться: поля появляются пустыми, а не роняют клиент."""
        item = _client(OK_ENVELOPE).by_tm("000000302").items[0]
        self.assertEqual(item.site_name, "")
        self.assertIsNone(item.thickness)
        self.assertEqual(item.properties, ())

    def test_flag_goes_into_query(self):
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            body = json.dumps(ITEM_ENVELOPE, ensure_ascii=False).encode("utf-8")
            return httpx.Response(200, content=b"\xef\xbb\xbf" + body)

        c = OnecClient("http://example.invalid/api", "token")
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        c.by_tm("000000325", include_not_exported=True, product_type="000000002")
        self.assertEqual(seen.get("include_not_exported"), "1")
        self.assertEqual(seen.get("product_type"), "000000002")

    def test_flag_absent_by_default(self):
        """В ценовом режиме флага быть не должно: снятым с производства цены не пишут."""
        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            body = json.dumps(ITEM_ENVELOPE, ensure_ascii=False).encode("utf-8")
            return httpx.Response(200, content=b"\xef\xbb\xbf" + body)

        c = OnecClient("http://example.invalid/api", "token")
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        c.by_tm("000000325")
        self.assertNotIn("include_not_exported", seen)


class FoldersTest(unittest.TestCase):

    def setUp(self):
        self.tree = _client(FOLDERS_ENVELOPE).folders(tm="000000325")

    def test_nodes_parsed(self):
        self.assertEqual(len(self.tree.items), 3)
        self.assertEqual(self.tree.total, 3)

    def test_name_trimmed(self):
        """Имена в 1С приходят с хвостовым пробелом — на нём ломается сравнение."""
        self.assertEqual(self.tree.by_ref("YO-00002590").name, "Водостойкий ламинат")

    def test_kind_comes_from_1c_not_from_name(self):
        """Ветка вида товара названа «Водостойкий ламинат», а не как вид товара."""
        self.assertEqual(self.tree.by_ref("YO-00002590").kind, "type")
        self.assertEqual(self.tree.by_ref("YO-00002590").product_type_ref, "000000002")

    def test_children(self):
        self.assertEqual([f.ref for f in self.tree.children("00000000001")],
                         ["YO-00002590"])

    def test_missing_ref_is_none(self):
        self.assertIsNone(self.tree.by_ref("YO-99999999"))


class PropertiesTest(unittest.TestCase):

    def setUp(self):
        self.cat = _client(PROPS_ENVELOPE).properties_by_type("000000002", tm="000000325")

    def test_matched_folder(self):
        self.assertEqual(self.cat.matched_folder["code"], "0004045")

    def test_values_parsed(self):
        self.assertEqual(self.cat.by_code("0000003").values[0].code, "0004046")

    def test_property_without_values_kept(self):
        """«Длина» приходит с пустым списком — агент должен знать, что поле есть."""
        self.assertEqual(self.cat.by_code("0000045").values, [])

    def test_matched_folder_always_a_dict(self):
        payload = dict(PROPS_ENVELOPE, matched_folder=None)
        cat = _client(payload).properties_by_type("000000002")
        self.assertEqual(cat.matched_folder, {"code": "", "name": ""})

    def test_empty_values_with_tm_mean_no_collections(self):
        """Пусто при запросе С МАРКОЙ — это «коллекций нет», а не «отбор не применился»."""
        payload = dict(PROPS_ENVELOPE, matched_folder={"code": "", "name": ""},
                       properties=[{"property": "Коллекция", "code": "0000003",
                                    "values": []}])
        cat = _client(payload).properties_by_type("000000003", tm="000000325")
        self.assertEqual(cat.by_code("0000003").values, [])
        self.assertEqual(cat.matched_folder["code"], "")


class SetItemsTest(unittest.TestCase):

    def test_posts_utf8_with_charset(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content
            seen["ctype"] = request.headers.get("Content-Type")
            return httpx.Response(200, content=b'{"created": 1, "errors": []}')

        c = OnecClient("http://example.invalid/api", "token")
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        out = c.set_items([{"op": "create_folder", "name": "Коллекция"}])
        self.assertEqual(out["created"], 1)
        self.assertIn("charset=utf-8", seen["ctype"])
        self.assertIn("Коллекция", seen["body"].decode("utf-8"))

    def test_html_answer_raises_with_status(self):
        """IIS отдаёт HTML при незарегистрированном маршруте — молча это глотать нельзя."""
        def handler(request):
            return httpx.Response(404, content=b"<!DOCTYPE html><html>404</html>")

        c = OnecClient("http://example.invalid/api", "token")
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        with self.assertRaises(RuntimeError):
            c.set_items([])


class RetryTest(unittest.TestCase):
    """Повторы при обрыве сети.

    Провайдер модели опрашивает 1С раз в 5 секунд бесконечно, и сервер РВЁТ простаивающее
    keep-alive соединение по своему таймауту. Найдено живым прогоном: цикл падал на
    `WinError 10054` (`httpx.ReadError`) через пару минут ожидания.
    """

    def setUp(self):
        # Паузы между попытками не нужны в тесте: проверяется, что повтор ЕСТЬ, а не
        # сколько он ждёт. Без подмены тест спал бы по шесть секунд на каждый случай.
        import src.onec.client as mod
        self._sleep = mod.time.sleep
        mod.time.sleep = lambda _s: None
        self.addCleanup(lambda: setattr(mod.time, "sleep", self._sleep))

    def _flaky(self, fails, error):
        """Клиент, чей транспорт падает `fails` раз, а потом отвечает."""
        state = {"n": 0}

        def handler(request):
            state["n"] += 1
            if state["n"] <= fails:
                raise error
            return httpx.Response(200, content=b'{"version": 7, "changed": 0}')

        c = OnecClient("http://example.invalid/api", "token", retries=4)
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        return c, state

    def test_post_retries_a_dropped_keepalive(self):
        """До правки повторов у POST не было вовсе, и снимок терялся на ровном месте."""
        c, state = self._flaky(2, httpx.ReadError("[WinError 10054]"))
        self.assertEqual(c.set_model_state([])["version"], 7)
        self.assertEqual(state["n"], 3)

    def test_get_retries_a_dropped_keepalive(self):
        c, state = self._flaky(1, httpx.ReadError("[WinError 10054]"))
        c.agent_commands()
        self.assertEqual(state["n"], 2)

    def test_connect_error_is_retried_too(self):
        """Прежний повод — 1С не принимает соединение (WinError 10060)."""
        c, state = self._flaky(2, httpx.ConnectError("[WinError 10060]"))
        c.agent_commands()
        self.assertEqual(state["n"], 3)

    def test_gives_up_after_the_limit(self):
        """Бесконечный повтор подвесил бы оборот цикла молча."""
        c, _ = self._flaky(99, httpx.ReadError("сеть легла"))
        with self.assertRaises(httpx.ReadError):
            c.set_model_state([])

    def test_server_error_is_not_retried(self):
        """500 от 1С — это ОТВЕТ, а не обрыв: повтор приведёт тот же самый.

        Отличать их обязательно: ошибка BSL иначе множилась бы на четыре попытки, а
        `set-model-state` четырежды пытался бы применить снимок, который 1С отвергла.
        """
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(500, content=b'{"error": "handler_failed"}')

        c = OnecClient("http://example.invalid/api", "token", retries=4)
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        with self.assertRaises(httpx.HTTPStatusError):
            c.set_model_state([])
        self.assertEqual(calls["n"], 1)


class PageSizeTest(unittest.TestCase):
    """Страница выгрузки маленькая НАМЕРЕННО (бой 24.09.2026).

    Обработчик собирает цены по каждой позиции тремя видами цен, и запрос на 200 позиций
    занимал базу на минуту, упираясь в наш же таймаут и блокируя всё остальное.
    """

    def test_default_page_is_small(self):
        from src.onec.client import PAGE_SIZE
        self.assertLessEqual(PAGE_SIZE, 50)

    def test_pages_are_requested_by_that_size(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"tm": "Egger", "total": 60, "offset": 1,
                                             "limit": 25, "items": [], "errors": []})

        c = OnecClient("http://example.invalid/api", "token")
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        c.by_tm_all("T1")

        self.assertEqual(seen[0]["size"], "25")
        # 60 позиций по 25 — три страницы, а не одна на двести
        self.assertEqual(len(seen), 3)


class RetryPolicyTest(unittest.TestCase):
    """Что повторяем, а что нет (бой 24.09.2026).

    1С не отменяет работу, когда клиент отвалился: брошенная выгрузка продолжает
    крутиться. Повторив таймаут пять раз, мы кладём на базу пять тяжёлых запросов вместо
    одного — и сами превращаем медленный ответ в неотвечающий.
    """

    def client(self, boom):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise boom

        c = OnecClient("http://example.invalid/api", "token", retries=5, backoff=0)
        c._client = httpx.Client(base_url="http://example.invalid/api",
                                 transport=httpx.MockTransport(handler))
        return c, calls

    def test_read_timeout_is_not_repeated(self):
        c, calls = self.client(httpx.ReadTimeout("слишком долго"))
        with self.assertRaises(httpx.ReadTimeout):
            c.selling_tm()
        self.assertEqual(calls["n"], 1)

    def test_dropped_connection_is_repeated(self):
        """Обрыв keep-alive — штатное поведение IIS, и повтор ровно лечит."""
        c, calls = self.client(httpx.ReadError("WinError 10054"))
        with self.assertRaises(httpx.ReadError):
            c.selling_tm()
        self.assertEqual(calls["n"], 5)

    def test_connect_timeout_is_repeated(self):
        """Соединение не установилось — сервер о нас не знает, повтор уместен."""
        c, calls = self.client(httpx.ConnectTimeout("WinError 10060"))
        with self.assertRaises(httpx.ConnectTimeout):
            c.selling_tm()
        self.assertEqual(calls["n"], 5)


if __name__ == "__main__":
    unittest.main()
