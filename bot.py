import os
import re
import json
import random
import time
import asyncio
import logging
import tempfile
from openai import OpenAI
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from nudenet import NudeDetector
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions,
    MessageOriginChannel,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Отключаем лишний шум от библиотек
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("apscheduler").setLevel(logging.ERROR)
logging.getLogger("telegram").setLevel(logging.CRITICAL)
logging.getLogger("telegram.ext").setLevel(logging.CRITICAL)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

_nude_detector: NudeDetector | None = None

NSFW_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "MALE_BREAST_EXPOSED",
}

def get_nude_detector() -> NudeDetector | None:
    global _nude_detector
    if _nude_detector is None:
        try:
            _nude_detector = NudeDetector()
            logger.info("NudeDetector успешно загружен.")
        except Exception as e:
            logger.error(f"Не удалось загрузить NudeDetector: {e}")
            return None
    return _nude_detector

WHITELIST_WORDS = set([
    "хорошо",
    "ассалам",
    "assalam",
    "horosho",
    "классно",
    "прекрасно",
])

BANNED_WORDS = set([
    # Русские
    "секс",
    "заебал",
    "массаж",
    "шеш",
    "massage",
    "хуй",
    "блять",
    "гандон",
    "ебать",
    "ебу",
    "выебу",
    "трахать",
    "трахнуть",
    "пизда",
    "сиськи",
    "член",
    "долбаеб",
    "сука",
    "мудак",
    "говно",
    "дерьмо",
    "шлюха",
    "блядь",
    "ублюдок",
    "жопа",
    "залупа",
    "пиздец",
    "ёбаный",
    "еблан",
    "потаскуха",
    # Английские
    "fuck",
    "fucking",
    "fucker",
    "motherfucker",
    "shit",
    "bullshit",
    "bitch",
    "dick",
    "pussy",
    "cock",
    "cunt",
    "asshole",
    "ass",
    "bastard",
    "whore",
    "slut",
    "nigga",
    "nigger",
    "tits",
    "titties",
    "boobs",
    "dumbass",
    "dipshit",
    "jackass",
    "prick",
    "twat",
    # Транслитерация (русский мат латиницей)
    "blyat",
    "blyad",
    "pizda",
    "ebat",
    "yebat",
    "khuy",
    "huy",
    "gandon",
    "mudak",
    "suka",
    "govno",
    "pizdec",
    # Фонетические русские варианты английских слов
    "фак",
    "факин",
    "факинг",
    "факер",
    "шит",
    "ассхол",
    "бастард",
])

CYRILLIC_TO_LATIN = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd',
    'е': 'e', 'ё': 'e', 'ж': 'zh', 'з': 'z', 'и': 'i',
    'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
    'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
    'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch',
    'ш': 'sh', 'щ': 'sh', 'ъ': '', 'ы': 'y', 'ь': '',
    'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def _transliterate(text: str) -> str:
    result = ''
    for char in text:
        result += CYRILLIC_TO_LATIN.get(char, char)
    return result


def _normalize_for_filter(text: str) -> str:
    text = text.lower()
    normalized_words = []
    for word in text.split():
        letters_only = re.sub(r'[^a-zа-яё]', '', word)
        transliterated = _transliterate(letters_only)
        deduped = re.sub(r'(.)\1{2,}', r'\1\1', transliterated)
        normalized_words.append(deduped)
    return ' '.join(normalized_words)


URL_PATTERN = re.compile(
    r"(https?://|www\.)\S+|"
    r"t\.me/\S+|"
    r"\S+\.(com|net|org|ru|io|xyz|info|biz|me|tv|co)\b",
    re.IGNORECASE,
)

DELETE_LINKS = True
MAX_WARNINGS = 3
SPAM_MAX_MESSAGES = 5
SPAM_WINDOW_SECONDS = 10
SPAM_MUTE_MINUTES = 5

user_warnings: dict[str, int] = {}
user_message_times: dict[str, list] = {}
user_warnings_ts: dict[str, float] = {}
group_members: dict[str, dict] = {}  # chat_id -> {user_id: {"first_name": str, "id": int}}
MEMBERS_FILE = "group_members.json"

def save_members():
    try:
        with open(MEMBERS_FILE, "w", encoding="utf-8") as f:
            # Преобразуем объекты User в словари для JSON
            data = {}
            for chat_id, users in group_members.items():
                data[str(chat_id)] = {str(uid): {"id": u.id, "first_name": u.first_name} for uid, u in users.items()}
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка при сохранении участников: {e}")

def load_members():
    global group_members
    if os.path.exists(MEMBERS_FILE):
        try:
            with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for chat_id_str, users in data.items():
                    group_members[int(chat_id_str)] = {
                        int(uid_str): type('User', (), u) for uid_str, u in users.items()
                    }
            logger.info(f"Загружено участников из базы: {sum(len(u) for u in group_members.values())}")
        except Exception as e:
            logger.error(f"Ошибка при загрузке участников: {e}")

