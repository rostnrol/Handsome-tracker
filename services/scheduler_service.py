"""
Scheduler Service для утренних и вечерних сводок через APScheduler
Использует cron job, который запускается каждый час и проверяет всех пользователей
"""
import os
from datetime import datetime
from typing import List, Dict
import pytz

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from services.ai_service import generate_morning_briefing
from services.calendar_service import get_credentials_from_stored
from services.db_service import get_google_tokens, get_user_timezone, get_morning_time, get_evening_time
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
        print(f"[Scheduler Service] Ошибка при отправке утреннего брифинга: {e}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Good morning! 🌅 Have a great day!"
            )
        except:
            pass


async def send_evening_recap(bot, chat_id: int, user_timezone: str):
    """
    Отправляет вечернюю сводку пользователю.
    
    Args:
        bot: Telegram Bot instance
        chat_id: ID чата пользователя
        user_timezone: Часовой пояс пользователя
    """
    try:
        # Получаем токены пользователя
        stored_tokens = get_google_tokens(chat_id)
        if not stored_tokens:
            await bot.send_message(
                chat_id=chat_id,
                text="Good evening! 🌙 Connect your Google Calendar to receive evening recaps."
            )
            return
        
        credentials = get_credentials_from_stored(chat_id, stored_tokens)
        if not credentials:
            await bot.send_message(
                chat_id=chat_id,
                text="Good evening! 🌙 Please reconnect your Google Calendar."
            )
            return
        
        # Получаем события на сегодня
        events = get_today_events(credentials, user_timezone)
        
        # Генерируем вечернюю сводку через AI
        recap = await generate_evening_recap(events, user_timezone)
        
        # Отправляем сводку
        await bot.send_message(
            chat_id=chat_id,
            text=recap
        )
    except Exception as e:
        print(f"[Scheduler Service] Ошибка при отправке вечерней сводки: {e}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Good evening! 🌙 Have a restful night!"
            )
        except:
            pass


async def generate_evening_recap(events: list, user_timezone: str) -> str:
    """
    Генерирует вечернюю сводку на основе событий дня через AI.
    
    Args:
        events: Список событий на день
        user_timezone: Часовой пояс пользователя
    
    Returns:
        Текст вечерней сводки
    """
    from services.ai_service import client
    
    if not events:
        return "Good evening! 🌙\n\nYou had no events scheduled for today. Hope you had a productive day!"
    
    events_text = "\n".join([
        f"- {event.get('summary', 'Event')} at {event.get('start_time', '')}"
        for event in events
    ])
    
    if not client:
        # Fallback к простому формату если нет OpenAI ключа
        return f"Good evening! 🌙\n\nToday you had {len(events)} event(s):\n{events_text}\n\nLet's reflect on what can be transferred to tomorrow and what can be forgotten."
    
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful evening recap assistant. Generate a friendly, reflective evening recap based on the user's calendar events for the day. Help them reflect on what can be transferred to the next day and what can be forgotten."
                },
                {
                    "role": "user",
                    "content": f"Generate an evening recap for today. Events:\n{events_text}\n\nMake it friendly, reflective (2-3 sentences), and help identify what can be transferred to tomorrow and what can be forgotten."
                }
            ],
            temperature=0.7,
            max_tokens=250
        )
        
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Scheduler Service] Ошибка при генерации вечерней сводки: {e}")
        # Fallback
        return f"Good evening! 🌙\n\nToday you had {len(events)} event(s):\n{events_text}\n\nLet's reflect on what can be transferred to tomorrow and what can be forgotten."


async def check_and_send_briefings(bot):
    """
    Проверяет всех пользователей и отправляет сводки, если наступило их время.
    Запускается каждую минуту через cron job, чтобы поддерживать любые времена
    (например, 09:30, 21:45), а не только :00 минут.
    """
    from services.db_service import get_con
    
    try:
        con = get_con()
        cur = con.cursor()
        # Получаем всех пользователей, которые прошли онбординг
        cur.execute("""
            SELECT chat_id, tz, morning_time, evening_time 
            FROM settings 
            WHERE onboard_done = 1 AND tz IS NOT NULL
        """)
        users = cur.fetchall()
        con.close()
        
        now_utc = datetime.now(pytz.utc)
        
        for chat_id, tz_str, morning_time, evening_time in users:
            if not tz_str:
                continue
            
            try:
                # Получаем локальное время пользователя
                user_tz = pytz.timezone(tz_str)
                now_local = now_utc.astimezone(user_tz)
                current_time_str = now_local.strftime("%H:%M")
                
                # Проверяем, нужно ли отправить утреннюю сводку
                if morning_time and current_time_str == morning_time:
                    print(f"[Scheduler] Sending morning briefing to {chat_id} at {current_time_str} ({tz_str})")
                    await send_morning_briefing(bot, chat_id, tz_str)
                
                # Проверяем, нужно ли отправить вечернюю сводку
                if evening_time and current_time_str == evening_time:
                    print(f"[Scheduler] Sending evening recap to {chat_id} at {current_time_str} ({tz_str})")
                    await send_evening_recap(bot, chat_id, tz_str)
                    
            except Exception as e:
                print(f"[Scheduler Service] Ошибка при обработке пользователя {chat_id}: {e}")
                continue
                
    except Exception as e:
        print(f"[Scheduler Service] Ошибка при проверке сводок: {e}")


def start_scheduler(bot):
    """
    Запускает scheduler с cron job, который проверяет пользователей каждую минуту.
    Это необходимо, чтобы отправлять сводки в любое время, указанное пользователем
    (например, 09:30, 21:45), а не только в :00 минут.
    
    Args:
        bot: Telegram Bot instance
    """
    if not scheduler.running:
        # Запускаем cron job каждую минуту для проверки всех пользователей
        scheduler.add_job(
            check_and_send_briefings,
            trigger=CronTrigger(minute="*"),  # Каждую минуту
            args=[bot],
            id="minute_briefings_check",
            replace_existing=True
        )
        scheduler.start()
        print("[Scheduler Service] Scheduler started with per-minute cron job")


def stop_scheduler():
    """Останавливает scheduler"""
    if scheduler.running:
        scheduler.shutdown()
        print("[Scheduler Service] Scheduler stopped")
