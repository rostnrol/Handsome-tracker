"""
Database Service для работы с БД
"""
import os
import sqlite3
import json
from typing import Optional, Dict
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "tasks.db")


def get_con():
    return sqlite3.connect(DB_PATH, timeout=10.0)


@contextmanager
def get_db_connection():
    """Safe context manager for database connections - ensures close even on error"""
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        yield con
    finally:
        con.close()


def get_google_tokens(user_id: int) -> Optional[Dict]:
    """Получает сохраненные Google OAuth токены для пользователя"""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT token, refresh_token, token_uri, client_id, client_secret, scopes FROM google_oauth_tokens WHERE user_id=?",
            (user_id,)
        )
        row = cur.fetchone()
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
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute("DELETE FROM google_oauth_tokens WHERE user_id=?", (user_id,))
        con.commit()
    print(f"[DB Service] Токены удалены для user_id={user_id}")


def save_google_tokens(user_id: int, tokens: Dict) -> None:
    """Сохраняет Google OAuth токены для пользователя"""
    with get_db_connection() as con:
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
    print(f"[DB Service] Токены сохранены для user_id={user_id}, refresh_token={'есть' if tokens.get('refresh_token') else 'отсутствует'}, client_secret={'есть' if tokens.get('client_secret') else 'отсутствует'}, client_id={'есть' if tokens.get('client_id') else 'отсутствует'}")


def get_user_timezone(chat_id: int) -> Optional[str]:
    """Получает таймзону пользователя"""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute("SELECT tz FROM settings WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else None


def get_morning_time(chat_id: int) -> str:
    """Получает время утренней сводки в формате HH:MM"""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute("SELECT morning_time FROM settings WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else "09:00"


def get_evening_time(chat_id: int) -> str:
    """Получает время вечерней сводки в формате HH:ММ"""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute("SELECT evening_time FROM settings WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else "21:00"


def get_cleanup_info(chat_id: int):
    """Returns (welcome_msg_id, last_morning_msg_id) or None if no record."""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute("SELECT welcome_msg_id, last_morning_msg_id FROM chat_cleanup WHERE chat_id=?", (chat_id,))
        return cur.fetchone()


def store_welcome_msg(chat_id: int, message_id: int) -> None:
    """Store or update the welcome message ID for a chat."""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO chat_cleanup (chat_id, welcome_msg_id)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET welcome_msg_id=excluded.welcome_msg_id
        """, (chat_id, message_id))
        con.commit()


def add_uncertain_event(chat_id: int, event_name: str, reminder_utc: str) -> int:
    """Store a new uncertain event. Returns the new row id."""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO uncertain_events (chat_id, event_name, reminder_utc, reminded, created_utc) VALUES (?, ?, ?, 0, ?)",
            (chat_id, event_name, reminder_utc, datetime.now(timezone.utc).isoformat())
        )
        con.commit()
        return cur.lastrowid


def get_due_uncertain_events() -> list:
    """Return list of (id, chat_id, event_name) for reminders whose time has come and haven't been sent."""
    with get_db_connection() as con:
        cur = con.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "SELECT id, chat_id, event_name FROM uncertain_events WHERE reminded=0 AND reminder_utc <= ?",
            (now_iso,)
        )
        return cur.fetchall()


def mark_uncertain_reminded(event_id: int) -> None:
    """Mark an uncertain event reminder as sent."""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute("UPDATE uncertain_events SET reminded=1 WHERE id=?", (event_id,))
        con.commit()


def store_morning_msg(chat_id: int, message_id: int) -> None:
    """Store or update the last morning briefing message ID for a chat."""
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO chat_cleanup (chat_id, last_morning_msg_id)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET last_morning_msg_id=excluded.last_morning_msg_id
        """, (chat_id, message_id))
        con.commit()

