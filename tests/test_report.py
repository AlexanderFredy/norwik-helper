"""Сводка по прайсу для менеджеров (решение админа 28.09.2026).

Уходит один раз — когда админ закрывает прайс. Собирается из дайджестов, снятых В МОМЕНТ
записи: по тексту результата не отличить «обновлены цены у четырёх» от «менять было
нечего», обе задачи закрыты как выполненные.
"""
import unittest

from src.model import report
from src.model.enums import TaskKind
from src.model.price import Price, SupplierPrice
from src.model.refs import Ref, TaskAddress
from src.model.task import PriceTask


def task(kind=TaskKind.CHANGE_PRICES, tm="Peli", collection="Vintage", digest=None):
    address = TaskAddress(tm=Ref(names=[tm]), subject=Ref(names=[collection]))
    return PriceTask(kind=kind, address=address, digest=dict(digest or {}))


def price(tasks, filename="Прайс Linderwood.xls"):
    return Price(supplier_price=SupplierPrice(supplier_id=1, file_id=1, file_path="p",
                                              filename=filename),
                 id=1, tasks=list(tasks))


class SummaryTest(unittest.TestCase):

    def test_nothing_changed_means_nothing_to_send(self):
        """«Изменений не было — не отправлять». Сообщение «изменений нет» бесполезно
        получателю и обесценивает следующие."""
        self.assertIsNone(report.summary(price([task(digest={})])))
        self.assertIsNone(report.summary(price([])))

    def test_zero_counts_are_not_news(self):
        """Ноль в дайджесте — это «работы не было», а не «работа с нулевым итогом»."""
        self.assertIsNone(report.summary(price([task(digest={"обновлены цены": 0})])))

    def test_normalization_alone_sends_nothing(self):
        """Про нормализацию менеджерам не говорят вовсе: регистр и пробелы в
        наименовании — не та новость, ради которой их дёргают."""
        self.assertIsNone(
            report.summary(price([task(kind=TaskKind.NORMALIZE_NAMES,
                                       digest={"нормализованы имена": 12})])))

    def test_normalization_is_dropped_but_the_rest_stays(self):
        text = report.summary(price([task(digest={"нормализованы имена": 12,
                                                  "обновлены цены": 4})]))
        self.assertIn("обновлены цены (4)", text)
        self.assertNotIn("нормализ", text.lower())

    def test_address_is_the_same_as_in_tasks(self):
        """Адресация ровно такая же, как у задач по прайсу: «марка / коллекция»."""
        text = report.summary(price([task(tm="Peli", collection="Elegance Large",
                                          digest={"заведены новые": 2})]))
        self.assertIn("Peli / Elegance Large: заведены новые (2)", text)

    def test_no_item_names_in_the_summary(self):
        """Конкретных товаров нет — только числа: список из сорока артикулов не читают."""
        text = report.summary(price([task(digest={"обновлены цены": 40})]))
        self.assertIn("(40)", text)
        self.assertNotIn("LE-", text)

    def test_tasks_of_one_collection_are_merged(self):
        """По коллекции бывает и цена, и заведение: менеджеру это одна строка."""
        rows = [task(collection="Vintage", digest={"обновлены цены": 4}),
                task(collection="Vintage", kind=TaskKind.ADD_NEW,
                     digest={"заведены новые": 2})]
        text = report.summary(price(rows))
        self.assertEqual(text.count("Peli / Vintage"), 1)
        self.assertIn("заведены новые (2), обновлены цены (4)", text)

    def test_composition_changes_come_before_prices(self):
        """Менеджер читает сверху: сперва то, что меняет состав каталога."""
        text = report.summary(price([task(digest={"уточнены размеры": 1,
                                                  "обновлены цены": 3,
                                                  "сняты с производства": 2})]))
        line = [ln for ln in text.splitlines() if "Peli" in ln][0]
        self.assertLess(line.index("сняты"), line.index("обновлены цены"))
        self.assertLess(line.index("обновлены цены"), line.index("уточнены размеры"))

    def test_price_is_named_by_its_file(self):
        text = report.summary(price([task(digest={"обновлены цены": 1})]))
        self.assertIn("Прайс Linderwood.xls", text)

    def test_header_overrides_the_file_name(self):
        text = report.summary(price([task(digest={"обновлены цены": 1})]),
                              header="Линдервуд")
        self.assertIn("Линдервуд", text)


class FakeBot:
    def __init__(self, dead=()):
        self.sent = []
        self._dead = set(dead)

    async def send_message(self, chat_id, text):
        if chat_id in self._dead:
            raise RuntimeError("bot can't initiate conversation")
        self.sent.append((chat_id, text))


class FakeUsers:
    def __init__(self, ids):
        self._ids = list(ids)

    async def list_all(self):
        from src.storage.users import AllowedUser
        return [AllowedUser(telegram_id=i, name=f"user{i}", added_by=1, added_at="")
                for i in self._ids]


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    """Сводка уходит МЕНЕДЖЕРАМ — всем из белого списка, кроме админа."""

    async def notify(self, ids, dead=(), kind=None, text="сводка"):
        from src.bot.model_handlers import ManagerListener
        from src.model.events import Event, EventKind

        bot = FakeBot(dead)
        listener = ManagerListener(bot, FakeUsers(ids), admin_id=100)
        await listener.notify(Event(kind=kind or EventKind.PRICE_SUMMARY, text=text))
        return bot

    async def test_managers_get_it_and_admin_gets_the_receipt(self):
        bot = await self.notify([100, 200, 300])
        to = [chat for chat, _ in bot.sent]
        self.assertEqual(to.count(200), 1)
        self.assertEqual(to.count(300), 1)
        # админу — не сводка, а строка о доставке
        receipt = [t for chat, t in bot.sent if chat == 100]
        self.assertEqual(len(receipt), 1)
        self.assertIn("2 из 2", receipt[0])

    async def test_undelivered_manager_is_named_to_the_admin(self):
        """Менеджер, ни разу не писавший боту, сообщения не получит — и админ должен
        узнать об этом, а не считать, что все оповещены."""
        bot = await self.notify([100, 200, 300], dead={300})
        receipt = [t for chat, t in bot.sent if chat == 100][0]
        self.assertIn("1 из 2", receipt)
        self.assertIn("user300", receipt)

    async def test_no_managers_is_said_out_loud(self):
        bot = await self.notify([100])
        self.assertEqual(bot.sent, [(100, "Сводка не отправлена: менеджеров в списке "
                                          "доступа нет.")])

    async def test_other_events_are_not_touched(self):
        from src.model.events import EventKind

        bot = await self.notify([100, 200], kind=EventKind.PRICE_STATUS)
        self.assertEqual(bot.sent, [])


if __name__ == "__main__":
    unittest.main()
