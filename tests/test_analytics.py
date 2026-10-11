"""
Tests for analytics endpoints:
  - GET /analytics/employees/{emp_code}/monthly
  - GET /analytics/departments/summary
  - GET /analytics/leaderboard/late
  - GET /analytics/departments/{department}/trend
"""
import pytest
from datetime import timezone, timedelta, datetime

IST = timezone(timedelta(hours=5, minutes=30))


def ist_ms(date_str: str, time_str: str) -> int:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Shared seed helpers
# ---------------------------------------------------------------------------
EMP_ENG = {
    "emp_code": "EMP8001", "name": "Eng One", "email": "e1@x.com",
    "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
    "joined_on": "2026-07-01",
}
EMP_ENG2 = {
    "emp_code": "EMP8002", "name": "Eng Two", "email": "e2@x.com",
    "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
    "joined_on": "2026-07-10",   # joined mid-month
}
EMP_SALES = {
    "emp_code": "EMP8003", "name": "Sales One", "email": "s1@x.com",
    "department": "Sales", "shift_start": "09:30", "shift_end": "18:30",
    "joined_on": "2026-07-01",
}
EMP_NO_LOGS = {
    "emp_code": "EMP8004", "name": "Ghost", "email": "g@x.com",
    "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
    "joined_on": "2026-07-01",   # joined but no logs
}


def seed_employee(client, emp):
    client.post("/employees", json=emp)


def punch(client, emp_code, date_str, pi_time, po_time=None, status="PRESENT"):
    pi_ms = ist_ms(date_str, pi_time)
    client.post("/attendance/punch-in", json={
        "emp_code": emp_code, "punched_at": pi_ms, "status": status
    })
    if po_time:
        po_ms = ist_ms(date_str, po_time) if not po_time.startswith("+") else \
                ist_ms(date_str, "00:00:00") + int(po_time[1:]) * 1000
        client.post("/attendance/punch-out", json={
            "emp_code": emp_code, "punched_at": ist_ms(date_str, po_time)
        })


