"""
Google Calendar Service для создания событий через OAuth2
"""
import os
import json
from typing import Optional, Dict, Tuple
from datetime import datetime
import pytz

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from services.db_service import save_google_tokens, delete_google_tokens


# Конфигурация OAuth2
SCOPES = ['https://www.googleapis.com/auth/calendar.events']
# REDIRECT_URI теперь формируется динамически на основе базового URL сервера


def get_authorization_url(user_id: int, redirect_uri: str) -> str:
    """
    Генерирует URL для авторизации пользователя в Google Calendar.
    
    Args:
        user_id: ID пользователя Telegram
        redirect_uri: URL для callback (например, https://your-domain.com/google/callback)
    
    Returns:
        URL для авторизации
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        # Возвращаем заглушку для тестирования
        return f"https://accounts.google.com/o/oauth2/auth?client_id=SETUP_REQUIRED&redirect_uri={redirect_uri}&scope=https://www.googleapis.com/auth/calendar.events&response_type=code&state={user_id}"
    
    # Создаем flow для OAuth2
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri]
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    # Генерируем URL авторизации
    authorization_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        state=str(user_id)  # Сохраняем user_id в state для безопасности
    )
    
    return authorization_url


def exchange_code_for_tokens(auth_code: str, redirect_uri: str) -> Optional[Dict[str, str]]:
    """
    Обменивает authorization code на access_token и refresh_token.
    
    Args:
        auth_code: Код авторизации от Google
        redirect_uri: URL для callback (должен совпадать с тем, что использовался при генерации URL)
    
    Returns:
        Словарь с токенами: {"access_token": "...", "refresh_token": "...", "token_uri": "...", "client_id": "...", "client_secret": "..."}
        Или None в случае ошибки
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        raise ValueError("GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET должны быть установлены")
    
    try:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri]
                }
            },
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
        
        # Обмениваем код на токены
        flow.fetch_token(code=auth_code)
        
        credentials = flow.credentials
        
        # Проверяем наличие refresh_token
        if not credentials.refresh_token:
            print(f"[Calendar Service] ВНИМАНИЕ: refresh_token отсутствует в ответе от Google!")
            print(f"[Calendar Service] Это может произойти, если пользователь уже авторизовал приложение ранее.")
        
        # ВАЖНО: credentials.client_secret может быть None, потому что Google не возвращает его в credentials
        # Используем client_secret из переменных окружения, который нам нужен для refresh токена
        # Возвращаем данные для сохранения
        tokens_dict = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri or "https://oauth2.googleapis.com/token",
            "client_id": credentials.client_id or client_id,
            "client_secret": client_secret,  # Берем из env, а не из credentials!
            "scopes": credentials.scopes
        }
        print(f"[Calendar Service] Токены успешно получены, refresh_token={'есть' if tokens_dict.get('refresh_token') else 'отсутствует'}, client_secret={'есть' if tokens_dict.get('client_secret') else 'отсутствует'}")
        return tokens_dict
    except Exception as e:
        print(f"[Calendar Service] Ошибка при обмене кода на токены: {e}")
        return None


