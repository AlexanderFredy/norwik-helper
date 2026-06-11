"""Поиск товаров на norwik.ru и парсинг карточки товара.

Механика сайта (разведано 2026-06-11):
- поиск: GET https://www.norwik.ru/search?query=<текст>
- ссылки в выдаче относительные: href="item/<id>"
- карточка: https://www.norwik.ru/item/<id>
- цена: <meta/span itemprop="price" content="...">
- ID: блок «ID: <strong>2140</strong>»
"""
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.norwik.ru"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; shop-helper)"}
_ID_RE = re.compile(r"ID:\s*<strong>(\d+)</strong>")


def _clean_title(text: str) -> str:
    """Убирает литеральные теги (сайт экранирует <b> в названиях) и лишние пробелы."""
    text = re.sub(r"</?\w+[^>]*>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_score(title: str) -> int:
    """Эвристика качества названия: текст со словами и кириллицей лучше
    служебных кодов вроде '9uu66u1_var2' из alt картинок."""
    if not title:
        return 0
    score = min(len(title), 80)
    if " " in title:
        score += 100
    if re.search(r"[А-Яа-яЁё]", title):
        score += 100
    return score


@dataclass(frozen=True)
class SearchResult:
    product_id: int
    title: str
    url: str


@dataclass(frozen=True)
class Product:
    product_id: int
    title: str
    url: str
    price: float | None
    currency: str = "руб."


class NorwikClient:
    """Синхронный клиент; вызывать через asyncio.to_thread."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers=_HEADERS,
            follow_redirects=True,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        response = self._client.get("/search", params={"query": query})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # На один товар в выдаче несколько ссылок (картинка, название);
        # собираем кандидатов и выбираем самый информативный.
        titles: dict[int, str] = {}
        order: list[int] = []
        for a in soup.find_all("a", href=re.compile(r"^/?item/\d+")):
            match = re.search(r"item/(\d+)", a["href"])
            product_id = int(match.group(1))
            if product_id not in titles:
                order.append(product_id)
                titles[product_id] = ""
            candidate = _clean_title(a.get_text(" ", strip=True))
            if not candidate:
                candidate = _clean_title(a.parent.get_text(" ", strip=True))
            if _title_score(candidate) > _title_score(titles[product_id]):
                titles[product_id] = candidate

        return [
            SearchResult(
                product_id=pid,
                title=titles[pid][:200],
                url=f"{BASE_URL}/item/{pid}",
            )
            for pid in order[:limit]
            if titles[pid]
        ]

    def get_product(self, product_id: int) -> Product | None:
        response = self._client.get(f"/item/{product_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        # подтверждаем, что это карточка товара (на ней есть блок «ID: …»)
        id_match = _ID_RE.search(html)
        if not id_match:
            return None

        title_tag = soup.find("h1") or soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else f"Товар {product_id}"
        title = re.sub(r"\s*\|\s*Norwik\.ru\s*$", "", title)

        price: float | None = None
        price_tag = soup.find(attrs={"itemprop": "price"})
        if price_tag:
            raw = price_tag.get("content") or price_tag.get_text(strip=True)
            try:
                price = float(str(raw).replace("\xa0", "").replace(" ", "").replace(",", "."))
            except ValueError:
                price = None

        return Product(
            product_id=int(id_match.group(1)),
            title=title,
            url=f"{BASE_URL}/item/{product_id}",
            price=price,
        )
