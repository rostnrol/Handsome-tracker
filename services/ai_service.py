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
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
            return transcript.text
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
Analyze the image and extract all events, schedules, or tasks mentioned.
Return a JSON array of events, each with: summary, start_time (ISO format), end_time (ISO format), description.
If multiple events are found, return all of them. If no events found, return empty array."""
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Extract events from this image. Current timezone: {user_timezone}. Return JSON array of events."
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
        
        # Если это массив событий, берем первое
        if isinstance(parsed, list) and len(parsed) > 0:
            return parsed[0]
        elif isinstance(parsed, dict) and "events" in parsed:
            events = parsed["events"]
            return events[0] if isinstance(events, list) and len(events) > 0 else None
        elif isinstance(parsed, dict) and "summary" in parsed:
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
        Словарь с ключами: summary, start_time, end_time, description
        Или None в случае ошибки
    """
    if not client:
        print("[AI Service] OPENAI_API_KEY не установлен")
        return None
    
    # Определяем текущее время в часовом поясе пользователя
    tz = pytz.timezone(user_timezone)
    now_local = datetime.now(tz)
    
    system_prompt = """You are an assistant for parsing tasks and events from text.
Your task is to extract information about the task and return STRICTLY valid JSON without additional characters.

JSON structure:
{
    "summary": "brief task title (keep original language if Russian, otherwise English)",
    "start_time": "ISO 8601 format (YYYY-MM-DDTHH:MM:SS+00:00 or YYYY-MM-DDTHH:MM:SSZ)",
    "end_time": "ISO 8601 format (YYYY-MM-DDTHH:MM:SS+00:00 or YYYY-MM-DDTHH:MM:SSZ)",
    "description": "detailed task description (can be empty, keep original language)"
}

Rules:
1. If user didn't specify date, use TODAY.
2. If user didn't specify time, use NOW + 30 minutes as start_time, and start_time + 30 minutes as end_time.
3. If user specified only date without time, use 09:00 as start time and 09:30 as end time.
4. If user specified only time without date, use TODAY.
5. If time is in the past, move to tomorrow.
6. All times must be in UTC (convert from user timezone).
7. summary should be brief (up to 100 characters).
8. description can be empty string if no additional details.
9. If input text is in Russian, keep summary and description in Russian. Otherwise use English.

IMPORTANT: Return ONLY valid JSON, no markdown formatting, no backticks, no additional text."""

    user_prompt = f"""Current time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}
User timezone: {user_timezone}

Task: {text}

Return JSON with task information."""

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
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
        
        # Валидация структуры
        required_keys = ["summary", "start_time", "end_time", "description"]
        for key in required_keys:
            if key not in parsed_data:
                raise ValueError(f"Отсутствует обязательный ключ: {key}")
        
        # Валидация и нормализация времени
        try:
            start_dt = datetime.fromisoformat(parsed_data["start_time"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(parsed_data["end_time"].replace("Z", "+00:00"))
            
            # Если время в прошлом, переносим на завтра
            now_utc = datetime.now(pytz.utc)
            if start_dt.replace(tzinfo=pytz.utc) < now_utc:
                # Переносим на завтра
                start_dt = start_dt.replace(tzinfo=pytz.utc) + timedelta(days=1)
                end_dt = end_dt.replace(tzinfo=pytz.utc) + timedelta(days=1)
            
            # Убеждаемся, что end_time >= start_time
            if end_dt.replace(tzinfo=pytz.utc) < start_dt.replace(tzinfo=pytz.utc):
                end_dt = start_dt.replace(tzinfo=pytz.utc) + timedelta(minutes=30)
            
            # Сохраняем в ISO формате
            parsed_data["start_time"] = start_dt.replace(tzinfo=pytz.utc).isoformat()
            parsed_data["end_time"] = end_dt.replace(tzinfo=pytz.utc).isoformat()
            
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


async def generate_morning_briefing(events: list, user_timezone: str) -> str:
    """
    Генерирует утренний брифинг на основе событий дня через AI.
    
    Args:
        events: Список событий на день
        user_timezone: Часовой пояс пользователя
    
    Returns:
        Текст брифинга
    """
    if not events:
        return "Good morning! You have no events scheduled for today. Have a productive day! 🌅"
    
    events_text = "\n".join([
        f"- {event.get('summary', 'Event')} at {event.get('start_time', '')}"
        for event in events
    ])
    
    if not client:
        # Fallback к простому формату если нет OpenAI ключа
        return f"Good morning! You have {len(events)} event(s) scheduled for today:\n{events_text}"
    
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful morning briefing assistant. Generate a friendly, motivating morning briefing based on the user's calendar events for the day."
                },
                {
                    "role": "user",
                    "content": f"Generate a morning briefing for today. Events:\n{events_text}\n\nMake it friendly, concise (2-3 sentences), and motivating."
                }
            ],
            temperature=0.7,
            max_tokens=200
        )
        
        return response.choices[0].message.content.strip()
    except AuthenticationError as e:
        print(f"[AI Service] Ошибка аутентификации OpenAI (Invalid API key) при генерации брифинга: {e}")
        # Fallback к простому формату
        return f"Good morning! You have {len(events)} event(s) scheduled for today:\n{events_text}"
    except APIError as e:
        print(f"[AI Service] Ошибка API OpenAI при генерации брифинга: {e}")
        # Fallback к простому формату
        return f"Good morning! You have {len(events)} event(s) scheduled for today:\n{events_text}"
    except Exception as e:
        print(f"[AI Service] Ошибка при генерации брифинга: {e}")
        # Fallback к простому формату
        return f"Good morning! You have {len(events)} event(s) scheduled for today:\n{events_text}"


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
