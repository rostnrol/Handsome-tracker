"""
Telegram Task Tracker Bot

Features:
- Добавление задач простым сообщением в свободном формате даты/времени:
  Примеры: "16:00 08.08 Позвонить", "08.08 16:00 Встреча",
           "август 16.00 созвон", "завтра 09:15 пробежка",
           "15 сентября 14 00 дедлайн", "сегодня в 18 встреча"
- Команды:
  /add <текст со временем/датой> — добавить задачу
  /today — список дел на сегодня
  /on DD.MM — список дел на конкретный день
  /daily HH:MM — время утренней сводки
  /tz [IANA] — выставить часовой пояс вручную или по геолокации
- Утренняя сводка каждый день в выбранное время (по умолчанию 08:00)
- SQLite-хранилище

Зависимости (requirements.txt):
python-telegram-bot==20.7
pytz==2024.1
python-dateutil==2.9.0
dateparser==1.2.0
timezonefinder==6.5.2
"""

import os
import sqlite3
from datetime import datetime, time, timedelta
from typing import Optional, Tuple, List

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

from timezonefinder import TimezoneFinder

DB_PATH = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Rome")
SUMMARY_HOUR = int(os.getenv("SUMMARY_HOUR", "8"))
SUMMARY_MINUTE = int(os.getenv("SUMMARY_MINUTE", "0"))

TF = TimezoneFinder()

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
        "INSERT INTO settings (chat_id, tz, daily_hour, daily_minute) VALUES (?, ?, ?, ?)\n"
        "ON CONFLICT(chat_id) DO UPDATE SET tz=excluded.tz, daily_hour=excluded.daily_hour, daily_minute=excluded.daily_minute",
        (chat_id, tzname, hour, minute),
    )
    con.commit()
    con.close()


def tz_from_location(lat: float, lon: float) -> Optional[str]:
    try:
        tzname = TF.timezone_at(lng=lon, lat=lat)
        return tzname
    except Exception:
        return None


def parse_task_input(text: str, chat_tz: str) -> Optional[Tuple[datetime, str]]:
    """
    Гибкий парсер даты/времени + текста задачи.
    Понимает варианты:
    - "16:00 08.08 Позвонить"
    - "08.08 16:00 Встреча"
    - "август 16.00 созвон"
    - "завтра 09:15 пробежка"
    - "15 сентября 14 00 дедлайн"
    - "сегодня в 18 встреча"
    Возвращает (due_utc, task_text) или None.
    """
    tzinfo = pytz.timezone(chat_tz)
    now_local = datetime.now(tzinfo)

    settings = {
        "TIMEZONE": chat_tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",   # без года — ближайшее будущее
        "DATE_ORDER": "DMY",
        "RELATIVE_BASE": now_local,      # для "сегодня/завтра/через 2 часа"
    }

    # Ищем первую дату/время на RU/EN/IT
    results = search_dates(
        text,
        languages=["ru", "en", "it"],
        settings=settings
    )
    if not results:
        return None

    matched_span, dt = results[0]  # ('завтра 16:00', datetime...)
    if dt.tzinfo is None:
        dt = tzinfo.localize(dt)

    # Остаток — текст задачи
    task_text = text.replace(matched_span, "").strip(" -—:,.;")
    if not task_text:
        task_text = "Без названия"

    due_utc = dt.astimezone(pytz.utc)
    # Если ушло в прошлое (редко бывает из-за распознавания) — сдвиг на год вперёд
    if due_utc < datetime.now(pytz.utc) - timedelta(minutes=1):
        try:
            due_utc = due_utc.replace(year=due_utc.year + 1)
        except ValueError:
            pass

    return due_utc, task_text


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
    init_db()
    tzname, hour, minute = get_chat_settings(chat_id)
    set_chat_settings(chat_id, tzname, hour, minute)

    # schedule daily summary for this chat
    await schedule_daily_summary(context, chat_id)

    await update.message.reply_text(
        "Привет! Я трекер задач. Кидай мне сообщение с датой/временем и текстом, например:\n"
        "16:00 08.08 Позвонить маме\n"
        "или: завтра 09:15 пробежка / сегодня в 18 встреча / 15 сентября 14 00 дедлайн\n\n"
        "Команды:\n"
        "/add <текст с датой и временем> — добавить задачу\n"
        "/today — дела на сегодня\n"
        "/on DD.MM — дела на конкретный день\n"
        "/daily HH:MM — время утренней сводки (по умолчанию 08:00)\n"
        "/tz [IANA] — выставить часовой пояс или поделиться геолокацией\n"
        f"Текущий часовой пояс: {tzname}"
    )

    # предложить автоопределение TZ
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(text="Определить по геолокации", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "Хочешь, подберу твой часовой пояс по геолокации? Нажми кнопку ниже.",
        reply_markup=kb
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname, *_ = get_chat_settings(chat_id)
    text = update.message.text
    payload = text[len("/add"):].strip()
    parsed = parse_task_input(payload, tzname)
    if not parsed:
        await update.message.reply_text("Не понял формат. Пример: /add завтра 16:00 Позвонить маме")
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
    if len(args) != 2 or "." not in args[1]:
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
    await schedule_daily_summary(context, chat_id, reschedule=True)
    await update.message.reply_text(f"Сводка будет приходить в {hh:02d}:{mm:02d} по {tzname}")


async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = update.message.text.split(maxsplit=1)
    if len(args) == 2:
        tzname = args[1].strip()
        try:
            pytz.timezone(tzname)  # валидация
            _, hour, minute = get_chat_settings(chat_id)
            set_chat_settings(chat_id, tzname, hour, minute)
            await update.message.reply_text(f"Часовой пояс обновлён: {tzname}")
            await schedule_daily_summary(context, chat_id, reschedule=True)
        except Exception:
            await update.message.reply_text("Не знаю такой зоны. Пример: /tz Europe/Rome")
        return

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(text="Определить по геолокации", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "Поделись геолокацией, чтобы я выставил твой часовой пояс автоматически.",
        reply_markup=kb
    )


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.location:
        return
    chat_id = update.effective_chat.id
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    tzname = tz_from_location(lat, lon)
    if not tzname:
        await update.message.reply_text(
            "Не удалось определить часовой пояс по геолокации :(",
            reply_markup=ReplyKeyboardRemove()
        )
        return
    _, hour, minute = get_chat_settings(chat_id)
    set_chat_settings(chat_id, tzname, hour, minute)
    await update.message.reply_text(
        f"Готово! Выставил часовой пояс: {tzname}",
        reply_markup=ReplyKeyboardRemove()
    )
    await schedule_daily_summary(context, chat_id, reschedule=True)


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
            "Не понял. Напиши дату/время и текст задачи (например: завтра 16:00 Позвонить) "
            "или используй /add, /today, /on"
        )


# ---------- Scheduler ----------

async def schedule_daily_summary(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reschedule: bool = False):
    tzname, hour, minute = get_chat_settings(chat_id)
    tzinfo = pytz.timezone(tzname)

    job_name = f"summary_{chat_id}"
    if reschedule:
        old = context.job_queue.get_jobs_by_name(job_name)
        for j in old:
            j.schedule_removal()

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
    app.add_handler(CommandHandler("tz", tz_cmd))

    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
