# bot.py
"""
Telegram Task Tracker Bot
Авто-TZ · Гибкий парсер · Напоминания · Онбординг по одному вопросу · Мультиязык (RU/EN)

Обновления:
- Автоопределение таймзоны по геолокации (timezonefinder)
- Персистентный флаг завершения онбординга (исключает зацикливание)
- Главное меню: [Сегодня] [Список на дату] [Настройки]
- Подменю Настройки: [Время сводки] [Напоминания] [Время напоминания] [Таймзона] [Язык] [Назад]
"""

import os
import re
import sqlite3
import time as time_module
from datetime import datetime, time, timedelta
from typing import Optional, Tuple, List, Dict

import pytz
from dateparser.search import search_dates

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import Conflict

# ---- timezonefinder (pure Python) ----
try:
    from timezonefinder import TimezoneFinder
except Exception:
    TimezoneFinder = None

# ----------------- Config -----------------

DB_PATH = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Rome")
DEFAULT_SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "8"))
DEFAULT_SUMMARY_MINUTE = int(os.getenv("SUMMARY_MINUTE", "0"))
DEFAULT_REMIND_MIN = int(os.getenv("REMIND_MINUTES", "30"))
DEFAULT_REMINDERS_ENABLED = int(os.getenv("REMINDERS_ENABLED", "1"))  # 1=on,0=off
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "ru")  # 'ru' or 'en'

TF = None  # lazy TimezoneFinder singleton


# ----------------- i18n -----------------

MESSAGES: Dict[str, Dict[str, str]] = {
    "ru": {
        "welcome": (
            "Привет! Возможно я самый простой таск-трекер, которым ты когда-либо пользовался.\n"
            "Выбери язык ниже и начнём.\n\n"
            "Hi! I might be the simplest task tracker you've ever used.\n"
            "Pick a language below and let's start."
        ),
        "choose_lang_prompt": "👉 Выбери язык:",
        "lang_saved": "Готово! Язык: Русский.",
        "intro_mechanics": (
            "Как я работаю:\n"
            "📜 Пиши задачи обычным текстом — я понимаю даты и время в свободном формате.\n"
            "📜 Если пишешь без даты/времени — добавлю в список на сегодня.\n"
            "📜 Для задач со временем шлются напоминания заранее — как настроишь.\n"
            "📜 Каждое утро присылаю список дел на день.\n\n"
            "Готов?"
        ),
        "ask_tz": (
            "Введи часовой пояс вручную в формате Continent/City, например: Europe/Rome.\n"
            "Если не хочешь делиться гео — просто напиши таймзону."
        ),
        "ask_reminder_lead": (
            "За сколько времени до задачи присылать напоминание?\n"
            "Напиши, например: 15 мин, 30 мин, 1 ч. Если не нужны — ответь «нет»."
        ),
        "ask_summary_time": (
            "Во сколько присылать утренний список дел? Введи время HH:MM, например 09:00."
        ),
        "setup_done_title": "Готово! Всё настроено ✅",
        "setup_done_body": (
            "Главное меню:\n"
            "• Сегодня\n"
            "• Список на дату\n"
            "• Настройки\n\n"
            "В Настройках:\n"
            "• Время сводки — во сколько присылать ежедневный список\n"
            "• Напоминания — включить/выключить\n"
            "• Время напоминания — за сколько минут напоминать\n"
            "• Таймзона — обновить часовой пояс\n"
            "• Язык — сменить язык\n\n"
            "💡 В задачах используй двоеточие для времени (16:30), а точку или слэш для даты (31.08, 31/08)."
        ),
        "help": (
            "Главное меню:\n"
            "• Сегодня — список на сегодня\n"
            "• Список на дату — задачи на выбранную дату\n"
            "• Настройки — открыть параметры\n\n"
            "В Настройках:\n"
            "• Время сводки\n"
            "• Напоминания (on/off)\n"
            "• Время напоминания\n"
            "• Таймзона\n"
            "• Язык\n\n"
            "Подсказка: время — с : , дата — с . или /"
        ),
        "state_summary": (
            "Текущий часовой пояс: {tz}\n"
            "Присылать список: {hh:02d}:{mm:02d}\n"
            "Напоминания: {rem} за {lead} мин"
        ),
        "daily_set": "Список будет приходить в {hh:02d}:{mm:02d} по {tz}.",
        "remind_set": "Напоминания будут приходить за {lead} мин до задачи.",
        "reminders_on": "Напоминания: включены.",
        "reminders_off": "Напоминания: выключены.",
        "tz_updated": "Часовой пояс обновлён: {tz}.",
        "tz_geo_prompt": "Поделись геолокацией, чтобы выставить часовой пояс автоматически.",
        "tz_geo_fail": "Не удалось определить часовой пояс.",
        "added_today_nodt": "Добавил на сегодня: {text}",
        "added_task": "Ок, добавил: {text}\nНа {date}{when}",
        "today_list": "Задачи на сегодня ({date}):\n{list}",
        "on_list": "Задачи на {date}:\n{list}",
        "empty": "Пока пусто",
        "reminder": "⏰ Напоминание: {text}\nВ {time}",
        "summary": "Доброе утро! Вот план на сегодня ({date}):\n{list}",
        "format_list": "Формат даты: ДД.ММ (например 31.08) или ДД/ММ.",
        "time_invalid": "Время некорректно. Пример: 09:30.",
        "dt_invalid_strict": "Дата/время некорректны. Пиши время с : (например 14:30) и дату с . или / (например 31.08).",
        "lead_invalid": "Не понял длительность. Примеры: 15 мин, 1 ч, 30 м, 2 часа, нет.",
        "range_invalid": "Значение вне диапазона (0..1440).",
        "tz_invalid": "Не знаю такой зоны. Пример: Europe/Rome.",
        "tip_setup": "Подсказка: кнопки меню помогут настроить всё без команд.",
    },
    "en": {
        "welcome": (
            "Hi! I might be the simplest task tracker you've ever used.\n"
            "Pick a language below and let's start.\n\n"
            "Привет! Возможно я самый простой таск-трекер, которым ты когда-либо пользовался.\n"
            "Выбери язык ниже и начнём."
        ),
        "choose_lang_prompt": "👉 Choose your language:",
        "lang_saved": "Done! Language: English.",
        "intro_mechanics": (
            "How I work:\n"
            "📜 Send tasks as plain text — I parse dates & times naturally.\n"
            "📜 If there's no date/time — I'll add it for today.\n"
            "📜 Tasks with time get advance reminders (configurable).\n"
            "📜 Every morning you'll get the daily list.\n\n"
            "Ready?"
        ),
        "ask_tz": (
            "Set your timezone manually, e.g. Europe/Rome.\n"
            "If you don't want to share location, just type the zone."
        ),
        "ask_reminder_lead": (
            "How long before a task should I remind you?\n"
            "Type e.g. 15 min, 30 min, 1 h. If you don't want reminders — reply no."
        ),
        "ask_summary_time": (
            "When should I send the morning list? Enter time HH:MM, e.g. 09:00."
        ),
        "setup_done_title": "All set! ✅",
        "setup_done_body": (
            "Main menu:\n"
            "• Today\n"
            "• List by date\n"
            "• Settings\n\n"
            "In Settings:\n"
            "• Summary time — daily summary time\n"
            "• Reminders — enable/disable\n"
            "• Reminder time — minutes before task\n"
            "• Timezone — update zone\n"
            "• Language — change language\n\n"
            "💡 Use : for time (14:30) and . or / for dates (31.08 or 31/08)."
        ),
        "help": (
            "Main menu:\n"
            "• Today — today's tasks\n"
            "• List by date — pick a date\n"
            "• Settings — open preferences\n\n"
            "Settings:\n"
            "• Summary time\n"
            "• Reminders (on/off)\n"
            "• Reminder time\n"
            "• Timezone\n"
            "• Language\n\n"
            "Tip: use : for time; use . or / for dates."
        ),
        "state_summary": (
            "Timezone: {tz}\n"
            "Summary: {hh:02d}:{mm:02d}\n"
            "Reminders: {rem}, lead {lead} min"
        ),
        "daily_set": "Daily summary at {hh:02d}:{mm:02d} ({tz}).",
        "remind_set": "Reminders will arrive {lead} minutes before a task.",
        "reminders_on": "Reminders: enabled.",
        "reminders_off": "Reminders: disabled.",
        "tz_updated": "Timezone updated: {tz}.",
        "tz_geo_prompt": "Share your location to set your timezone automatically.",
        "tz_geo_fail": "Couldn't determine timezone.",
        "added_today_nodt": "Added for today: {text}",
        "added_task": "Done: {text}\nFor {date}{when}",
        "today_list": "Today's tasks ({date}):\n{list}",
        "on_list": "Tasks for {date}:\n{list}",
        "empty": "Nothing yet",
        "reminder": "⏰ Reminder: {text}\nAt {time}",
        "summary": "Good morning! Here's your plan for today ({date}):\n{list}",
        "format_list": "Date format: DD.MM (e.g., 31.08) or DD/MM.",
        "time_invalid": "Invalid time. Example: 09:30.",
        "dt_invalid_strict": "Invalid date/time. Use : for time (e.g. 14:30) and . or / for dates (e.g. 31.08).",
        "lead_invalid": "Couldn't parse duration. Examples: 15 min, 1 h, 30 m, 2 h, no.",
        "range_invalid": "Value out of range (0..1440).",
        "tz_invalid": "Unknown zone. Example: Europe/London.",
        "tip_setup": "Tip: Use the buttons to configure everything without commands.",
    },
}

