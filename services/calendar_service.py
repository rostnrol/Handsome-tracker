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
        
        # Возвращаем данные для сохранения
        return {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": credentials.scopes
        }
    except Exception as e:
        print(f"[Calendar Service] Ошибка при обмене кода на токены: {e}")
        return None


def get_credentials_from_stored(user_id: int, stored_tokens: Dict) -> Optional[Credentials]:
    """
    Создает объект Credentials из сохраненных токенов.
    
    Args:
        user_id: ID пользователя Telegram
        stored_tokens: Словарь с сохраненными токенами из БД
    
    Returns:
        Объект Credentials или None
    """
    try:
        creds = Credentials(
            token=stored_tokens.get("token"),
            refresh_token=stored_tokens.get("refresh_token"),
            token_uri=stored_tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=stored_tokens.get("client_id"),
            client_secret=stored_tokens.get("client_secret"),
            scopes=stored_tokens.get("scopes", SCOPES)
        )
        
        # Обновляем токен если истек
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        
        return creds
    except Exception as e:
        print(f"[Calendar Service] Ошибка при создании credentials: {e}")
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

