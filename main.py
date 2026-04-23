# -*- coding: utf-8 -*-
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from tkinterdnd2 import DND_FILES, TkinterDnD
import threading
import os
import tempfile
import subprocess
import json
import re
import time
import requests
from pathlib import Path
from groq import Groq
import yt_dlp

# ===================== НАСТРОЙКИ =====================
def _load_groq_key():
    """Читаем ключ из локального файла (не попадает в GitHub)."""
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys.json")
    try:
        with open(key_file, encoding="utf-8") as f:
            return json.load(f).get("groq", "")
    except Exception:
        return ""

GROQ_API_KEY     = _load_groq_key()
CHUNK_MINUTES    = 9
PAUSE_THRESHOLD  = 2.0
SAMPLE_RATE      = 16000
DESKTOP          = os.path.join(os.path.expanduser("~"), "Desktop")
SYNC_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_config.json")

groq_client = Groq(api_key=GROQ_API_KEY)


# ===================== АУДИО / ВИДЕО =====================

def extract_audio(video_path: str, out_dir: str, status_cb) -> str:
    status_cb("Извлекаю аудио...")
    out_path = os.path.join(out_dir, "audio.mp3")
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-q:a", "5", out_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"ffmpeg не смог извлечь аудио:\n{result.stderr[-600:]}")
    return out_path


def download_audio(url: str, out_dir: str, status_cb) -> str:
    status_cb("Скачиваю аудио с сайта...")
    out_template = os.path.join(out_dir, "audio.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "quiet": True, "no_warnings": True,
        "postprocessors": [{"key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3", "preferredquality": "64"}],
        "postprocessor_args": ["-ar", str(SAMPLE_RATE), "-ac", "1"],
    }
    # Для Instagram
    if "instagram.com" in url or "tiktok.com" in url:
        import sys
        app_dir = os.path.dirname(os.path.abspath(__file__))

        # Читаем session_id
        session_id = ""
        ig_creds_path = os.path.join(app_dir, "ig_creds.json")
        if os.path.exists(ig_creds_path):
            try:
                with open(ig_creds_path, encoding="utf-8") as f:
                    session_id = json.load(f).get("session_id", "")
            except Exception:
                pass

        if "instagram.com" in url:
            # Проверяем что instaloader установлен
            try:
                import instaloader as _il_test  # noqa
            except ImportError:
                status_cb("Устанавливаю instaloader...")
                r = subprocess.run([sys.executable, "-m", "pip", "install",
                                    "instaloader", "-q"], capture_output=True)
                raise RuntimeError(
                    "Установил instaloader. Перезапусти приложение и попробуй снова.")

            import instaloader
            m = re.search(r'/(p|reel|tv)/([A-Za-z0-9_-]+)', url)
            if not m:
                raise RuntimeError("Не удалось распознать ссылку Instagram")
            shortcode = m.group(2)
            status_cb(f"Скачиваю через instaloader ({shortcode})...")

            L = instaloader.Instaloader(
                download_videos=True,
                download_video_thumbnails=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True,
                dirname_pattern=out_dir,
                filename_pattern="{shortcode}",
            )
            if session_id:
                L.context._session.cookies.set(
                    "sessionid", session_id, domain=".instagram.com")

            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
                L.download_post(post, target=out_dir)
            except Exception as e:
                raise RuntimeError(f"instaloader ошибка: {e}")

            # Ищем mp4 рекурсивно (instaloader может создать подпапку)
            mp4_found = None
            for root, _, files in os.walk(out_dir):
                for f in sorted(files):
                    if f.endswith(".mp4"):
                        candidate = os.path.join(root, f)
                        if os.path.getsize(candidate) > 10_000:
                            mp4_found = candidate
                            break
                if mp4_found:
                    break

            if not mp4_found:
                raise RuntimeError(
                    "instaloader не нашёл видео.\n"
                    "Возможно пост приватный или удалён.\n"
                    "Убедись что sessionid актуальный (скопирован сегодня).")

            status_cb("Конвертирую в аудио...")
            out_mp3 = os.path.join(out_dir, "audio.mp3")
            subprocess.run(
                ["ffmpeg", "-y", "-i", mp4_found, "-vn", "-ac", "1",
                 "-ar", str(SAMPLE_RATE), "-q:a", "5", out_mp3],
                capture_output=True)
            if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 0:
                return out_mp3
            raise RuntimeError("ffmpeg не смог конвертировать скачанное видео")

        # TikTok — yt-dlp справляется нормально
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        out_path = os.path.join(out_dir, "audio.mp3")
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        raise RuntimeError("yt-dlp не смог скачать TikTok")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    out_path = os.path.join(out_dir, "audio.mp3")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("yt-dlp не смог скачать аудио с этого URL.")
    return out_path


