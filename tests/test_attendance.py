"""
Tests for punch-in, punch-out, list attendance, and regularize endpoints.
Covers: normal flow, validation, 404, 409, concurrency guards, overnight shifts,
date-range filtering, pagination, and the history audit trail.
"""
import pytest
from datetime import timezone, timedelta, datetime

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

VALID_EMP = {
    "emp_code":    "EMP9001",
    "name":        "Attendance Tester",
    "email":       "attest@example.com",
    "department":  "QA",
    "shift_start": "09:30",
    "shift_end":   "18:30",
    "joined_on":   "2026-01-01",
}

OVERNIGHT_EMP = {
    "emp_code":    "EMP9002",
    "name":        "Night Shifter",
    "email":       "night@example.com",
    "department":  "Support",
    "shift_start": "22:00",
    "shift_end":   "06:00",
    "joined_on":   "2026-01-01",
}


def ist_ms(date_str: str, time_str: str) -> int:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


# Fixed instants for the test day 2026-07-06
PI_ONTIME   = ist_ms("2026-07-06", "09:28:00")  # on time (inside grace)
PI_GRACE    = ist_ms("2026-07-06", "09:40:00")  # exactly 10 min → late=0
PI_LATE     = ist_ms("2026-07-06", "09:40:01")  # 1 sec past grace → late=10
PO_NORMAL   = ist_ms("2026-07-06", "18:35:00")  # 5 min OT → <30 → ot=0
PO_OVERTIME = ist_ms("2026-07-06", "19:10:00")  # 40 min OT → ot=40
PO_HALF     = ist_ms("2026-07-06", "13:00:00")  # 3h32m → wh=3.53 → half_day
PO_NEXT_DAY = ist_ms("2026-07-07", "09:30:00")  # > 24h from 09:28 the previous day


class TestPunchIn:
    def test_creates_record_201(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME
        })
        assert r.status_code == 201

    def test_response_fields(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME
        }).json()
        assert r["emp_code"]         == "EMP9001"
        assert r["date"]             == "2026-07-06"
        assert r["status"]           == "PRESENT"
        assert r["punch_in"]         == PI_ONTIME
        assert r["punch_out"]        is None
        assert r["work_hours"]       is None
        assert r["late_minutes"]     == 0
        assert r["overtime_minutes"] == 0
        assert r["half_day"]         is False
        assert r["history"]          == []

    def test_no_id_in_response(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME
        }).json()
        assert "_id" not in r
        assert "id"  not in r

    def test_late_minutes_computed_correctly(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_LATE
        }).json()
        assert r["late_minutes"] == 10

    def test_exactly_grace_boundary_not_late(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_GRACE
        }).json()
        assert r["late_minutes"] == 0

    def test_unknown_employee_returns_404(self, client):
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9999", "punched_at": PI_ONTIME
        })
        assert r.status_code == 404

    def test_duplicate_punch_in_returns_409(self, client):
        client.post("/employees", json=VALID_EMP)
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME
        })
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME + 60_000  # same date
        })
        assert r.status_code == 409

    def test_invalid_status_returns_422(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME, "status": "ABSENT"
        })
        assert r.status_code == 422

    def test_absent_status_not_allowed_at_punch_in(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME, "status": "LEAVE"
        })
        assert r.status_code == 422

    def test_wfh_and_on_duty_are_valid_statuses(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME, "status": "WFH"
        })
        assert r.status_code == 201

    def test_epoch_ms_below_minimum_returns_422(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": 999_999_999  # looks like seconds
        })
        assert r.status_code == 422

    def test_punched_at_defaults_to_now(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP9001"})
        assert r.status_code == 201
        assert r.json()["punch_in"] is not None

    def test_overnight_shift_date_attribution(self, client):
        """Punch-in at 01:00 IST on 2026-07-07 belongs to 2026-07-06 for a 22:00-06:00 shift."""
        client.post("/employees", json=OVERNIGHT_EMP)
        pi = ist_ms("2026-07-07", "01:00:00")
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9002", "punched_at": pi
        }).json()
        assert r["date"] == "2026-07-06"

    def test_overnight_shift_punch_before_shift_end_is_previous_day(self, client):
        """Punch-in at 05:59 IST on 2026-07-07 → still 2026-07-06."""
        client.post("/employees", json=OVERNIGHT_EMP)
        pi = ist_ms("2026-07-07", "05:59:00")
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9002", "punched_at": pi
        }).json()
        assert r["date"] == "2026-07-06"

    def test_overnight_shift_punch_at_shift_end_is_same_day(self, client):
        """Punch-in at exactly 06:00 IST on 2026-07-07 → not before shift_end → 2026-07-07."""
        client.post("/employees", json=OVERNIGHT_EMP)
        pi = ist_ms("2026-07-07", "06:00:00")
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9002", "punched_at": pi
        }).json()
        assert r["date"] == "2026-07-07"


