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

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
    create_event,
    mark_event_done,
    reschedule_event
)
from services.scheduler_service import get_today_events
from services.analytics_service import track_event
from services.scheduler_service import start_scheduler
from services.db_service import get_google_tokens, save_google_tokens

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
        [KeyboardButton("📋 Tasks for Today")],
        [KeyboardButton("📅 Open Google Calendar")],
        [KeyboardButton("⚙️ Settings")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


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


def has_google_auth(user_id: int) -> bool:
    """Проверяет, авторизован ли пользователь в Google"""
    tokens = get_google_tokens(user_id)
    refresh_token = tokens.get("refresh_token") if tokens else None
    has_auth = tokens is not None and refresh_token is not None and refresh_token != ""
    print(f"[Bot] Проверка авторизации для user_id={user_id}: {'авторизован' if has_auth else 'не авторизован'}")
    if tokens and not has_auth:
        print(f"[Bot] Причина: tokens={'есть' if tokens else 'нет'}, refresh_token={'есть' if refresh_token else 'отсутствует'}")
    return has_auth


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
    
    # Обработка изменений настроек через callback
    waiting_for = context.user_data.get('waiting_for')
    if waiting_for == 'name':
        # Проверяем, не является ли текст кнопкой из меню
        if text.strip() and text not in ["📋 Tasks for Today", "📅 Open Google Calendar", "⚙️ Settings"]:
            set_user_name(chat_id, text.strip())
            await update.message.reply_text(
                f"✅ Name updated to: {text.strip()}",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
        else:
            await update.message.reply_text("Please enter a valid name (not a menu button):")
        return
    
    elif waiting_for == 'timezone':
        # Используем ту же логику, что и в онбординге
        if text == "✏️ Enter City Manually":
            await update.message.reply_text(
                "Please enter your city/timezone manually (e.g., Europe/London, America/New_York, Asia/Tokyo):",
                reply_markup=ReplyKeyboardRemove()
            )
            context.user_data['waiting_for'] = 'timezone_manual'
            return
        
        if text == "🌍 Choose from UTC List":
            await update.message.reply_text(
                "Choose your UTC offset:",
                reply_markup=build_utc_list_keyboard()
            )
            context.user_data['waiting_for'] = 'timezone_utc_list'
            return
        
        await update.message.reply_text(
            "Please choose one of the options:",
            reply_markup=build_timezone_keyboard()
        )
        return
    
    elif waiting_for == 'timezone_manual':
        try:
            pytz.timezone(text)
            set_user_timezone(chat_id, text)
            await update.message.reply_text(
                f"✅ Timezone updated to: {text}",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
        except pytz.exceptions.UnknownTimeZoneError:
            await update.message.reply_text(
                "Invalid timezone. Please enter a valid timezone (e.g., Europe/London):"
            )
        return
    
    elif waiting_for == 'timezone_utc_list':
        tz = parse_utc_offset(text)
        if tz:
            set_user_timezone(chat_id, tz)
            await update.message.reply_text(
                f"✅ Timezone updated to: {tz}",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
        else:
            await update.message.reply_text(
                "Please choose from the list:",
                reply_markup=build_utc_list_keyboard()
            )
        return
    
    elif waiting_for == 'morning_time':
        if text == "✏️ Enter Manually":
            await update.message.reply_text(
                "Enter time in HH:MM format (e.g., 09:00):",
                reply_markup=ReplyKeyboardRemove()
            )
            context.user_data['waiting_for'] = 'morning_time_manual'
            return
        
        # Проверяем формат времени из кнопок
        if text in ["08:00", "09:00", "10:00"]:
            set_morning_time(chat_id, text)
            await update.message.reply_text(
                f"✅ Morning briefing time updated to: {text}",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
        else:
            await update.message.reply_text(
                "Please choose from the options or enter manually:",
                reply_markup=build_morning_time_keyboard()
            )
        return
    
    elif waiting_for == 'morning_time_manual':
        try:
            if ':' in text:
                parts = text.split(':')
                if len(parts) == 2:
                    hour = int(parts[0].strip())
                    minute = int(parts[1].strip())
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        time_str = f"{hour:02d}:{minute:02d}"
                        set_morning_time(chat_id, time_str)
                        await update.message.reply_text(
                            f"✅ Morning briefing time updated to: {time_str}",
                            reply_markup=build_main_menu()
                        )
                        context.user_data.pop('waiting_for', None)
                        return
            raise ValueError("Invalid time format")
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time format. Please enter time in HH:MM format (e.g., 09:00):"
            )
        return
    
    elif waiting_for == 'evening_time':
        if text == "✏️ Enter Manually":
            await update.message.reply_text(
                "Enter time in HH:MM format (e.g., 21:00):",
                reply_markup=ReplyKeyboardRemove()
            )
            context.user_data['waiting_for'] = 'evening_time_manual'
            return
        
        # Проверяем формат времени из кнопок
        if text in ["18:00", "21:00", "23:00"]:
            set_evening_time(chat_id, text)
            await update.message.reply_text(
                f"✅ Evening recap time updated to: {text}",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
        else:
            await update.message.reply_text(
                "Please choose from the options or enter manually:",
                reply_markup=build_evening_time_keyboard()
            )
        return
    
    elif waiting_for == 'evening_time_manual':
        try:
            if ':' in text:
                parts = text.split(':')
                if len(parts) == 2:
                    hour = int(parts[0].strip())
                    minute = int(parts[1].strip())
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        time_str = f"{hour:02d}:{minute:02d}"
                        set_evening_time(chat_id, time_str)
                        await update.message.reply_text(
                            f"✅ Evening recap time updated to: {time_str}",
                            reply_markup=build_main_menu()
                        )
                        context.user_data.pop('waiting_for', None)
                        return
            raise ValueError("Invalid time format")
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time format. Please enter time in HH:MM format (e.g., 21:00):"
            )
        return
    
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
        except pytz.exceptions.UnknownTimeZoneError:
            await update.message.reply_text(
                "Invalid timezone. Please enter a valid timezone (e.g., Europe/London):"
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
        settings_text += "Select what you want to change:"
        
        keyboard = [
            [InlineKeyboardButton("✏️ Change Name", callback_data="set_name")],
            [InlineKeyboardButton("🌍 Change Timezone", callback_data="set_tz")],
            [InlineKeyboardButton("🌅 Morning Time", callback_data="set_morning")],
            [InlineKeyboardButton("🌙 Evening Time", callback_data="set_evening")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            settings_text,
            reply_markup=reply_markup
        )
        return
    
    if text == "📋 Tasks for Today":
        # Показываем задачи на сегодня
        await show_daily_tasks(update, context)
        return
    
    if text == "📅 Open Google Calendar":
        # Отправляем ссылку на Google Calendar сразу без дополнительного сообщения
        calendar_url = "https://calendar.google.com/calendar"
        keyboard = [[InlineKeyboardButton("📅 Open Google Calendar", url=calendar_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📅",
            reply_markup=reply_markup
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

        # Проверяем, является ли это задачей
        if not ai_parsed.get("is_task", True):
            await update.message.reply_text(
                "I didn't understand what task this is. Please try again with a clearer format (e.g., 'Meeting tomorrow at 3 PM' or 'Buy milk today at 15:00').",
                reply_markup=build_main_menu()
            )
            track_event(chat_id, "not_a_task", {"source": source})
            return

        # Дополнительная проверка: если summary пустой или слишком короткий, это может быть не задача
        summary = ai_parsed.get("summary", "").strip()
        if not summary or len(summary) < 2:
            await update.message.reply_text(
                "I didn't understand what task this is. Please specify a clear action or event (e.g., 'Meeting tomorrow at 3 PM' or 'Buy milk today at 15:00').",
                reply_markup=build_main_menu()
            )
            track_event(chat_id, "not_a_task", {"source": source, "reason": "empty_summary"})
            return

        # Трекинг успешного парсинга
        track_event(chat_id, f"task_processed_ai_{source}", {
            "has_summary": bool(ai_parsed.get("summary")),
            "has_description": bool(ai_parsed.get("description")),
            "has_location": bool(ai_parsed.get("location"))
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


async def show_daily_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает задачи на сегодня с возможностью отметки"""
    chat_id = update.effective_chat.id
    
    # Проверяем авторизацию Google Calendar
    stored_tokens = get_google_tokens(chat_id)
    if not stored_tokens:
        await update.message.reply_text(
            "❌ Please connect your Google Calendar first using /start",
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

    try:
        # Получаем таймзону пользователя
        user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
        
        # Получаем события на сегодня
        events = get_today_events(credentials, user_timezone)
        
        if not events:
            await update.message.reply_text(
                "📅 **Mark what you've already done:**\n\n"
                "No tasks scheduled for today! 🎉",
                reply_markup=build_main_menu(),
                parse_mode='Markdown'
            )
            return

        # Разделяем выполненные и невыполненные задачи
        completed_events = [e for e in events if e.get('summary', '').startswith('✅ ')]
        incomplete_events = [e for e in events if not e.get('summary', '').startswith('✅ ')]
        
        # Формируем текст сообщения
        message_text = "📅 **Mark what you've already done:**\n\n"
        
        # Добавляем выполненные задачи
        if completed_events:
            for event in completed_events:
                summary = event.get('summary', 'Task')
                # Убираем "✅ " для отображения, так как уже есть в тексте
                if summary.startswith('✅ '):
                    summary = summary[2:]
                # Добавляем время задачи
                start_time = event.get('start_time', '')
                time_str = ""
                if start_time:
                    try:
                        # Парсим время и форматируем
                        if 'T' in start_time:
                            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                            # Форматируем только если есть timezone info и конвертация прошла успешно
                            if dt.tzinfo:
                                dt = dt.astimezone(pytz.timezone(user_timezone))
                                time_str = f" {dt.strftime('%H:%M')}"
                    except:
                        pass
                message_text += f"✅ {summary}{time_str}\n"
            message_text += "\n"
        
        # Если есть невыполненные задачи, добавляем их в клавиатуру
        keyboard = []
        for event in incomplete_events:
            summary = event.get('summary', 'Task')
            event_id = event.get('id', '')
            if event_id:
                # Добавляем время к тексту кнопки
                start_time = event.get('start_time', '')
                time_str = ""
                if start_time:
                    try:
                        # Парсим время и форматируем
                        if 'T' in start_time:
                            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                            # Форматируем только если есть timezone info и конвертация прошла успешно
                            if dt.tzinfo:
                                dt = dt.astimezone(pytz.timezone(user_timezone))
                                time_str = f" {dt.strftime('%H:%M')}"
                    except:
                        pass
                # Ограничиваем длину текста кнопки (Telegram лимит 64 символа)
                button_text = f"{summary}{time_str}"
                if len(button_text) > 60:
                    button_text = f"{summary[:55]}{time_str}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"done_{event_id}")])
        
        # Добавляем кнопку обновления
        if keyboard:
            keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")])
        
        # Если нет невыполненных задач, добавляем только кнопку обновления
        if not keyboard and completed_events:
            keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")]]
        
        # Всегда создаем клавиатуру, даже если пустая (чтобы избежать ошибки "Inline keyboard expected")
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else InlineKeyboardMarkup([])
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        print(f"[Bot] Ошибка при отображении задач на сегодня: {e}")
        await update.message.reply_text(
            "❌ An error occurred while loading tasks. Please try again.",
            reply_markup=build_main_menu()
        )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на inline-кнопки"""
    query = update.callback_query
    # Не вызываем query.answer() здесь, чтобы убрать дублирование текста кнопки
    # Будем вызывать его только там, где нужно показать уведомление
    
    chat_id = query.message.chat_id
    callback_data = query.data
    
    # Обработка настроек (не требуют авторизации Google Calendar)
    if callback_data == "set_name":
        await query.answer("")  # Убираем дублирование текста кнопки
        await query.edit_message_text(
            "✏️ Enter your new name:",
            reply_markup=None
        )
        context.user_data['waiting_for'] = 'name'
        return

    elif callback_data == "set_tz":
        await query.answer("")  # Убираем дублирование текста кнопки
        # Отправляем новое сообщение с ReplyKeyboardMarkup вместо редактирования
        await query.message.reply_text(
            "🌍 Share your location or enter timezone manually:",
            reply_markup=build_timezone_keyboard()
        )
        context.user_data['waiting_for'] = 'timezone'
        return

    elif callback_data == "set_morning":
        await query.answer("")  # Убираем дублирование текста кнопки
        # Отправляем новое сообщение с ReplyKeyboardMarkup вместо редактирования
        await query.message.reply_text(
            "🌅 At what time do you want to receive your Daily Plan?\n\n"
            "Send time in HH:MM format (e.g., 09:00):",
            reply_markup=build_morning_time_keyboard()
        )
        context.user_data['waiting_for'] = 'morning_time'
        return

    elif callback_data == "set_evening":
        await query.answer("")  # Убираем дублирование текста кнопки
        # Отправляем новое сообщение с ReplyKeyboardMarkup вместо редактирования
        await query.message.reply_text(
            "🌙 When should I send you the Evening Recap?\n\n"
            "Send time in HH:MM format (e.g., 21:00):",
            reply_markup=build_evening_time_keyboard()
        )
        context.user_data['waiting_for'] = 'evening_time'
        return

    # Для остальных callback нужна авторизация Google Calendar
    # НЕ вызываем query.answer() здесь для callback, которые сами вызывают его позже:
    # - "done_*" и "already_done_*" - вызывают query.answer() в конце обработки
    # - "refresh_today" - вызывает query.answer() после обновления списка
    # - "reschedule_leftovers" - вызывает query.answer() после переноса задач
    # Вызываем только для других callback, которые не обрабатываются дальше
    if (not callback_data.startswith("done_") and 
        callback_data != "already_done_" and 
        callback_data != "refresh_today" and 
        callback_data != "reschedule_leftovers"):
        await query.answer("")  # Убираем дублирование текста кнопки для других callback
    
    stored_tokens = get_google_tokens(chat_id)
    if not stored_tokens:
        await query.edit_message_text(
            "❌ Please connect your Google Calendar first using /start"
        )
        return

    credentials = get_credentials_from_stored(chat_id, stored_tokens)
    if not credentials:
        await query.edit_message_text(
            "❌ Authorization error. Please reconnect your Google Calendar using /start"
        )
        return

    # Обработка обновления списка задач
    if callback_data == "refresh_today":
        try:
            # Авторизация уже проверена выше
            # Получаем таймзону пользователя
            user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
            
            # Получаем события на сегодня
            events = get_today_events(credentials, user_timezone)
            
            if not events:
                await query.edit_message_text(
                    "📅 **Mark what you've already done:**\n\n"
                    "No tasks scheduled for today! 🎉",
                    reply_markup=None,  # Явно очищаем клавиатуру
                    parse_mode='Markdown'
                )
                await query.answer("✅ List updated!")
                return

            # Разделяем выполненные и невыполненные задачи
            completed_events = [e for e in events if e.get('summary', '').startswith('✅ ')]
            incomplete_events = [e for e in events if not e.get('summary', '').startswith('✅ ')]
            
            # Формируем текст сообщения
            message_text = "📅 **Mark what you've already done:**\n\n"
            
            # Добавляем выполненные задачи
            if completed_events:
                for event in completed_events:
                    summary = event.get('summary', 'Task')
                    if summary.startswith('✅ '):
                        summary = summary[2:]
                    # Добавляем время задачи
                    start_time = event.get('start_time', '')
                    time_str = ""
                    if start_time:
                        try:
                            if 'T' in start_time:
                                dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                                # Форматируем только если есть timezone info и конвертация прошла успешно
                                if dt.tzinfo:
                                    dt = dt.astimezone(pytz.timezone(user_timezone))
                                    time_str = f" {dt.strftime('%H:%M')}"
                        except:
                            pass
                    message_text += f"✅ {summary}{time_str}\n"
                message_text += "\n"
            
            # Создаем клавиатуру для невыполненных задач
            keyboard = []
            for event in incomplete_events:
                summary = event.get('summary', 'Task')
                event_id = event.get('id', '')
                if event_id:
                    # Добавляем время к тексту кнопки
                    start_time = event.get('start_time', '')
                    time_str = ""
                    if start_time:
                        try:
                            if 'T' in start_time:
                                dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                                # Форматируем только если есть timezone info и конвертация прошла успешно
                                if dt.tzinfo:
                                    dt = dt.astimezone(pytz.timezone(user_timezone))
                                    time_str = f" {dt.strftime('%H:%M')}"
                        except:
                            pass
                    button_text = f"{summary}{time_str}"
                    if len(button_text) > 60:
                        button_text = f"{summary[:55]}{time_str}"
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=f"done_{event_id}")])
            
            # Добавляем кнопку обновления
            if keyboard:
                keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")])
            
            # Если нет невыполненных задач, добавляем только кнопку обновления
            if not keyboard and completed_events:
                keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")]]
            
            # Всегда создаем клавиатуру, даже если пустая (чтобы избежать ошибки "Inline keyboard expected")
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else InlineKeyboardMarkup([])
            
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            await query.answer("✅ List updated!")
        except Exception as e:
            print(f"[Bot] Ошибка при обновлении списка задач: {e}")
            await query.answer("❌ Error updating. Please try again.", show_alert=True)
        return

    # Обработка уже выполненной задачи (повторное нажатие)
    if callback_data.startswith("already_done_"):
        await query.answer("✅ This task is already marked as completed!", show_alert=True)
        return

    # Обработка отметки задачи как выполненной
    if callback_data.startswith("done_"):
        event_id = callback_data[5:]  # Убираем префикс "done_"
        
        try:
            # Получаем событие для получения текущего заголовка
            from googleapiclient.discovery import build
            service = build('calendar', 'v3', credentials=credentials)
            event = service.events().get(calendarId='primary', eventId=event_id).execute()
            event_title = event.get('summary', 'Task')
            
            # Убираем "✅ " если уже есть
            if event_title.startswith('✅ '):
                event_title = event_title[2:]
            
            # Отмечаем как выполненное
            success = mark_event_done(credentials, event_id, event_title)
            
            if success:
                # Проверяем, является ли это сообщение со списком задач
                message_text = query.message.text or ""
                is_tasks_list = "Mark what you've already done" in message_text or "Tasks for Today" in message_text
                
                if is_tasks_list:
                    # Если это список задач, обновляем весь список
                    user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
                    events = get_today_events(credentials, user_timezone)
                    
                    # Разделяем выполненные и невыполненные задачи
                    completed_events = [e for e in events if e.get('summary', '').startswith('✅ ')]
                    incomplete_events = [e for e in events if not e.get('summary', '').startswith('✅ ')]
                    
                    # Формируем новый текст сообщения
                    new_message_text = "📅 **Mark what you've already done:**\n\n"
                    
                    # Добавляем выполненные задачи
                    if completed_events:
                        for event in completed_events:
                            summary = event.get('summary', 'Task')
                            if summary.startswith('✅ '):
                                summary = summary[2:]
                            # Добавляем время задачи
                            start_time = event.get('start_time', '')
                            time_str = ""
                            if start_time:
                                try:
                                    if 'T' in start_time:
                                        dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                                        # Форматируем только если есть timezone info и конвертация прошла успешно
                                        if dt.tzinfo:
                                            dt = dt.astimezone(pytz.timezone(user_timezone))
                                            time_str = f" {dt.strftime('%H:%M')}"
                                except:
                                    pass
                            new_message_text += f"✅ {summary}{time_str}\n"
                        new_message_text += "\n"
                    
                    # Создаем новую клавиатуру для невыполненных задач
                    new_keyboard = []
                    for event in incomplete_events:
                        summary = event.get('summary', 'Task')
                        event_id_item = event.get('id', '')
                        if event_id_item:
                            # Добавляем время к тексту кнопки
                            start_time = event.get('start_time', '')
                            time_str = ""
                            if start_time:
                                try:
                                    if 'T' in start_time:
                                        dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                                        # Форматируем только если есть timezone info и конвертация прошла успешно
                                        if dt.tzinfo:
                                            dt = dt.astimezone(pytz.timezone(user_timezone))
                                            time_str = f" {dt.strftime('%H:%M')}"
                                except:
                                    pass
                            button_text = f"{summary}{time_str}"
                            if len(button_text) > 60:
                                button_text = f"{summary[:55]}{time_str}"
                            new_keyboard.append([InlineKeyboardButton(button_text, callback_data=f"done_{event_id_item}")])
                    
                    # Добавляем кнопку обновления
                    if new_keyboard:
                        new_keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")])
                    
                    # Если нет невыполненных задач, добавляем только кнопку обновления
                    if not new_keyboard and completed_events:
                        new_keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="refresh_today")]]
                    
                    # Всегда создаем клавиатуру, даже если пустая (чтобы избежать ошибки "Inline keyboard expected")
                    new_markup = InlineKeyboardMarkup(new_keyboard) if new_keyboard else InlineKeyboardMarkup([])
                    
                    await query.edit_message_text(
                        new_message_text,
                        reply_markup=new_markup,
                        parse_mode='Markdown'
                    )
                else:
                    # Если это не список задач (например, вечерняя сводка), просто обновляем кнопку
                    inline_keyboard = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
                    
                    new_keyboard = []
                    for row in inline_keyboard:
                        new_row = []
                        for button in row:
                            if button.callback_data == callback_data:
                                # Изменяем кнопку: добавляем "✅" к тексту
                                button_text = button.text
                                if not button_text.startswith('✅ '):
                                    button_text = f"✅ {button_text}"
                                new_row.append(InlineKeyboardButton(button_text, callback_data=f"already_done_{event_id}"))
                            else:
                                new_row.append(button)
                        if new_row:
                            new_keyboard.append(new_row)
                    
                    # Всегда создаем клавиатуру, даже если пустая (чтобы избежать ошибки "Inline keyboard expected")
                    new_markup = InlineKeyboardMarkup(new_keyboard) if new_keyboard else InlineKeyboardMarkup([])
                    await query.edit_message_reply_markup(reply_markup=new_markup)
                
                # Показываем визуальную обратную связь (вызываем только один раз)
                await query.answer("✅ Task marked as completed!")
                track_event(chat_id, "task_marked_done", {"event_id": event_id})
            else:
                await query.answer("❌ Failed to mark task as done. Please try again.", show_alert=True)
                
        except Exception as e:
            print(f"[Bot] Ошибка при отметке задачи как выполненной: {e}")
            await query.answer("❌ An error occurred. Please try again.", show_alert=True)
            track_event(chat_id, "error", {"error_type": "mark_task_done", "error_message": str(e)[:100]})
    
    # Обработка переноса остатка задач на завтра
    elif callback_data == "reschedule_leftovers":
        try:
            from datetime import timedelta
            
            user_timezone = get_user_timezone(chat_id) or "UTC"
            tz = pytz.timezone(user_timezone)
            now_local = datetime.now(tz)
            
            # Получаем события на сегодня
            events = get_today_events(credentials, user_timezone)
            
            # Фильтруем невыполненные (без "✅")
            incomplete_events = [e for e in events if not e.get('summary', '').startswith('✅ ')]
            
            if not incomplete_events:
                await query.answer("✅ All tasks are already completed!", show_alert=True)
                return
            
            # Переносим каждое событие на завтра
            from googleapiclient.discovery import build
            service = build('calendar', 'v3', credentials=credentials)
            
            rescheduled_count = 0
            tomorrow = now_local + timedelta(days=1)
            
            for event in incomplete_events:
                event_id = event.get('id')
                if not event_id:
                    continue
                
                try:
                    # Получаем событие
                    calendar_event = service.events().get(calendarId='primary', eventId=event_id).execute()
                    
                    # Парсим текущее время начала
                    start_str = calendar_event['start'].get('dateTime', calendar_event['start'].get('date'))
                    is_all_day = 'T' not in start_str
                    
                    if is_all_day:
                        # Если это событие на весь день, используем 09:00 завтра
                        start_dt = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
                        start_dt = tz.localize(start_dt) if start_dt.tzinfo is None else start_dt
                    else:
                        # Timed событие - парсим текущее время
                        start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                        if start_dt.tzinfo is None:
                            start_dt = pytz.utc.localize(start_dt)
                    
                    # Вычисляем длительность
                    end_str = calendar_event['end'].get('dateTime', calendar_event['end'].get('date'))
                    if 'T' in end_str:
                        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                        if end_dt.tzinfo is None:
                            end_dt = pytz.utc.localize(end_dt)
                        duration = end_dt - start_dt
                    else:
                        duration = timedelta(hours=1)  # По умолчанию 1 час для all-day событий
                    
                    # Переносим на завтра
                    if is_all_day:
                        # Для all-day событий start_dt уже установлен на завтра, не добавляем день
                        new_start = start_dt
                    else:
                        # Для timed событий добавляем один день
                        new_start = start_dt + timedelta(days=1)
                        if new_start < now_local:
                            # Если время уже прошло, ставим на утро завтра
                            new_start = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
                            new_start = tz.localize(new_start) if new_start.tzinfo is None else new_start
                    
                    new_end = new_start + duration
                    
                    # Конвертируем в UTC для API
                    new_start_utc = new_start.astimezone(pytz.utc)
                    new_end_utc = new_end.astimezone(pytz.utc)
                    
                    # Переносим событие
                    success = reschedule_event(credentials, event_id, new_start_utc, new_end_utc)
                    if success:
                        rescheduled_count += 1
                        
                except Exception as e:
                    print(f"[Bot] Ошибка при переносе события {event_id}: {e}")
                    continue
            
            if rescheduled_count > 0:
                await query.edit_message_text(
                    f"Rescheduled {rescheduled_count} task(s) to tomorrow."
                )
                track_event(chat_id, "tasks_rescheduled", {"count": rescheduled_count})
            else:
                await query.answer("❌ Failed to reschedule tasks. Please try again.", show_alert=True)
                
        except Exception as e:
            print(f"[Bot] Ошибка при переносе задач на завтра: {e}")
            await query.answer("❌ An error occurred. Please try again.", show_alert=True)
            track_event(chat_id, "error", {"error_type": "reschedule_tasks", "error_message": str(e)[:100]})


async def create_calendar_event(update: Update, context: ContextTypes.DEFAULT_TYPE, event_data: Dict, source: str):
    """Создает событие в Google Calendar"""
    chat_id = update.effective_chat.id
    
    # Проверяем авторизацию
    print(f"[Bot] create_calendar_event вызван для chat_id={chat_id}, source={source}")
    has_auth = has_google_auth(chat_id)
    print(f"[Bot] Результат проверки авторизации для chat_id={chat_id}: {has_auth}")
    
    if not has_auth:
        # Дополнительная проверка - может быть токены есть, но refresh_token отсутствует
        stored_tokens = get_google_tokens(chat_id)
        if stored_tokens:
            print(f"[Bot] Токены найдены для chat_id={chat_id}, но авторизация не прошла. Детали:")
            print(f"[Bot] - token: {'есть' if stored_tokens.get('token') else 'нет'}")
            print(f"[Bot] - refresh_token: {'есть' if stored_tokens.get('refresh_token') else 'нет'}")
            print(f"[Bot] - client_id: {'есть' if stored_tokens.get('client_id') else 'нет'}")
            print(f"[Bot] - client_secret: {'есть' if stored_tokens.get('client_secret') else 'нет'}")
        
        # Формируем redirect_uri для callback (используем тот же логику, что и в finish_onboarding)
        base_url = os.getenv("BASE_URL")
        if not base_url:
            port = int(os.getenv("PORT", 8000))
            base_url = f"http://localhost:{port}"
        redirect_uri = f"{base_url}/google/callback"
        
        auth_url = get_authorization_url(chat_id, redirect_uri)
        print(f"[Bot] Отправляем ссылку на авторизацию Google Calendar для chat_id={chat_id}")
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
    
    # Создаем bot application ПЕРЕД определением google_callback, чтобы он был доступен в замыкании
    app: Application = (
        ApplicationBuilder()
        .token(token)
        .build()
    )

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
    
    async def _post_init(app_instance):
        await app_instance.bot.delete_webhook(drop_pending_updates=True)
        await set_commands(app_instance)
        # Запускаем scheduler после инициализации бота
        start_scheduler(app_instance.bot)
        # Запускаем HTTP сервер в фоне через asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(start_http_server())
    
    # Регистрируем post_init callback
    app.post_init = _post_init

    # Регистрируем хендлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

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
