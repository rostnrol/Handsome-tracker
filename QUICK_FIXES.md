# Priority Bug Fixes - Quick Implementation Guide

This document provides quick, actionable fixes for the highest-severity bugs identified in the code review.

## 🔴 CRITICAL - Fix These First

### Fix #1: Database Connection Leaks (5 min)

**Problem:** `db_service.py` doesn't use try-finally for DB connections

**Quick Fix:**
```python
# services/db_service.py - Add this helper at the top

from contextlib import contextmanager
import sqlite3

@contextmanager
def get_db_connection():
    """Safe context manager for database connections"""
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        yield con
    finally:
        con.close()

# Then update ALL functions to use it:

# BEFORE:
def get_google_tokens(user_id: int) -> Optional[Dict]:
    con = get_con()
    cur = con.cursor()
    cur.execute(...)
    con.close()
    return tokens

# AFTER:
def get_google_tokens(user_id: int) -> Optional[Dict]:
    with get_db_connection() as con:
        cur = con.cursor()
        cur.execute(...)
        return tokens
```

**Files to update:** All functions in `db_service.py` (13 functions)  
**Time:** ~15 minutes

---

### Fix #2: Unvalidated Duration Input (3 min)

**Problem:** Users can enter unreasonable durations like "999999 hours"

**Quick Fix - Update `_parse_duration_to_minutes()` in bot.py around line 105:**

```python
def _parse_duration_to_minutes(text: str) -> int:
    """
    Парсит длительность задачи из текста и возвращает количество минут.
    """
    s = text.strip().lower()
    if not s:
        raise ValueError("Empty duration")

    MAX_DURATION_MINUTES = 1440  # 24 hours max - ADD THIS LINE
    
    # ... existing parsing code ...
    
    # Чистое число — интерпретируем как минуты
    if s.isdigit():
        minutes = int(s)
        if minutes > 0 and minutes <= MAX_DURATION_MINUTES:  # ADD BOUND CHECK
            return minutes
        elif minutes > MAX_DURATION_MINUTES:
            raise ValueError(f"Duration cannot exceed {MAX_DURATION_MINUTES} minutes (24 hours)")

    raise ValueError(f"Cannot parse duration from '{text}'")
```

**Files to update:** `bot.py` (1 location)  
**Time:** ~3 minutes

---

### Fix #3: Bare Exception Clauses (5 min)

**Problem:** `except: pass` silently swallows all errors

**Quick Fix - Replace all bare excepts:**

```python
# BEFORE (in bot.py line ~1820, handle_photo_message):
try:
    os.unlink(tmp_path)
except:
    pass

# AFTER:
try:
    os.unlink(tmp_path)
except FileNotFoundError:
    pass  # File already deleted - OK
except OSError as e:
    print(f"[Bot] Warning: Failed to delete temp file {tmp_path}: {e}")

# ALSO FIX in similar locations:
# - handle_voice_message (line ~1778)
# - format_event_preview exception handling
```

**Files to update:** `bot.py` (3 locations)  
**Time:** ~5 minutes

---

## 🟠 HIGH-PRIORITY - Fix Next

### Fix #4: Inconsistent State Cleanup (10 min)

**Problem:** State variables are popped inconsistently, leaving zombie state

**Quick Fix - Create cleanup helper function in bot.py:**

```python
# Add at line ~685 (before handle_text_message):

def _clear_reschedule_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all reschedule-related state variables"""
    for key in ['waiting_for', 'rescheduling_event_id', 'reschedule_conflict_start']:
        context.user_data.pop(key, None)

def _clear_event_preview_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all event preview state variables"""
    for key in ['pending_event_preview', 'pending_event_source', 'pending_event_data', 
                'waiting_for']:
        context.user_data.pop(key, None)

def _clear_schedule_import_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all schedule import state variables"""
    for key in ['state', 'pending_schedule', 'waiting_for', 'pending_schedule_preview', 
                'pending_event_source']:
        context.user_data.pop(key, None)
```

**Then replace all inconsistent cleanups with these functions:**

```python
# Example: line ~948, in reschedule_time handler:
# BEFORE:
context.user_data.pop('waiting_for', None)
context.user_data.pop('rescheduling_event_id', None)
context.user_data.pop('reschedule_conflict_start', None)

# AFTER:
_clear_reschedule_state(context)
```

**Files to update:** `bot.py` (20+ locations)  
**Time:** ~20 minutes

---

### Fix #5: No Input Length Validation (10 min)

**Problem:** User inputs (name, location, title) can be arbitrarily long

**Quick Fix - Create validation helper in bot.py:**

```python
# Add near line ~150:

def _validate_user_input(text: str, field_name: str, max_length: int = 255) -> str:
    """Validate and normalize user input"""
    text = text.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    if len(text) > max_length:
        raise ValueError(f"{field_name} must be under {max_length} characters (got {len(text)})")
    return text
```

**Then update input handlers:**

```python
# In handle_text_message, when processing names:
# BEFORE:
set_user_name(chat_id, text.strip())

# AFTER:
try:
    validated_name = _validate_user_input(text, "Name", max_length=100)
    set_user_name(chat_id, validated_name)
except ValueError as e:
    await update.message.reply_text(f"❌ {str(e)}")
    return
```

**Also apply to:**
- Event title editing (line ~1313+)
- Location editing (line ~1330+)  
- Event edit in callback_query

**Files to update:** `bot.py` (3-5 locations)  
**Time:** ~15 minutes

---

### Fix #6: SQLite Not Configured for Concurrency (3 min)

**Problem:** SQLite in default mode doesn't handle concurrent writes well

