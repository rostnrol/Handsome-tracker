"""
AI Service для обработки текста, голоса и фото с использованием OpenAI API
"""
import os
import re
import json
import base64
from typing import Dict, Optional
from datetime import datetime, timedelta
import pytz

from dotenv import load_dotenv
load_dotenv()

from openai import AsyncOpenAI
from openai import AuthenticationError, APIError


# Инициализация клиента OpenAI
_openai_key = os.getenv("OPENAI_API_KEY")
# Проверяем, что ключ не пустой и имеет правильный формат (начинается с "sk-" или "sk-proj-")
_openai_key_clean = _openai_key.strip() if _openai_key else None
_is_valid_key = _openai_key_clean and (_openai_key_clean.startswith("sk-") or _openai_key_clean.startswith("sk-proj-"))
client = AsyncOpenAI(api_key=_openai_key_clean) if _is_valid_key else None

if _openai_key and not _is_valid_key:
    print(f"[AI Service] ВНИМАНИЕ: OPENAI_API_KEY установлен, но имеет неверный формат")
    print(f"[AI Service] Ключ должен начинаться с 'sk-' или 'sk-proj-'")
    print(f"[AI Service] Первые 20 символов ключа: {_openai_key_clean[:20] if _openai_key_clean else 'N/A'}...")
elif not _openai_key:
    print(f"[AI Service] OPENAI_API_KEY не установлен, AI функции будут недоступны")
elif _is_valid_key:
    print(f"[AI Service] OPENAI_API_KEY успешно загружен (длина: {len(_openai_key_clean)} символов, начинается с '{_openai_key_clean[:10]}...')")


async def transcribe_voice(file_path: str) -> Optional[str]:
    """
    Транскрибирует голосовое сообщение через Whisper API.
    
    Args:
        file_path: Путь к аудио файлу
    
    Returns:
        Транскрибированный текст или None
    """
    if not client:
        print("[AI Service] OPENAI_API_KEY не установлен")
        return None
    try:
        with open(file_path, "rb") as audio_file:
            # Используем whisper-1 с улучшенными параметрами для лучшего распознавания
            # language=None позволяет автоматически определить язык
            # prompt помогает модели лучше распознавать время и числа
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language=None,  # Автоопределение языка
                prompt="This is a task or event description. Numbers, times, and dates are important. Please transcribe them accurately, including times like 3 PM, 15:00, three o'clock, etc.",
                response_format="text",
                temperature=0.0  # Более детерминированный результат для лучшего распознавания чисел
            )
            # Если response_format="text", transcript уже строка
            return transcript if isinstance(transcript, str) else transcript.text
    except AuthenticationError as e:
        print(f"[AI Service] Ошибка аутентификации OpenAI (Invalid API key): {e}")
        return None
    except APIError as e:
        print(f"[AI Service] Ошибка API OpenAI: {e}")
        return None
    except Exception as e:
        print(f"[AI Service] Ошибка при транскрибации голоса: {e}")
        return None


