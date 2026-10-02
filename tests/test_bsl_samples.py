"""Образец структуры и присваивание обязаны совпадать по составу полей (бой 02.10.2026).

В `set-model-state.bsl` каждая сущность описана ПАРОЙ функций: `ОбразецX` создаёт структуру
с полями, `ЗначенияX` их заполняет. `ЗаполнитьЗначенияСвойств` заполняет только СУЩЕСТВУЮЩИЕ
свойства, а присваивание отсутствующего падает с «Object field not found» — и падает не при
выкладке, а при первом снимке, то есть зеркало перестаёт применяться ЦЕЛИКОМ.

02.10.2026 поле `Сигнатура` добавили в `ЗначенияПрайса` и забыли в `ОбразецПрайса`. Стоило
это круга выкладки и живого разбора по тексту ошибки. Проверка статическая: BSL здесь не
запускается, читается как текст — этого довольно, чтобы поймать расхождение состава.
"""
import re
import unittest
from pathlib import Path

BSL = Path(__file__).resolve().parent.parent / "specs" / "1c" / "set-model-state.bsl"

#: Пары «образец → заполнитель». Имена не выводятся из текста намеренно: список обязан
#: ломаться, когда появится третья сущность без проверки, а не молча её пропускать.
PAIRS = (("ОбразецПрайса", "ЗначенияПрайса"),
         ("ОбразецЗадачи", "ЗначенияЗадачи"),
         ("ОбразецЛиста", "ЗначенияЛиста"))


def body(text: str, name: str) -> str:
    """Тело функции по имени — от объявления до `КонецФункции`."""
    start = re.search(rf"^Функция\s+{name}\s*\(", text, re.M)
    assert start, f"в файле нет функции {name}"
    end = text.index("КонецФункции", start.end())
    return text[start.end():end]


class SamplesMatchTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = BSL.read_text(encoding="utf-8")

    def fields_of_sample(self, name: str) -> set[str]:
        source = body(self.text, name)
        # Образец собирают двумя способами: `Вставить("Имя", …)` и строкой-конструктором
        # `Новый Структура("А,Б,В", …)`. Проверка обязана понимать оба, иначе она молча
        # пропустит ту сущность, которая описана другим.
        fields = set(re.findall(r'Вставить\("([^"]+)"', source))
        for listing in re.findall(r'Новый\s+Структура\(\s*"([^"]+)"', source):
            fields |= {part.strip() for part in listing.split(",") if part.strip()}
        return fields

    def fields_assigned(self, name: str) -> set[str]:
        source = body(self.text, name)
        return set(re.findall(r"Значения\.([А-Яа-яЁёA-Za-z_]+)\s*=", source))

    def test_every_assigned_field_exists_in_the_sample(self):
        for sample, filler in PAIRS:
            with self.subTest(sample):
                missing = self.fields_assigned(filler) - self.fields_of_sample(sample)
                self.assertEqual(missing, set(),
                                 f"{filler} заполняет поля, которых нет в {sample}: "
                                 f"{sorted(missing)} — снимок упадёт на «Object field "
                                 f"not found» при первом же применении")

    def test_samples_are_not_empty(self):
        """Пустой образец — признак того, что разбор сломался, а не что полей нет."""
        for sample, _ in PAIRS:
            with self.subTest(sample):
                self.assertTrue(self.fields_of_sample(sample))

    def test_signature_travels_all_the_way(self):
        """Поле, из-за которого проверка и появилась: сигнатура нужна форме выбора листов,
        и до регистра она доезжает только через образец."""
        self.assertIn("Сигнатура", self.fields_of_sample("ОбразецПрайса"))
        self.assertIn("Сигнатура", self.fields_assigned("ЗначенияПрайса"))


if __name__ == "__main__":
    unittest.main()