_handled_media_groups: set[str] = set()
_welcomed_users: set[str] = set()
_pending_welcome_msgs: dict[str, int] = {}  # key -> welcome message_id

LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID")

_admin_ids: set[int] = set()
_cached_chat_admins: set[int] = set()
_admin_cache_time: float = 0.0
_ADMIN_CACHE_TTL = 300  # обновляем список каждые 5 минут


def _load_admin_ids() -> None:
    global _admin_ids
    raw = os.environ.get("ADMIN_IDS", "")
    try:
        _admin_ids = {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError:
        _admin_ids = set()


async def _refresh_chat_admins(bot, chat_id: int) -> None:
    global _cached_chat_admins, _admin_cache_time
    now = time.time()
    if now - _admin_cache_time < _ADMIN_CACHE_TTL:
        return
    try:
        admins = await bot.get_chat_administrators(chat_id)
        _cached_chat_admins = {a.user.id for a in admins}
        _admin_cache_time = now
        logger.debug(f"Обновлён кэш админов: {len(_cached_chat_admins)} чел.")
    except Exception as e:
        logger.warning(f"Не удалось обновить список админов: {e}")


async def _cleanup_old_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = time.time()
    stale_warn = [k for k, ts in user_warnings_ts.items() if now - ts > 86400 * 7]
    for k in stale_warn:
        user_warnings.pop(k, None)
        user_warnings_ts.pop(k, None)
    stale_spam = [k for k, times in user_message_times.items() if not times or now - times[-1] > 3600]
    for k in stale_spam:
        user_message_times.pop(k, None)
    # Медиа-группы и приветствия хранятся недолго — чистим всё раз в час
    _handled_media_groups.clear()
    _welcomed_users.clear()
    _pending_welcome_msgs.clear()
    if stale_warn or stale_spam:
        logger.info(f"Очистка памяти: удалено {len(stale_warn)} предупреждений, {len(stale_spam)} спам-записей")


async def send_log(bot, text: str, source_chat_id: int = None) -> None:
    if not LOG_CHANNEL_ID:
        return
    if source_chat_id and str(source_chat_id) == str(LOG_CHANNEL_ID):
        return
    try:
        await bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, parse_mode=ParseMode.HTML)
    except:
        pass


FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)

NO_SEND_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
)


def is_admin(user_id: int) -> bool:
    return user_id in _admin_ids or user_id in _cached_chat_admins


