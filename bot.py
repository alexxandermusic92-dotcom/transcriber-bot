# -*- coding: utf-8 -*-
"""
Telegram-бот транскрибатор.
Принимает ссылку или видеофайл → транскрибирует → отвечает текстом + сохраняет .txt на рабочий стол.
"""
import os
import re
import tempfile
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# Импортируем логику транскрипции из основного модуля
from main import download_audio, extract_audio, split_audio, transcribe_all, save_txt

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN   = "6141003696:AAEnTsA4clcXN7C2jJfIfn-KbKbirNQRi5g"
DESKTOP     = os.path.join(os.path.expanduser("~"), "Desktop")
MAX_TG_MSG  = 4000   # Telegram лимит ~4096 символов на сообщение

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


# ===================== ХЭНДЛЕРЫ =====================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я транскрибирую видео.\n\n"
        "Отправь мне:\n"
        "🔗 Ссылку на видео (YouTube, Instagram, TikTok, любой сайт)\n"
        "📎 Видеофайл прямым файлом\n\n"
        "Я транскрибирую и пришлю текст, а также сохраню .txt на рабочий стол компа."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Как пользоваться:\n\n"
        "• Просто скинь ссылку — YouTube, Instagram, TikTok, ВКонтакте и т.д.\n"
        "• Или прикрепи видеофайл (mp4, mkv, mov...)\n\n"
        "Текст придёт прямо сюда и сохранится на рабочий стол компа."
    )


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем текстовое сообщение со ссылкой."""
    text = update.message.text.strip()

    # Проверяем что это ссылка
    if not re.match(r'https?://', text):
        await update.message.reply_text("Отправь ссылку или видеофайл 🎬")
        return

    msg = await update.message.reply_text("⏳ Скачиваю видео...")
    source_name = re.sub(r'https?://(www\.)?', '', text)[:60].replace("/", "_")

    await _process(update, ctx, msg, file_path=None, url=text, source_name=source_name)


async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем видеофайл или документ."""
    # Может прийти как video или document
    file_obj = update.message.video or update.message.document
    if not file_obj:
        return

    file_name = getattr(file_obj, "file_name", None) or "video.mp4"
    source_name = Path(file_name).stem

    msg = await update.message.reply_text("⏳ Скачиваю файл...")

    with tempfile.TemporaryDirectory() as tmp:
        tg_file = await ctx.bot.get_file(file_obj.file_id)
        local_path = os.path.join(tmp, file_name)
        await tg_file.download_to_drive(local_path)
        await msg.edit_text("⏳ Файл получен, транскрибирую...")
        await _process(update, ctx, msg, file_path=local_path, url=None, source_name=source_name)


async def _process(update, ctx, msg, file_path, url, source_name):
    """Общая логика: аудио → транскрипция → ответ."""
    steps = []

    def status_cb(text):
        steps.append(text)
        log.info(text)

    def progress_cb(val):
        pass  # в боте прогресс-бар не нужен

    try:
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Получаем аудио
            await msg.edit_text("⏳ Извлекаю аудио...")
            if url:
                audio = download_audio(url, tmp, status_cb)
            else:
                audio = extract_audio(file_path, tmp, status_cb)

            # 2. Нарезаем если нужно
            await msg.edit_text("⏳ Подготавливаю к транскрипции...")
            chunks = split_audio(audio, tmp, status_cb)

            n = len(chunks)
            note = f" (разбито на {n} частей)" if n > 1 else ""
            await msg.edit_text(f"🎙 Транскрибирую{note}...")

            # 3. Транскрибируем
            text = transcribe_all(chunks, status_cb, progress_cb)

            # 4. Сохраняем на рабочий стол
            txt_path = save_txt(text, source_name)

            # 5. Отправляем текст в Telegram (режем если длинный)
            await msg.edit_text(f"✅ Готово! Сохранено: {os.path.basename(txt_path)}")

            chunks_out = _split_message(text)
            for i, chunk in enumerate(chunks_out):
                header = f"📄 *Транскрипция* ({i+1}/{len(chunks_out)}):\n\n" if len(chunks_out) > 1 else ""
                await update.message.reply_text(
                    header + chunk,
                    parse_mode="Markdown" if header else None
                )

    except Exception as e:
        log.error(f"Ошибка: {e}")
        await msg.edit_text(f"❌ Ошибка:\n{str(e)[:300]}")


def _split_message(text: str, limit: int = MAX_TG_MSG) -> list:
    """Режем длинный текст на куски для Telegram."""
    if len(text) <= limit:
        return [text]

    parts = []
    # Пробуем резать по абзацам
    paragraphs = text.split("\n\n")
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= limit:
            current += ("\n\n" if current else "") + para
        else:
            if current:
                parts.append(current)
            # Если сам абзац больше лимита — режем по предложениям
            if len(para) > limit:
                words = para.split()
                line = ""
                for w in words:
                    if len(line) + len(w) + 1 <= limit:
                        line += (" " if line else "") + w
                    else:
                        parts.append(line)
                        line = w
                if line:
                    current = line
            else:
                current = para

    if current:
        parts.append(current)

    return parts or [text[:limit]]


# ===================== ЗАПУСК =====================

def main():
    print("🤖 Бот запущен. Ожидаю сообщения...")
    print(f"   Сохранение файлов: {DESKTOP}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))

    # Видеофайлы и документы
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    # Текстовые сообщения (ссылки)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