LANG_BTNS = [["Русский", "English"]]

# ---- Меню ----
MAIN_MENU = {
    "ru": [["Сегодня"], ["Список на дату"], ["Настройки"]],
    "en": [["Today"], ["List by date"], ["Settings"]],
}
SETTINGS_MENU = {
    "ru": [["Время сводки"], ["Напоминания", "Время напоминания"], ["Таймзона", "Язык"], ["⬅️ Назад"]],
    "en": [["Summary time"], ["Reminders", "Reminder time"], ["Timezone", "Language"], ["⬅️ Back"]],
}

def build_main_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MAIN_MENU.get(lang, MAIN_MENU[DEFAULT_LANG]), resize_keyboard=True)

def build_settings_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(SETTINGS_MENU.get(lang, SETTINGS_MENU["ru"]), resize_keyboard=True)


def T(lang: str, key: str, **kwargs) -> str:
    d = MESSAGES.get(lang, MESSAGES[DEFAULT_LANG])
    s = d.get(key, key)
    return s.format(**kwargs) if kwargs else s


# ----------------- Storage -----------------

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            due_utc TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            all_day INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL,
            daily_hour INTEGER NOT NULL,
            daily_minute INTEGER NOT NULL,
            remind_lead_min INTEGER NOT NULL,
            reminders_enabled INTEGER NOT NULL,
            prefer_no_dt_today INTEGER NOT NULL DEFAULT 1,
            lang TEXT NOT NULL DEFAULT 'ru',
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
    # мягкие миграции
    for alter in [
        "ALTER TABLE tasks ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0",
        f"ALTER TABLE settings ADD COLUMN remind_lead_min INTEGER NOT NULL DEFAULT {DEFAULT_REMIND_MIN}",
        f"ALTER TABLE settings ADD COLUMN reminders_enabled INTEGER NOT NULL DEFAULT {DEFAULT_REMINDERS_ENABLED}",
        "ALTER TABLE settings ADD COLUMN prefer_no_dt_today INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE settings ADD COLUMN lang TEXT NOT NULL DEFAULT 'ru'",
        "ALTER TABLE settings ADD COLUMN onboard_done INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            cur.execute(alter)
        except sqlite3.OperationalError:
            pass

    con.commit()
    con.close()


def get_con():
    return sqlite3.connect(DB_PATH)


# ----------------- Helpers -----------------

def get_chat_settings(chat_id: int) -> Tuple[str, int, int, int, int, int, str]:
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT tz, daily_hour, daily_minute, remind_lead_min, reminders_enabled, prefer_no_dt_today, lang FROM settings WHERE chat_id=?",
        (chat_id,),
    )
    row = cur.fetchone()
    con.close()
    if row:
        return row[0], int(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5]), row[6]
    return (
        DEFAULT_TZ,
        DEFAULT_SUMMARY_HOUR,
        DEFAULT_SUMMARY_MINUTE,
        DEFAULT_REMIND_MIN,
        DEFAULT_REMINDERS_ENABLED,
        1,
        DEFAULT_LANG,
    )