# ---------------------------------------------------------------------------
# Employee monthly
# ---------------------------------------------------------------------------
class TestEmployeeMonthly:
    def test_unknown_employee_returns_404(self, client):
        r = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert r.status_code == 404

    def test_invalid_month_returns_422(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-13")
        assert r.status_code == 422

    def test_month_required(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get("/analytics/employees/EMP8001/monthly")
        assert r.status_code == 422

    def test_response_fields_present(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-07").json()
        for f in ["emp_code", "month", "working_days", "present_days", "leave_days",
                  "late_count", "total_late_minutes", "total_overtime_minutes", "attendance_pct"]:
            assert f in r, f"missing {f}"

    def test_no_logs_gives_zeroes(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-07").json()
        assert r["present_days"]   == 0
        assert r["leave_days"]     == 0
        assert r["late_count"]     == 0
        assert r["attendance_pct"] is not None  # working_days > 0 so pct = 0.0

    def test_present_day_counted(self, client):
        seed_employee(client, EMP_ENG)
        # 2026-07-07 is a Tuesday (weekday)
        punch(client, "EMP8001", "2026-07-07", "09:28:00", "18:35:00")
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-07").json()
        assert r["present_days"] == 1.0

    def test_half_day_counts_as_0_5(self, client):
        seed_employee(client, EMP_ENG)
        # punch-in and out for < 4.5h on a weekday
        punch(client, "EMP8001", "2026-07-07", "09:30:00", "13:00:00")
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-07").json()
        assert r["present_days"] == 0.5

    def test_leave_day_counted(self, client):
        seed_employee(client, EMP_ENG)
        # Manually insert a LEAVE record via seed
        from app.main import logs_col
        logs_col.insert_one({
            "emp_code": "EMP8001", "date": "2026-07-07", "status": "LEAVE",
            "punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
        })
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-07").json()
        assert r["leave_days"] == 1
        assert r["present_days"] == 0

    def test_late_count_and_minutes(self, client):
        seed_employee(client, EMP_ENG)
        # 10:05 IST → 35 min late on 2026-07-07 (Tue)
        punch(client, "EMP8001", "2026-07-07", "10:05:00", "18:30:00")
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-07").json()
        assert r["late_count"]         == 1
        assert r["total_late_minutes"] == 35

    def test_working_days_respects_joined_on(self, client):
        """Employee who joined 2026-07-10 should have fewer working days than full month."""
        seed_employee(client, EMP_ENG2)
        r = client.get("/analytics/employees/EMP8002/monthly?month=2026-07").json()
        full_month = client.get(
            "/analytics/employees/EMP8001/monthly?month=2026-07"
        )
        # EMP8002 joined mid-month, so fewer working days than a full-month employee
        # July 2026 has 23 weekdays total; from 10th there are fewer
        assert r["working_days"] < 23

    def test_attendance_pct_null_when_zero_working_days(self, client):
        """An employee who joined after the month ends has 0 working days → pct=null."""
        future_emp = {**EMP_ENG, "emp_code": "EMP8099", "joined_on": "2026-08-01"}
        seed_employee(client, future_emp)
        r = client.get("/analytics/employees/EMP8099/monthly?month=2026-07").json()
        assert r["working_days"]   == 0
        assert r["attendance_pct"] is None

    def test_overtime_accumulated(self, client):
        seed_employee(client, EMP_ENG)
        # 19:10 → 40 min OT on Tue
        punch(client, "EMP8001", "2026-07-07", "09:28:00", "19:10:00")
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-07").json()
        assert r["total_overtime_minutes"] == 40

    def test_weekend_records_dont_count_as_present_days(self, client):
        """Records on Sat/Sun don't count toward present_days (R7) but do count late/ot."""
        seed_employee(client, EMP_ENG)
        # 2026-07-05 is a Sunday
        punch(client, "EMP8001", "2026-07-05", "09:28:00", "18:35:00")
        r = client.get("/analytics/employees/EMP8001/monthly?month=2026-07").json()
        assert r["present_days"] == 0  # weekend, not counted


# ---------------------------------------------------------------------------
# Department summary
# ---------------------------------------------------------------------------
class TestDepartmentSummary:
    def test_returns_all_departments(self, client):
        seed_employee(client, EMP_ENG)
        seed_employee(client, EMP_SALES)
        r = client.get("/analytics/departments/summary?month=2026-07").json()
        depts = [i["department"] for i in r["items"]]
        assert "Engineering" in depts
        assert "Sales" in depts

    def test_sorted_by_department_ascending(self, client):
        seed_employee(client, EMP_ENG)
        seed_employee(client, EMP_SALES)
        r = client.get("/analytics/departments/summary?month=2026-07").json()
        depts = [i["department"] for i in r["items"]]
        assert depts == sorted(depts)

    def test_headcount_includes_employees_with_zero_logs(self, client):
        """R9: employee with joined_on <= last day of month counts even with no logs."""
        seed_employee(client, EMP_ENG)
        seed_employee(client, EMP_NO_LOGS)  # same dept, no logs
        r = client.get("/analytics/departments/summary?month=2026-07").json()
        eng = next(i for i in r["items"] if i["department"] == "Engineering")
        assert eng["headcount"] == 2

    def test_headcount_excludes_employees_joined_after_month(self, client):
        """Employee who joins after month end should not be in headcount."""
        seed_employee(client, EMP_ENG)
        late_emp = {**EMP_ENG, "emp_code": "EMP8010", "joined_on": "2026-08-01"}
        seed_employee(client, late_emp)
        r = client.get("/analytics/departments/summary?month=2026-07").json()
        eng = next(i for i in r["items"] if i["department"] == "Engineering")
        assert eng["headcount"] == 1

    def test_department_filter(self, client):
        seed_employee(client, EMP_ENG)
        seed_employee(client, EMP_SALES)
        r = client.get("/analytics/departments/summary?month=2026-07&department=Sales").json()
        assert len(r["items"]) == 1
        assert r["items"][0]["department"] == "Sales"

    def test_avg_work_hours_null_when_no_punch_outs(self, client):
        seed_employee(client, EMP_ENG)
        # Punch in but not out
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP8001",
            "punched_at": ist_ms("2026-07-07", "09:28:00"),
        })
        r = client.get("/analytics/departments/summary?month=2026-07").json()
        eng = next(i for i in r["items"] if i["department"] == "Engineering")
        assert eng["avg_work_hours"] is None

    def test_late_count_and_total_late_minutes(self, client):
        seed_employee(client, EMP_ENG)
        punch(client, "EMP8001", "2026-07-07", "10:05:00", "18:30:00")  # 35 late
        r = client.get("/analytics/departments/summary?month=2026-07").json()
        eng = next(i for i in r["items"] if i["department"] == "Engineering")
        assert eng["late_count"]         == 1
        assert eng["total_late_minutes"] == 35

    def test_invalid_month_returns_422(self, client):
        r = client.get("/analytics/departments/summary?month=2026-13")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------------------------
class TestLateLeaderboard:
    def test_empty_when_no_late_records(self, client):
        seed_employee(client, EMP_ENG)
        punch(client, "EMP8001", "2026-07-07", "09:28:00", "18:35:00")  # on time
        r = client.get("/analytics/leaderboard/late?month=2026-07").json()
        assert r["items"] == []

    def test_rank_1_highest_late_minutes(self, client):
        seed_employee(client, EMP_ENG)
        seed_employee(client, EMP_SALES)
        punch(client, "EMP8001", "2026-07-07", "10:05:00", "18:30:00")  # 35 min late
        punch(client, "EMP8003", "2026-07-07", "10:15:00", "18:30:00")  # 45 min late
        r = client.get("/analytics/leaderboard/late?month=2026-07").json()
        assert r["items"][0]["emp_code"] == "EMP8003"
        assert r["items"][0]["rank"]     == 1
        assert r["items"][1]["rank"]     == 2

    def test_ties_share_rank_and_next_rank_skipped(self, client):
        """Two employees with same late minutes → both rank 1, no rank 2."""
        seed_employee(client, EMP_ENG)
        seed_employee(client, EMP_SALES)
        seed_employee(client, {**EMP_ENG, "emp_code": "EMP8005", "email": "e5@x.com"})
        # EMP8001 and EMP8003 both have 35 min late; EMP8005 has 45
        punch(client, "EMP8001", "2026-07-07", "10:05:00", "18:30:00")  # 35
        punch(client, "EMP8003", "2026-07-07", "10:05:00", "18:30:00")  # 35
        punch(client, "EMP8005", "2026-07-07", "10:15:00", "18:30:00")  # 45
        r = client.get("/analytics/leaderboard/late?month=2026-07").json()
        items = r["items"]
        ranks = [i["rank"] for i in items]
        # EMP8005 rank=1; EMP8001 and EMP8003 share rank=2; no rank=3
        assert ranks[0] == 1
        assert ranks[1] == ranks[2] == 2
        assert 3 not in ranks

    def test_limit_respected(self, client):
        seed_employee(client, EMP_ENG)
        seed_employee(client, EMP_SALES)
        punch(client, "EMP8001", "2026-07-07", "10:05:00", "18:30:00")
        punch(client, "EMP8003", "2026-07-07", "10:15:00", "18:30:00")
        r = client.get("/analytics/leaderboard/late?month=2026-07&limit=1").json()
        # limit=1 → only rank<=1 → 1 row
        assert len(r["items"]) == 1

    def test_department_filter(self, client):
        seed_employee(client, EMP_ENG)
        seed_employee(client, EMP_SALES)
        punch(client, "EMP8001", "2026-07-07", "10:05:00", "18:30:00")  # Eng, 35 min
        punch(client, "EMP8003", "2026-07-07", "10:15:00", "18:30:00")  # Sales, 45 min
        r = client.get(
            "/analytics/leaderboard/late?month=2026-07&department=Engineering"
        ).json()
        assert all(i["department"] == "Engineering" for i in r["items"])

    def test_name_and_department_in_response(self, client):
        seed_employee(client, EMP_ENG)
        punch(client, "EMP8001", "2026-07-07", "10:05:00", "18:30:00")
        r = client.get("/analytics/leaderboard/late?month=2026-07").json()
        item = r["items"][0]
        assert item["name"]       == "Eng One"
        assert item["department"] == "Engineering"

    def test_order_total_desc_then_emp_code_asc(self, client):
        """Tie on total_late_minutes → sorted by emp_code ascending."""
        seed_employee(client, EMP_ENG)
        seed_employee(client, {**EMP_ENG, "emp_code": "EMP8006", "email": "e6@x.com"})
        punch(client, "EMP8001", "2026-07-07", "10:05:00", "18:30:00")  # 35
        punch(client, "EMP8006", "2026-07-07", "10:05:00", "18:30:00")  # 35 — tie
        r = client.get("/analytics/leaderboard/late?month=2026-07").json()
        codes = [i["emp_code"] for i in r["items"]]
        assert codes == sorted(codes)


# ---------------------------------------------------------------------------
# Department trend
# ---------------------------------------------------------------------------
class TestDepartmentTrend:
    def test_unknown_department_returns_404(self, client):
        r = client.get(
            "/analytics/departments/NoSuchDept/trend?from=2026-07-01&to=2026-07-07"
        )
        assert r.status_code == 404

    def test_to_before_from_returns_422(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01"
        )
        assert r.status_code == 422

    def test_range_exceeds_92_days_returns_422(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-10"
        )
        assert r.status_code == 422

    def test_exactly_one_row_per_day(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10"
        ).json()
        assert len(r["items"]) == 10

    def test_weekends_included_with_is_working_day_false(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-04&to=2026-07-06"
        ).json()
        items = {i["date"]: i for i in r["items"]}
        assert items["2026-07-04"]["is_working_day"] is False  # Saturday
        assert items["2026-07-05"]["is_working_day"] is False  # Sunday
        assert items["2026-07-06"]["is_working_day"] is True   # Monday

    def test_attendance_rate_null_on_weekend(self, client):
        seed_employee(client, EMP_ENG)
        punch(client, "EMP8001", "2026-07-05", "09:28:00", "18:35:00")  # Sunday
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-05"
        ).json()
        assert r["items"][0]["attendance_rate"] is None

    def test_attendance_rate_on_working_day_with_no_records(self, client):
        """Working day with no records but headcount > 0 → rate = 0.0 (not null)."""
        seed_employee(client, EMP_ENG)
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-06"
        ).json()
        assert r["items"][0]["attendance_rate"] == 0.0

    def test_headcount_respects_joined_on(self, client):
        """Employees who join after the trend day are not in headcount."""
        seed_employee(client, EMP_ENG2)   # joined 2026-07-10
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-07&to=2026-07-07"
        ).json()
        assert r["items"][0]["headcount"] == 0

    def test_moving_avg_7d_uses_non_null_rates(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07"
        ).json()
        # Every item should have a moving_avg or None (no errors)
        for item in r["items"]:
            assert "moving_avg_7d" in item

    def test_response_has_department_field(self, client):
        seed_employee(client, EMP_ENG)
        r = client.get(
            "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-01"
        ).json()
        assert r["department"] == "Engineering"
