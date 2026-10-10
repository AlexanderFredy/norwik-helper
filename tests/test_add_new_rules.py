"""Добавление новых позиций: размеры в мм и цены (бой 10.10.2026, Atlas Concorde у Кераматики).

Агент на Haiku 5.5 завёл позиции с размерами в сантиметрах («120 х 60 х 0,9» из колонки
«Формат» ушло в «Длину» 120, в «Толщину» 0,9) и закрыл задачу, ни разу не записав цены,
хотя скидка дилера стояла в задании.
"""
import unittest

from src.model.enums import TaskKind, TaskStatus
from src.model.executor import TaskTools
from src.price_tool.items import to_millimetres


def allow():
    return None


class MillimetresTest(unittest.TestCase):

    def test_tile_format_in_centimetres_becomes_millimetres(self):
        out, converted = to_millimetres({"length": 120, "width": 60, "thickness": 0.9})
        self.assertTrue(converted)
        self.assertEqual((out["length"], out["width"], out["thickness"]), (1200, 600, 9))

    def test_ranges_are_converted_too(self):
        out, converted = to_millimetres({"length_from": 60, "length_to": 120, "width": 60,
                                         "thickness": 0.8})
        self.assertTrue(converted)
        self.assertEqual((out["length_from"], out["length_to"]), (600, 1200))

    def test_two_millimetre_vinyl_is_left_alone(self):
        """Виниловый ламинат бывает в 2 мм — но стороны у него за метр."""
        raw = {"length": 1220, "width": 180, "thickness": 2}
        self.assertEqual(to_millimetres(raw), (raw, False))

    def test_millimetres_stay_millimetres(self):
        raw = {"length": 1200, "width": 600, "thickness": 9}
        self.assertEqual(to_millimetres(raw), (raw, False))

    def test_no_thickness_no_guess(self):
        """Без толщины отличить см от мм не по чему — оставляем как пришло."""
        raw = {"length": 120, "width": 60}
        self.assertEqual(to_millimetres(raw), (raw, False))


class PricesReminderTest(unittest.TestCase):

    def tools(self, kind=TaskKind.ADD_NEW):
        return TaskTools(None, b"", "p.xlsx", allow, kind=kind)

    def finish(self, tools, status="выполнена"):
        return tools._finish({"status": status, "result": "Заведено 6 позиций Boost Balance."})

    def test_new_positions_without_prices_get_one_reminder(self):
        tools = self.tools()
        tools.created_items = 6
        answer = self.finish(tools)
        self.assertIn("write_prices", answer)
        self.assertIsNone(tools.outcome, "задача не закрыта")

    def test_second_finish_is_accepted(self):
        """Если цены в прайсе правда нет — повторный finish с причиной принимается."""
        tools = self.tools()
        tools.created_items = 6
        self.finish(tools)
        self.finish(tools, status="частично обработана")
        self.assertEqual(tools.outcome[0], TaskStatus.PARTIAL)

    def test_no_reminder_once_prices_were_written(self):
        tools = self.tools()
        tools.created_items = 6
        tools.prices_attempted = True
        self.finish(tools)
        self.assertEqual(tools.outcome[0], TaskStatus.DONE)

    def test_other_kinds_are_not_reminded(self):
        tools = self.tools(TaskKind.CHANGE_PROPERTIES)
        tools.created_items = 1
        self.finish(tools)
        self.assertIsNotNone(tools.outcome)


if __name__ == "__main__":
    unittest.main()
