# Comprehensive Code Review - Bugs & Improvements

## CRITICAL BUGS (High Priority)

### 1. **Database Connection Leaks** ⚠️
**File:** `services/db_service.py`  
**Severity:** HIGH  
**Issue:** All database query functions create connections but don't use try-finally. If an exception occurs between `get_con()` and `con.close()`, connections leak.

```python
# CURRENT (BAD):
def get_google_tokens(user_id: int) -> Optional[Dict]:
    con = get_con()
    cur = con.cursor()
    cur.execute(...)  # ← If this raises, con.close() never executes
    con.close()
    return tokens
```

**Fix:** Use context manager wrapper or try-finally
```python
# BETTER:
def get_google_tokens(user_id: int) -> Optional[Dict]:
    con = get_con()
    try:
        cur = con.cursor()
        cur.execute(...)
        # ... process
        return tokens
    finally:
        con.close()
```

---

### 2. **Unvalidated Duration Input** ⚠️
**File:** `bot.py` line ~105 in `_parse_duration_to_minutes()`  
**Severity:** HIGH  
**Issue:** Function accepts any positive number without max bound. User could enter "999999h" creating invalid events.

```python
# CURRENT:
if minutes > 0:
    return minutes  # ← No upper bound check!
```

**Fix:** Add validation
```python
if 0 < minutes <= 1440:  # Max 24 hours
    return minutes
else:
    raise ValueError("Duration must be between 1 minute and 24 hours")
```

---

### 3. **Bare Exception Silently Swallows Errors** ⚠️
**File:** `bot.py` line ~1820 (handle_photo_message) & multiple places  
**Severity:** MEDIUM  
**Issue:** `except: pass` silently ignores failures, making debugging impossible

```python
# CURRENT (BAD):
try:
    os.unlink(tmp_path)
except:
    pass
```

**Fix:** Be specific about exceptions
```python
try:
    os.unlink(tmp_path)
except FileNotFoundError:
    pass  # File already deleted, that's OK
except OSError as e:
    print(f"[Bot] Warning: Could not delete temp file {tmp_path}: {e}")
```

---

### 4. **State Cleanup Inconsistency** ⚠️
**File:** `bot.py` throughout `handle_text_message`  
**Severity:** HIGH  
**Issue:** State variables are popped inconsistently. In some error paths, only some keys are deleted, leaving zombie state.

```python
# PROBLEM: Different error paths delete different keys
if not credentials:
    context.user_data.pop('waiting_for', None)
    context.user_data.pop('rescheduling_event_id', None)
    context.user_data.pop('reschedule_conflict_start', None)
    return
    
except Exception:
    context.user_data.pop('waiting_for', None)
    # ← Missing rescheduling_event_id and reschedule_conflict_start cleanup!
```

**Fix:** Create helper function
```python
def _clear_reschedule_state(context):
    """Clear all reschedule-related state variables"""
    context.user_data.pop('waiting_for', None)
    context.user_data.pop('rescheduling_event_id', None)
    context.user_data.pop('reschedule_conflict_start', None)
```

---

### 5. **Null Reference Risk in Schedule Deduplication** ⚠️
**File:** `bot.py` line ~1900 in `handle_schedule_import()`  
**Severity:** MEDIUM  
**Issue:** Tuple elements could be None, and `.strip()` will crash

```python
# CURRENT (BAD):
key = (
    (ev.get("day_of_week") or "").strip(),  # ← OK, handled
    (ev.get("start_time") or "").strip(),   # ← OK, handled
    (ev.get("end_time") or "").strip(),
    (ev.get("summary") or "").strip(),
    (ev.get("location") or "").strip(),
)
```

This is actually OK, but inconsistent with other parts of code that assume keys exist.

---

## MEDIUM-PRIORITY BUGS

