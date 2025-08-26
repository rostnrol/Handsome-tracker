import unittest
from datetime import datetime, timedelta
import pytz

from bot import parse_task_input, parse_lead_minutes

class TestStrictDateParse(unittest.TestCase):
    def test_auto_bump_year_for_past_dates(self):
        now_utc = datetime.now(pytz.utc)
        past_date = (now_utc - timedelta(days=1)).strftime('%d.%m')
        due_utc, _, _ = parse_task_input(past_date, 'UTC')
        self.assertGreater(due_utc, datetime.now(pytz.utc))

    def test_time_only_moves_to_next_day(self):
        now_utc = datetime.now(pytz.utc)
        past = now_utc - timedelta(hours=1)
        past_time = past.strftime('%H:%M')
        due_utc, _, _ = parse_task_input(past_time, 'UTC')
        expected = (past + timedelta(days=1)).replace(second=0, microsecond=0)
        self.assertEqual(due_utc, expected)

    def test_parse_lead_minutes_accepts_dotted_units(self):
        minutes, status = parse_lead_minutes("15 мин.")
        self.assertEqual((minutes, status), (15, "ok"))
        minutes, status = parse_lead_minutes("1 ч.")
        self.assertEqual((minutes, status), (60, "ok"))

if __name__ == '__main__':
    unittest.main()
