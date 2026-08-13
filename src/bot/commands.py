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
    BotCommand(command="cancel_price", description="Прекратить работу с прайсом"),
    BotCommand(command="mappings", description="Запомненные форматы прайсов"),
    BotCommand(command="mapping_forget", description="Забыть формат прайса"),
    BotCommand(command="exclusives", description="Эксклюзивы поставщиков"),
    BotCommand(command="exclusive_forget", description="Снять пометку эксклюзива"),
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

<b>Обновление цен в 1С</b> (только для вас)
Пришлите прайс <b>файлом</b> — xlsx, xls, csv или pdf, до 20 МБ. В подписи к файлу
можно уточнить поставщика или нужный лист. Я разберу прайс, сверю с номенклатурой
1С, посчитаю розницу и покажу предложение «было → стало» по коллекциям.
<b>Цены запишутся только после нажатия кнопки «Записать в 1С».</b>

/cancel_price — прекратить работу с текущим прайсом
/mappings — какие форматы прайсов я уже разбираю без вопросов
/mapping_forget &lt;номер&gt; — забыть формат, чтобы снова спросил про колонки

<b>Эксклюзивы поставщиков</b>
Если в прайсе написано «эксклюзив», я это запомню и буду добавлять пометку после
названия в отчётах — например «Adventure (эксклюзив: Монарх Логистик)». На цены и
выбор поставщика она не влияет. Если эксклюзив на одно и то же заявят двое, спрошу
вас, за кем он, и до ответа показывать не буду.

/exclusives — что помечено и что ждёт решения
/exclusive_forget &lt;номер&gt; — снять пометку

<b>Доступ к боту</b>
/adduser &lt;id&gt; [имя] — добавить менеджера
/removeuser &lt;id&gt; — убрать
/listusers — список"""

_NO_ONEC = """

⚠️ Интеграция с 1С не настроена (нет ONEC_BASE_URL/ONEC_TOKEN) — приём прайсов
недоступен до перезапуска с этими переменными."""


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
