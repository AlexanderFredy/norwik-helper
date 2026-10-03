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
         ("ОбразецЛиста", "ЗначенияЛиста"),
         ("ОбразецБренда", "ЗначенияБренда"))


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


class CommandPayloadTest(unittest.TestCase):
    """Нагрузка команды обязана уезжать СТРОКОЙ, а не структурой (бой 02.10.2026).

    `Данные` в регистре `ии_МодельКоманды` — строка, и `ПоставитьКоманду` кладёт в неё то,
    что дали, без преобразования. Передав структуру, форма записывает команду с нагрузкой,
    из которой агент не достаёт ничего, и честно её отклоняет — а выглядит это как успех:
    форма закрылась, надпись погасла, выбор листов «сохранился» в пустоту. Свёртка — это
    `ДанныеКомандыВСтроку`.

    Проверяем ровно то, что было нарушено: четвёртый аргумент вызова не должен быть
    переменной, которой в этой же процедуре присвоили `Новый Структура`.
    """

    FORMS = ("model-form-module.bsl", "model-format-form-module.bsl")

    CALL = re.compile(r"ПоставитьКоманду\(\s*([^)]*?)\)", re.S)

    def test_payload_is_serialised(self):
        for name in self.FORMS:
            text = (BSL.parent / name).read_text(encoding="utf-8")
            structures = set(re.findall(
                r"^\s*([А-Яа-яЁёA-Za-z_]+)\s*=\s*Новый Структура", text, re.M))
            for call in self.CALL.finditer(text):
                args = [a.strip() for a in call.group(1).split(",")]
                if len(args) < 4:
                    continue
                payload = args[3]
                self.assertNotIn(
                    payload, structures,
                    f"{name}: в ПоставитьКоманду четвёртым аргументом уезжает структура "
                    f"«{payload}» — поле `Данные` строковое, нагрузка потеряется молча. "
                    f"Свернуть через ДанныеКомандыВСтроку.")


class PlatformFunctionsTest(unittest.TestCase):
    """В BSL для ЭТОЙ конфигурации нет части функций семейства `Стр*` (бой 03.10.2026).

    Платформа отказалась от `СтрСоединить`; раньше так же отказалась от `СтрНачинаетсяС` и
    `СтрЗаканчиваетсяНа` (22.09.2026). Ошибка вылезает не при выкладке модуля, а при первом
    вызове — то есть у админа на рабочей форме, а не у меня.

    Список — ТОЛЬКО подтверждённое админом, а не всё семейство 8.3.6. `СтрРазделить`,
    например, на сервере работает: пачка `find-items` отвечает живьём (проверено
    03.10.2026), и запрещать её значило бы выдумать ограничение.

    Своя реализация под тем же именем разрешена — так сделано в `find-items.bsl`.
    """

    MISSING = ("СтрСоединить", "СтрНачинаетсяС", "СтрЗаканчиваетсяНа")

    def test_no_calls_to_functions_this_platform_lacks(self):
        for path in sorted(BSL.parent.glob("*.bsl")):
            text = path.read_text(encoding="utf-8")
            own = set(re.findall(r"^\s*Функция\s+([А-Яа-яЁёA-Za-z_]+)", text, re.M))
            # Комментарии не считаем: имена называются в них по делу — ради объяснения,
            # почему написана своя функция.
            code = "\n".join(line for line in text.splitlines()
                             if not line.lstrip().startswith("//"))
            for name in self.MISSING:
                if name in own:
                    continue
                found = re.search(rf"\b{name}\s*\(", code)
                self.assertIsNone(
                    found,
                    f"{path.name}: вызов «{name}» — платформа этой конфигурации его не "
                    f"знает, и упадёт это при первом вызове, а не при выкладке. Напишите "
                    f"свою (образцы: СоединитьСтроки в model-form-module.bsl, "
                    f"РазделитьСтроку в properties.bsl).")


if __name__ == "__main__":
    unittest.main()
