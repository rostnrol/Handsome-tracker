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
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import asyncio
import re
from aiohttp import web

from dotenv import load_dotenv
load_dotenv()

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
    reschedule_event,
    check_availability,
    check_slot_availability,
    find_next_free_slot,
    cancel_event
)
from services.scheduler_service import get_today_events, get_events_for_date
from services.analytics_service import track_event
from services.scheduler_service import start_scheduler
from services.db_service import get_google_tokens, save_google_tokens, delete_google_tokens

# ---- timezonefinder (pure Python) ----
try:
    from timezonefinder import TimezoneFinder
except Exception:
    TimezoneFinder = None

# ----------------- Config -----------------

DB_PATH = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "UTC")
MAX_VOICE_DURATION_SECONDS = 20  # Максимальная длительность голосовых сообщений в секундах

TF = None  # lazy TimezoneFinder singleton


def _remove_task_row(inline_keyboard: list, event_id: str) -> list:
    """
    Removes every row from inline_keyboard that contains any button
    referencing event_id (works for both single-row and old 2-row layouts).
    """
    def row_has_event(row):
        for btn in row:
            cd = btn.callback_data or ""
            if (cd == f"done_{event_id}" or
                cd == f"resch_{event_id}" or
                cd == f"del_{event_id}" or
                cd == f"label_{event_id}" or
                cd == f"reschedule_{event_id}" or
                cd == f"delete_{event_id}" or
                cd == f"cancel_{event_id}" or
                cd == f"reschedule_manual_{event_id}" or
                cd.startswith(f"confirm_move_{event_id}|") or
                cd.startswith(f"confirm_move_{event_id}_")):
                return True
        return False

    return [row for row in inline_keyboard if not row_has_event(row)]


def _build_task_row(event_id: str, label_text: str) -> list:
    """Returns two keyboard rows: full-width label, then [✅, ➡️, ❌].
    Telegram splits button widths equally within a row, so putting the label
    on its own row is the only reliable way to make it visually wider."""
    label_text = label_text[:55] if len(label_text) > 55 else label_text
    return [
        [InlineKeyboardButton(label_text, callback_data=f"label_{event_id}")],
        [
            InlineKeyboardButton("✅", callback_data=f"done_{event_id}"),
            InlineKeyboardButton("➡️", callback_data=f"resch_{event_id}"),
            InlineKeyboardButton("❌", callback_data=f"del_{event_id}"),
        ],
    ]


def _parse_duration_to_minutes(text: str) -> int:
    """
    Parses task duration from text and returns the number of minutes.
    Supported formats:
    - "30" (minutes)
    - "30m", "30 min", "30 minutes"
    - "1h", "1 h", "1 hour", "2.5h"
    - "1:30" (hours:minutes)
    """
    MAX_DURATION_MINUTES = 1440  # 24 hours max
    s = text.strip().lower()
    if not s:
        raise ValueError("Empty duration")

    s = s.replace(",", ".")

    # Format H:MM (e.g., 1:30)
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            try:
                hours = float(parts[0].strip())
                minutes = float(parts[1].strip())
                total = int(hours * 60 + minutes)
                if 0 < total <= MAX_DURATION_MINUTES:
                    return total
                elif total > MAX_DURATION_MINUTES:
                    raise ValueError(f"Duration cannot exceed {MAX_DURATION_MINUTES} minutes (24 hours)")
            except ValueError:
                pass

    # Hour formats, e.g., "1.5h", "2 hours"
    hour_match = re.search(r"(\d+(\.\d+)?)\s*(h|hr|hour|hours)\b", s)
    if hour_match:
        hours = float(hour_match.group(1))
        total = int(hours * 60)
        if 0 < total <= MAX_DURATION_MINUTES:
            return total
        elif total > MAX_DURATION_MINUTES:
            raise ValueError(f"Duration cannot exceed {MAX_DURATION_MINUTES} minutes (24 hours)")

    # Minute formats, e.g., "30m", "45 min"
    minute_match = re.search(r"(\d+)\s*(m|min|mins|minute|minutes)\b", s)
    if minute_match:
        minutes = int(minute_match.group(1))
        if 0 < minutes <= MAX_DURATION_MINUTES:
            return minutes
        elif minutes > MAX_DURATION_MINUTES:
            raise ValueError(f"Duration cannot exceed {MAX_DURATION_MINUTES} minutes (24 hours)")

    # Plain number — interpret as minutes
    if s.isdigit():
        minutes = int(s)
        if 0 < minutes <= MAX_DURATION_MINUTES:
            return minutes
        elif minutes > MAX_DURATION_MINUTES:
            raise ValueError(f"Duration cannot exceed {MAX_DURATION_MINUTES} minutes (24 hours)")
        else:
            raise ValueError("Duration must be greater than 0")

    raise ValueError(f"Cannot parse duration from '{text}'")


def _clear_reschedule_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all reschedule-related state variables"""
    for key in ['waiting_for', 'rescheduling_event_id', 'reschedule_conflict_start', 'reschedule_prompt_msg_id']:
        context.user_data.pop(key, None)


async def _clear_reschedule_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Remove the inline cancel button from the reschedule prompt message."""
    msg_id = context.user_data.pop('reschedule_prompt_msg_id', None)
    if msg_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=None
            )
        except Exception:
            pass


def _clear_event_preview_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all event preview state variables"""
    for key in ['pending_event_preview', 'pending_event_source', 'pending_event_data', 'waiting_for']:
        context.user_data.pop(key, None)


def _clear_schedule_import_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all schedule import state variables"""
    for key in ['state', 'pending_schedule', 'waiting_for', 'pending_schedule_preview', 'pending_event_source',
                'schedule_weeks_prompt_msg_id']:
        context.user_data.pop(key, None)


async def _clear_schedule_weeks_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Remove the inline cancel button from the 'for how many weeks?' prompt message."""
    msg_id = context.user_data.pop('schedule_weeks_prompt_msg_id', None)
    if msg_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=None
            )
        except Exception:
            pass


def _validate_user_input(text: str, field_name: str, max_length: int = 255) -> str:
    """
    Validate and normalize user input.
    
    Args:
        text: User input text
        field_name: Name of field (for error messages)
        max_length: Maximum allowed length
    
    Returns:
        Validated and trimmed text
    
    Raises:
        ValueError: If validation fails
    """
    text = text.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    if len(text) > max_length:
        raise ValueError(f"{field_name} must be under {max_length} characters (got {len(text)})")
    return text


async def _get_credentials_or_notify(chat_id: int, stored_tokens: dict, reply_fn) -> Optional[object]:
    """
    Обёртка над get_credentials_from_stored с обработкой invalid_grant.
    reply_fn — корутина вида async fn(text: str).
    Возвращает credentials или None (уже отправив сообщение об ошибке).
    """
    try:
        creds = get_credentials_from_stored(chat_id, stored_tokens)
        if not creds:
            await reply_fn("❌ Authorization error. Please reconnect your Google Calendar using /start")
        return creds
    except ValueError as ve:
        if str(ve).startswith("invalid_grant:"):
            await reply_fn(
                "⚠️ Your Google Calendar connection has expired or was revoked.\n"
                "Please reconnect by typing /start."
            )
            return None
        raise


# ----------------- Menus -----------------

def build_main_menu() -> ReplyKeyboardMarkup:
    """Создает главное меню на английском"""
    keyboard = [
        [KeyboardButton("📋 Tasks for Today")],
        [KeyboardButton("📆 Tasks for a Date")],
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
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
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
    # Добавляем колонны для управления длительностью задач
    try:
        cur.execute("ALTER TABLE settings ADD COLUMN use_default_duration INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE settings ADD COLUMN default_task_duration INTEGER NOT NULL DEFAULT 30")
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
    return sqlite3.connect(DB_PATH, timeout=10.0)


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


def get_use_default_duration(chat_id: int) -> bool:
    """Получает флаг использования дефолтной длительности задач"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT use_default_duration FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return bool(row[0]) if row else False


def get_default_task_duration(chat_id: int) -> int:
    """Получает дефолтную длительность задачи в минутах"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT default_task_duration FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else 30


def set_default_duration_settings(chat_id: int, use_default: bool, duration_minutes: int):
    """Устанавливает настройки дефолтной длительности задач"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, use_default_duration, default_task_duration, onboard_done)
        VALUES (?, ?, ?, COALESCE((SELECT onboard_done FROM settings WHERE chat_id=?), 0))
        ON CONFLICT(chat_id) DO UPDATE SET 
            use_default_duration=excluded.use_default_duration,
            default_task_duration=excluded.default_task_duration
        """,
        (chat_id, int(use_default), duration_minutes, chat_id),
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
    return tokens is not None and refresh_token is not None and refresh_token != ""


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
    if not isinstance(text, str):
        return None

    text = text.strip().upper()
    if not text.startswith("UTC"):
        return None

    if len(text) > 10:  # Max reasonable length for "UTC-12 Back"
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

    # Direct lookup for exact matches
    if text in tz_map:
        return tz_map[text]

    # Fallback: try to extract offset from longer text
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

    # Трекинг события
    track_event(chat_id, "user_start")
    
    # Очищаем любые активные состояния (например, ожидание ответа о неделях)
    # user_data всегда доступен в telegram.ext контекстах
    context.user_data.pop('state', None)
    context.user_data.pop('pending_schedule', None)
    context.user_data.pop('waiting_for', None)
    context.user_data.pop('rescheduling_event_id', None)
    
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
        "I am a task tracker you've been dreaming of.\n"
        "With me you <b>won't forget a thing.</b>\n\n"
        "Every morning, I'll send you a <u>briefing of your day.</u>\n\n"
        "You can send me tasks in <b>any format:</b>\n"
        "• Text\n"
        "• Voice messages\n"
        "• or even Photos of notes/schedules.\n\n"
        "You can add one or several events at a time.\n\n"
        "You can even add a schedule of your regular meetings or classes.\n\n"
        "I will instantly add them to your <u>Google Calendar.</u>\n"
        "During the day you can <u>see</u> your tasks in a little app here and <u>mark</u> the completed ones, <u>reschedule</u>, or\n"
        "<u>cancel</u> those that are no longer relevant.\n\n"
        "Every evening, I'll send you a <u>brief summary of your day,</u> and we'll reflect on\n"
        "• what can be transferred to the next day\n"
        "• and what can be forgotten.\n\n"
        "Let's set you up✨",
        parse_mode='HTML'
    )
    
    # Шаг 2: Вопрос об имени
    await update.message.reply_text(
        "1️⃣ How should I address you?",
        reply_markup=ReplyKeyboardRemove()
    )
    context.chat_data['onboard_stage'] = 'ask_name'


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    await update.message.reply_text(
        "ℹ️ <b>How to use this bot:</b>\n\n"
        "• Send any text, voice message, or photo to create a task\n"
        "• When describing a task, specify both <b>start time</b> and (optionally) <b>duration</b> (e.g., <i>\"gym from 16:00 to 17:30\"</i>). If you don't specify duration, I'll ask how long the task takes.\n"
        "• <b>📋 Tasks for Today</b> — view and manage today's tasks\n"
        "• <b>📆 Tasks for a Date</b> — view tasks for any date\n"
        "• <b>📅 Open Google Calendar</b> — open your calendar\n"
        "• <b>⚙️ Settings</b> — change name, timezone, briefing times, and Google Calendar connection\n\n"
        "Task buttons:\n"
        "✅ — mark as done\n"
        "➡️ — reschedule to a new time\n"
        "❌ — delete the task\n\n"
        "Type /start to reset or reconnect Google Calendar.",
        parse_mode='HTML',
        reply_markup=build_main_menu()
    )


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик геолокации"""
    if not update.message or not update.message.location:
        return
    
    chat_id = update.effective_chat.id
    
    is_onboarding_tz = context.chat_data.get('onboard_stage') == 'timezone'
    is_settings_tz = context.user_data.get('waiting_for') == 'timezone'
    
    if not is_onboarding_tz and not is_settings_tz:
        return
    
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    
    tz = tz_from_location(lat, lon)
    if tz:
        set_user_timezone(chat_id, tz)
        if is_onboarding_tz:
            await ask_morning_time(update, context)
        else:
            context.user_data.pop('waiting_for', None)
            await update.message.reply_text(
                f"✅ Timezone updated to: {tz}",
                reply_markup=build_main_menu()
            )
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