async def delete_welcome_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    message_id = context.job.data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        f"Привет, {user.mention_html()}! 👋\n\n"
        "Вот что я умею:\n"
        "/start — Показать это сообщение\n"
        "/help — Получить помощь\n"
        "/menu — Открыть интерактивное меню\n"
        "/echo <текст> — Повторить твой текст\n\n"
        "<b>Команды администратора:</b>\n"
        "/addword <слово> — Добавить слово в список запрещённых\n"
        "/removeword <слово> — Убрать слово из списка запрещённых\n"
        "/listwords — Показать все запрещённые слова\n"
        "/togglelinks — Включить/выключить удаление ссылок\n"
        "/resetwarnings <id> — Сбросить предупреждения пользователя\n"
        "/warnings <id> — Проверить предупреждения пользователя\n"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Доступные команды:\n\n"
        "/start — Запустить бота\n"
        "/help — Показать это сообщение\n"
        "/menu — Открыть интерактивное меню\n"
        "/echo <текст> — Повторить твой текст\n\n"
        "Команды администратора:\n"
        "/addword <слово> — Запретить слово\n"
        "/removeword <слово> — Разрешить слово\n"
        "/listwords — Список запрещённых слов\n"
        "/togglelinks — Включить/выключить удаление ссылок\n"
        "/resetwarnings <id> — Сбросить предупреждения пользователя\n"
        "/warnings <id> — Проверить предупреждения пользователя\n"
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton("Вариант А", callback_data="option_a"),
            InlineKeyboardButton("Вариант Б", callback_data="option_b"),
        ],
        [InlineKeyboardButton("О боте", callback_data="about")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите вариант:", reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "option_a":
        await query.edit_message_text("Вы выбрали Вариант А!")
    elif query.data == "option_b":
        await query.edit_message_text("Вы выбрали Вариант Б!")
    elif query.data == "about":
        await query.edit_message_text(
            "Это шаблон Telegram-бота, созданный на python-telegram-bot."
        )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        text = " ".join(context.args)
        await update.message.reply_text(f"Вы сказали: {text}")
    else:
        await update.message.reply_text("Использование: /echo <текст>")


async def add_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /addword <слово1>, <слово2>, ...")
        return
    raw = " ".join(context.args)
    words = [w.strip().lower() for w in re.split(r"[,\s]+", raw) if w.strip()]
    for word in words:
        BANNED_WORDS.add(word)
    logger.info(f"Добавлены запрещённые слова: {words} пользователем {update.effective_user.id}")
    word_list = ", ".join(f"<code>{w}</code>" for w in words)
    await update.message.reply_html(f"Запрещено слов: {len(words)}\n{word_list}")


async def remove_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /removeword <слово1>, <слово2>, ...")
        return
    raw = " ".join(context.args)
    words = [w.strip().lower() for w in re.split(r"[,\s]+", raw) if w.strip()]
    removed = []
    not_found = []
    for word in words:
        if word in BANNED_WORDS:
            BANNED_WORDS.discard(word)
            removed.append(word)
        else:
            not_found.append(word)
    logger.info(f"Удалены запрещённые слова: {removed} пользователем {update.effective_user.id}")
    response = ""
    if removed:
        response += "Разрешено: " + ", ".join(f"<code>{w}</code>" for w in removed) + "\n"
    if not_found:
        response += "Не найдено: " + ", ".join(f"<code>{w}</code>" for w in not_found)
    await update.message.reply_html(response.strip())


async def list_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if BANNED_WORDS:
        word_list = "\n".join(f"• <code>{w}</code>" for w in sorted(BANNED_WORDS))
        await update.message.reply_html(f"<b>Запрещённые слова:</b>\n{word_list}")
    else:
        await update.message.reply_text("Список запрещённых слов пуст.")


async def toggle_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global DELETE_LINKS
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    DELETE_LINKS = not DELETE_LINKS
    state = "включено" if DELETE_LINKS else "выключено"
    logger.info(f"Удаление ссылок {state} пользователем {update.effective_user.id}")
    await update.message.reply_text(f"Удаление ссылок теперь {state}.")


async def reset_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /resetwarnings <id_пользователя>")
        return
    try:
        target_id = int(context.args[0])
        chat_id = update.effective_chat.id
        key = f"{chat_id}:{target_id}"
        if key in user_warnings:
            del user_warnings[key]
            await update.message.reply_text(f"Предупреждения пользователя {target_id} сброшены.")
        else:
            await update.message.reply_text(f"Предупреждения для пользователя {target_id} не найдены.")
    except ValueError:
        await update.message.reply_text("Укажите корректный числовой ID пользователя.")


async def check_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /warnings <id_пользователя>")
        return
    try:
        target_id = int(context.args[0])
        chat_id = update.effective_chat.id
        key = f"{chat_id}:{target_id}"
        count = user_warnings.get(key, 0)
        await update.message.reply_html(
            f"Пользователь <code>{target_id}</code>: "
            f"<b>{count}/{MAX_WARNINGS}</b> предупреждений."
        )
    except ValueError:
        await update.message.reply_text("Укажите корректный числовой ID пользователя.")


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /ban <id_пользователя> [причина]")
        return
    try:
        target_id = int(context.args[0])
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else "не указана"
        chat_id = update.effective_chat.id
        await context.bot.ban_chat_member(chat_id, target_id)
        await update.message.reply_html(
            f"🚫 Пользователь <code>{target_id}</code> заблокирован.\n"
            f"Причина: {reason}"
        )
        logger.info(f"Пользователь {target_id} заблокирован администратором {update.effective_user.id}")
        await send_log(
            context.bot,
            f"🚫 <b>Пользователь заблокирован</b>\n"
            f"ID: <code>{target_id}</code>\n"
            f"Причина: {reason}\n"
            f"Администратор: {update.effective_user.mention_html()}\n"
            f"Чат: <code>{chat_id}</code>",
            source_chat_id=update.effective_chat.id,
        )
    except ValueError:
        await update.message.reply_text("Укажите корректный числовой ID пользователя.")
    except BadRequest as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /unban <id_пользователя>")
        return
    try:
        target_id = int(context.args[0])
        chat_id = update.effective_chat.id
        await context.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        await update.message.reply_html(f"✅ Пользователь <code>{target_id}</code> разблокирован.")
        logger.info(f"Пользователь {target_id} разблокирован администратором {update.effective_user.id}")
        await send_log(
            context.bot,
            f"✅ <b>Пользователь разблокирован</b>\n"
            f"ID: <code>{target_id}</code>\n"
            f"Администратор: {update.effective_user.mention_html()}\n"
            f"Чат: <code>{chat_id}</code>",
            source_chat_id=update.effective_chat.id,
        )
    except ValueError:
        await update.message.reply_text("Укажите корректный числовой ID пользователя.")
    except BadRequest as e:
        await update.message.reply_text(f"Ошибка: {e}")


def parse_duration(arg: str) -> int | None:
    units = {"m": 60, "h": 3600, "d": 86400}
    if arg[-1] in units:
        try:
            return int(arg[:-1]) * units[arg[-1]]
        except ValueError:
            return None
    return None


async def unmute_user_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    user_id = context.job.data["user_id"]
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, FULL_PERMISSIONS)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔊 Пользователь <code>{user_id}</code> снова может писать в чате.",
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        logger.warning(f"Не удалось снять мут с {user_id}: {e}")


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /mute <id> <время>\n"
            "Примеры: /mute 123456789 30m | 2h | 1d"
        )
        return
    try:
        target_id = int(context.args[0])
        seconds = parse_duration(context.args[1])
        if not seconds:
            await update.message.reply_text("Неверный формат времени. Используйте: 30m, 2h, 1d")
            return
        chat_id = update.effective_chat.id
        await context.bot.restrict_chat_member(chat_id, target_id, NO_SEND_PERMISSIONS)
        context.job_queue.run_once(
            unmute_user_job,
            when=seconds,
            data={"chat_id": chat_id, "user_id": target_id},
            name=f"unmute_{chat_id}:{target_id}",
        )
        duration_text = context.args[1]
        await update.message.reply_html(
            f"🔇 Пользователь <code>{target_id}</code> замьючен на <b>{duration_text}</b>."
        )
        logger.info(f"Пользователь {target_id} замьючен на {duration_text} администратором {update.effective_user.id}")
        await send_log(
            context.bot,
            f"🔇 <b>Мут</b>\n"
            f"ID: <code>{target_id}</code>\n"
            f"Длительность: {duration_text}\n"
            f"Администратор: {update.effective_user.mention_html()}\n"
            f"Чат: <code>{chat_id}</code>",
            source_chat_id=update.effective_chat.id,
        )
    except ValueError:
        await update.message.reply_text("Укажите корректный числовой ID пользователя.")
    except BadRequest as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для использования этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /unmute <id_пользователя>")
        return
    try:
        target_id = int(context.args[0])
        chat_id = update.effective_chat.id
        await context.bot.restrict_chat_member(chat_id, target_id, FULL_PERMISSIONS)
        await update.message.reply_html(f"🔊 Пользователь <code>{target_id}</code> размьючен.")
        logger.info(f"Пользователь {target_id} размьючен администратором {update.effective_user.id}")
        await send_log(
            context.bot,
            f"🔊 <b>Размьючен</b>\n"
            f"ID: <code>{target_id}</code>\n"
            f"Администратор: {update.effective_user.mention_html()}\n"
            f"Чат: <code>{chat_id}</code>",
            source_chat_id=update.effective_chat.id,
        )
    except ValueError:
        await update.message.reply_text("Укажите корректный числовой ID пользователя.")
    except BadRequest as e:
        await update.message.reply_text(f"Ошибка: {e}")


