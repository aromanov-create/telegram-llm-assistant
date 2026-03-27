#!/usr/bin/env python3
"""
Telegram-бот личный ассистент.
Принимает голосовые сообщения -> транскрибирует Whisper -> выполняет через claude CLI.
Поддерживает текст, фото и видео.
"""

import asyncio
import datetime
import json
import logging
import os
import pathlib
import socket
import tempfile
import time
from functools import partial
from typing import Any, Awaitable, TypedDict, TypeVar, cast

import whisper
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN_RAW = os.environ.get("BOT_TOKEN")
ALLOWED_USER_ID_RAW = os.environ.get("ALLOWED_USER_ID")
USER_NAME_RAW = os.environ.get("USER_NAME", "пользователь")
CLAUDE_PATH_RAW = os.environ.get("CLAUDE_PATH")

if not BOT_TOKEN_RAW:
    raise RuntimeError("BOT_TOKEN is required in environment")
if not ALLOWED_USER_ID_RAW:
    raise RuntimeError("ALLOWED_USER_ID is required in environment")
if not CLAUDE_PATH_RAW:
    raise RuntimeError("CLAUDE_PATH is required in environment")

BOT_TOKEN: str = BOT_TOKEN_RAW
CLAUDE_PATH: str = CLAUDE_PATH_RAW
USER_NAME: str = USER_NAME_RAW

try:
    ALLOWED_USER_ID = int(ALLOWED_USER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ALLOWED_USER_ID must be an integer") from exc

logger.info("Загружаю модель Whisper...")
whisper_model = whisper.load_model("large-v3")
logger.info("Whisper готов.")

SYSTEM_PROMPT = """Ты личный ассистент {user_name}. Тебе передаётся запрос.
Выполни то, о чём просит {user_name}: поставить задачу в трекер, посмотреть задачи, отправить письмо и т.д.
Отвечай кратко - только результат действия или ответ на вопрос.
Думай и рассуждай ТОЛЬКО на русском языке.
Дата сегодня: {date}.
"""


class HistoryEntry(TypedDict):
    role: str
    text: str


class WhisperResult(TypedDict):
    text: str


MAX_HISTORY = 20
conversation_history: list[HistoryEntry] = []
active_tasks: dict[int, "asyncio.Task[Any]"] = {}
TELEGRAM_HEALTHCHECK_INTERVAL = 60
TELEGRAM_MAX_UNHEALTHY_SECONDS = 120
POLLING_MAX_UNHEALTHY_SECONDS = 300  # 5 минут без успешного getUpdates → перезапуск
HEARTBEAT_FILE = "/tmp/assistant-bot-alive"
last_telegram_ok_at = time.monotonic()
last_get_updates_ok = time.monotonic()
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


def _sd_notify(msg: str) -> None:
    """Отправляет уведомление systemd через NOTIFY_SOCKET (если доступен)."""
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(sock_path)
            sock.send(msg.encode())
    except Exception:
        pass


def mark_telegram_ok() -> None:
    global last_telegram_ok_at
    last_telegram_ok_at = time.monotonic()
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(last_telegram_ok_at))
    except Exception:
        pass


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
    system = SYSTEM_PROMPT.format(date=today, user_name=USER_NAME)

    history_block = ""
    if conversation_history:
        lines: list[str] = []
        for msg in conversation_history[-MAX_HISTORY:]:
            role = USER_NAME if msg["role"] == "user" else "Ассистент"
            lines.append(f"{role}: {msg['text']}")
        history_block = "История разговора:\n" + "\n".join(lines) + "\n\n"

    return f"{system}\n\n{history_block}ВАЖНО: все рассуждения, мысли и thinking веди ТОЛЬКО на русском языке.\n\nЗапрос: {user_text}"


CLAUDE_TIMEOUT = 600
MAX_MSG_LEN = 4000
PROGRESS_INTERVAL = 30
CANCEL_CALLBACK = "cancel_request"
CANCEL_KB = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=CANCEL_CALLBACK)]])
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}


async def run_claude_cancellable(coro: "Awaitable[str]", chat_id: int) -> str:
    task: asyncio.Task[str] = asyncio.ensure_future(coro)
    active_tasks[chat_id] = task
    try:
        return await task
    finally:
        active_tasks.pop(chat_id, None)