def build_default_duration_keyboard() -> ReplyKeyboardMarkup:
    """Создает клавиатуру для выбора использования дефолтной длительности задач"""
    keyboard = [
        [KeyboardButton("✅ Yes, use default duration"), KeyboardButton("❌ No, ask me each time")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def build_duration_choice_keyboard() -> ReplyKeyboardMarkup:
    """Создает клавиатуру для выбора дефолтной длительности задачи"""
    keyboard = [
        [KeyboardButton("15 min"), KeyboardButton("30 min"), KeyboardButton("1 hour")],
        [KeyboardButton("1.5 hours"), KeyboardButton("2 hours"), KeyboardButton("✏️ Custom")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


async def ask_default_duration_preference(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вопрос о предпочтении использовать дефолтную длительность для задач"""
    await update.message.reply_text(
        "5️⃣ Should I use a default task duration when you don't specify one?\n\n"
        "This makes creating tasks faster - just confirm without specifying length.",
        reply_markup=build_default_duration_keyboard()
    )
    context.chat_data['onboard_stage'] = 'ask_default_duration_preference'


async def ask_default_duration_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вопрос о значении дефолтной длительности"""
    await update.message.reply_text(
        "6️⃣ What should be the default task duration?",
        reply_markup=build_duration_choice_keyboard()
    )
    context.chat_data['onboard_stage'] = 'ask_default_duration_value'


async def finish_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение онбординга - подключение Google Calendar"""
    chat_id = update.effective_chat.id
    
    # Формируем redirect_uri для callback.
    # Если задана REDIRECT_URI, используем её (должна в точности совпадать с настройкой в Google Cloud).
    redirect_uri = os.getenv("REDIRECT_URI")
    if not redirect_uri:
        base_url = os.getenv("BASE_URL")
        if not base_url:
            port = int(os.getenv("PORT", 8000))
            base_url = f"http://localhost:{port}"
        redirect_uri = f"{base_url}/google/callback"
    
    # Генерируем URL авторизации с chat_id в state
    auth_url = get_authorization_url(chat_id, redirect_uri)
    
    user_name = get_user_name(chat_id)
    greeting = f"Perfect, {user_name}! ✅" if user_name else "Perfect! ✅"
    
    await update.message.reply_text(
        f"{greeting}\n\n"
        "To get started, connect your Google Calendar:\n\n"
        f'<a href="{auth_url}">🔗 Connect Google Calendar</a>\n\n'
        "Click the link above to authorize. You'll be redirected back automatically.",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardRemove()
    )

    # Очищаем стадию онбординга — ждём завершения OAuth через callback
    context.chat_data['onboard_stage'] = 'awaiting_gcal_auth'


# ---- Button Helper Functions ----

def build_event_preview_buttons() -> InlineKeyboardMarkup:
    """Build standard event preview buttons"""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Looks Good", callback_data="event_confirm"),
        InlineKeyboardButton("✏️ Edit", callback_data="event_edit")
    ]])


def build_schedule_buttons() -> InlineKeyboardMarkup:
    """Build standard schedule import buttons"""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Import Schedule", callback_data="schedule_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")
    ]])


def build_edit_menu_buttons() -> InlineKeyboardMarkup:
    """Build edit menu buttons"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Name", callback_data="edit_title")],
        [InlineKeyboardButton("🕐 Time", callback_data="edit_time")],
        [InlineKeyboardButton("📍 Location", callback_data="edit_location")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]
    ])


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    if not update.message or not update.message.text:
        return
    
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    # Обработка команд меню (проверяем ПЕРЕД состоянием, чтобы пользователь мог отменить)
    if text in ("⚙️ Settings", "📋 Tasks for Today", "📆 Tasks for a Date", "📅 Open Google Calendar"):
        # Очищаем все активные состояния при переходе в меню
        context.user_data.pop('state', None)
        context.user_data.pop('pending_schedule', None)
        context.user_data.pop('waiting_for', None)
        context.user_data.pop('rescheduling_event_id', None)

    if text == "⚙️ Settings":
        
        tz = get_user_timezone(chat_id) or DEFAULT_TZ
        morning_time = get_morning_time(chat_id)
        evening_time = get_evening_time(chat_id)
        user_name = get_user_name(chat_id)
        has_calendar = has_google_auth(chat_id)
        
        use_default_dur = get_use_default_duration(chat_id)
        default_dur = get_default_task_duration(chat_id)

        settings_text = f"⚙️ Settings\n\n"
        if user_name:
            settings_text += f"Name: {user_name}\n"
        settings_text += f"Timezone: {tz}\n"
        settings_text += f"Morning briefing: {morning_time}\n"
        settings_text += f"Evening recap: {evening_time}\n"
        if use_default_dur:
            settings_text += f"Task duration: {default_dur} min (default)\n"
        else:
            settings_text += "Task duration: ask each time\n"
        settings_text += "\nGoogle Calendar: "
        settings_text += "connected\n\n" if has_calendar else "not connected\n\n"
        settings_text += "Select what you want to change:"

        keyboard_rows = [
            [InlineKeyboardButton("✏️ Change Name", callback_data="set_name")],
            [InlineKeyboardButton("🌍 Change Timezone", callback_data="set_tz")],
            [InlineKeyboardButton("🌅 Morning Time", callback_data="set_morning")],
            [InlineKeyboardButton("🌙 Evening Time", callback_data="set_evening")],
            [InlineKeyboardButton("⏱ Task Duration", callback_data="set_duration")],
        ]
        if has_calendar:
            keyboard_rows.append([InlineKeyboardButton("🔌 Disconnect Google Calendar", callback_data="disconnect_gcal")])
        else:
            keyboard_rows.append([InlineKeyboardButton("🔗 Connect Google Calendar", callback_data="connect_gcal")])

        keyboard = keyboard_rows
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            settings_text,
            reply_markup=reply_markup
        )
        return
    
    if text == "📋 Tasks for Today":
        await show_daily_tasks(update, context)
        return
    
    if text == "📆 Tasks for a Date":
        context.user_data['waiting_for'] = 'tasks_date'
        await update.message.reply_text(
            "📆 Enter a date to view tasks:\n\n"
            "Examples: <b>tomorrow</b>, <b>Monday</b>, <b>March 5</b>, <b>2026-03-10</b>",
            parse_mode='HTML'
        )
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
    
    # Обработка ответа на вопрос о количестве недель для расписания
    if context.user_data.get('state') == 'WAITING_FOR_WEEKS':
        await handle_weeks_response(update, context, text)
        return
    
    # Обработка изменений настроек через callback
    waiting_for = context.user_data.get('waiting_for')
    if waiting_for == 'tasks_date':
        await show_tasks_for_date(update, context, text)
        return
    
    elif waiting_for == 'name':
        # Проверяем, не является ли текст кнопкой из меню
        if text.strip() and text not in ["📋 Tasks for Today", "📆 Tasks for a Date", "📅 Open Google Calendar", "⚙️ Settings"]:
            try:
                validated_name = _validate_user_input(text, "Name", max_length=100)
                set_user_name(chat_id, validated_name)
                await update.message.reply_text(
                    f"✅ Name updated to: {validated_name}",
                    reply_markup=build_main_menu()
                )
                context.user_data.pop('waiting_for', None)
            except ValueError as e:
                await update.message.reply_text(f"❌ {str(e)}")
                return
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
    
    elif waiting_for == 'reschedule_time':
        # Обработка ручного ввода времени (и при необходимости длительности) для переноса задачи
        event_id = context.user_data.get('rescheduling_event_id')
        if not event_id:
            await update.message.reply_text(
                "Error: Event ID not found. Please try rescheduling again.",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
            context.user_data.pop('rescheduling_event_id', None)
            context.user_data.pop('reschedule_conflict_start', None)
            return
        
        # Получаем credentials
        stored_tokens = get_google_tokens(chat_id)
        if not stored_tokens:
            await update.message.reply_text(
                "❌ Authorization error. Please reconnect your Google Calendar.",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
            context.user_data.pop('rescheduling_event_id', None)
            context.user_data.pop('reschedule_conflict_start', None)
            return
        
        credentials = await _get_credentials_or_notify(
            chat_id, stored_tokens,
            lambda t: update.message.reply_text(t, reply_markup=build_main_menu())
        )
        if not credentials:
            context.user_data.pop('waiting_for', None)
            context.user_data.pop('rescheduling_event_id', None)
            context.user_data.pop('reschedule_conflict_start', None)
            return
        
        try:
            user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
            tz = pytz.timezone(user_timezone)

            from googleapiclient.discovery import build
            service = build('calendar', 'v3', credentials=credentials)

            def _get_conflicts_for_slot(start_local, end_local):
                """Возвращает список конфликтующих событий в локальном времени пользователя."""
                start_utc = start_local.astimezone(pytz.utc)
                end_utc = end_local.astimezone(pytz.utc)
                events_result = service.events().list(
                    calendarId='primary',
                    timeMin=start_utc.isoformat(),
                    timeMax=end_utc.isoformat(),
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                raw_events = events_result.get('items', [])
                conflicts = []
                for ev in raw_events:
                    if ev.get('id') == event_id:
                        continue
                    ev_start_str = ev['start'].get('dateTime') or ev['start'].get('date')
                    ev_end_str = ev['end'].get('dateTime') or ev['end'].get('date')
                    if not ev_start_str or not ev_end_str:
                        continue
                    try:
                        if 'T' in ev_start_str:
                            ev_start = datetime.fromisoformat(ev_start_str.replace('Z', '+00:00'))
                            if ev_start.tzinfo is None:
                                ev_start = pytz.utc.localize(ev_start)
                            ev_end = datetime.fromisoformat(ev_end_str.replace('Z', '+00:00'))
                            if ev_end.tzinfo is None:
                                ev_end = pytz.utc.localize(ev_end)
                        else:
                            # All-day event
                            ev_start = tz.localize(datetime.strptime(ev_start_str, '%Y-%m-%d'))
                            ev_end = tz.localize(datetime.strptime(ev_end_str, '%Y-%m-%d'))
                        ev_start_local = ev_start.astimezone(tz)
                        ev_end_local = ev_end.astimezone(tz)
                    except Exception:
                        continue
                    # Проверяем пересечение интервалов
                    if not (end_local <= ev_start_local or start_local >= ev_end_local):
                        conflicts.append({
                            "summary": ev.get("summary", "Busy"),
                            "start": ev_start_local,
                            "end": ev_end_local,
                        })
                return conflicts

            # --- Вариант 1: пользователь меняет ДЛИТЕЛЬНОСТЬ при уже выбранном времени ---
            conflict_start_iso = context.user_data.get('reschedule_conflict_start')
            if conflict_start_iso and ':' not in text.strip():
                try:
                    new_duration_minutes = _parse_duration_to_minutes(text)
                except ValueError:
                    # Не похоже на длительность — будем трактовать как новое время
                    pass
                else:
                    try:
                        new_start_dt = datetime.fromisoformat(conflict_start_iso)
                    except Exception:
                        new_start_dt = None
                    if new_start_dt is not None:
                        if new_start_dt.tzinfo is None:
                            new_start_dt = tz.localize(new_start_dt)
                        new_end_dt = new_start_dt + timedelta(minutes=new_duration_minutes)

                        conflicts = _get_conflicts_for_slot(new_start_dt, new_end_dt)
                        if not conflicts:
                            # Слот свободен с новой длительностью — переносим
                            new_start_utc = new_start_dt.astimezone(pytz.utc)
                            new_end_utc = new_end_dt.astimezone(pytz.utc)

                            success = reschedule_event(credentials, event_id, new_start_utc, new_end_utc)
                            if success:
                                time_str = new_start_dt.strftime('%H:%M')
                                date_str = new_start_dt.strftime('%Y-%m-%d')
                                today_str = datetime.now(tz).strftime('%Y-%m-%d')
                                if date_str == today_str:
                                    time_display = f"today at {time_str}"
                                elif date_str == (datetime.now(tz) + timedelta(days=1)).strftime('%Y-%m-%d'):
                                    time_display = f"tomorrow at {time_str}"
                                else:
                                    time_display = f"{new_start_dt.strftime('%B %d')} at {time_str}"

                                await _clear_reschedule_prompt(context, chat_id)
                                await update.message.reply_text(
                                    f"✅ Task moved to {time_display} (duration {new_duration_minutes} min)!",
                                    reply_markup=build_main_menu()
                                )
                                track_event(chat_id, "task_rescheduled_manual", {
                                    "event_id": event_id,
                                    "duration_minutes": new_duration_minutes,
                                })
                                context.user_data.pop('waiting_for', None)
                                context.user_data.pop('rescheduling_event_id', None)
                                context.user_data.pop('reschedule_conflict_start', None)
                                return
                            else:
                                await _clear_reschedule_prompt(context, chat_id)
                                await update.message.reply_text(
                                    "❌ Failed to reschedule. Please try again.",
                                    reply_markup=build_main_menu()
                                )
                                context.user_data.pop('waiting_for', None)
                                context.user_data.pop('rescheduling_event_id', None)
                                context.user_data.pop('reschedule_conflict_start', None)
                                return

                        # Всё ещё конфликт — показываем детали и просим выбрать другое время/длительность
                        lines = []
                        for c in conflicts[:3]:
                            lines.append(
                                f"• {c['start'].strftime('%H:%M')}–{c['end'].strftime('%H:%M')} {c['summary']}"
                            )
                        if len(conflicts) > 3:
                            lines.append("• ...")
                        conflict_text = "⚠️ That duration still overlaps with other event(s):\n" + "\n".join(lines)
                        conflict_text += (
                            "\n\nSend another time (e.g., <b>tomorrow 15:00</b>) or a shorter duration "
                            "(e.g., <b>30</b>, <b>45 min</b>)."
                        )
                        cancel_keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ Cancel reschedule", callback_data=f"cancel_reschedule_{event_id}")]
                        ])
                        context.user_data['reschedule_conflict_start'] = new_start_dt.isoformat()
                        await update.message.reply_text(
                            conflict_text,
                            reply_markup=cancel_keyboard,
                            parse_mode='HTML'
                        )
                        return

            # --- Вариант 2: пользователь задаёт НОВОЕ ВРЕМЯ ---
            # Если был конфликт раньше, но мы получили новый ввод времени, сбрасываем сохранённый старт
            context.user_data.pop('reschedule_conflict_start', None)

            # Используем AI для парсинга естественного языка (например, "Tomorrow 15:00", "Friday 10am")
            ai_parsed = await parse_with_ai(text, user_timezone)

            if not ai_parsed or not ai_parsed.get("is_task", True):
                # Если AI не смог распарсить, пробуем простой формат HH:MM
                if ':' in text.strip():
                    time_part = text.strip().split()[-1]  # take last token as HH:MM
                    parts = time_part.split(':')
                    if len(parts) == 2:
                        hour = int(parts[0].strip())
                        minute = int(parts[1].strip()[:2])
                        if 0 <= hour <= 23 and 0 <= minute <= 59:
                            now_local = datetime.now(tz)
                            candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                            if candidate.tzinfo is None:
                                candidate = tz.localize(candidate)
                            # Use today if time hasn't passed, otherwise tomorrow
                            if candidate > now_local:
                                new_start_dt = candidate
                            else:
                                new_start_dt = candidate + timedelta(days=1)
                        else:
                            raise ValueError("Invalid time range")
                    else:
                        raise ValueError("Invalid time format")
                else:
                    raise ValueError("Invalid time format")
            else:
                # Используем время из AI парсинга
                start_dt_str = ai_parsed.get("start_time")
                if not start_dt_str:
                    raise ValueError("Could not parse time from input")
                
                start_dt = datetime.fromisoformat(start_dt_str.replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = pytz.utc.localize(start_dt)
                
                # Конвертируем в локальный timezone
                new_start_dt = start_dt.astimezone(tz)
            
            # Получаем событие для вычисления длительности
            event = service.events().get(calendarId='primary', eventId=event_id).execute()
            
            # Получаем длительность события
            start_str = event['start'].get('dateTime', event['start'].get('date'))
            end_str = event['end'].get('dateTime', event['end'].get('date'))
            
            if 'T' in start_str:
                orig_start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                if orig_start_dt.tzinfo is None:
                    orig_start_dt = pytz.utc.localize(orig_start_dt)
                orig_end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                if orig_end_dt.tzinfo is None:
                    orig_end_dt = pytz.utc.localize(orig_end_dt)
                duration = orig_end_dt - orig_start_dt
            else:
                duration = timedelta(hours=1)
            
            new_end_dt = new_start_dt + duration

            conflicts = _get_conflicts_for_slot(new_start_dt, new_end_dt)

            if not conflicts:
                # Слот свободен - переносим событие
                new_start_utc = new_start_dt.astimezone(pytz.utc)
                new_end_utc = new_end_dt.astimezone(pytz.utc)
                
                success = reschedule_event(credentials, event_id, new_start_utc, new_end_utc)
                
                if success:
                    task_summary = event.get('summary', 'Task')
                    time_str = new_start_dt.strftime('%H:%M')
                    date_str = new_start_dt.strftime('%Y-%m-%d')
                    today_str = datetime.now(tz).strftime('%Y-%m-%d')
                    
                    if date_str == today_str:
                        time_display = f"today at {time_str}"
                    elif date_str == (datetime.now(tz) + timedelta(days=1)).strftime('%Y-%m-%d'):
                        time_display = f"tomorrow at {time_str}"
                    else:
                        time_display = f"{new_start_dt.strftime('%B %d')} at {time_str}"
                    
                    await _clear_reschedule_prompt(context, chat_id)
                    await update.message.reply_text(
                        f"✅ Task moved to {time_display}!",
                        reply_markup=build_main_menu()
                    )
                    track_event(chat_id, "task_rescheduled_manual", {"event_id": event_id})
                    # Очищаем состояние после успешного переноса
                    context.user_data.pop('waiting_for', None)
                    context.user_data.pop('rescheduling_event_id', None)
                    context.user_data.pop('reschedule_conflict_start', None)
                else:
                    await _clear_reschedule_prompt(context, chat_id)
                    await update.message.reply_text(
                        "❌ Failed to reschedule. Please try again.",
                        reply_markup=build_main_menu()
                    )
                    # Очищаем состояние при ошибке
                    context.user_data.pop('waiting_for', None)
                    context.user_data.pop('rescheduling_event_id', None)
                    context.user_data.pop('reschedule_conflict_start', None)
            else:
                # Слот занят — показываем детали и предлагаем другое время или длительность
                lines = []
                for c in conflicts[:3]:
                    lines.append(
                        f"• {c['start'].strftime('%H:%M')}–{c['end'].strftime('%H:%M')} {c['summary']}"
                    )
                if len(conflicts) > 3:
                    lines.append("• ...")
                conflict_text = "⚠️ That time overlaps with other event(s):\n" + "\n".join(lines)
                conflict_text += (
                    "\n\nSend another time (e.g., <b>tomorrow 15:00</b>) or a new duration for this task "
                    "(e.g., <b>30</b>, <b>45 min</b>, <b>1h</b>)."
                )
                cancel_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel reschedule", callback_data=f"cancel_reschedule_{event_id}")]
                ])
                context.user_data['reschedule_conflict_start'] = new_start_dt.isoformat()
                await update.message.reply_text(
                    conflict_text,
                    reply_markup=cancel_keyboard,
                    parse_mode='HTML'
                )
                return
            
        except (ValueError, IndexError, TypeError):
            cancel_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel reschedule", callback_data=f"cancel_reschedule_{event_id}")]
            ])
            await update.message.reply_text(
                "❌ Couldn't understand that time or duration. Try: <b>today 18:00</b>, <b>tomorrow 10:00</b>, <b>wed 14:30</b> or a duration like <b>30</b>, <b>45 min</b>.",
                reply_markup=cancel_keyboard,
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"[Bot] Ошибка при ручном переносе задачи: {e}")
            await _clear_reschedule_prompt(context, chat_id)
            await update.message.reply_text(
                "❌ An error occurred. Please try again.",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
            context.user_data.pop('rescheduling_event_id', None)
            context.user_data.pop('reschedule_conflict_start', None)
        return

    elif waiting_for == 'task_duration':
        # Пользователь отвечает на вопрос о длительности задачи
        if text.strip().lower() in ("cancel", "отмена", "отменить"):
            context.user_data.pop('waiting_for', None)
            context.user_data.pop('pending_event_data', None)
            context.user_data.pop('pending_event_source', None)
            await update.message.reply_text("❌ Task creation cancelled.", reply_markup=build_main_menu())
            return
        pending_event = context.user_data.get('pending_event_data')
        pending_source = context.user_data.get('pending_event_source', 'text')
        if not pending_event:
            await update.message.reply_text(
                "Sorry, I lost the task details. Please send the task again.",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
            context.user_data.pop('pending_event_source', None)
            return

        try:
            duration_minutes = _parse_duration_to_minutes(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Couldn't understand the duration. Examples:\n"
                "<b>30</b>, <b>30 min</b>, <b>1h</b>, <b>1:30</b>",
                parse_mode='HTML'
            )
            return

        try:
            # Пересчитываем время окончания по указанной длительности
            start_str = pending_event.get("start_time")
            if not start_str:
                raise ValueError("Missing start_time in pending task")
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = pytz.utc.localize(start_dt)
            end_dt = start_dt + timedelta(minutes=duration_minutes)

            pending_event["end_time"] = end_dt.isoformat()
            pending_event["duration_minutes"] = duration_minutes
            pending_event["duration_was_inferred"] = False

            # Очищаем состояние duration-ожидания и показываем предпросмотр
            context.user_data.pop('waiting_for', None)
            context.user_data.pop('pending_event_data', None)
            context.user_data.pop('pending_event_source', None)

            await show_event_preview(update, context, pending_event, source=pending_source)
        except Exception:
            await update.message.reply_text(
                "❌ An error occurred while saving the task. Please try again.",
                reply_markup=build_main_menu()
            )
            context.user_data.pop('waiting_for', None)
            context.user_data.pop('pending_event_data', None)
            context.user_data.pop('pending_event_source', None)
        return
    
    # Обработка редактирования события при подтверждении
    elif waiting_for == 'edit_event_title':
        pending_event = context.user_data.get('pending_event_preview')
        if not pending_event:
            await update.message.reply_text("❌ No event in progress. Please start over.")
            context.user_data.pop('waiting_for', None)
            return
        try:
            validated_title = _validate_user_input(text, "Title", max_length=255)
            pending_event['summary'] = validated_title
            preview_text = format_event_preview(pending_event)
            await update.message.reply_text(
                preview_text,
                parse_mode='HTML',
                reply_markup=build_event_preview_buttons()
            )
            context.user_data['waiting_for'] = 'event_confirmation'
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}")
        return

    elif waiting_for == 'edit_event_location':
        pending_event = context.user_data.get('pending_event_preview')
        if not pending_event:
            await update.message.reply_text("❌ No event in progress. Please start over.")
            context.user_data.pop('waiting_for', None)
            return
        # Allow empty/dash to clear location
        stripped = text.strip()
        if stripped in ('-', 'none', 'clear', ''):
            pending_event['location'] = ''
        else:
            try:
                validated_location = _validate_user_input(stripped, "Location", max_length=255)
                pending_event['location'] = validated_location
            except ValueError as e:
                await update.message.reply_text(f"❌ {str(e)}")
                return
        preview_text = format_event_preview(pending_event)
        await update.message.reply_text(
            preview_text,
            parse_mode='HTML',
            reply_markup=build_event_preview_buttons()
        )
        context.user_data['waiting_for'] = 'event_confirmation'
        return
    
    elif waiting_for == 'edit_event_time':
        pending_event = context.user_data.get('pending_event_preview')
        if not pending_event:
            await update.message.reply_text("❌ No event in progress. Please start over.")
            context.user_data.pop('waiting_for', None)
            return
        try:
            user_tz = get_user_timezone(chat_id) or DEFAULT_TZ
            tz = pytz.timezone(user_tz)
            now_local = datetime.now(tz)

            time_match = re.search(r'(\d{1,2}):(\d{2})', text)
            if not time_match:
                await update.message.reply_text(
                    "❌ Couldn't parse the time. Please use HH:MM format (e.g., '14:30'):"
                )
                return

            new_hour = int(time_match.group(1))
            new_min = int(time_match.group(2))

            # Parse existing start datetime in user's timezone
            start_dt = datetime.fromisoformat(pending_event['start_time'].replace('Z', '+00:00'))
            if start_dt.tzinfo is None:
                start_dt = pytz.utc.localize(start_dt)
            start_local = start_dt.astimezone(tz)

            # Check if user also specified a day of week
            dow_map = {
                'monday': 0, 'mon': 0, 'tuesday': 1, 'tue': 1,
                'wednesday': 2, 'wed': 2, 'thursday': 3, 'thu': 3,
                'friday': 4, 'fri': 4, 'saturday': 5, 'sat': 5,
                'sunday': 6, 'sun': 6,
            }
            text_words = re.split(r'\W+', text.lower())
            mentioned_dow = None
            for kw, wd in dow_map.items():
                if kw in text_words:
                    mentioned_dow = wd
                    break

            if mentioned_dow is not None:
                today_wd = now_local.weekday()
                days_ahead = (mentioned_dow - today_wd) % 7
                target_date = now_local.date() + timedelta(days=days_ahead)
                new_start_local = tz.localize(datetime(
                    target_date.year, target_date.month, target_date.day,
                    new_hour, new_min, 0
                ))
            else:
                new_start_local = start_local.replace(hour=new_hour, minute=new_min, second=0, microsecond=0)

            # Preserve duration
            end_dt_raw = pending_event.get('end_time', '')
            if end_dt_raw:
                end_dt = datetime.fromisoformat(end_dt_raw.replace('Z', '+00:00'))
                if end_dt.tzinfo is None:
                    end_dt = pytz.utc.localize(end_dt)
                duration = end_dt - start_dt
            else:
                duration = timedelta(hours=1)
            new_end_local = new_start_local + duration

            pending_event['start_time'] = new_start_local.isoformat()
            pending_event['end_time'] = new_end_local.isoformat()

            preview_text = format_event_preview(pending_event)
            await update.message.reply_text(
                preview_text,
                parse_mode='HTML',
                reply_markup=build_event_preview_buttons()
            )
            context.user_data['waiting_for'] = 'event_confirmation'
        except Exception as e:
            print(f"[Bot] Error editing event time: {e}")
            await update.message.reply_text(
                "❌ An error occurred. Please try again or send the time in HH:MM format:"
            )
        return
    
    # Обработка онбординга
    if context.chat_data.get('onboard_stage') == 'ask_name':
        # Вопрос об имени
        if text.strip():
            try:
                validated_name = _validate_user_input(text, "Name", max_length=100)
                set_user_name(chat_id, validated_name)
                await ask_timezone(update, context)
            except ValueError as e:
                await update.message.reply_text(f"❌ {e}\n\nPlease enter your name:")
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
                        await ask_default_duration_preference(update, context)
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
                        await ask_default_duration_preference(update, context)
                        return
            raise ValueError("Invalid time format")
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time format. Please enter time in HH:MM format (e.g., 21:00, 23:00):"
            )
        return

    if context.chat_data.get('onboard_stage') == 'ask_default_duration_preference':
        # Вопрос о предпочтении использВания дефолтной длительности
        if "Yes" in text or "yes" in text:
            # Пользователь хочет использовать дефолтную длительность
            context.chat_data['use_default_duration'] = True
            await ask_default_duration_value(update, context)
            return
        elif "No" in text or "no" in text:
            # Пользователь хочет, чтобы мы спрашивали длительность каждый раз
            context.chat_data['use_default_duration'] = False
            set_default_duration_settings(chat_id, False, 30)
            await finish_onboarding(update, context)
            return
        else:
            await update.message.reply_text(
                "Please choose an option:",
                reply_markup=build_default_duration_keyboard()
            )
            return

    if context.chat_data.get('onboard_stage') == 'ask_default_duration_value':
        # Выбор дефолтной длительности задачи
        use_default = context.chat_data.get('use_default_duration', True)
        duration_map = {
            "15 min": 15,
            "15": 15,
            "30 min": 30,
            "30": 30,
            "1 hour": 60,
            "1": 60,
            "60": 60,
            "1.5 hours": 90,
            "1.5": 90,
            "90": 90,
            "2 hours": 120,
            "2": 120,
            "120": 120,
        }

        if text in duration_map:
            duration_minutes = duration_map[text]
            set_default_duration_settings(chat_id, use_default, duration_minutes)
            await finish_onboarding(update, context)
            return
        elif text == "✏️ Custom":
            await update.message.reply_text(
                "Please enter the default duration in minutes (e.g., 30, 45, 60):",
                reply_markup=ReplyKeyboardRemove()
            )
            context.chat_data['onboard_stage'] = 'ask_default_duration_custom'
            return
        else:
            await update.message.reply_text(
                "Please choose a duration from the options:",
                reply_markup=build_duration_choice_keyboard()
            )
            return

    if context.chat_data.get('onboard_stage') == 'ask_default_duration_custom':
        # Пользователь вводит дефолтную длительность вручную
        try:
            use_default = context.chat_data.get('use_default_duration', True)
            duration_minutes = int(text.strip())
            if duration_minutes <= 0 or duration_minutes > 1440:
                raise ValueError("Duration must be between 1 and 1440 minutes")
            set_default_duration_settings(chat_id, use_default, duration_minutes)
            await finish_onboarding(update, context)
            return
        except (ValueError, TypeError):
            await update.message.reply_text(
                "Invalid duration. Please enter a number between 1 and 1440 (minutes):"
            )
            return

    # Ожидание подключения Google Calendar после онбординга
    if context.chat_data.get('onboard_stage') == 'awaiting_gcal_auth':
        if is_onboarded(chat_id):
            # Auth completed (callback already fired) — clear stage and proceed
            context.chat_data.pop('onboard_stage', None)
        else:
            await update.message.reply_text(
                "⏳ Please click the Google Calendar link above to finish setup.\n\n"
                "Once you authorize, you'll be ready to add tasks!"
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

    if context.chat_data.get('onboard_stage') == 'awaiting_gcal_auth':
        if is_onboarded(chat_id):
            context.chat_data.pop('onboard_stage', None)
        else:
            await update.message.reply_text(
                "⏳ Please click the Google Calendar link above to finish setup.\n\n"
                "Once you authorize, you'll be ready to add tasks!"
            )
            return

    if not is_onboarded(chat_id):
        await update.message.reply_text(
            "Please complete the setup first by sending /start"
        )
        return

    # Проверяем длительность голосового сообщения
    voice_duration = update.message.voice.duration
    if voice_duration is not None and voice_duration > MAX_VOICE_DURATION_SECONDS:
        await update.message.reply_text(
            "⚠️ Voice message too long! Please keep it under 20 seconds to save time.",
            reply_markup=build_main_menu()
        )
        track_event(chat_id, "error", {"error_type": "voice_too_long", "duration": voice_duration})
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
        except FileNotFoundError:
            pass  # File already deleted - OK
        except OSError as e:
            print(f"[Bot] Warning: Failed to delete temp file {tmp_path}: {e}")


def format_event_preview(event_data: Dict[str, str]) -> str:
    """
    Форматирует данные события для предпросмотра.
    
    Args:
        event_data: Данные события (summary, start_time, end_time, location, description)
    
    Returns:
        Форматированная строка для показа пользователю
    """
    summary = event_data.get("summary", "Event")
    location = event_data.get("location", "").strip()
    description = event_data.get("description", "").strip()
    
    try:
        # Парсим ISO время
        start_dt = datetime.fromisoformat(event_data["start_time"].replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(event_data["end_time"].replace("Z", "+00:00"))
        
        # Форматируем время
        start_str = start_dt.strftime("%a %d %b %H:%M")
        end_str = end_dt.strftime("%H:%M")
    except Exception:
        start_str = event_data.get("start_time", "")
        end_str = event_data.get("end_time", "")
    
    preview = f"📋 <b>{summary}</b>\n"
    preview += f"🕐 {start_str} - {end_str}\n"
    
    if location:
        preview += f"📍 {location}\n"
    
    if description:
        preview += f"\n📝 {description}"
    
    return preview


def format_schedule_preview(events: List[Dict[str, str]]) -> str:
    """
    Форматирует рассписание для предпросмотра.
    
    Args:
        events: Список событий из расписания
    
    Returns:
        Форматированная строка со сводкой расписания
    """
    if not events:
        return "No events found"
    
    subject = events[0].get("summary", "Class")
    location = events[0].get("location", "").strip()
    
    preview = f"📋 <b>{subject}</b>\n"
    if location:
        preview += f"📍 {location}\n"
    
    preview += f"\n📅 Weekly Schedule ({len(events)} classes):\n"
    
    for event in events[:5]:  # Показываем первые 5 событий
        day = event.get("day_of_week", "Unknown")
        start = event.get("start_time", "")
        end = event.get("end_time", "")
        preview += f"  • {day}: {start} - {end}\n"
    
    if len(events) > 5:
        preview += f"  ... and {len(events) - 5} more\n"
    
    return preview


async def show_event_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, event_data: Dict[str, str], source: str):
    """
    Показывает предпросмотр события с кнопками подтверждения.
    
    Args:
        update: Telegram Update
        context: Context
        event_data: Данные события
        source: Источник (text/photo/voice)
    """
    preview_text = format_event_preview(event_data)
    
    # Сохраняем данные события для дальнейшей обработки
    context.user_data['pending_event_preview'] = event_data
    context.user_data['pending_event_source'] = source
    context.user_data['waiting_for'] = 'event_confirmation'
    
    await update.effective_message.reply_text(
        preview_text,
        parse_mode='HTML',
        reply_markup=build_event_preview_buttons()
    )


async def show_schedule_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, schedule_data: Dict, source: str):
    """
    Показывает предпросмотр расписания с кнопками подтверждения.
    
    Args:
        update: Telegram Update
        context: Context
        schedule_data: Данные расписания
        source: Источник (text/photo/voice)
    """
    events = schedule_data.get("events", [])
    preview_text = format_schedule_preview(events)
    
    # Сохраняем данные расписания для дальнейшей обработки
    context.user_data['pending_schedule_preview'] = schedule_data
    context.user_data['pending_event_source'] = source
    context.user_data['waiting_for'] = 'schedule_confirmation'
    
    await update.effective_message.reply_text(
        preview_text,
        parse_mode='HTML',
        reply_markup=build_schedule_buttons()
    )


