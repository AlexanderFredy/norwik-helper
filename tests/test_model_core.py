"""Ядро модели: адресация, задачи, прайс, ссылки на более свежий (§3 модели).

Здесь живёт самая тонкая логика будущей модели, и проверяется она без базы и без бота —
чистые функции над объектами. Всё, что ломается тихо (совпадение адресов, пересборка,
ссылки на самый новый прайс), должно ломать тест.
"""
import unittest

from src.model.enums import PriceStatus, TaskKind, TaskStatus, TaskSubject
from src.model.price import Price, SupplierPrice
from src.model import price_list as pl
from src.model.refs import Ref, TaskAddress, TradeMark
from src.model.task import PriceTask, sort_tasks


def ref(*names, code="", article=""):
    return Ref.make(code=code, article=article, names=names)


def addr(tm="Egger", collection="Quartz", tm_code="", coll_code=""):
    return TaskAddress(tm=Ref.make(code=tm_code, names=[tm]),
                       subject=Ref.make(code=coll_code, names=[collection]))


def task(kind=TaskKind.CHANGE_PRICES, address=None, description=""):
    return PriceTask(kind=kind, address=address or addr(), description=description)


def price(pid=1, supplier=1, signature="sig", price_date=None, received="2026-09-01T10:00"):
    return Price(id=pid, supplier_price=SupplierPrice(
        supplier_id=supplier, file_id=pid, file_path=f"/p/{pid}.xlsx",
        filename=f"{pid}.xlsx", signature=signature,
        price_date=price_date, received_at=received))


class RefTest(unittest.TestCase):

    def test_match_by_code(self):
        self.assertTrue(ref(code="YO-1").matches(ref(code="YO-1")))

    def test_match_by_article(self):
        self.assertTrue(ref(article="A001").matches(ref(article="a001")))

    def test_match_by_name_ignores_case_and_punctuation(self):
        """«QUARTZ» после нормализации становится «Quartz» — это отдельный вид задачи,
        и по имени они обязаны совпадать."""
        self.assertTrue(ref("QUARTZ").matches(ref("Quartz")))

    def test_one_common_identifier_is_enough(self):
        """У старой задачи есть только имя, у новой уже и код папки — это один предмет."""
        old = ref("Adventure")
        new = ref("Adventure", code="YO-77")
        self.assertTrue(old.matches(new))

    def test_different_codes_and_names_do_not_match(self):
        self.assertFalse(ref("один", code="A").matches(ref("другой", code="B")))

    def test_empty_matches_nothing_including_empty(self):
        """«Неизвестно» не равно «неизвестно»: иначе слились бы два разных безымянных."""
        self.assertFalse(Ref().matches(Ref()))
        self.assertFalse(Ref().matches(ref(code="A")))

    def test_merged_picks_up_new_identifiers(self):
        """Код папки появился после её создания — потеряв его, промахнёмся в следующий раз."""
        merged = ref("Adventure").merged(ref("Эдвенчер", code="YO-77"))
        self.assertEqual(merged.code, "YO-77")
        # Имена хранятся КАК НАПИСАНЫ — их читает админ в подписи задачи; сравнение
        # идёт по нормализованным `keys`.
        self.assertIn("Adventure", merged.names)
        self.assertIn("Эдвенчер", merged.names)

    def test_blank_names_are_dropped(self):
        self.assertEqual(Ref.make(names=["", "  ", "Quartz"]).names, ("Quartz",))

    def test_case_duplicates_collapse_but_spelling_survives(self):
        """«Ле Паркет» и «ЛЕ ПАРКЕТ» — одно имя; в списке остаётся первое написание."""
        made = Ref.make(names=["Ле Паркет", "ЛЕ ПАРКЕТ", "ле паркет"])
        self.assertEqual(made.names, ("Ле Паркет",))
        self.assertEqual(made.keys, {"ле паркет"})