### 6. **Timezone Offset Parsing Edge Case** ⚠️
**File:** `bot.py` line ~484 in `parse_utc_offset()`  
**Severity:** MEDIUM  
**Issue:** If user enters malformed UTC (e.g., "UTC" alone), split returns single element

```python
# CURRENT:
if "UTC" in text:
    parts = text.split()
    if len(parts) > 0:
        offset_str = parts[0]  # ← parts[0] is "UTC" not "UTC-5"!
        if offset_str in tz_map:  # ← Never matches
            return tz_map[offset_str]
```

**Fix:** Parse offset properly
```python
import re
match = re.match(r'UTC([+-])(\d{1,2})', text.upper())
if match:
    sign = match.group(1)
    hours = match.group(2)
    return tz_map.get(f"UTC{sign}{hours}")
```

---

### 7. **Conflicting State Keys in Preview Flow** ⚠️
**File:** `bot.py` throughout  
**Severity:** MEDIUM  
**Issue:** Both `pending_event_preview` (from callbacks) and `pending_event_data` (from process_task) exist, causing confusion

```python
# This is confusing - two keys for same thing:
context.user_data['pending_event_preview'] = event_data  # In show_event_preview()
context.user_data['pending_event_data'] = ai_parsed      # In process_task()
```

**Fix:** Use single consistent key
```python
# Use only one:
context.user_data['pending_event'] = event_data
```

---

### 8. **Schedule Import API Rate Limit Risk** ⚠️
**File:** `bot.py` line ~1980 in `handle_weeks_response()`  
**Severity:** MEDIUM  
**Issue:** Creating 52 weeks × 5 events = 260 API calls in loop without backoff

```python
# CURRENT: No rate limiting or backoff
for week_offset in range(num_weeks):
    for event_data in events:
        create_event(credentials, ...)  # ← Called 260+ times without delay
```

**Fix:** Add retry logic with exponential backoff

---

### 9. **Hard-coded Timezone Mappings** ⚠️
**File:** `bot.py` line ~447 in `parse_utc_offset()`  
**Severity:** MEDIUM  
**Issue:** 24 manually-typed timezone names. Maintenance nightmare & error-prone

```python
# CURRENT: 24 line hard-coded mappings...
tz_map = {
    "UTC-12": "Etc/GMT+12",
    "UTC-11": "Pacific/Midway",
    # ... 22 more lines ...
}
```

**Fix:** Generate dynamically
```python
def get_timezone_for_utc_offset(hours: int) -> str:
    """Get a timezone for UTC offset (simplified - one per offset)"""
    if hours < 0:
        return f"Etc/GMT+{abs(hours)}"  # Inverted for Etc/GMT
    elif hours > 0:
        return f"Etc/GMT-{hours}"
    else:
        return "UTC"
```

---

### 10. **No Input Length Validation** ⚠️
**File:** `bot.py` throughout (settings, event title, location, etc.)  
**Severity:** MEDIUM  
**Issue:** User text inputs not checked for max length, could overflow database fields or Google Calendar

```python
# CURRENT - no max length:
set_user_name(chat_id, text.strip())  # ← Could be 10,000 chars

# Google Calendar field limits:
# - title: 255 chars
# - location: ~255 chars
# - description: no strict limit but 32KB practical
```

**Fix:** Validate all user inputs
```python
def set_user_name(chat_id: int, name: str):
    name = name.strip()
    if not name:
        raise ValueError("Name cannot be empty")
    if len(name) > 100:
        raise ValueError("Name must be under 100 characters")
    # ... continue
```

---

### 11. **Missing SQLite Configuration for Concurrent Access** ⚠️
**File:** `bot.py` line ~218 in `init_db()`  
**Severity:** MEDIUM  
**Issue:** SQLite in default mode doesn't handle concurrent writes well. No WAL mode, no timeout

```python
# CURRENT:
con = sqlite3.connect(DB_PATH)
# ← Default journal_mode is DELETE, no timeout for locks
```

