"""
Telegram Task Tracker Bot
Авто-TZ · Гибкий парсер · Напоминания · Онбординг по одному вопросу · Мультиязык (RU/EN)

Тезисы:
- Пиши задачи свободным текстом: "завтра 16:00 созвон", "08.09 купить билеты", "позвонить маме"
- Без даты/времени — автоматически добавлю в список на сегодня (без времени)
- Для задач с временем приходят напоминания заранее (настраивается)
- Каждое утро приходит список дел (время настраивается)
"""

import os
import sqlite3
from datetime import datetime, time, timedelta
from typing import Optional, Tuple, List, Dict

import pytz
from dateparser.search import search_dates
from timezonefinder import TimezoneFinder

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ----------------- Config -----------------

DB_PATH = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Rome")
DEFAULT_SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "8"))
DEFAULT_SUMMARY_MINUTE = int(os.getenv("SUMMARY_MINUTE", "0"))
DEFAULT_REMIND_MIN = int(os.getenv("REMIND_MINUTES", "30"))
DEFAULT_REMINDERS_ENABLED = int(os.getenv("REMINDERS_ENABLED", "1"))  # 1=on,0=off
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "ru")  # 'ru' or 'en'

TF = TimezoneFinder()

# ----------------- i18n -----------------

MESSAGES: Dict[str, Dict[str, str]] = {
    "ru": {
        "welcome": (
            "Привет! Возможно я самый простой таск-трекер, которым ты когда-либо пользовался.\n"
            "Выбери язык ниже — и начнём.\n\n"
            "Hi! I might be the simplest task tracker you've ever used.\n"
            "Pick a language below and let's start."
        ),
        "choose_lang_prompt": "👉 Выбери язык:",
        "lang_saved": "Готово! Язык: Русский.",
        # Онбординг
        "intro_mechanics": (
            "Как я работаю:\n"
            "📜 Пиши задачи обычным текстом — я понимаю даты и время в свободном формате.\n"
            "📜 Если пишешь |без| даты и времени — просто добавлю в общий список дед на сегодня.\n"
            "📜 Для задач, где указано время, могу прислать напоминание заранее — как настроишь.\n"
            "📜 Каждое утро пришлю список дел на день.\n\n"
            "Готов?"
        ),
        "ask_tz": (
            "Отправь геолокацию — я настрою твой часовой пояс автоматически.\n"
            "Если не получается, используй: `/tz Europe/Rome` (континент/город).\n"
            "Например: `/tz Europe/Rome`."
        ),
        "ask_reminder_lead": (
            "За сколько времени до задачи присылать напоминание?\n"
            "Напиши, например: `15 мин`, `30 мин`, `1 ч`. Если не нужны — ответь «нет»."
        ),
        "ask_summary_time": (
            "Во сколько присылать утренний список дел? Напиши время в формате `HH:MM`, например `09:00`."
        ),
        "setup_done_title": "Готово! Всё настроено ✅",
        "setup_done_body": "Вот что доступно:",
        # Команды / Help
        "help": (
            "Команды:\n"
            "/list — список на сегодня\n"
            "/list DD.MM — список на указанную дату\n"
            "/list time HH:MM — во сколько присылать ежедневный список\n"
            "/reminder on|off — включить/выключить напоминания\n"
            "/remindertime <15 мин|1 ч> — за сколько напоминать\n"
            "/tz — обновить таймзону по геолокации (по запросу)\n"
            "/tz Europe/Rome — выставить таймзону вручную\n"
            "/lang — сменить язык"
        ),
        "state_summary": (
            "Текущий часовой пояс: {tz}\n"
            "Сводка: {hh:02d}:{mm:02d}\n"
            "Напоминания: {rem} за {lead} мин"
        ),
        # Ответы и статусы
        "daily_set": "Сводка будет приходить в {hh:02d}:{mm:02d} по {tz}.",
        "remind_set": "Напоминания будут приходить за {lead} мин до задачи.",
        "reminders_on": "Напоминания: включены.",
        "reminders_off": "Напоминания: выключены.",
        "tz_updated": "Часовой пояс обновлён: {tz}.",
        "tz_geo_prompt": "Поделись геолокацией, чтобы я выставил твой часовой пояс автоматически.",
        "tz_geo_fail": "Не удалось определить часовой пояс.",
        "added_today_nodt": "Добавил на сегодня [без времени]: {text}",
        "added_task": "Ок, добавил: {text}\nНа {date} {when}",
        "today_list": "Задачи на сегодня ({date}):\n{list}",
        "on_list": "Задачи на {date}:\n{list}",
        "empty": "Пока пусто",
        "reminder": "⏰ Напоминание: {text}\nВ {time}",
        "summary": "Доброе утро! Вот план на сегодня ({date}):\n{list}",
        "format_list": "Форматы: `/list`, `/list DD.MM`, `/list time HH:MM`",
        "time_invalid": "Время некорректно. Пример: `09:30`.",
        "lead_invalid": "Не понял длительность. Примеры: `15 мин`, `1 ч`, `30 m`, `2 h`, `нет`.",
        "range_invalid": "Значение вне диапазона (0..1440).",
        "tz_invalid": "Не знаю такой зоны. Пример: `/tz Europe/Rome`.",
        "tip_setup": "Подсказка: /tz → /remindertime → /list time.",
        "please_yesno": "Ответь, пожалуйста: «да/yes» или «нет/no».",
    },
    "en": {
        "welcome": (
            "Hi! I might be the simplest task tracker you've ever used.\n"
            "Pick a language below and let's start.\n\n"
            "Привет! Возможно я самый простой таск-трекер, которым ты когда-либо пользовался.\n"
            "Выбери язык ниже — и начнём."
        ),
        "choose_lang_prompt": "👉 Choose your language:",
        "lang_saved": "Done! Language: English.",
        # Onboarding
        "intro_mechanics": (
            "How I work:\n"
            "📜 Send tasks as plain text — I parse dates & times in natural language.\n"
            "📜 If you send |no| date/time — I'll just add it for today.\n"
            "📜 Tasks that have a time will trigger reminders in advance — as you configure.\n"
            "📜 Every morning you'll get the daily list.\n\n"
            "Ready?"
        ),
        "ask_tz": (
            "Share your location to auto-set your timezone.\n"
            "Prefer not to share? Use `/tz Europe/Rome` (Continent/City) instead.\n"
            "For example: `/tz Europe/Rome`."
        ),
        "ask_reminder_lead": (
            "How long before a task should I remind you?\n"
            "Type e.g. `15 min`, `30 min`, `1 h`. If you don't want reminders — reply `no`."
        ),
        "ask_summary_time": (
            "When should I send the morning list? Reply with time `HH:MM`, e.g. `09:00`."
        ),
        "setup_done_title": "All set! ✅",
        "setup_done_body": "Here are the commands:",
        # Commands / Help
        "help": (
            "Commands:\n"
            "/list — today's tasks\n"
            "/list DD.MM — tasks for a given date\n"
            "/list time HH:MM — set daily summary time\n"
            "/tz — update timezone via location (on request)\n"
            "/tz Europe/Rome — set a specific timezone manually\n"
            "/reminder on|off — enable/disable reminders\n"
            "/remindertime <15 min|1 h> — reminder lead time\n"
            "/lang — change language"
        ),
        "state_summary": (
            "Timezone: {tz}\n"
            "Summary: {hh:02d}:{mm:02d}\n"
            "Reminders: {rem}, lead {lead} min"
        ),
        # Status
        "daily_set": "Daily summary at {hh:02d}:{mm:02d} ({tz}).",
        "remind_set": "Reminders will arrive {lead} minutes before a task.",
        "reminders_on": "Reminders: enabled.",
        "reminders_off": "Reminders: disabled.",
        "tz_updated": "Timezone updated: {tz}.",
        "tz_geo_prompt": "Share your location to set your timezone automatically.",
        "tz_geo_fail": "Couldn't determine timezone.",
        "added_today_nodt": "Added for today [no time]: {text}",
        "added_task": "Done: {text}\nFor {date} {when}",
        "today_list": "Today's tasks ({date}):\n{list}",
        "on_list": "Tasks for {date}:\n{list}",
        "empty": "Nothing yet",
        "reminder": "⏰ Reminder: {text}\nAt {time}",
        "summary": "Good morning! Here's your plan for today ({date}):\n{list}",
        "format_list": "Formats: `/list`, `/list DD.MM`, `/list time HH:MM`",
        "time_invalid": "Invalid time, e.g. `09:30`.",
        "lead_invalid": "Couldn't parse duration. Examples: `15 min`, `1 h`, `30 м`, `2 ч`, `no`.",
        "range_invalid": "Value out of range (0..1440).",
        "tz_invalid": "Unknown zone. Example: `/tz Europe/Rome`.",
        "tip_setup": "Tip: /tz → /remindertime → /list time.",
        "please_yesno": "Please reply `yes` or `no`.",
    },
}

