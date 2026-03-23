#!/usr/bin/env python3
"""
Telegram-бот ассистент Андрея.
Принимает голосовые сообщения -> транскрибирует Whisper -> выполняет через claude CLI.
Поддерживает текст, фото и видео.
"""

import asyncio
import datetime
import logging
import os
import tempfile
import time
from functools import partial
from typing import Any, Awaitable, TypedDict, TypeVar, cast

import whisper
from telegram import Message, Update, User
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN_RAW = os.environ.get("BOT_TOKEN")
ALLOWED_USER_ID_RAW = os.environ.get("ALLOWED_USER_ID")

if not BOT_TOKEN_RAW:
    raise RuntimeError("BOT_TOKEN is required in environment")
if not ALLOWED_USER_ID_RAW:
    raise RuntimeError("ALLOWED_USER_ID is required in environment")

BOT_TOKEN: str = BOT_TOKEN_RAW

try:
    ALLOWED_USER_ID = int(ALLOWED_USER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ALLOWED_USER_ID must be an integer") from exc

logger.info("Загружаю модель Whisper...")
whisper_model = whisper.load_model("large-v3")
logger.info("Whisper готов.")

SYSTEM_PROMPT = """Ты личный ассистент Андрея Романова. Тебе передаётся запрос.
Выполни то, о чём просит Андрей: поставить задачу в трекер, посмотреть задачи, отправить письмо и т.д.
Отвечай кратко - только результат действия или ответ на вопрос.
Дата сегодня: {date}.
"""


class HistoryEntry(TypedDict):
    role: str
    text: str


class WhisperResult(TypedDict):
    text: str


MAX_HISTORY = 20
conversation_history: list[HistoryEntry] = []
TELEGRAM_HEALTHCHECK_INTERVAL = 60
TELEGRAM_MAX_UNHEALTHY_SECONDS = 300
last_telegram_ok_at = time.monotonic()
T = TypeVar("T")


def normalize_proxy_environment() -> None:
    """Оставляем только HTTP(S)-прокси и убираем конфликтный SOCKS env."""
    # Сервис стартует под systemd и может унаследовать глобальный ALL_PROXY,
    # который ломает запросы Telegram. Явно оставляем только HTTP(S)-прокси.
    for key in ("ALL_PROXY", "all_proxy"):
        if os.environ.pop(key, None):
            logger.info("Убрана переменная окружения %s", key)

    http_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    https_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")

    if http_proxy:
        os.environ["http_proxy"] = http_proxy
        os.environ["HTTP_PROXY"] = http_proxy
    if https_proxy:
        os.environ["https_proxy"] = https_proxy
        os.environ["HTTPS_PROXY"] = https_proxy


def mark_telegram_ok() -> None:
    global last_telegram_ok_at
    last_telegram_ok_at = time.monotonic()


def require_user(update: Update) -> User:
    # PTB типизирует effective_user/message как optional, хотя в наших хендлерах
    # дальше код имеет смысл только для обычных входящих сообщений.
    user = update.effective_user
    if user is None:
        raise RuntimeError("Update has no effective_user")
    return user


def require_message(update: Update) -> Message:
    message = update.message
    if message is None:
        raise RuntimeError("Update has no message")
    return message


def add_to_history(user_text: str, assistant_text: str) -> None:
    conversation_history.append({"role": "user", "text": user_text})
    conversation_history.append({"role": "assistant", "text": assistant_text})
    while len(conversation_history) > MAX_HISTORY * 2:
        conversation_history.pop(0)


def build_prompt(user_text: str) -> str:
    today = datetime.date.today().isoformat()
    system = SYSTEM_PROMPT.format(date=today)

    history_block = ""
    if conversation_history:
        lines: list[str] = []
        for msg in conversation_history[-MAX_HISTORY:]:
            role = "Андрей" if msg["role"] == "user" else "Ассистент"
            lines.append(f"{role}: {msg['text']}")
        history_block = "История разговора:\n" + "\n".join(lines) + "\n\n"

    return f"{system}\n\n{history_block}Запрос: {user_text}"


CLAUDE_TIMEOUT = 600
MAX_MSG_LEN = 4000
PROGRESS_INTERVAL = 30


async def send_response(msg: Message, text: str, prefix: str = "✅ ") -> None:
    full = prefix + text
    if len(full) <= MAX_MSG_LEN:
        await msg.edit_text(full)
        return

    await msg.edit_text(full[:MAX_MSG_LEN])
    for i in range(MAX_MSG_LEN, len(full), MAX_MSG_LEN):
        await msg.reply_text(full[i:i + MAX_MSG_LEN])


async def run_with_progress(coro: Awaitable[T], msg: Message, prefix: str = "") -> T:
    # Обёртка нужна, чтобы длинные операции не выглядели как зависание бота.
    task: asyncio.Task[T] = asyncio.ensure_future(coro)
    elapsed = 0
    dots = ["⏳", "⌛"]
    i = 0
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=PROGRESS_INTERVAL)
        except asyncio.TimeoutError:
            elapsed += PROGRESS_INTERVAL
            try:
                await msg.edit_text(f"{dots[i % 2]} {prefix}Выполняю... ({elapsed}с)")
            except Exception:
                pass
            i += 1
        except Exception:
            break
    return await task