async def _process_photo_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, suffix: str = '.jpg'):
    """Shared logic for processing a photo or image document."""
    chat_id = update.effective_chat.id
    track_event(chat_id, "task_source_photo")

    photo_file = await context.bot.get_file(file_id)

    # Create temp file then close it immediately so download_to_drive can write
    # to it freely (on Windows an open NamedTemporaryFile causes a lock error).
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
        tmp_path = tmp_file.name
    await photo_file.download_to_drive(tmp_path)

    try:
        file_size = os.path.getsize(tmp_path)
        print(f"[Bot] Image downloaded: {tmp_path} ({file_size} bytes)")
        if file_size == 0:
            print("[Bot] Error: downloaded image file is empty")
            await update.message.reply_text(
                "❌ Couldn't download the image. Please try again.",
                reply_markup=build_main_menu()
            )
            track_event(chat_id, "error", {"error_type": "image_download_empty"})
            return

        tz = get_user_timezone(chat_id) or DEFAULT_TZ
        event_data = await extract_events_from_image(tmp_path, tz)

        if not event_data:
            await update.message.reply_text(
                "❌ Couldn't find any events in the image.\n\n"
                "Make sure the photo clearly shows a schedule, timetable, or a task with a time.\n"
                "You can also send the task as a text message.",
                reply_markup=build_main_menu()
            )
            track_event(chat_id, "error", {"error_type": "image_extraction_failed"})
            return

        if event_data.get("is_recurring_schedule", False):
            await show_schedule_preview(update, context, event_data, source="photo")
        else:
            await show_event_preview(update, context, event_data, source="photo")
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[Bot] Warning: Failed to delete temp file {tmp_path}: {e}")


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик фото"""
    if not update.message or not update.message.photo:
        return

    chat_id = update.effective_chat.id

    if context.chat_data.get('onboard_stage') == 'awaiting_gcal_auth':
        if is_onboarded(chat_id):
            context.chat_data.pop('onboard_stage', None)
        else:
            await update.message.reply_text(
                "⏳ Please click the Google Calendar link above to finish setup.\n\n"
                "Once you authorize, you'll be ready to add tasks!"
            )
            return

    if not is_onboarded(chat_id):
        await update.message.reply_text(
            "Please complete the setup first by sending /start"
        )
        return

    # Получаем фото наибольшего размера
    photo = update.message.photo[-1]
    await _process_photo_file(update, context, photo.file_id, suffix='.jpg')


async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles image files sent as documents (e.g. iPhone 'Send as File' or HEIC photos)."""
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    mime = doc.mime_type or ""

    # Only handle image documents
    if not mime.startswith("image/"):
        return

    chat_id = update.effective_chat.id

    if context.chat_data.get('onboard_stage') == 'awaiting_gcal_auth':
        if is_onboarded(chat_id):
            context.chat_data.pop('onboard_stage', None)
        else:
            await update.message.reply_text(
                "⏳ Please click the Google Calendar link above to finish setup.\n\n"
                "Once you authorize, you'll be ready to add tasks!"
            )
            return

    if not is_onboarded(chat_id):
        await update.message.reply_text(
            "Please complete the setup first by sending /start"
        )
        return

    # HEIC/HEIF (iPhone native format): convert to JPEG before processing
    if mime in ("image/heic", "image/heif"):
        await _process_heic_document(update, context, doc.file_id)
        return

    # Map MIME type to a temp file extension GPT-4o understands
    mime_to_ext = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    suffix = mime_to_ext.get(mime, ".jpg")

    await _process_photo_file(update, context, doc.file_id, suffix=suffix)


