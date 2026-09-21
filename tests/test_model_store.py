"""Персистентность модели (§10 specs/agent-workflow-model.md).

Главное, что проверяется, — граф объектов, поднятый из базы, совпадает с тем, что в неё
клали. Ради этого всё и делается: модель живёт в памяти процесса, а база нужна, чтобы
пережить его остановку. Молчаливая потеря идентификатора адреса или порядка имён всплыла бы
позже — промахом при сопоставлении задач.
"""
import tempfile
import unittest
from pathlib import Path

from src.model.enums import PriceStatus, TaskKind, TaskStatus, TaskSubject
from src.model import price_list as pl
from src.model.price import Price, SupplierPrice
from src.model.refs import Ref, TaskAddress, TradeMark
from src.model.task import PriceTask
from src.storage.model_store import ModelStore


def addr(tm="Egger", collection="Quartz", tm_code="", coll_code="", article=""):
    return TaskAddress(tm=Ref.make(code=tm_code, names=[tm]),
                       subject=Ref.make(code=coll_code, article=article,
                                        names=[collection]))


def task(kind=TaskKind.CHANGE_PRICES, address=None, description="сверить цены"):
    return PriceTask(kind=kind, address=address or addr(), description=description)


def price(supplier=1, signature="sig", price_date=None, received="2026-09-01T10:00",
          path="/p/1.xlsx", file_id=1):
    return Price(supplier_price=SupplierPrice(
        supplier_id=supplier, file_id=file_id, file_path=path, filename=Path(path).name,
        signature=signature, price_date=price_date, received_at=received))


