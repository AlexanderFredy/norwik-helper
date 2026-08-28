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
        file = Path(path)
        return file.read_bytes() if file.is_file() else None
    except OSError:
        logger.exception("Не удалось прочитать сохранённый прайс %s", path)
        return None


def forget(paths) -> int:
    """Удалить файлы, на которые больше не ссылается ни одна задача."""
    removed = 0
    for path in paths or []:
        try:
            file = Path(path)
            if file.is_file():
                file.unlink()
                removed += 1
        except OSError:
            logger.warning("Не удалось удалить сохранённый прайс %s", path, exc_info=True)
    return removed