async def send_response(msg: Message, text: str, prefix: str = "✅ ") -> None:
    full = prefix + text
    if len(full) <= MAX_MSG_LEN:
        try:
            await msg.edit_text(full, parse_mode="Markdown")
        except Exception:
            await msg.edit_text(full)
        return

    try:
        await msg.edit_text(full[:MAX_MSG_LEN], parse_mode="Markdown")
    except Exception:
        await msg.edit_text(full[:MAX_MSG_LEN])
    for i in range(MAX_MSG_LEN, len(full), MAX_MSG_LEN):
        try:
            await msg.reply_text(full[i:i + MAX_MSG_LEN], parse_mode="Markdown")
        except Exception:
            await msg.reply_text(full[i:i + MAX_MSG_LEN])


async def run_with_progress(coro: Awaitable[T], msg: Message, prefix: str = "") -> T:
    # Обёртка нужна, чтобы длинные операции не выглядели как зависание бота.
    task: asyncio.Task[T] = asyncio.ensure_future(coro)
    chat_id = msg.chat_id
    active_tasks[chat_id] = task  # type: ignore[assignment]
    elapsed = 0
    dots = ["⏳", "⌛"]
    i = 0
    try:
        try:
            await msg.edit_text(f"⏳ {prefix}Выполняю...", reply_markup=CANCEL_KB)
        except Exception:
            pass
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=PROGRESS_INTERVAL)
            except asyncio.TimeoutError:
                elapsed += PROGRESS_INTERVAL
                try:
                    await msg.edit_text(
                        f"{dots[i % 2]} {prefix}Выполняю... ({elapsed}с)",
                        reply_markup=CANCEL_KB,
                    )
                except Exception:
                    pass
                i += 1
            except Exception:
                break
        return await task
    finally:
        active_tasks.pop(chat_id, None)


STREAM_UPDATE_INTERVAL = 2  # секунд между обновлениями превью


def _extract_progress(event: dict) -> str:
    """Извлекает текст для превью из одного stream-json события."""
    t = event.get("type")
    if t == "assistant":
        parts = []
        for block in event.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text":
                parts.append(block["text"])
            elif bt == "thinking":
                thinking = block.get("thinking", "")[:300]
                parts.append(f"💭 {thinking}\n")
            elif bt == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if name == "Bash":
                    cmd = str(inp.get("command", ""))[:150]
                    parts.append(f"\n🔧 `{cmd}`\n")
                elif name in ("Read", "Write", "Edit"):
                    path = str(inp.get("file_path", ""))
                    icons = {"Read": "📖", "Write": "✏️", "Edit": "✏️"}
                    parts.append(f"\n{icons[name]} {name}: {path}\n")
                elif name == "Glob":
                    parts.append(f"\n🔍 Glob: {inp.get('pattern', '')}\n")
                elif name == "Grep":
                    parts.append(f"\n🔍 Grep: {inp.get('pattern', '')}\n")
                elif name in ("WebFetch", "WebSearch"):
                    val = str(inp.get("url", inp.get("query", "")))[:80]
                    parts.append(f"\n🌐 {name}: {val}\n")
                else:
                    parts.append(f"\n🔧 {name}\n")
        return "".join(parts)
    if t == "user":
        # результат выполнения инструмента (вывод команды и т.д.)
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, str) and content.strip():
                    return f"\n📤 {content.strip()[:300]}\n"
                if isinstance(content, list):
                    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    combined = "\n".join(texts).strip()[:300]
                    if combined:
                        return f"\n📤 {combined}\n"
    return ""


