# bot.py
"""
AI-Powered Telegram Calendar Assistant
Transforms voice, text, and photos into Google Calendar events
"""

import os
import sqlite3
import json
import tempfile
import time as time_module
from datetime import datetime
from typing import Optional, Dict

import pytz

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, WebAppInfo
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import Conflict

# Импорты сервисов
from services.ai_service import parse_with_ai, transcribe_voice, extract_events_from_image
from services.calendar_service import (
    get_authorization_url,
    exchange_code_for_tokens,
    get_credentials_from_stored,
    create_event
)
from services.analytics_service import track_event
from services.scheduler_service import schedule_morning_briefing, start_scheduler
from services.db_service import get_google_tokens

# ---- timezonefinder (pure Python) ----
try:
    from timezonefinder import TimezoneFinder
except Exception:
    TimezoneFinder = None

# ----------------- Config -----------------

DB_PATH = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "UTC")
MORNING_BRIEFING_HOUR = 9
MORNING_BRIEFING_MINUTE = 0

TF = None  # lazy TimezoneFinder singleton

# ----------------- Menus -----------------

def build_main_menu() -> ReplyKeyboardMarkup:
    """Создает главное меню на английском"""
    keyboard = [
        [KeyboardButton("📅 Open Schedule", web_app=WebAppInfo(url=os.getenv("WEB_APP_URL", "https://example.com")))],
        [KeyboardButton("⚙️ Settings"), KeyboardButton("🆘 Support")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, persistent=True)


def build_timezone_keyboard() -> ReplyKeyboardMarkup:
    """Создает клавиатуру для выбора таймзоны"""
    # Популярные таймзоны UTC-5 до UTC+3
    timezones = [
        ["UTC-5 (EST)", "UTC-4 (EDT)"],
        ["UTC-3 (BRT)", "UTC-2"],
        ["UTC-1", "UTC+0 (GMT)"],
        ["UTC+1 (CET)", "UTC+2 (EET)"],
        ["UTC+3 (MSK)"]
    ]
    keyboard = [
        [KeyboardButton("📍 Share Location", request_location=True)],
        *timezones,
        [KeyboardButton("🌍 Enter Manually")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


# ----------------- Storage -----------------

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL,
            onboard_done INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_lock (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            holder TEXT,
            acquired_utc TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS google_oauth_tokens (
            user_id INTEGER PRIMARY KEY,
            token TEXT,
            refresh_token TEXT,
            token_uri TEXT,
            client_id TEXT,
            client_secret TEXT,
            scopes TEXT,
            updated_utc TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def get_con():
    return sqlite3.connect(DB_PATH)


# ----------------- Helpers -----------------

def get_user_timezone(chat_id: int) -> str:
    """Получает таймзону пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT tz FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def set_user_timezone(chat_id: int, tzname: str):
    """Устанавливает таймзону пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, tz, onboard_done)
        VALUES (?, ?, COALESCE((SELECT onboard_done FROM settings WHERE chat_id=?), 0))
        ON CONFLICT(chat_id) DO UPDATE SET tz=excluded.tz
        """,
        (chat_id, tzname, chat_id),
    )
    con.commit()
    con.close()


def is_onboarded(chat_id: int) -> bool:
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT onboard_done FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return bool(row and int(row[0]) == 1)


def set_onboarded(chat_id: int, done: bool = True):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, tz, onboard_done)
        VALUES (?, COALESCE((SELECT tz FROM settings WHERE chat_id=?), ?), ?)
        ON CONFLICT(chat_id) DO UPDATE SET onboard_done=excluded.onboard_done
        """,
        (chat_id, chat_id, DEFAULT_TZ, 1 if done else 0),
    )
    con.commit()
    con.close()


# ----------------- Google OAuth Storage -----------------

def save_google_tokens(user_id: int, tokens: Dict[str, str]):
    """Сохраняет Google OAuth токены для пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO google_oauth_tokens 
        (user_id, token, refresh_token, token_uri, client_id, client_secret, scopes, updated_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            token=excluded.token,
            refresh_token=excluded.refresh_token,
            token_uri=excluded.token_uri,
            client_id=excluded.client_id,
            client_secret=excluded.client_secret,
            scopes=excluded.scopes,
            updated_utc=excluded.updated_utc
        """,
        (
            user_id,
            tokens.get("token"),
            tokens.get("refresh_token"),
            tokens.get("token_uri"),
            tokens.get("client_id"),
            tokens.get("client_secret"),
            json.dumps(tokens.get("scopes", [])),
            datetime.utcnow().isoformat()
        ),
    )
    con.commit()
    con.close()


def has_google_auth(user_id: int) -> bool:
    """Проверяет, авторизован ли пользователь в Google"""
    tokens = get_google_tokens(user_id)
    return tokens is not None and tokens.get("refresh_token") is not None


def tz_from_location(lat: float, lon: float) -> Optional[str]:
    """Определяет таймзону по геолокации"""
    global TF
    if TF is None and TimezoneFinder is not None:
        try:
            TF = TimezoneFinder(in_memory=True)
        except Exception:
            TF = None
    if TF is None:
        return None
    try:
        tz = TF.timezone_at(lat=lat, lng=lon) or TF.certain_timezone_at(lat=lat, lng=lon)
        return tz
    except Exception:
        return None


def parse_utc_offset(text: str) -> Optional[str]:
    """Парсит UTC offset из текста (например, "UTC-5" -> "America/New_York")"""
    text = text.strip().upper()
    if not text.startswith("UTC"):
        return None
    
    # Популярные маппинги
    tz_map = {
        "UTC-5": "America/New_York",  # EST
        "UTC-4": "America/New_York",  # EDT
        "UTC-3": "America/Sao_Paulo",  # BRT
        "UTC-2": "Atlantic/South_Georgia",
        "UTC-1": "Atlantic/Azores",
        "UTC+0": "Europe/London",  # GMT
        "UTC+1": "Europe/Paris",  # CET
        "UTC+2": "Europe/Kiev",  # EET
        "UTC+3": "Europe/Moscow",  # MSK
    }
    
    # Извлекаем UTC offset
    if "UTC" in text:
        parts = text.split()
        if len(parts) > 0:
            offset_str = parts[0]
            if offset_str in tz_map:
                return tz_map[offset_str]
    
    return None


# ----------------- Bot Handlers -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    chat_id = update.effective_chat.id
    init_db()
    
    # Трекинг события
    track_event(chat_id, "user_start")
    
    # Проверяем, прошел ли онбординг
    if is_onboarded(chat_id):
        # Пользователь уже прошел онбординг - показываем меню
        await update.message.reply_text(
            "Welcome back! 👋\n\n"
            "Send me tasks in any format:\n"
            "• Text messages\n"
            "• Voice messages\n"
            "• Photos of schedules/notes",
            reply_markup=build_main_menu()
        )
        return
    
    # Шаг 1: Запрос таймзоны
    await update.message.reply_text(
        "Hi! 👋 Let's set up your timezone first.\n\n"
        "You can:\n"
        "• Share your location (recommended)\n"
        "• Choose from the list below\n"
        "• Enter manually (e.g., Europe/London)",
        reply_markup=build_timezone_keyboard()
    )
    context.chat_data['onboard_stage'] = 'timezone'


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик геолокации"""
    if not update.message or not update.message.location:
        return
    
    chat_id = update.effective_chat.id
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    
    tz = tz_from_location(lat, lon)
    if tz:
        set_user_timezone(chat_id, tz)
        await continue_onboarding(update, context)
    else:
        await update.message.reply_text(
            "Couldn't determine timezone from location. Please try selecting from the list or enter manually.",
            reply_markup=build_timezone_keyboard()
        )


async def continue_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Продолжение онбординга после выбора таймзоны"""
    chat_id = update.effective_chat.id
    
    # Шаг 2: Welcome message и кнопка подключения Google Calendar
    auth_url = get_authorization_url(chat_id)
    
    keyboard = [[KeyboardButton("🔗 Connect Google Calendar")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        "Perfect! ✅\n\n"
        "Hi! I am your AI Assistant. Every morning at 9:00 AM, I'll send you a briefing of your day. "
        "You can send me tasks in ANY format: Voice messages, Text, or even Photos of notes/schedules. "
        "I will instantly add them to your Google Calendar.\n\n"
        f"To get started, connect your Google Calendar:\n{auth_url}",
        reply_markup=reply_markup
    )
    
    context.chat_data['onboard_stage'] = 'waiting_oauth'
    context.chat_data['auth_url'] = auth_url


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    if not update.message or not update.message.text:
        return
    
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    # Обработка онбординга
    if context.chat_data.get('onboard_stage') == 'timezone':
        # Пользователь выбирает таймзону
        if text == "🌍 Enter Manually":
            await update.message.reply_text(
                "Please enter your timezone manually (e.g., Europe/London, America/New_York, Asia/Tokyo):",
                reply_markup=ReplyKeyboardRemove()
            )
            context.chat_data['onboard_stage'] = 'timezone_manual'
            return
        
        # Проверяем UTC offset
        tz = parse_utc_offset(text)
        if tz:
            set_user_timezone(chat_id, tz)
            await continue_onboarding(update, context)
            return
        
        # Проверяем, является ли это валидной таймзоной
        try:
            pytz.timezone(text)
            set_user_timezone(chat_id, text)
            await continue_onboarding(update, context)
            return
        except pytz.exceptions.UnknownTimeZoneError:
            await update.message.reply_text(
                "Invalid timezone. Please select from the list or enter a valid timezone (e.g., Europe/London):",
                reply_markup=build_timezone_keyboard()
            )
            return
    
    if context.chat_data.get('onboard_stage') == 'timezone_manual':
        # Пользователь вводит таймзону вручную
        try:
            pytz.timezone(text)
            set_user_timezone(chat_id, text)
            await continue_onboarding(update, context)
            return
        except pytz.exceptions.UnknownTimeZoneError:
            await update.message.reply_text(
                "Invalid timezone. Please enter a valid timezone (e.g., Europe/London, America/New_York):"
            )
            return
    
    if context.chat_data.get('onboard_stage') == 'waiting_oauth':
        # Пользователь отправил OAuth код
        if text == "🔗 Connect Google Calendar":
            auth_url = context.chat_data.get('auth_url', get_authorization_url(chat_id))
            await update.message.reply_text(
                f"Click the link to authorize:\n{auth_url}\n\n"
                "After authorization, send me the code you receive."
            )
            return
        
        # Пытаемся обменять код на токены
        try:
            tokens = exchange_code_for_tokens(text, chat_id)
            if tokens:
                save_google_tokens(chat_id, tokens)
                set_onboarded(chat_id, True)
                context.chat_data.pop('onboard_stage', None)
                track_event(chat_id, "google_auth_success")
                
                # Планируем утренний брифинг
                tz = get_user_timezone(chat_id) or DEFAULT_TZ
                schedule_morning_briefing(context.bot, chat_id, tz, MORNING_BRIEFING_HOUR, MORNING_BRIEFING_MINUTE)
                
                await update.message.reply_text(
                    "✅ Great! Your Google Calendar is connected.\n\n"
                    "Now you can send me tasks in any format and I'll add them to your calendar!",
                    reply_markup=build_main_menu()
                )
                return
            else:
                await update.message.reply_text(
                    "❌ Invalid authorization code. Please try again or use the button to get a new link."
                )
                track_event(chat_id, "google_auth_failed")
                return
        except Exception as e:
            print(f"[Bot] Ошибка при обработке OAuth кода: {e}")
            track_event(chat_id, "error", {"error_type": "oauth_code_processing", "error_message": str(e)[:100]})
            await update.message.reply_text(
                "An error occurred during authorization. Please try again."
            )
            return
    
    # Обработка команд меню
    if text == "⚙️ Settings":
        tz = get_user_timezone(chat_id) or DEFAULT_TZ
        await update.message.reply_text(
            f"⚙️ Settings\n\n"
            f"Timezone: {tz}\n\n"
            f"To change timezone, send /start to reset onboarding.",
            reply_markup=build_main_menu()
        )
        return
    
    if text == "🆘 Support":
        await update.message.reply_text(
            "🆘 Support\n\n"
            "Need help? Contact support or check the documentation.",
            reply_markup=build_main_menu()
        )
        return
    
    # Обработка обычного текста как задачи
    if not is_onboarded(chat_id):
        await update.message.reply_text(
            "Please complete the setup first by sending /start",
            reply_markup=ReplyKeyboardRemove()
        )
        return
    
    await process_task(update, context, text=text, source="text")


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик голосовых сообщений"""
    if not update.message or not update.message.voice:
        return
    
    chat_id = update.effective_chat.id
    
    if not is_onboarded(chat_id):
        await update.message.reply_text(
            "Please complete the setup first by sending /start"
        )
        return
    
    # Трекинг события
    track_event(chat_id, "task_source_voice")
    
    # Скачиваем голосовое сообщение
    voice_file = await context.bot.get_file(update.message.voice.file_id)
    
    # Сохраняем во временный файл
    with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as tmp_file:
        await voice_file.download_to_drive(tmp_file.name)
        tmp_path = tmp_file.name
    
    try:
        # Транскрибируем голос
        transcribed_text = await transcribe_voice(tmp_path)
        
        if not transcribed_text:
            await update.message.reply_text(
                "❌ Couldn't transcribe the voice message. Please try again or send as text.",
                reply_markup=build_main_menu()
            )
            track_event(chat_id, "error", {"error_type": "voice_transcription_failed"})
            return
        
        # Обрабатываем транскрибированный текст
        await process_task(update, context, text=transcribed_text, source="voice")
    finally:
        # Удаляем временный файл
        try:
            os.unlink(tmp_path)
        except:
            pass


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик фото"""
    if not update.message or not update.message.photo:
        return
    
    chat_id = update.effective_chat.id
    
    if not is_onboarded(chat_id):
        await update.message.reply_text(
            "Please complete the setup first by sending /start"
        )
        return
    
    # Трекинг события
    track_event(chat_id, "task_source_photo")
    
    # Получаем фото наибольшего размера
    photo = update.message.photo[-1]
    photo_file = await context.bot.get_file(photo.file_id)
    
    # Сохраняем во временный файл
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
        await photo_file.download_to_drive(tmp_file.name)
        tmp_path = tmp_file.name
    
    try:
        # Извлекаем события из фото
        tz = get_user_timezone(chat_id) or DEFAULT_TZ
        event_data = await extract_events_from_image(tmp_path, tz)
        
        if not event_data:
            await update.message.reply_text(
                "❌ Couldn't extract events from the image. Please try again or send as text.",
                reply_markup=build_main_menu()
            )
            track_event(chat_id, "error", {"error_type": "image_extraction_failed"})
            return
        
        # Создаем событие в календаре
        await create_calendar_event(update, context, event_data, source="photo")
    finally:
        # Удаляем временный файл
        try:
            os.unlink(tmp_path)
        except:
            pass


async def process_task(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, source: str):
    """Обрабатывает задачу (текст или транскрибированный голос)"""
    chat_id = update.effective_chat.id
    tz = get_user_timezone(chat_id) or DEFAULT_TZ
    
    # Трекинг события
    track_event(chat_id, "message_received", {"source": source, "text_length": len(text)})
    
    try:
        # Определяем язык (простая проверка на кириллицу)
        source_language = "ru" if any('\u0400' <= char <= '\u04FF' for char in text) else "en"
        
        # Парсим задачу с помощью AI
        ai_parsed = await parse_with_ai(text, tz, source_language)
        
        if not ai_parsed:
            await update.message.reply_text(
                "❌ Couldn't process the task. Please try again with more details.",
                reply_markup=build_main_menu()
            )
            track_event(chat_id, "error", {"error_type": "ai_parse_failed"})
            return
        
        # Трекинг успешного парсинга
        track_event(chat_id, f"task_processed_ai_{source}", {
            "has_summary": bool(ai_parsed.get("summary")),
            "has_description": bool(ai_parsed.get("description"))
        })
        
        # Создаем событие в календаре
        await create_calendar_event(update, context, ai_parsed, source=source)
        
    except Exception as e:
        print(f"[Bot] Ошибка при обработке задачи: {e}")
        track_event(chat_id, "error", {"error_type": str(type(e).__name__), "error_message": str(e)[:100]})
        await update.message.reply_text(
            "❌ An error occurred. Please try again.",
            reply_markup=build_main_menu()
        )


async def create_calendar_event(update: Update, context: ContextTypes.DEFAULT_TYPE, event_data: Dict, source: str):
    """Создает событие в Google Calendar"""
    chat_id = update.effective_chat.id
    
    # Проверяем авторизацию
    if not has_google_auth(chat_id):
        auth_url = get_authorization_url(chat_id)
        await update.message.reply_text(
            f"🔗 Please connect your Google Calendar first:\n{auth_url}",
            reply_markup=build_main_menu()
        )
        return
    
    # Получаем credentials
    stored_tokens = get_google_tokens(chat_id)
    if not stored_tokens:
        await update.message.reply_text(
            "❌ Authorization error. Please reconnect your Google Calendar using /start",
            reply_markup=build_main_menu()
        )
        return
    
    credentials = get_credentials_from_stored(chat_id, stored_tokens)
    if not credentials:
        await update.message.reply_text(
            "❌ Authorization error. Please reconnect your Google Calendar using /start",
            reply_markup=build_main_menu()
        )
        return
    
    # Создаем событие
    event_url = create_event(credentials, event_data)
    
    if event_url:
        # Успешно создано
        track_event(chat_id, "calendar_event_created", {
            "source": source,
            "summary": event_data.get("summary", "")[:50]
        })
        
        tz = get_user_timezone(chat_id) or DEFAULT_TZ
        start_dt = datetime.fromisoformat(event_data["start_time"].replace("Z", "+00:00"))
        start_local = start_dt.replace(tzinfo=pytz.utc).astimezone(pytz.timezone(tz))
        
        await update.message.reply_text(
            f"✅ Event added to calendar!\n\n"
            f"📅 {event_data.get('summary', 'Task')}\n"
            f"🕐 {start_local.strftime('%m/%d %H:%M')}\n\n"
            f"🔗 {event_url}",
            reply_markup=build_main_menu()
        )
    else:
        track_event(chat_id, "error", {"error_type": "calendar_event_creation_failed"})
        await update.message.reply_text(
            "❌ Failed to create calendar event. Please try again.",
            reply_markup=build_main_menu()
        )


async def set_commands(app: Application):
    """Устанавливает команды бота"""
    commands = [
        BotCommand("start", "Start the bot / Reset settings"),
        BotCommand("help", "Get help"),
    ]
    await app.bot.set_my_commands(commands)


# ----------------- Main -----------------

def main():
    init_db()
    
    # Singleton gates (Render etc.)
    holder = os.getenv("RENDER_INSTANCE_ID") or os.getenv("DYNO") or os.getenv("HOSTNAME") or "unknown"
    primary_env = os.getenv("PRIMARY_INSTANCE_ID")
    if primary_env and holder != primary_env:
        print(f"[singleton-env] Instance {holder} != PRIMARY_INSTANCE_ID {primary_env}: exiting.")
        return
    if os.getenv("INSTANCE_PREFERRED", "").lower() == "min":
        idx = os.getenv("RENDER_INSTANCE_INDEX")
        if idx and idx != "0":
            print(f"[singleton-env] RENDER_INSTANCE_INDEX={idx} != 0: exiting.")
            return
        if not (holder.endswith("0") or holder.endswith("a")):
            print(f"[singleton-env] Heuristic min holder not matched for {holder}: exiting.")
            return
    
    con = get_con()
    try:
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO app_lock (id, holder, acquired_utc) VALUES (1, ?, ?)", (holder, datetime.utcnow().isoformat()))
        con.commit()
        cur.execute("SELECT holder FROM app_lock WHERE id=1")
        row = cur.fetchone()
        if row and row[0] and row[0] != holder:
            print("[singleton-sqlite] Another instance is already running (holder=", row[0], ") — exiting.")
            return
    finally:
        con.close()
    
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN env variable")
    
    # Запускаем scheduler
    start_scheduler()
    
    async def _post_init(app):
        await app.bot.delete_webhook(drop_pending_updates=True)
        await set_commands(app)
    
    app: Application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .build()
    )
    
    # Регистрируем хендлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    while True:
        try:
            app.run_polling(close_loop=False)
            break
        except Conflict as e:
            print(
                "[polling] Conflict detected (another getUpdates request is active). "
                "Retrying in 5 seconds...",
                str(e),
            )
            time_module.sleep(5)


if __name__ == "__main__":
    main()
