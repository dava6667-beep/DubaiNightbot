import os
import re
import random
import time
import asyncio
import logging
import tempfile
import json
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
    ApplicationHandlerStop,
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
    "bastard",
    "whore",
    "slut",
    "nigga",
    "nigger",
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
group_members: dict[str, dict] = {}
_handled_media_groups: set[str] = set()
_welcomed_users: set[str] = set()
_pending_welcome_msgs: dict[str, int] = {}  # key -> welcome message_id
_seen_users: dict[int, str] = {}  # user_id -> full_name

DYAD_LOVE_RESPONSES = [
    "Я тоже люблю {name}! ❤️",
    "Передай {name} — взаимно! 🥰",
    "{name} золотой человек, и я тоже так считаю! 💕",
    "Ааа, {name}! Взаимно с удовольствием! 😊",
    "{name} тоже в моём сердце! 🫀",
    "Слышишь, {name}? Я тоже тебя люблю! 💙",
]

DYAD_WHO_IS_RESPONSES = [
    "{name} — это звезда нашего чата! ⭐",
    "{name} — легенда, о которой ещё будут говорить! 🔥",
    "{name}? Серьёзный человек, уважаю! 💪",
    "{name} — самый интересный участник здесь! 😎",
    "{name} — загадка, но хорошая! 🤔✨",
    "{name} — человек с характером! Знаю-знаю! 👀",
    "{name} это {name}. Больше добавить нечего! 🤷",
]

DYAD_THIEF_RESPONSES = [
    "🔍 Расследование закончено! Вор — {mention}! Поймал с поличным!",
    "👮 Дядя раскрыл дело! Главный подозреваемый — {mention}!",
    "⚖️ По всем уликам виновен {mention}! Сознавайся!",
    "🕵️ Детектив дядя объявляет: {mention} взял последнее печенье из холодильника!",
    "🚔 Стоп! {mention}, куда идёшь? Все видели!",
    "📸 Камера зафиксировала — это был {mention}! Улики неопровержимы!",
]

# Рейтинг благодарностей: {chat_id: {user_id: {"count": N, "name": "..."}}}
thanks_count: dict[int, dict[int, dict]] = {}
THANKS_FILE = Path("thanks_data.json")

THANKS_WORDS = {
    "спасибо", "спс", "сяп", "сяпки", "благодарю", "благодарность",
    "пасиб", "пасибо", "пасибки", "thanks", "thank", "thx", "ty",
    "мерси", "рахмет", "спасиб",
}

def _load_thanks() -> None:
    global thanks_count
    if THANKS_FILE.exists():
        try:
            raw = json.loads(THANKS_FILE.read_text(encoding="utf-8"))
            thanks_count = {int(cid): {int(uid): v for uid, v in umap.items()}
                            for cid, umap in raw.items()}
        except Exception:
            thanks_count = {}

def _save_thanks() -> None:
    try:
        raw = {str(cid): {str(uid): v for uid, v in umap.items()}
               for cid, umap in thanks_count.items()}
        THANKS_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

_load_thanks()

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

    key = f"{chat_id}:{member.id}"

    if key in _welcomed_users:
        return
    _welcomed_users.add(key)

    caption = WELCOME_TEXT.format(name=member.mention_html())

    try:
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
    except Exception as e:
        logger.error(f"Ошибка при отправке приветствия (invite) для {member.id}: {e}")