LANG_BTNS = [["Русский", "English"]]


def T(lang: str, key: str, **kwargs) -> str:
    d = MESSAGES.get(lang, MESSAGES[DEFAULT_LANG])
    s = d.get(key, key)
    return s.format(**kwargs) if kwargs else s

# ----------------- Storage -----------------

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # base tables
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
            lang TEXT NOT NULL DEFAULT 'ru'
        )
        """
    )
    # migrations / defaults
    for alter in [
        "ALTER TABLE tasks ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0",
        f"ALTER TABLE settings ADD COLUMN remind_lead_min INTEGER NOT NULL DEFAULT {DEFAULT_REMIND_MIN}",
        f"ALTER TABLE settings ADD COLUMN reminders_enabled INTEGER NOT NULL DEFAULT {DEFAULT_REMINDERS_ENABLED}",
        "ALTER TABLE settings ADD COLUMN prefer_no_dt_today INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE settings ADD COLUMN lang TEXT NOT NULL DEFAULT 'ru'",
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
        INSERT INTO settings (chat_id, tz, daily_hour, daily_minute, remind_lead_min, reminders_enabled, prefer_no_dt_today, lang)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            tz=excluded.tz,
            daily_hour=excluded.daily_hour,
            daily_minute=excluded.daily_minute,
            remind_lead_min=excluded.remind_lead_min,
            reminders_enabled=excluded.reminders_enabled,
            prefer_no_dt_today=excluded.prefer_no_dt_today,
            lang=excluded.lang
        """,
        (chat_id, tzname, hour, minute, remind_lead_min, reminders_enabled, prefer_no_dt_today, lang),
    )
    con.commit()
    con.close()


