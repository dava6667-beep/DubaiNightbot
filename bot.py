import os
import re
import time
import base64
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions,
    MessageOriginChannel,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

_openai_base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
_openai_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", "dummy")

openai_client = AsyncOpenAI(
    base_url=_openai_base_url if _openai_base_url else None,
    api_key=_openai_api_key,
)

BANNED_WORDS = set([
    # Русские
    "секс",
    "заебал",
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
])

URL_PATTERN = re.compile(
    r"(https?://|www\.)\S+|"
    r"t\.me/\S+|"
    r"\S+\.(com|net|org|ru|io|xyz|info|biz|me|tv|co)\b",
    re.IGNORECASE,
)

DELETE_LINKS = False
MAX_WARNINGS = 3
SPAM_MAX_MESSAGES = 5
SPAM_WINDOW_SECONDS = 10
SPAM_MUTE_MINUTES = 5

user_warnings: dict[str, int] = {}
user_message_times: dict[str, list] = {}

LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID")


async def send_log(bot, text: str) -> None:
    if not LOG_CHANNEL_ID:
        return
    try:
        await bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Не удалось отправить лог в канал: {e}")


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
    admin_ids_raw = os.environ.get("ADMIN_IDS", "")
    if not admin_ids_raw:
        return False
    try:
        admin_ids = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()]
        return user_id in admin_ids
    except ValueError:
        return False


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
            f"Чат: <code>{chat_id}</code>"
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
            f"Чат: <code>{chat_id}</code>"
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
            f"Чат: <code>{chat_id}</code>"
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
            f"Чат: <code>{chat_id}</code>"
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

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        chat_id = update.effective_chat.id
        key = f"{chat_id}:{member.id}"

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
            msg = await update.message.reply_html(caption)

        context.job_queue.run_once(
            delete_welcome_message,
            when=30,
            data={"chat_id": chat_id, "message_id": msg.message_id},
            name=f"del_welcome_{key}",
        )

        logger.info(f"Новый участник {member.id} (@{member.username}) вошёл в чат {chat_id}")
        await send_log(
            context.bot,
            f"👤 <b>Новый участник</b>\n"
            f"Пользователь: {member.mention_html()} (<code>{member.id}</code>)\n"
            f"Имя: {member.full_name}\n"
            f"Чат: <code>{chat_id}</code>"
        )


async def _handle_channel_forward(message, user, context) -> bool:
    """Проверяет форвард из канала, удаляет и предупреждает. Возвращает True если обработано."""
    if not message.forward_origin or is_admin(user.id):
        return False
    if isinstance(message.forward_origin, MessageOriginChannel):
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
                name=f"del_fwd_warn_{message.chat.id}_{user.id}",
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
    await _handle_channel_forward(message, user, context)


async def filter_nsfw_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.photo:
        return

    user = message.from_user
    if not user:
        return

    if await _handle_channel_forward(message, user, context):
        return

    if is_admin(user.id):
        return

    photo = message.photo[-1]
    try:
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        b64 = base64.b64encode(file_bytes).decode("utf-8")

        # ИСПРАВЛЕНИЕ: gpt-5-mini не существует — заменено на gpt-4o-mini
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_completion_tokens=10,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                        {
                            "type": "text",
                            "text": (
                                "Does this image contain adult/explicit/18+ content such as nudity, "
                                "sexual acts, or pornographic material? "
                                "Reply with exactly one word: YES or NO."
                            ),
                        },
                    ],
                }
            ],
        )

        answer = response.choices[0].message.content.strip().upper()
        logger.info(f"NSFW проверка фото от {user.id}: {answer}")

        if answer.startswith("YES"):
            await message.delete()
            warn = await message.chat.send_message(
                f"🔞 {user.mention_html()}, фотографии с контентом 18+ запрещены в этом чате. "
                f"Сообщение удалено.",
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
            )

    except Exception as e:
        logger.warning(f"Ошибка при проверке фото на 18+: {e}")


async def filter_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    user = message.from_user
    if not user:
        return

    chat_id = message.chat.id
    key = f"{chat_id}:{user.id}"

    if await _handle_channel_forward(message, user, context):
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
            )
        except BadRequest as e:
            logger.warning(f"Ошибка спам-мута {user.id}: {e}")
        return

    text = message.text.lower()
    reason = None

    for word in BANNED_WORDS:
        if word in text:
            reason = f"запрещённое слово: <code>{word}</code>"
            break

    if reason is None and DELETE_LINKS and URL_PATTERN.search(message.text):
        reason = "ссылки запрещены в этом чате"

    if reason:
        user_warnings[key] = user_warnings.get(key, 0) + 1
        count = user_warnings[key]
        remaining = MAX_WARNINGS - count

        try:
            await message.delete()
            logger.info(
                f"Удалено сообщение от {user.id} (@{user.username}) — {reason} — предупреждение {count}/{MAX_WARNINGS}"
            )

            if remaining > 0:
                warn_text = (
                    f"⚠️ {user.mention_html()}, ваше сообщение удалено ({reason}).\n"
                    f"Предупреждение <b>{count}/{MAX_WARNINGS}</b> — осталось {remaining} предупреждений."
                )
            else:
                warn_text = (
                    f"🚫 {user.mention_html()}, ваше сообщение удалено ({reason}).\n"
                    f"Вы получили максимальное количество предупреждений: <b>{MAX_WARNINGS}/{MAX_WARNINGS}</b>. "
                    f"Пожалуйста, соблюдайте правила чата."
                )

            await message.chat.send_message(warn_text, parse_mode=ParseMode.HTML)
            await send_log(
                context.bot,
                f"⚠️ <b>Нарушение правил</b>\n"
                f"Пользователь: {user.mention_html()} (<code>{user.id}</code>)\n"
                f"Причина: {reason}\n"
                f"Предупреждение: <b>{count}/{MAX_WARNINGS}</b>\n"
                f"Чат: <code>{chat_id}</code>"
            )
        except BadRequest as e:
            logger.warning(f"Не удалось удалить сообщение: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления:", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("Переменная окружения TELEGRAM_BOT_TOKEN не задана!")

    app = Application.builder().token(BOT_TOKEN).build()

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
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