async def _process_heic_document(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    """Downloads a HEIC file, converts it to JPEG, then runs the normal image processing."""
    heic_path = None
    jpeg_path = None
    try:
        import pillow_heif
        from PIL import Image
        pillow_heif.register_heif_opener()
    except ImportError:
        await update.message.reply_text(
            "📸 Please send the photo using the 📎 attachment icon and choose "
            "<b>Photo</b> (not File). Telegram will compress it to a format the AI can read.",
            parse_mode='HTML',
            reply_markup=build_main_menu()
        )
        return

    try:
        photo_file = await context.bot.get_file(file_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix='.heic') as hf:
            heic_path = hf.name
        await photo_file.download_to_drive(heic_path)

        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as jf:
            jpeg_path = jf.name

        with Image.open(heic_path) as img:
            img.convert('RGB').save(jpeg_path, 'JPEG', quality=90)

        # Process the converted JPEG
        chat_id = update.effective_chat.id
        track_event(chat_id, "task_source_photo")
        tz = get_user_timezone(chat_id) or DEFAULT_TZ
        event_data = await extract_events_from_image(jpeg_path, tz)

        if not event_data:
            await update.message.reply_text(
                "❌ Couldn't extract events from the image. Please try again or send as text.",
                reply_markup=build_main_menu()
            )
            track_event(chat_id, "error", {"error_type": "image_extraction_failed"})
            return

        if event_data.get("is_recurring_schedule", False):
            await show_schedule_preview(update, context, event_data, source="photo")
        else:
            await show_event_preview(update, context, event_data, source="photo")

    except Exception as e:
        print(f"[Bot] Error processing HEIC image: {e}")
        await update.message.reply_text(
            "❌ Couldn't process the HEIC image. Please try sending as a regular photo.",
            reply_markup=build_main_menu()
        )
    finally:
        for path in (heic_path, jpeg_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def get_next_occurrence_of_weekday(start_date: datetime, target_weekday: str) -> datetime:
    """
    Находит следующее вхождение указанного дня недели, начиная с start_date.
    
    Args:
        start_date: Дата начала поиска (datetime с timezone)
        target_weekday: День недели на английском (Monday, Tuesday, etc.)
    
    Returns:
        datetime следующего вхождения дня недели
    """
    weekday_map = {
        "Monday": 0,
        "Tuesday": 1,
        "Wednesday": 2,
        "Thursday": 3,
        "Friday": 4,
        "Saturday": 5,
        "Sunday": 6
    }
    
    target_weekday_num = weekday_map.get(target_weekday)
    if target_weekday_num is None:
        raise ValueError(f"Invalid weekday: {target_weekday}")
    
    current_weekday = start_date.weekday()
    days_ahead = target_weekday_num - current_weekday

    if days_ahead < 0:
        # Day already passed this week — jump to next week
        days_ahead += 7
    # If days_ahead == 0, today is that weekday — use today as-is
    # (caller is responsible for checking if the time has passed and adding 7 days if needed)

    return start_date + timedelta(days=days_ahead)


async def handle_schedule_import(update: Update, context: ContextTypes.DEFAULT_TYPE, schedule_data: Dict, source: str):
    """
    Обрабатывает импорт рекуррентного расписания.
    
    Args:
        update: Telegram Update object
        context: Context object
        schedule_data: Данные расписания с ключом 'events'
        source: Источник (text/photo)
    """
    chat_id = update.effective_chat.id
    
    if not schedule_data.get("is_recurring_schedule", False) or "events" not in schedule_data:
        return
    
    events = schedule_data["events"]
    msg = update.effective_message  # works in both message and callback_query contexts
    if not events or len(events) == 0:
        await msg.reply_text(
            "❌ No valid events found in the schedule.",
            reply_markup=build_main_menu()
        )
        return

    # Удаляем возможные дубликаты событий, которые иногда может вернуть AI
    unique_events = []
    seen_keys = set()
    for ev in events:
        key = (
            (ev.get("day_of_week") or "").strip(),
            (ev.get("start_time") or "").strip(),
            (ev.get("end_time") or "").strip(),
            (ev.get("summary") or "").strip(),
            (ev.get("location") or "").strip(),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_events.append(ev)

    events = unique_events
    if not events:
        await msg.reply_text(
            "❌ No valid events found in the schedule.",
            reply_markup=build_main_menu()
        )
        return

    # Сохраняем расписание в user_data (уже без дубликатов)
    context.user_data['pending_schedule'] = events
    context.user_data['state'] = 'WAITING_FOR_WEEKS'

    # Отправляем сообщение с вопросом о количестве недель
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="schedule_weeks_cancel")]])
    weeks_prompt_msg = await msg.reply_text(
        f"👀 I see a weekly schedule with {len(events)} classes. For how many weeks should I add this to your calendar? (e.g., write '10' or '12'):",
        reply_markup=cancel_kb
    )
    context.user_data['schedule_weeks_prompt_msg_id'] = weeks_prompt_msg.message_id

    track_event(chat_id, "schedule_import_initiated", {"source": source, "events_count": len(events)})


async def handle_weeks_response(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """
    Обрабатывает ответ пользователя о количестве недель для расписания.

    Args:
        update: Telegram Update object
        context: Context object
        text: Текст ответа пользователя
    """
    chat_id = update.effective_chat.id

    # Allow user to cancel via text
    if text.strip().lower() in ("cancel", "отмена", "отменить"):
        await _clear_schedule_weeks_prompt(context, chat_id)
        context.user_data.pop('state', None)
        context.user_data.pop('pending_schedule', None)
        await update.effective_message.reply_text("❌ Schedule import cancelled.", reply_markup=build_main_menu())
        return

    # Проверяем авторизацию Google Calendar
    stored_tokens = get_google_tokens(chat_id)
    if not stored_tokens:
        await update.effective_message.reply_text(
            "❌ Please connect your Google Calendar first using /start",
            reply_markup=build_main_menu()
        )
        context.user_data.pop('state', None)
        context.user_data.pop('pending_schedule', None)
        return

    credentials = await _get_credentials_or_notify(
        chat_id, stored_tokens,
        lambda t: update.effective_message.reply_text(t, reply_markup=build_main_menu())
    )
    if not credentials:
        context.user_data.pop('state', None)
        context.user_data.pop('pending_schedule', None)
        return

    # Парсим количество недель
    try:
        num_weeks = int(text.strip())
        if num_weeks <= 0 or num_weeks > 52:
            raise ValueError("Invalid number of weeks")
    except (ValueError, TypeError):
        await update.effective_message.reply_text(
            "❌ Please enter a valid number of weeks (1-52):"
        )
        return

    # Получаем сохраненное расписание
    pending_schedule = context.user_data.get('pending_schedule')
    if not pending_schedule:
        await _clear_schedule_weeks_prompt(context, chat_id)
        await update.effective_message.reply_text(
            "❌ Schedule data not found. Please try importing the schedule again.",
            reply_markup=build_main_menu()
        )
        context.user_data.pop('state', None)
        context.user_data.pop('pending_schedule', None)
        return
    
    # Получаем таймзону пользователя
    user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
    tz = pytz.timezone(user_timezone)
    now_local = datetime.now(tz)

    # Начинаем с сегодняшнего дня
    start_date = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    # Build the full list of events to create (without creating them yet)
    events_to_create = _build_schedule_event_list(pending_schedule, num_weeks, start_date)

    if not events_to_create:
        await _clear_schedule_weeks_prompt(context, chat_id)
        await update.effective_message.reply_text(
            "❌ No valid events could be parsed from the schedule.",
            reply_markup=build_main_menu()
        )
        context.user_data.pop('state', None)
        context.user_data.pop('pending_schedule', None)
        return

    # Check for conflicts with existing [SCHEDULE] events
    conflict_lines = []
    try:
        from googleapiclient.discovery import build as gcal_build
        service = gcal_build('calendar', 'v3', credentials=credentials)

        # Fetch all events in the import date range in one call
        range_start = events_to_create[0]["start_time"]
        range_end = events_to_create[-1]["end_time"]
        existing_result = service.events().list(
            calendarId='primary',
            timeMin=range_start,
            timeMax=range_end,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        existing_events = existing_result.get('items', [])

        # Check against ALL existing events (not just previously imported schedules)
        schedule_existing = existing_events

        # Check each new event against existing events
        seen_conflicts: set = set()
        conflict_existing_ids: list = []
        for ev in events_to_create:
            new_start = datetime.fromisoformat(ev["start_time"].replace("Z", "+00:00"))
            new_end = datetime.fromisoformat(ev["end_time"].replace("Z", "+00:00"))
            if new_start.tzinfo is None:
                new_start = pytz.utc.localize(new_start)
            if new_end.tzinfo is None:
                new_end = pytz.utc.localize(new_end)

            for ex in schedule_existing:
                ex_start_str = (ex.get('start') or {}).get('dateTime') or (ex.get('start') or {}).get('date')
                ex_end_str = (ex.get('end') or {}).get('dateTime') or (ex.get('end') or {}).get('date')
                if not ex_start_str or not ex_end_str:
                    continue
                try:
                    ex_start = datetime.fromisoformat(ex_start_str.replace("Z", "+00:00"))
                    ex_end = datetime.fromisoformat(ex_end_str.replace("Z", "+00:00"))
                    if ex_start.tzinfo is None:
                        ex_start = pytz.utc.localize(ex_start)
                    if ex_end.tzinfo is None:
                        ex_end = pytz.utc.localize(ex_end)
                except Exception:
                    continue
                if new_start < ex_end and new_end > ex_start:
                    ex_id = ex.get('id', '')
                    ex_start_local = ex_start.astimezone(tz)
                    conflict_key = (ex_id, ex_start_local.strftime('%a %H:%M'))
                    if conflict_key not in seen_conflicts:
                        seen_conflicts.add(conflict_key)
                        ex_end_local = ex_end.astimezone(tz)
                        conflict_lines.append(
                            f"• {ex_start_local.strftime('%a %d %b %H:%M')}–{ex_end_local.strftime('%H:%M')} {ex.get('summary', 'Event')}"
                        )
                        if ex_id and ex_id not in conflict_existing_ids:
                            conflict_existing_ids.append(ex_id)
                    break
    except Exception as e:
        print(f"[Bot] Warning: Could not check schedule conflicts: {e}")

    if conflict_lines:
        # Clear the "for how many weeks?" prompt now that we're moving to the conflict picker
        await _clear_schedule_weeks_prompt(context, chat_id)
        # Store state for confirmation callbacks
        context.user_data['pending_schedule_events'] = events_to_create
        context.user_data['pending_schedule_conflict_ids'] = conflict_existing_ids
        conflict_summary = "\n".join(conflict_lines[:5])
        if len(conflict_lines) > 5:
            conflict_summary += f"\n• ... and {len(conflict_lines) - 5} more"
        await update.effective_message.reply_text(
            f"⚠️ New schedule conflicts with {len(conflict_lines)} existing event(s):\n"
            f"{conflict_summary}\n\n"
            "What would you like to do?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Replace old", callback_data="schedule_weeks_replace"),
                    InlineKeyboardButton("➕ Add both", callback_data="schedule_weeks_force"),
                ],
                [
                    InlineKeyboardButton("⏭ Skip conflicts", callback_data="schedule_weeks_skip"),
                    InlineKeyboardButton("❌ Cancel", callback_data="schedule_weeks_cancel"),
                ],
            ])
        )
        track_event(chat_id, "schedule_import_conflicts_found", {"conflicts": len(conflict_lines)})
        context.user_data.pop('state', None)
        context.user_data.pop('pending_schedule', None)
        return

    # No conflicts – proceed with creation
    events_created = await _execute_schedule_creation(credentials, events_to_create, include_conflicts=True)
    await _clear_schedule_weeks_prompt(context, chat_id)
    await update.effective_message.reply_text(
        f"✅ Added schedule for {num_weeks} weeks! Created {events_created} event(s).",
        reply_markup=build_main_menu()
    )
    track_event(chat_id, "schedule_imported", {"weeks": num_weeks, "events_created": events_created})
    context.user_data.pop('state', None)
    context.user_data.pop('pending_schedule', None)