def tz_from_location(lat: float, lon: float) -> Optional[str]:
    try:
        tzname = TF.timezone_at(lng=lon, lat=lat)
        return tzname
    except Exception:
        return None


def _guess_all_day_from_span(span_text: str, dt: datetime) -> bool:
    span = span_text.lower()
    has_time = any(sep in span for sep in [":", "."]) and any(t.isdigit() for t in span)
    return (dt.hour == 0 and dt.minute == 0) and not has_time


def parse_task_input(text: str, chat_tz: str):
    tzinfo = pytz.timezone(chat_tz)
    now_local = datetime.now(tzinfo)

    settings = {
        "TIMEZONE": chat_tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "DATE_ORDER": "DMY",
        "RELATIVE_BASE": now_local,
    }

    results = search_dates(text, languages=["ru", "en", "it"], settings=settings)
    if not results:
        return None

    matched_span, dt = results[0]
    if dt.tzinfo is None:
        dt = tzinfo.localize(dt)

    task_text = text.replace(matched_span, "").strip(" -—:,.;") or ("Без названия" if "ru" in chat_tz or DEFAULT_LANG=="ru" else "Untitled")

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
            lines.append(f"• [no time] — {text}" if lang == "en" else f"• [без времени] — {text}")
        else:
            lines.append(f"• {due_local.strftime('%H:%M')} — {text}")
    return "\n".join(lines)