**Quick Fix - Update init_db() in bot.py at line ~218:**

```python
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")  # ADD THIS LINE
    con.execute("PRAGMA synchronous=NORMAL")  # ADD THIS LINE
    cur = con.cursor()
    # ... rest of init_db code ...
```

**Files to update:** `bot.py` (1 location)  
**Time:** ~3 minutes

---

### Fix #7: Consolidate Duplicate Button Code (10 min)

**Problem:** Same button patterns repeated 4+ times

**Quick Fix - Add helper functions in bot.py around line ~1600:**

```python
def build_event_preview_buttons() -> InlineKeyboardMarkup:
    """Build standard event preview buttons"""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Looks Good", callback_data="event_confirm"),
        InlineKeyboardButton("✏️ Edit", callback_data="event_edit")
    ]])

def build_schedule_buttons() -> InlineKeyboardMarkup:
    """Build standard schedule import buttons"""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Import Schedule", callback_data="schedule_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="schedule_cancel")
    ]])

def build_edit_menu_buttons() -> InlineKeyboardMarkup:
    """Build edit menu buttons"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Title", callback_data="edit_title")],
        [InlineKeyboardButton("📍 Location", callback_data="edit_location")],
        [InlineKeyboardButton("🕐 Time", callback_data="edit_time")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_edit")]
    ])
```

**Then replace all duplicates:**

```python
# BEFORE (multiple locations):
keyboard = [
    [
        InlineKeyboardButton("✅ Looks Good", callback_data="event_confirm"),
        InlineKeyboardButton("✏️ Edit", callback_data="event_edit")
    ]
]

# AFTER:
keyboard = build_event_preview_buttons()
```

**Files to update:** `bot.py` (8+ locations)  
**Time:** ~10 minutes

---

## 🟡 MEDIUM-PRIORITY - Fix Soon

### Fix #8: UTC Offset Parsing (5 min)

**Problem:** `parse_utc_offset()` fails on edge cases

**Files:** `bot.py` line ~484  
**Current Code:** Has hardcoded timezone mappings

**Better Approach:**
```python
def parse_utc_offset(text: str) -> Optional[str]:
    """Parses UTC offset and returns Python timezone name
    
    Examples: "UTC+1" -> "Europe/Paris", "UTC-5" -> "America/New_York"
    """
    text = text.strip().upper()
    
    # Predefined quality timezone names for each offset
    tz_map = {
        # Negative offsets (West)
        "UTC-12": "Etc/GMT+12",
        "UTC-11": "Pacific/Midway",
        # ... existing 25 mappings ...
    }
    
    if text in tz_map:
        return tz_map[text]
    return None  # Unrecognized format
```

This is already mostly correct - but consider adding validation:
```python
# At start of parse_utc_offset:
if not isinstance(text, str):
    return None
if len(text) > 10:  # Max reasonable length for "UTC-12 Back"
    return None
```

---

### Fix #9: Missing Type Hints (20 min - optional but recommended)

**Example:** `bot.py` line ~1640

```python
# BEFORE - no hints:
def format_event_preview(event_data):
    summary = event_data.get("summary", "Event")
    
# AFTER - with hints:
from typing import Dict

def format_event_preview(event_data: Dict[str, str]) -> str:
    """Format event data for preview display"""
    summary = event_data.get("summary", "Event")
    return preview_text
```

Priority functions to add types to:
1. `format_event_preview()`
2. `format_schedule_preview()`
3. `show_event_preview()`
4. `show_schedule_preview()`
5. `_parse_duration_to_minutes()`
6. All `async def` handlers

---

## 🟢 NICE-TO-HAVE IMPROVEMENTS

### Add Logging
```python
import logging
logger = logging.getLogger(__name__)

# Use instead of print():
logger.info(f"User {chat_id} created event")
logger.warning(f"Failed to update token for {user_id}")
logger.error(f"Database error: {e}", exc_info=True)
```

### Add Exponential Backoff for OpenAI
```python
# services/ai_service.py
import asyncio
import random

async def parse_with_ai_retry(text: str, user_timezone: str, max_retries: int = 3):
    """Call parse_with_ai with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return await parse_with_ai(text, user_timezone)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait_time = 2 ** attempt + random.uniform(0, 1)
            logger.warning(f"Retry {attempt + 1}/{max_retries} after {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
```

---

## IMPLEMENTATION CHECKLIST

- [ ] Fix #1: Database context manager (15 min)
- [ ] Fix #2: Duration validation (3 min)
- [ ] Fix #3: Remove bare excepts (5 min)
- [ ] Fix #4: State cleanup helpers (20 min)
- [ ] Fix #5: Input validation (15 min)
- [ ] Fix #6: SQLite WAL mode (3 min)
- [ ] Fix #7: Consolidate buttons (10 min)
- [ ] Fix #8: UTC offset validation (5 min)
- [ ] Fix #9: Add type hints (20 min optional)
- [ ] Add logging throughout (30 min optional)
- [ ] Test all fixes (30 min)

**Total Time:** ~90 minutes for critical fixes + medium-priority items

---

## TESTING RECOMMENDATIONS

After applying fixes, test these scenarios:

1. **Database:** Kill process mid-insert, verify connections are closed
2. **Duration:** Try "999999h", "0 min", "1440 min", "-5 min"  
3. **Input:** Paste 500-char string as name/location/title
4. **State:** Click Edit, then Settings menu, verify state clears
5. **Concurrency:** Open bot in 3 simultaneous terminals
6. **Schedule:** Import 52-week schedule with 10 events each
7. **Error recovery:** Send photo that fails OCR