class TestPunchOut:
    def _punch_in(self, client, punch_at=PI_ONTIME, emp="EMP9001"):
        client.post("/employees", json={**VALID_EMP, "emp_code": emp})
        client.post("/attendance/punch-in", json={"emp_code": emp, "punched_at": punch_at})

    def test_punch_out_returns_200(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NORMAL
        })
        assert r.status_code == 200

    def test_work_hours_computed(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NORMAL
        }).json()
        # 09:28 → 18:35 = 9h 7m = 9.1167 → 9.12
        assert r["work_hours"] == 9.12

    def test_overtime_when_gte_30_min(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_OVERTIME
        }).json()
        assert r["overtime_minutes"] == 40

    def test_no_overtime_below_30_min(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NORMAL  # 5 min OT
        }).json()
        assert r["overtime_minutes"] == 0

    def test_half_day_flagged(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_HALF
        }).json()
        assert r["half_day"] is True
        assert r["work_hours"] < 4.50

    def test_full_day_not_half(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NORMAL
        }).json()
        assert r["half_day"] is False

    def test_unknown_employee_returns_404(self, client):
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9999", "punched_at": PO_NORMAL
        })
        assert r.status_code == 404

    def test_no_open_punch_in_returns_404(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NORMAL
        })
        assert r.status_code == 404

    def test_double_punch_out_returns_409(self, client):
        self._punch_in(client)
        client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NORMAL
        })
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NORMAL + 1000
        })
        assert r.status_code == 409

    def test_punch_out_before_punch_in_returns_422(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME - 1000
        })
        assert r.status_code == 422

    def test_punch_out_equal_to_punch_in_returns_422(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME
        })
        assert r.status_code == 422

    def test_punch_out_more_than_24h_returns_422(self, client):
        self._punch_in(client)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NEXT_DAY
        })
        assert r.status_code == 422

    def test_overnight_punch_out_within_24h_accepted(self, client):
        """Overnight shift: punch-in 21:55 Jul-6, punch-out 06:40 Jul-7 — within 24h."""
        client.post("/employees", json=OVERNIGHT_EMP)
        pi = ist_ms("2026-07-06", "21:55:00")
        po = ist_ms("2026-07-07", "06:40:00")
        client.post("/attendance/punch-in",  json={"emp_code": "EMP9002", "punched_at": pi})
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP9002", "punched_at": po})
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 40


class TestListAttendance:
    def _seed(self, client):
        client.post("/employees", json=VALID_EMP)
        for day, pi_offset in [("2026-07-06", 0), ("2026-07-07", 86_400_000),
                                ("2026-07-08", 172_800_000)]:
            client.post("/attendance/punch-in", json={
                "emp_code": "EMP9001",
                "punched_at": PI_ONTIME + pi_offset,
            })

    def test_returns_all_records(self, client):
        self._seed(client)
        r = client.get("/attendance?emp_code=EMP9001").json()
        assert r["total"] == 3

    def test_sorted_date_desc_emp_asc(self, client):
        self._seed(client)
        dates = [i["date"] for i in client.get("/attendance").json()["items"]]
        assert dates == sorted(dates, reverse=True)

    def test_date_from_to_filter(self, client):
        self._seed(client)
        r = client.get("/attendance?date_from=2026-07-07&date_to=2026-07-08").json()
        assert r["total"] == 2
        for item in r["items"]:
            assert "2026-07-07" <= item["date"] <= "2026-07-08"

    def test_date_from_after_date_to_returns_422(self, client):
        r = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
        assert r.status_code == 422

    def test_status_filter(self, client):
        client.post("/employees", json=VALID_EMP)
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_ONTIME, "status": "WFH"
        })
        r = client.get("/attendance?status=WFH").json()
        assert r["total"] == 1
        assert r["items"][0]["status"] == "WFH"

    def test_invalid_status_filter_returns_422(self, client):
        r = client.get("/attendance?status=HOLIDAY")
        assert r.status_code == 422

    def test_pagination_page_size(self, client):
        self._seed(client)
        r = client.get("/attendance?page_size=2").json()
        assert len(r["items"]) == 2
        assert r["total"] == 3

    def test_no_id_fields_in_items(self, client):
        self._seed(client)
        for item in client.get("/attendance").json()["items"]:
            assert "_id" not in item
            assert "id"  not in item

    def test_punch_in_is_epoch_ms(self, client):
        self._seed(client)
        item = client.get("/attendance?emp_code=EMP9001").json()["items"][0]
        assert isinstance(item["punch_in"], int)
        assert item["punch_in"] > 100_000_000_000