def parse_lead_minutes(s: str) -> Optional[int]:
    """
    Parse human-friendly durations:
    "15", "15 м", "15 мин", "15 min", "1 ч", "2 часа", "1 h", "2 hours"
    Returns minutes (int) or None if 'no/нет/off'.
    """
    if not s:
        return None
    txt = s.strip().lower()
    if txt in {"нет", "no", "off"}:
        return None
    # extract number
    num = ""
    for ch in txt:
        if ch.isdigit():
            num += ch
    if not num:
        return None
    n = int(num)
    # unit
    if any(u in txt for u in ["ч", "час", "hours", "hour", "h"]):
        return n * 60
    # default or explicit minutes
    return n

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
    if reminder_utc <= datetime.utcnow():
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
    tzname, *_ = get_chat_settings(chat_id)
    due_local = datetime.fromisoformat(due_iso).astimezone(pytz.timezone(tzname))
    lang = get_chat_settings(chat_id)[-1]
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
        (chat_id, datetime.utcnow().isoformat()),
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
    # ensure settings row exists
    tzname, hour, minute, lead_min, enabled, _, lang = get_chat_settings(chat_id)
    set_chat_settings(chat_id, tzname, hour, minute, lead_min, enabled, 1, lang)

    # расписания можно проставить и сейчас; не мешает онбордингу
    await schedule_daily_summary(context, chat_id)
    await reschedule_all_reminders(context, chat_id)

    # ОДНО сообщение: приветствие уже содержит просьбу выбрать язык
    kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(T(lang, "welcome"), reply_markup=kb)
    context.chat_data['onboard_stage'] = 'lang_select'


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    tzname, hour, minute, lead, enabled, _, _ = get_chat_settings(chat_id)
    await update.message.reply_text(T(lang, "help"))
    await update.message.reply_text(T(lang, "state_summary",
                                      tz=tzname, hh=hour, mm=minute,
                                      rem=("on" if (enabled and lang=="en") else ("включены" if enabled else ("off" if lang=="en" else "выключены"))),
                                      lead=lead))


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
        await update.message.reply_text("/reminder on|off")
        return
    enable = 1 if parts[1].lower() == "on" else 0
    set_chat_settings(chat_id, reminders_enabled=enable)
    await reschedule_all_reminders(context, chat_id)
    await update.message.reply_text(T(lang, "reminders_on") if enable else T(lang, "reminders_off"))