def get_credentials_from_stored(user_id: int, stored_tokens: Dict) -> Optional[Credentials]:
    """
    Создает объект Credentials из сохраненных токенов.
    Если токен истек, обновляет его и сохраняет обратно в БД.
    
    Args:
        user_id: ID пользователя Telegram
        stored_tokens: Словарь с сохраненными токенами из БД
    
    Returns:
        Объект Credentials или None
    """
    try:
        # Проверяем наличие обязательных полей для refresh
        refresh_token = stored_tokens.get("refresh_token")
        client_secret = stored_tokens.get("client_secret")
        client_id = stored_tokens.get("client_id")
        token_uri = stored_tokens.get("token_uri", "https://oauth2.googleapis.com/token")
        
        # Если client_secret отсутствует в сохраненных токенах, берем из env (для старых записей)
        if not client_secret:
            client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
            print(f"[Calendar Service] client_secret не найден в сохраненных токенах для user_id={user_id}, используем из env")
        
        if not client_id:
            client_id = os.getenv("GOOGLE_CLIENT_ID")
            print(f"[Calendar Service] client_id не найден в сохраненных токенах для user_id={user_id}, используем из env")
        
        # Проверяем наличие обязательных полей
        if not refresh_token:
            print(f"[Calendar Service] ОШИБКА: refresh_token отсутствует для user_id={user_id}")
            return None
        if not client_secret:
            print(f"[Calendar Service] ОШИБКА: client_secret отсутствует для user_id={user_id}")
            return None
        if not client_id:
            print(f"[Calendar Service] ОШИБКА: client_id отсутствует для user_id={user_id}")
            return None
        
        creds = Credentials(
            token=stored_tokens.get("token"),
            refresh_token=refresh_token,
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=stored_tokens.get("scopes", SCOPES)
        )
        
        # Обновляем токен если истек
        if creds.expired and creds.refresh_token:
            print(f"[Calendar Service] Токен истек для user_id={user_id}, обновляем...")
            try:
                creds.refresh(Request())
                
                # Сохраняем обновленные токены обратно в БД
                # ВАЖНО: сохраняем client_secret из stored_tokens, а не из creds (который может быть None)
                updated_tokens = {
                    "token": creds.token,
                    "refresh_token": creds.refresh_token,  # refresh_token обычно не меняется
                    "token_uri": creds.token_uri or token_uri,
                    "client_id": creds.client_id or client_id,
                    "client_secret": client_secret,  # Берем из stored_tokens/env, а не из creds!
                    "scopes": list(creds.scopes) if creds.scopes else []
                }
                save_google_tokens(user_id, updated_tokens)
                print(f"[Calendar Service] Токены обновлены и сохранены для user_id={user_id}")
            except Exception as refresh_error:
                error_str = str(refresh_error)
                print(f"[Calendar Service] Ошибка при обновлении токена для user_id={user_id}: {refresh_error}")
                if "invalid_grant" in error_str or "Token has been expired or revoked" in error_str:
                    print(f"[Calendar Service] invalid_grant для user_id={user_id} — удаляем токены из БД")
                    delete_google_tokens(user_id)
                    # Re-raise as a specific sentinel so callers can notify the user.
                    # We raise here OUTSIDE the inner try/except so the outer handler won't swallow it.
                    raise ValueError(f"invalid_grant:{user_id}") from refresh_error
                # Any other refresh error — keep going; the token might still be usable.
        
        return creds
    except ValueError:
        # Let ValueError (e.g. invalid_grant sentinel) propagate to the caller unchanged.
        raise
    except Exception as e:
        print(f"[Calendar Service] Ошибка при создании credentials для user_id={user_id}: {e}")
        import traceback
        traceback.print_exc()
        return None


def create_event(credentials: Credentials, event_data: Dict[str, str]) -> Optional[str]:
    """
    Создает событие в Google Calendar.
    
    Args:
        credentials: Объект Credentials для доступа к API
        event_data: Словарь с данными события:
            - summary: название события
            - start_time: ISO формат времени начала
            - end_time: ISO формат времени окончания
            - description: описание события
    
    Returns:
        URL созданного события или None в случае ошибки
    """
    try:
        service = build('calendar', 'v3', credentials=credentials)
        
        # Парсим время
        start_dt = datetime.fromisoformat(event_data["start_time"].replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(event_data["end_time"].replace("Z", "+00:00"))
        
        # Формируем событие для Google Calendar API
        event = {
            'summary': event_data.get("summary", "Задача"),
            'description': event_data.get("description", ""),
            'start': {
                'dateTime': start_dt.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_dt.isoformat(),
                'timeZone': 'UTC',
            },
        }
        
        # Добавляем location, если указан
        location = event_data.get("location", "")
        if location:
            event['location'] = location
        
        # Создаем событие
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        
        # Возвращаем HTML ссылку на событие
        return created_event.get('htmlLink')
        
    except HttpError as e:
        print(f"[Calendar Service] Ошибка HTTP при создании события: {e}")
        return None
    except Exception as e:
        print(f"[Calendar Service] Ошибка при создании события: {e}")
        return None


def mark_event_done(credentials: Credentials, event_id: str, event_title: str) -> bool:
    """
    Отмечает событие как выполненное, добавляя эмодзи "✅ " в начало заголовка.
    
    Args:
        credentials: Объект Credentials для доступа к API
        event_id: ID события в Google Calendar
        event_title: Текущий заголовок события
    
    Returns:
        True если успешно, False в случае ошибки
    """
    try:
        service = build('calendar', 'v3', credentials=credentials)
        
        # Получаем текущее событие
        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        
        # Получаем текущий summary из API (актуальное значение)
        current_summary = event.get('summary', event_title)
        
        # Проверяем, не отмечено ли уже событие как выполненное
        if current_summary.startswith('✅ '):
            # Уже выполнено
            return True
        
        # Добавляем эмодзи в начало заголовка, используя актуальный current_summary
        # Убираем "✅ " если уже есть (на случай, если был передан с эмодзи)
        clean_summary = current_summary
        if clean_summary.startswith('✅ '):
            clean_summary = clean_summary[2:]
        new_summary = f"✅ {clean_summary}"
        
        # Обновляем событие
        event['summary'] = new_summary
        updated_event = service.events().update(
            calendarId='primary',
            eventId=event_id,
            body=event
        ).execute()
        
        return True
        
    except HttpError as e:
        print(f"[Calendar Service] Ошибка HTTP при отметке события как выполненного: {e}")
        return False
    except Exception as e:
        print(f"[Calendar Service] Ошибка при отметке события как выполненного: {e}")
        return False


def check_slot_availability(credentials: Credentials, start_dt: datetime, end_dt: datetime, exclude_event_id: Optional[str] = None) -> bool:
    """
    Проверяет, свободен ли временной слот в календаре.
    Alias для check_availability для обратной совместимости.
    
    Args:
        credentials: Объект Credentials для доступа к API
        start_dt: Время начала (datetime с timezone)
        end_dt: Время окончания (datetime с timezone)
        exclude_event_id: ID события, которое нужно исключить из проверки
    
    Returns:
        True если слот свободен, False если занят
    """
    return check_availability(credentials, start_dt, end_dt, exclude_event_id)


def check_availability(credentials: Credentials, start_dt: datetime, end_dt: datetime, exclude_event_id: Optional[str] = None) -> bool:
    """
    Checks whether a time slot is free in Google Calendar.
    Uses events().list to find events that overlap with [start_dt, end_dt].
    
    Returns True if the slot is free, False if busy.
    """
    try:
        service = build('calendar', 'v3', credentials=credentials)

        # Ensure both datetimes are timezone-aware
        if start_dt.tzinfo is None:
            start_dt = pytz.utc.localize(start_dt)
        if end_dt.tzinfo is None:
            end_dt = pytz.utc.localize(end_dt)

        # RFC3339 strings in UTC
        time_min = start_dt.astimezone(pytz.utc).isoformat()
        time_max = end_dt.astimezone(pytz.utc).isoformat()

        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])
        print(f"[Calendar Service] check_availability {time_min} → {time_max}: found {len(events)} event(s)")

        if not events:
            return True  # Slot is free

        # Filter out the event being rescheduled
        if exclude_event_id:
            events = [e for e in events if e.get('id') != exclude_event_id]
            print(f"[Calendar Service] After excluding {exclude_event_id}: {len(events)} event(s) remain")

        return len(events) == 0  # True = free

    except HttpError as e:
        print(f"[Calendar Service] HTTP error in check_availability: {e}")
        return False
    except Exception as e:
        print(f"[Calendar Service] Error in check_availability: {e}")
        return False