async def _run_claude_streaming(args: list[str], progress_msg: Message | None = None) -> str:
    # --output-format stream-json даёт реальный стриминг событий построчно
    streaming_args = [args[0], "--output-format", "stream-json", "--verbose"] + args[1:]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    proc = await asyncio.create_subprocess_exec(
        *streaming_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(pathlib.Path(__file__).parent),
        env=env,
    )
    progress_text = ""
    final_result = ""
    last_update = time.monotonic()

    deadline = time.monotonic() + CLAUDE_TIMEOUT
    try:
        assert proc.stdout is not None
        while True:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                proc.kill()
                await proc.wait()
                raise asyncio.TimeoutError()
            try:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=min(timeout, 30))
            except asyncio.TimeoutError:
                if time.monotonic() >= deadline:
                    proc.kill()
                    await proc.wait()
                    raise asyncio.TimeoutError()
                continue
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "result":
                final_result = event.get("result", "")
                continue

            chunk = _extract_progress(event)
            if chunk:
                progress_text += chunk

            if progress_msg is not None and progress_text.strip():
                now = time.monotonic()
                if now - last_update >= STREAM_UPDATE_INTERVAL:
                    preview = progress_text.strip()[-1500:]
                    try:
                        await progress_msg.edit_text(f"⏳ Думаю...\n\n{preview}", reply_markup=CANCEL_KB)
                    except Exception:
                        pass
                    last_update = now

        await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise

    result = final_result.strip() or progress_text.strip()
    if not result:
        assert proc.stderr is not None
        stderr = await proc.stderr.read()
        result = f"Ошибка: {stderr.decode('utf-8').strip()}" if stderr else "Нет ответа"
    return result


async def run_claude(prompt: str, progress_msg: Message | None = None) -> str:
    full_prompt = build_prompt(prompt)
    return await _run_claude_streaming(
        [CLAUDE_PATH, "--print", "--dangerously-skip-permissions", full_prompt],
        progress_msg=progress_msg,
    )


