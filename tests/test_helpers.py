"""
Unit tests for pure helper functions (no DB, no HTTP).
Tests cover R1-R5 thoroughly with known IST timestamps.
"""
from datetime import timezone, timedelta, datetime

import pytest

from app.main import (
    compute_late_minutes,
    compute_work_hours,
    compute_overtime_minutes,
    compute_half_day,
    attendance_date,
    _half_up,
)

IST = timezone(timedelta(hours=5, minutes=30))


def ist_ms(date_str: str, time_str: str) -> int:
    """Build epoch ms from 'YYYY-MM-DD' + 'HH:MM:SS' in IST."""
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# R2 — late_minutes
# ---------------------------------------------------------------------------
class TestLateMinutes:
    SHIFT = "09:30"

    def test_before_shift_start_is_zero(self):
        assert compute_late_minutes(ist_ms("2026-07-06", "09:00:00"), self.SHIFT) == 0

    def test_exactly_on_shift_start_is_zero(self):
        assert compute_late_minutes(ist_ms("2026-07-06", "09:30:00"), self.SHIFT) == 0

    def test_grace_boundary_exactly_10_minutes_is_zero(self):
        # 09:40:00 → exactly 10 min → NOT late (rule says *strictly more than* 10)
        assert compute_late_minutes(ist_ms("2026-07-06", "09:40:00"), self.SHIFT) == 0

    def test_one_second_past_grace_is_10(self):
        # 09:40:01 → 10 min 1 sec → late_minutes = floor(601/60) = 10
        assert compute_late_minutes(ist_ms("2026-07-06", "09:40:01"), self.SHIFT) == 10

    def test_45_minutes_late(self):
        # 10:15:59 → 45 min 59 sec since 09:30 → floor = 45
        assert compute_late_minutes(ist_ms("2026-07-06", "10:15:59"), self.SHIFT) == 45

    def test_35_minutes_late(self):
        # 10:05:00 IST → 35 min since 09:30
        assert compute_late_minutes(ist_ms("2026-07-06", "10:05:00"), self.SHIFT) == 35

    def test_different_day_same_logic(self):
        # Works correctly across different dates
        assert compute_late_minutes(ist_ms("2026-07-14", "09:40:01"), self.SHIFT) == 10


# ---------------------------------------------------------------------------
# R3 — overtime_minutes
# ---------------------------------------------------------------------------
class TestOvertimeMinutes:
    SHIFT_END = "18:30"
    DATE      = "2026-07-06"

    def test_no_overtime_below_30_minutes(self):
        # 18:44:59 → 14 min 59 sec → 0 (< 30)
        assert compute_overtime_minutes(ist_ms(self.DATE, "18:44:59"), self.SHIFT_END, self.DATE) == 0

    def test_exactly_29_minutes_is_zero(self):
        assert compute_overtime_minutes(ist_ms(self.DATE, "18:59:00"), self.SHIFT_END, self.DATE) == 0

    def test_exactly_30_minutes_counts(self):
        assert compute_overtime_minutes(ist_ms(self.DATE, "19:00:00"), self.SHIFT_END, self.DATE) == 30

    def test_40_minutes_overtime(self):
        assert compute_overtime_minutes(ist_ms(self.DATE, "19:10:00"), self.SHIFT_END, self.DATE) == 40

    def test_floor_not_ceil(self):
        # 19:10:59 → 40 min 59 sec → floor = 40
        assert compute_overtime_minutes(ist_ms(self.DATE, "19:10:59"), self.SHIFT_END, self.DATE) == 40

    def test_overnight_shift_overtime(self):
        # Overnight shift 22:00-06:00, shift_end is on the next day
        # punch-out 06:40 IST on 2026-07-07 → 40 min after 06:00 → overtime = 40
        punch_out_ms = ist_ms("2026-07-07", "06:40:00")
        assert compute_overtime_minutes(punch_out_ms, "06:00", "2026-07-06") == 40