def set_chat_settings(chat_id: int, tzname: Optional[str] = None, hour: Optional[int] = None, minute: Optional[int] = None,
                      remind_lead_min: Optional[int] = None, reminders_enabled: Optional[int] = None,
                      prefer_no_dt_today: Optional[int] = None, lang: Optional[str] = None):
    cur_tz, cur_h, cur_m, cur_lead, cur_enabled, cur_pref, cur_lang = get_chat_settings(chat_id)
    tzname = tzname or cur_tz
    hour = cur_h if hour is None else hour
    minute = cur_m if minute is None else minute
    remind_lead_min = cur_lead if remind_lead_min is None else remind_lead_min
    reminders_enabled = cur_enabled if reminders_enabled is None else reminders_enabled
    prefer_no_dt_today = cur_pref if prefer_no_dt_today is None else prefer_no_dt_today
    lang = cur_lang if lang is None else lang

    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, tz, daily_hour, daily_minute, remind_lead_min, reminders_enabled, prefer_no_dt_today, lang, onboard_done)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT onboard_done FROM settings WHERE chat_id=?), 0))
        ON CONFLICT(chat_id) DO UPDATE SET
            tz=excluded.tz,
            daily_hour=excluded.daily_hour,
            daily_minute=excluded.daily_minute,
            remind_lead_min=excluded.remind_lead_min,
            reminders_enabled=excluded.reminders_enabled,
            prefer_no_dt_today=excluded.prefer_no_dt_today,
            lang=excluded.lang
        """,
        (chat_id, tzname, hour, minute, remind_lead_min, reminders_enabled, prefer_no_dt_today, lang, chat_id),
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
    tz, hour, minute, lead, enabled, pref, lang = get_chat_settings(chat_id)
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO settings (chat_id, tz, daily_hour, daily_minute, remind_lead_min,
                              reminders_enabled, prefer_no_dt_today, lang, onboard_done)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET onboard_done=excluded.onboard_done
        """,
        (chat_id, tz, hour, minute, lead, enabled, pref, lang, 1 if done else 0),
    )
    con.commit()
    con.close()


def tz_from_location(lat: float, lon: float) -> Optional[str]:
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


def _guess_all_day_from_span(span_text: str, dt: datetime) -> bool:
    span = span_text.lower()
    if re.search(r"\b\d{1,2}:\d{2}\b", span):
        return False
    return dt.hour == 0 and dt.minute == 0


class InvalidDateTime(ValueError):
    pass


def _clean_task_text(raw: str, lang: Optional[str] = None) -> str:
    s = raw.strip()
    s = s.strip(" -—:,.;")
    trailing_ru = ["в", "во", "к", "на"]
    trailing_en = ["at", "on", "in", "by"]
    def strip_trailing_word(s: str, words: List[str]) -> str:
        for w in words:
            if re.search(rf"\b{re.escape(w)}\s*$", s, flags=re.IGNORECASE):
                return re.sub(rf"\b{re.escape(w)}\s*$", "", s, flags=re.IGNORECASE).strip()
        return s
    s = strip_trailing_word(s, trailing_ru + trailing_en)
    def strip_leading_word(s: str, words: List[str]) -> str:
        for w in words:
            if re.search(rf"^\s*{re.escape(w)}\b\s+", s, flags=re.IGNORECASE):
                return re.sub(rf"^\s*{re.escape(w)}\b\s+", "", s, flags=re.IGNORECASE).strip()
        return s
    s = strip_leading_word(s, trailing_ru + trailing_en)
    return s or ("Без названия" if (lang or DEFAULT_LANG) == "ru" else "Untitled")


def _strict_dt_parse(text: str, chat_tz: str):
    tzinfo = pytz.timezone(chat_tz)
    now_local = datetime.now(tzinfo)

    date_re = re.search(r'\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b', text)
    time_re = re.search(r'\b(\d{1,2}):(\d{2})\b', text)

    if not date_re and not time_re:
        return None

    if date_re:
        dd, mm, yy = date_re.groups()
        dd = int(dd); mm = int(mm)
        if not (1 <= dd <= 31 and 1 <= mm <= 12):
            raise InvalidDateTime("bad date")
        if yy:
            yy = int(yy)
            if yy < 100:
                yy += 2000 if yy < 70 else 1900
            year = yy
        else:
            year = now_local.year
    else:
        year, mm, dd = now_local.year, now_local.month, now_local.day

    if time_re:
        hh, mn = int(time_re.group(1)), int(time_re.group(2))
        if not (0 <= hh <= 23 and 0 <= mn <= 59):
            raise InvalidDateTime("bad time")
        all_day = 0
    else:
        hh, mn = 23, 59
        all_day = 1

    try:
        local_dt = tzinfo.localize(datetime(year, mm, dd, hh, mn))
    except ValueError:
        raise InvalidDateTime("bad calendar date")

    due_utc = local_dt.astimezone(pytz.utc)

    if due_utc < datetime.now(pytz.utc) - timedelta(minutes=1):
        if not date_re and time_re:
            due_utc += timedelta(days=1)
        else:
            try:
                due_utc = due_utc.replace(year=due_utc.year + 1)
            except ValueError:
                pass
  
    task_text = text
    if date_re:
        task_text = task_text.replace(date_re.group(0), "")
    if time_re:
        task_text = task_text.replace(time_re.group(0), "")
    task_text = _clean_task_text(task_text)

    return due_utc, task_text, all_day