class Base(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = ModelStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()


class RoundTripTest(Base):

    async def test_price_survives_reload(self):
        saved = await self.store.add_price(price(price_date="2026-09-15"))
        self.assertIsNotNone(saved.id)

        loaded = (await self.store.load_all())[0]

        self.assertEqual(loaded.id, saved.id)
        self.assertEqual(loaded.supplier_price.supplier_id, 1)
        self.assertEqual(loaded.supplier_price.file_path, "/p/1.xlsx")
        self.assertEqual(loaded.supplier_price.price_date, "2026-09-15")
        self.assertEqual(loaded.status, PriceStatus.TODO)

    async def test_tasks_survive_with_full_address(self):
        """Потеря хоть одного идентификатора всплыла бы промахом при сопоставлении."""
        p = price()
        p.add_task(task(address=addr(tm="Egger", tm_code="T1", collection="Adventure",
                                     coll_code="YO-77", article="A001")))
        await self.store.add_price(p)

        loaded = (await self.store.load_all())[0]
        got = loaded.tasks[0].address

        self.assertEqual(got.tm.code, "T1")
        self.assertIn("Egger", got.tm.names)
        self.assertEqual(got.subject.code, "YO-77")
        self.assertEqual(got.subject.article, "a001")
        self.assertIn("Adventure", got.subject.names)

    async def test_all_names_survive_not_just_the_first(self):
        """Их несколько намеренно: совпадение ищется по любому."""
        p = price()
        p.add_task(PriceTask(kind=TaskKind.ADD_NEW, address=TaskAddress(
            tm=Ref.make(names=["Egger"]),
            subject=Ref.make(names=["Adventure", "Эдвенчер", "ADVENTURE"]))))
        await self.store.add_price(p)

        got = (await self.store.load_all())[0].tasks[0].address.subject.names
        # Написание сохраняется, а повтор в другом регистре отбрасывается.
        self.assertEqual(set(got), {"Adventure", "Эдвенчер"})

    async def test_loaded_task_still_matches_the_original(self):
        p = price()
        original = task(address=addr(collection="Adventure"))
        p.add_task(original)
        await self.store.add_price(p)

        loaded = (await self.store.load_all())[0].tasks[0]
        self.assertTrue(loaded.matches(original))

    async def test_trade_marks_survive_including_unknown(self):
        p = price()
        p.supplier_price.set_trade_marks(
            [TradeMark("Egger", "T1"), TradeMark.unknown("A+ FLOOR")])
        await self.store.add_price(p)

        marks = (await self.store.load_all())[0].supplier_price.trade_marks
        self.assertEqual(len(marks), 2)
        self.assertEqual([m.name for m in marks if not m.in_1c], ["A+ FLOOR"])

    async def test_task_status_and_result_survive(self):
        p = price()
        t = p.add_task(task())
        await self.store.add_price(p)
        t.complete(TaskStatus.PARTIAL, "из 12 записано 9")
        await self.store.update_task(t)

        loaded = (await self.store.load_all())[0].tasks[0]
        self.assertEqual(loaded.status, TaskStatus.PARTIAL)
        self.assertEqual(loaded.result, "из 12 записано 9")
        self.assertIsNotNone(loaded.run_at)
        self.assertTrue(loaded.closed)

    async def test_run_mark_survives_a_failed_attempt(self):
        """Задача снова «к обработке», но отметка о прогоне обязана пережить перезапуск:
        по ней админ в 1С отличает «не брались» от «пробовали и не вышло»."""
        p = price()
        t = p.add_task(task())
        await self.store.add_price(p)
        t.complete(TaskStatus.TODO, "1С не ответила")
        await self.store.update_task(t)

        loaded = (await self.store.load_all())[0].tasks[0]
        self.assertEqual(loaded.status, TaskStatus.TODO)
        self.assertEqual(loaded.run_at, t.run_at)

    async def test_old_done_at_moves_into_the_run_mark(self):
        """База с прежней колонкой поднимается без потери дат: у закрытой задачи закрывший
        её прогон — тот же самый, и выбрасывать его значило бы обнулить всю историю."""
        import aiosqlite

        p = price()
        t = p.add_task(task())
        await self.store.add_price(p)

        path = self.store._db_path
        async with aiosqlite.connect(path) as db:      # вернули базу к старому виду
            await db.execute("ALTER TABLE price_task ADD COLUMN done_at TEXT")
            await db.execute("UPDATE price_task SET run_at = NULL, "
                             "done_at = '2026-09-14T13:05:00' WHERE id = ?", (t.id,))
            await db.commit()

        await self.store.init()
        loaded = (await self.store.load_all())[0].tasks[0]
        self.assertEqual(loaded.run_at, "2026-09-14T13:05:00")

        async with aiosqlite.connect(path) as db:      # и сама колонка убрана
            cur = await db.execute("PRAGMA table_info(price_task)")
            self.assertNotIn("done_at", {row[1] for row in await cur.fetchall()})

    async def test_item_subject_survives(self):
        p = price()
        p.add_task(PriceTask(kind=TaskKind.CHANGE_PROPERTIES, address=TaskAddress(
            tm=Ref.make(names=["Egger"]), subject=Ref.make(code="YO-9"),
            subject_kind=TaskSubject.ITEM)))
        await self.store.add_price(p)

        loaded = (await self.store.load_all())[0].tasks[0]
        self.assertEqual(loaded.subject, TaskSubject.ITEM)
        self.assertEqual(loaded.address.subject_kind, TaskSubject.ITEM)

    async def test_empty_base_loads_empty_list(self):
        self.assertEqual(await self.store.load_all(), [])


class MutationTest(Base):

    async def test_price_status_is_saved(self):
        saved = await self.store.add_price(price())
        await self.store.set_price_status(saved.id, PriceStatus.DONE)
        self.assertEqual((await self.store.load_all())[0].status, PriceStatus.DONE)

    async def test_task_added_after_the_price(self):
        saved = await self.store.add_price(price())
        added = await self.store.add_task(saved.id, task())
        self.assertIsNotNone(added.id)
        self.assertEqual(len((await self.store.load_all())[0].tasks), 1)

    async def test_task_removed(self):
        p = price()
        t = p.add_task(task())
        await self.store.add_price(p)
        self.assertTrue(await self.store.remove_task(t.id))
        self.assertEqual((await self.store.load_all())[0].tasks, [])

    async def test_update_without_id_is_refused(self):
        with self.assertRaises(ValueError):
            await self.store.update_task(task())

    async def test_description_edit_is_saved(self):
        p = price()
        t = p.add_task(task(description="исходное"))
        await self.store.add_price(p)
        t.set_description("правка админа")
        await self.store.update_task(t)
        self.assertEqual((await self.store.load_all())[0].tasks[0].description,
                         "правка админа")


class RebuildTest(Base):

    async def test_rebuild_drops_old_tasks_and_their_state(self):
        p = price()
        old = p.add_task(task(address=addr(collection="A"), description="старое"))
        await self.store.add_price(p)
        old.complete(TaskStatus.DONE, "сделано")
        await self.store.update_task(old)

        fresh = [task(address=addr(collection="B"))]
        await self.store.replace_tasks(p.id, fresh)

        loaded = (await self.store.load_all())[0].tasks
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].status, TaskStatus.TODO)
        self.assertEqual(loaded[0].result, "")

    async def test_old_ids_are_not_reused(self):
        """«Выполни задачу 7» после пересборки должна НЕ НАЙТИ задачу,
        а не выполнить другую, оказавшуюся под тем же номером."""
        p = price()
        old = p.add_task(task(address=addr(collection="A")))
        await self.store.add_price(p)
        old_id = old.id

        await self.store.replace_tasks(p.id, [task(address=addr(collection="A"))])

        new_id = (await self.store.load_all())[0].tasks[0].id
        self.assertNotEqual(new_id, old_id)

    async def test_rebuild_touches_only_its_own_price(self):
        one = await self.store.add_price(price(path="/p/1.xlsx", file_id=1))
        two = price(path="/p/2.xlsx", file_id=2, signature="other")
        two.add_task(task())
        await self.store.add_price(two)

        await self.store.replace_tasks(one.id, [task()])

        loaded = {p.id: p for p in await self.store.load_all()}
        self.assertEqual(len(loaded[two.id].tasks), 1)


