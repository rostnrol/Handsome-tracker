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
import asyncio
from aiohttp import web

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
from services.scheduler_service import start_scheduler
from services.db_service import get_google_tokens

# ---- timezonefinder (pure Python) ----
try:
    from timezonefinder import TimezoneFinder
except Exception:
    TimezoneFinder = None

# ----------------- Config -----------------

DB_PATH = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "UTC")

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
    """Создает клавиатуру для выбора таймзоны (3 варианта)"""
    keyboard = [
        [KeyboardButton("📍 Share Location", request_location=True)],
        [KeyboardButton("✏️ Enter City Manually")],
        [KeyboardButton("🌍 Choose from UTC List")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def build_utc_list_keyboard() -> ReplyKeyboardMarkup:
    """Создает клавиатуру со списком UTC таймзон"""
    timezones = [
        ["UTC-12", "UTC-11", "UTC-10", "UTC-9"],
        ["UTC-8", "UTC-7", "UTC-6", "UTC-5"],
        ["UTC-4", "UTC-3", "UTC-2", "UTC-1"],
        ["UTC+0", "UTC+1", "UTC+2", "UTC+3"],
        ["UTC+4", "UTC+5", "UTC+6", "UTC+7"],
        ["UTC+8", "UTC+9", "UTC+10", "UTC+11"],
        ["UTC+12", "⬅️ Back"]
    ]
    return ReplyKeyboardMarkup(timezones, resize_keyboard=True, one_time_keyboard=True)


# ----------------- Storage -----------------

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            tz TEXT,
            user_name TEXT,
            morning_time TEXT NOT NULL DEFAULT '09:00',
            evening_time TEXT NOT NULL DEFAULT '21:00',
            onboard_done INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Мягкие миграции для существующих БД
    try:
        cur.execute("ALTER TABLE settings ADD COLUMN user_name TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE settings ADD COLUMN morning_time TEXT NOT NULL DEFAULT '09:00'")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE settings ADD COLUMN evening_time TEXT NOT NULL DEFAULT '21:00'")
    except sqlite3.OperationalError:
        pass
    # Миграция старых полей briefing_hour/briefing_minute в morning_time
    try:
        cur.execute("SELECT briefing_hour, briefing_minute FROM settings LIMIT 1")
        # Если поля существуют, мигрируем данные
        cur.execute("""
            UPDATE settings 
            SET morning_time = printf('%02d:%02d', briefing_hour, briefing_minute)
            WHERE morning_time = '09:00' AND briefing_hour IS NOT NULL
        """)
    except sqlite3.OperationalError:
        pass
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

def get_user_timezone(chat_id: int) -> Optional[str]:
    """Получает таймзону пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT tz FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def get_user_name(chat_id: int) -> Optional[str]:
    """Получает имя пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT user_name FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def get_morning_time(chat_id: int) -> str:
    """Получает время утренней сводки в формате HH:MM"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT morning_time FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row and row[0] else "09:00"


def get_evening_time(chat_id: int) -> str:
    """Получает время вечерней сводки в формате HH:MM"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT evening_time FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row and row[0] else "21:00"


def set_user_timezone(chat_id: int, tzname: str):
    """Устанавливает таймзону пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, tz, morning_time, evening_time, onboard_done)
        VALUES (?, ?, ?, ?, COALESCE((SELECT onboard_done FROM settings WHERE chat_id=?), 0))
        ON CONFLICT(chat_id) DO UPDATE SET tz=excluded.tz
        """,
        (chat_id, tzname, "09:00", "21:00", chat_id),
    )
    con.commit()
    con.close()


def set_user_name(chat_id: int, name: str):
    """Устанавливает имя пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, user_name, morning_time, evening_time, onboard_done)
        VALUES (?, ?, ?, ?, COALESCE((SELECT onboard_done FROM settings WHERE chat_id=?), 0))
        ON CONFLICT(chat_id) DO UPDATE SET user_name=excluded.user_name
        """,
        (chat_id, name, "09:00", "21:00", chat_id),
    )
    con.commit()
    con.close()


def set_morning_time(chat_id: int, time_str: str):
    """Устанавливает время утренней сводки в формате HH:MM"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, morning_time, evening_time, onboard_done)
        VALUES (?, ?, ?, COALESCE((SELECT onboard_done FROM settings WHERE chat_id=?), 0))
        ON CONFLICT(chat_id) DO UPDATE SET morning_time=excluded.morning_time
        """,
        (chat_id, time_str, "21:00", chat_id),
    )
    con.commit()
    con.close()


