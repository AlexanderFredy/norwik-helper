# Shop Helper — агент-помощник менеджера интернет-магазина

AI-агент с Telegram-ботом: по запросу менеджера находит товар у поставщиков
(письма на mail.ru — только чтение), показывает остатки, закупочные цены и
цену на сайте norwik.ru.

Документация: [master-spec.md](specs/master-spec.md) · [cart-orders-1c.md](specs/cart-orders-1c.md) · [implementation-plan.md](implementation-plan.md)

## Запуск локально

```sh
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
cp .env.example .env                            # заполнить значения
.venv\Scripts\python -m src.main
```

### Настройка почты mail.ru

1. В настройках ящика включить IMAP.
2. Создать **пароль приложения** (Настройки → Безопасность → Пароли для внешних
   приложений) — основной пароль не подойдёт.
3. Указать его в `MAIL_PASSWORD`.

### Настройка Telegram

1. Создать бота у @BotFather, токен — в `TELEGRAM_BOT_TOKEN`.
2. Свой Telegram ID (узнать у @userinfobot) — в `ADMIN_TELEGRAM_ID`.
3. Админ добавляет менеджеров: `/adduser <telegram_id> [имя]`.
   Прочие команды: `/removeuser <id>`, `/listusers`.

## Деплой на VPS (Docker)

```sh
git clone <repo> && cd shop-helper
cp .env.example .env   # заполнить
docker compose up -d --build
docker compose logs -f # проверить запуск
```

База whitelist хранится в volume `bot-data` и переживает пересборку.

## Тесты

```sh
.venv\Scripts\python -m unittest discover tests   # юнит-тесты
.venv\Scripts\python -m tests.integration_norwik  # живая проверка norwik.ru
```

## E2E чек-лист

1. Неавторизованный пользователь → «Доступ запрещён»
2. Неполный запрос («Ламинат Kronospan 33 класс») → уточняющий вопрос
3. Полный запрос → поставщики, остатки, цены, ссылка на сайт
4. Товара нет, бренд есть в почте → список поставщиков бренда
5. Ни товара, ни бренда → ссылка на сайт производителя (web search)
6. Товар есть в почте, нет на сайте → «товар не найден на norwik.ru»
