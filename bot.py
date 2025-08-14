"""
Telegram Task Tracker Bot

Features:
- Add tasks via command or plain message: "HH:MM DD.MM Task text"
- /add HH:MM DD.MM Task text — add a task
- /today — list today's tasks
- /on DD.MM — list tasks on a specific day
- Daily morning summary at 08:00 (per-user) in Europe/Amsterdam timezone by default
- Simple SQLite storage

Requirements (pip):
python-telegram-bot==20.7
pytz==2024.1
python-dateutil==2.9.0

Run:
export BOT_TOKEN=123:ABC
python bot.py

Notes:
- Default timezone is Europe/Amsterdam. You can change DEFAULT_TZ below.
- Each chat gets its own daily 08:00 summary job when they /start the bot.
- You can customize the summary time with /daily HH:MM (optional feature included).
"""

import os
import sqlite3
from datetime import datetime, time, timedelta
from dateutil import parser as dateparser
from dateutil import tz
import pytz
from typing import Optional, Tuple, List

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

DB_PATH = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Amsterdam")
SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "8"))
SUMMARY_MINUTE = int(os.getenv("SUMMARY_MINUTE", "0"))

# ---------- Storage ----------

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
            done INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL,
            daily_hour INTEGER NOT NULL,
            daily_minute INTEGER NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def get_con():
    return sqlite3.connect(DB_PATH)


# ---------- Helpers ----------

def get_chat_settings(chat_id: int) -> Tuple[str, int, int]:
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT tz, daily_hour, daily_minute FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    if row:
        return row[0], int(row[1]), int(row[2])
    return DEFAULT_TZ, SUMMARY_HOUR, SUMMARY_MINUTE


def set_chat_settings(chat_id: int, tzname: str, hour: int, minute: int):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO settings (chat_id, tz, daily_hour, daily_minute) VALUES (?, ?, ?, ?)\n         ON CONFLICT(chat_id) DO UPDATE SET tz=excluded.tz, daily_hour=excluded.daily_hour, daily_minute=excluded.daily_minute",
        (chat_id, tzname, hour, minute),
    )
    con.commit()
    con.close()


def parse_task_input(text: str, chat_tz: str) -> Optional[Tuple[datetime, str]]:
    """
    Expect formats like:
    - "HH:MM DD.MM Task text"
    - "HH:MM DD.MM" + newline + "Task text"
    Returns (due_dt_utc, task_text) or None
    """
    text = text.strip()
    if "\n" in text:
        header, body = text.split("\n", 1)
        candidate = f"{header} {body.strip()}"
    else:
        candidate = text

    # Split once on the first space after date
    # First two tokens should be time and date
    parts = candidate.split(maxsplit=2)
    if len(parts) < 3:
        return None
    hhmm, ddmm, task_text = parts[0], parts[1], parts[2].strip()

    try:
        # validate time
        hh, mm = hhmm.split(":")
        hour, minute = int(hh), int(mm)
        # validate date
        dd, mm_ = ddmm.split(".")
        day, month = int(dd), int(mm_)

        now_local = datetime.now(pytz.timezone(chat_tz))
        year = now_local.year
        # If date already passed this year, assume next year
        candidate_local = pytz.timezone(chat_tz).localize(datetime(year, month, day, hour, minute))
        if candidate_local < now_local - timedelta(minutes=1):
            candidate_local = candidate_local.replace(year=year + 1)

        due_utc = candidate_local.astimezone(pytz.utc)
        return due_utc, task_text
    except Exception:
        return None


def save_task(chat_id: int, due_utc: datetime, text: str):
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO tasks (chat_id, text, due_utc, created_utc, done) VALUES (?, ?, ?, ?, 0)",
        (
            chat_id,
            text,
            due_utc.isoformat(),
            datetime.utcnow().isoformat(),
        ),
    )
    con.commit()
    con.close()


def fetch_tasks_for_date(chat_id: int, day: datetime, chat_tz: str) -> List[Tuple[int, str, datetime]]:
    tzinfo = pytz.timezone(chat_tz)
    start_local = tzinfo.localize(datetime(day.year, day.month, day.day, 0, 0))
    end_local = start_local + timedelta(days=1)

    start_utc = start_local.astimezone(pytz.utc).isoformat()
    end_utc = end_local.astimezone(pytz.utc).isoformat()

    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT id, text, due_utc FROM tasks WHERE chat_id=? AND due_utc >= ? AND due_utc < ? AND done=0 ORDER BY due_utc ASC",
        (chat_id, start_utc, end_utc),
    )
    rows = cur.fetchall()
    con.close()

    tasks = []
    for _id, text, due_iso in rows:
        due_dt_local = datetime.fromisoformat(due_iso).astimezone(tzinfo)
        tasks.append((_id, text, due_dt_local))
    return tasks


def format_tasks(tasks: List[Tuple[int, str, datetime]]) -> str:
    if not tasks:
        return "Пока пусто"
    lines = []
    for _id, text, due_local in tasks:
        lines.append(f"• {due_local.strftime('%H:%M')} — {text}")
    return "\n".join(lines)