**Fix:** Enable WAL mode
```python
con = sqlite3.connect(DB_PATH, timeout=10.0)
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA synchronous=NORMAL")
con.close()
```

---

## MINOR BUGS & IMPROVEMENTS

### 12. **Duplicate Code for Preview Buttons** 
**Files:** Multiple (lines ~1730, ~1314, ~1330, etc.)  
**Severity:** LOW  
**Issue:** Same button creation logic repeated 4+ times

```python
# REPEATED in multiple places:
keyboard = [
    [
        InlineKeyboardButton("✅ Looks Good", callback_data="event_confirm"),
        InlineKeyboardButton("✏️ Edit", callback_data="event_edit")
    ]
]
```

**Fix:** Extract to function
```python
def build_event_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Looks Good", callback_data="event_confirm"),
        InlineKeyboardButton("✏️ Edit", callback_data="event_edit")
    ]])
```

---

### 13. **Inconsistent Error Messages**
**Files:** Throughout  
**Severity:** LOW  
**Issue:** Some errors show technical details, some don't. Makes UX unpredictable

```python
# INCONSISTENT:
"❌ An error occurred. Please try again."  # Vague
"Error: Invalid time format"  # Specific
f"❌ Couldn't extract events from the image. {error_detail}"  # Too detailed
```

**Fix:** Standardize error messages
```python
# Use consistent format and don't show technical details to user
ERRORS = {
    "auth_failed": "❌ Authorization failed. Please reconnect using /start",
    "invalid_input": "❌ I didn't understand that. Please try again.",
    "api_error": "❌ An error occurred. Please try again later.",
    "not_found": "❌ Information not found. Please try again.",
}
```

---

### 14. **No Exponential Backoff for OpenAI API**
**Files:** `services/ai_service.py`  
**Severity:** MEDIUM  
**Issue:** If OpenAI rate-limits, request fails immediately instead of retrying

```python
# CURRENT:
try:
    response = await client.chat.completions.create(...)
except APIError:
    return None  # ← Immediate failure, no retry
```

---

### 15. **Missing Type Hints**
**Files:** Throughout  
**Severity:** LOW  
**Issue:** Many functions missing return type hints, making IDE support poor

```python
# CURRENT (no hints):
def format_event_preview(event_data):
    # ...
    
# BETTER:
def format_event_preview(event_data: Dict[str, str]) -> str:
    # ...
```

---

### 16. **Potential Infinite Recursion in Timezone Finding**
**File:** `bot.py` line ~428 in `tz_from_location()`  
**Severity:** LOW  
**Issue:** Global `TF` singleton could cause issues if module reloads

```python
global TF
if TF is None and TimezoneFinder is not None:
    TF = TimezoneFinder(in_memory=True)  # ← Singleton could be stale
```

---

### 17. **No OAuth Token Refresh Validation**
**File:** `services/calendar_service.py` line ~217  
**Severity:** MEDIUM  
**Issue:** Token refresh catches generic Exception, might hide real errors

```python
# CURRENT:
except Exception as refresh_error:
    error_str = str(refresh_error)
    if "invalid_grant" in error_str:
        # ...
    # Other exceptions silently ignored
```

---

## CODE QUALITY IMPROVEMENTS

### A. **Create a Context Manager for Database Connections**
```python
# services/db_service.py
from contextlib import contextmanager

@contextmanager
def get_db():
    """Context manager for database connections"""
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    try:
        yield con
    finally:
        con.close()

# Usage:
def get_google_tokens(user_id: int) -> Optional[Dict]:
    with get_db() as con:
        cur = con.cursor()
        cur.execute(...)
        # ...
```

### B. **Create TypedDict for Data Structures**
```python
from typing import TypedDict

class EventData(TypedDict):
    summary: str
    start_time: str  # ISO 8601
    end_time: str    # ISO 8601
    location: str
    description: str
    is_task: bool
    is_recurring_schedule: bool
    duration_was_inferred: bool
```

