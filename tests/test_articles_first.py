"""Артикулы раньше снятия (алгоритм админа 28.10.2026, Mark у Артисаны)."""
import unittest

from src.model.commands import Command, CommandKind
from src.model.enums import TaskKind, TaskStatus, TaskSubject
from src.model.price import Price, SupplierPrice
from src.model.refs import Ref, TaskAddress
from src.model.service import PriceListService
from src.model.task import PriceTask


def address(collection="Mark", tm="000000115"):
    return TaskAddress(tm=Ref.make(code=tm, names=["Atlas Concorde"]),
                       subject=Ref.make(names=[collection]),
                       subject_kind=TaskSubject.COLLECTION)


class Service(PriceListService):
    def __init__(self, prices):
        self._prices = prices


def setup(*tasks):
    price = Price(supplier_price=SupplierPrice(supplier_id=1, file_id=1, file_path="p.xls"),
                  id=8, tasks=list(tasks))
    return Service([price]), price


def run(task_id, seq):
    return Command(kind=CommandKind.EXECUTE_TASK, source="1c", actor="1c:Саша",
                   price_id=8, task_id=task_id, payload={"seq": seq})


class ArticlesFirstTest(unittest.TestCase):

    def test_batch_moves_discontinue_after_props_of_the_same_collection(self):
        """В списке снятие стоит первым (решение 24.09.2026), но в пачке оно ждёт
        «изменение свойств» той же коллекции: иначе увезло бы неопознанные позиции."""
        move = PriceTask(kind=TaskKind.MOVE_DISCONTINUED, address=address(), id=1)
        props = PriceTask(kind=TaskKind.CHANGE_PROPERTIES, address=address(), id=2)
        other = PriceTask(kind=TaskKind.MOVE_DISCONTINUED, address=address("Nid"), id=3)
        service, _ = setup(move, props, other)
        order = service.articles_first([run(1, 1), run(3, 2), run(2, 3)])
        self.assertEqual([c.task_id for c in order], [3, 2, 1])

    def test_lone_discontinue_waits_for_open_props(self):
        move = PriceTask(kind=TaskKind.MOVE_DISCONTINUED, address=address(), id=1)
        props = PriceTask(kind=TaskKind.CHANGE_PROPERTIES, address=address(), id=2)
        service, price = setup(move, props)
        self.assertIs(service.open_props_before(price, move), props)

        props.complete(TaskStatus.PARTIAL, "часть артикулов проставлена")
        self.assertIsNone(service.open_props_before(price, move),
                          "закрытая (в т.ч. частично) задача снятие не держит")

    def test_stub_addresses_are_not_held(self):
        """У заглушки адрес — имя листа без кода марки: все виды на одном адресе."""
        stub = address(tm="")
        move = PriceTask(kind=TaskKind.MOVE_DISCONTINUED, address=stub, id=1)
        props = PriceTask(kind=TaskKind.CHANGE_PROPERTIES, address=stub, id=2)
        service, price = setup(move, props)
        self.assertIsNone(service.open_props_before(price, move))


if __name__ == "__main__":
    unittest.main()