async def _run_claude_proc(args: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/home/andrey/Projects/my-assistant",
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    response = stdout.decode("utf-8").strip()
    if not response and stderr:
        response = f"Ошибка: {stderr.decode('utf-8').strip()}"
    return response


async def run_claude(prompt: str) -> str:
    full_prompt = build_prompt(prompt)
    return await _run_claude_proc(
        ["/home/andrey/.local/bin/claude", "--print", "--dangerously-skip-permissions", full_prompt]
    )


async def run_claude_vision(prompt: str, image_path: str) -> str:
    full_prompt = build_prompt(
        f"Изображение сохранено в файл: {image_path}\nПрочитай этот файл и выполни задание.\n{prompt}"
    )
    return await _run_claude_proc(
        ["/home/andrey/.local/bin/claude", "--print", "--dangerously-skip-permissions", full_prompt]
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = require_user(update)
    if user.id != ALLOWED_USER_ID:
        return

    message = require_message(update)
    logger.info("Голосовое от %s (%s)", user.id, user.username)
    msg = await message.reply_text("🎙 Транскрибирую...")

    voice = message.voice
    if voice is None:
        await msg.edit_text("❌ В сообщении нет голосового файла.")
        return

    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await tg_file.download_to_drive(tmp_path)

        loop = asyncio.get_running_loop()
        transcribe = partial(whisper_model.transcribe, tmp_path, language="ru")
        # whisper.transcribe возвращает нестрого типизированный dict, поэтому
        # здесь явно приводим результат к ожидаемой структуре.
        result = cast(
            WhisperResult,
            await run_with_progress(
                loop.run_in_executor(None, transcribe),
                msg,
                prefix="🎙 Транскрибирую... ",
            ),
        )
        text = result["text"].strip()
        logger.info("Транскрипция: %s", text)

        await msg.edit_text(f"🎙 «{text}»\n\n⏳ Выполняю...")

        response = await run_with_progress(run_claude(text), msg, prefix=f"🎙 «{text}»\n\n")
        add_to_history(text, response)
        logger.info("Ответ claude: %s", response[:200])
        await send_response(msg, response, prefix=f"🎙 «{text}»\n\n✅ ")

    except asyncio.TimeoutError:
        await msg.edit_text("⏱ Превышено время ожидания. Попробуй ещё раз.")
    except Exception as exc:
        logger.exception("Ошибка обработки голосового")
        await msg.edit_text(f"❌ Ошибка: {exc}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = require_user(update)
    if user.id != ALLOWED_USER_ID:
        return

    message = require_message(update)
    logger.info("Фото от %s (%s)", user.id, user.username)
    msg = await message.reply_text("🖼 Анализирую фото...")

    if not message.photo:
        await msg.edit_text("❌ В сообщении нет фото.")
        return

    photo = message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    caption = message.caption or "Что на этом фото? Опиши и выполни если есть задание."

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await tg_file.download_to_drive(tmp_path)

        response = await run_with_progress(run_claude_vision(caption, image_path=tmp_path), msg)
        add_to_history(f"[фото] {caption}", response)
        logger.info("Ответ claude: %s", response[:200])
        await send_response(msg, response)

    except asyncio.TimeoutError:
        await msg.edit_text("⏱ Превышено время ожидания.")
    except Exception as exc:
        logger.exception("Ошибка обработки фото")
        await msg.edit_text(f"❌ Ошибка: {exc}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = require_user(update)
    if user.id != ALLOWED_USER_ID:
        return

    message = require_message(update)
    logger.info("Видео от %s (%s)", user.id, user.username)
    msg = await message.reply_text("🎬 Обрабатываю видео...")

    video = message.video or message.video_note
    if video is None:
        await msg.edit_text("❌ В сообщении нет видео.")
        return

    tg_file = await context.bot.get_file(video.file_id)
    caption = message.caption or "Что на этом видео? Опиши и выполни если есть задание."

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        video_path = tmp.name
    frame_path = video_path + "_frame.jpg"

    try:
        await tg_file.download_to_drive(video_path)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            frame_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)

        if not os.path.exists(frame_path):
            await msg.edit_text("❌ Не удалось извлечь кадр из видео.")
            return

        response = await run_with_progress(run_claude_vision(caption, image_path=frame_path), msg)
        add_to_history(f"[видео] {caption}", response)
        logger.info("Ответ claude: %s", response[:200])
        await send_response(msg, response)

    except asyncio.TimeoutError:
        await msg.edit_text("⏱ Превышено время ожидания.")
    except Exception as exc:
        logger.exception("Ошибка обработки видео")
        await msg.edit_text(f"❌ Ошибка: {exc}")
    finally:
        if os.path.exists(video_path):
            os.unlink(video_path)
        if os.path.exists(frame_path):
            os.unlink(frame_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user = require_user(update)
    if user.id != ALLOWED_USER_ID:
        return

    message = require_message(update)
    text = message.text
    if text is None:
        return
    text = text.strip()
    logger.info("Текст от %s: %s", user.id, text)

    msg = await message.reply_text("⏳ Выполняю...")

    try:
        response = await run_with_progress(run_claude(text), msg)
        add_to_history(text, response)
        await send_response(msg, response)

    except asyncio.TimeoutError:
        await msg.edit_text("⏱ Превышено время ожидания.")
    except Exception as exc:
        logger.exception("Ошибка обработки текста")
        await msg.edit_text(f"❌ Ошибка: {exc}")


async def handle_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user = require_user(update)
    if user.id != ALLOWED_USER_ID:
        return

    message = require_message(update)
    parts: list[str] = []
    if message.forward_origin or getattr(message, "forward_date", None):
        parts.append("[Пересланное сообщение]")
    if message.text:
        parts.append(message.text)
    if message.caption:
        parts.append(message.caption)
    if message.document:
        parts.append(f"[Документ: {message.document.file_name}]")
    if message.sticker and message.sticker.emoji:
        parts.append(f"[Стикер: {message.sticker.emoji}]")

    content = "\n".join(parts).strip()
    if not content:
        return

    logger.info("Прочее сообщение от %s: %s", user.id, content[:100])
    msg = await message.reply_text("⏳ Выполняю...")

    try:
        response = await run_with_progress(run_claude(content), msg)
        add_to_history(content, response)
        await send_response(msg, response)
    except asyncio.TimeoutError:
        await msg.edit_text("⏱ Превышено время ожидания.")
    except Exception as exc:
        logger.exception("Ошибка обработки сообщения")
        await msg.edit_text(f"❌ Ошибка: {exc}")


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user = require_user(update)
    if user.id != ALLOWED_USER_ID:
        return

    message = require_message(update)
    conversation_history.clear()
    await message.reply_text("🔄 История очищена. Начинаем новый разговор.")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user = require_user(update)
    message = require_message(update)
    await message.reply_text(
        f"Привет, {user.first_name}! Твой ID: `{user.id}`\n"
        "Отправь голосовое, текст, фото или видео - выполню.",
        parse_mode="Markdown",
    )


async def telegram_watchdog(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    # Если Telegram API недоступен слишком долго, принудительно завершаем процесс.
    # Дальше восстановлением занимается systemd через Restart=always.
    while True:
        await asyncio.sleep(TELEGRAM_HEALTHCHECK_INTERVAL)
        try:
            await app.bot.get_me()
            mark_telegram_ok()
        except Exception:
            unhealthy_for = int(time.monotonic() - last_telegram_ok_at)
            logger.exception(
                "Проверка Telegram API не прошла, бот без связи %sс",
                unhealthy_for,
            )
            if unhealthy_for >= TELEGRAM_MAX_UNHEALTHY_SECONDS:
                logger.error(
                    "Telegram недоступен слишком долго (%sс), аварийно завершаю процесс для рестарта systemd",
                    unhealthy_for,
                )
                os._exit(1)


async def post_init(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    # Сначала проверяем, что Telegram доступен на старте, и только потом
    # запускаем фоновый watchdog.
    await app.bot.get_me()
    mark_telegram_ok()
    app.bot_data["watchdog_task"] = asyncio.create_task(telegram_watchdog(app))
    logger.info("Watchdog Telegram API запущен.")


async def post_shutdown(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    task = cast(asyncio.Task[None] | None, app.bot_data.get("watchdog_task"))
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    normalize_proxy_environment()
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connection_pool_size(8)
        .connect_timeout(60)
        .read_timeout(90)
        .write_timeout(90)
        .pool_timeout(120)
        .concurrent_updates(False)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("new", handle_new))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.ALL, handle_any))

    logger.info("Бот запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