def _build_schedule_event_list(pending_schedule: List[Dict], num_weeks: int, start_date: datetime) -> List[Dict]:
    """
    Builds the full list of event dicts for a schedule import without creating them.
    Returns a list of event dicts ready for create_event().
    """
    tz = start_date.tzinfo
    events_list = []
    for week in range(num_weeks):
        for event in pending_schedule:
            day_of_week = event.get("day_of_week")
            start_time_str = event.get("start_time")
            end_time_str = event.get("end_time")
            summary = event.get("summary", "Event")
            location = event.get("location", "")

            if not day_of_week or not start_time_str:
                continue

            week_start = start_date + timedelta(weeks=week)
            try:
                event_date = get_next_occurrence_of_weekday(week_start, day_of_week)
            except ValueError:
                continue

            try:
                start_parts = start_time_str.split(":")
                end_parts = (end_time_str or "").split(":")
                if len(start_parts) != 2 or len(end_parts) != 2:
                    continue

                start_hour = int(start_parts[0])
                start_minute = int(start_parts[1])
                end_hour = int(end_parts[0])
                end_minute = int(end_parts[1])

                if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59 and
                        0 <= end_hour <= 23 and 0 <= end_minute <= 59):
                    continue

                event_start = event_date.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
                event_end = event_date.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)

                if event_end <= event_start:
                    if end_hour < start_hour or (end_hour == start_hour and end_minute < start_minute):
                        event_end = event_end + timedelta(days=1)
                    else:
                        event_end = event_start + timedelta(hours=1)

                event_start_utc = event_start.astimezone(pytz.utc)
                event_end_utc = event_end.astimezone(pytz.utc)

                events_list.append({
                    "summary": summary,
                    "start_time": event_start_utc.isoformat(),
                    "end_time": event_end_utc.isoformat(),
                    "description": "[SCHEDULE]",
                    "location": location,
                })
            except (ValueError, IndexError) as e:
                print(f"[Bot] Error building schedule event: {e}")
                continue

    return events_list


async def _execute_schedule_creation(credentials, events_to_create: List[Dict], include_conflicts: bool = True,
                                     conflict_starts: Optional[set] = None) -> int:
    """
    Creates all events in events_to_create.
    If include_conflicts=False, skips events whose start_time is in conflict_starts (ISO strings).
    Returns the number of events successfully created.
    """
    events_created = 0
    for event_data in events_to_create:
        if not include_conflicts and conflict_starts and event_data["start_time"] in conflict_starts:
            continue
        try:
            event_url = create_event(credentials, event_data)
            if event_url:
                events_created += 1
            await asyncio.sleep(0.05)  # Avoid hitting Google Calendar API rate limits
        except Exception as e:
            print(f"[Bot] Error creating schedule event '{event_data.get('summary')}': {e}")
            continue
    return events_created


def _find_best_matching_event_for_text(events: List[Dict], text_lower: str, user_timezone: str) -> Optional[Dict]:
    """
    Находит событие, лучше всего соответствующее текстовому описанию пользователя.
    Учитывает время, фрагменты названия и простые указания даты (today / tomorrow / сегодня / завтра).
    """
    if not events:
        return None

    tz = pytz.timezone(user_timezone)
    now_local = datetime.now(tz)
    today = now_local.date()
    tomorrow = (now_local + timedelta(days=1)).date()

    # Ищем времена формата HH:MM в тексте
    time_matches = re.findall(r"\b(\d{1,2}):(\d{2})\b", text_lower)
    times_in_text = []
    for h_str, m_str in time_matches:
        try:
            h = int(h_str)
            m = int(m_str)
            if 0 <= h <= 23 and 0 <= m <= 59:
                times_in_text.append((h, m))
        except ValueError:
            continue

    best_event = None
    best_score = 0

    for ev in events:
        summary = ev.get("summary", "") or ""
        if summary.startswith("❌ "):
            # Уже отменённые события пропускаем
            continue

        summary_lower = summary.lower()
        if summary_lower.startswith("✅ "):
            summary_lower = summary_lower[2:]

        start_raw = ev.get("start_time") or ""
        local_time_tuple = None
        local_date = None

        try:
            if start_raw:
                if "T" in start_raw:
                    dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = pytz.utc.localize(dt)
                    dt_local = dt.astimezone(tz)
                    local_time_tuple = (dt_local.hour, dt_local.minute)
                    local_date = dt_local.date()
                else:
                    # All-day event
                    dt_local = tz.localize(datetime.strptime(start_raw, "%Y-%m-%d"))
                    local_date = dt_local.date()
        except Exception:
            pass

        score = 0

        # Совпадение по времени
        if times_in_text and local_time_tuple:
            for (h, m) in times_in_text:
                if h == local_time_tuple[0] and m == local_time_tuple[1]:
                    score += 5
                    break

        # Совпадение по словам в summary
        for token in summary_lower.split():
            token = token.strip()
            if token and token in text_lower:
                score += 1

        # Простые указания на дату
        if local_date:
            if ("today" in text_lower or "сегодня" in text_lower) and local_date == today:
                score += 2
            if ("tomorrow" in text_lower or "завтра" in text_lower) and local_date == tomorrow:
                score += 2

        if score > best_score:
            best_score = score
            best_event = ev

    return best_event if best_score > 0 else None


async def _try_handle_management_command(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, source: str) -> bool:
    """
    Пытается интерпретировать входящее сообщение как команду управления задачами
    (отмена или перенос существующей задачи).
    
    Возвращает True, если сообщение было обработано здесь и не требует дальнейшей
    обработки как новая задача.
    """
    chat_id = update.effective_chat.id
    text_lower = text.lower()

    is_cancel_cmd = any(
        kw in text_lower
        for kw in ["отмени", "отменить", "удали", "удалить", "cancel", "delete", "remove task", "cancel task"]
    )
    is_reschedule_cmd = any(
        kw in text_lower
        for kw in ["перенеси", "перенести", "перепланируй", "reschedule", "move task", "move my task"]
    )

    if not (is_cancel_cmd or is_reschedule_cmd):
        return False

    # Проверяем авторизацию Google Calendar
    stored_tokens = get_google_tokens(chat_id)
    if not stored_tokens:
        await update.message.reply_text(
            "❌ Please connect your Google Calendar first using /start",
            reply_markup=build_main_menu()
        )
        return True

    credentials = await _get_credentials_or_notify(
        chat_id, stored_tokens,
        lambda t: update.message.reply_text(t, reply_markup=build_main_menu())
    )
    if not credentials:
        return True

    user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
    tz = pytz.timezone(user_timezone)
    now_local = datetime.now(tz)

    # Собираем события на сегодня и ближайшие 6 дней
    events: List[Dict] = []
    try:
        events.extend(get_today_events(credentials, user_timezone))
        for offset in range(1, 7):
            target_date = (now_local + timedelta(days=offset)).date()
            events.extend(get_events_for_date(credentials, user_timezone, target_date))
    except Exception as e:
        print(f"[Bot] Ошибка при загрузке событий для команды управления задачами: {e}")
        await update.message.reply_text(
            "❌ Couldn't load your tasks. Please try again.",
            reply_markup=build_main_menu()
        )
        return True

    if not events:
        await update.message.reply_text(
            "I couldn't find any tasks in the next 7 days.",
            reply_markup=build_main_menu()
        )
        return True

    best_event = _find_best_matching_event_for_text(events, text_lower, user_timezone)
    if not best_event:
        await update.message.reply_text(
            "I couldn't find which task you meant. Please mention the time and title, "
            "for example: <b>\"cancel 16:00 gym\"</b> or <b>\"перенеси завтра 18:00 тренировку\"</b>.",
            parse_mode='HTML',
            reply_markup=build_main_menu()
        )
        return True

    event_id = best_event.get("id")
    if not event_id:
        await update.message.reply_text(
            "I found a matching task but couldn't read its ID. Please try again using the buttons.",
            reply_markup=build_main_menu()
        )
        return True

    # Формируем человеко-понятное описание найденной задачи
    summary = best_event.get("summary", "Task")
    start_raw = best_event.get("start_time") or ""
    time_str = ""
    date_str = ""
    try:
        if start_raw:
            if "T" in start_raw:
                dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = pytz.utc.localize(dt)
                dt_local = dt.astimezone(tz)
                time_str = dt_local.strftime("%H:%M")
                date_str = dt_local.strftime("%Y-%m-%d")
            else:
                dt_local = tz.localize(datetime.strptime(start_raw, "%Y-%m-%d"))
                date_str = dt_local.strftime("%Y-%m-%d")
    except Exception:
        pass

    label = summary
    if time_str and date_str:
        label = f"{date_str} {time_str} — {summary}"
    elif time_str:
        label = f"{time_str} — {summary}"

    if is_cancel_cmd and not is_reschedule_cmd:
        # Отмена задачи
        try:
            success = cancel_event(credentials, event_id)
            if success:
                await update.message.reply_text(
                    f"✅ Task cancelled: {label}",
                    reply_markup=build_main_menu()
                )
                track_event(chat_id, "task_cancelled_nl", {"event_id": event_id, "source": source})
            else:
                await update.message.reply_text(
                    "❌ Failed to cancel the task. Please try again or use the buttons.",
                    reply_markup=build_main_menu()
                )
        except Exception as e:
            print(f"[Bot] Ошибка при отмене задачи через естественный язык: {e}")
            await update.message.reply_text(
                "❌ An error occurred while cancelling the task. Please try again.",
                reply_markup=build_main_menu()
            )
        return True

    # Перенос задачи (если есть хотя бы один триггер переноса)
    if is_reschedule_cmd:
        context.user_data['rescheduling_event_id'] = event_id
        context.user_data['waiting_for'] = 'reschedule_time'

        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel reschedule", callback_data=f"cancel_reschedule_{event_id}")]
        ])

        await update.message.reply_text(
            f"📅 I found this task:\n<b>{label}</b>\n\n"
            "For what time to reschedule?\n\n"
            "Examples: <b>today 18:00</b>, <b>tomorrow 10:00</b>, <b>wed 14:30</b>, <b>15:00</b>",
            reply_markup=cancel_keyboard,
            parse_mode='HTML'
        )
        track_event(chat_id, "task_reschedule_nl_started", {"event_id": event_id, "source": source})
        return True

    # Если дошли сюда, но не обработали — ничего не делаем
    return False


async def process_task(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, source: str):
    """Обрабатывает задачу (текст или транскрибированный голос)"""
    chat_id = update.effective_chat.id
    tz = get_user_timezone(chat_id) or DEFAULT_TZ
    
    # Трекинг события
    track_event(chat_id, "message_received", {"source": source, "text_length": len(text)})
    
    try:
        # Сначала пробуем интерпретировать сообщение как команду управления задачами
        handled = await _try_handle_management_command(update, context, text, source)
        if handled:
            return
        
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

        # Проверяем, является ли это рекуррентным расписанием
        if ai_parsed.get("is_recurring_schedule", False):
            # Always show preview so user can confirm before import
            await show_schedule_preview(update, context, ai_parsed, source=source)
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
        
        # Check if duration was inferred (not explicitly specified by user)
        duration_inferred = bool(ai_parsed.get("duration_was_inferred", True))
        
        if duration_inferred:
            # Duration was not specified by user
            use_default = get_use_default_duration(chat_id)
            
            if use_default:
                # User wants to use default duration - apply it and show preview
                default_duration = get_default_task_duration(chat_id)
                start_dt = datetime.fromisoformat(ai_parsed["start_time"].replace("Z", "+00:00"))
                end_dt = start_dt + timedelta(minutes=default_duration)
                ai_parsed["end_time"] = end_dt.isoformat()
                ai_parsed["duration_minutes"] = default_duration
                await show_event_preview(update, context, ai_parsed, source=source)
            else:
                # User wants to be asked for duration each time
                context.user_data['waiting_for'] = 'task_duration'
                context.user_data['pending_event_data'] = ai_parsed
                context.user_data['pending_event_source'] = source

                cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_task_duration")]])
                await update.message.reply_text(
                    "⏱ Please specify how long this task will take.\n\n"
                    "Examples: <b>30</b>, <b>30 min</b>, <b>1h</b>, <b>1:30</b>",
                    parse_mode='HTML',
                    reply_markup=cancel_kb
                )
                return
        else:
            # Duration was explicitly specified - just show preview
            await show_event_preview(update, context, ai_parsed, source=source)
        
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

    credentials = await _get_credentials_or_notify(
        chat_id, stored_tokens,
        lambda t: update.message.reply_text(t, reply_markup=build_main_menu())
    )
    if not credentials:
        return

    try:
        # Получаем таймзону пользователя
        user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
        
        # Получаем события на сегодня
        events = get_today_events(credentials, user_timezone)
        
        if not events:
            await update.message.reply_text(
                "📅 <b>Here are your tasks for today:</b>\n\n"
                "No tasks scheduled for today! 🎉",
                reply_markup=build_main_menu(),
                parse_mode='HTML'
            )
            return

        # Разделяем выполненные и невыполненные задачи; скрываем отменённые (❌)
        completed_events = [e for e in events if e.get('summary', '').startswith('✅ ')]
        incomplete_events = [
            e for e in events
            if not e.get('summary', '').startswith('✅ ')
            and not e.get('summary', '').startswith('❌ ')
        ]
        
        # Формируем текст сообщения - только интро
        message_text = "📅 <b>Here are your tasks for today:</b>\n\n"
        
        # Добавляем информацию о выполненных задачах в текст
        if completed_events:
            message_text += "✅ Completed:\n"
            for event in completed_events:
                summary = event.get('summary', 'Task')
                # Убираем "✅ " для отображения
                if summary.startswith('✅ '):
                    summary = summary[2:]
                # Добавляем время задачи
                start_time = event.get('start_time', '')
                time_str = ""
                if start_time:
                    try:
                        if 'T' in start_time:
                            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                            if dt.tzinfo:
                                dt = dt.astimezone(pytz.timezone(user_timezone))
                                time_str = dt.strftime('%H:%M')
                    except Exception:
                        pass
                message_text += f"  • {time_str} {summary}\n" if time_str else f"  • {summary}\n"
            message_text += "\n"
        
        # Добавляем информацию о невыполненных задачах
        if incomplete_events:
            message_text += "📋 Tasks to complete:\n"
        else:
            message_text += "🎉 All tasks completed! Great job!"
        
        # Создаем inline-клавиатуру для невыполненных задач (одна строка на задачу)
        keyboard = []
        tz = pytz.timezone(user_timezone)
        for event in incomplete_events:
            summary = event.get('summary', 'Task')
            event_id = event.get('id', '')
            if event_id:
                start_time = event.get('start_time', '')
                time_str = ""
                if start_time:
                    try:
                        if 'T' in start_time:
                            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                            if dt.tzinfo:
                                dt = dt.astimezone(tz)
                                time_str = dt.strftime('%H:%M')
                    except Exception:
                        pass

                label_text = f"{time_str} {summary}" if time_str else summary
                keyboard.extend(_build_task_row(event_id, label_text))
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )

    except Exception as e:
        print(f"[Bot] Ошибка при отображении задач на сегодня: {e}")
        await update.message.reply_text(
            "❌ An error occurred while loading tasks. Please try again.",
            reply_markup=build_main_menu()
        )


