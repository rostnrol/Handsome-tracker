"""
Analytics Service для отправки событий в Amplitude
"""
import os
from typing import Dict, Optional
from amplitude import Amplitude, BaseEvent, EventOptions, Identify, IdentifyEvent


# Инициализация клиента Amplitude
amplitude_client = None


def init_amplitude():
    """Инициализирует клиент Amplitude"""
    global amplitude_client
    api_key = os.getenv("AMPLITUDE_API_KEY")
    if api_key:
        try:
            amplitude_client = Amplitude(api_key)
            print(f"[Analytics] Amplitude клиент успешно инициализирован (ключ: {api_key[:10]}...)")
        except Exception as e:
            print(f"[Analytics] ОШИБКА при инициализации Amplitude клиента: {e}")
            print(f"[Analytics] Проверьте правильность AMPLITUDE_API_KEY в настройках Render")
            amplitude_client = None
    else:
        print("[Analytics] AMPLITUDE_API_KEY не установлен, аналитика отключена")


def track_event(user_id: int, event_name: str, event_properties: Optional[Dict] = None) -> bool:
    """
    Отправляет событие в Amplitude.
    
    Args:
        user_id: ID пользователя Telegram (будет преобразован в строку)
        event_name: Название события
        event_properties: Дополнительные свойства события
    
    Returns:
        True если событие отправлено успешно, False в противном случае
    """
    global amplitude_client
    
    if amplitude_client is None:
        init_amplitude()
    
    if amplitude_client is None:
        return False
    
    try:
        # Amplitude требует user_id в виде строки
        user_id_str = str(user_id)
        
        # Создаем событие
        event = BaseEvent(
            event_type=event_name,
            user_id=user_id_str,
            event_properties=event_properties or {}
        )
        
        # Отправляем событие
        amplitude_client.track(event)
        
        return True
    except ValueError as e:
        # Ошибка валидации (например, Invalid API Key)
        error_msg = str(e)
        if "Invalid" in error_msg or "API" in error_msg or "key" in error_msg.lower():
            print(f"[Analytics] ОШИБКА: Неверный AMPLITUDE_API_KEY. Проверьте ключ в настройках Render.")
        else:
            print(f"[Analytics] Ошибка валидации при отправке события в Amplitude: {e}")
        return False
    except Exception as e:
        error_msg = str(e)
        if "Invalid" in error_msg or "API" in error_msg or "key" in error_msg.lower():
            print(f"[Analytics] ОШИБКА: Проблема с AMPLITUDE_API_KEY: {e}")
        else:
            print(f"[Analytics] Ошибка при отправке события в Amplitude: {e}")
        return False


# Инициализация при импорте модуля
init_amplitude()

