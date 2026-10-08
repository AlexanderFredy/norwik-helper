"""Прайсы, сохранённые на сервере под отложенные задачи (§9.7).

Обычный прайс живёт в памяти процесса на время диалога и после прогона выбрасывается. Но
если по нему что-то отложено, вернуться к задаче без файла нельзя — а просить админа
пересылать 12-мегабайтный прайс через полторы недели значит просто не вернуться никогда.

Поэтому файл кладётся на диск **лениво**: только когда заводится первая отложенная задача,
и удаляется, как только исчезла последняя ссылающаяся на него задача. Архива прайсов здесь
нет и не предполагается — нет задачи, нет файла.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DIR_NAME = "prices"
_SAFE_EXT = re.compile(r"^\.[a-z0-9]{1,8}$")


def text(path) -> str:
    """Путь для базы — ВСЕГДА с прямыми слэшами.

    **БАЗА ПЕРЕЕЗЖАЕТ С WINDOWS НА LINUX** (решение 08.10.2026: бот переходит на VPS). На
    Windows `str(Path)` даёт «data\\prices\\x.xlsx», а на Linux обратный слэш — обычный
    символ имени: такой путь не найдётся НИКОГДА, и уборка сирот (`sweep`) при первом же
    старте сочла бы все прайсы осиротевшими и стёрла их. Прямой слэш понимают обе системы.
    """
    return str(path or "").replace("\\", "/")


def to_path(path) -> Path:
    """Путь из базы — в файл. Понимает и старую запись с обратными слэшами."""
    return Path(text(path))


def _dir(db_path: Path) -> Path:
    return db_path.parent / DIR_NAME


def _ext(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if _SAFE_EXT.match(ext) else ".bin"


def save(db_path: Path, filename: str, content: bytes) -> Path | None:
    """Положить прайс рядом с базой. Имя — от содержимого, поэтому копий не плодит.

    Возвращает путь либо None, если записать не удалось: отложить задачу всё равно нужно,
    просто вернуться к ней получится только с присланным заново файлом.
    """
    if not content:
        return None
    try:
        folder = _dir(db_path)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (hashlib.sha1(content).hexdigest()[:16] + _ext(filename))
        if not path.exists():
            path.write_bytes(content)
        return path
    except OSError:
        logger.exception("Не удалось сохранить прайс для отложенной задачи")
        return None


def load(path: str | Path | None) -> bytes | None:
    if not path:
        return None
    try:
        file = to_path(path)
        return file.read_bytes() if file.is_file() else None
    except OSError:
        logger.exception("Не удалось прочитать сохранённый прайс %s", path)
        return None


def forget(paths) -> int:
    """Удалить файлы, на которые больше не ссылается ни одна задача."""
    removed = 0
    for path in paths or []:
        try:
            file = to_path(path)
            if file.is_file():
                file.unlink()
                removed += 1
        except OSError:
            logger.warning("Не удалось удалить сохранённый прайс %s", path, exc_info=True)
    return removed


def sweep(db_path, known) -> int:
    """Удалить прайсы, на которые больше никто не ссылается (§9.8).

    Файл пишется на диск раньше, чем строка в базе, — между этими шагами процесс может
    упасть, и останется сирота. По одному такому файлу беда невелика, но за месяцы работы
    каталог вырастет на десятки мегабайт, и понять, что там лишнее, будет уже нельзя.

    Чистим ТОЛЬКО при старте и ТОЛЬКО свой каталог: пока бот работает, файл может быть
    создан секунду назад и ещё не попасть в базу — гонка, в которой мы стёрли бы нужное.
    """
    folder = _dir(Path(db_path))
    if not folder.is_dir():
        return 0

    keep = {str(to_path(p).resolve()) for p in known or () if p}

    # ПРЕДОХРАНИТЕЛЬ: ссылки есть, а ни одна не указывает на существующий файл — значит
    # пути в базе не того вида (переезд базы, смена каталога), а не «все файлы сироты».
    # Стереть в такой момент весь каталог — необратимо; лучше не убрать ни одной сироты.
    if keep and not any(Path(p).is_file() for p in keep):
        logger.warning("Уборка прайсов пропущена: ни одна из %d ссылок базы не указывает "
                       "на существующий файл — пути, вероятно, не того вида", len(keep))
        return 0

    removed = 0
    for file in folder.iterdir():
        try:
            if file.is_file() and str(file.resolve()) not in keep:
                file.unlink()
                removed += 1
        except OSError:
            logger.warning("Не удалось убрать осиротевший прайс %s", file, exc_info=True)

    if removed:
        logger.info("Убрано осиротевших прайсов: %d", removed)
    return removed