class TestRegularize:
    def _setup(self, client):
        client.post("/employees", json=VALID_EMP)
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP9001", "punched_at": PI_LATE  # late=10
        })
        client.post("/attendance/punch-out", json={
            "emp_code": "EMP9001", "punched_at": PO_NORMAL
        })

    def test_correction_returns_200(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_in":      PI_ONTIME,
            "reason":        "Biometric glitch corrected",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200

    def test_history_appended(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_in":      PI_ONTIME,
            "reason":        "Biometric glitch corrected",
            "regularized_by": "hr.admin",
        }).json()
        assert len(r["history"]) == 1
        h = r["history"][0]
        assert h["by"]     == "hr.admin"
        assert h["reason"] == "Biometric glitch corrected"
        assert "punch_in"  in h["changes"]

    def test_history_changes_only_changed_fields(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_in":      PI_ONTIME,
            "reason":        "Biometric glitch corrected",
            "regularized_by": "hr.admin",
        }).json()
        # Only punch_in changed (and derived late_minutes/work_hours)
        changes = r["history"][0]["changes"]
        assert "punch_in" in changes
        assert "status"   not in changes   # status did not change

    def test_late_minutes_recomputed_after_correction(self, client):
        self._setup(client)  # punch_in = PI_LATE → late=10
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_in":      PI_ONTIME,  # on time now
            "reason":        "Fixed time",
            "regularized_by": "hr",
        }).json()
        assert r["late_minutes"] == 0

    def test_work_hours_recomputed_after_punch_in_change(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_in":      PI_ONTIME,
            "reason":        "Fixed time",
            "regularized_by": "hr",
        }).json()
        assert r["work_hours"] is not None

    def test_no_change_returns_422(self, client):
        self._setup(client)
        # Sending the exact same punch_in value
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_in":      PI_LATE,   # same as current
            "reason":        "No change",
            "regularized_by": "hr",
        })
        assert r.status_code == 422

    def test_absent_status_clears_punch_times(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "status":        "ABSENT",
            "reason":        "Retroactive absence",
            "regularized_by": "hr",
        }).json()
        assert r["status"]    == "ABSENT"
        assert r["punch_in"]  is None
        assert r["punch_out"] is None

    def test_absent_with_punch_in_returns_422(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "status":        "ABSENT",
            "punch_in":      PI_ONTIME,
            "reason":        "Bad request",
            "regularized_by": "hr",
        })
        assert r.status_code == 422

    def test_punch_out_before_punch_in_returns_422(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_out":     PI_ONTIME - 1000,
            "reason":        "Invalid",
            "regularized_by": "hr",
        })
        assert r.status_code == 422

    def test_unknown_employee_returns_404(self, client):
        r = client.patch("/attendance/EMP9999/2026-07-06", json={
            "reason": "Testing unknown emp", "regularized_by": "hr"
        })
        assert r.status_code == 404

    def test_unknown_date_returns_404(self, client):
        client.post("/employees", json=VALID_EMP)
        r = client.patch("/attendance/EMP9001/2026-01-01", json={
            "reason": "Test no record", "regularized_by": "hr"
        })
        assert r.status_code == 404

    def test_reason_too_short_returns_422(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_in": PI_ONTIME, "reason": "hi", "regularized_by": "hr"
        })
        assert r.status_code == 422

    def test_history_punch_times_are_epoch_ms(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP9001/2026-07-06", json={
            "punch_in":      PI_ONTIME,
            "reason":        "Time correction check",
            "regularized_by": "hr.admin",
        }).json()
        changes = r["history"][0]["changes"]
        assert isinstance(changes["punch_in"]["from"], int)
        assert isinstance(changes["punch_in"]["to"],   int)