def find_next_free_slot(credentials: Credentials, start_dt: datetime, duration_minutes: int = 30, exclude_event_id: Optional[str] = None) -> Optional[datetime]:
    """
    Находит следующий свободный слот, начиная с указанного времени.
    Проверяет доступность в 30-минутных интервалах в течение следующих 12 часов.
    
    Args:
        credentials: Объект Credentials для доступа к API
        start_dt: Время начала поиска (datetime с timezone)
        duration_minutes: Длительность слота в минутах (по умолчанию 30)
        exclude_event_id: ID события, которое нужно исключить из проверки (например, при переносе)
    
    Returns:
        Первый найденный свободный datetime или None, если ничего не найдено
    """
    try:
        from datetime import timedelta

        if start_dt.tzinfo is None:
            start_dt = pytz.utc.localize(start_dt)

        end_search_dt = start_dt + timedelta(hours=12)

        time_min = start_dt.astimezone(pytz.utc).isoformat()
        time_max = end_search_dt.astimezone(pytz.utc).isoformat()

        service = build('calendar', 'v3', credentials=credentials)

        # Fetch all events in the search window
        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        raw_events = events_result.get('items', [])
        print(f"[Calendar Service] find_next_free_slot {time_min} → {time_max}: {len(raw_events)} event(s)")

        # Exclude the event being rescheduled
        if exclude_event_id:
            raw_events = [e for e in raw_events if e.get('id') != exclude_event_id]
            print(f"[Calendar Service] After excluding {exclude_event_id}: {len(raw_events)} event(s)")

        # Build busy_periods list
        # All-day events (date-only) block the entire day and are treated as busy.
        busy_periods = []
        tz = start_dt.tzinfo or pytz.utc
        for ev in raw_events:
            ev_start_str = ev['start'].get('dateTime')
            ev_end_str = ev['end'].get('dateTime')
            if not ev_start_str or not ev_end_str:
                # All-day event: 'date' field only (format YYYY-MM-DD).
                # Treat the whole day as busy by converting to midnight–midnight in user tz.
                date_str = ev['start'].get('date')
                date_end_str = ev['end'].get('date')
                if not date_str:
                    continue
                # Use localize() for pytz timezones to avoid broken UTC-offset datetimes.
                tz_for_localize = tz if hasattr(tz, 'localize') else pytz.utc
                day_start_naive = datetime.strptime(date_str, '%Y-%m-%d')
                day_start = tz_for_localize.localize(day_start_naive)
                if date_end_str:
                    day_end_naive = datetime.strptime(date_end_str, '%Y-%m-%d')
                    day_end = tz_for_localize.localize(day_end_naive)
                else:
                    day_end = day_start + timedelta(days=1)
                busy_periods.append((day_start, day_end))
                continue
            ev_start = datetime.fromisoformat(ev_start_str.replace('Z', '+00:00'))
            ev_end = datetime.fromisoformat(ev_end_str.replace('Z', '+00:00'))
            if ev_start.tzinfo is None:
                ev_start = pytz.utc.localize(ev_start)
            if ev_end.tzinfo is None:
                ev_end = pytz.utc.localize(ev_end)
            ev_start = ev_start.astimezone(tz)
            ev_end = ev_end.astimezone(tz)
            busy_periods.append((ev_start, ev_end))
        
        # Сортируем занятые периоды по времени начала
        busy_periods.sort(key=lambda x: x[0])
        
        duration = timedelta(minutes=duration_minutes)

        # Start from start_dt exactly (truncate seconds for cleanliness).
        current_check = start_dt.replace(second=0, microsecond=0)

        while current_check < end_search_dt:
            slot_end = current_check + duration

            # Check overlap with every busy period.
            is_free = True
            conflict_end = None

            for busy_start, busy_end in busy_periods:
                if current_check < busy_end and slot_end > busy_start:
                    is_free = False
                    if conflict_end is None or busy_end > conflict_end:
                        conflict_end = busy_end

            if is_free:
                return current_check

            # Jump to the end of the latest conflicting period and try again.
            if conflict_end and conflict_end > current_check:
                current_check = conflict_end.replace(second=0, microsecond=0)
            else:
                # Guard against infinite loop (shouldn't normally happen).
                current_check += timedelta(minutes=1)
        
        # Ничего не найдено
        return None
        
    except HttpError as e:
        print(f"[Calendar Service] Ошибка HTTP при поиске свободного слота: {e}")
        return None
    except Exception as e:
        print(f"[Calendar Service] Ошибка при поиске свободного слота: {e}")
        return None