def parse_task_input(text: str, chat_tz: str):
    tzinfo = pytz.timezone(chat_tz)
    now_local = datetime.now(tzinfo)

    m1 = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s+(\d{1,2}):(\d{2})\b", text)
    m2 = re.search(r"\b(\d{1,2}):(\d{2})\s+(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", text)
    match_used = None
    if m1 or m2:
        if m1:
            dd, mm, yy, hh, mn = m1.groups()
            dd = int(dd); mm = int(mm); hh = int(hh); mn = int(mn)
            year = int(yy) if yy else now_local.year
            if yy and int(yy) < 100:
                year = (2000 + int(yy)) if int(yy) < 70 else (1900 + int(yy))
            match_used = m1.group(0)
        else:
            hh, mn, dd, mm, yy = m2.groups()
            dd = int(dd); mm = int(mm); hh = int(hh); mn = int(mn)
            year = int(yy) if yy else now_local.year
            if yy and int(yy) < 100:
                year = (2000 + int(yy)) if int(yy) < 70 else (1900 + int(yy))
            match_used = m2.group(0)
        if not (1 <= dd <= 31 and 1 <= mm <= 12 and 0 <= hh <= 23 and 0 <= mn <= 59):
            raise InvalidDateTime("bad dt explicit")
        try:
            local_dt = tzinfo.localize(datetime(year, mm, dd, hh, mn))
        except ValueError:
            raise InvalidDateTime("bad calendar date")
        due_utc = local_dt.astimezone(pytz.utc)
        if due_utc < datetime.now(pytz.utc) - timedelta(minutes=1):
            try:
                due_utc = due_utc.replace(year=due_utc.year + 1)
            except ValueError:
                pass
        task_text = _clean_task_text(text.replace(match_used, ""))
        return due_utc, task_text, 0

    settings = {
        "TIMEZONE": chat_tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "DATE_ORDER": "DMY",
        "RELATIVE_BASE": now_local,
    }

    try:
        results = search_dates(text, languages=["ru", "en", "it"], settings=settings)
    except Exception:
        results = None

    if results:
        matched_span, dt = results[0]
        if dt.tzinfo is None:
            dt = tzinfo.localize(dt)

        task_text = _clean_task_text(text.replace(matched_span, ""))
        all_day_flag = 1 if _guess_all_day_from_span(matched_span, dt) else 0
        if all_day_flag:
            dt = tzinfo.localize(datetime(dt.year, dt.month, dt.day, 23, 59))

        due_utc = dt.astimezone(pytz.utc)
        if due_utc < datetime.now(pytz.utc) - timedelta(minutes=1):
            try:
                due_utc = due_utc.replace(year=due_utc.year + 1)
            except ValueError:
                pass
        return due_utc, task_text, all_day_flag

    strict = _strict_dt_parse(text, chat_tz)
    return strict


def save_task(chat_id: int, due_utc: datetime, text: str, all_day: int) -> int:
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO tasks (chat_id, text, due_utc, created_utc, done, all_day) VALUES (?, ?, ?, ?, 0, ?)",
        (
            chat_id,
            text,
            due_utc.isoformat(),
            datetime.utcnow().isoformat(),
            all_day,
        ),
    )
    task_id = cur.lastrowid
    con.commit()
    con.close()
    return task_id


def fetch_tasks_for_date(chat_id: int, day: datetime, chat_tz: str) -> List[Tuple[int, str, datetime, int]]:
    tzinfo = pytz.timezone(chat_tz)
    start_local = tzinfo.localize(datetime(day.year, day.month, day.day, 0, 0))
    end_local = start_local + timedelta(days=1)

    start_utc = start_local.astimezone(pytz.utc).isoformat()
    end_utc = end_local.astimezone(pytz.utc).isoformat()

    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT id, text, due_utc, all_day FROM tasks WHERE chat_id=? AND due_utc >= ? AND due_utc < ? AND done=0 ORDER BY all_day DESC, due_utc ASC",
        (chat_id, start_utc, end_utc),
    )
    rows = cur.fetchall()
    con.close()

    tasks = []
    for _id, text, due_iso, all_day in rows:
        due_dt_local = datetime.fromisoformat(due_iso).astimezone(tzinfo)
        tasks.append((_id, text, due_dt_local, int(all_day)))
    return tasks


def format_tasks(lang: str, tasks: List[Tuple[int, str, datetime, int]]) -> str:
    if not tasks:
        return T(lang, "empty")
    lines = []
    for _id, text, due_local, all_day in tasks:
        if all_day:
            lines.append(f"• {text}")
        else:
            lines.append(f"• {due_local.strftime('%H:%M')} — {text}")
    return "\n".join(lines)


def parse_lead_minutes(s: str) -> Tuple[Optional[int], str]:
    if s is None:
        return None, "empty"
    txt = s.strip().lower()
    if txt == "":
        return None, "empty"

    if txt in {"нет", "no", "off", "выкл", "disable"}:
        return 0, "disable"

    m = re.match(r"^\s*(\d+)\s*([a-zа-я.]*)\s*$", txt)
    if not m:
        return None, "invalid"

    n = int(m.group(1))
    unit = m.group(2)

    hours = {"ч", "час", "часа", "часов", "h", "hr", "hrs", "hour", "hours"}
    mins  = {"м", "мин", "минута", "минуты", "минут", "m", "min", "mins", "minute", "minutes", ""}

    if unit in hours:
        minutes = n * 60
    elif unit in mins:
        minutes = n
    else:
        return None, "invalid"

    return minutes, "ok"

def is_commandish(text: str) -> Optional[Tuple[str, List[str]]]:
    t = text.strip()

    m = re.fullmatch(r'(?i)list\s+time\s+(\d{1,2}:\d{2})', t)
    if m:
        return ("list_time", [m.group(1)])

    m = re.fullmatch(r'(?i)list\s+(\d{1,2}[./]\d{1,2})', t)
    if m:
        return ("list_date", [m.group(1)])

    if re.fullmatch(r'(?i)list', t):
        return ("list", [])

    if re.fullmatch(r'(?i)help', t):
        return ("help", [])

    m = re.fullmatch(r'(?i)remindertime\s+(.+)', t)
    if m:
        return ("remindertime", [m.group(1)])

    m = re.fullmatch(r'(?i)reminder\s+(on|off)', t)
    if m:
        return ("reminder", [m.group(1)])

    m = re.fullmatch(r'(?i)tz\s+(.+)', t)
    if m:
        return ("tz", [m.group(1)])

    if re.fullmatch(r'(?i)lang', t):
        return ("lang", [])

    return None

# ----------------- Reminders -----------------