class AddressTest(unittest.TestCase):

    def test_same_collection_under_different_marks_is_not_the_same(self):
        """Артикул «A001» встречается у двух поставщиков — без марки задачи слились бы."""
        a = addr(tm="Egger", collection="Quartz")
        b = addr(tm="Classen", collection="Quartz")
        self.assertFalse(a.matches(b))

    def test_collection_and_item_are_different_subjects(self):
        a = TaskAddress(tm=ref("Egger"), subject=ref("X"),
                        subject_kind=TaskSubject.COLLECTION)
        b = TaskAddress(tm=ref("Egger"), subject=ref("X"),
                        subject_kind=TaskSubject.ITEM)
        self.assertFalse(a.matches(b))

    def test_match_survives_appearance_of_a_folder_code(self):
        before = addr(collection="Adventure")
        after = addr(collection="Adventure", coll_code="YO-77")
        self.assertTrue(before.matches(after))


class TaskTest(unittest.TestCase):

    def test_complete_marks_the_run(self):
        t = task()
        t.complete(TaskStatus.DONE, "записано 12 позиций")
        self.assertTrue(t.closed)
        self.assertIsNotNone(t.run_at)

    def test_nothing_worked_returns_to_todo(self):
        """«Не получилось ничего» — это возврат в очередь, а не отдельный статус."""
        t = task()
        t.complete(TaskStatus.DONE, "ок")
        t.complete(TaskStatus.TODO, "1С не ответила")
        self.assertFalse(t.closed)

    def test_partial_is_closed_for_readiness(self):
        """Штатный исход: иначе прайс не смог бы стать готовым никогда."""
        t = task()
        t.complete(TaskStatus.PARTIAL, "из 12 записано 9, три без размера")
        self.assertTrue(t.closed)

    def test_reopen_resets_the_status_only(self):
        t = task()
        t.complete(TaskStatus.DONE)
        t.reopen()
        self.assertEqual(t.status, TaskStatus.TODO)

    def test_run_mark_is_set_whatever_the_outcome(self):
        """Прогон был — значит отметка есть, даже если задача вернулась в очередь."""
        t = task()
        self.assertIsNone(t.run_at)
        t.complete(TaskStatus.TODO, "1С не ответила")
        self.assertIsNotNone(t.run_at)

    def test_run_mark_survives_reopen(self):
        """Иначе «не брались» и «пробовали трижды» снова стали бы неразличимы."""
        t = task()
        t.complete(TaskStatus.DONE)
        t.reopen()
        self.assertIsNotNone(t.run_at)

    def test_absorb_appends_description_and_merges_identifiers(self):
        first = task(description="сверить размеры")
        second = task(address=addr(collection="Quartz", coll_code="YO-77"),
                      description="и класс износостойкости")
        first.absorb(second)
        self.assertIn("сверить размеры", first.description)
        self.assertIn("класс износостойкости", first.description)
        self.assertEqual(first.address.subject.code, "YO-77")

    def test_absorb_does_not_duplicate_the_same_text(self):
        first = task(description="сверить размеры")
        first.absorb(task(description="сверить размеры"))
        self.assertEqual(first.description.count("сверить размеры"), 1)

    def test_sort_follows_the_spec_order(self):
        kinds = [TaskKind.CHANGE_PRICES, TaskKind.ADD_NEW, TaskKind.NORMALIZE_NAMES,
                 TaskKind.MOVE_DISCONTINUED, TaskKind.CHANGE_PROPERTIES]
        out = sort_tasks([task(kind=k, address=addr(collection=k.value)) for k in kinds])
        self.assertEqual([t.kind for t in out], [
            TaskKind.MOVE_DISCONTINUED, TaskKind.NORMALIZE_NAMES,
            TaskKind.CHANGE_PROPERTIES, TaskKind.ADD_NEW, TaskKind.CHANGE_PRICES])

    def test_discontinued_goes_before_the_rest(self):
        """Решение админа 24.09.2026: снятое вычищается ПЕРВЫМ. Пока оно лежит в живых
        папках, нормализация и свойства причёсывают позиции, которые вот-вот уедут."""
        self.assertEqual(TaskKind.MOVE_DISCONTINUED.order, 0)

    def test_discontinued_sorts_before_adding(self):
        """Перед созданием позиции обязательна проверка среди снятых — иначе дубль."""
        self.assertLess(TaskKind.MOVE_DISCONTINUED.order, TaskKind.ADD_NEW.order)