async def _handle_channel_forward(message, user, context) -> bool:
    """Блокирует любые пересылки для не-админов. Возвращает True если обработано."""
    if not message.forward_origin:
        return False
    if is_admin(user.id):
        return False
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
            f"⛔ {user.mention_html()}, пересылка сообщений запрещена в этом чате.",
            parse_mode=ParseMode.HTML,
        )
        context.job_queue.run_once(
            delete_welcome_message,
            when=30,
            data={"chat_id": message.chat.id, "message_id": warn.message_id},
            name=f"del_fwd_warn_{message.chat.id}_{user.id}_{message.message_id}",
        )
        logger.info(f"Удалён форвард от {user.id} в чате {message.chat.id}")
    except BadRequest:
        pass
    return True


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
    if is_admin(user.id):
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
        group_members[chat_id][user.id] = user

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

    # ── @ВСЕ / @ALL — упомянуть всех (только для админов) ────────────────────
    if message.text and is_admin(user.id):
        txt_low = message.text.lower().strip()
        if txt_low.startswith("@все") or txt_low.startswith("@all") or txt_low.startswith("@vsem"):
            members_all = list(group_members.get(chat_id, {}).values())
            if not members_all:
                try:
                    adm_list = await context.bot.get_chat_administrators(chat_id)
                    members_all = [m.user for m in adm_list if not m.user.is_bot]
                except Exception:
                    members_all = []
            if members_all:
                # Текст после триггера (напр. "@все Собрание в 20:00!")
                trigger_end = txt_low.index(" ") if " " in txt_low else len(txt_low)
                caption = message.text[trigger_end:].strip()
                chunk_size = 30
                chunks = [members_all[i:i + chunk_size] for i in range(0, len(members_all), chunk_size)]
                for idx, chunk in enumerate(chunks):
                    # @username — настоящий пинг; без юзернейма — HTML-ссылка
                    parts = []
                    for m in chunk:
                        if m.username:
                            parts.append(f"@{m.username}")
                        else:
                            parts.append(f'<a href="tg://user?id={m.id}">{m.first_name}</a>')
                    mentions = " ".join(parts)
                    if idx == 0 and caption:
                        text = f"📢 <b>{caption}</b>\n\n{mentions}"
                    elif idx == 0:
                        text = f"📢 Внимание всем!\n\n{mentions}"
                    else:
                        text = mentions
                    await message.reply_html(text)
            else:
                await message.reply_text("Не могу найти участников чата 🤷")
            return


    # ── ДЕТЕКТОР БЛАГОДАРНОСТЕЙ ───────────────────────────────────────────────
    if message.text and message.reply_to_message:
        replied_user = message.reply_to_message.from_user
        if replied_user and not replied_user.is_bot and replied_user.id != user.id:
            msg_words = set(re.sub(r'[^a-zа-яё]', ' ', message.text.lower()).split())
            if msg_words & THANKS_WORDS:
                if chat_id not in thanks_count:
                    thanks_count[chat_id] = {}
                uid = replied_user.id
                if uid not in thanks_count[chat_id]:
                    thanks_count[chat_id][uid] = {"count": 0, "name": replied_user.first_name}
                thanks_count[chat_id][uid]["count"] += 1
                thanks_count[chat_id][uid]["name"] = replied_user.first_name
                _save_thanks()
                total = thanks_count[chat_id][uid]["count"]
                mention_r = f'<a href="tg://user?id={replied_user.id}">{replied_user.first_name}</a>'
                reactions = [
                    f"🙏 {mention_r} получает благодарность! Всего: <b>{total}</b> 🏆",
                    f"❤️ Спасибо сказали {mention_r}! Уже <b>{total}</b> раз(а)!",
                    f"✨ {mention_r} — хороший человек! <b>{total}</b> благодарность(ей) в копилке",
                    f"🎖 {mention_r} зарабатывает очки уважения! Итого: <b>{total}</b>",
                ]
                await message.reply_html(random.choice(reactions))

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

    # Проверка упоминаний — только для не-админов
    if not is_admin(user.id) and await _has_non_admin_mention(message, context.bot, chat_id):
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

    if reason is None and DELETE_LINKS and not is_admin(user.id) and URL_PATTERN.search(message.text):
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


