"""
SQLite-backed persistence for python-telegram-bot.
Persists user_data and chat_data across bot restarts.
"""
import sqlite3
import json
import os
from collections import defaultdict
from typing import Dict, Optional, Tuple, Any

from telegram.ext import BasePersistence
from telegram.ext._utils.types import ConversationDict

DB_PATH = os.getenv("DB_PATH", "tasks.db")


def _get_con():
    return sqlite3.connect(DB_PATH, timeout=10.0)


class SqlitePersistence(BasePersistence):
    """Stores user_data and chat_data in the existing SQLite database."""

    def __init__(self):
        from telegram.ext import PersistenceInput
        super().__init__(
            store_data=PersistenceInput(
                bot_data=False,
                chat_data=True,
                user_data=True,
                callback_data=False,
            )
        )
        self._ensure_table()

    def _ensure_table(self) -> None:
        con = _get_con()
        con.execute("""
            CREATE TABLE IF NOT EXISTS bot_persistence (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
        """)
        con.commit()
        con.close()

    def _load(self, key: str) -> Any:
        con = _get_con()
        cur = con.cursor()
        cur.execute("SELECT data FROM bot_persistence WHERE key=?", (key,))
        row = cur.fetchone()
        con.close()
        return json.loads(row[0]) if row else None

    def _save(self, key: str, data: Any) -> None:
        con = _get_con()
        con.execute(
            "INSERT INTO bot_persistence (key, data) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (key, json.dumps(data, default=str))
        )
        con.commit()
        con.close()

    def _delete(self, key: str) -> None:
        con = _get_con()
        con.execute("DELETE FROM bot_persistence WHERE key=?", (key,))
        con.commit()
        con.close()

    # ---- Abstract method implementations ----

    async def get_user_data(self) -> defaultdict:
        raw = self._load("user_data") or {}
        return defaultdict(dict, {int(k): v for k, v in raw.items()})

    async def get_chat_data(self) -> defaultdict:
        raw = self._load("chat_data") or {}
        return defaultdict(dict, {int(k): v for k, v in raw.items()})

    async def get_bot_data(self) -> Dict:
        return {}

    async def get_callback_data(self) -> None:
        return None

    async def get_conversations(self, name: str) -> ConversationDict:
        raw = self._load(f"conversations:{name}") or {}
        return {tuple(json.loads(k)): v for k, v in raw.items()}

    async def update_user_data(self, user_id: int, data: Dict) -> None:
        raw = self._load("user_data") or {}
        raw[str(user_id)] = data
        self._save("user_data", raw)

    async def update_chat_data(self, chat_id: int, data: Dict) -> None:
        raw = self._load("chat_data") or {}
        raw[str(chat_id)] = data
        self._save("chat_data", raw)

    async def update_bot_data(self, data: Dict) -> None:
        pass

    async def update_callback_data(self, data: Any) -> None:
        pass

    async def update_conversation(
        self, name: str, key: Tuple, new_state: Optional[object]
    ) -> None:
        raw = self._load(f"conversations:{name}") or {}
        str_key = json.dumps(list(key))
        if new_state is None:
            raw.pop(str_key, None)
        else:
            raw[str_key] = new_state
        self._save(f"conversations:{name}", raw)

    async def flush(self) -> None:
        pass  # writes are immediate

    async def drop_chat_data(self, chat_id: int) -> None:
        raw = self._load("chat_data") or {}
        raw.pop(str(chat_id), None)
        self._save("chat_data", raw)

    async def drop_user_data(self, user_id: int) -> None:
        raw = self._load("user_data") or {}
        raw.pop(str(user_id), None)
        self._save("user_data", raw)

    async def refresh_user_data(self, user_id: int, user_data: Dict) -> None:
        pass

    async def refresh_chat_data(self, chat_id: int, chat_data: Dict) -> None:
        pass

    async def refresh_bot_data(self, bot_data: Dict) -> None:
        pass