async def remindertime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    payload = update.message.text.replace("/remindertime", "", 1).strip()
    minutes = parse_lead_minutes(payload)
    if minutes is None:
        # interpret as turning off
        set_chat_settings(chat_id, reminders_enabled=0)
        await update.message.reply_text(T(lang, "reminders_off"))
        return
    if minutes < 0 or minutes > 24*60:
        await update.message.reply_text(T(lang, "range_invalid"))
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
            # Если мы в онбординге на шаге TZ — переходим дальше
            if context.chat_data.get('onboard_stage') == 'ask_tz':
                await ask_reminder_lead_step(update, context)
            return
        except Exception:
            await update.message.reply_text(T(lang, "tz_invalid"))
            # ВАЖНО: остаёмся в ask_tz и снова показываем пример
            await ask_tz_step(update, context)
            return

    # Если /tz без аргумента — просто снова подсказываем правильный формат
    await ask_tz_step(update, context)

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(text=("Определить по геолокации" if lang=="ru" else "Detect via geolocation"), request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await update.message.reply_text(T(lang, "tz_geo_prompt"), reply_markup=kb)
    # mark stage if onboarding
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
        return
    set_chat_settings(chat_id, tzname=newtz)
    await schedule_daily_summary(context, chat_id, reschedule=True)
    await reschedule_all_reminders(context, chat_id)
    await update.message.reply_text(T(lang, "tz_updated", tz=newtz), reply_markup=ReplyKeyboardRemove())

    # Continue onboarding chain
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

    # --- шаг выбора языка ---
    if stage == "lang_select":
        msg = text.lower()
        if msg in {"русский", "russian"}:
            set_chat_settings(chat_id, lang="ru")
            context.chat_data['onboard_stage'] = "ask_tz"
            await update.message.reply_text(MESSAGES['ru']["lang_saved"], reply_markup=ReplyKeyboardRemove())
            await ask_tz_step(update, context)
            return
        if msg in {"english", "английский"}:
            set_chat_settings(chat_id, lang="en")
            context.chat_data['onboard_stage'] = "ask_tz"
            await update.message.reply_text(MESSAGES['en']["lang_saved"], reply_markup=ReplyKeyboardRemove())
            await ask_tz_step(update, context)
            return
        # неверный ответ → снова показать кнопки
        kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(T(lang, "choose_lang_prompt"), reply_markup=kb)
        return

    # --- шаг выбора таймзоны ---
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

    # --- шаг выбора напоминаний ---
    if stage == 'ask_reminder':
        minutes = parse_lead_minutes(text)
        if minutes is None:
            # выключаем напоминания
            set_chat_settings(chat_id, reminders_enabled=0)
            await update.message.reply_text(T(lang, "reminders_off"))
            await ask_summary_time_step(update, context)
            return
        if minutes < 0 or minutes > 24*60:
            await update.message.reply_text(T(lang, "range_invalid"))
            return
        set_chat_settings(chat_id, remind_lead_min=minutes, reminders_enabled=1)
        await reschedule_all_reminders(context, chat_id)
        await update.message.reply_text(T(lang, "remind_set", lead=minutes))
        await ask_summary_time_step(update, context)
        return

    # --- шаг выбора времени ежедневной сводки ---
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
        # Финальная карточка
        await update.message.reply_text(
            f"{T(lang, 'setup_done_title')}\n\n{T(lang, 'setup_done_body')}\n\n{T(lang, 'help')}"
        )
        context.chat_data.pop('onboard_stage', None)
        return

    # -------- обычный режим: добавление задач --------
    parsed = parse_task_input(text, tzname)
    if parsed:
        due_utc, task_text, all_day = parsed
        task_id = save_task(chat_id, due_utc, task_text, all_day)
        due_local = due_utc.astimezone(pytz.timezone(tzname))
        when_str = ("[no time]" if lang=="en" else "[без времени]") if all_day else f"{due_local.strftime('%H:%M')}"
        when_prefix = ("at " if (not all_day and lang=="en") else ("в " if not all_day else ""))
        await update.message.reply_text(
            T(lang, "added_task", text=task_text, date=due_local.strftime('%d.%m'),
              when=(when_prefix+when_str if when_prefix else when_str))
        )
        await schedule_task_reminder(context, chat_id, task_id, due_utc)
    else:
        # без даты/времени → всегда на сегодня (конец дня)
        tzinfo = pytz.timezone(tzname)
        now_local = datetime.now(tzinfo)
        due_local = tzinfo.localize(datetime(now_local.year, now_local.month, now_local.day, 23, 59))
        save_task(chat_id, due_local.astimezone(pytz.utc), text or ("Без названия" if lang=="ru" else "Untitled"), 1)
        await update.message.reply_text(T(lang, "added_today_nodt", text=text))
        return

# ---------- Онбординг шаги (хелперы) ----------

async def ask_tz_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(text=("Определить по геолокации" if lang=="ru" else "Detect via geolocation"), request_location=True)],
         [KeyboardButton(text=("Пропустить" if lang=="ru" else "Skip"))]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await update.message.reply_text(T(lang, "ask_tz"), reply_markup=kb)
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
        "август 16.00 созвон",
        "завтра 09:15 пробежка",
        "15 сентября 14 00 дедлайн",
        "15 сентября доклад",   # дата без времени → all_day
        "сегодня в 18 встреча",
        "купить хлеб",          # без даты/времени — автодобавление на сегодня
    ]
    tzname = DEFAULT_TZ
    ok = 0
    for s in samples:
        try:
            res = parse_task_input(s, tzname)
            if s == "купить хлеб":
                assert res is None
            elif s == "15 сентября доклад":
                assert res is not None and res[2] == 1
            else:
                assert res is not None
            ok += 1
        except Exception as e:
            print("[TEST FAIL]", s, e)
    print(f"Parser smoke tests passed: {ok}/{len(samples)}")

# ----------------- Main -----------------

def main():
    if os.getenv("RUN_PARSER_TESTS") == "1":
        _run_parser_smoke_tests()
        return

    init_db()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN env variable")

    # сбрасываем возможный webhook и висящие апдейты перед стартом polling
    async def _post_init(app):
        await app.bot.delete_webhook(drop_pending_updates=True)

    app: Application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CommandHandler("reminder", reminder_toggle_cmd))
    app.add_handler(CommandHandler("remindertime", remindertime_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))

    # геолокация и текст
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