def set_evening_time(chat_id: int, time_str: str):
    """Устанавливает время вечерней сводки в формате HH:MM"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, morning_time, evening_time, onboard_done)
        VALUES (?, ?, ?, COALESCE((SELECT onboard_done FROM settings WHERE chat_id=?), 0))
        ON CONFLICT(chat_id) DO UPDATE SET evening_time=excluded.evening_time
        """,
        (chat_id, "09:00", time_str, chat_id),
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
    """Парсит UTC offset из текста (например, "UTC-5" -> таймзона)"""
    text = text.strip().upper()
    if not text.startswith("UTC"):
        return None

    # Маппинг UTC offset к таймзонам
    tz_map = {
        "UTC-12": "Etc/GMT+12",
        "UTC-11": "Pacific/Midway",
        "UTC-10": "Pacific/Honolulu",
        "UTC-9": "America/Anchorage",
        "UTC-8": "America/Los_Angeles",
        "UTC-7": "America/Denver",
        "UTC-6": "America/Chicago",
        "UTC-5": "America/New_York",
        "UTC-4": "America/Halifax",
        "UTC-3": "America/Sao_Paulo",
        "UTC-2": "Atlantic/South_Georgia",
        "UTC-1": "Atlantic/Azores",
        "UTC+0": "Europe/London",
        "UTC+1": "Europe/Paris",
        "UTC+2": "Europe/Kiev",
        "UTC+3": "Europe/Moscow",
        "UTC+4": "Asia/Dubai",
        "UTC+5": "Asia/Karachi",
        "UTC+6": "Asia/Dhaka",
        "UTC+7": "Asia/Bangkok",
        "UTC+8": "Asia/Shanghai",
        "UTC+9": "Asia/Tokyo",
        "UTC+10": "Australia/Sydney",
        "UTC+11": "Pacific/Norfolk",
        "UTC+12": "Pacific/Auckland",
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
        user_name = get_user_name(chat_id)
        greeting = f"Welcome back, {user_name}! 👋" if user_name else "Welcome back! 👋"
        await update.message.reply_text(
            f"{greeting}\n\n"
            "Send me tasks in any format:\n"
            "• Text messages\n"
            "• Voice messages\n"
            "• Photos of schedules/notes",
            reply_markup=build_main_menu()
        )
        return
    
    # Шаг 1: Приветственное сообщение
    await update.message.reply_text(
        "Hi!👋🏻\n\n"
        "I am a task tracker you've been dreaming of\n"
        "With me you won't forget a thing\n\n"
        "Every morning, I'll send you a briefing of your day\n\n"
        "You can send me tasks in any format:\n"
        "• Voice messages\n"
        "• Text\n"
        "• or even Photos of notes/schedules\n\n"
        "I will instantly add them to your Google Calendar\n"
        "During the day you can see your tasks in a little app here and mark the completed ones\n\n"
        "Every evening, I'll send you a brief summary of your day, and we'll reflect on\n"
        "• what can be transferred to the next day\n"
        "• and what can be forgotten\n\n"
        "Let's set you up✨"
    )
    
    # Шаг 2: Вопрос об имени
    await update.message.reply_text(
        "1️⃣ How should I address you?",
        reply_markup=ReplyKeyboardRemove()
    )
    context.chat_data['onboard_stage'] = 'ask_name'


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик геолокации"""
    if not update.message or not update.message.location:
        return
    
    chat_id = update.effective_chat.id
    
    # Проверяем, на каком этапе онбординга
    if context.chat_data.get('onboard_stage') != 'timezone':
        return
    
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    
    tz = tz_from_location(lat, lon)
    if tz:
        set_user_timezone(chat_id, tz)
        await ask_morning_time(update, context)
    else:
        await update.message.reply_text(
            "Couldn't determine timezone from location. Please try another option.",
            reply_markup=build_timezone_keyboard()
        )


async def ask_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вопрос о таймзоне"""
    await update.message.reply_text(
        "2️⃣ What's your timezone?\n\n"
        "You can:\n"
        "• Share your location (recommended)\n"
        "• Enter city manually\n"
        "• Choose from UTC list",
        reply_markup=build_timezone_keyboard()
    )
    context.chat_data['onboard_stage'] = 'timezone'


def build_morning_time_keyboard() -> ReplyKeyboardMarkup:
    """Создает клавиатуру для выбора времени утренней сводки"""
    keyboard = [
        [KeyboardButton("08:00"), KeyboardButton("09:00"), KeyboardButton("10:00")],
        [KeyboardButton("✏️ Enter Manually")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def build_evening_time_keyboard() -> ReplyKeyboardMarkup:
    """Создает клавиатуру для выбора времени вечерней сводки"""
    keyboard = [
        [KeyboardButton("18:00"), KeyboardButton("21:00"), KeyboardButton("23:00")],
        [KeyboardButton("✏️ Enter Manually")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


async def ask_morning_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вопрос о времени утренней сводки"""
    await update.message.reply_text(
        "3️⃣ At what time do you want to receive your Daily Plan?",
        reply_markup=build_morning_time_keyboard()
    )
    context.chat_data['onboard_stage'] = 'ask_morning_time'


async def ask_evening_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вопрос о времени вечерней сводки"""
    await update.message.reply_text(
        "4️⃣ When should I send you the Evening Recap?",
        reply_markup=build_evening_time_keyboard()
    )
    context.chat_data['onboard_stage'] = 'ask_evening_time'


async def finish_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение онбординга - подключение Google Calendar"""
    chat_id = update.effective_chat.id
    
    # Формируем redirect_uri для callback (используем тот же логику, что и в main())
    base_url = os.getenv("BASE_URL")
    if not base_url:
        port = int(os.getenv("PORT", 8000))
        base_url = f"http://localhost:{port}"
    redirect_uri = f"{base_url}/google/callback"
    
    # Генерируем URL авторизации с chat_id в state
    auth_url = get_authorization_url(chat_id, redirect_uri)
    
    keyboard = [[KeyboardButton("🔗 Connect Google Calendar")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    user_name = get_user_name(chat_id)
    greeting = f"Perfect, {user_name}! ✅" if user_name else "Perfect! ✅"
    
    await update.message.reply_text(
        f"{greeting}\n\n"
        "To get started, connect your Google Calendar:\n"
        f"{auth_url}\n\n"
        "Click the link above to authorize. You'll be redirected back automatically.",
        reply_markup=reply_markup
    )
    
    # Очищаем стадию онбординга, так как авторизация теперь происходит автоматически через callback
    # Пользователь может отправлять сообщения, и они будут обрабатываться как обычные задачи
    context.chat_data.pop('onboard_stage', None)


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    if not update.message or not update.message.text:
        return
    
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    # Обработка онбординга
    if context.chat_data.get('onboard_stage') == 'ask_name':
        # Вопрос об имени
        if text.strip():
            set_user_name(chat_id, text.strip())
            await ask_timezone(update, context)
        else:
            await update.message.reply_text(
                "Please enter your name:"
            )
        return
    
    if context.chat_data.get('onboard_stage') == 'timezone':
        # Пользователь выбирает таймзону
        if text == "✏️ Enter City Manually":
            await update.message.reply_text(
                "Please enter your city/timezone manually (e.g., Europe/London, America/New_York, Asia/Tokyo):",
                reply_markup=ReplyKeyboardRemove()
            )
            context.chat_data['onboard_stage'] = 'timezone_manual'
            return

        if text == "🌍 Choose from UTC List":
            await update.message.reply_text(
                "Choose your UTC offset:",
                reply_markup=build_utc_list_keyboard()
            )
            context.chat_data['onboard_stage'] = 'timezone_utc_list'
            return

        # Если это не кнопка, значит пользователь ввел что-то другое
        await update.message.reply_text(
            "Please choose one of the options:",
            reply_markup=build_timezone_keyboard()
        )
        return

    if context.chat_data.get('onboard_stage') == 'timezone_manual':
        # Пользователь вводит таймзону вручную
        try:
            pytz.timezone(text)
            set_user_timezone(chat_id, text)
            await ask_morning_time(update, context)
            return
        except pytz.exceptions.UnknownTimeZoneError:
            await update.message.reply_text(
                "Invalid timezone. Please enter a valid timezone (e.g., Europe/London, America/New_York):"
            )
            return

    if context.chat_data.get('onboard_stage') == 'timezone_utc_list':
        # Пользователь выбрал UTC из списка
        if text == "⬅️ Back":
            await ask_timezone(update, context)
            return
        
        # Парсим UTC offset
        tz = parse_utc_offset(text)
        if tz:
            set_user_timezone(chat_id, tz)
            await ask_morning_time(update, context)
            return
        else:
            await update.message.reply_text(
                "Invalid selection. Please choose from the list:",
                reply_markup=build_utc_list_keyboard()
            )
            return
    
    if context.chat_data.get('onboard_stage') == 'ask_morning_time':
        # Вопрос о времени утренней сводки
        if text == "✏️ Enter Manually":
            await update.message.reply_text(
                "Please enter time in format HH:MM (e.g., 09:00, 08:30):",
                reply_markup=ReplyKeyboardRemove()
            )
            context.chat_data['onboard_stage'] = 'ask_morning_time_manual'
            return

        # Проверяем, является ли это валидным временем
        try:
            if ':' in text:
                parts = text.split(':')
                if len(parts) == 2:
                    hour = int(parts[0].strip())
                    minute = int(parts[1].strip())
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        time_str = f"{hour:02d}:{minute:02d}"
                        set_morning_time(chat_id, time_str)
                        await ask_evening_time(update, context)
                        return
            raise ValueError("Invalid time format")
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time format. Please choose from the buttons or enter manually:",
                reply_markup=build_morning_time_keyboard()
            )
            return

    if context.chat_data.get('onboard_stage') == 'ask_morning_time_manual':
        # Пользователь вводит время утренней сводки вручную
        try:
            if ':' in text:
                parts = text.split(':')
                if len(parts) == 2:
                    hour = int(parts[0].strip())
                    minute = int(parts[1].strip())
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        time_str = f"{hour:02d}:{minute:02d}"
                        set_morning_time(chat_id, time_str)
                        await ask_evening_time(update, context)
                        return
            raise ValueError("Invalid time format")
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time format. Please enter time in HH:MM format (e.g., 09:00, 08:30):"
            )
            return

    if context.chat_data.get('onboard_stage') == 'ask_evening_time':
        # Вопрос о времени вечерней сводки
        if text == "✏️ Enter Manually":
            await update.message.reply_text(
                "Please enter time in format HH:MM (e.g., 21:00, 23:00):",
                reply_markup=ReplyKeyboardRemove()
            )
            context.chat_data['onboard_stage'] = 'ask_evening_time_manual'
            return

        # Проверяем, является ли это валидным временем
        try:
            if ':' in text:
                parts = text.split(':')
                if len(parts) == 2:
                    hour = int(parts[0].strip())
                    minute = int(parts[1].strip())
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        time_str = f"{hour:02d}:{minute:02d}"
                        set_evening_time(chat_id, time_str)
                        await finish_onboarding(update, context)
                        return
            raise ValueError("Invalid time format")
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time format. Please choose from the buttons or enter manually:",
                reply_markup=build_evening_time_keyboard()
            )
            return

    if context.chat_data.get('onboard_stage') == 'ask_evening_time_manual':
        # Пользователь вводит время вечерней сводки вручную
        try:
            if ':' in text:
                parts = text.split(':')
                if len(parts) == 2:
                    hour = int(parts[0].strip())
                    minute = int(parts[1].strip())
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        time_str = f"{hour:02d}:{minute:02d}"
                        set_evening_time(chat_id, time_str)
                        await finish_onboarding(update, context)
                        return
            raise ValueError("Invalid time format")
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time format. Please enter time in HH:MM format (e.g., 21:00, 23:00):"
            )
            return

    # Обработка команд меню
    if text == "⚙️ Settings":
        tz = get_user_timezone(chat_id) or DEFAULT_TZ
        morning_time = get_morning_time(chat_id)
        evening_time = get_evening_time(chat_id)
        user_name = get_user_name(chat_id)
        
        settings_text = f"⚙️ Settings\n\n"
        if user_name:
            settings_text += f"Name: {user_name}\n"
        settings_text += f"Timezone: {tz}\n"
        settings_text += f"Morning briefing: {morning_time}\n"
        settings_text += f"Evening recap: {evening_time}\n\n"
        settings_text += "To change settings, send /start to reset onboarding."
        
        await update.message.reply_text(
            settings_text,
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
        # Формируем redirect_uri для callback (используем тот же логику, что и в finish_onboarding)
        base_url = os.getenv("BASE_URL")
        if not base_url:
            port = int(os.getenv("PORT", 8000))
            base_url = f"http://localhost:{port}"
        redirect_uri = f"{base_url}/google/callback"
        
        auth_url = get_authorization_url(chat_id, redirect_uri)
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

    # Запускаем HTTP сервер для Render (health check и Google OAuth callback)
    port = int(os.getenv("PORT", 8000))
    base_url = os.getenv("BASE_URL")
    if not base_url:
        base_url = f"http://localhost:{port}"
    
    async def health_check(request):
        """Health check endpoint для Render"""
        return web.Response(text="OK")
    
    async def google_callback(request):
        """Обработчик Google OAuth callback"""
        state = None
        chat_id = None
        try:
            # Получаем code и state из query parameters
            code = request.query.get('code')
            state = request.query.get('state')  # Это chat_id
            
            if not code or not state:
                return web.Response(
                    text="Error: Missing code or state parameter",
                    status=400
                )
            
            chat_id = int(state)
            redirect_uri = f"{base_url}/google/callback"
            
            # Обмениваем код на токены
            tokens = exchange_code_for_tokens(code, redirect_uri)
            
            if tokens:
                # Сохраняем токены в БД
                save_google_tokens(chat_id, tokens)
                set_onboarded(chat_id, True)
                
                # Отправляем уведомление пользователю в Telegram
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text="✅ Great! Your Google Calendar is connected.\n\n"
                             "Now you can send me tasks in any format and I'll add them to your calendar!",
                        reply_markup=build_main_menu()
                    )
                    track_event(chat_id, "google_auth_success")
                except Exception as e:
                    print(f"[Bot] Ошибка при отправке сообщения пользователю {chat_id}: {e}")
                
                # Возвращаем HTML страницу
                html_response = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Authorization Successful</title>
                    <meta charset="UTF-8">
                    <style>
                        body {
                            font-family: Arial, sans-serif;
                            display: flex;
                            justify-content: center;
                            align-items: center;
                            height: 100vh;
                            margin: 0;
                            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        }
                        .container {
                            background: white;
                            padding: 40px;
                            border-radius: 10px;
                            box-shadow: 0 10px 25px rgba(0,0,0,0.2);
                            text-align: center;
                        }
                        h1 {
                            color: #4CAF50;
                            margin-bottom: 20px;
                        }
                        p {
                            color: #666;
                            font-size: 16px;
                        }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h1>✅ Authorization Successful!</h1>
                        <p>You can close this window and return to the bot.</p>
                    </div>
                </body>
                </html>
                """
                return web.Response(text=html_response, content_type='text/html')
            else:
                if chat_id:
                    track_event(chat_id, "google_auth_failed")
                return web.Response(
                    text="Error: Failed to exchange authorization code for tokens",
                    status=500
                )
        except Exception as e:
            print(f"[Bot] Ошибка при обработке Google callback: {e}")
            # Используем chat_id если он был определен, иначе 0
            error_chat_id = chat_id if chat_id else (int(state) if state and state.isdigit() else 0)
            track_event(error_chat_id, "error", {
                "error_type": "oauth_callback_processing",
                "error_message": str(e)[:100]
            })
            return web.Response(
                text=f"Error: {str(e)}",
                status=500
            )
    
    # Создаем aiohttp приложение
    http_app = web.Application()
    http_app.router.add_get("/", health_check)
    http_app.router.add_get("/health", health_check)
    http_app.router.add_get("/google/callback", google_callback)
    
    # Запускаем HTTP сервер в фоне
    async def start_http_server():
        """Запускает HTTP сервер на указанном порту"""
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"[HTTP Server] Started on port {port}")
        print(f"[HTTP Server] Callback URL: {base_url}/google/callback")
    
    async def _post_init(app):
        await app.bot.delete_webhook(drop_pending_updates=True)
        await set_commands(app)
        # Запускаем scheduler после инициализации бота
        start_scheduler(app.bot)
        # Запускаем HTTP сервер в фоне через asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(start_http_server())
    
    # Создаем bot application с правильной регистрацией post_init
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
