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

**Инструменты** (`src/agent/tools.py`) — 6 кастомных + 1 серверный:
- `search_emails` / `read_attachment` / `get_email_contacts` — IMAP
- `search_norwik` / `get_norwik_product` — парсинг norwik.ru
- `get_price_history` — «когда меняли цены»: даты из 1С (`by-tm`), источник цены из
  журнала `price_writes`; форматирует ответ код (`src/price_tool/history.py`), не модель
- `web_search` — `{"type": "web_search_20260209"}`, выполняется на стороне Anthropic (случай А: поставщик не найден)

**IMAP-клиент** (`src/email_tool/client.py`) — синхронный, вызывается через `asyncio.to_thread`. Строго read-only: `select(readonly=True)`, только `BODY.PEEK` (не ставит флаг `\Seen`). Операции записи не реализованы намеренно.

**Классификатор писем** (`src/email_tool/classifier.py`) — определяет тип письма (прайс/остатки) в порядке: тема → имя вложения → названия листов Excel. Парсит подпись для извлечения имени и телефона менеджера поставщика.

**NorwikClient** (`src/website_tool/norwik.py`) — поиск через `/search?query=`, цена из `itemprop="price" content="..."`. Один товар имеет несколько `<a href="item/...">` на странице (картинка + текст); выбирается лучший title через `_title_score()` (предпочтение кириллице и тексту с пробелами).

**Итог прогона** (`_finish_run` в `pricing_handlers.py`) — единственное место, где
печатаются отложенные замечания по прайсу целиком (`price_run.notes`, инструмент
`add_final_note`) и строка «обработан полностью». Один выход на оба пути: и после кнопки
на последней марке, и когда по ней нечего было писать. Замечания уровня файла НЕ должны
попадать в предложение по марке — иначе повторяются под каждым брендом.

**Прогон прайса по маркам** (`price_run` в `src/storage/pricing.py`) — предложение
строится на ОДНУ ТМ (`propose_prices` откажет, если передали несколько), в хвосте
«Осталось обработать: …». После нажатия кнопки обработчик сам продолжает диалог следующей
маркой через `_run(..., user_id=...)` — **user_id передаётся явно**, потому что там
`message` это сообщение бота и `from_user` в нём бот, а не админ. Режим прайса и файл в
`_files` живут до конца прогона.

**Скачки цен** (`unit_warnings` в `src/price_tool/changes.py`) — изменение больше чем в
1.5 раза выносится наверх предупреждений. Причина определяется ПО КОЛЛЕКЦИИ, в порядке:
часть позиций уже стоит новую цену (значит в папке разные товары) → цены внутри папки
разные → и только для однородной коллекции гипотеза «цена за упаковку». Порядок важен:
на Classen отношение 2.02 совпало с упаковкой 1.974 случайно, цена была верной, а
выбивалась одна позиция. Не блокирует запись.

**Маппинг колонок прайса** (`src/storage/pricing.py`) — ключ **`(signature, sheet)`**, по
строке на лист. Одна строка на файл приводила к тому, что запомненный лист сужал работу
агента до себя, а остальные листы мультилистового прайса молча выпадали. Блок «ЗАПОМНЕННЫЙ
МАППИНГ» в `read_price_file` обязан явно требовать разбора остальных листов. Старая схема
мигрирует в `init()` (лист достаётся из JSON и переезжает в ключ).

**Категории товаров** (`src/price_tool/scope.py`) — что вообще анализируем; задаётся
командами `/categories` один раз на все прайсы. Пустой список = ограничений нет, а не
наоборот. Сопоставление с `product_type` из 1С нестрогое (вхождение подстроки), поэтому
«плитка» покрывает «Керамическая плитка». `propose_prices` проверяет это независимо от
модели.

**Эксклюзивы поставщиков** (`src/price_tool/exclusive.py`) — надпись «эксклюзив» в прайсе
запоминается заявкой (`exclusive_claims`), действующая пометка **выводится** из заявок и
решений админа (`exclusive_decisions`), а не хранится. Спор двух поставщиков в окне 2 мес →
вопрос админу, пометка до ответа не показывается. Список эксклюзивов вкладывается в
системный промпт (`build_system_prompt`), потому что у менеджерского агента нет кодов 1С,
чтобы сопоставить свой ответ с БД. Функция справочная — на цены не влияет.

**Авторизация** (`src/bot/auth.py`) — middleware проверяет whitelist в SQLite (`src/storage/users.py`). `ADMIN_TELEGRAM_ID` из конфига всегда проходит; добавляет `is_admin: bool` в данные обработчика.

**Транскрипция голоса** (`src/bot/handlers.py`) — OpenAI Whisper API (`whisper-1`, `language="ru"`). Требует `OPENAI_API_KEY` в `.env`; без ключа возвращает пояснение пользователю.

**Статус действий** — при каждом вызове инструмента `on_tool` редактирует одно сообщение в чате (не создаёт новые). Маппинг `_TOOL_STATUS` в `handlers.py`.

### Конфигурация

`src/config.py` загружает `.env` через `python-dotenv`. Обязательные переменные: `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `MAIL_USER`, `MAIL_PASSWORD`, `ANTHROPIC_API_KEY`. Опциональные: `OPENAI_API_KEY` (голос), `MAIL_HOST`, `MAIL_PORT`, `DB_PATH`.

### Системный промпт

`src/agent/prompts.py` кодирует полную логику агента: валидация запроса (бренд + коллекция обязательны, для плитки — размер) → поиск в почте → сайт → 4 формата ответа (основной / случай А / Б / В). Промпт передаётся с `cache_control: ephemeral`.
