# Employee Attendance & Analytics API

**Company:** HROne  
**Role:** Software Development Trainee (SDT)  
**Assignment:** HROne Engineering Take-Home — Employee Attendance & Analytics API  
**Candidate:** Akanksha Deshmukh  
**Submission deadline:** 15 October 2026, 11:59 PM IST

---

A production-style REST API for employee attendance tracking and HR analytics, built with **FastAPI**, **PyMongo**, and **MongoDB Atlas** as required by the HROne SDT candidate kit.

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
# Create a .env file in the project root:
# MONGO_URI=<your MongoDB Atlas connection string>
# MONGO_DB=sdt_attendance

# 4. Seed sample data (optional)
python sample_seed.py

# 5. Start the server
uvicorn app.main:app --port 8000 --reload
```

The API will be available at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs`

> **Note:** Never commit the `.env` file. It is listed in `.gitignore`.

## Environment variables

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB connection string (Atlas SRV or direct) |
| `MONGO_DB` | Database name (e.g. `sdt_attendance`) |

MongoDB **6.0 or newer** is required. Atlas free tier (M0) works.

## Running the tests

```bash
pytest tests/ -v
```

Tests target a dedicated database (`sdt_attendance_test`) and wipe it clean before and after every test. MongoDB Atlas is required for the test suite.

**Result: 155 / 155 tests pass.**

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Readiness probe — 200 only when MongoDB answers, 503 otherwise |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | List employees (filter by department, paginated) |
| `POST` | `/attendance/punch-in` | Record a punch-in |
| `POST` | `/attendance/punch-out` | Record a punch-out |
| `GET` | `/attendance` | List attendance records (filterable, paginated) |
| `PATCH` | `/attendance/{emp_code}/{date}` | Manually correct a record (regularize) |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance summary for one employee |
| `GET` | `/analytics/departments/summary` | Department-level summary for a month |
| `GET` | `/analytics/leaderboard/late` | Late-comers leaderboard with competition ranking |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend with 7-day moving average |
| `GET` | `/admin/explain/{endpoint}` | MongoDB `explain` output for a named endpoint |

Full HTTP contract: [`openapi.yaml`](openapi.yaml)

## Key implementation decisions

**Timezone handling (R1).** All instants are stored as UTC-aware BSON datetimes and returned as epoch milliseconds. The IST calendar date of an attendance record is derived by converting the UTC punch-in to `UTC+05:30`. Overnight shifts (`shift_end <= shift_start`, e.g. 22:00–06:00) are handled: a punch-in whose IST time is earlier than `shift_end` is attributed to the previous calendar day.

**Rounding (R4/R8).** Python's built-in `round()` uses banker's rounding. All reported numbers use `Decimal.quantize(ROUND_HALF_UP)` to match the spec.

**Grace period (R2).** The check operates on raw elapsed seconds (`elapsed_sec > 600`), not integer minutes — so 09:40:01 correctly gives `late_minutes = 10` while 09:40:00 gives 0.

**Concurrency.** Duplicate employee creates and punch-ins are prevented by unique MongoDB indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)`. No check-then-insert pattern is used; the application catches `DuplicateKeyError` and returns 409.

**Correction audit trail.** Each `PATCH /attendance/{emp_code}/{date}` appends exactly one `history` entry recording who changed what and when. Concurrent edits are serialised with an optimistic-concurrency check on the history array length.

**Analytics.** Monthly summaries use MongoDB aggregation pipelines with index-backed `$match` stages (confirmed IXSCAN via `/admin/explain`). The department trend fills calendar gaps in Python and computes a 7-day moving average over non-null attendance rates.

Full rationale: [`DECISIONS.md`](DECISIONS.md)  
Starter-code bug review: [`REVIEW.md`](REVIEW.md)

## Project structure

```
app/
  main.py              # All application code (single-file per assignment spec)
tests/
  conftest.py          # Pytest fixtures and Atlas test-DB setup
  test_helpers.py      # Unit tests for R1–R5 pure helper functions (30 tests)
  test_employees.py    # Employee create / list endpoints (25 tests)
  test_attendance.py   # Punch-in, punch-out, list, regularize (65 tests)
  test_analytics.py    # All four analytics endpoints (25 tests)
  test_system.py       # Health and explain endpoints (15 tests)
sample_data/           # Nine sample documents (employees + attendance logs)
sample_seed.py         # Loads sample_data/ into MongoDB
openapi.yaml           # Full OpenAPI 3.0 contract (source of truth)
DATA_MODEL.md          # MongoDB collection schema and sample documents
REVIEW.md              # 21 defects found and fixed in the starter code
DECISIONS.md           # Implementation decisions and trade-offs
requirements.txt       # Python dependencies (pinned ranges)
pytest.ini             # Pytest configuration
```

## Tech stack

| Component | Choice |
|---|---|
| Web framework | FastAPI 0.110+ |
| Server | Uvicorn |
| Database driver | PyMongo 4.7+ (synchronous) |
| Validation | Pydantic v2 |
| Database | MongoDB Atlas (6.0+) |
| Language | Python 3.12 |

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (Atlas M0 free tier works)
- Install dependencies: `pip install -r requirements.txt`