WELCOME_IMAGE = Path(__file__).parent / "welcome.jpeg"

WELCOME_TEXT = (
    "Привет, {name}! 👋\n\n"
    "Добро пожаловать в наш чат!\n\n"
    "Тусовки, вечеринки, яхты и лучшие знакомства в городе!\n\n"
    "Здесь ты можешь:\n"
    "✨ •Найти компанию для тусовки\n"
    "💬 •Общаться с реальными людьми\n"
    "🚀 •Попасть в эпицентр ночной жизни Дубая\n\n"
    "📌 Правила группы:\n"
    "• Уважайте друг друга\n"
    "• Без спама и рекламы\n"
    "• Запрещён мат и оскорбления"
)


async def delete_left_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.delete()
    except BadRequest:
        pass


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.delete()
    except BadRequest:
        pass

    chat_id = update.effective_chat.id

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        if chat_id not in group_members:
            group_members[chat_id] = {}
        group_members[chat_id][member.id] = member
        save_members()

        key = f"{chat_id}:{member.id}"

        if key in _welcomed_users:
            continue
        _welcomed_users.add(key)

        try:
            caption = WELCOME_TEXT.format(name=member.mention_html())

            if WELCOME_IMAGE.exists():
                with open(WELCOME_IMAGE, "rb") as photo:
                    msg = await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
            else:
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                )

            _pending_welcome_msgs[key] = msg.message_id
            context.job_queue.run_once(
                delete_welcome_message,
                when=86400,
                data={"chat_id": chat_id, "message_id": msg.message_id},
                name=f"del_welcome_{key}",
            )
            logger.info(f"Новый участник {member.id} (@{member.username}) вошёл в чат {chat_id}")
        except Exception as e:
            logger.error(f"Ошибка при отправке приветствия для {member.id}: {e}")

        await asyncio.sleep(0.5)


