"""Есть ли у товара фото на norwik.ru.

**КАК ЭТО УЗНАЁТСЯ, И ПОЧЕМУ НЕ СТРАНИЦЕЙ.** На карточке с фото стоит `<img id="main_image"
src="/images/products/<id>/…">`, у карточки без фото этого тега нет вовсе — признак
однозначный, но страница весит 170 КБ и отдаётся 0,8 с. Папка же `/images/products/<id>/`
отвечает **403, когда фото есть** (листинг закрыт) и **404, когда его нет**, а `HEAD` по ней
— 0,1 с и ноль байт. Разведка 30.09.2026: оба способа сверены на 25 живых карточках, среди
них 9 без фото, — совпало 25 из 25. Обход трёх тысяч позиций отличается от «минуты и
полгигабайта» до «минуты и ноль трафика».

**ИСХОДОВ ЧЕТЫРЕ, А НЕ ДВА**, и слипаться им нельзя:

* `HAS` — фото есть;
* `NONE` — карточка на сайте есть, фото нет: это и есть работа;
* `NO_CARD` — карточки на сайте нет вовсе. Выгрузка на сайт идёт не мгновенно, и только
  что заведённый товар там честно отсутствует. Показать его как «без фото» — послать
  человека искать то, чего ещё нет;
* `FAILED` — сайт не ответил. Тоже НЕ «нет фото»: придуманная работа хуже ненайденной, а
  сеть рвётся и без нашего участия.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://www.norwik.ru"

HAS = "есть"
NONE = "нет"
NO_CARD = "нет карточки"
FAILED = "сайт не ответил"

#: Сколько проверок идёт одновременно. Сайт наш собственный, но обход это всё же
#: несколько тысяч запросов подряд: восемь потоков дают минуту на каталог и не выглядят
#: для сайта нагрузкой, ради которой стоило бы что-то настраивать.
WORKERS = 8


def item_url(site_id: str) -> str:
    """Адрес карточки. Один на весь код: он попадает и в отчёт, и в проверку, и разойтись
    эти два места не должны."""
    return f"{BASE_URL}/item/{site_id}"


class PhotoChecker:
    """Синхронный; вызывать через `asyncio.to_thread`, как и остальные клиенты сайта."""

    def __init__(self, timeout: float = 15.0, workers: int = WORKERS) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL, follow_redirects=True, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; norwik-helper)"})
        self._workers = max(1, workers)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def status(self, site_id: str) -> str:
        """Исход по одному товару.

        Порядок проверок выбран по цене: сперва папка (0,1 с, ноль байт), и лишь когда её
        нет — карточка, чтобы отличить «фото не добавили» от «товара на сайте ещё нет».
        Второй запрос уходит только по проблемным позициям, а их меньшинство.
        """
        site_id = (site_id or "").strip()
        if not site_id:
            # Пустой id — товар ни разу не выгружался на сайт: его карточки нет по
            # определению, и спрашивать о ней нечего.
            return NO_CARD
        try:
            if self._client.head(f"/images/products/{site_id}/").status_code == 403:
                return HAS
            return NONE if self._card_exists(site_id) else NO_CARD
        except Exception as exc:                             # noqa: BLE001
            logger.warning("Фото %s: сайт не ответил — %s", site_id, exc)
            return FAILED

    def _card_exists(self, site_id: str) -> bool:
        return self._client.head(f"/item/{site_id}").status_code == 200

    def statuses(self, site_ids) -> dict[str, str]:
        """Исходы пачкой, параллельно. Ключ — id сайта.

        Повторы в списке схлопываются: один id — один запрос, сколько бы позиций на него
        ни ссылалось.
        """
        unique = sorted({(i or "").strip() for i in site_ids})
        if not unique:
            return {}
        with ThreadPoolExecutor(max_workers=min(self._workers, len(unique))) as pool:
            return dict(zip(unique, pool.map(self.status, unique)))
