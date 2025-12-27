"""
Database Service для работы с БД
"""
import os
import sqlite3
import json
from typing import Optional, Dict
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "tasks.db")


def get_con():
    return sqlite3.connect(DB_PATH)


def get_google_tokens(user_id: int) -> Optional[Dict]:
    """Получает сохраненные Google OAuth токены для пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        "SELECT token, refresh_token, token_uri, client_id, client_secret, scopes FROM google_oauth_tokens WHERE user_id=?",
        (user_id,)
    )
    row = cur.fetchone()
    con.close()
    if row:
        return {
            "token": row[0],
            "refresh_token": row[1],
            "token_uri": row[2],
            "client_id": row[3],
            "client_secret": row[4],
            "scopes": json.loads(row[5]) if row[5] else []
        }
    return None


def get_user_timezone(chat_id: int) -> Optional[str]:
    """Получает таймзону пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT tz FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def get_morning_time(chat_id: int) -> str:
    """Получает время утренней сводки в формате HH:MM"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT morning_time FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row and row[0] else "09:00"


def get_evening_time(chat_id: int) -> str:
    """Получает время вечерней сводки в формате HH:MM"""
    con = get_con()
    cur = con.cursor()
    cur.execute("SELECT evening_time FROM settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row and row[0] else "21:00"