def get_duration(audio_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", audio_path],
        capture_output=True, text=True)
    info = json.loads(result.stdout)
    for stream in info.get("streams", []):
        if "duration" in stream:
            return float(stream["duration"])
    return 0.0


def split_audio(audio_path: str, out_dir: str, status_cb) -> list:
    duration  = get_duration(audio_path)
    chunk_sec = CHUNK_MINUTES * 60
    if duration <= chunk_sec:
        return [audio_path]
    total = int(duration // chunk_sec) + (1 if duration % chunk_sec > 0 else 0)
    status_cb(f"Нарезаю на {total} частей...")
    chunks = []
    for i in range(total):
        cp = os.path.join(out_dir, f"chunk_{i:03d}.mp3")
        subprocess.run(["ffmpeg", "-y", "-i", audio_path,
                        "-ss", str(i * chunk_sec), "-t", str(chunk_sec),
                        "-c", "copy", cp], capture_output=True)
        if os.path.exists(cp) and os.path.getsize(cp) > 1000:
            chunks.append(cp)
    return chunks


# ===================== ТРАНСКРИПЦИЯ =====================

def transcribe_chunk(chunk_path: str, chunk_idx: int, total: int, status_cb) -> str:
    status_cb(f"Транскрибирую часть {chunk_idx + 1} из {total}...")
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
        paragraphs, current = [], []
        for i, seg in enumerate(segments):
            if seg.text.strip():
                current.append(seg.text.strip())
            if i + 1 < len(segments):
                if (segments[i+1].start - seg.end) >= PAUSE_THRESHOLD and current:
                    paragraphs.append(" ".join(current))
                    current = []
        if current:
            paragraphs.append(" ".join(current))
        return "\n\n".join(paragraphs)
    except Exception:
        return raw.text.strip()


def transcribe_all(chunks: list, status_cb, progress_cb) -> str:
    total = len(chunks)
    parts = []
    for i, chunk in enumerate(chunks):
        parts.append(transcribe_chunk(chunk, i, total, status_cb))
        progress_cb(int((i + 1) / total * 100))
    return "\n\n".join(parts)


def save_txt(text: str, source_name: str) -> str:
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', source_name)[:80]
    txt_path  = os.path.join(DESKTOP, f"{safe_name}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
    return txt_path


# ===================== ЦВЕТА =====================
BG      = "#1e1e2e"
BG2     = "#181825"
BG3     = "#313244"
ACCENT  = "#cba6f7"
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
TEXT    = "#cdd6f4"
SUBTEXT = "#6c7086"
BORDER  = "#45475a"


# ===================== ПРИЛОЖЕНИЕ =====================

class TranscriberApp:

    def __init__(self):
        self.root = TkinterDnD.Tk()
        self.root.title("Транскрибатор")
        self.root.geometry("720x720")
        self.root.configure(bg=BG)
        self.root.resizable(True, True)
        self.root.minsize(520, 520)

        self._cloud_cfg   = self._cloud_load_config()
        self._cloud_items = []   # список {id, filename, source, created_at, frame}

        self._build_ui()
        self._set_icon()

    # ──────── ИКОНКА ────────

    def _set_icon(self):
        ico = os.path.join(os.path.dirname(__file__), "transcriber.ico")
        if not os.path.exists(ico):
            ico = self._make_icon()
        if ico:
            self.root.iconbitmap(ico)

    def _make_icon(self):
        try:
            from PIL import Image, ImageDraw
            size = 256
            img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d    = ImageDraw.Draw(img)
            pad  = int(size * 0.04)
            d.ellipse([pad, pad, size-pad, size-pad], fill=(30,30,46,255))
            cx, cy = size//2, size//2
            r = int(size*0.25)
            d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(203,166,247,255))
            r2 = int(size*0.12)
            d.ellipse([cx-r2, cy-r2, cx+r2, cy+r2], fill=(30,30,46,255))
            for offset, alpha in [(0.38,200),(0.48,140),(0.58,80)]:
                ro    = int(size*offset)
                color = (166,227,161,alpha)
                d.arc([cx-ro, cy-ro, cx+ro, cy+ro], start=225, end=315, fill=color, width=max(3,int(size*0.04)))
                d.arc([cx-ro, cy-ro, cx+ro, cy+ro], start=45,  end=135, fill=color, width=max(3,int(size*0.04)))
            ico_path = os.path.join(os.path.dirname(__file__), "transcriber.ico")
            img.save(ico_path, format="ICO", sizes=[(256,256),(64,64),(32,32),(16,16)])
            return ico_path
        except Exception:
            return None

    # ──────── ГЛАВНЫЙ UI ────────

    def _build_ui(self):
        root = self.root

        # ── Заголовок ──
        hdr = tk.Frame(root, bg=BG2, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🎬  Транскрибатор", font=("Segoe UI", 16, "bold"),
                 bg=BG2, fg=ACCENT).pack(side="left", padx=20)
        self.status_lbl = tk.Label(hdr, text="● Готов", font=("Segoe UI", 10),
                                   bg=BG2, fg=GREEN)
        self.status_lbl.pack(side="right", padx=20)

        # ── Notebook (вкладки) ──
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Dark.TNotebook",        background=BG2, borderwidth=0)
        style.configure("Dark.TNotebook.Tab",    background=BG3, foreground=SUBTEXT,
                        padding=[16, 8], font=("Segoe UI", 10))
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", ACCENT)])
        style.configure("Trans.Horizontal.TProgressbar",
                        troughcolor=BG3, background=ACCENT,
                        borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)

        nb = ttk.Notebook(root, style="Dark.TNotebook")
        nb.pack(fill="both", expand=True, padx=0, pady=0)

        # Вкладка 1 — Транскрипция
        tab1 = tk.Frame(nb, bg=BG)
        nb.add(tab1, text="🎬  Транскрипция")
        self._build_transcribe_tab(tab1)

        # Вкладка 2 — Облако
        tab2 = tk.Frame(nb, bg=BG)
        nb.add(tab2, text="☁️  Облако")
        self._build_cloud_tab(tab2)

    # ──────── ВКЛАДКА 1: ТРАНСКРИПЦИЯ ────────

    def _build_transcribe_tab(self, parent):

        # ── URL ──
        url_frame = tk.Frame(parent, bg=BG, pady=8)
        url_frame.pack(fill="x", padx=16)
        tk.Label(url_frame, text="Ссылка на видео:", font=("Segoe UI", 10),
                 bg=BG, fg=SUBTEXT).pack(anchor="w")
        url_row = tk.Frame(url_frame, bg=BG)
        url_row.pack(fill="x", pady=(4,0))
        self.url_var = tk.StringVar()
        url_entry = tk.Entry(url_row, textvariable=self.url_var,
                             font=("Segoe UI", 11), bg=BG3, fg=TEXT,
                             insertbackground=TEXT, relief="flat", bd=0,
                             highlightthickness=1, highlightcolor=ACCENT,
                             highlightbackground=BORDER)
        url_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0,8))
        url_entry.bind("<Return>", lambda e: self._start_from_url())
        self._add_paste_menu(url_entry)
        self._btn(url_row, "▶  Транскрибировать", self._start_from_url,
                  ACCENT, BG2).pack(side="left")

        # ── Instagram Session ID ──
        ig_frame = tk.Frame(parent, bg=BG)
        ig_frame.pack(fill="x", padx=16, pady=(4, 0))

        tk.Label(ig_frame, text="Instagram sessionid:", font=("Segoe UI", 9),
                 bg=BG, fg=SUBTEXT).pack(side="left", padx=(0, 6))

        self.ig_session_var = tk.StringVar()
        ig_session_entry = tk.Entry(ig_frame, textvariable=self.ig_session_var,
                                    font=("Segoe UI", 10), bg=BG3, fg=TEXT,
                                    insertbackground=TEXT, relief="flat", bd=0,
                                    highlightthickness=1, highlightcolor=ACCENT,
                                    highlightbackground=BORDER, width=32, show="●")
        ig_session_entry.pack(side="left", ipady=5, padx=(0, 4))

        self._btn(ig_frame, "💾", self._ig_save_session, GREEN, BG3).pack(side="left", padx=(0, 4))

        self.ig_status_lbl = tk.Label(ig_frame, text="", font=("Segoe UI", 9), bg=BG, fg=SUBTEXT)
        self.ig_status_lbl.pack(side="left")

        self._btn(ig_frame, "?", self._ig_show_help, SUBTEXT, BG3).pack(side="left", padx=(0, 6))
        self._btn(ig_frame, "🍪 cookies.txt", self._pick_cookies, SUBTEXT, BG3).pack(side="right")

        # Загружаем сохранённый session id
        self._ig_load_session(ig_session_entry)

        # ── Разделитель ──
        sep = tk.Frame(parent, bg=BG, pady=6)
        sep.pack(fill="x", padx=16)
        tk.Frame(sep, bg=BORDER, height=1).pack(fill="x", side="left", expand=True, pady=9)
        tk.Label(sep, text="  или  ", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Frame(sep, bg=BORDER, height=1).pack(fill="x", side="left", expand=True, pady=9)

        # ── Файл + Drag & Drop ──
        file_frame = tk.Frame(parent, bg=BG, pady=4)
        file_frame.pack(fill="x", padx=16)
        self._btn(file_frame, "📁  Выбрать файл", self._start_from_file,
                  GREEN, BG2).pack(side="left", padx=(0,10))
        self.drop_zone = tk.Label(file_frame, text="  ➕  Перетащи видео сюда  ",
                                  font=("Segoe UI", 10), bg=BG3, fg=SUBTEXT,
                                  relief="flat", bd=0, padx=14, pady=7, cursor="hand2")
        self.drop_zone.pack(side="left", fill="x", expand=True)
        self.drop_zone.drop_target_register(DND_FILES)
        self.drop_zone.dnd_bind("<<Drop>>",      self._on_drop)
        self.drop_zone.dnd_bind("<<DragEnter>>", self._on_drag_enter)
        self.drop_zone.dnd_bind("<<DragLeave>>", self._on_drag_leave)

        # ── Прогресс ──
        prog_frame = tk.Frame(parent, bg=BG, pady=8)
        prog_frame.pack(fill="x", padx=16)
        self.prog_var = tk.IntVar(value=0)
        self.progressbar = ttk.Progressbar(prog_frame, variable=self.prog_var,
                                           maximum=100,
                                           style="Trans.Horizontal.TProgressbar")
        self.progressbar.pack(fill="x")
        self.step_lbl = tk.Label(prog_frame, text="", font=("Consolas", 9),
                                 bg=BG, fg=SUBTEXT)
        self.step_lbl.pack(anchor="w", pady=(4,0))

        # ── Результат ──
        tk.Label(parent, text="Результат:", font=("Segoe UI", 10),
                 bg=BG, fg=SUBTEXT).pack(anchor="w", padx=16, pady=(8,2))
        self.result_box = scrolledtext.ScrolledText(
            parent, font=("Segoe UI", 11), bg=BG2, fg=TEXT,
            insertbackground=TEXT, relief="flat", bd=0,
            padx=12, pady=10, wrap="word", state="disabled")
        self.result_box.pack(fill="both", expand=True, padx=16, pady=(0,8))

        # ── Кнопки ──
        btn_row = tk.Frame(parent, bg=BG, pady=10)
        btn_row.pack(fill="x", padx=16)
        self._btn(btn_row, "📋  Копировать",    self._copy,    ACCENT, BG3).pack(side="left", padx=(0,8))
        self._btn(btn_row, "💾  Сохранить как...", self._save_as, GREEN,  BG3).pack(side="left")
        self.saved_lbl = tk.Label(btn_row, text="", font=("Segoe UI", 9),
                                  bg=BG, fg=GREEN)
        self.saved_lbl.pack(side="left", padx=12)

    # ──────── ВКЛАДКА 2: ОБЛАКО ────────

    def _build_cloud_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        # ── Шапка ──
        hdr = tk.Frame(parent, bg=BG2, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="☁️  Файлы из облака", font=("Segoe UI", 13, "bold"),
                 bg=BG2, fg=ACCENT).pack(side="left", padx=16)
        self.cloud_status_lbl = tk.Label(hdr, text="", font=("Segoe UI", 9),
                                         bg=BG2, fg=SUBTEXT)
        self.cloud_status_lbl.pack(side="left", padx=8)
        self._btn(hdr, "🔄  Обновить", self._cloud_refresh, TEXT, BG3).pack(side="right", padx=16)

        # ── Список (Canvas + Scrollbar) ──
        list_outer = tk.Frame(parent, bg=BG)
        list_outer.pack(fill="both", expand=True, padx=16, pady=10)

        self.cloud_canvas = tk.Canvas(list_outer, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(list_outer, orient="vertical",
                                 command=self.cloud_canvas.yview)
        self.cloud_canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self.cloud_canvas.pack(side="left", fill="both", expand=True)

        self.cloud_list_frame = tk.Frame(self.cloud_canvas, bg=BG)
        self.cloud_canvas_window = self.cloud_canvas.create_window(
            (0, 0), window=self.cloud_list_frame, anchor="nw")

        self.cloud_list_frame.bind("<Configure>", self._on_cloud_list_resize)
        self.cloud_canvas.bind("<Configure>",     self._on_cloud_canvas_resize)

        # Заглушка
        self.cloud_empty_lbl = tk.Label(self.cloud_list_frame,
                                        text="Нажми 🔄 Обновить, чтобы проверить новые файлы",
                                        font=("Segoe UI", 10), bg=BG, fg=SUBTEXT)
        self.cloud_empty_lbl.pack(pady=40)

        # ── URL настройка ──
        cfg_frame = tk.Frame(parent, bg=BG2, pady=10)
        cfg_frame.pack(fill="x", side="bottom")
        tk.Frame(cfg_frame, bg=BORDER, height=1).pack(fill="x")
        inner = tk.Frame(cfg_frame, bg=BG2)
        inner.pack(fill="x", padx=16, pady=(8,0))
        tk.Label(inner, text="Railway URL:", font=("Segoe UI", 9),
                 bg=BG2, fg=SUBTEXT).pack(side="left", padx=(0,8))
        self.cloud_url_var = tk.StringVar(value=self._cloud_cfg.get("railway_url", ""))
        url_entry = tk.Entry(inner, textvariable=self.cloud_url_var,
                             font=("Segoe UI", 10), bg=BG3, fg=TEXT,
                             insertbackground=TEXT, relief="flat", bd=0,
                             highlightthickness=1, highlightcolor=ACCENT,
                             highlightbackground=BORDER, width=40)
        url_entry.pack(side="left", ipady=5, padx=(0,8))
        self._btn(inner, "Сохранить", self._cloud_save_config, GREEN, BG3).pack(side="left")

    def _on_cloud_list_resize(self, event):
        self.cloud_canvas.configure(scrollregion=self.cloud_canvas.bbox("all"))

    def _on_cloud_canvas_resize(self, event):
        self.cloud_canvas.itemconfig(self.cloud_canvas_window, width=event.width)

    # ──────── ОБЛАКО: ЛОГИКА ────────

    def _cloud_load_config(self):
        try:
            with open(SYNC_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"railway_url": "", "secret": "vcs_7kRp9xMnQw3T"}

    def _cloud_save_config(self):
        self._cloud_cfg["railway_url"] = self.cloud_url_var.get().strip().rstrip("/")
        try:
            with open(SYNC_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._cloud_cfg, f, ensure_ascii=False, indent=4)
            self.cloud_status_lbl.config(text="✅ Сохранено", fg=GREEN)
            self.root.after(3000, lambda: self.cloud_status_lbl.config(text="", fg=SUBTEXT))
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _cloud_refresh(self):
        """Запрашиваем список файлов с Railway в отдельном потоке."""
        url = self._cloud_cfg.get("railway_url", "").strip()
        if not url:
            self.cloud_status_lbl.config(text="⚠️ Введи Railway URL внизу", fg=RED)
            return
        self.cloud_status_lbl.config(text="⏳ Загружаю...", fg=SUBTEXT)
        threading.Thread(target=self._cloud_fetch, daemon=True).start()

    def _cloud_fetch(self):
        url    = self._cloud_cfg.get("railway_url", "").rstrip("/")
        secret = self._cloud_cfg.get("secret", "vcs_7kRp9xMnQw3T")
        try:
            r     = requests.get(f"{url}/pending", params={"secret": secret}, timeout=10)
            items = r.json()
            if isinstance(items, list):
                self.root.after(0, lambda: self._cloud_render_list(items))
            else:
                raise ValueError(str(items))
        except Exception as e:
            self.root.after(0, lambda: self.cloud_status_lbl.config(
                text=f"❌ Ошибка: {str(e)[:60]}", fg=RED))

    def _cloud_render_list(self, items):
        # Чистим старый список
        for widget in self.cloud_list_frame.winfo_children():
            widget.destroy()

        if not items:
            self.cloud_status_lbl.config(text="Нет новых файлов", fg=SUBTEXT)
            tk.Label(self.cloud_list_frame,
                     text="Нет файлов, ожидающих скачивания",
                     font=("Segoe UI", 10), bg=BG, fg=SUBTEXT).pack(pady=40)
            return

        self.cloud_status_lbl.config(text=f"{len(items)} файлов", fg=GREEN)

        for item in items:
            self._cloud_render_row(item)

    def _cloud_render_row(self, item):
        row = tk.Frame(self.cloud_list_frame, bg=BG2, pady=10, padx=14)
        row.pack(fill="x", pady=(0, 6))

        # Левая часть — имя и источник
        left = tk.Frame(row, bg=BG2)
        left.pack(side="left", fill="x", expand=True)

        tk.Label(left, text=f"📄  {item['filename']}",
                 font=("Segoe UI", 11, "bold"), bg=BG2, fg=TEXT,
                 anchor="w").pack(anchor="w")

        source_short = (item.get("source") or "")[:60]
        date_str     = item.get("created_at", "")
        meta         = f"📅 {date_str}   🔗 {source_short}" if source_short else f"📅 {date_str}"
        tk.Label(left, text=meta, font=("Segoe UI", 9),
                 bg=BG2, fg=SUBTEXT, anchor="w").pack(anchor="w", pady=(2,0))

        # Кнопка скачать
        dl_btn = self._btn(row, "⬇  Скачать", None, GREEN, BG3)
        dl_btn.config(command=lambda i=item, r=row, b=dl_btn: self._cloud_download(i, r, b))
        dl_btn.pack(side="right", padx=(10, 0))

    def _cloud_download(self, item, row_frame, btn):
        btn.config(text="⏳...", state="disabled", fg=SUBTEXT)
        threading.Thread(
            target=self._cloud_download_worker,
            args=(item, row_frame, btn),
            daemon=True
        ).start()

    def _cloud_download_worker(self, item, row_frame, btn):
        url    = self._cloud_cfg.get("railway_url", "").rstrip("/")
        secret = self._cloud_cfg.get("secret", "vcs_7kRp9xMnQw3T")
        try:
            # Скачиваем содержимое
            r    = requests.get(f"{url}/content/{item['id']}",
                                params={"secret": secret}, timeout=15)
            data = r.json()
            text = data.get("text") or data.get("content", "")
            fname = data.get("filename") or item["filename"]

            # Сохраняем на рабочий стол
            safe  = re.sub(r'[<>:"/\\|?*]', '_', fname)
            path  = os.path.join(DESKTOP, safe)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

            # Помечаем как скачанный
            requests.post(f"{url}/done/{item['id']}",
                          params={"secret": secret}, timeout=10)

            # Обновляем UI — зелёный статус → через 3с удаляем строку
            def _ok():
                for w in row_frame.winfo_children():
                    w.destroy()
                tk.Label(row_frame, text=f"✅  Сохранено: {path}",
                         font=("Segoe UI", 10), bg=BG2, fg=GREEN).pack(
                    anchor="w", pady=6)
                row_frame.after(3000, row_frame.destroy)

            self.root.after(0, _ok)

        except Exception as e:
            self.root.after(0, lambda: btn.config(
                text=f"❌ {str(e)[:30]}", state="normal", fg=RED))

    # ──────── ОБЩИЕ МЕТОДЫ ────────

    def _ig_creds_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ig_creds.json")

    def _ig_load_session(self, entry):
        try:
            with open(self._ig_creds_path(), encoding="utf-8") as f:
                data = json.load(f)
            s = data.get("session_id", "")
            if s:
                entry.delete(0, "end")
                entry.insert(0, s)
                self.ig_session_var.set(s)
                self.ig_status_lbl.config(text="✅ сохранён", fg=GREEN)
        except Exception:
            pass

    def _ig_save_session(self):
        s = self.ig_session_var.get().strip()
        if not s:
            self.ig_status_lbl.config(text="⚠️ Пусто", fg="#fab387")
            return
        try:
            with open(self._ig_creds_path(), "w", encoding="utf-8") as f:
                json.dump({"session_id": s}, f, ensure_ascii=False)
            self.ig_status_lbl.config(text="✅ сохранён", fg=GREEN)
        except Exception as e:
            self.ig_status_lbl.config(text=f"❌ {e}", fg=RED)

    def _ig_show_help(self):
        messagebox.showinfo("Как получить Instagram sessionid",
            "1. Открой Chrome и зайди на instagram.com\n"
            "2. Нажми F12 (DevTools)\n"
            "3. Перейди во вкладку: Application\n"
            "4. Слева: Storage → Cookies → https://www.instagram.com\n"
            "5. Найди строку sessionid\n"
            "6. Скопируй значение из колонки Value\n"
            "7. Вставь сюда и нажми 💾\n\n"
            "Работает с 2FA, безопасно, не требует пароля."
        )

    def _pick_cookies(self):
        """Выбрать cookies.txt и скопировать в папку приложения."""
        path = filedialog.askopenfilename(
            title="Выбери файл куков (cookies.txt)",
            filetypes=[("Cookies", "*.txt"), ("Все файлы", "*.*")])
        if not path:
            return
        import shutil
        app_dir = os.path.dirname(os.path.abspath(__file__))
        dest    = os.path.join(app_dir, "instagram_cookies.txt")
        shutil.copy2(path, dest)
        self.ig_lbl.config(text="✅ instagram_cookies.txt сохранён!", fg=GREEN)
        self.root.after(3000, lambda: self.ig_lbl.config(
            text="✅ instagram_cookies.txt найден", fg=GREEN))

    def _add_paste_menu(self, entry):
        """Добавляем контекстное меню с Вставить и фикс Ctrl+V."""
        menu = tk.Menu(self.root, tearoff=0, bg=BG3, fg=TEXT,
                       activebackground=ACCENT, activeforeground=BG,
                       relief="flat", bd=0)
        menu.add_command(label="Вставить", command=lambda: self._do_paste(entry))
        menu.add_command(label="Очистить", command=lambda: entry.delete(0, "end"))

        def show_menu(e):
            entry.focus_set()
            menu.tk_popup(e.x_root, e.y_root)

        entry.bind("<Button-3>", show_menu)
        entry.bind("<<Paste>>",   lambda e: self._do_paste(entry) or "break")
        entry.bind("<Control-v>", lambda e: self._do_paste(entry) or "break")
        entry.bind("<Control-V>", lambda e: self._do_paste(entry) or "break")
        # Ctrl+V работает на любой раскладке (keycode 86 = физическая клавиша V)
        self.root.bind_all("<Control-v>",       lambda e: self._do_paste_focused())
        self.root.bind_all("<Control-V>",       lambda e: self._do_paste_focused())
        self.root.bind_all("<Control-KeyPress>", self._on_ctrl_key)

    def _do_paste(self, entry):
        try:
            clip = self.root.tk.call("clipboard", "get")
            entry.delete(0, "end")
            entry.insert(0, str(clip).strip())
            entry.focus_set()
        except Exception:
            pass

    def _do_paste_focused(self):
        """Вставка в текущий активный виджет."""
        try:
            w = self.root.focus_get()
            if isinstance(w, tk.Entry):
                clip = self.root.tk.call("clipboard", "get")
                w.delete(0, "end")
                w.insert(0, str(clip).strip())
        except Exception:
            pass

    def _on_ctrl_key(self, event):
        """Ловим Ctrl+V и Ctrl+A по keycode — работает на любой раскладке."""
        if event.keycode == 86:   # физическая клавиша V
            self._do_paste_focused()
        elif event.keycode == 65:  # физическая клавиша A
            w = self.root.focus_get()
            if isinstance(w, tk.Entry):
                w.select_range(0, "end")
                return "break"

    def _btn(self, parent, text, cmd, fg, bg):
        return tk.Button(parent, text=text, command=cmd,
                         font=("Segoe UI", 10, "bold"),
                         bg=bg, fg=fg, activebackground=BG3, activeforeground=fg,
                         relief="flat", bd=0, padx=14, pady=7, cursor="hand2")

    # ──────── Drag & Drop ────────

    def _on_drag_enter(self, event):
        self.drop_zone.config(bg=ACCENT, fg=BG2)

    def _on_drag_leave(self, event):
        self.drop_zone.config(bg=BG3, fg=SUBTEXT)

    def _on_drop(self, event):
        self.drop_zone.config(bg=BG3, fg=SUBTEXT)
        path = event.data.strip().strip("{}").split("} {")[0]
        if not os.path.isfile(path):
            messagebox.showwarning("Транскрибатор", f"Файл не найден:\n{path}")
            return
        self.drop_zone.config(text=f"  ✅  {os.path.basename(path)}  ", fg=GREEN)
        threading.Thread(target=self._run,
                         args=(path, None, Path(path).stem), daemon=True).start()

    # ──────── ЗАПУСК ТРАНСКРИПЦИИ ────────

    def _start_from_url(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Транскрибатор", "Вставь ссылку на видео")
            return
        source_name = re.sub(r'https?://(www\.)?', '', url)[:60].replace("/", "_")
        threading.Thread(target=self._run, args=(None, url, source_name), daemon=True).start()

    def _start_from_file(self):
        path = filedialog.askopenfilename(
            title="Выбери видео",
            filetypes=[("Видео", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.ts *.m4v *.wmv"),
                       ("Все файлы", "*.*")])
        if not path:
            return
        self.drop_zone.config(text=f"  ✅  {os.path.basename(path)}  ", fg=GREEN)
        threading.Thread(target=self._run,
                         args=(path, None, Path(path).stem), daemon=True).start()

    # ──────── ОСНОВНОЙ ПРОЦЕСС ────────

    def _run(self, file_path, url, source_name):
        self._set_ui_busy(True)
        self._clear_result()
        self._set_saved("")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if url:
                    audio = download_audio(url, tmp, self._set_step)
                else:
                    audio = extract_audio(file_path, tmp, self._set_step)
                chunks = split_audio(audio, tmp, self._set_step)
                text   = transcribe_all(chunks, self._set_step, self._set_progress)
                self._show_result(text)
                txt_path = save_txt(text, source_name)
                self._set_saved(f"✅ Сохранено: {txt_path}")
                self._set_step(f"Готово! {txt_path}")
                self._set_status("● Готов", GREEN)
        except Exception as e:
            self._set_step(f"Ошибка: {e}")
            self._set_status("● Ошибка", RED)
            messagebox.showerror("Ошибка транскрипции", str(e))
        finally:
            self._set_ui_busy(False)

    # ──────── UI HELPERS ────────

    def _set_ui_busy(self, busy):
        self.root.after(0, lambda: self._set_status(
            "⏳ Обрабатываю..." if busy else "● Готов",
            SUBTEXT if busy else GREEN))
        if busy:
            self.root.after(0, lambda: self._set_progress(0))

    def _set_status(self, text, color=TEXT):
        self.root.after(0, lambda: self.status_lbl.config(text=text, fg=color))

    def _set_step(self, text):
        self.root.after(0, lambda: self.step_lbl.config(text=text))

    def _set_progress(self, val):
        self.root.after(0, lambda: self.prog_var.set(val))

    def _show_result(self, text):
        def _do():
            self.result_box.config(state="normal")
            self.result_box.delete("1.0", "end")
            self.result_box.insert("end", text)
            self.result_box.config(state="disabled")
        self.root.after(0, _do)

    def _clear_result(self):
        def _do():
            self.result_box.config(state="normal")
            self.result_box.delete("1.0", "end")
            self.result_box.config(state="disabled")
        self.root.after(0, _do)

    def _copy(self):
        text = self.result_box.get("1.0", "end").strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._set_saved("📋 Скопировано!")
            self.root.after(2000, lambda: self._set_saved(""))

    def _save_as(self):
        text = self.result_box.get("1.0", "end").strip()
        if not text:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")],
            initialdir=DESKTOP)
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._set_saved(f"✅ Сохранено: {path}")

    def _set_saved(self, text):
        self.root.after(0, lambda: self.saved_lbl.config(text=text))

    def run(self):
        self.root.mainloop()


# ===================== ТОЧКА ВХОДА =====================
if __name__ == "__main__":
    app = TranscriberApp()
    app.run()
