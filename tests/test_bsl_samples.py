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


class CompareGuardTest(unittest.TestCase):
    """Сравнение значений обязано переживать отсутствие строки (бой 02.10.2026).

    У прайсов и задач защита `Было <> Неопределено` стояла на стороне вызывающего, и
    применение листов её не повторило: на первом снимке, где все строки новые,
    `Неопределено.Свойство(...)` уронил применение ЦЕЛИКОМ. Защита переехала внутрь
    функции — иначе четвёртая сущность наступит на то же место.
    """

    def test_comparison_handles_a_missing_row(self):
        source = body(BSL.read_text(encoding="utf-8"), "ОдинаковыеЗначения")
        head = source[:source.index("Для Каждого")]
        self.assertIn("Текущее = Неопределено", head,
                      "проверка на отсутствие строки должна стоять ДО обхода полей")


class FormQueryTest(unittest.TestCase):
    """Поле, читаемое из строки таблицы формы, обязано быть ВЫБРАНО в запросе (бой
    02.10.2026).

    Колонку реквизита формы мало объявить: `ЗаполнитьЗначенияСвойств` заполняет её из
    выборки, и без `Т.Поле КАК Поле` в запросе колонка ЕСТЬ и всегда пуста. Ошибки при этом
    никакой — двойной щелчок по имени файла честно отвечал «формат не записан» по прайсу, у
    которого формат записан.
    """

    FORM = BSL.parent / "model-form-module.bsl"

    #: Колонки, которых в запросах нет по построению: их считает сам модуль.
    COMPUTED = {"Пометка"}

    def test_every_read_field_is_selected(self):
        text = self.FORM.read_text(encoding="utf-8")
        read = set(re.findall(r"Текущий\.([А-Яа-яЁёA-Za-z_]+)", text))
        selected = set(re.findall(r"\|\s*\w+\.([А-Яа-яЁёA-Za-z_]+) КАК", text))
        missing = read - selected - self.COMPUTED
        self.assertEqual(missing, set(),
                         f"модуль читает поля, которых нет ни в одном запросе: "
                         f"{sorted(missing)} — колонка будет всегда пустой, без ошибки")


if __name__ == "__main__":
    unittest.main()