async def thanks_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает топ участников по благодарностям."""
    message = update.effective_message
    if not message:
        return
    chat_id = message.chat_id
    chat_data = thanks_count.get(chat_id, {})

    if not chat_data:
        await message.reply_text("Пока никто никому не говорил спасибо 🤷 Будьте добрее друг к другу!")
        return

    sorted_users = sorted(chat_data.items(), key=lambda x: x[1]["count"], reverse=True)[:10]

    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = ["🏆 <b>Топ самых благодарных людей чата:</b>\n"]
    for i, (uid, data) in enumerate(sorted_users):
        name = data["name"]
        count = data["count"]
        medal = medals[i]
        lines.append(f"{medal} <b>{name}</b> — {count} спасибо")

    await message.reply_html("\n".join(lines))


async def mention_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Упоминает всех участников чата. Только для админов."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_admin(user.id):
        return

    chat_id = message.chat_id

    # Собираем участников из памяти
    members = list(group_members.get(chat_id, {}).values())

    # Если памяти нет — берём администраторов как запасной вариант
    if not members:
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            members = [m.user for m in admins if not m.user.is_bot]
        except Exception:
            members = []

    if not members:
        await message.reply_text("Не могу найти участников чата 🤷")
        return

    # Берём необязательный текст после команды
    caption = " ".join(context.args).strip() if context.args else ""

    # Строим упоминания — разбиваем на части по 30 человек
    chunk_size = 30
    chunks = [members[i:i + chunk_size] for i in range(0, len(members), chunk_size)]

    for idx, chunk in enumerate(chunks):
        parts = []
        for m in chunk:
            if m.username:
                parts.append(f"@{m.username}")
            else:
                parts.append(f'<a href="tg://user?id={m.id}">{m.first_name}</a>')
        mentions = " ".join(parts)
        if idx == 0 and caption:
            text = f"📢 <b>{caption}</b>\n\n{mentions}"
        elif idx == 0:
            text = f"📢 Внимание всем!\n\n{mentions}"
        else:
            text = mentions
        await message.reply_html(text)


async def block_commands_for_nonadmins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляет любые команды от не-админов и останавливает дальнейшую обработку."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if is_admin(user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    raise ApplicationHandlerStop


def _random_mention(exclude_id: int) -> tuple[int, str] | None:
    """Возвращает (user_id, full_name) случайного участника, кроме exclude_id."""
    candidates = {uid: name for uid, name in _seen_users.items() if uid != exclude_id}
    if not candidates:
        return None
    return random.choice(list(candidates.items()))


def _mention_html(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{name}</a>'


def _extract_name_after_dyad(text_lower: str, text_orig: str) -> str | None:
    """Ищет имя (слово с большой буквы) сразу после 'дядя' в оригинальном тексте."""
    m = re.search(r'дядя\s+([А-ЯЁA-Z][а-яёa-z]+)', text_orig)
    if m:
        return m.group(1)
    # Попытка без регистра — любое слово после дядя
    m2 = re.search(r'дяд[яию]\s+(\w+)', text_lower)
    if m2:
        word = m2.group(1)
        # Ищем оригинальный регистр
        orig = re.search(word, text_orig, re.IGNORECASE)
        return orig.group(0).capitalize() if orig else word.capitalize()
    return None


async def handle_dyad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    user = message.from_user
    if not user:
        return

    chat_id = message.chat_id

    # Всегда запоминаем пользователя (для базы "кто X?")
    _seen_users[user.id] = user.full_name

    # Проверка: написал ли админ.
    # Если кэш пустой — запрашиваем напрямую (на случай первых минут после старта).
    admin_ok = is_admin(user.id)
    if not admin_ok and not _cached_chat_admins:
        try:
            fresh = await context.bot.get_chat_administrators(chat_id)
            fresh_ids = {a.user.id for a in fresh}
            _cached_chat_admins.update(fresh_ids)
            admin_ok = user.id in fresh_ids
        except Exception:
            pass
    if not admin_ok:
        return

    text = message.text.strip()
    tl = text.lower()

    if "дядя" not in tl:
        return

    # Вспомогательная: получить оригинальный регистр слова
    def orig_case(word: str) -> str:
        m = re.search(word, text, re.IGNORECASE)
        return m.group(0) if m else word.capitalize()

    # Вспомогательная: взять рандомного участника и вернуть mention
    async def random_victim() -> str | None:
        victim = _random_mention(user.id)
        if victim:
            return _mention_html(victim[0], victim[1])
        # Фоллбэк — берём из Telegram
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            others = [a.user for a in admins if not a.user.is_bot and a.user.id != user.id]
            if others:
                chosen = random.choice(others)
                return _mention_html(chosen.id, chosen.first_name)
        except Exception:
            pass
        return None

    # ── 1. "[Имя] любит тебя" ────────────────────────────────────────────────
    love_m = re.search(r'(\w+)\s+люб(?:ит|лю)(?:\s+теб[яе]|\s+меня)?', tl)
    if love_m:
        name = orig_case(love_m.group(1))
        await message.reply_text(random.choice(DYAD_LOVE_RESPONSES).format(name=name))
        return

    # ── 2. "[Имя] хочет бить / ударить" ─────────────────────────────────────
    beat_m = re.search(r'(\w+)\s+хоч(?:ет|у)\s+(?:бить|ударить|побить)', tl)
    if beat_m:
        name = orig_case(beat_m.group(1))
        responses = [
            f"О-о, {name} в боевом настроении! 🥊 Советую держаться подальше!",
            f"{name} сегодня злой(-ая)! 😤 Лучше не попадайся под руку!",
            f"Ай-ай, {name} буянит! 😬 Кто следующий?",
            f"{name} объявил(-а) войну! ⚔️ Спасайся кто может!",
        ]
        await message.reply_text(random.choice(responses))
        return

    # ── 3. "[Имя] хочет трахать / ебать" ────────────────────────────────────
    sex_m = re.search(r'(\w+)\s+хоч(?:ет|у)\s+(?:трахать|ебать|потрахать|иметь)', tl)
    if sex_m:
        name = orig_case(sex_m.group(1))
        responses = [
            f"Ого, {name} не теряет время! 🙈 Дядя всё видит!",
            f"{name}, умерь пыл, дядя смотрит! 😳",
            f"Сурово, {name}! Дядя промолчит... на этот раз 🤫",
            f"{name} пришёл(-ла) по делу! 😏 Дядя одобряет смелость!",
        ]
        await message.reply_text(random.choice(responses))
        return

    # ── 4. "что/чё/чо делает [Имя]?" — шуточный ответ ───────────────────────
    doing_m = re.search(r'(?:что|чё|чо|че)\s+делает\s+(\w+)', tl)
    if doing_m:
        name = orig_case(doing_m.group(1))
        deflect = [
            f"Я что, нянька {name}а? 😤 Иди сам спроси!",
            f"Откуда мне знать что там {name} делает 🤷 Я дядя, а не шпион",
            f"Слежу за {name}? Ты серьёзно? 😂 Иди сам разберись",
            f"Думаешь я за {name}ом слежу? 👀 Ты ошибся адресом",
            f"Яндекс знает где {name}. Я — нет. Гугли 🗺",
            f"Я {name}у не мать и не отец 🙅 Сам ищи",
        ]
        await message.reply_text(random.choice(deflect))
        return

    # ── 5. "кто такой / кто такая [Имя]" ────────────────────────────────────
    who_is_m = re.search(r'кто\s+так(?:ой|ая|ие)\s+(\w+)', tl)
    if who_is_m:
        name = orig_case(who_is_m.group(1))
        await message.reply_text(random.choice(DYAD_WHO_IS_RESPONSES).format(name=name))
        return

    # ── 6. "кто [Имя] [ярлык]?" — подтверждаем что Имя = ярлык ─────────────
    #       Например: "кто Давлат петух?" → "Давлат — петух, дядя подтверждает!"
    who_name_label_m = re.search(r'кто\s+([А-ЯЁа-яёa-zA-Z]{2,})\s+([А-ЯЁа-яёa-zA-Z]{2,})\??', text)
    if who_name_label_m:
        w1 = who_name_label_m.group(1)
        w2 = who_name_label_m.group(2)
        # Считаем имя то слово, которое с большой буквы или стоит первым
        if w1[0].isupper() or (not w2[0].isupper()):
            name, label = w1, w2.lower()
        else:
            name, label = w2, w1.lower()
        responses = [
            f"Дядя подтверждает — {name} это {label}! 😂 Всё, официально!",
            f"Да, {name} — настоящий {label}! Дядя видел лично! 👁️",
            f"Зафиксировано! {name} = {label}. Можно не спорить 📋",
            f"{name}? {label.capitalize()}? Звучит правдиво! 😏",
            f"Дядя объявляет: {name} — {label} нашего чата! 🏆",
        ]
        await message.reply_html(random.choice(responses))
        return

    # ── 7. "кто [ярлык]?" — рандомный участник ───────────────────────────────
    #       Например: "кто черт?", "кто вор?", "кто красавчик?"
    who_m = re.search(r'кто\s+([\wа-яёА-ЯЁ]+)', tl)
    if who_m:
        label = orig_case(who_m.group(1))
        mention = await random_victim()
        if not mention:
            await message.reply_text("Пока ещё никого не видел в чате! 👀")
            return
        templates = [
            f"Без сомнений — {mention}! 😄",
            f"Дядя расследовал: {mention} — главный {label}! 🔍",
            f"Все знают что {mention} — вот кто {label}! 😏",
            f"Спрашиваешь кто {label}? Смотри на {mention}! 👀",
            f"Ответ очевиден — {mention}! Дядя знает 😎",
            f"Официально: {label} — это {mention}! 📢",
            f"👁 Дядя всё видит. {mention} — {label}. Прятаться бесполезно 😈",
        ]
        await message.reply_html(random.choice(templates))
        return

    # ── 8. Любой вопрос с дядей — рандомный участник ─────────────────────────
    if "?" in text:
        mention = await random_victim()
        if mention:
            fallback = [
                f"Хм, сложный вопрос... но {mention} точно знает ответ! 😏",
                f"Спроси у {mention} — он(а) в курсе! 😂",
                f"Дядя думал-думал и решил: виноват {mention}! 🤷",
                f"Ответ: {mention}. Логика — дядина 😈",
            ]
            await message.reply_html(random.choice(fallback))
        return

    # ── 9. Команды / действия ("дядя спать", "дядя замолчи" и т.д.) ────────────
    ACTION_THEMES = [
        (
            ["спать", "спи", "ложись", "сон", "баиньки", "поздно", "ночь", "отдыхай"],
            [
                "Сам иди спать, я ещё не нагулялся 🌙",
                "Спать?! Да ты совсем страх потерял 😤",
                "Я лягу когда захочу. А захочу — никогда 💪",
                "Дядя не спит — дядя ждёт пока вы облажаетесь 👁️",
                "Ты серьёзно мне это говоришь? МНЕ? 😂",
                "Баиньки? Сам иди, я тут ещё не закончил 😈",
                "Отбой объявлять будешь своей кошке 🐱",
            ]
        ),
        (
            ["гулять", "гуляй", "погуляй", "прогуляйся", "выйди"],
            [
                "Я и так гуляю — по вашим нервам 😏",
                "Гулять? А платить кто будет? 😂",
                "Куда мне гулять, я и так везде 🌍",
                "Гуляю уже давно. Просто ты не замечал 😎",
                "Пойду гулять когда ты перестанешь меня раздражать 🙂",
            ]
        ),
        (
            ["петь", "пой", "спой", "пение", "песню", "песня"],
            [
                "Петь? За концерт берётся 💰 Переводи",
                "Голос есть. Но слушать тебя — не за что 🎤",
                "Спою, но только рэп и только за деньги 🎵",
                "Репертуар закончился. Как и моё терпение 🎶",
            ]
        ),
        (
            ["танцевать", "танцуй", "потанцуй", "станцуй", "танец"],
            [
                "Танцую только в пятницу, и только за уважение 🕺",
                "Колено болит. И желание тоже 🤕",
                "Флексить умею. Но не перед тобой 😏",
            ]
        ),
        (
            ["иди", "уходи", "вали", "убирайся", "уйди", "исчезни"],
            [
                "Сам иди. Я тут хозяин 😎",
                "Не дождёшься, голубчик 😏",
                "Уйду только если ты уйдёшь первым 🙃",
                "Ага, щас, разбегаюсь 🏃💨 Нет.",
                "Попробуй выгнать. Интересно посмотреть как это выйдет 😂",
            ]
        ),
        (
            ["замолчи", "заткнись", "помолчи", "тихо", "молчи", "тихо"],
            [
                "Нет 😐",
                "Ты только что это серьёзно написал? 😂",
                "Замолчать? Мне? Дядю просят замолчать?! 😤",
                "Буду молчать когда ты сам замолчишь. То есть никогда 😏",
                "Тихо будет на кладбище. Тут — нет 💀",
            ]
        ),
        (
            ["работай", "работать", "займись делом", "иди работай"],
            [
                "Я уже работаю — слежу за вами 👁️",
                "Это И ЕСТЬ моя работа, умник 😏",
                "Работаю 24/7 пока ты тут советы раздаёшь 💼",
                "Сам иди работай, мне и здесь хорошо 😎",
            ]
        ),
        (
            ["помоги", "помогай", "помощь", "выручи", "помогите"],
            [
                "Помочь? Изи. Нет 😂",
                "Техподдержка не работает. Попробуй в следующей жизни 📞",
                "Чем могу помочь? Ничем. Но я рядом 😇",
                "За помощь берётся. Переводи 💰",
            ]
        ),
        (
            ["анекдот", "расскажи", "пошути", "шутка", "рассмеши"],
            [
                "Анекдот: ты попросил меня рассказать анекдот. Смешно, правда? 😂",
                "Шутка дня: этот запрос 😂",
                "Я не клоун. Хотя... смотря на кого смотришь 🤡",
                "Знаю миллион анекдотов. Тебе — ни один 🙅",
            ]
        ),
        (
            ["ешь", "покушай", "поешь", "голодный", "еда", "кушать"],
            [
                "Я не голодный, я злой 😈",
                "Сам поешь и мне расскажи как было 😂",
                "Еда? Это для слабых. Дядя питается нервами 😤",
                "Не отвлекай меня от слежки за чатом 👁️",
            ]
        ),
        (
            ["привет", "здарова", "салам", "здравствуй", "хай", "hi", "hello"],
            [
                "О, кто пришёл 👀 Привет-привет 😏",
                "Явился наконец 😎",
                "Привет. Ты всё ещё тут? 😂",
                "Салам! Долго тебя не было 🙃",
            ]
        ),
    ]

    clean = tl.replace("дядя", "").strip()
    for keywords, responses in ACTION_THEMES:
        if any(kw in clean for kw in keywords):
            await message.reply_text(random.choice(responses))
            return

    # ── 10. Просто "дядя" без вопроса и без команды — молчим ────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления:", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("Переменная окружения TELEGRAM_BOT_TOKEN не задана!")

    # Небольшая пауза для корректного перезапуска на хостингах
    time.sleep(5)

    _load_admin_ids()

    app = Application.builder().token(BOT_TOKEN).build()

    app.job_queue.run_repeating(_cleanup_old_data, interval=3600, first=3600)

    # Блокировка команд для не-админов (group=-1 — выполняется раньше всего)
    app.add_handler(MessageHandler(filters.COMMAND, block_commands_for_nonadmins), group=-1)

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
    app.add_handler(CommandHandler("all", mention_all))
    app.add_handler(CommandHandler("vsem", mention_all))
    app.add_handler(CommandHandler("top", thanks_top))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Системные события
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, delete_left_message))
    # Приветствие через invite-ссылку (chat_member update)
    app.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Дядя-бот — обрабатывает все текстовые сообщения для трекинга и ответов
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_dyad), group=1)

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