class PriceTaskListTest(unittest.TestCase):

    def setUp(self):
        self.price = price()

    def test_duplicate_task_becomes_an_addition(self):
        """Пара (адрес, вид) уникальна: повторное создание — не вторая задача."""
        first = self.price.add_task(task(description="раз"))
        again = self.price.add_task(task(description="два"))
        self.assertIs(first, again)
        self.assertEqual(len(self.price.tasks), 1)
        self.assertIn("два", first.description)

    def test_same_kind_other_collection_is_a_second_task(self):
        self.price.add_task(task(address=addr(collection="Quartz")))
        self.price.add_task(task(address=addr(collection="Adventure")))
        self.assertEqual(len(self.price.tasks), 2)

    def test_same_collection_other_kind_is_a_second_task(self):
        self.price.add_task(task(kind=TaskKind.CHANGE_PRICES))
        self.price.add_task(task(kind=TaskKind.ADD_NEW))
        self.assertEqual(len(self.price.tasks), 2)

    def test_empty_price_is_not_ready(self):
        """«Разбирать нечего» и «всё сделано» — разные вещи."""
        self.assertFalse(self.price.ready)

    def test_ready_when_every_task_is_closed(self):
        a = self.price.add_task(task(address=addr(collection="A")))
        b = self.price.add_task(task(address=addr(collection="B")))
        a.complete(TaskStatus.DONE)
        self.assertFalse(self.price.ready)
        b.complete(TaskStatus.PARTIAL, "часть не удалась")
        self.assertTrue(self.price.ready)

    def test_status_is_not_touched_by_readiness(self):
        """Статус ставит админ; модель считает только готовность."""
        a = self.price.add_task(task())
        a.complete(TaskStatus.DONE)
        self.assertTrue(self.price.ready)
        self.assertEqual(self.price.status, PriceStatus.TODO)

    def test_rebuild_drops_everything(self):
        done = self.price.add_task(task(address=addr(collection="A"), description="старое"))
        done.complete(TaskStatus.DONE, "сделано")

        self.price.rebuild([task(address=addr(collection="B"))])

        self.assertEqual(len(self.price.tasks), 1)
        self.assertEqual(self.price.tasks[0].status, TaskStatus.TODO)
        self.assertEqual(self.price.tasks[0].result, "")

    def test_rebuild_returns_a_refused_task(self):
        """Принятое следствие, а не баг: «собери заново» = начать с чистого листа."""
        refused = self.price.add_task(task(address=addr(collection="A")))
        refused.complete(TaskStatus.DONE, "решили не делать")

        self.price.rebuild([task(address=addr(collection="A"))])

        self.assertEqual(self.price.tasks[0].status, TaskStatus.TODO)

    def test_file_is_mandatory(self):
        with self.assertRaises(ValueError):
            SupplierPrice(supplier_id=1, file_id=1, file_path="")


class TradeMarkTest(unittest.TestCase):

    def test_unknown_mark_is_still_listed(self):
        """«Не нашли в 1С» — факт о прайсе, а не повод выбросить раздел."""
        sp = price().supplier_price
        sp.set_trade_marks([TradeMark("Egger", "T1"), TradeMark.unknown("A+ FLOOR")])
        self.assertEqual(len(sp.trade_marks), 2)
        self.assertEqual([m.name for m in sp.unknown_marks], ["A+ FLOOR"])