async def schedule_task_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: int, due_utc: datetime):
    tzname, _, _, lead_min, enabled, _, lang = get_chat_settings(chat_id)
    if not enabled or lead_min <= 0:
        return

    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT all_day FROM tasks WHERE id=? AND chat_id=?", (task_id, chat_id))
    row = cur.fetchone()
    con.close()
    if not row or int(row[0]) == 1:
        return

    reminder_utc = due_utc - timedelta(minutes=lead_min)
    if reminder_utc <= datetime.now(pytz.utc):
        return

    job_name = f"reminder_{chat_id}_{task_id}"
    for j in context.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()

    context.job_queue.run_once(
        when=reminder_utc,
        callback=reminder_job,
        name=job_name,
        data={"chat_id": chat_id, "task_id": task_id},
    )

async def reminder_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.data["chat_id"]
    task_id = ctx.job.data["task_id"]
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT text, due_utc FROM tasks WHERE id=? AND chat_id=? AND done=0", (task_id, chat_id))
    row = cur.fetchone()
    con.close()
    if not row:
        return
    text, due_iso = row
    tzname, _, _, _, _, _, lang = get_chat_settings(chat_id)
    due_local = datetime.fromisoformat(due_iso).astimezone(pytz.timezone(tzname))
    await ctx.bot.send_message(
        chat_id=chat_id,
        text=T(lang, "reminder", text=text, time=due_local.strftime('%H:%M %d.%m')),
    )


async def reschedule_all_reminders(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    tzname, _, _, lead_min, enabled, _, _ = get_chat_settings(chat_id)
    if not enabled or lead_min <= 0:
        return
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT id, due_utc FROM tasks WHERE chat_id=? AND done=0 AND all_day=0 AND due_utc > ?",
        (chat_id, datetime.now(pytz.utc).isoformat()),
    )
    rows = cur.fetchall()
    con.close()
    for task_id, due_iso in rows:
        due_utc = datetime.fromisoformat(due_iso).astimezone(pytz.utc)
        await schedule_task_reminder(context, chat_id, task_id, due_utc)


