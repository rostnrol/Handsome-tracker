import unittest
from datetime import datetime, timedelta
import pytz

from bot import parse_task_input

class TestStrictDateParse(unittest.TestCase):
    def test_auto_bump_year_for_past_dates(self):
        now_utc = datetime.now(pytz.utc)
        past_date = (now_utc - timedelta(days=1)).strftime('%d.%m')
        due_utc, _, _ = parse_task_input(past_date, 'UTC')
        self.assertGreater(due_utc, datetime.now(pytz.utc))

if __name__ == '__main__':
    unittest.main()