async def show_tasks_for_date(update: Update, context: ContextTypes.DEFAULT_TYPE, date_text: str):
    """Shows tasks for a user-specified date"""
    chat_id = update.effective_chat.id
    user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
    tz = pytz.timezone(user_timezone)
    now_local = datetime.now(tz)

    # Parse the date using AI or simple rules
    target_date = None
    date_text_lower = date_text.strip().lower()

    # Simple built-in parsing first
    if date_text_lower in ('today',):
        target_date = now_local.date()
    elif date_text_lower in ('tomorrow',):
        target_date = (now_local + timedelta(days=1)).date()
    else:
        # Day-of-week names
        day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        day_shorts = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
        matched_dow = None
        for i, (full, short) in enumerate(zip(day_names, day_shorts)):
            if date_text_lower == full or date_text_lower == short:
                matched_dow = i  # 0=Monday
                break
        if matched_dow is not None:
            current_dow = now_local.weekday()  # 0=Monday
            days_ahead = (matched_dow - current_dow) % 7
            target_date = (now_local + timedelta(days=days_ahead)).date()
        else:
            # Try ISO format YYYY-MM-DD
            try:
                target_date = datetime.strptime(date_text.strip(), '%Y-%m-%d').date()
            except ValueError:
                pass

        if target_date is None:
            # Fall back to AI parsing
            ai_parsed = await parse_with_ai(date_text, user_timezone)
            if ai_parsed and ai_parsed.get('start_time'):
                try:
                    dt = datetime.fromisoformat(ai_parsed['start_time'].replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        dt = pytz.utc.localize(dt)
                    target_date = dt.astimezone(tz).date()
                except Exception:
                    pass

    if target_date is None:
        await update.message.reply_text(
            "❌ Couldn't understand the date. Try: <b>tomorrow</b>, <b>Monday</b>, <b>2026-03-10</b>",
            parse_mode='HTML'
        )
        return

    # Clear state
    context.user_data.pop('waiting_for', None)

    # Get credentials
    stored_tokens = get_google_tokens(chat_id)
    if not stored_tokens:
        await update.message.reply_text(
            "❌ Please connect your Google Calendar first using /start",
            reply_markup=build_main_menu()
        )
        return

    credentials = await _get_credentials_or_notify(
        chat_id, stored_tokens,
        lambda t: update.message.reply_text(t, reply_markup=build_main_menu())
    )
    if not credentials:
        return

    try:
        events = get_events_for_date(credentials, user_timezone, target_date)

        date_label = target_date.strftime('%A, %B %d')
        if target_date == now_local.date():
            date_label = f"Today ({date_label})"
        elif target_date == (now_local + timedelta(days=1)).date():
            date_label = f"Tomorrow ({date_label})"

        if not events:
            await update.message.reply_text(
                f"📆 <b>{date_label}</b>\n\nNo tasks scheduled for this day! 🎉",
                reply_markup=build_main_menu(),
                parse_mode='HTML'
            )
            return

        completed_events = [e for e in events if e.get('summary', '').startswith('✅ ')]
        incomplete_events = [
            e for e in events
            if not e.get('summary', '').startswith('✅ ')
            and not e.get('summary', '').startswith('❌ ')
        ]

        message_text = f"📆 <b>{date_label}</b>\n\n"

        if completed_events:
            message_text += "✅ Completed:\n"
            for event in completed_events:
                summary = event.get('summary', 'Task')
                if summary.startswith('✅ '):
                    summary = summary[2:]
                start_time = event.get('start_time', '')
                time_str = ""
                if start_time and 'T' in start_time:
                    try:
                        dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                        if dt.tzinfo:
                            dt = dt.astimezone(tz)
                            time_str = dt.strftime('%H:%M')
                    except Exception:
                        pass
                if time_str:
                    message_text += f"  • {time_str} {summary}\n"
                else:
                    message_text += f"  • {summary}\n"
            message_text += "\n"

        if incomplete_events:
            message_text += "📋 Tasks to complete:\n"
        else:
            message_text += "🎉 All tasks completed!"

        keyboard = []
        for event in incomplete_events:
            event_id = event.get('id', '')
            if not event_id:
                continue
            summary = event.get('summary', 'Task')
            start_time = event.get('start_time', '')
            time_str = ""
            if start_time and 'T' in start_time:
                try:
                    dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    if dt.tzinfo:
                        dt = dt.astimezone(tz)
                        time_str = dt.strftime('%H:%M')
                except Exception:
                    pass
            label_text = f"{time_str} {summary}" if time_str else summary
            keyboard.extend(_build_task_row(event_id, label_text))

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else build_main_menu()
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )

    except Exception as e:
        print(f"[Bot] Error loading tasks for date: {e}")
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
    
    # Обработка игнорируемых кнопок (label buttons)
    if callback_data == 'ignore' or callback_data.startswith('label_'):
        await query.answer("")  # Тихий ответ, чтобы убрать loading
        return
    
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
        await query.answer("")
        await query.edit_message_text("🌍 Changing timezone...")
        await query.message.reply_text(
            "🌍 Share your location or enter timezone manually:",
            reply_markup=build_timezone_keyboard()
        )
        context.user_data['waiting_for'] = 'timezone'
        return

    elif callback_data == "set_morning":
        await query.answer("")
        await query.edit_message_text("🌅 Changing morning briefing time...")
        await query.message.reply_text(
            "🌅 At what time do you want to receive your Daily Plan?\n\n"
            "Send time in HH:MM format (e.g., 09:00):",
            reply_markup=build_morning_time_keyboard()
        )
        context.user_data['waiting_for'] = 'morning_time'
        return

    elif callback_data == "set_evening":
        await query.answer("")
        await query.edit_message_text("🌙 Changing evening recap time...")
        await query.message.reply_text(
            "🌙 When should I send you the Evening Recap?\n\n"
            "Send time in HH:MM format (e.g., 21:00):",
            reply_markup=build_evening_time_keyboard()
        )
        context.user_data['waiting_for'] = 'evening_time'
        return

    elif callback_data == "connect_gcal":
        await query.answer("")  # тихий ответ
        chat_id = query.message.chat_id
        # Формируем redirect_uri для callback.
        redirect_uri = os.getenv("REDIRECT_URI")
        if not redirect_uri:
            base_url = os.getenv("BASE_URL")
            if not base_url:
                port = int(os.getenv("PORT", 8000))
                base_url = f"http://localhost:{port}"
            redirect_uri = f"{base_url}/google/callback"

        auth_url = get_authorization_url(chat_id, redirect_uri)
        await query.edit_message_text(
            "To (re)connect your Google Calendar, click the link below:\n\n"
            f'<a href="{auth_url}">🔗 Connect Google Calendar</a>',
            parse_mode='HTML'
        )
        return

    elif callback_data == "disconnect_gcal":
        await query.answer("")  # тихий ответ
        chat_id = query.message.chat_id
        # Удаляем токены и помечаем, что онбординг по календарю больше не активен
        delete_google_tokens(chat_id)
        set_onboarded(chat_id, False)

        await query.edit_message_text(
            "🔌 Google Calendar has been disconnected.\n\n"
            "You can connect a new account at any time from Settings or by typing /start."
        )
        await query.message.reply_text("What would you like to do next?", reply_markup=build_main_menu())
        return

    elif callback_data == "set_duration":
        await query.answer("")
        use_default = get_use_default_duration(chat_id)
        default_dur = get_default_task_duration(chat_id)
        status = f"{default_dur} min default" if use_default else "ask each time"
        keyboard = [
            [InlineKeyboardButton("❓ Ask me each time", callback_data="duration_ask")],
            [
                InlineKeyboardButton("15 min", callback_data="duration_15"),
                InlineKeyboardButton("30 min", callback_data="duration_30"),
                InlineKeyboardButton("45 min", callback_data="duration_45"),
            ],
            [
                InlineKeyboardButton("1h", callback_data="duration_60"),
                InlineKeyboardButton("1.5h", callback_data="duration_90"),
                InlineKeyboardButton("2h", callback_data="duration_120"),
            ],
        ]
        await query.edit_message_text(
            f"⏱ Task Duration\nCurrent: {status}\n\n"
            "When a task has no specified duration, should I ask you or use a default?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    elif callback_data == "duration_ask":
        await query.answer("")
        set_default_duration_settings(chat_id, False, get_default_task_duration(chat_id))
        await query.edit_message_text("✅ I'll ask you for duration each time it's not specified.")
        return

    elif callback_data.startswith("duration_"):
        await query.answer("")
        try:
            mins = int(callback_data.split("_")[1])
        except (IndexError, ValueError):
            return
        set_default_duration_settings(chat_id, True, mins)
        if mins < 60:
            label = f"{mins} min"
        elif mins % 60 == 0:
            label = f"{mins // 60}h"
        else:
            label = f"{mins // 60}h {mins % 60}min"
        await query.edit_message_text(f"✅ Default task duration set to {label}.")
        return

    # Обработка подтверждения события из предпросмотра
    elif callback_data == "event_confirm":
        await query.answer("")  # тихий ответ
        chat_id = query.message.chat_id

        # Получаем сохраненные данные события
        event_data = context.user_data.get('pending_event_preview')
        source = context.user_data.get('pending_event_source', 'unknown')

        if not event_data:
            await query.edit_message_text("❌ Event data not found. Please try again.")
            return

        # Remove preview buttons immediately so user can't double-tap
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Очищаем состояние
        context.user_data.pop('pending_event_preview', None)
        context.user_data.pop('pending_event_source', None)
        context.user_data.pop('waiting_for', None)

        await create_calendar_event(update, context, event_data, source=source)
        track_event(chat_id, "event_preview_confirmed", {"source": source})
        return

    elif callback_data == "event_edit":
        await query.answer("")  # тихий ответ
        chat_id = query.message.chat_id
        
        await query.edit_message_text(
            "What would you like to edit?",
            reply_markup=build_edit_menu_buttons()
        )
        context.user_data['waiting_for'] = 'event_edit_choice'
        return

    # Обработка подтверждения расписания из предпросмотра
    elif callback_data == "schedule_confirm":
        await query.answer("")  # тихий ответ
        
        # Получаем сохраненные данные расписания
        schedule_data = context.user_data.get('pending_schedule_preview')
        source = context.user_data.get('pending_event_source', 'unknown')
        
        if not schedule_data:
            await query.edit_message_text("❌ Schedule data not found. Please try again.")
            return
        
        # Очищаем временные данные предпросмотра
        context.user_data.pop('pending_schedule_preview', None)
        context.user_data.pop('pending_event_source', None)
        context.user_data.pop('waiting_for', None)
        
        # Вызываем обработку импорта расписания (которая спросит о количестве недель)
        await handle_schedule_import(update, context, schedule_data, source=source)
        track_event(chat_id, "schedule_preview_confirmed", {"source": source})
        return

    elif callback_data == "schedule_cancel":
        await query.answer("")  # тихий ответ

        # Очищаем сохраненные данные
        context.user_data.pop('pending_schedule_preview', None)
        context.user_data.pop('pending_event_source', None)
        context.user_data.pop('waiting_for', None)

        await query.edit_message_text("❌ Schedule import cancelled.")
        await query.message.reply_text("What would you like to do next?", reply_markup=build_main_menu())
        return

    elif callback_data in ("schedule_weeks_force", "schedule_weeks_skip", "schedule_weeks_replace", "schedule_weeks_cancel"):
        await query.answer("")
        events_to_create = context.user_data.pop('pending_schedule_events', None)
        conflict_existing_ids = context.user_data.pop('pending_schedule_conflict_ids', [])
        context.user_data.pop('state', None)
        context.user_data.pop('pending_schedule', None)

        if callback_data == "schedule_weeks_cancel" or not events_to_create:
            await query.edit_message_text("❌ Schedule import cancelled.")
            await query.message.reply_text("What would you like to do next?", reply_markup=build_main_menu())
            return

        # Need credentials to create events
        stored_tokens = get_google_tokens(chat_id)
        if not stored_tokens:
            await query.edit_message_text("❌ Authorization error. Please reconnect your Google Calendar using /start")
            return
        credentials = await _get_credentials_or_notify(
            chat_id, stored_tokens,
            lambda t: query.edit_message_text(t)
        )
        if not credentials:
            return

        if callback_data == "schedule_weeks_replace":
            # Delete conflicting existing events, then import all new ones
            deleted = 0
            for eid in conflict_existing_ids:
                try:
                    ok = await asyncio.to_thread(cancel_event, credentials, eid)
                    if ok:
                        deleted += 1
                except Exception as e:
                    print(f"[Bot] Error deleting conflicting schedule event {eid}: {e}")
            events_created = await _execute_schedule_creation(credentials, events_to_create, include_conflicts=True)
            await query.edit_message_text(
                f"🔄 Replaced {deleted} old event(s). Created {events_created} new event(s)."
            )
        elif callback_data == "schedule_weeks_force":
            # Add both — import all new events without deleting old ones
            events_created = await _execute_schedule_creation(credentials, events_to_create, include_conflicts=True)
            await query.edit_message_text(
                f"✅ Schedule imported! Created {events_created} event(s) (kept existing ones too)."
            )
        else:
            # skip conflicts: re-check and skip new events that still conflict
            conflict_starts: set = set()
            try:
                from googleapiclient.discovery import build as gcal_build2
                service2 = gcal_build2('calendar', 'v3', credentials=credentials)
                range_start = events_to_create[0]["start_time"]
                range_end = events_to_create[-1]["end_time"]
                existing_result2 = service2.events().list(
                    calendarId='primary',
                    timeMin=range_start,
                    timeMax=range_end,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                schedule_existing2 = existing_result2.get('items', [])
                for ev in events_to_create:
                    new_start = datetime.fromisoformat(ev["start_time"].replace("Z", "+00:00"))
                    new_end = datetime.fromisoformat(ev["end_time"].replace("Z", "+00:00"))
                    if new_start.tzinfo is None:
                        new_start = pytz.utc.localize(new_start)
                    if new_end.tzinfo is None:
                        new_end = pytz.utc.localize(new_end)
                    for ex in schedule_existing2:
                        ex_s = (ex.get('start') or {}).get('dateTime') or (ex.get('start') or {}).get('date')
                        ex_e = (ex.get('end') or {}).get('dateTime') or (ex.get('end') or {}).get('date')
                        if not ex_s or not ex_e:
                            continue
                        try:
                            ex_start = datetime.fromisoformat(ex_s.replace("Z", "+00:00"))
                            ex_end = datetime.fromisoformat(ex_e.replace("Z", "+00:00"))
                            if ex_start.tzinfo is None:
                                ex_start = pytz.utc.localize(ex_start)
                            if ex_end.tzinfo is None:
                                ex_end = pytz.utc.localize(ex_end)
                        except Exception:
                            continue
                        if new_start < ex_end and new_end > ex_start:
                            conflict_starts.add(ev["start_time"])
                            break
            except Exception as e:
                print(f"[Bot] Error re-checking schedule conflicts: {e}")

            events_created = await _execute_schedule_creation(
                credentials, events_to_create, include_conflicts=False, conflict_starts=conflict_starts
            )
            await query.edit_message_text(
                f"✅ Schedule imported! Created {events_created} event(s) (conflicting slots skipped)."
            )

        track_event(chat_id, "schedule_imported", {"events_created": events_created})
        return

    # Обработка редактирования события
    elif callback_data == "edit_title":
        await query.answer("")  # тихий ответ
        await query.edit_message_text(
            "📋 Enter new event title:"
        )
        context.user_data['waiting_for'] = 'edit_event_title'
        return

    elif callback_data == "edit_location":
        await query.answer("")  # тихий ответ
        await query.edit_message_text(
            "📍 Enter new location (or send '-' to clear it):"
        )
        context.user_data['waiting_for'] = 'edit_event_location'
        return

    elif callback_data == "edit_time":
        await query.answer("")  # тихий ответ
        await query.edit_message_text(
            "🕐 Enter new time (e.g., 'Mon 21:00' or '14:30'):"
        )
        context.user_data['waiting_for'] = 'edit_event_time'
        return

    elif callback_data == "cancel_edit":
        await query.answer("")  # тихий ответ

        # Показываем предпросмотр еще раз
        event_data = context.user_data.get('pending_event_preview')
        if event_data:
            preview_text = format_event_preview(event_data)
            await query.edit_message_text(
                preview_text,
                parse_mode='HTML',
                reply_markup=build_event_preview_buttons()
            )
            context.user_data['waiting_for'] = 'event_confirmation'
        else:
            await query.edit_message_text("Event data not found.")
        return

    elif callback_data == "conflict_replace":
        await query.answer("")
        event_data = context.user_data.pop('pending_conflict_event', None)
        source = context.user_data.pop('pending_conflict_source', 'unknown')
        conflict_ids = context.user_data.pop('pending_conflict_ids', [])
        if not event_data:
            await query.edit_message_text("❌ Event data not found. Please try again.")
            return
        stored_tokens = get_google_tokens(chat_id)
        if not stored_tokens:
            await query.edit_message_text("❌ Authorization error. Please reconnect your Google Calendar using /start")
            return
        credentials = await _get_credentials_or_notify(
            chat_id, stored_tokens,
            lambda t: query.edit_message_text(t)
        )
        if not credentials:
            return
        # Delete the conflicting old events first
        for eid in conflict_ids:
            try:
                await asyncio.to_thread(cancel_event, credentials, eid)
            except Exception as e:
                print(f"[Bot] Error deleting conflicting event {eid}: {e}")
        await query.edit_message_text("🔄 Replacing old event(s)...")
        await _do_create_and_confirm(update, context, credentials, event_data, source)
        return

    elif callback_data == "conflict_proceed":
        await query.answer("")
        event_data = context.user_data.pop('pending_conflict_event', None)
        source = context.user_data.pop('pending_conflict_source', 'unknown')
        context.user_data.pop('pending_conflict_ids', None)
        if not event_data:
            await query.edit_message_text("❌ Event data not found. Please try again.")
            return
        stored_tokens = get_google_tokens(chat_id)
        if not stored_tokens:
            await query.edit_message_text("❌ Authorization error. Please reconnect your Google Calendar using /start")
            return
        credentials = await _get_credentials_or_notify(
            chat_id, stored_tokens,
            lambda t: query.edit_message_text(t)
        )
        if not credentials:
            return
        await _do_create_and_confirm(update, context, credentials, event_data, source)
        return

    elif callback_data == "conflict_change_time":
        await query.answer("")
        event_data = context.user_data.pop('pending_conflict_event', None)
        context.user_data.pop('pending_conflict_source', None)
        context.user_data.pop('pending_conflict_ids', None)
        if not event_data:
            await query.edit_message_text("❌ Event data not found. Please try again.")
            return
        # Go directly to time editing — skip the redundant preview step
        context.user_data['pending_event_preview'] = event_data
        context.user_data['waiting_for'] = 'edit_event_time'
        await query.edit_message_text(
            "🕐 Enter a new time for the event (e.g., 'Mon 21:00' or '14:30'):"
        )
        return

    elif callback_data == "conflict_cancel":
        await query.answer("")
        context.user_data.pop('pending_conflict_event', None)
        context.user_data.pop('pending_conflict_source', None)
        context.user_data.pop('pending_conflict_ids', None)
        await query.edit_message_text("🚫 Kept the existing event. New event was not added.")
        await query.message.reply_text("What would you like to do next?", reply_markup=build_main_menu())
        return

    elif callback_data == "cancel_task_duration":
        await query.answer("")
        context.user_data.pop('waiting_for', None)
        context.user_data.pop('pending_event_data', None)
        context.user_data.pop('pending_event_source', None)
        await query.edit_message_text("❌ Task creation cancelled.")
        await query.message.reply_text("What would you like to do next?", reply_markup=build_main_menu())
        return

    # Для остальных callback нужна авторизация Google Calendar
    # НЕ вызываем query.answer() здесь для callback, которые сами вызывают его позже:
    # - "done_*" и "already_done_*" - вызывают query.answer() в конце обработки
    # - "refresh_today" - вызывает query.answer() после обновления списка
    # - "reschedule_*" - вызывают query.answer() после обработки
    # - "confirm_move_*" - вызывают query.answer() после обработки
    # - "cancel_*" - вызывают query.answer() после обработки
    # - "reschedule_leftovers" - вызывает query.answer() после переноса задач
    # Вызываем только для других callback, которые не обрабатываются дальше
    if (not callback_data.startswith("done_") and 
        not callback_data.startswith("already_done_") and 
        callback_data != "refresh_today" and 
        not callback_data.startswith("resch_") and
        not callback_data.startswith("reschedule_") and
        not callback_data.startswith("confirm_move_") and
        not callback_data.startswith("cancel_") and
        not callback_data.startswith("del_") and
        not callback_data.startswith("delete_") and
        callback_data != "reschedule_leftovers"):
        await query.answer("")  # Убираем дублирование текста кнопки для других callback
    
    stored_tokens = get_google_tokens(chat_id)
    if not stored_tokens:
        await query.answer("")
        await query.edit_message_text(
            "❌ Please connect your Google Calendar first using /start"
        )
        return

    credentials = await _get_credentials_or_notify(
        chat_id, stored_tokens,
        lambda t: query.edit_message_text(t)
    )
    if not credentials:
        await query.answer("")
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
                    "📅 <b>Here are your tasks for today:</b>\n\n"
                    "No tasks scheduled for today! 🎉",
                    reply_markup=None,
                    parse_mode='HTML'
                )
                await query.answer("✅ List updated!")
                return

            # Разделяем выполненные и невыполненные задачи; скрываем отменённые (❌)
            completed_events = [e for e in events if e.get('summary', '').startswith('✅ ')]
            incomplete_events = [
                e for e in events
                if not e.get('summary', '').startswith('✅ ')
                and not e.get('summary', '').startswith('❌ ')
            ]
            
            # Формируем текст сообщения
            message_text = "📅 <b>Here are your tasks for today:</b>\n\n"
            
            # Добавляем выполненные задачи
            if completed_events:
                message_text += "✅ Completed:\n"
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
                                    time_str = dt.strftime('%H:%M')
                        except Exception:
                            pass
                    message_text += f"  • {time_str} {summary}\n" if time_str else f"  • {summary}\n"
                message_text += "\n"
            
            # Добавляем секцию невыполненных задач
            if incomplete_events:
                message_text += "📋 Tasks to complete:\n"
            else:
                message_text += "🎉 All tasks completed! Great job!"

            # Создаем клавиатуру для невыполненных задач (одна строка на задачу)
            keyboard = []
            tz_obj = pytz.timezone(user_timezone)
            for event in incomplete_events:
                summary = event.get('summary', 'Task')
                event_id = event.get('id', '')
                if event_id:
                    start_time = event.get('start_time', '')
                    time_str = ""
                    if start_time:
                        try:
                            if 'T' in start_time:
                                dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                                if dt.tzinfo:
                                    dt = dt.astimezone(tz_obj)
                                    time_str = dt.strftime('%H:%M')
                        except Exception:
                            pass
                    label_text = f"{time_str} {summary}" if time_str else summary
                    keyboard.extend(_build_task_row(event_id, label_text))
            
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
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
                # Обновляем UI на месте - удаляем строки с кнопками для этой задачи и обновляем текст
                message_text = query.message.text or ""
                
                # Определяем тип сообщения
                is_evening_recap = "hope it was a productive day" in message_text
                is_tasks_today = "Here are your tasks" in message_text or "Mark what you've already done" in message_text
                
                if is_evening_recap or is_tasks_today:
                    # Получаем обновленный список событий
                    user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
                    events = get_today_events(credentials, user_timezone)
                    completed_events = [e for e in events if e.get('summary', '').startswith('✅ ')]
                    incomplete_events = [
                        e for e in events
                        if not e.get('summary', '').startswith('✅ ')
                        and not e.get('summary', '').startswith('❌ ')
                    ]
                    
                    # Пересоздаем текст сообщения
                    if is_evening_recap:
                        new_message_text = "Hey, hope it was a productive day!\n\n"
                    else:
                        new_message_text = "📅 <b>Here are your tasks for today:</b>\n\n"
                    
                    # Добавляем выполненные задачи
                    if completed_events:
                        tz = pytz.timezone(user_timezone)
                        new_message_text += "✅ Completed:\n"
                        for event in completed_events:
                            summary = event.get('summary', 'Task')
                            if summary.startswith('✅ '):
                                summary = summary[2:]
                            start_time = event.get('start_time', '')
                            time_str = ""
                            if start_time:
                                try:
                                    if 'T' in start_time:
                                        dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                                        if dt.tzinfo:
                                            dt = dt.astimezone(tz)
                                            time_str = dt.strftime('%H:%M')
                                except Exception:
                                    pass
                            new_message_text += f"  • {time_str} {summary}\n" if time_str else f"  • {summary}\n"
                        new_message_text += "\n"
                    
                    # Добавляем информацию о невыполненных задачах
                    if incomplete_events:
                        if is_evening_recap:
                            new_message_text += "📋 Tasks left behind:\n"
                        else:
                            new_message_text += "📋 Tasks to complete:\n"
                    else:
                        new_message_text += "🎉 All tasks completed! Great job!"
                    
                    # Пересоздаем клавиатуру для оставшихся задач (одна строка на задачу)
                    new_keyboard = []
                    tz = pytz.timezone(user_timezone)
                    for evt in incomplete_events:
                        evt_summary = evt.get('summary', 'Task')
                        event_id_item = evt.get('id', '')
                        if event_id_item:
                            start_time = evt.get('start_time', '')
                            time_str = ""
                            if start_time:
                                try:
                                    if 'T' in start_time:
                                        dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                                        if dt.tzinfo:
                                            dt = dt.astimezone(tz)
                                            time_str = dt.strftime('%H:%M')
                                except Exception:
                                    pass
                            label_text = f"{time_str} {evt_summary}" if time_str else evt_summary
                            new_keyboard.extend(_build_task_row(event_id_item, label_text))
                    
                    new_markup = InlineKeyboardMarkup(new_keyboard) if new_keyboard else None
                    await query.edit_message_text(
                        new_message_text,
                        reply_markup=new_markup,
                        parse_mode='HTML' if "<b>" in new_message_text else None
                    )
                else:
                    inline_keyboard = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
                    new_keyboard = _remove_task_row(inline_keyboard, event_id)
                    new_markup = InlineKeyboardMarkup(new_keyboard) if new_keyboard else None
                    await query.edit_message_reply_markup(reply_markup=new_markup)
                
                await query.answer("✅ Task marked as completed!")
                track_event(chat_id, "task_marked_done", {"event_id": event_id})
            else:
                await query.answer("❌ Failed to mark task as done. Please try again.", show_alert=True)
                
        except Exception as e:
            print(f"[Bot] Ошибка при отметке задачи как выполненной: {e}")
            await query.answer("❌ An error occurred. Please try again.", show_alert=True)
            track_event(chat_id, "error", {"error_type": "mark_task_done", "error_message": str(e)[:100]})
    
    # Обработка подтверждения переноса на предложенный слот (должен быть перед reschedule_manual_)
    elif callback_data.startswith("confirm_move_"):
        # Формат: confirm_move_{event_id}|{timestamp}
        # Используем | как разделитель, так как event_id может содержать underscores
        prefix = "confirm_move_"
        if len(callback_data) > len(prefix):
            remaining = callback_data[len(prefix):]
            # Разделяем на event_id и timestamp по |
            if "|" in remaining:
                parts = remaining.split("|", 1)
                if len(parts) == 2:
                    event_id = parts[0]
                    timestamp_str = parts[1]
                else:
                    event_id = None
                    timestamp_str = None
            else:
                event_id = None
                timestamp_str = None
        else:
            event_id = None
            timestamp_str = None
        
        if event_id and timestamp_str:
            try:
                from googleapiclient.discovery import build
                
                # Восстанавливаем datetime из timestamp
                try:
                    timestamp_int = int(timestamp_str)
                    suggested_time = datetime.fromtimestamp(timestamp_int, tz=pytz.utc)
                except (ValueError, OSError) as e:
                    print(f"[Bot] Invalid timestamp in confirm_move: {timestamp_str}, error: {e}")
                    await query.answer("❌ Invalid timestamp. Please try rescheduling again.", show_alert=True)
                    return
                
                user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
                tz = pytz.timezone(user_timezone)
                suggested_time = suggested_time.astimezone(tz)
                
                # Получаем событие для вычисления длительности
                service = build('calendar', 'v3', credentials=credentials)
                event = service.events().get(calendarId='primary', eventId=event_id).execute()
                
                # Вычисляем длительность
                start_str = event['start'].get('dateTime', event['start'].get('date'))
                end_str = event['end'].get('dateTime', event['end'].get('date'))
                
                if 'T' in start_str:
                    orig_start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                    if orig_start_dt.tzinfo is None:
                        orig_start_dt = pytz.utc.localize(orig_start_dt)
                    orig_end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                    if orig_end_dt.tzinfo is None:
                        orig_end_dt = pytz.utc.localize(orig_end_dt)
                    duration = orig_end_dt - orig_start_dt
                else:
                    duration = timedelta(hours=1)
                
                new_end_dt = suggested_time + duration
                
                # Переносим событие
                new_start_utc = suggested_time.astimezone(pytz.utc)
                new_end_utc = new_end_dt.astimezone(pytz.utc)
                
                success = reschedule_event(credentials, event_id, new_start_utc, new_end_utc)
                
                if success:
                    inline_keyboard = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
                    new_keyboard = _remove_task_row(inline_keyboard, event_id)
                    message_text = query.message.text or ""
                    user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
                    tz_local = pytz.timezone(user_timezone)
                    now_local = datetime.now(tz_local)
                    time_str = suggested_time.strftime('%H:%M')
                    date_str = suggested_time.strftime('%Y-%m-%d')
                    if date_str == now_local.strftime('%Y-%m-%d'):
                        time_display = f"today at {time_str}"
                    elif date_str == (now_local + timedelta(days=1)).strftime('%Y-%m-%d'):
                        time_display = f"tomorrow at {time_str}"
                    else:
                        time_display = f"{suggested_time.strftime('%B %d')} at {time_str}"
                    message_text += f"\n\n✅ Moved to {time_display}"
                    new_markup = InlineKeyboardMarkup(new_keyboard) if new_keyboard else None
                    await query.edit_message_text(message_text, reply_markup=new_markup)
                    await query.answer("✅ Task moved!")
                    track_event(chat_id, "task_rescheduled_smart", {"event_id": event_id})
                else:
                    await query.answer("❌ Failed to reschedule. Please try again.", show_alert=True)
                    
            except Exception as e:
                print(f"[Bot] Ошибка при подтверждении переноса задачи: {e}")
                await query.answer("❌ An error occurred. Please try again.", show_alert=True)
                track_event(chat_id, "error", {"error_type": "confirm_reschedule", "error_message": str(e)[:100]})
        else:
            await query.answer("❌ Invalid confirmation data.", show_alert=True)
    
    # Обработка ручного ввода времени для переноса (должен быть перед общим reschedule_)
    elif callback_data.startswith("reschedule_manual_"):
        event_id = callback_data[18:]  # Убираем префикс "reschedule_manual_"

        context.user_data['rescheduling_event_id'] = event_id
        context.user_data['waiting_for'] = 'reschedule_time'

        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel reschedule", callback_data=f"cancel_reschedule_{event_id}")]
        ])
        prompt_msg = await query.message.reply_text(
            "📅 For what time to reschedule?\n\n"
            "Examples: <b>today 18:00</b>, <b>tomorrow 10:00</b>, <b>wed 14:30</b>, <b>15:00</b>",
            reply_markup=cancel_keyboard,
            parse_mode='HTML'
        )
        context.user_data['reschedule_prompt_msg_id'] = prompt_msg.message_id
        await query.answer("")

    # Обработка переноса задачи (resch_ or legacy reschedule_)
    elif callback_data.startswith("resch_") or (callback_data.startswith("reschedule_") and not callback_data.startswith("reschedule_manual_") and not callback_data.startswith("reschedule_leftovers")):
        event_id = callback_data[6:] if callback_data.startswith("resch_") else callback_data[11:]

        context.user_data['rescheduling_event_id'] = event_id
        context.user_data['waiting_for'] = 'reschedule_time'

        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel reschedule", callback_data=f"cancel_reschedule_{event_id}")]
        ])
        prompt_msg = await query.message.reply_text(
            "📅 For what time to reschedule?\n\n"
            "Examples: <b>today 18:00</b>, <b>tomorrow 10:00</b>, <b>wed 14:30</b>, <b>15:00</b>",
            reply_markup=cancel_keyboard,
            parse_mode='HTML'
        )
        context.user_data['reschedule_prompt_msg_id'] = prompt_msg.message_id
        await query.answer("")
    
    # Обработка удаления задачи (del_ or legacy delete_)
    elif callback_data.startswith("del_") or callback_data.startswith("delete_"):
        event_id = callback_data[4:] if callback_data.startswith("del_") else callback_data[7:]
        
        try:
            success = cancel_event(credentials, event_id)
            
            if success:
                inline_keyboard = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
                new_keyboard = _remove_task_row(inline_keyboard, event_id)
                new_markup = InlineKeyboardMarkup(new_keyboard) if new_keyboard else None
                await query.edit_message_reply_markup(reply_markup=new_markup)
                await query.answer("✅ Task deleted!")
                track_event(chat_id, "task_deleted", {"event_id": event_id})
            else:
                await query.answer("❌ Failed to delete task. Please try again.", show_alert=True)
                
        except Exception as e:
            print(f"[Bot] Ошибка при удалении задачи: {e}")
            await query.answer("❌ An error occurred. Please try again.", show_alert=True)
            track_event(chat_id, "error", {"error_type": "delete_task", "error_message": str(e)[:100]})
    
    # Отмена операции переноса (не удаляет задачу)
    elif callback_data.startswith("cancel_reschedule_"):
        # Clear all reschedule state
        await _clear_reschedule_prompt(context, chat_id)
        context.user_data.pop('waiting_for', None)
        context.user_data.pop('rescheduling_event_id', None)
        context.user_data.pop('reschedule_conflict_start', None)
        await query.edit_message_text("❌ Reschedule cancelled.")
        await query.message.reply_text("What would you like to do next?", reply_markup=build_main_menu())
        await query.answer("")

    # Обработка отмены задачи (cancel_ - для обратной совместимости)
    elif callback_data.startswith("cancel_"):
        event_id = callback_data[7:]  # Убираем префикс "cancel_"
        
        try:
            success = cancel_event(credentials, event_id)
            
            if success:
                inline_keyboard = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
                new_keyboard = _remove_task_row(inline_keyboard, event_id)
                new_markup = InlineKeyboardMarkup(new_keyboard) if new_keyboard else None
                await query.edit_message_reply_markup(reply_markup=new_markup)
                await query.answer("✅ Task cancelled!")
                track_event(chat_id, "task_cancelled", {"event_id": event_id})
            else:
                await query.answer("❌ Failed to cancel task. Please try again.", show_alert=True)
                
        except Exception as e:
            print(f"[Bot] Ошибка при отмене задачи: {e}")
            await query.answer("❌ An error occurred. Please try again.", show_alert=True)
            track_event(chat_id, "error", {"error_type": "cancel_task", "error_message": str(e)[:100]})
    
    # Обработка переноса остатка задач на завтра
    elif callback_data == "reschedule_leftovers":
        try:
            user_timezone = get_user_timezone(chat_id) or DEFAULT_TZ
            tz = pytz.timezone(user_timezone)
            now_local = datetime.now(tz)
            
            # Получаем события на сегодня
            events = get_today_events(credentials, user_timezone)
            
            # Фильтруем невыполненные (без "✅") и не отменённые (без "❌")
            incomplete_events = [
                e for e in events
                if not e.get('summary', '').startswith('✅ ')
                and not e.get('summary', '').startswith('❌ ')
            ]
            
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
                        # Для timed событий конвертируем в локальный timezone и добавляем один день
                        start_dt_local = start_dt.astimezone(tz)
                        new_start = start_dt_local + timedelta(days=1)
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
                    f"✅ Rescheduled {rescheduled_count} task(s) to tomorrow."
                )
                await query.answer("✅ Done!")
                track_event(chat_id, "tasks_rescheduled", {"count": rescheduled_count})
            else:
                await query.answer("❌ Failed to reschedule tasks. Please try again.", show_alert=True)
                
        except Exception as e:
            print(f"[Bot] Ошибка при переносе задач на завтра: {e}")
            await query.answer("❌ An error occurred. Please try again.", show_alert=True)
            track_event(chat_id, "error", {"error_type": "reschedule_tasks", "error_message": str(e)[:100]})