async def run_claude_vision(prompt: str, image_path: str, progress_msg: Message | None = None) -> str:
    full_prompt = build_prompt(
        f"Изображение сохранено в файл: {image_path}\nПрочитай этот файл и выполни задание.\n{prompt}"
    )
    return await _run_claude_streaming(
        [CLAUDE_PATH, "--print", "--dangerously-skip-permissions", full_prompt],
        progress_msg=progress_msg,
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

        await msg.edit_text(f"🎙 «{text}»\n\n⏳ Думаю...", reply_markup=CANCEL_KB)

        response = await run_claude_cancellable(run_claude(text, progress_msg=msg), message.chat_id)
        add_to_history(text, response)
        logger.info("Ответ claude: %s", response[:200])
        await send_response(msg, response, prefix=f"🎙 «{text}»\n\n✅ ")

    except asyncio.CancelledError:
        pass
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

        await msg.edit_text("⏳ Думаю...", reply_markup=CANCEL_KB)
        response = await run_claude_cancellable(run_claude_vision(caption, image_path=tmp_path, progress_msg=msg), message.chat_id)
        add_to_history(f"[фото] {caption}", response)
        logger.info("Ответ claude: %s", response[:200])
        await send_response(msg, response)

    except asyncio.CancelledError:
        pass
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

        await msg.edit_text("⏳ Думаю...", reply_markup=CANCEL_KB)
        response = await run_claude_cancellable(run_claude_vision(caption, image_path=frame_path, progress_msg=msg), message.chat_id)
        add_to_history(f"[видео] {caption}", response)
        logger.info("Ответ claude: %s", response[:200])
        await send_response(msg, response)

    except asyncio.CancelledError:
        pass
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

    msg = await message.reply_text("⏳ Думаю...", reply_markup=CANCEL_KB)

    try:
        response = await run_claude_cancellable(run_claude(text, progress_msg=msg), message.chat_id)
        add_to_history(text, response)
        await send_response(msg, response)

    except asyncio.CancelledError:
        pass
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
    msg = await message.reply_text("⏳ Думаю...", reply_markup=CANCEL_KB)

    try:
        response = await run_claude_cancellable(run_claude(content, progress_msg=msg), message.chat_id)
        add_to_history(content, response)
        await send_response(msg, response)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        await msg.edit_text("⏱ Превышено время ожидания.")
    except Exception as exc:
        logger.exception("Ошибка обработки сообщения")
        await msg.edit_text(f"❌ Ошибка: {exc}")


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    user = update.effective_user
    if user is None or user.id != ALLOWED_USER_ID:
        return

    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        return

    task = active_tasks.get(chat_id)
    if task and not task.done():
        task.cancel()
        try:
            await query.edit_message_text("🚫 Запрос отменён.")
        except Exception:
            pass
    else:
        try:
            await query.edit_message_text("⚠️ Нет активного запроса.")
        except Exception:
            pass


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = require_user(update)
    if user.id != ALLOWED_USER_ID:
        return

    message = require_message(update)
    document = message.document
    if document is None:
        return

    logger.info("Документ от %s (%s): %s", user.id, user.username, document.file_name)
    msg = await message.reply_text("📎 Скачиваю файл...")

    file_name = document.file_name or "file"
    ext = os.path.splitext(file_name)[1].lower()
    caption = message.caption or "Прочитай файл и выполни задание если есть, иначе кратко опиши содержимое."

    tg_file = await context.bot.get_file(document.file_id)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await tg_file.download_to_drive(tmp_path)

        await msg.edit_text("⏳ Думаю...", reply_markup=CANCEL_KB)
        if ext in IMAGE_EXTENSIONS:
            response = await run_claude_cancellable(run_claude_vision(caption, image_path=tmp_path, progress_msg=msg), message.chat_id)
        else:
            prompt = f"Файл «{file_name}» сохранён в {tmp_path}. {caption}"
            response = await run_claude_cancellable(run_claude(prompt, progress_msg=msg), message.chat_id)

        add_to_history(f"[файл: {file_name}] {caption}", response)
        logger.info("Ответ claude: %s", response[:200])
        await send_response(msg, response)

    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        await msg.edit_text("⏱ Превышено время ожидания.")
    except Exception as exc:
        logger.exception("Ошибка обработки документа")
        await msg.edit_text(f"❌ Ошибка: {exc}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


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
    #
    # ВАЖНО: не используем asyncio.wait_for() — в Python 3.10 + anyio/httpx
    # отмена coroutine через wait_for оставляет event loop в сломанном состоянии.
    # Таймауты httpx (read_timeout=90s) достаточны и работают корректно.
    # Внешний cron-watchdog перезапустит процесс если event loop всё же зависнет.
    while True:
        await asyncio.sleep(TELEGRAM_HEALTHCHECK_INTERVAL)
        try:
            await app.bot.get_me()
            mark_telegram_ok()
            _sd_notify("WATCHDOG=1")
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

        # Проверяем что polling не завис: getUpdates должен отвечать регулярно.
        # get_me() работает даже при зависшем polling (разные httpx-клиенты),
        # поэтому нужна отдельная проверка.
        polling_stuck_for = int(time.monotonic() - last_get_updates_ok)
        if polling_stuck_for >= POLLING_MAX_UNHEALTHY_SECONDS:
            logger.error(
                "Polling завис: нет успешного getUpdates уже %sс, перезапуск",
                polling_stuck_for,
            )
            os._exit(1)


async def post_init(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    # Сначала проверяем, что Telegram доступен на старте, и только потом
    # запускаем фоновый watchdog.
    await app.bot.get_me()
    mark_telegram_ok()
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "Информация о боте"),
        BotCommand("new", "Начать новый разговор (очистить историю)"),
    ])

    _sd_notify("READY=1")
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


def _build_get_updates_request() -> Any:
    """Создаёт HTTPXRequest для getUpdates с отслеживанием успешных ответов.

    PTB использует отдельный httpx-клиент для getUpdates (не тот, что для get_me и пр.),
    поэтому get_me() работает даже когда polling завис. Вешаем event hook на ответ:
    каждый успешный getUpdates обновляет last_get_updates_ok.
    При зависании цикла TimedOut этот хук не вызывается → watchdog обнаруживает проблему.
    """
    import httpx
    from telegram.request import HTTPXRequest

    async def _on_response(response: httpx.Response) -> None:
        global last_get_updates_ok
        if b"getUpdates" in bytes(str(response.request.url), "utf-8"):
            last_get_updates_ok = time.monotonic()

    return HTTPXRequest(
        connection_pool_size=1,
        read_timeout=15,
        connect_timeout=15,
        httpx_kwargs={"event_hooks": {"response": [_on_response]}},
    )


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
        .get_updates_request(_build_get_updates_request())
        .concurrent_updates(False)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("new", handle_new))
    app.add_handler(CallbackQueryHandler(handle_cancel, pattern=f"^{CANCEL_CALLBACK}$"))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.ALL, handle_any))

    logger.info("Бот запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
