"""Чей логотип на баннере: один вопрос модели на картинку, и больше никогда.

**ЗАЧЕМ.** Бренд в прайсе бывает обозначен не словом, а картинкой — так у A+ Floor. Код
знает, в какой СТРОКЕ лежит баннер (`parser.image_anchor_rows`), но прочитать имя на нём не
может: это изображение. Единственный, кто может, — модель, и это ровно тот случай, когда её
участие оправдано: решения тут нет, есть чтение, которого код не умеет.

**СПРАШИВАЕМ ОДИН РАЗ НА КАРТИНКУ и помним по её ХЕШУ.** Прайс того же поставщика приходит
каждый месяц с теми же логотипами: ключ по хешу делает второй и все последующие файлы
бесплатными. Ключ именно хеш, а НЕ номер строки — строки в новом файле сдвигаются, и
запомненная раскладка молча приписала бы бренды чужим позициям (то же правило, по которому
`price_layout` ключуется содержимым файла).

**«НЕ ЛОГОТИП» — ТОЖЕ ОТВЕТ, и он сохраняется.** В прайсах полно рамок, значков акций и фото
товара; без этой отметки мы спрашивали бы о них в каждом прогоне. Пустое имя значит «смотрели,
бренда тут нет», а не «не смотрели».

**ОДИН ВЫЗОВ НА ВСЕ НЕИЗВЕСТНЫЕ СРАЗУ.** Круг ручного цикла несёт всю историю и стоит
~$0.097 — поштучные вопросы о десяти логотипах стоили бы доллар вместо десяти центов.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

#: Сколько картинок спрашиваем за один раз. Больше — это уже не баннеры брендов, а фото
#: товаров: у Монарха 93 картинки на листе, и все они привязаны к первой строке (замер
#: 04.10.2026). Такой лист разделителями не размечен, и платить за его разбор не за что.
MAX_LOGOS = 24

PROMPT = (
    "Ты смотришь на картинки, вырезанные из прайс-листа поставщика отделочных материалов. "
    "Каждая может быть ЛОГОТИПОМ ТОРГОВОЙ МАРКИ (баннер-разделитель перед блоком её "
    "товаров), а может быть чем угодно другим: рамкой, значком акции, фотографией товара, "
    "гербом, элементом вёрстки.\n\n"
    "Ответь СТРОГО JSON-массивом, по одному элементу на картинку, в том же порядке: "
    '[{"n": 1, "brand": "Kronotex"}, {"n": 2, "brand": ""}]\n\n'
    "ПРАВИЛА:\n"
    "— `brand` — название марки РОВНО как написано на картинке, без пояснений;\n"
    "— не логотип марки либо надпись не читается — пустая строка, и это нормальный ответ;\n"
    "— БАННЕР КОЛЛЕКЦИИ — НЕ МАРКА: надпись «Collection», «коллекция» или имя коллекции "
    "внутри марки (VINTAGE Collection, ELEGANCE Collection) — пустая строка. В прайсе такие "
    "баннеры стоят рядом с логотипом марки, и спутать их значит резать файл не там;\n"
    "— КАРТИНКА БЫВАЕТ КРИВОЙ: повёрнутой на бок, снимком экрана с панелью задач и окнами "
    "редактора, обрезанной. Название марки на ней всё равно читается — читай и называй; "
    "мусор вокруг значения не имеет (у Линдервуда логотип вставлен именно так);\n"
    "— НЕ УГАДЫВАЙ по виду товара или по цвету: неверная марка хуже отсутствующей, по ней "
    "будут отобраны строки прайса и записаны цены чужой марке;\n"
    "— название завода-изготовителя маркой НЕ считается, если это не то же имя.\n\n"
    "Никакого текста кроме JSON."
)


def image_hash(data: bytes) -> str:
    """Хеш картинки — ключ памяти. SHA-256 от байтов: тот же логотип в следующем файле
    поставщика лежит байт в байт, а вёрстка вокруг него меняется свободно."""
    return hashlib.sha256(data or b"").hexdigest()


def _parse(answer: str, count: int) -> dict[int, str]:
    """Разобрать ответ модели: {номер картинки (с 1) → имя}. Мусор — молча мимо."""
    text = (answer or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        rows = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        logger.warning("Ответ о логотипах не разобрался как JSON")
        return {}

    out: dict[int, str] = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        try:
            number = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= number <= count:
            out[number] = str(row.get("brand") or "").strip()
    return out


async def name_logos(orchestrator, suppliers, signature: str, content: bytes,
                     filename: str) -> dict:
    """Строки баннеров с именами брендов: `{лист: {строка: бренд}}`.

    Известные логотипы берутся из памяти, неизвестные — одним вопросом модели. Пустые имена
    в результат не попадают: строка без бренда ничего не размечает.

    Ни модели, ни памяти — работаем на том, что есть: отсутствие картинок это не ошибка, а
    самый обычный прайс.
    """
    from src.price_tool.parser import extract_images

    try:
        found = extract_images(content) or {}
    except Exception:                                   # noqa: BLE001
        logger.warning("Картинки прайса %s не извлеклись", filename, exc_info=True)
        return {}
    if not found:
        return {}

    known = await suppliers.logos_for(signature) if suppliers else {}

    # Одна картинка на хеш: один и тот же логотип стоит в прайсе много раз.
    unknown: dict[str, tuple[bytes, str]] = {}
    for images in found.values():
        for _row, data, media in images:
            key = image_hash(data)
            if key not in known and key not in unknown:
                unknown[key] = (data, media)

    if unknown and orchestrator is not None and len(unknown) <= MAX_LOGOS:
        named = await _ask(orchestrator, unknown, filename)
        if named and suppliers:
            await suppliers.remember_logos(signature, list(named.items()))
        known = {**known, **named}
    elif unknown and len(unknown) > MAX_LOGOS:
        logger.info("Картинок в %s слишком много (%d) — это не разделители брендов",
                    filename, len(unknown))

    out: dict[str, dict[int, str]] = {}
    for sheet, images in found.items():
        for row, data, _media in images:
            brand = (known.get(image_hash(data)) or "").strip()
            if brand:
                out.setdefault(sheet, {})[int(row)] = brand
    return out


async def _ask(orchestrator, unknown: dict, filename: str) -> dict[str, str]:
    """Спросить модель об НЕизвестных логотипах. Возвращает {хеш: имя}, включая пустые."""
    keys = list(unknown)
    blocks: list[dict] = [{"type": "text", "text": PROMPT}]
    for number, key in enumerate(keys, 1):
        data, media = unknown[key]
        blocks.append({"type": "text", "text": f"Картинка {number}:"})
        blocks.append({"type": "image",
                       "source": {"type": "base64", "media_type": media,
                                  "data": _b64(data)}})

    try:
        answer, _ = await orchestrator.handle_turn(
            [{"role": "user", "content": blocks}],
            system=PROMPT, base_tools=False,
            usage_labels={"kind": "logo_intake", "price_doc": filename})
    except Exception:                                   # noqa: BLE001
        logger.warning("Не удалось прочитать логотипы прайса %s", filename, exc_info=True)
        return {}

    got = _parse(answer, len(keys))
    # ЗАПИСЫВАЕМ ВСЕ, о чём спросили, — в том числе пустые ответы: «это не логотип» нужно
    # запомнить ровно так же, как имя, иначе та же картинка поедет в модель следующий раз.
    return {key: got.get(number, "") for number, key in enumerate(keys, 1)}


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data or b"").decode("ascii")
