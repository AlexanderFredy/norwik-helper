"""Выбор наименьшей АКТУАЛЬНОЙ цены (`src/model/offers.py`).

Вопрос, на который отвечает модуль, поставил админ 22.09.2026: поставщик с меньшей ценой
дал её год назад и больше не присылал прайсов, другие шлют свежие, но дороже — чью писать?
Ответ: актуальность нельзя проверить, её можно только датировать, и окно в 180 дней
отсекает то, что про сегодняшнюю цену уже ничего не говорит.
"""
import unittest

from src.model.offers import FRESH_WINDOW_DAYS, Offer, best


def mine(purchase, when="2026-09-22"):
    return Offer(supplier_id=1, supplier="Наш", purchase=purchase, price_date=when)


def other(purchase, when, sid=2, name="Паркет-Холл", rrc=None):
    return Offer(supplier_id=sid, supplier=name, purchase=purchase, rrc=rrc,
                 price_date=when)


class BestTest(unittest.TestCase):

    def test_fresh_and_cheaper_wins(self):
        choice = best(mine(1880), [other(1560, "2026-09-15")])
        self.assertEqual(choice.offer.supplier_id, 2)
        self.assertIn("Паркет-Холл", choice.notes[0])

    def test_stale_and_cheaper_does_not_win(self):
        """СЛУЧАЙ АДМИНА. Цена годовой давности про сегодня не говорит ничего: пишем
        свежую, пусть и большую, — иначе в базе окажется цифра, по которой не продают."""
        choice = best(mine(1880), [other(1560, "2025-09-15")])
        self.assertEqual(choice.offer.supplier_id, 1)
        self.assertEqual(choice.offer.purchase, 1880)

    def test_stale_bargain_is_reported_not_swallowed(self):
        """Протухшая выгода — повод запросить прайс, а не потерянная строка."""
        choice = best(mine(1880), [other(1560, "2025-09-15")])
        text = " ".join(choice.notes)
        self.assertIn("1 560", text)
        self.assertIn("запросить свежий", text)

    def test_edge_of_the_window_still_counts(self):
        from datetime import date, timedelta
        edge = (date(2026, 9, 22) - timedelta(days=FRESH_WINDOW_DAYS)).isoformat()
        choice = best(mine(1880), [other(1560, edge)])
        self.assertEqual(choice.offer.supplier_id, 2)

    def test_a_day_past_the_window_does_not(self):
        from datetime import date, timedelta
        past = (date(2026, 9, 22) - timedelta(days=FRESH_WINDOW_DAYS + 1)).isoformat()
        choice = best(mine(1880), [other(1560, past)])
        self.assertEqual(choice.offer.supplier_id, 1)

    def test_newer_offer_is_always_comparable(self):
        """Чужой прайс СВЕЖЕЕ нашего — свежести много не бывает."""
        choice = best(mine(1880, "2026-03-01"), [other(1560, "2026-09-15")])
        self.assertEqual(choice.offer.supplier_id, 2)

    def test_more_expensive_neighbour_changes_nothing(self):
        choice = best(mine(1880), [other(2100, "2026-09-15")])
        self.assertEqual(choice.offer.supplier_id, 1)
        self.assertEqual(choice.notes, [])

    def test_cheapest_of_several_wins(self):
        choice = best(mine(1880), [other(1700, "2026-09-01", sid=2),
                                   other(1560, "2026-08-20", sid=3, name="Дельта"),
                                   other(1990, "2026-09-10", sid=4, name="Омега")])
        self.assertEqual(choice.offer.supplier_id, 3)
        self.assertEqual(choice.offer.purchase, 1560)

    def test_tie_keeps_the_price_list_in_hand(self):
        """Менять поставщика ради нуля незачем, а история цен останется чище."""
        choice = best(mine(1880), [other(1880, "2026-09-15")])
        self.assertEqual(choice.offer.supplier_id, 1)
        self.assertEqual(choice.notes, [])

    def test_own_older_row_is_ignored(self):
        """Своя же прошлая строка в журнале — не конкурент самому себе."""
        choice = best(mine(1880), [other(1000, "2026-09-01", sid=1, name="Наш")])
        self.assertEqual(choice.offer.purchase, 1880)

    def test_offer_without_a_price_is_skipped(self):
        choice = best(mine(1880), [other(None, "2026-09-15")])
        self.assertEqual(choice.offer.supplier_id, 1)

    def test_without_our_price_nothing_is_decided(self):
        choice = best(mine(None), [other(1560, "2026-09-15")])
        self.assertIsNone(choice.offer.purchase)
        self.assertEqual(choice.notes, [])

    def test_rrc_comes_from_the_winner(self):
        """Решение админа: пара «закупка + РРЦ» берётся у одного поставщика."""
        choice = best(mine(1880), [other(1560, "2026-09-15", rrc=2870)])
        self.assertEqual(choice.offer.rrc, 2870)

    def test_dateless_offer_is_compared(self):
        """Отбрасывать из-за незаполненной даты хуже, чем сравнить."""
        choice = best(mine(1880), [other(1560, None)])
        self.assertEqual(choice.offer.supplier_id, 2)


if __name__ == "__main__":
    unittest.main()