# ---------- Bot Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # ensure DB and default settings
    init_db()
    tzname, hour, minute = get_chat_settings(chat_id)
    set_chat_settings(chat_id, tzname, hour, minute)

    # schedule daily summary for this chat
    await schedule_daily_summary(context, chat_id)

    await update.message.reply_text(
        "Привет! Я трекер задач. Кидай мне в сообщении:\n"
        "HH:MM DD.MM Текст задачи\n\n"
        "Команды:\n"
        "/add HH:MM DD.MM Текст — добавить задачу\n"
        "/today — дела на сегодня\n"
        "/on DD.MM — дела на конкретный день\n"
        "/daily HH:MM — время утренней сводки (по умолчанию 08:00)\n"
        f"Текущий часовой пояс: {tzname}"
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, *_ = get_chat_settings(chat_id)
    text = update.message.text
    payload = text[len("/add"):].strip()
    parsed = parse_task_input(payload, tzname)
    if not parsed:
        await update.message.reply_text("Не понял формат. Пример: /add 14:30 15.08 Позвонить маме")
        return
    due_utc, task_text = parsed
    save_task(chat_id, due_utc, task_text)
    due_local = due_utc.astimezone(pytz.timezone(tzname))
    await update.message.reply_text(
        f"Ок, добавил: {task_text}\nНа {due_local.strftime('%H:%M %d.%m (%Z)')}"
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, *_ = get_chat_settings(chat_id)
    now_local = datetime.now(pytz.timezone(tzname))
    tasks = fetch_tasks_for_date(chat_id, now_local, tzname)
    await update.message.reply_text(
        f"Задачи на сегодня ({now_local.strftime('%d.%m')}):\n" + format_tasks(tasks)
    )


async def on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, *_ = get_chat_settings(chat_id)
    args = update.message.text.split()
    if len(args) != 2:
        await update.message.reply_text("Формат: /on DD.MM")
        return
    try:
        dd, mm = args[1].split(".")
        day = int(dd)
        month = int(mm)
        now_local = datetime.now(pytz.timezone(tzname))
        year = now_local.year
        target = datetime(year, month, day)
    except Exception:
        await update.message.reply_text("Не понял дату. Пример: /on 16.08")
        return
    tasks = fetch_tasks_for_date(chat_id, target, tzname)
    await update.message.reply_text(
        f"Задачи на {target.strftime('%d.%m')}:\n" + format_tasks(tasks)
    )


async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    parts = update.message.text.split()
    if len(parts) != 2 or ":" not in parts[1]:
        await update.message.reply_text("Формат: /daily HH:MM")
        return
    try:
        hh, mm = map(int, parts[1].split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        await update.message.reply_text("Время некорректно. Пример: /daily 09:30")
        return
    tzname, _, _ = get_chat_settings(chat_id)
    set_chat_settings(chat_id, tzname, hh, mm)
    # reschedule
    await schedule_daily_summary(context, chat_id, reschedule=True)
    await update.message.reply_text(f"Сводка будет приходить в {hh:02d}:{mm:02d} по {tzname}")


async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    tzname, *_ = get_chat_settings(chat_id)
    parsed = parse_task_input(update.message.text, tzname)
    if parsed:
        due_utc, task_text = parsed
        save_task(chat_id, due_utc, task_text)
        due_local = due_utc.astimezone(pytz.timezone(tzname))
        await update.message.reply_text(
            f"Добавил: {task_text}\nНа {due_local.strftime('%H:%M %d.%m (%Z)')}"
        )
    else:
        await update.message.reply_text(
            "Не понял. Формат: HH:MM DD.MM Задача. Либо используй /add, /today, /on"
        )


# ---------- Scheduler ----------

async def schedule_daily_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reschedule: bool = False):
    tzname, hour, minute = get_chat_settings(chat_id)
    tzinfo = pytz.timezone(tzname)

    job_name = f"summary_{chat_id}"
    if reschedule:
        # remove old if exists
        old = context.job_queue.get_jobs_by_name(job_name)
        for j in old:
            j.schedule_removal()

    # schedule a new one
    context.job_queue.run_daily(
        callback=daily_summary_job,
        time=time(hour=hour, minute=minute, tzinfo=tzinfo),
        name=job_name,
        data={"chat_id": chat_id},
    )


async def daily_summary_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.data["chat_id"]
    tzname, *_ = get_chat_settings(chat_id)
    now_local = datetime.now(pytz.timezone(tzname))
    tasks = fetch_tasks_for_date(chat_id, now_local, tzname)
    text = f"Доброе утро! Вот твой план на сегодня ({now_local.strftime('%d.%m')}):\n" + format_tasks(tasks)
    await ctx.bot.send_message(chat_id=chat_id, text=text)


# ---------- Main ----------

def main():
    init_db()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN env variable")

    app: Application = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("on", on_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
