"""
Scheduler Service для утренних брифингов через APScheduler
"""
import os
from datetime import datetime, time
from typing import List, Dict
import pytz

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from services.ai_service import generate_morning_briefing
from services.calendar_service import get_credentials_from_stored
from services.db_service import get_google_tokens
from googleapiclient.discovery import build


scheduler = AsyncIOScheduler()


def get_today_events(credentials, user_timezone: str) -> List[Dict]:
    """
    Получает события на сегодня из Google Calendar.
    
    Args:
        credentials: Google OAuth credentials
        user_timezone: Часовой пояс пользователя
    
    Returns:
        Список событий
    """
    try:
        service = build('calendar', 'v3', credentials=credentials)
        tz = pytz.timezone(user_timezone)
        now_local = datetime.now(tz)
        
        # Начало и конец дня в локальном времени
        start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day.replace(hour=23, minute=59, second=59)
        
        # Конвертируем в UTC для API
        start_utc = start_of_day.astimezone(pytz.utc).isoformat()
        end_utc = end_of_day.astimezone(pytz.utc).isoformat()
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=start_utc,
            timeMax=end_utc,
            maxResults=50,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        # Форматируем события
        formatted_events = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'No title')
            description = event.get('description', '')
            
            formatted_events.append({
                'summary': summary,
                'start_time': start,
                'description': description
            })
        
        return formatted_events
    except Exception as e:
        print(f"[Scheduler Service] Ошибка при получении событий: {e}")
        return []


async def send_morning_briefing(bot, chat_id: int, user_timezone: str):
    """
    Отправляет утренний брифинг пользователю.
    
    Args:
        bot: Telegram Bot instance
        chat_id: ID чата пользователя
        user_timezone: Часовой пояс пользователя
    """
    try:
        # Получаем токены пользователя
        stored_tokens = get_google_tokens(chat_id)
        if not stored_tokens:
            # Если нет авторизации, отправляем простое сообщение
            await bot.send_message(
                chat_id=chat_id,
                text="Good morning! 🌅 Connect your Google Calendar to receive daily briefings."
            )
            return
        
        credentials = get_credentials_from_stored(chat_id, stored_tokens)
        if not credentials:
            await bot.send_message(
                chat_id=chat_id,
                text="Good morning! 🌅 Please reconnect your Google Calendar."
            )
            return
        
        # Получаем события на сегодня
        events = get_today_events(credentials, user_timezone)
        
        # Генерируем брифинг через AI
        briefing = await generate_morning_briefing(events, user_timezone)
        
        # Отправляем брифинг
        await bot.send_message(
            chat_id=chat_id,
            text=briefing
        )
    except Exception as e:
        print(f"[Scheduler Service] Ошибка при отправке брифинга: {e}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Good morning! 🌅 Have a great day!"
            )
        except:
            pass


def schedule_morning_briefing(bot, chat_id: int, user_timezone: str, hour: int = 9, minute: int = 0):
    """
    Планирует утренний брифинг для пользователя.
    
    Args:
        bot: Telegram Bot instance
        chat_id: ID чата пользователя
        user_timezone: Часовой пояс пользователя
        hour: Час отправки (по умолчанию 9)
        minute: Минута отправки (по умолчанию 0)
    """
    try:
        tz = pytz.timezone(user_timezone)
        
        # Удаляем старые задачи для этого пользователя
        job_id = f"morning_briefing_{chat_id}"
        try:
            scheduler.remove_job(job_id)
        except:
            pass
        
        # Создаем новую задачу
        scheduler.add_job(
            send_morning_briefing,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[bot, chat_id, user_timezone],
            id=job_id,
            replace_existing=True
        )
    except Exception as e:
        print(f"[Scheduler Service] Ошибка при планировании брифинга: {e}")


def start_scheduler():
    """Запускает scheduler"""
    if not scheduler.running:
        scheduler.start()
        print("[Scheduler Service] Scheduler started")


def stop_scheduler():
    """Останавливает scheduler"""
    if scheduler.running:
        scheduler.shutdown()
        print("[Scheduler Service] Scheduler stopped")

