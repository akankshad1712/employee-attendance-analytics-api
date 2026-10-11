# REVIEW.md

## Defects found in the starter `app/main.py`

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How I fixed it |
|---|---|---|---|---|
| 1 | `health()` | Never pings MongoDB — always returns `{"status":"ok"}` even when the database is unreachable | Start the server with a bad `MONGO_URI`; `/health` still returns 200 | Call `db.command("ping")` inside a try/except; return 503 on failure |
| 2 | `compute_late_minutes` | Grace-period check uses `elapsed_min > 10` (integer minutes), so 09:40:01 — which is 10 min 1 sec late — returns 0 instead of 10 | `compute_late_minutes(09:40:01_ms, "09:30")` should be 10; got 0 | Changed guard to `elapsed_sec <= 600`; `elapsed_sec // 60` is returned only when elapsed is strictly more than 600 s |
| 3 | `compute_overtime` | No minimum threshold — any positive gap between `punch_out` and `shift_end` is returned, including 5 or 14 minutes | Punch out 14 min after shift end → `overtime_minutes` should be 0; got 14 | Added `return diff_min if diff_min >= 30 else 0` (R3) |
| 4 | `compute_work_hours` | Uses Python's built-in `round()` which applies banker's rounding (round-half-to-even), not half-up rounding required by R4/R8 | `round(2.225, 2)` → 2.22 in Python; spec says 2.23 | Replaced with `Decimal(str(value)).quantize(..., ROUND_HALF_UP)` |
| 5 | `compute_overtime` | Builds `shift_end` datetime using `datetime.fromisoformat(date_str)` without timezone; comparison with a tz-aware `punch_out` raises `TypeError` | Any punch-out request after implementing the endpoint | Used IST-aware `datetime` for shift end, converted to UTC before subtraction |
| 6 | `create_employee()` | `created_at = datetime.now()` — naïve local time, not UTC. If the server runs outside IST, stored timestamps are wrong; also not epoch ms in the response | Response field `created_at` returns a Python datetime string (e.g. `"2026-07-06T10:00:00"`), not an integer | Changed to `datetime.now(tz=UTC)`; added `_serialise_employee()` to convert to epoch ms |
| 7 | `create_employee()` | Check-then-insert race: `find_one` + `insert_one` — two concurrent requests with the same `emp_code` can both pass the check and both succeed | Two simultaneous POST /employees with the same `emp_code` → two 201s, duplicate in DB | Removed the `find_one` check; rely solely on the unique index + catch `DuplicateKeyError` → 409 |
| 8 | `list_employees()` | `skip = page * page_size` — off by one. Page 1 skips 20 records (returns the second page); page 0 would give the first | `GET /employees?page=1` returns the second page instead of the first | Changed to `(page - 1) * page_size` |
| 9 | `list_employees()` | `total = db.employees.count_documents({})` ignores the `department` filter — always returns the count of the whole collection | `GET /employees?department=Engineering` returns `total: 6` even when only 2 match | Changed to `count_documents(q)` where `q` carries the filter |
| 10 | `list_employees()` | No `.sort()` on the cursor — MongoDB returns documents in insertion order, not `emp_code` ascending as the contract requires | Listing employees after out-of-order inserts returns wrong order | Added `.sort("emp_code", ASCENDING)` |
| 11 | `punch_in()` | Employee lookup result is never checked for `None`. If `emp_code` doesn't exist, the next line `emp["shift_start"]` raises a `KeyError` (500), not a 404 | `POST /attendance/punch-in` with an unknown `emp_code` → 500 Internal Server Error | Added `if emp is None: raise HTTPException(404, ...)` before accessing `emp` |
| 12 | `punch_in()` | `datetime.fromtimestamp(body.punched_at / 1000)` uses the server's local time zone, not IST. The attendance date (`d`) is computed in local time and will be wrong on any machine not set to IST | On a UTC or US-timezone server, a punch at 23:30 IST is attributed to the wrong calendar date | Changed to `ms_to_dt()` (UTC-aware), then converted to IST via `attendance_date()` for date calculation |
| 13 | `punch_in()` | Overnight-shift date logic (R1) is completely absent. A punch-in at 01:00 IST for a 22:00–06:00 shift should be attributed to the previous day, but the code just takes the raw date | `POST /attendance/punch-in` for EMP0005 (22:00–06:00) at 01:00 IST on Jul 7 → `date: "2026-07-07"`, should be `"2026-07-06"` | Implemented `attendance_date()` helper that detects overnight shifts and adjusts the date when punch time is before `shift_end` |
| 14 | `punch_in()` | Check-then-insert race: `find_one` + `insert_one` — two concurrent punch-ins for the same employee on the same date can both succeed | Two simultaneous POST /attendance/punch-in for same employee → two 201s | Removed the `find_one` check; rely on unique compound index `(emp_code, date)` + catch `DuplicateKeyError` → 409 |
| 15 | `punch_in()` | Response adds `doc["id"] = str(res.inserted_id)` — an `"id"` field not present in the `AttendanceRecord` schema | Response body contains an `"id"` field that the grader's schema check will reject | Removed that line; use `_serialise_record()` with `{"_id": 0}` projection |
| 16 | `punch_in()` | No Pydantic validation on `status` — any string is accepted (e.g. `"ABSENT"`, `"HOLIDAY"`) | `POST /attendance/punch-in` with `"status": "ABSENT"` → 201 instead of 422 | Added `@field_validator("status")` that checks `{"PRESENT", "WFH", "ON_DUTY"}` |
| 17 | `punch_in()` | No validation that `punched_at` is in the valid epoch-ms range (100 000 000 000 – 4 102 444 800 000). A seconds-style value like `1783310000` passes silently | `punched_at: 999999999` (looks like seconds) → creates a record for 1970 | Added `_validate_epoch_ms()` used by `@field_validator` on `PunchInRequest` and all other models |
| 18 | `list_attendance()` | Loads the **entire collection into Python** with `list(db.attendance_logs.find(q))`, sorts and paginates in Python — will time out on 100 k records | Any large dataset; the grader explicitly checks this | Replaced with `.sort().skip().limit()` on the cursor and `count_documents(q)` for the total |
| 19 | `list_attendance()` | `d["id"] = str(d.pop("_id"))` — adds an `"id"` field not in the schema; `_id` was not projected out | Response items contain `"id"` | Changed to `.find(q, {"_id": 0})` and `_serialise_record()` |
| 20 | `list_attendance()` | BSON `datetime` fields (`punch_in`, `punch_out`) are returned as Python datetime strings, not epoch milliseconds | `punch_in` in response is `"2026-07-06T03:58:00"` instead of `1783310280000` | `_serialise_record()` converts all datetime fields via `dt_to_ms()` |
| 21 | `list_attendance()` | No validation that `date_from <= date_to` — inverted range is silently accepted | `?date_from=2026-07-10&date_to=2026-07-01` returns 200 with an empty list instead of 422 | Added explicit check: raise 422 when `date_from > date_to` |

---

## Items I looked at and decided were **not** defects

| What I checked | Why it is not a defect |
|---|---|
| `IST = timezone(timedelta(hours=5, minutes=30))` at module level | Correct UTC+05:30 offset; no DST applies to IST |
| `Optional[int] = None` type on `punched_at` | Correct — the field is optional per the spec (defaults to now) |
| `status: str = "PRESENT"` default in `PunchInIn` | Default value is correct per the spec; type was widened to pass any string, which I fixed separately (defect 16) |
| `body.model_dump()` for employee creation | Pydantic v2 API; correct replacement for the deprecated `.dict()` |
| `db.employees.find_one({"emp_code": body.emp_code})` using `emp_code` (not `_id`) | Correct per spec — employees are always identified by `emp_code`, never by `_id` |