async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle new members joining via invite link (chat_member update)."""
    if not update.chat_member:
        return

    old_status = update.chat_member.old_chat_member.status
    new_status = update.chat_member.new_chat_member.status

    # Only fire for users transitioning into the group
    joined = old_status in ("left", "kicked") and new_status in ("member", "administrator")
    if not joined:
        return

    member = update.chat_member.new_chat_member.user
    if member.is_bot:
        return

    chat_id = update.chat_member.chat.id

    if chat_id not in group_members:
        group_members[chat_id] = {}
    group_members[chat_id][member.id] = member
    save_members()

    key = f"{chat_id}:{member.id}"

    if key in _welcomed_users:
        return
    _welcomed_users.add(key)

    caption = WELCOME_TEXT.format(name=member.mention_html())

    if WELCOME_IMAGE.exists():
        with open(WELCOME_IMAGE, "rb") as photo:
            msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
    else:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
        )

    _pending_welcome_msgs[key] = msg.message_id
    context.job_queue.run_once(
        delete_welcome_message,
        when=86400,
        data={"chat_id": chat_id, "message_id": msg.message_id},
        name=f"del_welcome_{key}",
    )
    logger.info(f"Новый участник {member.id} (@{member.username}) вошёл через invite в чат {chat_id}")


async def _handle_channel_forward(message, user, context) -> bool:
    """Проверяет форвард из канала, удаляет и предупреждает. Возвращает True если обработано."""
    if not message.forward_origin or is_admin(user.id):
        return False
    if isinstance(message.forward_origin, MessageOriginChannel):
        # Если это часть медиа-группы (альбома) — удаляем тихо, предупреждение только одно
        if message.media_group_id:
            if message.media_group_id in _handled_media_groups:
                try:
                    await message.delete()
                except BadRequest:
                    pass
                return True
            _handled_media_groups.add(message.media_group_id)
        try:
            await message.delete()
            warn = await message.chat.send_message(
                f"⛔ {user.mention_html()}, пересылка сообщений из каналов запрещена в этом чате.",
                parse_mode=ParseMode.HTML,
            )
            context.job_queue.run_once(
                delete_welcome_message,
                when=30,
                data={"chat_id": message.chat.id, "message_id": warn.message_id},
                name=f"del_fwd_warn_{message.chat.id}_{user.id}_{message.message_id}",
            )
            logger.info(f"Удалён форвард из канала от {user.id} в чате {message.chat.id}")
        except BadRequest:
            pass
        return True
    return False


async def filter_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    user = message.from_user
    if not user:
        return
    if await _handle_channel_forward(message, user, context):
        return
    await _refresh_chat_admins(context.bot, message.chat.id)
    if is_admin(user.id):
        return
    if await _has_non_admin_mention(message, context.bot, message.chat.id):
        deleted = False
        try:
            await message.delete()
            deleted = True
        except BadRequest:
            pass
        warn = await message.chat.send_message(
            f"🔇 {user.mention_html()}, упоминания других участников запрещены."
            + (" Сообщение удалено." if deleted else ""),
            parse_mode=ParseMode.HTML,
        )
        context.job_queue.run_once(
            delete_welcome_message,
            when=30,
            data={"chat_id": message.chat.id, "message_id": warn.message_id},
            name=f"del_mention_warn_{message.chat.id}_{user.id}_{message.message_id}",
        )
        return
    await _delete_for_link(message, user, context, message.caption or "")


async def _delete_for_link(message, user, context, caption: str) -> bool:
    """Удаляет сообщение с подписью-ссылкой. Возвращает True если удалено."""
    if not DELETE_LINKS or not caption:
        return False
    if not URL_PATTERN.search(caption):
        return False
    try:
        await message.delete()
        warn = await message.chat.send_message(
            f"🔗 {user.mention_html()}, ссылки запрещены в этом чате. Сообщение удалено.",
            parse_mode=ParseMode.HTML,
        )
        context.job_queue.run_once(
            delete_welcome_message,
            when=30,
            data={"chat_id": message.chat.id, "message_id": warn.message_id},
            name=f"del_link_warn_{message.chat.id}_{user.id}_{message.message_id}",
        )
        logger.info(f"Удалена ссылка в подписи от {user.id} в чате {message.chat.id}")
    except BadRequest:
        pass
    return True


async def filter_nsfw_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.photo:
        return

    user = message.from_user
    if not user:
        return

    if await _handle_channel_forward(message, user, context):
        return

    await _refresh_chat_admins(context.bot, message.chat.id)
    if is_admin(user.id):
        return

    if await _delete_for_link(message, user, context, message.caption or ""):
        return

    if await _has_non_admin_mention(message, context.bot, message.chat.id):
        deleted = False
        try:
            await message.delete()
            deleted = True
        except BadRequest:
            pass
        warn = await message.chat.send_message(
            f"🔇 {user.mention_html()}, упоминания других участников запрещены."
            + (" Сообщение удалено." if deleted else ""),
            parse_mode=ParseMode.HTML,
        )
        context.job_queue.run_once(
            delete_welcome_message,
            when=30,
            data={"chat_id": message.chat.id, "message_id": warn.message_id},
            name=f"del_mention_warn_{message.chat.id}_{user.id}_{message.message_id}",
        )
        return

    photo = message.photo[-1]

    try:
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        is_nsfw = False
        try:
            detector = get_nude_detector()
            if detector is None:
                logger.warning("NudeDetector недоступен, пропуск проверки фото.")
            else:
                detections = detector.detect(tmp_path)
                if detections:
                    for d in detections:
                        logger.info(f"NSFW детекция: {d['class']} score={d['score']:.2f}")
                else:
                    logger.info(f"NSFW: объектов не найдено на фото от {user.id}")
                is_nsfw = any(
                    d["class"] in NSFW_LABELS and d["score"] > 0.35
                    for d in detections
                )
                logger.info(f"NSFW итог фото от {user.id}: {'18+' if is_nsfw else 'чисто'}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        if is_nsfw:
            deleted = False
            try:
                await message.delete()
                deleted = True
            except BadRequest:
                logger.warning(f"Не удалось удалить 18+ фото от {user.id}")

            warn = await message.chat.send_message(
                f"🔞 {user.mention_html()}, фотографии с контентом 18+ запрещены в этом чате."
                + (" Сообщение удалено." if deleted else ""),
                parse_mode=ParseMode.HTML,
            )
            context.job_queue.run_once(
                delete_welcome_message,
                when=30,
                data={"chat_id": message.chat.id, "message_id": warn.message_id},
                name=f"del_nsfw_warn_{message.chat.id}_{user.id}_{message.message_id}",
            )
            logger.info(f"Удалено 18+ фото от {user.id} в чате {message.chat.id}")
            await send_log(
                context.bot,
                f"🔞 <b>Удалено 18+ фото</b>\n"
                f"Пользователь: {user.mention_html()} (<code>{user.id}</code>)\n"
                f"Чат: <code>{message.chat.id}</code>",
                source_chat_id=message.chat.id,
            )

    except Exception as e:
        logger.error(f"Ошибка при проверке фото на 18+: {e}", exc_info=False)


async def _has_non_admin_mention(message, bot, chat_id: int) -> bool:
    """Возвращает True если сообщение содержит упоминание НЕ-админа."""
    entities = message.entities or message.caption_entities or []
    mention_entities = [e for e in entities if e.type in ("mention", "text_mention")]
    if not mention_entities:
        return False

    try:
        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = {a.user.id for a in admins}
        admin_usernames = {a.user.username.lower() for a in admins if a.user.username}
    except Exception:
        admin_ids = _admin_ids.copy()
        admin_usernames = set()

    text = message.text or message.caption or ""
    for entity in mention_entities:
        if entity.type == "text_mention":
            if entity.user.id not in admin_ids:
                return True
        elif entity.type == "mention":
            username = text[entity.offset + 1: entity.offset + entity.length].lower()
            if username not in admin_usernames:
                return True
    return False


async def filter_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    user = message.from_user
    if not user:
        return

    chat_id = message.chat.id
    key = f"{chat_id}:{user.id}"

    if not user.is_bot:
        if chat_id not in group_members:
            group_members[chat_id] = {}
        if user.id not in group_members[chat_id]:
            group_members[chat_id][user.id] = user
            save_members()

    if await _handle_channel_forward(message, user, context):
        return

    # Удаляем приветствие как только новый участник написал первое сообщение
    if key in _pending_welcome_msgs:
        welcome_msg_id = _pending_welcome_msgs.pop(key)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=welcome_msg_id)
        except BadRequest:
            pass

    await _refresh_chat_admins(context.bot, chat_id)

    if message.text and "дядя" in message.text.lower():
        text_lower = message.text.lower()

        # Если есть OpenAI API Key, используем ИИ для ответов
        if openai_client:
            try:
                # Специальный случай для вопроса "кто ты"
                if "кто ты" in text_lower or "ты кто" in text_lower:
                    await message.reply_text("Если ты от Бога, значит, я твой брат. 🙏✨")
                    return

                system_prompt = (
                    "Ты — 'Дядя', авторитетный и уважаемый человек в чате 'Dubai Night'. "
                    "Твой образ: солидный мужчина из Дубая, который знает жизнь, ценит уважение и говорит только по делу. "
                    "ПРАВИЛА ТВОЕГО ПОВЕДЕНИЯ:\n"
                    "1. ОТВЕЧАЙ КРАТКО. Максимум 1-2 предложения. Никакой воды и длинных рассуждений.\n"
                    "2. СТИЛЬ: 'С понятием', по-мужски, уверенно. Используй сленг Дубая (яхты, тусовки, движ), но оставайся достойным.\n"
                    "3. НА ХАМСТВО И ПРИКАЗЫ: Отвечай хлестко, ставь на место. Ты не слуга, ты — старший в этом чате. На слова 'спи', 'иди', 'лижи' и т.д. отвечай дерзко, но без мата.\n"
                    "4. КТО ТЫ: На любой вопрос о твоей личности отвечай ТОЛЬКО: 'Если ты от Бога, значит, я твой брат. 🙏✨'.\n"
                    "5. ЭМОДЗИ: Используй редко и только по делу (🤝, 🥃, 🛥️, 🙏, 😎).\n"
                    "6. Если сообщение не содержит вопроса или смысла, отвечай что-то вроде: 'Говори по делу, не мути воду. 🤝' или 'Дядя всё видит. Веди себя достойно. 😎'."
                )

                response = openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message.text}
                    ],
                    max_tokens=150,
                    temperature=0.7
                )
                ai_reply = response.choices[0].message.content
                await message.reply_text(ai_reply)
                return
            except Exception as e:
                logger.error(f"Ошибка OpenAI: {e}")
                # Если ИИ упал, используем старую логику как запасной вариант

        # Запасная логика (если нет API ключа или ошибка)
        action_keywords = ["иди", "спи", "замолчи", "заткнись", "уходи", "вали", "убирайся", "помолчи", "отдыхай", "работай", "ложись", "закрой", "рот", "ползай", "лижи"]
        is_action = any(kw in text_lower for kw in action_keywords)

        if "кто ты" in text_lower or "ты кто" in text_lower:
            await message.reply_text("Если ты от Бога, значит, я твой брат. 🙏✨")
            return

        if is_action:
            witty_responses = [
                "Дядя сам решает, что ему делать. 😌",
                "Своим ртом командуй, командир. 🤐",
                "Дядя на отдыхе, не мешай. 🥂",
                "Если ты от Бога, ты бы такого не сказал. 🙏",
            ]
            await message.reply_text(random.choice(witty_responses))
            return

        # Выбор случайного участника (кто сегодня...)
        if "кто" in text_lower:
            # Берем всех накопленных участников из базы для этого чата
            members_dict = group_members.get(chat_id, {})
            
            # Исключаем самого отправителя и ботов
            potential_candidates = [u for uid, u in members_dict.items() if uid != user.id]
            
            if not potential_candidates:
                # Если база пуста (бывает при первом запуске), пробуем взять админов
                try:
                    chat_members = await context.bot.get_chat_administrators(chat_id)
                    potential_candidates = [m.user for m in chat_members if not m.user.is_bot and m.user.id != user.id]
                except Exception:
                    potential_candidates = []
            
            if potential_candidates:
                chosen = random.choice(potential_candidates)
                mention = f'<a href="tg://user?id={chosen.id}">{chosen.first_name}</a>'
                
                # ИИ может сам решить, как объявить победителя, если OpenAI подключен
                if openai_client:
                    try:
                        prompt = f"В чате спросили: '{message.text}'. Дядя выбрал участника {chosen.first_name}. Объяви это максимально коротко и 'с понятием'. Например: 'Дядя решил — это {chosen.first_name}. Без вариантов. 🤝'"
                        response = openai_client.chat.completions.create(
                            model="gpt-4o",
                            messages=[{"role": "system", "content": "Ты краткий и авторитетный Дядя."}, {"role": "user", "content": prompt}],
                            max_tokens=50
                        )
                        ai_reply = response.choices[0].message.content.replace(chosen.first_name, mention)
                        await message.reply_html(ai_reply)
                        return
                    except: pass
                
                await message.reply_html(f"🎯 Дядя присмотрелся... Сегодня это {mention}! 🤝")
                return

        await message.reply_text("Дядя тебя слышит. Говори по делу. 🤝")
        return

    if is_admin(user.id):
        return

    now = time.time()
    times = user_message_times.get(key, [])
    times = [t for t in times if now - t < SPAM_WINDOW_SECONDS]
    times.append(now)
    user_message_times[key] = times

    if len(times) > SPAM_MAX_MESSAGES:
        until = datetime.now(timezone.utc) + timedelta(minutes=SPAM_MUTE_MINUTES)
        try:
            await context.bot.restrict_chat_member(chat_id, user.id, NO_SEND_PERMISSIONS, until_date=until)
            warn = await message.chat.send_message(
                f"🚫 {user.mention_html()} заблокирован за спам на <b>{SPAM_MUTE_MINUTES} минут</b>.",
                parse_mode=ParseMode.HTML,
            )
            context.job_queue.run_once(
                delete_welcome_message,
                when=30,
                data={"chat_id": chat_id, "message_id": warn.message_id},
                name=f"del_spam_warn_{key}_{int(now)}",
            )
            user_message_times[key] = []
            logger.info(f"Спам-мут: {user.id} в чате {chat_id} на {SPAM_MUTE_MINUTES} мин.")
            await send_log(
                context.bot,
                f"🚫 <b>Антиспам — мут</b>\n"
                f"Пользователь: {user.mention_html()} (<code>{user.id}</code>)\n"
                f"Причина: {SPAM_MAX_MESSAGES}+ сообщений за {SPAM_WINDOW_SECONDS} секунд\n"
                f"Мут: <b>{SPAM_MUTE_MINUTES} минут</b>\n"
                f"Чат: <code>{chat_id}</code>",
                source_chat_id=chat_id,
            )
        except BadRequest:
            pass
        return

    # Проверка упоминаний
    if await _has_non_admin_mention(message, context.bot, chat_id):
        deleted = False
        try:
            await message.delete()
            deleted = True
        except BadRequest:
            pass
        warn = await message.chat.send_message(
            f"🔇 {user.mention_html()}, упоминания других участников запрещены."
            + (" Сообщение удалено." if deleted else ""),
            parse_mode=ParseMode.HTML,
        )
        context.job_queue.run_once(
            delete_welcome_message,
            when=30,
            data={"chat_id": chat_id, "message_id": warn.message_id},
            name=f"del_mention_warn_{key}_{int(now)}",
        )
        logger.info(f"Удалено упоминание от {user.id} в чате {chat_id}")
        return

    text = message.text.lower()
    reason = None

    normalized_text = _normalize_for_filter(text)
    
    # Сначала проверяем, нет ли в тексте разрешенных слов из белого списка
    # Если слово из белого списка найдено, мы временно "маскируем" его в тексте для проверки на мат
    temp_text = text
    temp_normalized = normalized_text
    for white_word in WHITELIST_WORDS:
        temp_text = temp_text.replace(white_word.lower(), " [SAFE] ")
        temp_normalized = temp_normalized.replace(white_word.lower(), " [SAFE] ")

    # Разбиваем на слова и убираем знаки препинания для точного сравнения
    text_words = {re.sub(r'[^a-zа-яё]', '', w) for w in temp_text.split() if w}
    norm_words = set(temp_normalized.split())

    for word in BANNED_WORDS:
        if word in text_words or word in norm_words:
            reason = f"запрещённое слово: <code>{word}</code>"
            logger.info(f"Найдено запрещённое слово '{word}' от {user.id}")
            break

    if reason is None and DELETE_LINKS and URL_PATTERN.search(message.text):
        reason = "ссылки запрещены в этом чате"

    if reason:
        user_warnings[key] = user_warnings.get(key, 0) + 1
        user_warnings_ts[key] = time.time()
        count = user_warnings[key]
        remaining = MAX_WARNINGS - count

        # Удаляем сообщение отдельно — если нет прав, предупреждение всё равно отправится
        deleted = False
        try:
            await message.delete()
            deleted = True
            logger.info(
                f"Удалено сообщение от {user.id} (@{user.username}) — {reason} — предупреждение {count}/{MAX_WARNINGS}"
            )
        except BadRequest:
            logger.warning(f"Не удалось удалить сообщение от {user.id} — нет прав на удаление")

        if remaining > 0:
            warn_text = (
                f"⚠️ {user.mention_html()}, {'ваше сообщение удалено' if deleted else 'нарушение правил'} ({reason}).\n"
                f"Предупреждение <b>{count}/{MAX_WARNINGS}</b> — осталось {remaining} предупреждений."
            )
        else:
            warn_text = (
                f"🚫 {user.mention_html()}, {'ваше сообщение удалено' if deleted else 'нарушение правил'} ({reason}).\n"
                f"Вы получили максимальное количество предупреждений: <b>{MAX_WARNINGS}/{MAX_WARNINGS}</b>. "
                f"Пожалуйста, соблюдайте правила чата."
            )

        try:
            await message.chat.send_message(warn_text, parse_mode=ParseMode.HTML)
            await send_log(
                context.bot,
                f"⚠️ <b>Нарушение правил</b>\n"
                f"Пользователь: {user.mention_html()} (<code>{user.id}</code>)\n"
                f"Причина: {reason}\n"
                f"Предупреждение: <b>{count}/{MAX_WARNINGS}</b>\n"
                f"Чат: <code>{chat_id}</code>",
                source_chat_id=chat_id,
            )
        except BadRequest:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления:", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("Переменная окружения TELEGRAM_BOT_TOKEN не задана!")

    # Небольшая пауза для корректного перезапуска на хостингах
    time.sleep(5)

    _load_admin_ids()
    load_members()

    app = Application.builder().token(BOT_TOKEN).build()

    app.job_queue.run_repeating(_cleanup_old_data, interval=3600, first=3600)

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("echo", echo))
    app.add_handler(CommandHandler("addword", add_word))
    app.add_handler(CommandHandler("removeword", remove_word))
    app.add_handler(CommandHandler("listwords", list_words))
    app.add_handler(CommandHandler("togglelinks", toggle_links))
    app.add_handler(CommandHandler("resetwarnings", reset_warnings))
    app.add_handler(CommandHandler("warnings", check_warnings))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Системные события
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, delete_left_message))
    # Приветствие через invite-ссылку (chat_member update)
    app.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Фильтры сообщений — всё в одной группе, без дублирования
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, filter_message))
    app.add_handler(MessageHandler(filters.PHOTO, filter_nsfw_photo))
    # Форварды нетекстовых и нефото сообщений (стикеры, видео и т.д.) из каналов
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & ~filters.TEXT & ~filters.PHOTO,
        filter_forward,
    ))

    app.add_error_handler(error_handler)

    logger.info("Бот запущен...")
    try:
        app.run_polling(
            allowed_updates=["message", "callback_query", "chat_member"],
            drop_pending_updates=True,
            close_loop=False
        )
    except Exception as e:
        if "Conflict" in str(e):
            logger.warning("Замечена вторая копия бота. Ожидаю завершения старой сессии...")
        else:
            logger.error(f"Критическая ошибка при работе: {e}")


if __name__ == "__main__":
    main()
