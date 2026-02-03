"""
AI Service для обработки текста, голоса и фото с использованием OpenAI API
"""
import os
import json
import base64
from typing import Dict, Optional
from datetime import datetime, timedelta
import pytz

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
        print("[AI Service] OPENAI_API_KEY не установлен")
        return None
    try:
        # Читаем изображение и кодируем в base64
        with open(image_path, "rb") as image_file:
            image_data = base64.b64encode(image_file.read()).decode('utf-8')
        
        # Определяем формат изображения
        if image_path.lower().endswith('.png'):
            image_format = "image/png"
        else:
            image_format = "image/jpeg"
        
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """You are an assistant that extracts calendar events from images.
Analyze the image and determine if it shows:
1. A SINGLE event/task - return single event format
2. A RECURRING WEEKLY SCHEDULE (timetable) - return schedule format

For SINGLE EVENT, return:
{
    "is_recurring_schedule": false,
    "summary": "event title",
    "start_time": "ISO 8601 format",
    "end_time": "ISO 8601 format",
    "description": "optional description",
    "location": "optional location"
}

For RECURRING WEEKLY SCHEDULE (timetable with days of week), return:
{
    "is_recurring_schedule": true,
    "events": [
        {
            "day_of_week": "Wednesday",  // Always English full name: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday
            "start_time": "12:15",       // HH:MM 24h format (local time)
            "end_time": "13:45",         // HH:MM 24h format (local time)
            "summary": "Class/Event name",
            "location": "optional location"
        },
        ...
    ]
}

If the image shows a weekly timetable with multiple classes on different days, it's a recurring schedule."""
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Extract events from this image. Current timezone: {user_timezone}. If this is a weekly schedule/timetable, return is_recurring_schedule: true with events array. Otherwise return single event format."
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
                
                # Нормализуем день недели
                day = event["day_of_week"].strip().capitalize()
                day_mapping = {
                    "Lunedì": "Monday", "Martedì": "Tuesday", "Mercoledì": "Wednesday",
                    "Giovedì": "Thursday", "Venerdì": "Friday", "Sabato": "Saturday", "Domenica": "Sunday",
                    "Lun": "Monday", "Mar": "Tuesday", "Mer": "Wednesday", "Gio": "Thursday",
                    "Ven": "Friday", "Sab": "Saturday", "Dom": "Sunday",
                    "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
                    "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"
                }
                day = day_mapping.get(day, day)
                
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
                    except:
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
                except:
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
        
        return None
    except AuthenticationError as e:
        print(f"[AI Service] Ошибка аутентификации OpenAI (Invalid API key) при обработке изображения: {e}")
        return None
    except APIError as e:
        print(f"[AI Service] Ошибка API OpenAI при обработке изображения: {e}")
        return None
    except Exception as e:
        print(f"[AI Service] Ошибка при обработке изображения: {e}")
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
    current_date = now_local.strftime('%Y-%m-%d')
    current_time = now_local.strftime('%H:%M:%S')
    
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
    "location": "location if mentioned (can be empty string)"
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
            "location": "San Giobbe"     // Optional, can be empty string
        },
        ...
    ]
}

CRITICAL RULES:
1. DETECT RECURRING SCHEDULES: If text contains multiple events with days of week (e.g., "Monday 10:00 Math, Wednesday 14:00 History", "Mercoledì 12:15 Aula 4A, Giovedì 12:15 Aula 4A"), set "is_recurring_schedule": true and return the events array. Each event must have day_of_week (normalized to English), start_time and end_time in HH:MM format.
2. For SINGLE TASKS: If the message does NOT look like a task (e.g., "Hello", "How are you", "Thanks", greetings, casual conversation, random words, questions without action, random characters like "000000", meaningless text), set "is_task": false and return minimal valid JSON.
3. If "is_task": false, you can set summary to empty string, but still provide valid ISO times (use tomorrow 09:00 as default).
4. If user did NOT specify time explicitly (e.g., "Buy milk", "Call John"), set the task to TOMORROW at 09:00 (default morning slot).
5. If user specified only date without time, use 09:00 as start time and 09:30 as end time.
6. If user specified only time without date (e.g., "Meeting at 15:00"), use TODAY if that time has NOT passed yet, otherwise use TOMORROW.
7. If time is in the past, move to tomorrow.
8. For single tasks: All times must be in UTC (convert from user timezone).
9. Default duration is 30 minutes (end_time = start_time + 30 minutes).
10. summary should be brief (up to 100 characters).
11. description can be empty string if no additional details.
12. location can be empty string if not mentioned.
13. If input text is in Russian, keep summary and description in Russian. Otherwise use English.
14. Be VERY strict: if the message is unclear, ambiguous, doesn't contain a clear action/task, or looks like random text/characters (e.g., "Cheche tv 000000"), set "is_task": false.
15. A valid task must contain at least one action verb (e.g., "buy", "call", "meet", "go", "do", "make", "send", "write", etc.) or a clear event description.
16. Random words, numbers, or character sequences without clear meaning are NOT tasks.

IMPORTANT: Return ONLY valid JSON, no markdown formatting, no backticks, no additional text."""

    user_prompt = f"""Current date: {current_date}
Current time: {current_time}
User timezone: {user_timezone}

Task: {text}

Return JSON with task information."""

    try:
        # Пытаемся использовать gpt-5-mini, fallback на gpt-4o-mini
        model = "gpt-5-mini"
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
        except Exception as e:
            print(f"[AI Service] Модель {model} недоступна, используем gpt-4o-mini: {e}")
            model = "gpt-4o-mini"
            # gpt-4o-mini поддерживает только temperature=1 (по умолчанию)
            response = await client.chat.completions.create(
                model=model,
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
                
                # Нормализуем день недели
                day = event["day_of_week"].strip().capitalize()
                # Маппинг для разных языков
                day_mapping = {
                    "Lunedì": "Monday", "Martedì": "Tuesday", "Mercoledì": "Wednesday",
                    "Giovedì": "Thursday", "Venerdì": "Friday", "Sabato": "Saturday", "Domenica": "Sunday",
                    "Lun": "Monday", "Mar": "Tuesday", "Mer": "Wednesday", "Gio": "Thursday",
                    "Ven": "Friday", "Sab": "Saturday", "Dom": "Sunday",
                    "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
                    "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"
                }
                day = day_mapping.get(day, day)
                
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
                    except:
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
                except:
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
            
            # Нормализуем timezone
            if start_dt.tzinfo is None:
                start_dt = pytz.utc.localize(start_dt)
            else:
                start_dt = start_dt.astimezone(pytz.utc)
            
            if end_dt.tzinfo is None:
                end_dt = pytz.utc.localize(end_dt)
            else:
                end_dt = end_dt.astimezone(pytz.utc)
            
            # Если время в прошлом, переносим на завтра
            now_utc = datetime.now(pytz.utc)
            if start_dt < now_utc:
                # Переносим на завтра
                start_dt = start_dt + timedelta(days=1)
                end_dt = end_dt + timedelta(days=1)
            
            # Убеждаемся, что end_time >= start_time
            if end_dt < start_dt:
                end_dt = start_dt + timedelta(minutes=30)
            
            # Сохраняем в ISO формате
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