class RelinkTest(unittest.TestCase):
    """Ссылка ведёт на САМЫЙ НОВЫЙ прайс группы, а не на следующий по порядку."""

    def test_all_outdated_point_at_the_newest(self):
        old = price(1, price_date="2026-07-01")
        mid = price(2, price_date="2026-08-01")
        new = price(3, price_date="2026-09-01")

        pl.relink([old, mid, new])

        self.assertEqual(old.newer_id, 3)
        self.assertEqual(mid.newer_id, 3)
        self.assertIsNone(new.newer_id)
        self.assertTrue(old.has_newer)
        self.assertFalse(new.has_newer)

    def test_a_newer_arrival_repoints_everyone(self):
        old = price(1, price_date="2026-07-01")
        mid = price(2, price_date="2026-08-01")
        pl.relink([old, mid])
        self.assertEqual(old.newer_id, 2)

        newest = price(3, price_date="2026-10-01")
        pl.relink([old, mid, newest])

        self.assertEqual(old.newer_id, 3)
        self.assertEqual(mid.newer_id, 3)

    def test_destroying_the_newest_repoints_to_the_next(self):
        """Не очищаем: более новые в списке ещё есть, и терять этот факт нельзя."""
        old = price(1, price_date="2026-07-01")
        mid = price(2, price_date="2026-08-01")
        new = price(3, price_date="2026-09-01")
        pl.relink([old, mid, new])

        pl.relink([old, mid])          # самый свежий уничтожен

        self.assertEqual(old.newer_id, 2)
        self.assertIsNone(mid.newer_id)

    def test_link_clears_when_nothing_newer_remains(self):
        old = price(1, price_date="2026-07-01")
        new = price(2, price_date="2026-09-01")
        pl.relink([old, new])
        pl.relink([old])
        self.assertIsNone(old.newer_id)

    def test_groups_are_independent(self):
        """Один поставщик, два формата: «плитка» не может быть новее «ламината»."""
        lam_old = price(1, supplier=7, signature="lam", price_date="2026-07-01")
        lam_new = price(2, supplier=7, signature="lam", price_date="2026-09-01")
        tile = price(3, supplier=7, signature="tile", price_date="2026-08-01")

        pl.relink([lam_old, lam_new, tile])

        self.assertEqual(lam_old.newer_id, 2)
        self.assertIsNone(tile.newer_id)

    def test_different_suppliers_do_not_mix(self):
        a = price(1, supplier=1, signature="same", price_date="2026-07-01")
        b = price(2, supplier=2, signature="same", price_date="2026-09-01")
        pl.relink([a, b])
        self.assertIsNone(a.newer_id)

    def test_received_date_decides_when_price_dates_are_equal(self):
        old = price(1, price_date="2026-09-01", received="2026-09-05T10:00")
        new = price(2, price_date="2026-09-01", received="2026-09-11T10:00")
        pl.relink([old, new])
        self.assertEqual(old.newer_id, 2)

    def test_relink_reports_changes(self):
        old = price(1, price_date="2026-07-01")
        new = price(2, price_date="2026-09-01")
        self.assertEqual(pl.relink([old, new]), 1)
        self.assertEqual(pl.relink([old, new]), 0)      # второй раз менять нечего

    def test_nobody_points_at_himself(self):
        one = price(1, price_date="2026-09-01")
        pl.relink([one])
        self.assertIsNone(one.newer_id)


class OutdatedTest(unittest.TestCase):

    def test_candidate_older_than_existing(self):
        existing = price(1, price_date="2026-09-01")
        late = price(2, price_date="2026-07-01")
        self.assertTrue(pl.is_outdated(late, [existing, late]))

    def test_candidate_is_the_freshest(self):
        existing = price(1, price_date="2026-07-01")
        fresh = price(2, price_date="2026-09-01")
        self.assertFalse(pl.is_outdated(fresh, [existing, fresh]))

    def test_other_group_does_not_make_it_outdated(self):
        other = price(1, signature="tile", price_date="2026-09-01")
        mine = price(2, signature="lam", price_date="2026-07-01")
        self.assertFalse(pl.is_outdated(mine, [other, mine]))


if __name__ == "__main__":
    unittest.main()
