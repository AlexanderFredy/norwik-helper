"""Справка `/help` и меню команд Telegram.

Список команд регистрируется в двух областях: общая (менеджеры) и личная область
администратора. Менеджер не должен видеть в меню команды, которыми ему нельзя
пользоваться, — режим цен и управление доступом доступны только админу.
"""
import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

logger = logging.getLogger(__name__)

COMMON_COMMANDS = [
    BotCommand(command="start", description="Что я умею"),
    BotCommand(command="help", description="Справка по командам"),
]

ADMIN_COMMANDS = COMMON_COMMANDS + [
    # Старый прайсовый поток отключён на время обкатки модели (см. main.py), и его команды
    # убраны из меню: показывать то, что не отвечает, хуже, чем не показывать вовсе.
    BotCommand(command="prices", description="Список прайсов в работе"),
    BotCommand(command="tasks", description="Задачи по прайсу"),
    BotCommand(command="run", description="Выполнить задачу"),
    BotCommand(command="rebuild", description="Собрать задачи прайса заново"),
    BotCommand(command="suppliers", description="Справочник поставщиков"),
    BotCommand(command="signatures", description="Форматы прайсов поставщиков"),
    BotCommand(command="price_files", description="Файлы прайсов на сервере"),
    BotCommand(command="adduser", description="Добавить пользователя"),
    BotCommand(command="removeuser", description="Удалить пользователя"),
    BotCommand(command="listusers", description="Список пользователей"),
]

_MANAGER_HELP = """<b>Поиск товара у поставщиков</b>
Напишите бренд и коллекцию (для плитки — ещё размер) и нужное количество.
Найду, у каких поставщиков товар есть, по каким ценам, и дам их контакты.
Можно надиктовать голосовым сообщением.

<b>Когда меняли наши цены</b>
Спросите «когда последний раз меняли цены на Classen Adventure?» — отвечу датой,
ценами и названием прайса, из которого они взяты. Про товар, коллекцию или марку целиком.
Об изменении цен я сам пришлю короткое сообщение.

<b>Команды</b>
/start — короткое приветствие
/help — эта справка"""

_ADMIN_HELP = """

<b>Прайсы и задачи</b> (обкатка модели)
Пришлите файл прайса — он попадёт в модель, поставщик определится по подписи к файлу.
Устаревший прайс не принимается; чтобы взять его силой, пришлите файл с подписью
/model_force.

<b>ВНИМАНИЕ: выполнение задачи ПИШЕТ В 1С сразу, без второго подтверждения.</b>

/prices — список прайсов: статус, задачи, захват
/tasks &lt;номер&gt; — задачи по прайсу
/run &lt;номер задачи&gt; — выполнить (пишет в 1С)
/edit &lt;номер&gt; &lt;текст&gt; — поправить описание задачи
/status &lt;номер&gt; &lt;статус&gt; — сменить статус задачи
/task_delete &lt;номер&gt; — удалить задачу
/price_status &lt;номер&gt; &lt;статус&gt; — статус прайса ставит админ, не модель
/rebuild &lt;номер&gt; — собрать задачи заново (статусы и правки НЕ переносятся)
/price_delete &lt;номер&gt; — уничтожить прайс
/unlock &lt;номер&gt; — снять свой захват досрочно

Захватывается прайс целиком: пока с ним работает один админ, остальные получают
«прайс занят». Захват снимается через минуту после задачи либо по аренде в 10 минут.

<b>Справочник поставщиков</b>
Поставщик → его форматы прайсов (сигнатуры) → файлы этих прайсов на сервере.
У одного поставщика форматов несколько: он дробит прайс по типам товаров и меняет
вёрстку. Справочник заполняется сам при получении прайса — поставщика можно
назвать в подписи к файлу.

/suppliers — список поставщиков
/supplier_add &lt;имя&gt; — завести вручную
/supplier_rename &lt;номер&gt; &lt;имя&gt; — переименовать
/supplier_delete &lt;номер&gt; — удалить (только если у него нет прайсов)
/supplier_merge &lt;дубль&gt; &lt;основной&gt; — слить дубль в основного: форматы и файлы
переезжают, дубль уничтожается

/signatures [&lt;номер поставщика&gt;] — форматы прайсов
/signature_move &lt;номер&gt; &lt;номер поставщика&gt; — перепривязать формат
/signature_delete &lt;номер&gt; — убрать формат

/price_files [&lt;номер формата&gt;] — файлы прайсов
/price_file_delete &lt;номер&gt; — убрать запись о файле

Номера во всех этих списках <b>сквозные</b>: фильтр прячет лишние строки, но номера
не сдвигает — иначе команда удалила бы не то, что видно.

<b>Доступ к боту</b>
/adduser &lt;id&gt; [имя] — добавить менеджера
/removeuser &lt;id&gt; — убрать
/listusers — список"""

_NO_ONEC = """

⚠️ Интеграция с 1С не настроена (нет ONEC_BASE_URL/ONEC_TOKEN). Для обкатки модели
это не мешает: задачи модели работают отдельно."""


def build_help(is_admin: bool, onec_enabled: bool = True) -> str:
    """Текст `/help`: менеджеру — только поиск, админу — ещё и режим цен."""
    if not is_admin:
        return _MANAGER_HELP
    return _MANAGER_HELP + _ADMIN_HELP + ("" if onec_enabled else _NO_ONEC)


async def setup_bot_commands(bot: Bot, admin_id: int) -> None:
    """Заполнить меню команд. Сбой здесь не должен мешать запуску бота."""
    try:
        await bot.set_my_commands(COMMON_COMMANDS, scope=BotCommandScopeDefault())
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception:
        logger.warning("Не удалось обновить меню команд Telegram", exc_info=True)
