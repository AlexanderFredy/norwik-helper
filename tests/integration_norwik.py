"""Интеграционная проверка norwik.ru (живые сетевые вызовы).

Запуск вручную: python -m tests.integration_norwik
Не входит в unittest discover (имя без префикса test_).
"""
import sys

from src.website_tool.norwik import NorwikClient


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    client = NorwikClient()
    try:
        product = client.get_product(2140)
        assert product is not None, "Товар 2140 не найден"
        print(f"Товар: {product.title}")
        print(f"URL:   {product.url}")
        print(f"Цена:  {product.price} {product.currency}")
        assert product.product_id == 2140
        assert product.price is not None and product.price > 0

        results = client.search("Porcelanosa Liston oxford")
        print(f"\nПоиск 'Porcelanosa Liston oxford': {len(results)} результатов")
        for r in results[:5]:
            print(f"  [{r.product_id}] {r.title[:80]}")
        assert any(r.product_id == 2140 for r in results), "2140 не найден поиском"

        missing = client.get_product(999999999)
        assert missing is None, "Несуществующий товар должен вернуть None"
        print("\nНесуществующий ID -> None: OK")
        print("\nВсе интеграционные проверки пройдены")
    finally:
        client.close()


if __name__ == "__main__":
    main()
