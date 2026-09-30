"""Живая проверка: сходится ли «быстрый» ответ про фото с тем, что на самой странице.

    python -m tests.integration_photos 189486 189490 189491

Зачем отдельный прогон. Вывод «фото есть» делается по КОСВЕННОМУ признаку: папка
`/images/products/<id>/` отвечает 403, когда она есть, и 404, когда нет. Это в восемь раз
быстрее страницы и не тратит трафика вовсе, но держится на настройке nginx, которую никто
не обещал не менять. Здесь мы сверяем его с прямым признаком — тегом `main_image` на самой
карточке. Разошлось — быстрый способ негоден: «фото есть» по пустой папке означало бы
молчаливо пропущенный товар, а это ровно то, что ищем.

Разведка 30.09.2026: 25 живых карточек, из них 9 без фото — совпало 25 из 25.

.env не нужен: сайт публичный.
"""
import re
import sys

import httpx

from src.website_tool import photos

#: Если ничего не передали — образцы из разведки: с фото и без.
DEFAULT_IDS = ("189486", "189490", "189491")


def by_page(client: httpx.Client, site_id: str) -> str:
    """Прямой признак: ссылка на картинку товара в самой странице."""
    r = client.get(f"/item/{site_id}")
    if r.status_code != 200:
        return photos.NO_CARD
    return photos.HAS if re.search(rf"/images/products/{site_id}/", r.text) \
        else photos.NONE


def main(ids: list[str]) -> int:
    page_client = httpx.Client(base_url=photos.BASE_URL, follow_redirects=True,
                               timeout=30.0, headers={"User-Agent": "Mozilla/5.0"})
    checker = photos.PhotoChecker()
    bad = 0
    try:
        print(f"{'id':>10}  {'быстро':<14} {'по странице':<14} сходится")
        for site_id in ids:
            quick = checker.status(site_id)
            slow = by_page(page_client, site_id)
            ok = quick == slow
            bad += not ok
            print(f"{site_id:>10}  {quick:<14} {slow:<14} {'да' if ok else 'НЕТ ***'}")
    finally:
        checker.close()
        page_client.close()

    print()
    if bad:
        print(f"РАСХОЖДЕНИЙ: {bad}. Быстрый способ больше не годен — проверку фото надо "
              "переводить на чтение страницы (см. src/website_tool/photos.py).")
        return 1
    print(f"Сошлось всё ({len(ids)} шт.).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or list(DEFAULT_IDS)))
