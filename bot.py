"""
Telegram Task Tracker Bot
Авто-TZ · Гибкий парсер · Напоминания · Интерактив без даты · Мультиязык (RU/EN)

Что умеет / What it does:
- Свободный ввод дат/времени и текста задачи / Natural date-time parsing
- Задачи без времени/даты — интерактив + запоминание предпочтения
- Daily summary time, reminders lead time, timezone autodetect
- RU/EN локализация команд и сообщений, выбор языка в /start и /lang

Requirements (pip):
python-telegram-bot==20.7
pytz==2024.1
python-dateutil==2.9.0
dateparser==1.2.0
timezonefinder==6.5.2
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

DB_PATH = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Rome")
DEFAULT_SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "8"))
DEFAULT_SUMMARY_MINUTE = int(os.getenv("SUMMARY_MINUTE", "0"))
DEFAULT_REMIND_MIN = int(os.getenv("REMIND_MINUTES", "30"))
DEFAULT_REMINDERS_ENABLED = int(os.getenv("REMINDERS_ENABLED", "1"))  # 1=on,0=off
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "ru")  # 'ru' or 'en'

TF = TimezoneFinder()

# ---------- i18n ----------

MESSAGES: Dict[str, Dict[str, str]] = {
    "ru": {
        "welcome": (
            "Привет! Возможно я самый простой таск трекер которым ты когда-либо пользовался.\n"
            "Выбери язык и давай начнем.\n\n"
            "Hi! I might be the simplest task tracker you've ever used.\n"
            "Choose a language and let's get started."
        ),
        "choose_lang_prompt": "👉 Выбери язык ниже:",
        "lang_saved": "Готово! Язык: Русский.",
        "help": (
            "Команды:\n"
            "/add <текст с датой/временем> — добавить задачу\n"
            "/today — дела на сегодня\n"
            "/on DD.MM — дела на дату\n"
            "/daily HH:MM — время утренней сводки\n"
            "/remind <минуты> — за сколько минут до задачи напоминать (например 30)\n"
            "/reminders on|off — включить/выключить напоминания\n"
            "/tz — автоопределение часового пояса или вручную (например /tz Europe/Rome)\n"
            "/lang — сменить язык"
        ),
        "state_summary": (
            "Текущий часовой пояс: {tz}; сводка: {hh:02d}:{mm:02d}; "
            "напоминания: {rem} за {lead} мин; без даты = {pref}"
        ),
        "pref_yes": "считать как сегодня",
        "pref_no": "спрашивать",
        "daily_set": "Сводка будет приходить в {hh:02d}:{mm:02d} по {tz}",
        "remind_set": "Напоминания будут приходить за {lead} мин до задачи",
        "reminders_on": "Напоминания: включены",
        "reminders_off": "Напоминания: выключены",
        "tz_updated": "Часовой пояс обновлён: {tz}",
        "tz_geo_prompt": "Поделись геолокацией, чтобы я выставил твой часовой пояс автоматически.",
        "tz_geo_fail": "Не удалось определить часовой пояс.",
        "ask_add_today": "Эта задача без даты и времени. Добавить её на сегодня? (да/нет)",
        "ask_make_default": "Ок, добавил на сегодня. Делать так всегда для задач без даты/времени? (да/нет)",
        "answer_yesno": "Ответь, пожалуйста, 'да' или 'нет'.",
        "added_today_nodt": "Добавил на сегодня [без времени]: {text}",
        "not_added_hint": (
            "Ок, не добавляю. Укажи дату/время, например: 'завтра 14:00 созвон' или "
            "'15.09 купить билеты' (добавлю без времени на эту дату).\nТакже ты можешь посмотреть планы командой /on DD.MM или /today."
        ),
        "added_task": "Ок, добавил: {text}\nНа {date} {when}",
        "today_list": "Задачи на сегодня ({date}):\n{list}",
        "on_list": "Задачи на {date}:\n{list}",
        "empty": "Пока пусто",
        "reminder": "⏰ Напоминание: {text}\nВ {time}",
        "summary": "Доброе утро! Вот план на сегодня ({date}):\n{list}",
        "format_on": "Формат: /on DD.MM",
        "format_daily": "Формат: /daily HH:MM",
        "format_remind": "Формат: /remind <минуты>, например /remind 30",
        "format_reminders": "Формат: /reminders on|off",
        "time_invalid": "Время некорректно. Пример: /daily 09:30",
        "range_invalid": "Значение вне диапазона (0..1440)",
        "tz_invalid": "Не знаю такой зоны. Пример: /tz Europe/Rome",
        "tip_setup": "Совет: сначала выставь часовой пояс /tz, затем время сводки /daily и время напоминаний /remind.",
    },
    "en": {
        "welcome": (
            "Hi! I might be the simplest task tracker you've ever used.\n"
            "Choose a language and let's get started.\n\n"
            "Привет! Возможно я самый простой таск трекер которым ты когда-либо пользовался.\n"
            "Выбери язык и давай начнем."
        ),
        "choose_lang_prompt": "👉 Choose your language:",
        "lang_saved": "Done! Language: English.",
        "help": (
            "Commands:\n"
            "/add <text with date/time> — add a task\n"
            "/today — today's tasks\n"
            "/on DD.MM — tasks on a given date\n"
            "/daily HH:MM — daily summary time\n"
            "/remind <minutes> — reminder lead time (e.g., 30)\n"
            "/reminders on|off — enable/disable reminders\n"
            "/tz — set timezone via geolocation or manually (e.g., /tz Europe/Rome)\n"
            "/lang — change language"
        ),
        "state_summary": (
            "Timezone: {tz}; summary: {hh:02d}:{mm:02d}; reminders: {rem} {lead} min; no-date = {pref}"
        ),
        "pref_yes": "treat as today",
        "pref_no": "ask",
        "daily_set": "Daily summary at {hh:02d}:{mm:02d} ({tz})",
        "remind_set": "Reminders will arrive {lead} minutes before a task",
        "reminders_on": "Reminders: enabled",
        "reminders_off": "Reminders: disabled",
        "tz_updated": "Timezone updated: {tz}",
        "tz_geo_prompt": "Share your location to set your timezone automatically.",
        "tz_geo_fail": "Couldn't determine timezone.",
        "ask_add_today": "This task has no date or time. Add it for today? (yes/no)",
        "ask_make_default": "Added for today. Always do this for tasks without date/time? (yes/no)",
        "answer_yesno": "Please reply 'yes' or 'no'.",
        "added_today_nodt": "Added for today [no time]: {text}",
        "not_added_hint": (
            "Okay, won't add it. Please specify date/time, e.g., 'tomorrow 14:00 call' or "
            "'15.09 buy tickets' (I'll add it for that date without time).\nYou can also view plans via /on DD.MM or /today."
        ),
        "added_task": "Done: {text}\nFor {date} {when}",
        "today_list": "Today's tasks ({date}):\n{list}",
        "on_list": "Tasks for {date}:\n{list}",
        "empty": "Nothing yet",
        "reminder": "⏰ Reminder: {text}\nAt {time}",
        "summary": "Good morning! Here's your plan for today ({date}):\n{list}",
        "format_on": "Format: /on DD.MM",
        "format_daily": "Format: /daily HH:MM",
        "format_remind": "Format: /remind <minutes>, e.g. /remind 30",
        "format_reminders": "Format: /reminders on|off",
        "time_invalid": "Invalid time. Example: /daily 09:30",
        "range_invalid": "Value out of range (0..1440)",
        "tz_invalid": "Unknown zone. Example: /tz Europe/Rome",
        "tip_setup": "Tip: set timezone via /tz, then daily summary via /daily and reminders via /remind.",
    },
}

LANG_BTNS = [["Русский", "English"]]


def T(lang: str, key: str, **kwargs) -> str:
    d = MESSAGES.get(lang, MESSAGES[DEFAULT_LANG])
    s = d.get(key, key)
    return s.format(**kwargs) if kwargs else s

# ---------- Storage ----------

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
            prefer_no_dt_today INTEGER NOT NULL DEFAULT 0,
            lang TEXT NOT NULL DEFAULT 'ru'
        )
        """
    )
    # migrations
    try:
        cur.execute("ALTER TABLE tasks ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    for alter in [
        "ALTER TABLE settings ADD COLUMN remind_lead_min INTEGER NOT NULL DEFAULT " + str(DEFAULT_REMIND_MIN),
        "ALTER TABLE settings ADD COLUMN reminders_enabled INTEGER NOT NULL DEFAULT " + str(DEFAULT_REMINDERS_ENABLED),
        "ALTER TABLE settings ADD COLUMN prefer_no_dt_today INTEGER NOT NULL DEFAULT 0",
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

# ---------- Helpers ----------

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
        0,
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

    task_text = text.replace(matched_span, "").strip(" -—:,.;") or "Без названия"

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

# ---------- Reminders ----------

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

# ---------- Bot Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    init_db()
    tzname, hour, minute, lead_min, enabled, prefer_no_dt, lang = get_chat_settings(chat_id)
    set_chat_settings(chat_id, tzname, hour, minute, lead_min, enabled, prefer_no_dt, lang)

    await schedule_daily_summary(context, chat_id)
    await reschedule_all_reminders(context, chat_id)

    # bilingual welcome & language choice
    await update.message.reply_text(T(lang, "welcome"))
    kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(T(lang, "choose_lang_prompt"), reply_markup=kb)
    context.chat_data['lang_select'] = True


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    tzname, hour, minute, lead_min, enabled, prefer_no_dt, _ = get_chat_settings(chat_id)
    await update.message.reply_text(T(lang, "help"))
    await update.message.reply_text(T(lang, "state_summary",
                                      tz=tzname, hh=hour, mm=minute,
                                      rem=("on" if (enabled and lang=="en") else ("включены" if enabled else ("off" if lang=="en" else "выключены"))),
                                      lead=lead_min,
                                      pref=(T(lang, "pref_yes") if prefer_no_dt else T(lang, "pref_no"))))


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, _, _, _, _, prefer_no_dt, lang = get_chat_settings(chat_id)
    payload = update.message.text[len("/add"):].strip()
    parsed = parse_task_input(payload, tzname)
    if not parsed:
        if prefer_no_dt:
            tzinfo = pytz.timezone(tzname)
            now_local = datetime.now(tzinfo)
            due_local = tzinfo.localize(datetime(now_local.year, now_local.month, now_local.day, 23, 59))
            save_task(chat_id, due_local.astimezone(pytz.utc), payload.strip() or "Без названия", 1)
            await update.message.reply_text(T(lang, "added_today_nodt", text=payload.strip()))
            return
        context.chat_data['pending_no_dt'] = {'text': payload.strip(), 'stage': 'confirm_today'}
        await update.message.reply_text(T(lang, "ask_add_today"))
        return
    due_utc, task_text, all_day = parsed
    task_id = save_task(chat_id, due_utc, task_text, all_day)
    due_local = due_utc.astimezone(pytz.timezone(tzname))
    when = ("[no time]" if lang=="en" else "[без времени]") if all_day else f"{due_local.strftime('%H:%M')}"
    await update.message.reply_text(T(lang, "added_task", text=task_text, date=due_local.strftime('%d.%m'), when=("at "+when if (not all_day and lang=="en") else ("в "+when if not all_day else when))))
    await schedule_task_reminder(context, chat_id, task_id, due_utc)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, _, _, _, _, _, lang = get_chat_settings(chat_id)
    now_local = datetime.now(pytz.timezone(tzname))
    tasks = fetch_tasks_for_date(chat_id, now_local, tzname)
    await update.message.reply_text(T(lang, "today_list", date=now_local.strftime('%d.%m'), list=format_tasks(lang, tasks)))


async def on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, _, _, _, _, _, lang = get_chat_settings(chat_id)
    args = update.message.text.split()
    if len(args) != 2 or "." not in args[1]:
        await update.message.reply_text(T(lang, "format_on"))
        return
    try:
        dd, mm = args[1].split(".")
        day = int(dd); month = int(mm)
        now_local = datetime.now(pytz.timezone(tzname))
        year = now_local.year
        target = datetime(year, month, day)
    except Exception:
        await update.message.reply_text(T(lang, "format_on"))
        return
    tasks = fetch_tasks_for_date(chat_id, target, tzname)
    await update.message.reply_text(T(lang, "on_list", date=target.strftime('%d.%m'), list=format_tasks(lang, tasks)))


async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, _, _, _, _, _, lang = get_chat_settings(chat_id)
    parts = update.message.text.split()
    if len(parts) != 2 or ":" not in parts[1]:
        await update.message.reply_text(T(lang, "format_daily"))
        return
    try:
        hh, mm = map(int, parts[1].split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        await update.message.reply_text(T(lang, "time_invalid"))
        return
    set_chat_settings(chat_id, tzname=tzname, hour=hh, minute=mm)
    await schedule_daily_summary(context, chat_id, reschedule=True)
    await update.message.reply_text(T(lang, "daily_set", hh=hh, mm=mm, tz=tzname))


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, h, m, _, en, pref, lang = get_chat_settings(chat_id)
    parts = update.message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text(T(lang, "format_remind"))
        return
    lead = int(parts[1])
    if lead < 0 or lead > 24*60:
        await update.message.reply_text(T(lang, "range_invalid"))
        return
    set_chat_settings(chat_id, tzname=tzname, remind_lead_min=lead)
    await reschedule_all_reminders(context, chat_id)
    await update.message.reply_text(T(lang, "remind_set", lead=lead))


async def reminders_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, h, m, lead, _, pref, lang = get_chat_settings(chat_id)
    parts = update.message.text.split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        await update.message.reply_text(T(lang, "format_reminders"))
        return
    enable = 1 if parts[1].lower() == "on" else 0
    set_chat_settings(chat_id, tzname=tzname, reminders_enabled=enable)
    await reschedule_all_reminders(context, chat_id)
    await update.message.reply_text(T(lang, "reminders_on") if enable else T(lang, "reminders_off"))


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
        except Exception:
            await update.message.reply_text(T(lang, "tz_invalid"))
        return

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(text=("Определить по геолокации" if lang=="ru" else "Detect via geolocation"), request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await update.message.reply_text(T(lang, "tz_geo_prompt"), reply_markup=kb)


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.location:
        return
    chat_id = update.effective_chat.id
    tzname, hour, minute, lead, en, pref, lang = get_chat_settings(chat_id)
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


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang = get_chat_settings(chat_id)[-1]
    kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(T(lang, "choose_lang_prompt"), reply_markup=kb)
    context.chat_data['lang_select'] = True


async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    tzname, hour, minute, lead, enabled, prefer_no_dt, lang = get_chat_settings(chat_id)

    # language selection flow
    if context.chat_data.get('lang_select'):
        msg = update.message.text.strip().lower()
        if msg in {"русский", "russian"}:
            set_chat_settings(chat_id, lang="ru")
            context.chat_data.pop('lang_select', None)
            await update.message.reply_text(MESSAGES['ru']["lang_saved"], reply_markup=ReplyKeyboardRemove())
            await help_cmd(update, context)
            return
        if msg in {"english", "английский"}:
            set_chat_settings(chat_id, lang="en")
            context.chat_data.pop('lang_select', None)
            await update.message.reply_text(MESSAGES['en']["lang_saved"], reply_markup=ReplyKeyboardRemove())
            await help_cmd(update, context)
            return
        # ask again
        kb = ReplyKeyboardMarkup(LANG_BTNS, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(T(lang, "choose_lang_prompt"), reply_markup=kb)
        return

    # pending no-date flow
    pending = context.chat_data.get('pending_no_dt')
    if pending:
        msg = update.message.text.strip().lower()
        yes = {"да", "ага", "угу", "yes", "y"}
        no = {"нет", "no", "n", "не"}
        if pending.get('stage') == 'confirm_today':
            if msg in yes:
                tzinfo = pytz.timezone(tzname)
                now_local = datetime.now(tzinfo)
                due_local = tzinfo.localize(datetime(now_local.year, now_local.month, now_local.day, 23, 59))
                save_task(chat_id, due_local.astimezone(pytz.utc), pending['text'], 1)
                context.chat_data['pending_no_dt'] = {'stage': 'ask_default'}
                await update.message.reply_text(T(lang, "ask_make_default"))
                return
            elif msg in no:
                context.chat_data.pop('pending_no_dt', None)
                await update.message.reply_text(T(lang, "not_added_hint"))
                return
        elif pending.get('stage') == 'ask_default':
            if msg in yes:
                set_chat_settings(chat_id, prefer_no_dt_today=1)
                context.chat_data.pop('pending_no_dt', None)
                await update.message.reply_text(
                    "Запомнил: задачи без даты — всегда добавлять на сегодня." if lang=="ru" else "Saved: tasks without date will be added for today." )
                return
            elif msg in no:
                set_chat_settings(chat_id, prefer_no_dt_today=0)
                context.chat_data.pop('pending_no_dt', None)
                await update.message.reply_text(
                    "Ок, буду спрашивать каждый раз." if lang=="ru" else "Okay, I'll ask every time." )
                return
        await update.message.reply_text(T(lang, "answer_yesno"))
        return

    # regular text → try parse
    parsed = parse_task_input(update.message.text, tzname)
    if parsed:
        due_utc, task_text, all_day = parsed
        task_id = save_task(chat_id, due_utc, task_text, all_day)
        due_local = due_utc.astimezone(pytz.timezone(tzname))
        when_str = ("[no time]" if lang=="en" else "[без времени]") if all_day else f"{due_local.strftime('%H:%M')}"
        when_prefix = ("at " if (not all_day and lang=="en") else ("в " if not all_day else ""))
        await update.message.reply_text(T(lang, "added_task", text=task_text, date=due_local.strftime('%d.%m'), when=(when_prefix+when_str if when_prefix else when_str)))
        await schedule_task_reminder(context, chat_id, task_id, due_utc)
    else:
        if prefer_no_dt:
            tzinfo = pytz.timezone(tzname)
            now_local = datetime.now(tzinfo)
            due_local = tzinfo.localize(datetime(now_local.year, now_local.month, now_local.day, 23, 59))
            save_task(chat_id, due_local.astimezone(pytz.utc), update.message.text.strip() or ("Без названия" if lang=="ru" else "Untitled"), 1)
            await update.message.reply_text(T(lang, "added_today_nodt", text=update.message.text.strip()))
            return
        context.chat_data['pending_no_dt'] = {'text': update.message.text.strip(), 'stage': 'confirm_today'}
        await update.message.reply_text(T(lang, "ask_add_today"))
        return

# ---------- Scheduler ----------

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


# ---------- Dev helper: parser smoke tests ----------

def _run_parser_smoke_tests():
    samples = [
        "16:00 08.08 Позвонить маме",
        "08.08 16:00 Встреча",
        "август 16.00 созвон",
        "завтра 09:15 пробежка",
        "15 сентября 14 00 дедлайн",
        "15 сентября доклад",   # дата без времени → all_day
        "сегодня в 18 встреча",
        "купить хлеб",          # без даты/времени — интерактив/правило по умолчанию
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


# ---------- Main ----------

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
        .post_init(_post_init)   # <-- подключаем пост-инициализацию
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("on", on_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("reminders", reminders_toggle_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))

    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