def reschedule_event(credentials: Credentials, event_id: str, new_start_time: datetime, new_end_time: datetime) -> bool:
    """
    Переносит событие на новое время.
    
    Args:
        credentials: Объект Credentials для доступа к API
        event_id: ID события в Google Calendar
        new_start_time: Новое время начала (datetime с timezone)
        new_end_time: Новое время окончания (datetime с timezone)
    
    Returns:
        True если успешно, False в случае ошибки
    """
    try:
        service = build('calendar', 'v3', credentials=credentials)
        
        # Получаем текущее событие
        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        
        # Обновляем время
        event['start'] = {
            'dateTime': new_start_time.isoformat(),
            'timeZone': 'UTC',
        }
        event['end'] = {
            'dateTime': new_end_time.isoformat(),
            'timeZone': 'UTC',
        }
        
        # Обновляем событие
        updated_event = service.events().update(
            calendarId='primary',
            eventId=event_id,
            body=event
        ).execute()
        
        return True
        
    except HttpError as e:
        print(f"[Calendar Service] Ошибка HTTP при переносе события: {e}")
        return False
    except Exception as e:
        print(f"[Calendar Service] Ошибка при переносе события: {e}")
        return False


def cancel_event(credentials: Credentials, event_id: str) -> bool:
    """
    Отменяет событие, добавляя префикс "❌ " к заголовку.
    
    Args:
        credentials: Объект Credentials для доступа к API
        event_id: ID события в Google Calendar
    
    Returns:
        True если успешно, False в случае ошибки
    """
    try:
        service = build('calendar', 'v3', credentials=credentials)
        
        # Получаем текущее событие
        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        
        # Получаем текущий summary
        current_summary = event.get('summary', 'Task')
        
        # Проверяем, не отменено ли уже событие
        if current_summary.startswith('❌ '):
            return True  # Уже отменено
        
        # Добавляем префикс "❌ "
        new_summary = f"❌ {current_summary}"
        
        # Обновляем событие
        event['summary'] = new_summary
        updated_event = service.events().update(
            calendarId='primary',
            eventId=event_id,
            body=event
        ).execute()
        
        return True
        
    except HttpError as e:
        print(f"[Calendar Service] Ошибка HTTP при отмене события: {e}")
        return False
    except Exception as e:
        print(f"[Calendar Service] Ошибка при отмене события: {e}")
        return False

