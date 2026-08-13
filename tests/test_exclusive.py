"""Разрешение эксклюзива: заявки + решения админа → пометка (§9.5).

Проверяется главное свойство: пометка появляется только когда она однозначна. Спор
двух поставщиков и протухшая заявка дают ОТСУТСТВИЕ пометки, а не догадку.
"""
import unittest

from src.price_tool.exclusive import (
    Claim, Decision, annotate, find, label, prompt_block, resolve,
)

TODAY = "2026-08-13"


def claim(supplier, price_date, tm="000000104", coll="YO-C1", item="", **kw):
    return Claim(supplier=supplier, tm_code=tm, tm_name=kw.get("tm_name", "Classen"),
                 collection_ref=coll, collection=kw.get("collection", "Adventure"),
                 item_ref=item, item_name=kw.get("item_name", ""),
                 phrase=kw.get("phrase", "эксклюзив"), where_found="column",
                 price_date=price_date)


class ResolveTest(unittest.TestCase):
    def test_single_claim_becomes_label(self):
        active, disputes = resolve([claim("Монарх Логистик", "2026-08-01")], today=TODAY)
        self.assertEqual(disputes, [])
        exc = find(active, "000000104", "YO-C1")
        self.assertEqual(exc.supplier, "Монарх Логистик")
        self.assertEqual(exc.since, "2026-08-01")

    def test_two_suppliers_in_window_is_dispute_and_no_label(self):
        """Ровно то, ради чего задаётся вопрос админу: пометка не показывается."""
        active, disputes = resolve(
            [claim("Монарх Логистик", "2026-07-01"), claim("ТД Паркет", "2026-08-01")],
            today=TODAY)
        self.assertEqual(len(disputes), 1)
        self.assertEqual(set(disputes[0].suppliers), {"Монарх Логистик", "ТД Паркет"})
        self.assertIsNone(find(active, "000000104", "YO-C1"))

    def test_claims_further_apart_are_handover_not_dispute(self):
        """Заявки вне окна 2 мес — это смена эксклюзива, побеждает свежая."""
        active, disputes = resolve(
            [claim("ТД Паркет", "2026-01-10"), claim("Монарх Логистик", "2026-08-01")],
            today=TODAY)
        self.assertEqual(disputes, [])
        self.assertEqual(find(active, "000000104", "YO-C1").supplier, "Монарх Логистик")

    def test_window_boundary(self):
        """62 дня — ещё спор, 63 — уже смена."""
        _, inside = resolve([claim("A", "2026-06-01"), claim("B", "2026-08-02")], today=TODAY)
        self.assertEqual(len(inside), 1)
        _, outside = resolve([claim("A", "2026-06-01"), claim("B", "2026-08-03")], today=TODAY)
        self.assertEqual(outside, [])

    def test_stale_claim_dropped(self):
        """Заявка старше года не подтверждается прайсами — пометку снимаем."""
        active, disputes = resolve([claim("Монарх Логистик", "2025-08-01")], today=TODAY)
        self.assertEqual((active, disputes), ({}, []))

    def test_stale_claim_does_not_create_dispute(self):
        """Протухшая заявка не спорит со свежей."""
        active, disputes = resolve(
            [claim("ТД Паркет", "2025-01-01"), claim("Монарх Логистик", "2026-08-01")],
            today=TODAY)
        self.assertEqual(disputes, [])
        self.assertEqual(find(active, "000000104", "YO-C1").supplier, "Монарх Логистик")

    def test_decision_wins_over_claims(self):
        active, disputes = resolve(
            [claim("Монарх Логистик", "2026-07-01"), claim("ТД Паркет", "2026-08-01")],
            [Decision("000000104", "YO-C1", supplier="ТД Паркет", decided_at="2026-08-12")],
            today=TODAY)
        self.assertEqual(disputes, [])
        exc = find(active, "000000104", "YO-C1")
        self.assertEqual(exc.supplier, "ТД Паркет")
        self.assertTrue(exc.by_admin)

    def test_decision_none_suppresses_label(self):
        """«Эксклюзива нет» должно пережить новые заявки того же поставщика."""
        active, disputes = resolve(
            [claim("Монарх Логистик", "2026-08-01")],
            [Decision("000000104", "YO-C1", supplier=None, decided_at="2026-08-12")],
            today=TODAY)
        self.assertEqual((active, disputes), ({}, []))

    def test_decision_without_any_claim(self):
        """Админ может назначить эксклюзив руками, до всякого прайса."""
        active, _ = resolve([], [Decision("000000104", "YO-C1", supplier="ТД Паркет",
                                          decided_at="2026-08-12")], today=TODAY)
        self.assertEqual(find(active, "000000104", "YO-C1").supplier, "ТД Паркет")

    def test_empty(self):
        self.assertEqual(resolve([], [], today=TODAY), ({}, []))

    def test_broken_date_ignored(self):
        self.assertEqual(resolve([claim("A", "не дата")], today=TODAY), ({}, []))