def _check_event_conflicts(credentials, event_start_utc: datetime, event_end_utc: datetime, exclude_event_id: str = None) -> List[Dict]:
    """
    Checks if an event conflicts with existing events.
    
    Args:
        credentials: Google Calendar credentials
        event_start_utc: Event start time (UTC)
        event_end_utc: Event end time (UTC)
        exclude_event_id: Event ID to exclude from conflict check (optional)
    
    Returns:
        List of conflicting events (empty if no conflicts)
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        
        # Refresh credentials if needed
        if credentials.expired:
            credentials.refresh(Request())
        
        service = build('calendar', 'v3', credentials=credentials)
        
        # Query for events in the time range
        events_result = service.events().list(
            calendarId='primary',
            timeMin=event_start_utc.isoformat(),
            timeMax=event_end_utc.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        conflicts = []
        for event in events:
            if exclude_event_id and event.get('id') == exclude_event_id:
                continue
            
            event_start = event.get('start', {})
            event_end = event.get('end', {})
            
            # Handle both dateTime and date formats
            event_start_iso = event_start.get('dateTime') or event_start.get('date')
            event_end_iso = event_end.get('dateTime') or event_end.get('date')
            
            if not event_start_iso or not event_end_iso:
                continue
            
            try:
                # Handle all-day events (date-only, no 'T' in the string)
                if 'T' not in event_start_iso:
                    # All-day event: parse as midnight UTC so it gets a proper timezone
                    e_start = pytz.utc.localize(datetime.strptime(event_start_iso, '%Y-%m-%d'))
                    e_end = pytz.utc.localize(datetime.strptime(event_end_iso, '%Y-%m-%d'))
                else:
                    e_start = datetime.fromisoformat(event_start_iso.replace('Z', '+00:00'))
                    e_end = datetime.fromisoformat(event_end_iso.replace('Z', '+00:00'))
                    if e_start.tzinfo is None:
                        e_start = pytz.utc.localize(e_start)
                    if e_end.tzinfo is None:
                        e_end = pytz.utc.localize(e_end)

                # Check for overlap
                if e_start < event_end_utc and e_end > event_start_utc:
                    conflicts.append({
                        'id': event.get('id'),
                        'summary': event.get('summary', 'Event'),
                        'start': e_start,
                        'end': e_end
                    })
            except Exception:
                continue
        
        return conflicts
    except Exception as e:
        print(f"[Bot] Error checking event conflicts: {e}")
        return []


async def create_calendar_event(update: Update, context: ContextTypes.DEFAULT_TYPE, event_data: Dict, source: str):
    """Создает событие в Google Calendar"""
    chat_id = update.effective_chat.id
    reply_fn = update.effective_message.reply_text

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

        redirect_uri = os.getenv("REDIRECT_URI")
        if not redirect_uri:
            base_url = os.getenv("BASE_URL")
            if not base_url:
                port = int(os.getenv("PORT", 8000))
                base_url = f"http://localhost:{port}"
            redirect_uri = f"{base_url}/google/callback"

        auth_url = get_authorization_url(chat_id, redirect_uri)
        print(f"[Bot] Отправляем ссылку на авторизацию Google Calendar для chat_id={chat_id}")
        await reply_fn(
            f"🔗 Please connect your Google Calendar first:\n\n"
            f'<a href="{auth_url}">🔗 Connect Google Calendar</a>',
            reply_markup=build_main_menu(),
            parse_mode='HTML'
        )
        return

    # Получаем credentials
    stored_tokens = get_google_tokens(chat_id)
    if not stored_tokens:
        await reply_fn(
            "❌ Authorization error. Please reconnect your Google Calendar using /start",
            reply_markup=build_main_menu()
        )
        return

    credentials = await _get_credentials_or_notify(
        chat_id, stored_tokens,
        lambda t: reply_fn(t, reply_markup=build_main_menu())
    )
    if not credentials:
        return

    # Check for conflicts with existing events
    try:
        start_dt = datetime.fromisoformat(event_data["start_time"].replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(event_data["end_time"].replace("Z", "+00:00"))
        if start_dt.tzinfo is None:
            start_dt = pytz.utc.localize(start_dt)
        if end_dt.tzinfo is None:
            end_dt = pytz.utc.localize(end_dt)

        conflicts = await asyncio.to_thread(_check_event_conflicts, credentials, start_dt, end_dt)
        if conflicts:
            user_tz = get_user_timezone(chat_id) or DEFAULT_TZ
            tz = pytz.timezone(user_tz)
            lines = []
            for c in conflicts[:3]:
                c_start = c['start'].astimezone(tz)
                c_end = c['end'].astimezone(tz)
                lines.append(f"• {c_start.strftime('%a %d %b %H:%M')}–{c_end.strftime('%H:%M')} {c['summary']}")
            if len(conflicts) > 3:
                lines.append(f"• ... and {len(conflicts) - 3} more")

            context.user_data['pending_conflict_event'] = event_data
            context.user_data['pending_conflict_source'] = source
            context.user_data['pending_conflict_ids'] = [c['id'] for c in conflicts if c.get('id')]

            conflict_text = "⚠️ This event overlaps with existing event(s):\n" + "\n".join(lines)
            conflict_text += "\n\nWhat would you like to do?"

            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Replace old", callback_data="conflict_replace"),
                    InlineKeyboardButton("➕ Add both", callback_data="conflict_proceed"),
                ],
                [
                    InlineKeyboardButton("🚫 Keep old", callback_data="conflict_cancel"),
                    InlineKeyboardButton("✏️ Change time", callback_data="conflict_change_time"),
                ],
            ])
            await reply_fn(conflict_text, reply_markup=markup)
            return
    except Exception as e:
        print(f"[Bot] Error checking conflicts before event creation: {e}")
        # Continue with creation even if conflict check fails

    # Создаем событие
    await _do_create_and_confirm(update, context, credentials, event_data, source)


async def _do_create_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, credentials, event_data: Dict, source: str):
    """Actually creates the calendar event and sends confirmation."""
    chat_id = update.effective_chat.id
    reply_fn = update.effective_message.reply_text

    event_url = create_event(credentials, event_data)

    if event_url:
        # Успешно создано
        track_event(chat_id, "calendar_event_created", {
            "source": source,
            "summary": event_data.get("summary", "")[:50]
        })

        tz = get_user_timezone(chat_id) or DEFAULT_TZ
        start_dt = datetime.fromisoformat(event_data["start_time"].replace("Z", "+00:00"))
        # Убеждаемся, что timezone установлен правильно
        if start_dt.tzinfo is None:
            start_dt = pytz.utc.localize(start_dt)
        start_local = start_dt.astimezone(pytz.timezone(tz))

        await reply_fn(
            f"✅ Event added: {event_data.get('summary', 'Task')} on {start_local.strftime('%a %d %b')} at {start_local.strftime('%H:%M')}",
            reply_markup=build_main_menu()
        )
    else:
        track_event(chat_id, "error", {"error_type": "calendar_event_creation_failed"})
        await reply_fn(
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
        now_utc = datetime.now(pytz.utc)
        stale_threshold_seconds = 180  # treat lock as stale after 3 minutes

        cur.execute("SELECT holder, acquired_utc FROM app_lock WHERE id=1")
        row = cur.fetchone()

        if row is None:
            cur.execute("INSERT INTO app_lock (id, holder, acquired_utc) VALUES (1, ?, ?)",
                        (holder, now_utc.isoformat()))
            con.commit()
        elif row[0] == holder:
            # Same identity restarting — just refresh the timestamp
            cur.execute("UPDATE app_lock SET acquired_utc=? WHERE id=1", (now_utc.isoformat(),))
            con.commit()
        else:
            # Different holder — check if the lock is stale
            stale = True
            try:
                lock_time = datetime.fromisoformat(row[1])
                if lock_time.tzinfo is None:
                    lock_time = pytz.utc.localize(lock_time)
                stale = (now_utc - lock_time).total_seconds() > stale_threshold_seconds
            except Exception:
                pass  # unparseable timestamp → treat as stale

            if stale:
                print(f"[singleton-sqlite] Stale lock from '{row[0]}' (age>{stale_threshold_seconds}s), taking over.")
                cur.execute("UPDATE app_lock SET holder=?, acquired_utc=? WHERE id=1",
                            (holder, now_utc.isoformat()))
                con.commit()
            else:
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
            
            try:
                chat_id = int(state)
            except (ValueError, TypeError):
                return web.Response(
                    text="Error: Invalid state parameter",
                    status=400
                )
            # Формируем redirect_uri: сначала пробуем REDIRECT_URI, иначе BASE_URL/google/callback
            redirect_uri = os.getenv("REDIRECT_URI")
            if not redirect_uri:
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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_message))
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
