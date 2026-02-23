"""
Database Service для работы с БД
"""
import os
import sqlite3
import json
from typing import Optional, Dict
from datetime import datetime, timezone

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
        tokens = {
            "token": row[0],
            "refresh_token": row[1],
            "token_uri": row[2],
            "client_id": row[3],
            "client_secret": row[4],
            "scopes": json.loads(row[5]) if row[5] else []
        }
        print(f"[DB Service] Получены токены для user_id={user_id}, refresh_token={'есть' if tokens.get('refresh_token') else 'отсутствует'}, client_secret={'есть' if tokens.get('client_secret') else 'отсутствует'}, client_id={'есть' if tokens.get('client_id') else 'отсутствует'}")
        return tokens
    print(f"[DB Service] Токены для user_id={user_id} не найдены в БД")
    return None


def delete_google_tokens(user_id: int) -> None:
    """Удаляет Google OAuth токены пользователя из БД (например, при invalid_grant)"""
    con = get_con()
    cur = con.cursor()
    cur.execute("DELETE FROM google_oauth_tokens WHERE user_id=?", (user_id,))
    con.commit()
    con.close()
    print(f"[DB Service] Токены удалены для user_id={user_id}")


def save_google_tokens(user_id: int, tokens: Dict) -> None:
    """Сохраняет Google OAuth токены для пользователя"""
    con = get_con()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO google_oauth_tokens 
        (user_id, token, refresh_token, token_uri, client_id, client_secret, scopes, updated_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            token=excluded.token,
            refresh_token=excluded.refresh_token,
            token_uri=excluded.token_uri,
            client_id=excluded.client_id,
            client_secret=excluded.client_secret,
            scopes=excluded.scopes,
            updated_utc=excluded.updated_utc
        """,
        (
            user_id,
            tokens.get("token"),
            tokens.get("refresh_token"),
            tokens.get("token_uri"),
            tokens.get("client_id"),
            tokens.get("client_secret"),
            json.dumps(tokens.get("scopes", [])),
            datetime.now(timezone.utc).isoformat()
        ),
    )
    con.commit()
    con.close()
    print(f"[DB Service] Токены сохранены для user_id={user_id}, refresh_token={'есть' if tokens.get('refresh_token') else 'отсутствует'}, client_secret={'есть' if tokens.get('client_secret') else 'отсутствует'}, client_id={'есть' if tokens.get('client_id') else 'отсутствует'}")


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