class FindTest(unittest.TestCase):
    def test_item_inherits_collection(self):
        active, _ = resolve([claim("Монарх Логистик", "2026-08-01")], today=TODAY)
        self.assertEqual(find(active, "000000104", "YO-C1", "YO-777").supplier,
                         "Монарх Логистик")

    def test_collection_inherits_tm(self):
        """Эксклюзив на всю ТМ распространяется на любую её коллекцию."""
        active, _ = resolve([claim("Монарх Логистик", "2026-08-01", coll="")], today=TODAY)
        self.assertEqual(find(active, "000000104", "YO-ANY").supplier, "Монарх Логистик")

    def test_item_level_beats_collection(self):
        active, _ = resolve([claim("Монарх Логистик", "2026-08-01"),
                             claim("ТД Паркет", "2026-08-01", item="YO-777")], today=TODAY)
        self.assertEqual(find(active, "000000104", "YO-C1", "YO-777").supplier, "ТД Паркет")
        self.assertEqual(find(active, "000000104", "YO-C1", "YO-888").supplier,
                         "Монарх Логистик")

    def test_other_tm_untouched(self):
        active, _ = resolve([claim("Монарх Логистик", "2026-08-01")], today=TODAY)
        self.assertIsNone(find(active, "000000999", "YO-C1"))

    def test_no_tm(self):
        self.assertIsNone(find({}, None))


class LabelTest(unittest.TestCase):
    def test_nominative_case(self):
        """Родительный падеж от произвольного названия кодом не построить — не пытаемся."""
        active, _ = resolve([claim("ТД Паркет", "2026-08-01")], today=TODAY)
        self.assertEqual(label(find(active, "000000104", "YO-C1")), "эксклюзив: ТД Паркет")

    def test_annotate(self):
        active, _ = resolve([claim("Монарх Логистик", "2026-08-01")], today=TODAY)
        self.assertEqual(annotate("Adventure", find(active, "000000104", "YO-C1")),
                         "Adventure (эксклюзив: Монарх Логистик)")

    def test_annotate_without_exclusive_is_unchanged(self):
        self.assertEqual(annotate("Adventure", None), "Adventure")
        self.assertEqual(label(None), "")


class PromptBlockTest(unittest.TestCase):
    def test_lists_tm_and_collection(self):
        active, _ = resolve([claim("Монарх Логистик", "2026-08-01")], today=TODAY)
        block = prompt_block(active)
        self.assertIn("Classen / Adventure — Монарх Логистик", block)
        self.assertIn("справочная", block)

    def test_empty_block_when_nothing_to_say(self):
        """Пустой список не должен занимать место в промпте и ломать кеш."""
        self.assertEqual(prompt_block({}), "")

    def test_item_level_shown_by_name(self):
        active, _ = resolve([claim("ТД Паркет", "2026-08-01", item="YO-777",
                                   item_name="Дуб Авола 62593")], today=TODAY)
        self.assertIn("Дуб Авола 62593 — ТД Паркет", prompt_block(active))


if __name__ == "__main__":
    unittest.main()