async def extract_events_from_image(image_path: str, user_timezone: str = "UTC") -> Optional[Dict[str, str]]:
    """
    Извлекает события из изображения через GPT-4 Vision.
    
    Args:
        image_path: Путь к изображению
        user_timezone: Часовой пояс пользователя
    
    Returns:
        Словарь с событиями или None
    """
    if not client:
        print("[AI Service] OPENAI_API_KEY not set")
        return None
    try:
        # Читаем изображение и кодируем в base64
        import os as _os
        if not _os.path.exists(image_path):
            print(f"[AI Service] Image file not found: {image_path}")
            return None
        file_size = _os.path.getsize(image_path)
        if file_size == 0:
            print(f"[AI Service] Image file is empty: {image_path}")
            return None
        print(f"[AI Service] Processing image: {image_path} ({file_size} bytes)")

        with open(image_path, "rb") as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
        
        # Определяем формат изображения
        path_lower = image_path.lower()
        if path_lower.endswith('.png'):
            image_format = "image/png"
        elif path_lower.endswith('.gif'):
            image_format = "image/gif"
        elif path_lower.endswith('.webp'):
            image_format = "image/webp"
        elif path_lower.endswith(('.jpg', '.jpeg')):
            image_format = "image/jpeg"
        else:
            # По умолчанию предполагаем JPEG для неизвестных расширений
            image_format = "image/jpeg"
        
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """You are an expert in OCR and parsing university timetables and schedules from images.
You must handle different languages (English, Russian, Italian, Spanish, etc.).
Always respond with valid JSON.

Analyze the image and determine if it shows:
1. A SINGLE event/task - return single event format
2. A RECURRING WEEKLY SCHEDULE (timetable) - return schedule format

IMPORTANT FOR ITALIAN SCHEDULES:
- Look for patterns like "Ore 10:30", "Ore HH:MM", or "HH:MM - HH:MM"
- Days of the week in Italian: Lunedì, Martedì, Mercoledì, Giovedì, Venerdì, Sabato, Domenica
- Dates: "16 febbraio", "17/02", "febbraio 16"
- If you see a date like "Lunedì 16 febbraio", use that specific date
- If you see only "Lunedì" without a date, assume next Monday
- Time formats: "Ore 10:30", "10:30", "10.30", "10:30-11:30"

For SINGLE EVENT, return:
{
    "is_recurring_schedule": false,
    "summary": "event title",
    "start_time": "ISO 8601 format with timezone (e.g., 2026-02-16T10:30:00+00:00)",
    "end_time": "ISO 8601 format with timezone (e.g., 2026-02-16T11:30:00+00:00)",
    "description": "optional description",
    "location": "optional location - include all components (room, building, etc.) if present"
}

For RECURRING WEEKLY SCHEDULE (timetable with days of week), return:
{
    "is_recurring_schedule": true,
    "events": [
        {
            "day_of_week": "Wednesday",  // Always normalize to English full name: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday
            "start_time": "12:15",       // HH:MM 24h format (local time)
            "end_time": "13:45",         // HH:MM 24h format (local time)
            "summary": "Class/Event name",
            "location": "Aula 4A, San Giobbe"  // IMPORTANT: Include ALL location components (room, building, etc.) combined in one field
        },
        ...
    ]
}

CRITICAL RULES:
- If the image shows a weekly timetable/schedule with TWO OR MORE classes/events listed across different days, it's a recurring schedule. A single event shown on one day is NOT a recurring schedule.
- Always normalize day names to English (e.g., "Lunedì" -> "Monday", "Martedì" -> "Tuesday")
- Extract time in 24h format (e.g., "Ore 10:30" -> "10:30")
- If you see a specific date (e.g., "16 febbraio"), use it for single events
- For recurring schedules, ignore specific dates and use only day of week
- **CRITICAL for location**: When extracting location from schedule entries, include ALL location components (room number, building name, campus) in one field, separated by ", " if needed. Example: "Aula 4A, San Giobbe" not just "San Giobbe"""
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Extract events from this image. User timezone: {user_timezone}. For single events, format times as ISO 8601 with timezone offset (e.g., 2026-02-16T14:30:00+01:00). For schedules/timetables, extract as day+time in HH:MM format. If unsure about timezone, use UTC (Z) or the provided timezone."
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image_format};base64,{image_data}"
                            }
                        }
                    ]
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.3
        )
        
        content = response.choices[0].message.content.strip()
        parsed = json.loads(content)
        
        print(f"[AI Service] Image parsed response keys: {list(parsed.keys())}, is_recurring={parsed.get('is_recurring_schedule')}")

        # Проверяем, является ли это рекуррентным расписанием
        if parsed.get("is_recurring_schedule", False):
            # Валидация структуры расписания
            if "events" not in parsed or not isinstance(parsed["events"], list):
                print("[AI Service] Invalid schedule structure from image: missing events array")
                return None
            
            # Валидируем каждое событие (аналогично parse_with_ai)
            valid_events = []
            day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            
            for event in parsed["events"]:
                if not isinstance(event, dict):
                    continue
                
                if "day_of_week" not in event or "start_time" not in event:
                    continue
                
                # Нормализуем день недели (поддерживаем английский, итальянский и русский)
                raw_day = str(event["day_of_week"]).strip()
                # Сначала пытаемся привести к единообразному виду без учёта регистра
                day_norm = raw_day.lower()
                day_mapping = {
                    # Italian full
                    "lunedì": "Monday", "martedì": "Tuesday", "mercoledì": "Wednesday",
                    "giovedì": "Thursday", "venerdì": "Friday", "sabato": "Saturday", "domenica": "Sunday",
                    # Italian short
                    "lun": "Monday", "mar": "Tuesday", "mer": "Wednesday", "gio": "Thursday",
                    "ven": "Friday", "sab": "Saturday", "dom": "Sunday",
                    # English full
                    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
                    "thursday": "Thursday", "friday": "Friday", "saturday": "Saturday", "sunday": "Sunday",
                    # English short
                    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
                    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
                    # Russian full
                    "понедельник": "Monday", "вторник": "Tuesday", "среда": "Wednesday",
                    "четверг": "Thursday", "пятница": "Friday", "суббота": "Saturday", "воскресенье": "Sunday",
                    # Russian short
                    "пн": "Monday", "вт": "Tuesday", "ср": "Wednesday",
                    "чт": "Thursday", "пт": "Friday", "сб": "Saturday", "вс": "Sunday",
                }
                day = day_mapping.get(day_norm, raw_day.capitalize())
                
                if day not in day_names:
                    continue
                
                start_time = event.get("start_time", "").strip()
                end_time = event.get("end_time", "").strip()
                
                if not end_time and start_time:
                    try:
                        parts = start_time.split(":")
                        if len(parts) == 2:
                            hour = int(parts[0])
                            minute = int(parts[1])
                            end_hour = (hour + 1) % 24
                            end_time = f"{end_hour:02d}:{minute:02d}"
                    except Exception:
                        end_time = ""
                
                if not start_time or not end_time:
                    continue
                
                try:
                    start_parts = start_time.split(":")
                    end_parts = end_time.split(":")
                    if len(start_parts) != 2 or len(end_parts) != 2:
                        continue
                    int(start_parts[0])
                    int(start_parts[1])
                    int(end_parts[0])
                    int(end_parts[1])
                except Exception:
                    continue
                
                valid_event = {
                    "day_of_week": day,
                    "start_time": start_time,
                    "end_time": end_time,
                    "summary": event.get("summary", "Event").strip(),
                    "location": event.get("location", "").strip()
                }
                valid_events.append(valid_event)
            
            if valid_events:
                return {"is_recurring_schedule": True, "events": valid_events}
            else:
                print("[AI Service] No valid events found in schedule from image")
                return None
        
        # Одиночное событие - возвращаем как есть
        if isinstance(parsed, dict) and "summary" in parsed:
            parsed["is_recurring_schedule"] = False
            return parsed

        print(f"[AI Service] Image response has no 'summary' key. Full response: {content[:300]}")
        return None
    except AuthenticationError as e:
        print(f"[AI Service] OpenAI auth error processing image: {e}")
        return None
    except APIError as e:
        print(f"[AI Service] OpenAI API error processing image (model=gpt-4o): {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"[AI Service] JSON parse error from image response: {e}")
        print(f"[AI Service] Raw content: {content[:500] if 'content' in locals() else 'N/A'}")
        return None
    except Exception as e:
        import traceback
        print(f"[AI Service] Unexpected error processing image: {type(e).__name__}: {e}")
        print(traceback.format_exc())
        return None


async def parse_with_ai(text: str, user_timezone: str = "UTC", source_language: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    Парсит текст задачи с помощью OpenAI API.
    
    Args:
        text: Текст задачи от пользователя
        user_timezone: Часовой пояс пользователя (например, "Europe/Moscow")
        source_language: Язык исходного текста (для сохранения в summary/description)
    
    Returns:
        Словарь с ключами: is_task, summary, start_time, end_time, description, location
        Или None в случае ошибки
    """
    if not client:
        print("[AI Service] OPENAI_API_KEY не установлен")
        return None
    
    # Определяем текущее время в часовом поясе пользователя
    tz = pytz.timezone(user_timezone)
    now_local = datetime.now(tz)
    now_utc = datetime.now(pytz.utc)
    current_date = now_local.strftime('%Y-%m-%d')
    current_time = now_local.strftime('%H:%M:%S')
    utc_offset = now_local.strftime('%z')  # e.g. +0300
    # Format as +03:00
    if len(utc_offset) == 5:
        utc_offset_fmt = utc_offset[:3] + ':' + utc_offset[3:]
    else:
        utc_offset_fmt = utc_offset
    
    system_prompt = """You are an assistant for parsing tasks and events from text.
Your task is to extract information about the task and return STRICTLY valid JSON without additional characters.

FIRST: Analyze if the text represents a **single task** OR a **recurring weekly schedule** (timetable).
If it looks like a list of classes/events with Days of Week and Times (e.g., 'Mon 10:00 Math, Tue 12:00 History', 'Mercoledì 12:15 Aula 4A', weekly timetable), it is a recurring schedule.

JSON structure for SINGLE TASK:
{
    "is_recurring_schedule": false,
    "is_task": bool,
    "summary": "brief task title (keep original language if Russian, otherwise English)",
    "start_time": "ISO 8601 format (YYYY-MM-DDTHH:MM:SS+00:00 or YYYY-MM-DDTHH:MM:SSZ)",
    "end_time": "ISO 8601 format (YYYY-MM-DDTHH:MM:SS+00:00 or YYYY-MM-DDTHH:MM:SSZ)",
    "description": "detailed task description (can be empty, keep original language)",
    "location": "location if mentioned (can be empty string)",
    "duration_minutes": integer,              // total duration in minutes (end_time - start_time)
    "duration_was_inferred": bool            // true if user did NOT explicitly specify duration and you used a default guess
}

JSON structure for RECURRING WEEKLY SCHEDULE:
{
    "is_recurring_schedule": true,
    "events": [
        {
            "day_of_week": "Wednesday",  // Always normalize to English full day name: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday
            "start_time": "12:15",       // HH:MM 24h format (local time, not ISO)
            "end_time": "13:45",         // HH:MM 24h format (local time, not ISO)
            "summary": "Class name or event title",
            "location": "Aula 4A, San Giobbe"     // IMPORTANT: Include ALL location info (room number, building name, etc.) combined in single field. Separate parts with comma+space if needed. Can be empty string.
        },
        ...
    ]
}

CRITICAL RULES:
1. DETECT RECURRING SCHEDULES: If text contains events listed across MULTIPLE different days of the week (e.g., "Monday 10:00 Math, Wednesday 14:00 History", "Mercoledì 12:15 Aula 4A, Giovedì 12:15 Aula 4A", "statistics\nMercoledì 12:15 - 13:45\nGiovedì 12:15 - 13:45"), set "is_recurring_schedule": true and return the events array. A SINGLE mention of a day of week with a time is NOT a recurring schedule — it is a single event (e.g., "Wednesday at 14:00 dentist" → single event on Wednesday). Only use recurring schedule when you see TWO OR MORE distinct day+time entries. Each event must have day_of_week (normalized to English), start_time and end_time in HH:MM format.
1a. **CRITICAL for class schedules**: If the text STARTS with one or more lines that don't contain a day of week or time (these are the class/subject name lines), followed by schedule entries with days and times, extract the subject name from those initial lines and use it as the "summary" for ALL events in the schedule. Examples:
   - Input: "statistics\nMercoledì 12:15 - 13:45 Aula 4A" → all events get "summary": "statistics"
   - Input: "Math - Advanced\nMonday 10:00 - 11:30, Wednesday 14:00 - 15:30" → all events get "summary": "Math - Advanced"
   - Input: "Chemistry Lab\nLunedì 09:00 - 10:30, Mercoledì 09:00 - 10:30" → all events get "summary": "Chemistry Lab"
   **IMPORTANT**: Always extract and use the first non-schedule line(s) as the subject name for recurring schedules. This is the most common format for class timetables.
1b. **CRITICAL for location extraction**: When extracting location for each event, include ALL location components found (room/classroom number, building name, campus name, etc.). If text shows "Aula 4A San Giobbe", the location field must be "Aula 4A, San Giobbe" (combining room and building). Combine multiple location parts with ", " (comma+space). Never extract only the last location component - include everything.
2. For SINGLE TASKS: If the message does NOT look like a task (e.g., "Hello", "How are you", "Thanks", greetings, casual conversation, random words, questions without action, random characters like "000000", meaningless text), set "is_task": false and return minimal valid JSON.
3. If "is_task": false, you can set summary to empty string, but still provide valid ISO times (use today 09:00 as default).
4. If user did NOT specify time explicitly (e.g., "Buy milk", "Call John"), set the task to TODAY at 09:00 (morning slot). Do NOT use tomorrow unless the user explicitly says "tomorrow", "next week", or similar future words.
5. If user specified only date without time, use 09:00 as start time and 09:30 as end time.
6. **TIME WITHOUT DATE**: If user specified only a time (e.g., "Meeting at 15:00", "call at 21:30"), ALWAYS use TODAY at that local time. Only use TOMORROW if that exact time has ALREADY PASSED today (e.g., if current time is 22:00 and user says "21:30", then use tomorrow since 21:30 already passed).
6a. **DAY OF WEEK**: If user specified a day of week (e.g., "Wednesday 14:00", "wed 10:30", "friday 9am", "poop wed 10:30"), find the NEXT upcoming occurrence of that day:
    - If today is NOT that weekday → use the nearest upcoming day of that name this week or next
    - If today IS that weekday AND the time has NOT yet passed → use today
    - If today IS that weekday AND the time HAS already passed → use NEXT week's same weekday (7 days ahead)
    Example: today is Friday, user says "wed 10:30" → schedule for next Wednesday
    Example: today is Wednesday 09:00, user says "wed 10:30" → schedule for today (Wednesday) at 10:30
    Example: today is Wednesday 11:00, user says "wed 10:30" → 10:30 already passed → schedule for NEXT Wednesday
7. Only schedule for tomorrow/future if the computed time would be in the past relative to NOW.
8. For single tasks: All times must be in the USER'S LOCAL TIMEZONE with the correct numeric UTC offset (e.g., "2026-03-10T14:00:00+03:00"). DO NOT convert times to pure UTC yourself; keep the local offset.
9. Default duration is 30 minutes (end_time = start_time + 30 minutes) ONLY when the user did NOT explicitly specify duration. In that case set "duration_was_inferred": true. If the user clearly specifies duration (e.g., "for 2 hours", "1.5h", "for 45 minutes"), compute end_time accordingly and set "duration_was_inferred": false.
10. Always set "duration_minutes" = total duration in minutes (end_time - start_time), even if the user did not specify duration explicitly.
11. summary should be brief (up to 100 characters).
12. description can be empty string if no additional details.
13. location can be empty string if not mentioned.
14. If input text is in Russian, keep summary and description in Russian. Otherwise use English.
15. Be VERY strict: if the message is unclear, ambiguous, doesn't contain a clear action/task, or looks like random text/characters (e.g., "Cheche tv 000000"), set "is_task": false.
16. A valid task must contain at least one action verb (e.g., "buy", "call", "meet", "go", "do", "make", "send", "write", etc.) or a clear event description.
17. Random words, numbers, or character sequences without clear meaning are NOT tasks.

IMPORTANT: Return ONLY valid JSON, no markdown formatting, no backticks, no additional text."""

    user_prompt = f"""Current date (local): {current_date}
Current time (local): {current_time}
UTC offset: {utc_offset_fmt}
User timezone: {user_timezone}
Current UTC time: {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}

IMPORTANT: When the user says a time (e.g. "14:00"), treat it as LOCAL time in the user's timezone (UTC offset {utc_offset_fmt}). Your output start_time and end_time MUST also be in the SAME LOCAL TIMEZONE with the SAME offset (e.g. +03:00). Do NOT subtract the offset or convert to pure UTC yourself — just attach the correct local offset. The calling code will convert to UTC later.

Task: {text}

Return JSON with task information."""

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"}
        )
        
        content = response.choices[0].message.content.strip()
        
        # Убираем markdown форматирование если есть
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        
        # Парсим JSON
        parsed_data = json.loads(content)
        
        # Проверяем, является ли это рекуррентным расписанием
        if parsed_data.get("is_recurring_schedule", False):
            # Валидация структуры расписания
            if "events" not in parsed_data or not isinstance(parsed_data["events"], list):
                print("[AI Service] Invalid schedule structure: missing events array")
                return None
            
            # Валидируем каждое событие в расписании
            valid_events = []
            day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            
            for event in parsed_data["events"]:
                if not isinstance(event, dict):
                    continue
                
                # Проверяем обязательные поля
                if "day_of_week" not in event or "start_time" not in event:
                    continue
                
                # Нормализуем день недели (английский, итальянский, русский)
                raw_day = str(event["day_of_week"]).strip()
                day_norm = raw_day.lower()
                # Маппинг для разных языков
                day_mapping = {
                    # Italian full
                    "lunedì": "Monday", "martedì": "Tuesday", "mercoledì": "Wednesday",
                    "giovedì": "Thursday", "venerdì": "Friday", "sabato": "Saturday", "domenica": "Sunday",
                    # Italian short
                    "lun": "Monday", "mar": "Tuesday", "mer": "Wednesday", "gio": "Thursday",
                    "ven": "Friday", "sab": "Saturday", "dom": "Sunday",
                    # English full
                    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
                    "thursday": "Thursday", "friday": "Friday", "saturday": "Saturday", "sunday": "Sunday",
                    # English short
                    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
                    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
                    # Russian full
                    "понедельник": "Monday", "вторник": "Tuesday", "среда": "Wednesday",
                    "четверг": "Thursday", "пятница": "Friday", "суббота": "Saturday", "воскресенье": "Sunday",
                    # Russian short
                    "пн": "Monday", "вт": "Tuesday", "ср": "Wednesday",
                    "чт": "Thursday", "пт": "Friday", "сб": "Saturday", "вс": "Sunday",
                }
                day = day_mapping.get(day_norm, raw_day.capitalize())
                
                if day not in day_names:
                    continue
                
                # Проверяем формат времени
                start_time = event.get("start_time", "").strip()
                end_time = event.get("end_time", "").strip()
                
                # Если end_time отсутствует, вычисляем (по умолчанию +1 час)
                if not end_time and start_time:
                    try:
                        parts = start_time.split(":")
                        if len(parts) == 2:
                            hour = int(parts[0])
                            minute = int(parts[1])
                            end_hour = (hour + 1) % 24
                            end_time = f"{end_hour:02d}:{minute:02d}"
                    except Exception:
                        end_time = ""
                
                if not start_time or not end_time:
                    continue
                
                # Валидируем формат HH:MM
                try:
                    start_parts = start_time.split(":")
                    end_parts = end_time.split(":")
                    if len(start_parts) != 2 or len(end_parts) != 2:
                        continue
                    int(start_parts[0])  # Проверка что это число
                    int(start_parts[1])
                    int(end_parts[0])
                    int(end_parts[1])
                except Exception:
                    continue
                
                valid_event = {
                    "day_of_week": day,
                    "start_time": start_time,
                    "end_time": end_time,
                    "summary": event.get("summary", "Event").strip(),
                    "location": event.get("location", "").strip()
                }
                valid_events.append(valid_event)
            
            if valid_events:
                return {"is_recurring_schedule": True, "events": valid_events}
            else:
                print("[AI Service] No valid events found in schedule")
                return None
        
        # Валидация структуры для одиночной задачи
        required_keys = ["is_task", "summary", "start_time", "end_time", "description", "location"]
        for key in required_keys:
            if key not in parsed_data:
                # Устанавливаем значения по умолчанию для отсутствующих ключей
                if key == "is_task":
                    parsed_data[key] = True  # По умолчанию считаем, что это задача
                elif key == "location":
                    parsed_data[key] = ""
                else:
                    raise ValueError(f"Отсутствует обязательный ключ: {key}")
        
        # Устанавливаем is_recurring_schedule = false для одиночных задач
        parsed_data["is_recurring_schedule"] = False
        
        # Если это не задача, возвращаем сразу
        if not parsed_data.get("is_task", True):
            return parsed_data
        
        # Валидация и нормализация времени
        try:
            start_dt = datetime.fromisoformat(parsed_data["start_time"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(parsed_data["end_time"].replace("Z", "+00:00"))

            # Strip any offset returned by the AI and re-localize in user's timezone.
            # GPT models often return +00:00 (UTC) instead of the user's local offset,
            # but the date/time values themselves are correct in local terms.
            # Store in local timezone (with offset) so format_event_preview can display correctly.
            tz_obj = pytz.timezone(user_timezone)
            start_dt = tz_obj.localize(start_dt.replace(tzinfo=None))
            end_dt = tz_obj.localize(end_dt.replace(tzinfo=None))

            # Check if user mentioned a specific day of week.
            # If so, compute the correct date in Python — AI can return the wrong date
            # for day abbreviations (e.g. "Mon" → Sunday instead of Monday).
            day_to_weekday = {
                "monday": 0, "mon": 0, "понедельник": 0, "пн": 0,
                "tuesday": 1, "tue": 1, "вторник": 1, "вт": 1,
                "wednesday": 2, "wed": 2, "среда": 2, "среду": 2, "ср": 2,
                "thursday": 3, "thu": 3, "четверг": 3, "чт": 3,
                "friday": 4, "fri": 4, "пятница": 4, "пятницу": 4, "пт": 4,
                "saturday": 5, "sat": 5, "суббота": 5, "субботу": 5, "сб": 5,
                "sunday": 6, "sun": 6, "воскресенье": 6, "вс": 6,
            }
            user_words = re.split(r'\W+', text.lower())
            mentioned_weekday = None
            for kw, wd in day_to_weekday.items():
                if kw in user_words:
                    mentioned_weekday = wd
                    break

            if mentioned_weekday is not None:
                # User explicitly mentioned a weekday — override the AI's date with the correct one
                now_local = datetime.now(tz_obj)
                today_wd = now_local.weekday()  # 0=Monday, 6=Sunday
                start_local = start_dt.astimezone(tz_obj)
                days_ahead = (mentioned_weekday - today_wd) % 7
                if days_ahead == 0:
                    # Today is that weekday — check if the time has already passed
                    today_at_time = tz_obj.localize(datetime(
                        now_local.year, now_local.month, now_local.day,
                        start_local.hour, start_local.minute, 0
                    ))
                    if today_at_time <= now_local:
                        days_ahead = 7  # Use next week's occurrence
                duration = end_dt - start_dt
                target_date = now_local.date() + timedelta(days=days_ahead)
                start_dt = tz_obj.localize(datetime(
                    target_date.year, target_date.month, target_date.day,
                    start_local.hour, start_local.minute, start_local.second
                ))
                end_dt = start_dt + duration
            else:
                # No specific day mentioned — apply past/tomorrow correction logic
                now_utc = datetime.now(pytz.utc)
                if start_dt < now_utc:
                    # Time is in the past — move to tomorrow
                    start_dt = start_dt + timedelta(days=1)
                    end_dt = end_dt + timedelta(days=1)
                else:
                    # Time is in the future — check if AI unnecessarily pushed it to tomorrow
                    # when the same clock time today hasn't passed yet.
                    now_local = datetime.now(tz_obj)
                    start_local = start_dt.astimezone(tz_obj)
                    tomorrow_local = now_local.date() + timedelta(days=1)
                    if start_local.date() == tomorrow_local:
                        today_candidate = tz_obj.localize(
                            datetime(now_local.year, now_local.month, now_local.day,
                                     start_local.hour, start_local.minute, start_local.second)
                        )
                        if today_candidate > now_local:
                            user_text_lower = text.lower()
                            tomorrow_keywords = ["tomorrow", "завтра", "next day", "следующий день"]
                            user_said_tomorrow = any(kw in user_text_lower for kw in tomorrow_keywords)
                            if not user_said_tomorrow:
                                # Correct: use today instead of tomorrow
                                duration = end_dt - start_dt
                                start_dt = today_candidate
                                end_dt = start_dt + duration
            
            # Убеждаемся, что end_time >= start_time
            if end_dt < start_dt:
                end_dt = start_dt + timedelta(minutes=30)
            
            # Вычисляем длительность и нормализуем duration_* поля
            duration_td = end_dt - start_dt
            duration_minutes_actual = max(int(duration_td.total_seconds() // 60), 1)
            # Если модель вернула duration_minutes, уважаем его, но используем фактическую длительность как fallback
            try:
                model_duration = int(parsed_data.get("duration_minutes", duration_minutes_actual))
                if model_duration <= 0:
                    model_duration = duration_minutes_actual
            except (TypeError, ValueError):
                model_duration = duration_minutes_actual
            parsed_data["duration_minutes"] = model_duration

            # duration_was_inferred может отсутствовать. Если поле отсутствует,
            # безопаснее считать, что длительность была НЕ явно указана пользователем
            # и спросить её отдельно.
            if "duration_was_inferred" in parsed_data:
                parsed_data["duration_was_inferred"] = bool(parsed_data["duration_was_inferred"])
            else:
                parsed_data["duration_was_inferred"] = True
            
            # Сохраняем нормализованные времена в ISO формате (local timezone with offset)
            parsed_data["start_time"] = start_dt.isoformat()
            parsed_data["end_time"] = end_dt.isoformat()
            
        except (ValueError, AttributeError) as e:
            raise ValueError(f"Неверный формат времени: {e}")
        
        return parsed_data
        
    except AuthenticationError as e:
        print(f"[AI Service] Ошибка аутентификации OpenAI (Invalid API key) при парсинге текста: {e}")
        return None
    except APIError as e:
        print(f"[AI Service] Ошибка API OpenAI при парсинге текста: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"[AI Service] Ошибка парсинга JSON: {e}")
        print(f"[AI Service] Полученный контент: {content[:200]}")
        return None
    except Exception as e:
        print(f"[AI Service] Ошибка при запросе к OpenAI: {e}")
        return None


async def generate_morning_briefing_intro() -> str:
    """
    Генерирует только вступительное сообщение для утреннего брифинга через AI.
    
    Returns:
        Текст вступления (1-2 предложения)
    """
    if not client:
        # Fallback к простому формату если нет OpenAI ключа
        return "Good morning! 🌅 Have a productive day and stay hydrated!"
    
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Write a short (1-2 sentences), energetic morning greeting for the user. Wish them a productive day and remind them to stay hydrated. Tone: friendly, motivating. DO NOT list any tasks, just write the intro."
                },
                {
                    "role": "user",
                    "content": "Generate a short, energetic morning greeting (1-2 sentences). Wish the user a productive day and remind them to stay hydrated. Be friendly and motivating."
                }
            ],
            temperature=0.7,
            max_tokens=100
        )
        
        return response.choices[0].message.content.strip()
    except AuthenticationError as e:
        print(f"[AI Service] Ошибка аутентификации OpenAI (Invalid API key) при генерации брифинга: {e}")
        # Fallback к простому формату
        return "Good morning! 🌅 Have a productive day and stay hydrated!"
    except APIError as e:
        print(f"[AI Service] Ошибка API OpenAI при генерации брифинга: {e}")
        # Fallback к простому формату
        return "Good morning! 🌅 Have a productive day and stay hydrated!"
    except Exception as e:
        print(f"[AI Service] Ошибка при генерации брифинга: {e}")
        # Fallback к простому формату
        return "Good morning! 🌅 Have a productive day and stay hydrated!"


async def generate_text_response(input_text: str, model: str = "gpt-4o-mini") -> Optional[str]:
    """
    Генерирует текстовый ответ на основе входного текста через OpenAI API.
    Используется для различных задач генерации текста (истории, сводки и т.д.).
    
    Args:
        input_text: Входной текст для генерации ответа
        model: Модель OpenAI для использования (по умолчанию "gpt-4o-mini")
               Если указана "gpt-5-nano" и она недоступна, будет использована "gpt-4o-mini"
    
    Returns:
        Сгенерированный текст или None в случае ошибки
    """
    if not client:
        print("[AI Service] OPENAI_API_KEY не установлен")
        return None
    
    # Пытаемся использовать запрошенную модель, если она недоступна - fallback на gpt-4o-mini
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": input_text
                }
            ],
            temperature=0.7
        )
        
        return response.choices[0].message.content.strip()
    except AuthenticationError as e:
        print(f"[AI Service] Ошибка аутентификации OpenAI (Invalid API key) при генерации текста с моделью {model}: {e}")
        return None
    except APIError as e:
        print(f"[AI Service] Ошибка API OpenAI при генерации текста с моделью {model}: {e}")
        # Если ошибка связана с моделью и это не gpt-4o-mini, пробуем fallback
        if model != "gpt-4o-mini":
            try:
                print(f"[AI Service] Пробуем fallback на gpt-4o-mini")
                response = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "user",
                            "content": input_text
                        }
                    ],
                    temperature=0.7
                )
                return response.choices[0].message.content.strip()
            except Exception as e2:
                print(f"[AI Service] Ошибка при использовании fallback модели: {e2}")
        return None
    except Exception as e:
        print(f"[AI Service] Ошибка при генерации текста с моделью {model}: {e}")
        # Если ошибка связана с моделью и это не gpt-4o-mini, пробуем fallback
        if model != "gpt-4o-mini":
            try:
                print(f"[AI Service] Пробуем fallback на gpt-4o-mini")
                response = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "user",
                            "content": input_text
                        }
                    ],
                    temperature=0.7
                )
                return response.choices[0].message.content.strip()
            except Exception as e2:
                print(f"[AI Service] Ошибка при использовании fallback модели: {e2}")
        return None