class RemovalTest(Base):

    async def test_price_removed_with_its_tasks_and_marks(self):
        p = price()
        p.add_task(task())
        p.supplier_price.set_trade_marks([TradeMark("Egger", "T1")])
        await self.store.add_price(p)

        self.assertEqual(await self.store.remove_price(p.id), "/p/1.xlsx")
        self.assertEqual(await self.store.load_all(), [])

    async def test_dangling_link_is_cleared_on_removal(self):
        """В базе не должно остаться указателя в пустоту."""
        old = await self.store.add_price(price(price_date="2026-07-01"))
        new = await self.store.add_price(
            price(price_date="2026-09-01", path="/p/2.xlsx", file_id=2))
        pl.relink([old, new])
        await self.store.save_links([old, new])
        self.assertEqual((await self.store.load_all())[0].newer_id, new.id)

        await self.store.remove_price(new.id)

        self.assertIsNone((await self.store.load_all())[0].newer_id)

    async def test_removing_a_missing_price_is_not_an_error(self):
        self.assertIsNone(await self.store.remove_price(999))


class LinkTest(Base):
    """Ссылки считает `relink` в памяти, а `save_links` только кладёт результат."""

    async def test_links_survive_reload(self):
        old = await self.store.add_price(price(price_date="2026-07-01"))
        mid = await self.store.add_price(
            price(price_date="2026-08-01", path="/p/2.xlsx", file_id=2))
        new = await self.store.add_price(
            price(price_date="2026-09-01", path="/p/3.xlsx", file_id=3))

        pl.relink([old, mid, new])
        await self.store.save_links([old, mid, new])

        loaded = {p.id: p for p in await self.store.load_all()}
        self.assertEqual(loaded[old.id].newer_id, new.id)
        self.assertEqual(loaded[mid.id].newer_id, new.id)
        self.assertIsNone(loaded[new.id].newer_id)
        self.assertTrue(loaded[old.id].has_newer)

    async def test_relink_after_reload_is_stable(self):
        """Поднятый из базы список не должен «переезжать» на ровном месте."""
        old = await self.store.add_price(price(price_date="2026-07-01"))
        new = await self.store.add_price(
            price(price_date="2026-09-01", path="/p/2.xlsx", file_id=2))
        pl.relink([old, new])
        await self.store.save_links([old, new])

        loaded = await self.store.load_all()
        self.assertEqual(pl.relink(loaded), 0)


class SweepTest(Base):

    async def test_known_paths_for_orphan_cleanup(self):
        await self.store.add_price(price(path="/p/1.xlsx", file_id=1))
        await self.store.add_price(price(path="/p/2.xlsx", file_id=2, signature="two"))
        self.assertEqual(await self.store.known_paths(), {"/p/1.xlsx", "/p/2.xlsx"})

    async def test_file_held_only_by_the_model_survives_the_sweep(self):
        """Уборка сирот обязана знать про модель.

        Забыть её в объединении путей — значит стереть файл живого прайса, а прайс без
        файла существовать не может (§3.1). Ломается это тихо и на старте бота, когда
        смотреть некому.
        """
        from src.storage import price_files
        from src.storage.pricing import PricingStore

        db = Path(self._dir.name) / "t.db"
        pricing = PricingStore(db)
        await pricing.init()

        saved = price_files.save(db, "прайс.xlsx", b"PK fake")
        await self.store.add_price(price(path=str(saved), file_id=1))

        known = await pricing.known_price_paths() | await self.store.known_paths()
        removed = price_files.sweep(db, known)

        self.assertEqual(removed, 0)
        self.assertTrue(Path(saved).is_file())

    async def test_sweep_without_the_model_would_delete_it(self):
        """Обратная проверка: тест выше поймает реальную регрессию, а не тавтологию."""
        from src.storage import price_files
        from src.storage.pricing import PricingStore

        db = Path(self._dir.name) / "t.db"
        pricing = PricingStore(db)
        await pricing.init()

        saved = price_files.save(db, "прайс.xlsx", b"PK fake")
        await self.store.add_price(price(path=str(saved), file_id=1))

        price_files.sweep(db, await pricing.known_price_paths())   # модель забыта

        self.assertFalse(Path(saved).is_file())


if __name__ == "__main__":
    unittest.main()