# ---------------------------------------------------------------------------
# R4 — work_hours (half-up rounding)
# ---------------------------------------------------------------------------
class TestWorkHours:
    def test_simple_full_day(self):
        # 09:28 to 18:35 = 9h 7m = 9.1167... → rounded half-up to 2dp = 9.12
        pi = ist_ms("2026-07-06", "09:28:00")
        po = ist_ms("2026-07-06", "18:35:00")
        assert compute_work_hours(pi, po) == 9.12

    def test_half_up_not_bankers(self):
        # 3h 30m exactly = 3.5h — half-up (not banker's which would round to 4)
        pi = ist_ms("2026-07-07", "09:30:00")
        po = ist_ms("2026-07-07", "13:00:00")
        assert compute_work_hours(pi, po) == 3.5

    def test_overnight_work_hours(self):
        # 22:00 to 06:40 next day = 8h 40m = 8.6667 → 8.67
        pi = ist_ms("2026-07-06", "22:00:00")
        po = ist_ms("2026-07-07", "06:40:00")
        assert compute_work_hours(pi, po) == 8.67

    def test_wh_rounds_half_up_at_half(self):
        # exactly 2.5 seconds → 2.5/3600 = 0.000694... → 0.0
        # use a boundary that matters: 5h 27m = 5.45h → rounds to 5.45
        pi = ist_ms("2026-07-06", "09:00:00")
        po = ist_ms("2026-07-06", "14:27:00")
        assert compute_work_hours(pi, po) == 5.45


# ---------------------------------------------------------------------------
# R5 — half_day
# ---------------------------------------------------------------------------
class TestHalfDay:
    def test_exactly_4_50_is_not_half(self):
        # work_hours = 4.50 → NOT a half day (rule: < 4.50)
        assert compute_half_day(4.50) is False

    def test_just_below_4_50_is_half(self):
        assert compute_half_day(4.49) is True

    def test_3_5_is_half(self):
        assert compute_half_day(3.5) is True

    def test_full_day_is_not_half(self):
        assert compute_half_day(9.0) is False


# ---------------------------------------------------------------------------
# R1 — attendance_date (IST + overnight shift)
# ---------------------------------------------------------------------------
class TestAttendanceDate:
    def test_normal_shift_date(self):
        # 09:28 IST on 2026-07-06 → date = 2026-07-06
        ts = ist_ms("2026-07-06", "09:28:00")
        assert attendance_date(ts, "09:30", "18:30") == "2026-07-06"

    def test_overnight_shift_before_midnight(self):
        # Shift 22:00-06:00; punch-in 21:55 IST on 2026-07-06 → date = 2026-07-06
        ts = ist_ms("2026-07-06", "21:55:00")
        assert attendance_date(ts, "22:00", "06:00") == "2026-07-06"

    def test_overnight_shift_after_midnight_belongs_to_previous_day(self):
        # Shift 22:00-06:00; punch-in 01:00 IST on 2026-07-07 → belongs to 2026-07-06
        ts = ist_ms("2026-07-07", "01:00:00")
        assert attendance_date(ts, "22:00", "06:00") == "2026-07-06"

    def test_overnight_shift_exactly_at_shift_end_is_next_day(self):
        # punch-in exactly at 06:00 IST on 2026-07-07 → NOT before shift_end → stays 2026-07-07
        ts = ist_ms("2026-07-07", "06:00:00")
        assert attendance_date(ts, "22:00", "06:00") == "2026-07-07"

    def test_normal_shift_near_midnight(self):
        # 09:30 shift does not trigger overnight logic
        ts = ist_ms("2026-07-06", "09:30:00")
        assert attendance_date(ts, "09:30", "18:30") == "2026-07-06"


# ---------------------------------------------------------------------------
# _half_up rounding helper
# ---------------------------------------------------------------------------
class TestHalfUp:
    def test_rounds_half_up(self):
        assert _half_up(2.225, 2) == 2.23   # Python float round would give 2.22

    def test_rounds_down_normally(self):
        assert _half_up(2.224, 2) == 2.22

    def test_four_decimal_places(self):
        assert _half_up(0.00005, 4) == 0.0001

    def test_integer_input(self):
        assert _half_up(3.0, 2) == 3.0
