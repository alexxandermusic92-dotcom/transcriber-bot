# -*- coding: utf-8 -*-
"""
bot_cloud.py — облачный бот для Railway.
Использует Flask + raw Telegram API (requests) + SQLite + ThreadPoolExecutor.
"""

import os
import re
import json
import time
import sqlite3
import logging
import tempfile
import subprocess
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Flask, request, jsonify, abort
from groq import Groq

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ===================== CONFIG =====================
BOT_TOKEN        = os.environ.get("BOT_TOKEN",        "6141003696:AAEnTsA4clcXN7C2jJfIfn-KbKbirNQRi5g")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY",     "")
SYNC_SECRET      = os.environ.get("SYNC_SECRET",      "vcs_7kRp9xMnQw3T")
PORT             = int(os.environ.get("PORT",          "8080"))
RAILWAY_DOMAIN   = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")

TG_API           = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE_API      = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
DB_PATH          = "/tmp/transcriptions.db"

CHUNK_MINUTES    = 9
SAMPLE_RATE      = 16000
PAUSE_THRESHOLD  = 2.0

groq_client      = Groq(api_key=GROQ_API_KEY)
executor         = ThreadPoolExecutor(max_workers=2)
app              = Flask(__name__)
db_lock          = threading.Lock()   # защита от конкурентных записей в SQLite

# ===================== DATABASE =====================

def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcriptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER,
                filename    TEXT,
                content     TEXT,
                source      TEXT,
                created_at  TEXT,
                downloaded  INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    log.info("БД инициализирована: %s", DB_PATH)


def db_insert(chat_id: int, filename: str, content: str, source: str) -> int:
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        with db_connect() as conn:
            cur = conn.execute(
                "INSERT INTO transcriptions (chat_id, filename, content, source, created_at, downloaded) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (chat_id, filename, content, source, created_at)
            )
            conn.commit()
            return cur.lastrowid


def db_pending():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, filename, source, created_at FROM transcriptions WHERE downloaded=0 ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def db_get_content(item_id: int):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT filename, content FROM transcriptions WHERE id=?", (item_id,)
        ).fetchone()
    return dict(row) if row else None


def db_mark_downloaded(item_id: int):
    with db_connect() as conn:
        conn.execute("UPDATE transcriptions SET downloaded=1 WHERE id=?", (item_id,))
        conn.commit()


# ===================== TELEGRAM HELPERS =====================

def tg_send(chat_id: int, text: str):
    """Отправляем сообщение пользователю."""
    try:
        r = requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
        }, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.error("tg_send error: %s", e)


def tg_get_file_path(file_id: str) -> str:
    """Получаем путь к файлу на серверах Telegram."""
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data["result"]["file_path"]


def tg_download_file(file_path: str, dest: str):
    """Скачиваем файл с серверов Telegram."""
    url = f"{TG_FILE_API}/{file_path}"
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)


def set_webhook():
    """Устанавливаем Webhook если есть RAILWAY_PUBLIC_DOMAIN."""
    if not RAILWAY_DOMAIN:
        log.warning("RAILWAY_PUBLIC_DOMAIN не задан — webhook не установлен.")
        return
    webhook_url = f"https://{RAILWAY_DOMAIN}/webhook"
    try:
        r = requests.post(f"{TG_API}/setWebhook", json={"url": webhook_url}, timeout=15)
        data = r.json()
        if data.get("ok"):
            log.info("Webhook установлен: %s", webhook_url)
        else:
            log.error("Ошибка установки webhook: %s", data)
    except Exception as e:
        log.error("set_webhook error: %s", e)


# ===================== TRANSCRIPTION FUNCTIONS =====================

def extract_audio_cloud(video_path: str, out_dir: str) -> str:
    """Конвертируем видео → mp3 16kHz моно через ffmpeg."""
    out_path = os.path.join(out_dir, "audio.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-q:a", "5",
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"ffmpeg не смог извлечь аудио: {result.stderr[-400:]}")
    return out_path


