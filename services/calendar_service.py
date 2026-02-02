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

from services.db_service import save_google_tokens


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
                print(f"[Calendar Service] Ошибка при обновлении токена для user_id={user_id}: {refresh_error}")
                # Если не удалось обновить, все равно возвращаем credentials (может быть еще валидным)
        
        return creds
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
        
        # Проверяем, не отмечено ли уже событие как выполненное
        if event.get('summary', '').startswith('✅ '):
            # Уже выполнено
            return True
        
        # Добавляем эмодзи в начало заголовка
        new_summary = f"✅ {event_title}"
        
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

