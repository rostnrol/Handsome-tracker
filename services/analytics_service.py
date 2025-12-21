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
        amplitude_client = Amplitude(api_key)
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
    except Exception as e:
        print(f"[Analytics] Ошибка при отправке события в Amplitude: {e}")
        return False


# Инициализация при импорте модуля
init_amplitude()

