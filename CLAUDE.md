# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
# Запуск бота локально
.venv\Scripts\python -m src.main

# Все юнит-тесты
.venv\Scripts\python -m unittest discover tests

# Один тест-файл
.venv\Scripts\python -m unittest tests.test_classifier

# Живая проверка norwik.ru (не нужен .env)
.venv\Scripts\python -m tests.integration_norwik

# Установка зависимостей
.venv\Scripts\pip install -r requirements.txt
```

## Architecture

Telegram-бот с AI-агентом для менеджера norwik.ru: принимает текст/голос → агент ищет товар в почте и на сайте → отвечает ценами и контактами поставщиков.

### Поток данных

```
Telegram (aiogram) → AuthMiddleware → handlers.py
                                           ↓
                                    Orchestrator (Claude API manual loop)
                                     ├── ToolExecutor.execute(name, input)
                                     │    ├── MailClient (IMAP, sync via to_thread)
                                     │    └── NorwikClient (httpx, sync via to_thread)
                                     └── on_tool callback → edit_text в Telegram
```

### Ключевые детали реализации

**Оркестратор** (`src/agent/orchestrator.py`) — ручной agentic loop, не tool runner. Причина: нужна обработка `stop_reason == "pause_turn"` для серверного `web_search` (Anthropic-hosted). Цикл продолжается до `end_turn` или отсутствия `tool_uses`. Лимит `MAX_ITERATIONS = 30`. Принимает `on_tool` callback — вызывается перед каждым инструментом для обновления статуса в Telegram.

**Инструменты** (`src/agent/tools.py`) — 5 кастомных + 1 серверный:
- `search_emails` / `read_attachment` / `get_email_contacts` — IMAP
- `search_norwik` / `get_norwik_product` — парсинг norwik.ru
- `web_search` — `{"type": "web_search_20260209"}`, выполняется на стороне Anthropic (случай А: поставщик не найден)

**IMAP-клиент** (`src/email_tool/client.py`) — синхронный, вызывается через `asyncio.to_thread`. Строго read-only: `select(readonly=True)`, только `BODY.PEEK` (не ставит флаг `\Seen`). Операции записи не реализованы намеренно.

**Классификатор писем** (`src/email_tool/classifier.py`) — определяет тип письма (прайс/остатки) в порядке: тема → имя вложения → названия листов Excel. Парсит подпись для извлечения имени и телефона менеджера поставщика.

**NorwikClient** (`src/website_tool/norwik.py`) — поиск через `/search?query=`, цена из `itemprop="price" content="..."`. Один товар имеет несколько `<a href="item/...">` на странице (картинка + текст); выбирается лучший title через `_title_score()` (предпочтение кириллице и тексту с пробелами).

**Авторизация** (`src/bot/auth.py`) — middleware проверяет whitelist в SQLite (`src/storage/users.py`). `ADMIN_TELEGRAM_ID` из конфига всегда проходит; добавляет `is_admin: bool` в данные обработчика.

**Транскрипция голоса** (`src/bot/handlers.py`) — OpenAI Whisper API (`whisper-1`, `language="ru"`). Требует `OPENAI_API_KEY` в `.env`; без ключа возвращает пояснение пользователю.

**Статус действий** — при каждом вызове инструмента `on_tool` редактирует одно сообщение в чате (не создаёт новые). Маппинг `_TOOL_STATUS` в `handlers.py`.

### Конфигурация

`src/config.py` загружает `.env` через `python-dotenv`. Обязательные переменные: `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `MAIL_USER`, `MAIL_PASSWORD`, `ANTHROPIC_API_KEY`. Опциональные: `OPENAI_API_KEY` (голос), `MAIL_HOST`, `MAIL_PORT`, `DB_PATH`.

### Системный промпт

`src/agent/prompts.py` кодирует полную логику агента: валидация запроса (бренд + коллекция обязательны, для плитки — размер) → поиск в почте → сайт → 4 формата ответа (основной / случай А / Б / В). Промпт передаётся с `cache_control: ephemeral`.