def download_audio_cloud(url: str, out_dir: str) -> str:
    """Скачиваем аудио из URL через yt-dlp → mp3."""
    import yt_dlp
    out_template = os.path.join(out_dir, "audio.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }],
        "postprocessor_args": ["-ar", str(SAMPLE_RATE), "-ac", "1"],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    out_path = os.path.join(out_dir, "audio.mp3")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("yt-dlp не смог скачать аудио. Попробуй скинуть файл напрямую.")
    return out_path


def get_duration_cloud(path: str) -> float:
    """Получаем длину аудио через ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True
    )
    try:
        info = json.loads(result.stdout)
        for stream in info.get("streams", []):
            if "duration" in stream:
                return float(stream["duration"])
    except Exception:
        pass
    return 0.0


def split_audio_cloud(audio_path: str, out_dir: str) -> list:
    """Режем аудио на куски по CHUNK_MINUTES минут."""
    duration = get_duration_cloud(audio_path)
    chunk_sec = CHUNK_MINUTES * 60
    if duration <= chunk_sec:
        return [audio_path]

    total_chunks = int(duration // chunk_sec) + (1 if duration % chunk_sec > 0 else 0)
    chunks = []
    for i in range(total_chunks):
        start = i * chunk_sec
        chunk_path = os.path.join(out_dir, f"chunk_{i:03d}.mp3")
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-ss", str(start), "-t", str(chunk_sec),
            "-c", "copy", chunk_path
        ]
        subprocess.run(cmd, capture_output=True)
        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 1000:
            chunks.append(chunk_path)
    return chunks


def transcribe_chunk_cloud(chunk_path: str, chunk_idx: int, total: int) -> str:
    """Транскрибируем один кусок через Groq Whisper."""
    log.info("Транскрибирую часть %d/%d: %s", chunk_idx + 1, total, chunk_path)
    with open(chunk_path, "rb") as f:
        raw = groq_client.audio.transcriptions.create(
            file=(os.path.basename(chunk_path), f),
            model="whisper-large-v3",
            response_format="verbose_json",
        )
    try:
        segments = raw.segments or []
        if not segments:
            return raw.text.strip()

        paragraphs = []
        current = []
        for i, seg in enumerate(segments):
            text = seg.text.strip()
            if text:
                current.append(text)
            if i + 1 < len(segments):
                gap = segments[i + 1].start - seg.end
                if gap >= PAUSE_THRESHOLD and current:
                    paragraphs.append(" ".join(current))
                    current = []
        if current:
            paragraphs.append(" ".join(current))
        return "\n\n".join(paragraphs)
    except Exception:
        return raw.text.strip()


def transcribe_all_cloud(chunks: list) -> str:
    """Транскрибируем все куски и склеиваем."""
    total = len(chunks)
    parts = []
    for i, chunk in enumerate(chunks):
        text = transcribe_chunk_cloud(chunk, i, total)
        parts.append(text)
    return "\n\n".join(parts)


def make_filename(source: str) -> str:
    """Создаём безопасное имя файла из source (URL или имя файла)."""
    name = re.sub(r'https?://(www\.)?', '', source)
    name = re.sub(r'[<>:"/\\|?*\s]+', '_', name)
    name = name.strip('_')[:60]
    return name + ".txt"


# ===================== CORE BOT LOGIC =====================

def process_url(chat_id: int, url: str):
    """Фоновая задача: скачать и транскрибировать URL."""
    log.info("Обрабатываю URL для chat_id=%d: %s", chat_id, url)
    tg_send(chat_id, "⏳ Скачиваю аудио...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            audio = download_audio_cloud(url, tmp)
            tg_send(chat_id, "🎙 Транскрибирую...")
            chunks = split_audio_cloud(audio, tmp)
            text = transcribe_all_cloud(chunks)

        if not text or not text.strip():
            tg_send(chat_id, "⚠️ Транскрипция пустая — возможно видео без речи или закрытый контент.")
            return

        filename = make_filename(url)
        item_id = db_insert(chat_id, filename, text, url)
        log.info("Сохранено в БД id=%d filename=%s", item_id, filename)

        chars = len(text)
        words = len(text.split())
        if chars < 3500:
            tg_send(chat_id, f"📝 Транскрипция:\n\n{text}")
        else:
            tg_send(chat_id, f"📝 Начало транскрипции:\n\n{text[:3000]}\n\n... [обрезано, полный текст на компе]")

        tg_send(chat_id,
            f"✅ Готово! Файл сохранён в облаке.\n"
            f"📊 {words} слов / {chars} символов\n"
            f"💾 {filename}\n\n"
            f"Открой Транскрибатор на компе — вкладка Облако — скачай файл"
        )

    except Exception as e:
        err = str(e)
        log.error("process_url error: %s", err)
        if "login" in err.lower() or "private" in err.lower() or "age" in err.lower():
            tg_send(chat_id, "❌ Этот контент закрыт или требует авторизации.\nПопробуй скачать видео на телефон и отправить файлом.")
        else:
            tg_send(chat_id, f"❌ Ошибка:\n{err[:250]}\n\nПопробуй скинуть видеофайл напрямую.")


def process_file(chat_id: int, file_id: str, original_name: str):
    """Фоновая задача: скачать файл Telegram и транскрибировать."""
    log.info("Обрабатываю файл для chat_id=%d file_id=%s", chat_id, file_id)
    try:
        file_path_tg = tg_get_file_path(file_id)
        ext = os.path.splitext(file_path_tg)[1] or ".mp4"

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, f"input{ext}")
            tg_download_file(file_path_tg, dest)
            audio = extract_audio_cloud(dest, tmp)
            chunks = split_audio_cloud(audio, tmp)
            text = transcribe_all_cloud(chunks)

        source_name = original_name or os.path.basename(file_path_tg)
        filename = make_filename(source_name)
        item_id = db_insert(chat_id, filename, text, source_name)
        log.info("Сохранено в БД id=%d filename=%s", item_id, filename)

        chars = len(text)
        words = len(text.split())
        if chars < 3500:
            tg_send(chat_id, f"📝 Транскрипция:\n\n{text}")
        else:
            tg_send(chat_id, f"📝 Начало транскрипции:\n\n{text[:3000]}\n\n... [обрезано, полный текст на компе]")

        tg_send(chat_id,
            f"✅ Готово! Файл сохранён в облаке.\n"
            f"📊 {words} слов, {chars} символов\n"
            f"💾 Файл: {filename}\n\n"
            f"Открой Транскрибатор на компе → вкладка ☁️ Облако → скачай файл"
        )

    except Exception as e:
        log.error("process_file error: %s", e)
        tg_send(chat_id, f"❌ Ошибка при транскрипции:\n{str(e)[:300]}\n\nПопробуй другую ссылку или скинь файл напрямую.")


def handle_update(update: dict):
    """Обрабатываем одно Telegram update."""
    message = update.get("message") or update.get("channel_post")
    if not message:
        return

    chat_id = message["chat"]["id"]
    text_msg = message.get("text", "")
    caption = message.get("caption", "")

    # Видео или документ
    video = message.get("video")
    document = message.get("document")
    video_note = message.get("video_note")
    audio = message.get("audio")
    voice = message.get("voice")

    if message.get("sticker") or message.get("photo"):
        tg_send(chat_id, "Отправь ссылку или видеофайл 🎬")
        return

    if video:
        file_id = video["file_id"]
        fname = f"video_{chat_id}.mp4"
        tg_send(chat_id, "⏳ Получил видео, транскрибирую...")
        executor.submit(process_file, chat_id, file_id, fname)
        return

    if document:
        mime = document.get("mime_type", "")
        fname = document.get("file_name", "document")
        # Проверяем что это видео или аудио
        if mime.startswith("video/") or mime.startswith("audio/") or \
                any(fname.lower().endswith(ext) for ext in
                    [".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v",
                     ".wmv", ".mp3", ".m4a", ".ogg", ".wav", ".aac"]):
            file_id = document["file_id"]
            tg_send(chat_id, f"⏳ Получил файл {fname}, транскрибирую...")
            executor.submit(process_file, chat_id, file_id, fname)
        else:
            tg_send(chat_id, "Отправь ссылку или видеофайл 🎬")
        return

    if voice:
        file_id = voice["file_id"]
        tg_send(chat_id, "⏳ Получил голосовое, транскрибирую...")
        executor.submit(process_file, chat_id, file_id, f"voice_{chat_id}.ogg")
        return

    if audio:
        file_id = audio["file_id"]
        fname = audio.get("file_name", f"audio_{chat_id}.mp3")
        tg_send(chat_id, f"⏳ Получил аудио, транскрибирую...")
        executor.submit(process_file, chat_id, file_id, fname)
        return

    if video_note:
        file_id = video_note["file_id"]
        tg_send(chat_id, "⏳ Получил кружок, транскрибирую...")
        executor.submit(process_file, chat_id, file_id, f"videonote_{chat_id}.mp4")
        return

    # Текст
    content = text_msg or caption

    if content == "/start":
        help_text = (
            "👋 Привет! Я транскрибирую видео и аудио.\n\n"
            "Отправь мне:\n"
            "• Ссылку на YouTube, Instagram, TikTok и другие сайты\n"
            "• Видеофайл или аудиофайл прямо в чат\n"
            "• Голосовое сообщение\n\n"
            "Я транскрибирую и сохраню результат. "
            "Скачать файл можно в Транскрибаторе на компьютере во вкладке ☁️ Облако."
        )
        tg_send(chat_id, help_text)
        return

    if content and "http" in content:
        # Ищем URL в тексте
        urls = re.findall(r'https?://[^\s]+', content)
        if urls:
            url = urls[0]
            tg_send(chat_id, f"📨 Получил ссылку!\n{url[:80]}...\nОбрабатываю — подожди...")
            executor.submit(process_url, chat_id, url)
            return

    tg_send(chat_id, "Отправь ссылку или видеофайл 🎬")


# ===================== FLASK ENDPOINTS =====================

def check_secret():
    """Проверяем SYNC_SECRET из query string."""
    secret = request.args.get("secret", "")
    if secret != SYNC_SECRET:
        abort(403, description="Invalid secret")


@app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram webhook endpoint."""
    try:
        update = request.get_json(force=True)
        if update:
            threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    except Exception as e:
        log.error("webhook error: %s", e)
    return jsonify({"ok": True})


@app.route("/pending", methods=["GET"])
def pending():
    """Список не скачанных транскрипций."""
    check_secret()
    try:
        items = db_pending()
        return jsonify(items)
    except Exception as e:
        log.error("pending error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/content/<int:item_id>", methods=["GET"])
def content(item_id: int):
    """Получаем содержимое транскрипции по id."""
    check_secret()
    try:
        row = db_get_content(item_id)
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(row)
    except Exception as e:
        log.error("content error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/done/<int:item_id>", methods=["POST"])
def done(item_id: int):
    """Помечаем запись как скачанную."""
    check_secret()
    try:
        db_mark_downloaded(item_id)
        return jsonify({"ok": True})
    except Exception as e:
        log.error("done error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/debug", methods=["GET"])
def debug():
    """Показываем состояние бота + пробуем отправить сообщение."""
    check_secret()
    chat_id = request.args.get("chat_id")
    result  = {}

    # Все записи в БД
    try:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT id, chat_id, filename, created_at, downloaded FROM transcriptions ORDER BY id DESC LIMIT 10"
            ).fetchall()
        result["db_rows"] = [dict(r) for r in rows]
    except Exception as e:
        result["db_error"] = str(e)

    # Показываем какой токен используется
    result["token_prefix"] = BOT_TOKEN[:20] + "..."
    result["tg_api_url"]   = TG_API[:50]

    # Тест getMe
    try:
        r_me = requests.get(f"{TG_API}/getMe", timeout=15)
        result["getMe"] = r_me.json()
    except Exception as e:
        result["getMe_error"] = str(e)

    # Тест отправки сообщения
    if chat_id:
        try:
            r = requests.post(f"{TG_API}/sendMessage",
                              json={"chat_id": int(chat_id), "text": "✅ Тест: бот работает!"},
                              timeout=15)
            result["tg_send_status"] = r.status_code
            result["tg_send_response"] = r.json()
        except Exception as e:
            result["tg_send_error"] = str(e)

    return jsonify(result)


# ===================== ENTRY POINT =====================

if __name__ == "__main__":
    db_init()
    # Устанавливаем webhook в отдельном потоке чтобы не блокировать старт
    threading.Thread(target=set_webhook, daemon=True).start()
    log.info("Запускаю Flask на порту %d", PORT)
    app.run(host="0.0.0.0", port=PORT)
