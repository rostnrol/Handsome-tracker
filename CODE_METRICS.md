# Code Quality Metrics & Testing Guide

## Code Quality Status

```
OVERALL SCORE: 6.5/10 (Needs Improvement)

Architecture:    7/10  ✓ Good async design, clear separation of services
Database:        4/10  ✗ Connection leaks, no concurrency config
Error Handling:  5/10  ✗ Bare excepts, inconsistent cleanup
Input Validation: 3/10  ✗ No length checks, weak bounds validation
Type Safety:     4/10  ✗ Missing type hints on 50+ functions
State Management: 5/10  ✗ Inconsistent state keys and cleanup
API Integration: 6/10  ~ Good OAuth flow, but no backoff/retry
Testing:         3/10  ✗ Only test_strict_parse.py exists (1 test file)
Documentation:   7/10  ✓ Good comments in system prompts
Logging:         5/10  ~ Mix of print() and proper logging
```

---

## Risk Matrix

```
                    LIKELIHOOD
                Low    Medium    High
         H
I        I    ▓▓▓▓▓▓   #17
M        G    
P        H    #14      #4,8     #1,2
A              #9       #7       #3
C        M    #13,16   #5,6     
T        L    #12,15   #10,11
```

**Color Legend:**
- ▓▓▓▓▓ = Critical (#1,2,3 - Must fix)
- #### = High (#4,8,14,17 - Should fix)
- Regular = Medium and Low

---

## Files Deep-Dived

### 🔴 bot.py (3,800 lines)
| Issue | Lines | Severity | Impact |
|-------|-------|----------|--------|
| DB connection leak cascade | ~700+ | HIGH | Memory exhaustion |
| Bare excepts | #1820, #1778 | MEDIUM | Debug nightmare |
| State inconsistency | ~50 locations | HIGH | Stuck user flows |
| No input validation | #1120, #1313, #1330 | MEDIUM | Data overflow |
| Duplicate button code | #1730, #1314, #1330, #2860+ | LOW | Maintenance |
| Missing type hints | 50+ functions | LOW | IDE support |

**Overall:** 6/10 - Large file with good logic but poor error handling

---

### 🟠 services/db_service.py (105 lines)
| Issue | Lines | Severity | Impact |
|-------|-------|----------|--------|
| Connection leak in all 13 functions | all | HIGH | Resource exhaustion |
| No try-finally | all | HIGH | Silent failures |
| Missing WAL config | N/A | MEDIUM | Write contention |

**Overall:** 3/10 - Critical database safety issues

---

### 🟡 services/ai_service.py (730 lines)
| Issue | Lines | Severity | Impact |
|-------|-------|----------|--------|
| No exponential backoff | ~500+ | MEDIUM | Rate limiting fails |
| Generic error handling | ~600+ | MEDIUM | Silent failures |
| Unclear JSON parsing errors | ~290 | MEDIUM | Hard to debug |

**Overall:** 6.5/10 - Logic is good, but error handling needs work

---

### 🟢 services/calendar_service.py (650 lines)
| Issue | Lines | Severity | Impact |
|-------|-------|----------|--------|
| Permissive error recovery | ~217 | MEDIUM | Token refresh issues |
| Limited logging | throughout | LOW | Debugging hard |

**Overall:** 7/10 - Solid implementation

---

### 🟢 services/scheduler_service.py (500 lines)
**Overall:** 7.5/10 - Well-structured, good error handling

---

## Test Coverage

```
Current Tests:           1 file (test_strict_parse.py)
Test Functions:          12 tests
Coverage:                ~5% of codebase
Missing:                 Database, API integration, state management, 
                         error paths, edge cases

Recommendations:
- Add pytest fixtures for bot context
- Mock OpenAI/Google Calendar APIs
- Test all error paths
- Test concurrent state access
- Test schedule import with large datasets
```

---

## Testing Checklist

Use this to verify all fixes work:

### Database Safety Tests
```
✓ Create 100 events rapidly (stress test)
✓ Kill process during event creation
✓ Verify .db-wal files appear (WAL mode)
✓ Verify no connection leaks with `lsof` (Linux)
✓ Test with 10 concurrent Telegram users
```

### Duration Validation Tests
```
✓ Enter valid: "30", "1h", "1:30", "45min"
✓ Reject: "0", "-5", "1440", "999999"
✓ Verify error message is helpful
✓ Verify bot doesn't crash
```

### Input Validation Tests
```
✓ Name: 100 chars (pass), 101 chars (fail)
✓ Location: 255 chars (pass), 256 chars (fail)
✓ Title: 255 chars (pass), 256 chars (fail)
✓ Unicode: "Москва" (pass), emojis (handle gracefully)
```

### State Cleanup Tests
```
✓ Click Edit, then Settings, verify state clears
✓ Start reschedule, then click different menu
✓ Multiple rapid edits work without corruption
✓ Error during edit leaves clean state
✓ Cancel operations clear all state
```

### Error Recovery Tests
```
✓ OpenAI API returns 429 (rate limit)
✓ Google Calendar API returns 403 (permission)
✓ Network timeout during image upload
✓ Malformed timezone input handled gracefully
✓ Photo extraction fails, user can retry
```

### Concurrency Tests
```
✓ Two users edit same schedule simultaneously
✓ One user creates event while another imports schedule
✓ Rapid callback clicks don't corrupt state
✓ Database transactions don't race
```

### Performance Tests
```
✓ Import 52-week schedule (260 events) takes < 30 seconds
✓ Create event with image processing < 5 seconds
✓ Database queries on 1000+ events stay < 1 second
✓ Memory usage stays < 100MB after 100 events
```

---

## Severity Classification

### 🔴 CRITICAL (Blocks functionality)
- Database connection leaks
- Bare except clauses  
- Unvalidated user input
- Inconsistent state cleanup

**Impact:** 
- System crashes
- Data loss
- Memory exhaustion
- Stuck users

**Fix by:** This week

---

### 🟠 HIGH (Causes issues)
- Missing validation bounds
- SQLite not configured for concurrency
- API rate limiting issues
- Conflicting state keys

**Impact:**
- Edge case failures
- Slow performance
- Authentication fails
- Hard to debug

**Fix by:** Next week

---

### 🟡 MEDIUM (Impacts quality)
- Duplicate code
- UTC parsing edge cases
- Inconsistent error messages
- Limited logging

**Impact:**
- Maintenance burden
- Poor UX consistency
- Hard to troubleshoot
- Technical debt

**Fix by:** Next sprint

---

### 🟢 LOW (Nice to have)
- Missing type hints
- Slow logging
- Singleton issues

**Impact:**
- IDE support worse
- Slightly slower
- Edge case edge cases

**Fix by:** When time permits

---

## Before & After Metrics

### Database Connections
```
BEFORE:
- Risk: Connection leak in any error path
- Max connections: Unbounded
- Lock timeout: 0 (immediate error)
- Concurrency: Single writer

AFTER:
- Risk: Guaranteed close with context manager
- Max connections: Bounded by SQLite
- Lock timeout: 10 seconds
- Concurrency: Better with WAL mode
```

### Error Handling
```
BEFORE:
- Bare excepts: 5+ locations
- Specific error handling: ~30%
- User-friendly errors: ~40%

AFTER:
- Bare excepts: 0
- Specific error handling: ~90%
- User-friendly errors: ~95%
```

### State Safety
```
BEFORE:
- Inconsistent cleanup: 20+ locations
- State coupling: Multiple conflicting keys
- Error path coverage: ~40%

AFTER:
- Consistent cleanup: Helper functions
- Single source of truth: Merged keys
- Error path coverage: ~100%
```

### Input Safety
```
BEFORE:
- Length validation: 0%
- Bounds checking: ~20%
- Type checking: ~10%

AFTER:
- Length validation: 100%
- Bounds checking: 100%
- Type checking: ~60% (with type hints)
```

---

## Implementation Timeline

### Week 1 (Critical)
- [ ] Mon: Database connection manager
- [ ] Tue: Duration & input validation
- [ ] Wed: State cleanup helpers
- [ ] Thu: Remove bare excepts
- [ ] Fri: SQLite WAL, testing

### Week 2 (High Priority)
- [ ] Mon-Tue: Duplicate code consolidation
- [ ] Wed: UTC offset validation
- [ ] Thu: Export metrics, document
- [ ] Fri: Code review round 2

### Week 3+ (Nice to Have)
- [ ] Add comprehensive type hints
- [ ] Implement exponential backoff
- [ ] Add proper logging framework
- [ ] Write integration tests

---

## Performance Targets

After fixes, these should improve:

| Metric | Before | Target | Test |
|--------|--------|--------|------|
| Connection open time | N/A | <10ms | SQLite metadata query |
| Event creation time | ~500ms | ~300ms | Full event flow |
| Schedule import speed | ~2sec/event | ~0.5sec/event | 52 week × 5 events |
| Memory per user | ~5MB | <2MB | Context cleanup |
| Database lock wait | 0 (fail) | <10s | Concurrent writes |
| Error recovery time | N/A | <1s | User can retry |

---

## Code Health Score Breakdown

```
BEFORE FIX:
database:     ███░░░░░░░ 3/10
error_handle: ████░░░░░░ 4/10
validation:   ██░░░░░░░░ 2/10
state_mgmt:   █░░░░░░░░░ 1/10
testing:      ░░░░░░░░░░ 0/10
─────────────────────────
OVERALL:      ███░░░░░░░ 4/10

AFTER FIXES:
database:     ██████████ 9/10
error_handle: ████████░░ 8/10
validation:   ██████████ 10/10
state_mgmt:   ████████░░ 8/10
testing:      ███░░░░░░░ 3/10
─────────────────────────
OVERALL:      ████████░░ 8/10
```

---

## Monitoring & Alerting

After fixes, monitor these metrics:

```
Production Alerts:
- Database connection count > 50
- API error rate > 5%
- User state corruption detected
- Memory usage > 200MB
- Event creation latency > 10s

Health Checks:
- Database connectivity (every 5 min)
- OAuth token refresh success rate
- Schedule import success rate
- State cleanup verification (logs)
```

