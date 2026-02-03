"""
Scheduler Service для утренних и вечерних сводок через APScheduler
Использует cron job, который запускается каждый час и проверяет всех пользователей
"""
import os
from datetime import datetime, timedelta
from typing import List, Dict
import pytz

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from services.ai_service import generate_morning_briefing_intro
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
            event_id = event.get('id', '')
            
            formatted_events.append({
                'id': event_id,
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
        
        # Если нет задач, отправляем специальное сообщение
        if not events:
            await bot.send_message(
                chat_id=chat_id,
                text="No tasks for today yet. Enjoy your freedom!"
            )
            return
        
        # Генерируем только вступление через AI
        intro = await generate_morning_briefing_intro()
        
        # Форматируем список задач (Time - Title)
        tz = pytz.timezone(user_timezone)
        tasks_list = []
        for event in events:
            summary = event.get('summary', 'Task')
            # Убираем "✅ " если есть (для отображения)
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
                except:
                    pass
            
            if time_str:
                tasks_list.append(f"{time_str} - {summary}")
            else:
                tasks_list.append(summary)
        
        # Объединяем вступление и список задач
        briefing = f"{intro}\n\n" + "\n".join(tasks_list)
        
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
    Отправляет вечернюю сводку пользователю с inline-кнопками для отметки задач.
    
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
        
        # Разделяем выполненные и невыполненные задачи
        completed_events = [e for e in events if e.get('summary', '').startswith('✅ ')]
        incomplete_events = [e for e in events if not e.get('summary', '').startswith('✅ ')]
        
        # Формируем сообщение - только интро
        message_text = "Hey, hope it was a productive day!\n\n"
        
        # Добавляем информацию о выполненных задачах в текст
        if completed_events:
            tz = pytz.timezone(user_timezone)
            message_text += "✅ Completed:\n"
            for event in completed_events:
                summary = event.get('summary', 'Task')
                # Убираем "✅ " для отображения
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
                    except:
                        pass
                
                if time_str:
                    message_text += f"  • {time_str} - {summary}\n"
                else:
                    message_text += f"  • {summary}\n"
            message_text += "\n"
        
        # Добавляем информацию о невыполненных задачах
        if incomplete_events:
            message_text += "📋 Tasks left behind:\n"
        else:
            message_text += "🎉 No uncompleted tasks! Great job!"
        
        # Создаем inline-клавиатуру для невыполненных задач
        # Каждая задача представлена кнопками inline
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        keyboard = []
        tz = pytz.timezone(user_timezone)
        for event in incomplete_events:
            event_id = event.get('id', '')
            if event_id:
                summary = event.get('summary', 'Task')
                start_time = event.get('start_time', '')
                time_str = ""
                if start_time:
                    try:
                        if 'T' in start_time:
                            dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                            if dt.tzinfo:
                                dt = dt.astimezone(tz)
                                time_str = f" {dt.strftime('%H:%M')}"
                    except:
                        pass
                
                # Создаем кнопку с названием задачи (Row 1)
                # Ограничиваем длину до ~40 символов для кнопки (Telegram лимит 64)
                task_display = f"{summary}{time_str}"
                if len(task_display) > 40:
                    task_display = f"{summary[:35]}{time_str}"
                
                # Row 1: Кнопка "✅ Done: Task Name"
                done_button_text = f"✅ Done: {task_display}"
                if len(done_button_text) > 64:
                    done_button_text = f"✅ Done: {summary[:15]}..."
                
                # Row 2: Кнопки "➡️ Reschedule" и "❌ Delete"
                keyboard.append([InlineKeyboardButton(done_button_text, callback_data=f"done_{event_id}")])
                keyboard.append([
                    InlineKeyboardButton("➡️ Reschedule", callback_data=f"reschedule_{event_id}"),
                    InlineKeyboardButton("❌ Delete", callback_data=f"cancel_{event_id}")
                ])
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        # Отправляем сообщение с кнопками
        await bot.send_message(
            chat_id=chat_id,
            text=message_text,
            reply_markup=reply_markup
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