### C. **Extract Common Validation Functions**
```python
# services/validation.py
def validate_event_data(event: EventData) -> None:
    """Validate event data before creation"""
    if not event.get("summary"):
        raise ValueError("Event must have a title")
    if len(event["summary"]) > 255:
        raise ValueError("Title must be under 255 characters")
    if event.get("location") and len(event["location"]) > 255:
        raise ValueError("Location must be under 255 characters")
```

### D. **Add Request Rate Limiting**
```python
from datetime import datetime, timedelta

class RateLimiter:
    def __init__(self, max_calls: int, time_window: int):
        self.max_calls = max_calls
        self.time_window = time_window
        self.calls = []
    
    async def acquire(self):
        now = datetime.now()
        self.calls = [c for c in self.calls if c > now - timedelta(seconds=self.time_window)]
        if len(self.calls) >= self.max_calls:
            raise RuntimeError("Rate limit exceeded")
        self.calls.append(now)
```

### E. **Implement Exponential Backoff for API Calls**
```python
async def call_with_backoff(func, max_retries=3):
    """Call function with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return await func()
        except (RateLimitError, TimeoutError) as e:
            if attempt == max_retries - 1:
                raise
            wait_time = 2 ** attempt + random.uniform(0, 1)
            await asyncio.sleep(wait_time)
```

### F. **Add Comprehensive Logging**
```python
import logging

logger = logging.getLogger(__name__)

# Usage:
logger.info(f"Processing task for user {chat_id}")
logger.warning(f"Timezone lookup failed for coordinates {lat}, {lon}")
logger.error(f"Failed to create event: {e}", exc_info=True)
```

---

## SUMMARY TABLE

| # | Bug | Severity | File | Impact |
|---|-----|----------|------|--------|
| 1 | DB connection leaks | HIGH | db_service.py | Memory leak, resource exhaustion |
| 2 | Unvalidated duration | HIGH | bot.py | Invalid events, data corruption |
| 3 | Bare except clauses | MEDIUM | bot.py | Silent failures, hard to debug |
| 4 | Inconsistent state cleanup | HIGH | bot.py | Zombie state, stuck user flows |
| 5 | Schedule dedup fragile | MEDIUM | bot.py | Potential crashes |
| 6 | UTC parsing edge case | MEDIUM | bot.py | Wrong timezone selection |
| 7 | Conflicting state keys | MEDIUM | bot.py | Confusion, bugs |
| 8 | API rate limit risk | MEDIUM | bot.py | Rate limiting issues |
| 9 | Hard-coded timezones | MEDIUM | bot.py | Maintenance burden |
| 10 | No input validation | MEDIUM | bot.py | Data overflow |
| 11 | SQLite not configured | MEDIUM | bot.py | Lock contention |
| 12 | Code duplication | LOW | bot.py | Maintenance |
| 13 | Inconsistent errors | LOW | bot.py | Poor UX |
| 14 | No API backoff | MEDIUM | ai_service.py | Rate limiting |
| 15 | Missing type hints | LOW | All | IDE support |
| 16 | Singleton race risk | LOW | bot.py | Edge case |
| 17 | Token refresh too permissive | MEDIUM | calendar_service.py | Silent failures |

---

## RECOMMENDED FIXES (Priority Order)

1. ✅ **Fix database connection leaks** (use try-finally)
2. ✅ **Fix duration validation** (add max bound)
3. ✅ **Replace bare excepts** (be specific)
4. ✅ **Consolidate state cleanup** (use helper function)
5. ✅ **Validate user input lengths** (add max length checks)
6. ✅ **Enable SQLite WAL mode** (better concurrency)
7. ✅ **Consolidate duplicate button code** (DRY principle)
8. ✅ **Add exponential backoff for APIs** (handle rate limits)
9. ✅ **Add type hints** (IDE support & documentation)
10. ✅ **Create context manager for DB** (cleaner code)