# ----------------- Bot Handlers -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    init_db()

    # гарантируем запись в settings с дефолтами
    tzname, hour, minute, lead_min, enabled, pref, lang = get_chat_settings(chat_id)
    set_chat_settings(chat_id, tzname, hour, minute, lead_min, enabled, pref, lang)

    await schedule_daily_summary(context, chat_id)
    await reschedule_all_reminders(context, chat_id)

    context.chat_data['in_settings'] = False

    if is_onboarded(chat_id):
        await help_cmd(update, context)
        return

    kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(T(lang, "welcome"), reply_markup=kb)
    context.chat_data['onboard_stage'] = 'lang_select'


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, hour, minute, lead, enabled, _, lang = get_chat_settings(chat_id)
    await update.message.reply_text(T(lang, "help"), reply_markup=build_main_menu(lang))
    await update.message.reply_text(T(
        lang,
        "state_summary",
        tz=tzname,
        hh=hour,
        mm=minute,
        rem=("on" if (enabled and lang == "en") else ("включены" if enabled else ("off" if lang == "en" else "выключены"))),
        lead=lead,
    ))


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, _, _, _, _, _, lang = get_chat_settings(chat_id)
    args = update.message.text.split()
    if len(args) == 1:
        now_local = datetime.now(pytz.timezone(tzname))
        tasks = fetch_tasks_for_date(chat_id, now_local, tzname)
        await update.message.reply_text(T(lang, "today_list", date=now_local.strftime('%d.%m'), list=format_tasks(lang, tasks)))
        return
    if len(args) == 2 and "." in args[1]:
        try:
            dd, mm = args[1].split(".")
            day = int(dd); month = int(mm)
            now_local = datetime.now(pytz.timezone(tzname))
            year = now_local.year
            target = datetime(year, month, day)
        except Exception:
            await update.message.reply_text(T(lang, "format_list"))
            return
        tasks = fetch_tasks_for_date(chat_id, target, tzname)
        await update.message.reply_text(T(lang, "on_list", date=target.strftime('%d.%m'), list=format_tasks(lang, tasks)))
        return
    if len(args) == 3 and args[1].lower() == "time" and ":" in args[2]:
        try:
            hh, mm = map(int, args[2].split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except Exception:
            await update.message.reply_text(T(lang, "time_invalid"))
            return
        set_chat_settings(chat_id, hour=hh, minute=mm)
        await schedule_daily_summary(context, chat_id, reschedule=True)
        await update.message.reply_text(T(lang, "daily_set", hh=hh, mm=mm, tz=tzname))
        return
    await update.message.reply_text(T(lang, "format_list"))


async def reminder_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    parts = update.message.text.split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        await update.message.reply_text("Use Reminders button to toggle on/off.")
        return
    enable = 1 if parts[1].lower() == "on" else 0
    set_chat_settings(chat_id, reminders_enabled=enable)
    await reschedule_all_reminders(context, chat_id)
    await update.message.reply_text(T(lang, "reminders_on") if enable else T(lang, "reminders_off"))

async def remindertime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, hour, minute, lead, enabled, _, lang = get_chat_settings(chat_id)

    parts = update.message.text.split(maxsplit=1)
    payload = parts[1] if len(parts) == 2 else ""

    minutes, status = parse_lead_minutes(payload)

    if status == "empty":
        await update.message.reply_text(
            T(
                lang,
                "state_summary",
                tz=tzname,
                hh=hour,
                mm=minute,
                rem=("on" if (enabled and lang=="en") else ("включены" if enabled else ("off" if lang=="en" else "выключены"))),
                lead=lead,
            )
        )
        return

    if status == "disable":
        set_chat_settings(chat_id, reminders_enabled=0)
        await update.message.reply_text(T(lang, "reminders_off"))
        return

    if status == "invalid" or minutes is None or minutes < 0 or minutes > 24 * 60:
        await update.message.reply_text(T(lang, "lead_invalid"))
        return

    set_chat_settings(chat_id, remind_lead_min=minutes, reminders_enabled=1)
    await reschedule_all_reminders(context, chat_id)
    await update.message.reply_text(T(lang, "remind_set", lead=minutes))

async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, hour, minute, lead, en, pref, lang = get_chat_settings(chat_id)
    args = update.message.text.split(maxsplit=1)

    if len(args) == 2:
        newtz = args[1].strip()
        try:
            pytz.timezone(newtz)
            set_chat_settings(chat_id, tzname=newtz)
            await schedule_daily_summary(context, chat_id, reschedule=True)
            await reschedule_all_reminders(context, chat_id)
            await update.message.reply_text(T(lang, "tz_updated", tz=newtz))
            if context.chat_data.get('onboard_stage') == 'ask_tz':
                await ask_reminder_lead_step(update, context)
            return
        except Exception:
            await update.message.reply_text(T(lang, "tz_invalid"))
            await ask_tz_step(update, context)
            return

    await ask_tz_step(update, context)
    if context.chat_data.get('onboard_stage') in (None, 'intro_confirm'):
        context.chat_data['onboard_stage'] = 'ask_tz'

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.location:
        return
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    newtz = tz_from_location(lat, lon)
    if not newtz:
        await update.message.reply_text(T(lang, "tz_geo_fail"), reply_markup=ReplyKeyboardRemove())
        await ask_tz_step(update, context)
        return
    set_chat_settings(chat_id, tzname=newtz)
    await schedule_daily_summary(context, chat_id, reschedule=True)
    await reschedule_all_reminders(context, chat_id)
    await update.message.reply_text(T(lang, "tz_updated", tz=newtz), reply_markup=ReplyKeyboardRemove())

    if context.chat_data.get('onboard_stage') == 'ask_tz':
        await ask_reminder_lead_step(update, context)


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(T(lang, "choose_lang_prompt"), reply_markup=kb)
    context.chat_data['onboard_stage'] = 'lang_select'


async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    tzname, hour, minute, lead, enabled, prefer_no_dt, lang = get_chat_settings(chat_id)
    stage = context.chat_data.get('onboard_stage')
    text = update.message.text.strip()
    in_settings = context.chat_data.get('in_settings', False)

    # Если онбординг не пройден и /start не вызывался
    if stage is None and not is_onboarded(chat_id) and (text.lower() not in {"/start", "start"}):
        kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(T(lang, "welcome"), reply_markup=kb)
        context.chat_data['onboard_stage'] = 'lang_select'
        return

    # ------------ Главное меню ------------
    if not in_settings:
        if text in {"Сегодня", "Today"}:
            context.chat_data.pop('awaiting_list_date', None)
            context.chat_data.pop('awaiting_summary_time', None)
            context.chat_data.pop('awaiting_lead', None)
            now_local = datetime.now(pytz.timezone(tzname))
            tasks = fetch_tasks_for_date(chat_id, now_local, tzname)
            await update.message.reply_text(T(lang, "today_list", date=now_local.strftime('%d.%m'), list=format_tasks(lang, tasks)), reply_markup=build_main_menu(lang))
            return

        if text in {"Список на дату", "List by date"}:
            context.chat_data.pop('awaiting_summary_time', None)
            context.chat_data.pop('awaiting_lead', None)
            context.chat_data.pop('awaiting_list_date', None)
            await update.message.reply_text("Введите дату в формате DD.MM" if lang=="ru" else "Enter date as DD.MM")
            context.chat_data['awaiting_list_date'] = True
            return

        if context.chat_data.get('awaiting_list_date'):
            m = re.fullmatch(r"\s*(\d{1,2})[./](\d{1,2})\s*", text)
            if m:
                dd, mm = int(m.group(1)), int(m.group(2))
                try:
                    now_local = datetime.now(pytz.timezone(tzname))
                    target = datetime(now_local.year, mm, dd)
                    tasks = fetch_tasks_for_date(chat_id, target, tzname)
                    await update.message.reply_text(T(lang, "on_list", date=target.strftime('%d.%m'), list=format_tasks(lang, tasks)), reply_markup=build_main_menu(lang))
                except Exception:
                    await update.message.reply_text(T(lang, "format_list"))
                finally:
                    context.chat_data.pop('awaiting_list_date', None)
                return

        if text in {"Настройки", "Settings"}:
            context.chat_data.pop('awaiting_list_date', None)
            context.chat_data.pop('awaiting_summary_time', None)
            context.chat_data.pop('awaiting_lead', None)
            context.chat_data['in_settings'] = True
            await update.message.reply_text("Настройки:" if lang=="ru" else "Settings:", reply_markup=build_settings_menu(lang))
            return

    # ------------ Под-меню Настройки ------------
    if in_settings:
        back_btn = "⬅️ Назад" if lang == "ru" else "⬅️ Back"
        lead_btn = "Время напоминания" if lang == "ru" else "Reminder time"

        if text == back_btn:
            context.chat_data['in_settings'] = False
            context.chat_data.pop('awaiting_list_date', None)
            context.chat_data.pop('awaiting_summary_time', None)
            context.chat_data.pop('awaiting_lead', None)
            await update.message.reply_text("Главное меню:" if lang=="ru" else "Main menu:", reply_markup=build_main_menu(lang))
            return

        if text in {"Время сводки", "Summary time"}:
            context.chat_data.pop('awaiting_list_date', None)
            context.chat_data.pop('awaiting_lead', None)
            context.chat_data.pop('awaiting_summary_time', None)
            await update.message.reply_text("Во сколько присылать? HH:MM" if lang=="ru" else "What time? HH:MM")
            context.chat_data['awaiting_summary_time'] = True
            return

        if context.chat_data.get('awaiting_summary_time'):
            try:
                hh, mm = map(int, text.split(":"))
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    raise ValueError
            except Exception:
                await update.message.reply_text(T(lang, "time_invalid"))
                return
            set_chat_settings(chat_id, hour=hh, minute=mm)
            await schedule_daily_summary(context, chat_id, reschedule=True)
            await update.message.reply_text(T(lang, "daily_set", hh=hh, mm=mm, tz=tzname), reply_markup=build_settings_menu(lang))
            context.chat_data.pop('awaiting_summary_time', None)
            return

        if text in {"Напоминания", "Reminders"}:
            context.chat_data.pop('awaiting_list_date', None)
            context.chat_data.pop('awaiting_summary_time', None)
            context.chat_data.pop('awaiting_lead', None)
            enable = 0 if enabled else 1
            set_chat_settings(chat_id, reminders_enabled=enable)
            await reschedule_all_reminders(context, chat_id)
            await update.message.reply_text(T(lang, "reminders_on") if enable else T(lang, "reminders_off"), reply_markup=build_settings_menu(lang))
            return

        if text == lead_btn:
            context.chat_data.pop('awaiting_list_date', None)
            context.chat_data.pop('awaiting_summary_time', None)
            context.chat_data.pop('awaiting_lead', None)
            await update.message.reply_text("За сколько минут напоминать? Например: 15 мин" if lang=="ru" else "How many minutes before? e.g., 15 min")
            context.chat_data['awaiting_lead'] = True
            return

        if context.chat_data.get('awaiting_lead'):
            minutes, status = parse_lead_minutes(text)
            if status == "disable":
                set_chat_settings(chat_id, reminders_enabled=0)
                await update.message.reply_text(T(lang, "reminders_off"), reply_markup=build_settings_menu(lang))
                context.chat_data.pop('awaiting_lead', None)
                return
            if status != "ok" or minutes is None or minutes < 0 or minutes > 24*60:
                await update.message.reply_text(T(lang, "lead_invalid"))
                return
            set_chat_settings(chat_id, remind_lead_min=minutes, reminders_enabled=1)
            await reschedule_all_reminders(context, chat_id)
            await update.message.reply_text(T(lang, "remind_set", lead=minutes), reply_markup=build_settings_menu(lang))
            context.chat_data.pop('awaiting_lead', None)
            return

        if text in {"Таймзона", "Timezone"}:
            context.chat_data.pop('awaiting_list_date', None)
            context.chat_data.pop('awaiting_summary_time', None)
            context.chat_data.pop('awaiting_lead', None)
            await ask_tz_step(update, context)
            return

        if text in {"Язык", "Language"}:
            context.chat_data.pop('awaiting_list_date', None)
            context.chat_data.pop('awaiting_summary_time', None)
            context.chat_data.pop('awaiting_lead', None)
            await lang_cmd(update, context)
            return

    # -------- онбординг: выбор языка --------
    if stage == "lang_select":
        msg = text.lower()
        if msg in {"русский", "russian"}:
            set_chat_settings(chat_id, lang="ru")
            context.chat_data['onboard_stage'] = "ask_tz"
            await update.message.reply_text(MESSAGES['ru']["lang_saved"], reply_markup=ReplyKeyboardRemove())
            await update.message.reply_text(T("ru", "intro_mechanics"))
            await ask_tz_step(update, context)
            return
        if msg in {"english", "английский"}:
            set_chat_settings(chat_id, lang="en")
            context.chat_data['onboard_stage'] = "ask_tz"
            await update.message.reply_text(MESSAGES['en']["lang_saved"], reply_markup=ReplyKeyboardRemove())
            await update.message.reply_text(T("en", "intro_mechanics"))
            await ask_tz_step(update, context)
            return
        kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(T(lang, "choose_lang_prompt"), reply_markup=kb)
        return

    # -------- онбординг: таймзона --------
    if stage == "ask_tz":
        raw = text.strip()
        if "/" in raw:
            try:
                pytz.timezone(raw)
                set_chat_settings(chat_id, tzname=raw)
                context.chat_data['onboard_stage'] = "ask_reminder"
                await update.message.reply_text(T(lang, "tz_updated", tz=raw), reply_markup=ReplyKeyboardRemove())
                await ask_reminder_lead_step(update, context)
                return
            except Exception:
                await update.message.reply_text(T(lang, "tz_invalid"))
                await ask_tz_step(update, context)
                return
        await update.message.reply_text(T(lang, "tz_invalid"))
        await ask_tz_step(update, context)
        return

    # -------- онбординг: напоминания --------
    if stage == 'ask_reminder':
        minutes, status = parse_lead_minutes(text)

        if status == "disable":
            set_chat_settings(chat_id, reminders_enabled=0)
            await update.message.reply_text(T(lang, "reminders_off"))
            await ask_summary_time_step(update, context)
            return

        if status != "ok" or minutes is None or minutes > 24 * 60 or minutes < 0:
            await update.message.reply_text(T(lang, "lead_invalid"))
            return

        set_chat_settings(chat_id, remind_lead_min=minutes, reminders_enabled=1)
        await reschedule_all_reminders(context, chat_id)
        await update.message.reply_text(T(lang, "remind_set", lead=minutes))
        await ask_summary_time_step(update, context)
        return

    # -------- онбординг: время ежедневной сводки --------
    if stage == 'ask_summary_time':
        try:
            hh, mm = map(int, text.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except Exception:
            await update.message.reply_text(T(lang, "time_invalid"))
            return
        set_chat_settings(chat_id, hour=hh, minute=mm)
        await schedule_daily_summary(context, chat_id, reschedule=True)
        await update.message.reply_text(T(lang, "daily_set", hh=hh, mm=mm, tz=tzname))
        await update.message.reply_text(f"{T(lang, 'setup_done_title')}\n\n{T(lang, 'setup_done_body')}")
        context.chat_data.pop('onboard_stage', None)
        set_onboarded(chat_id, True)
        await update.message.reply_text("Выберите действие:" if lang=="ru" else "Choose an action:", reply_markup=build_main_menu(lang))
        return

    # -------- команды без слэша (legacy) --------
    cmd = is_commandish(text)
    if cmd:
        name, args = cmd
        if name == "list":
            now_local = datetime.now(pytz.timezone(tzname))
            tasks = fetch_tasks_for_date(chat_id, now_local, tzname)
            await update.message.reply_text(
                T(lang, "today_list", date=now_local.strftime('%d.%m'), list=format_tasks(lang, tasks))
            )
            return

        if name == "list_time":
            try:
                hh, mm = map(int, args[0].split(":"))
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    raise ValueError
            except Exception:
                await update.message.reply_text(T(lang, "time_invalid"))
                return
            set_chat_settings(chat_id, hour=hh, minute=mm)
            await schedule_daily_summary(context, chat_id, reschedule=True)
            await update.message.reply_text(T(lang, "daily_set", hh=hh, mm=mm, tz=tzname))
            return

        if name == "list_date":
            try:
                dd, mm = re.split(r"[./]", args[0])
                day = int(dd); month = int(mm)
                now_local = datetime.now(pytz.timezone(tzname))
                target = datetime(now_local.year, month, day)
            except Exception:
                await update.message.reply_text(T(lang, "format_list"))
                return
            tasks = fetch_tasks_for_date(chat_id, target, tzname)
            await update.message.reply_text(
                T(lang, "on_list", date=target.strftime('%d.%m'), list=format_tasks(lang, tasks))
            )
            return

        if name == "help":
            await help_cmd(update, context)
            return

        if name == "remindertime":
            payload = args[0]
            minutes, status = parse_lead_minutes(payload)
            if status == "disable":
                set_chat_settings(chat_id, reminders_enabled=0)
                await update.message.reply_text(T(lang, "reminders_off"))
                return
            if status != "ok" or minutes is None or minutes < 0 or minutes > 24*60:
                await update.message.reply_text(T(lang, "lead_invalid"))
                return
            set_chat_settings(chat_id, remind_lead_min=minutes, reminders_enabled=1)
            await reschedule_all_reminders(context, chat_id)
            await update.message.reply_text(T(lang, "remind_set", lead=minutes))
            return

        if name == "reminder":
            enable = 1 if args[0].lower() == "on" else 0
            set_chat_settings(chat_id, reminders_enabled=enable)
            await reschedule_all_reminders(context, chat_id)
            await update.message.reply_text(T(lang, "reminders_on") if enable else T(lang, "reminders_off"))
            return

        if name == "tz":
            newtz = args[0].strip()
            try:
                pytz.timezone(newtz)
                set_chat_settings(chat_id, tzname=newtz)
                await schedule_daily_summary(context, chat_id, reschedule=True)
                await reschedule_all_reminders(context, chat_id)
                await update.message.reply_text(T(lang, "tz_updated", tz=newtz))
                return
            except Exception:
                await update.message.reply_text(T(lang, "tz_invalid"))
                return

        if name == "lang":
            await lang_cmd(update, context)
            return

    # -------- обычный режим: добавление задач --------
    try:
        parsed = parse_task_input(text, tzname)
    except InvalidDateTime:
        await update.message.reply_text(T(lang, "dt_invalid_strict"), reply_markup=build_main_menu(lang))
        return

    if parsed:
        due_utc, task_text, all_day = parsed
        task_id = save_task(chat_id, due_utc, task_text, all_day)
        due_local = due_utc.astimezone(pytz.timezone(tzname))
        when_suffix = "" if all_day else ((" at " if lang=="en" else " в ") + due_local.strftime('%H:%M'))
        await update.message.reply_text(
            T(lang, "added_task", text=task_text, date=due_local.strftime('%d.%m'), when=when_suffix),
            reply_markup=build_main_menu(lang)
        )
        await schedule_task_reminder(context, chat_id, task_id, due_utc)
    else:
        tzinfo = pytz.timezone(tzname)
        now_local = datetime.now(tzinfo)
        due_local = tzinfo.localize(datetime(now_local.year, now_local.month, now_local.day, 23, 59))
        save_task(chat_id, due_local.astimezone(pytz.utc), text or ("Без названия" if lang=="ru" else "Untitled"), 1)
        await update.message.reply_text(T(lang, "added_today_nodt", text=text), reply_markup=build_main_menu(lang))
        return

    await update.message.reply_text(T(lang, "help"), reply_markup=build_main_menu(lang))
    return


# ---------- Онбординг шаги (хелперы) ----------

async def ask_tz_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Поделиться геолокацией", request_location=True)]] if lang == "ru"
        else [[KeyboardButton("📍 Share location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(T(lang, "tz_geo_prompt"), reply_markup=kb)
    await update.message.reply_text(T(lang, "ask_tz"))
    context.chat_data['onboard_stage'] = 'ask_tz'


async def ask_reminder_lead_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    await update.message.reply_text(T(lang, "ask_reminder_lead"), reply_markup=ReplyKeyboardRemove())
    context.chat_data['onboard_stage'] = 'ask_reminder'


async def ask_summary_time_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    await update.message.reply_text(T(lang, "ask_summary_time"))
    context.chat_data['onboard_stage'] = 'ask_summary_time'


# ----------------- Scheduler -----------------

async def schedule_daily_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reschedule: bool = False):
    tzname, hour, minute, *_ = get_chat_settings(chat_id)
    tzinfo = pytz.timezone(tzname)

    job_name = f"summary_{chat_id}"
    if reschedule:
        for j in context.job_queue.get_jobs_by_name(job_name):
            j.schedule_removal()

    context.job_queue.run_daily(
        callback=daily_summary_job,
        time=time(hour=hour, minute=minute, tzinfo=tzinfo),
        name=job_name,
        data={"chat_id": chat_id},
    )


async def daily_summary_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.data["chat_id"]
    tzname, _, _, _, _, _, lang = get_chat_settings(chat_id)
    now_local = datetime.now(pytz.timezone(tzname))
    tasks = fetch_tasks_for_date(chat_id, now_local, tzname)
    await ctx.bot.send_message(
        chat_id=chat_id,
        text=T(lang, "summary", date=now_local.strftime('%d.%m'), list=format_tasks(lang, tasks)),
    )


# ----------------- Dev helper: parser smoke tests -----------------

def _run_parser_smoke_tests():
    samples = [
        "16:00 08.08 Позвонить маме",
        "08.08 16:00 Встреча",
        "30.08 23:00 drink beer",
        "Go to the shop at 23:00",
        "Идти в магазин в 23:00",
        "01/09 Meeting",
        "Сегодня в 7:00 пробежка",
        "август 16.00 созвон",
        "завтра 09:15 пробежка",
        "15 сентября 14 00 дедлайн",
        "15 сентября доклад",
        "сегодня в 18 встреча",
        "купить хлеб",
        "31.08 пожарить бычков",
        "32.08 что-то неверное",
    ]
    tzname = DEFAULT_TZ
    ok = 0
    for s in samples:
        try:
            res = parse_task_input(s, tzname)
            if s.startswith("32.08"):
                assert False, "должно быть InvalidDateTime"
            ok += 1
        except InvalidDateTime:
            ok += 1
        except Exception as e:
            print("[TEST FAIL]", s, e)
    print(f"Parser tests passed: {ok}/{len(samples)}")


# ----------------- Main -----------------

def main():
    if os.getenv("RUN_PARSER_TESTS") == "1":
        _run_parser_smoke_tests()
        return

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

    use_webhook = False

    async def _post_init(app):
        if not use_webhook:
            await app.bot.delete_webhook(drop_pending_updates=True)

    app: Application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CommandHandler("reminder", reminder_toggle_cmd))
    app.add_handler(CommandHandler("remindertime", remindertime_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))

    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))

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
