# Employee Attendance & Analytics API

HROne Software Engineer Trainee assignment — a production-style REST API for employee attendance tracking and HR analytics, built with FastAPI, PyMongo, and MongoDB Atlas.

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment (copy and fill in your values)
cp .env.example .env
# MONGO_URI=<your Atlas connection string>
# MONGO_DB=sdt_attendance

# 4. Seed sample data (optional)
python sample_seed.py

# 5. Start the server
uvicorn app.main:app --port 8000 --reload
```

The API will be available at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs`

## Environment variables

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB connection string (Atlas or local) |
| `MONGO_DB` | Database name (e.g. `sdt_attendance`) |

Create a `.env` file in the project root. It is git-ignored and must never be committed.

## Running the tests

```bash
pytest tests/ -v
```

Tests run against a separate database (`sdt_attendance_test`) and wipe it clean before and after every test. MongoDB Atlas (M0 or higher, 6.0+) is required.

**Result: 155 / 155 tests pass.**

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Readiness probe — returns 200 only if MongoDB answers |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | List employees (filter by department, paginated) |
| `POST` | `/attendance/punch-in` | Record a punch-in |
| `POST` | `/attendance/punch-out` | Record a punch-out |
| `GET` | `/attendance` | List attendance records (filter + paginate) |
| `PATCH` | `/attendance/{emp_code}/{date}` | Manually correct a record (regularize) |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| `GET` | `/analytics/departments/summary` | Department summary for a month |
| `GET` | `/analytics/leaderboard/late` | Late-comers leaderboard |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend for a department |
| `GET` | `/admin/explain/{endpoint}` | MongoDB explain output for a named endpoint |

Full contract: [`openapi.yaml`](openapi.yaml)

## Key implementation decisions

**Timezone handling (R1).** All instants are stored as UTC-aware BSON datetimes and returned as epoch milliseconds. The IST calendar date for an attendance record is derived by converting the UTC punch-in to IST (`UTC+05:30`). Overnight shifts (e.g. 22:00–06:00) are detected by comparing `shift_end <= shift_start`; a punch-in whose IST time falls before `shift_end` is attributed to the previous calendar day.

**Rounding (R4/R8).** Python's built-in `round()` uses banker's rounding. All reported numbers use `Decimal.quantize(ROUND_HALF_UP)` instead.

**Grace period (R2).** The check is on raw elapsed seconds (`elapsed_sec > 600`), not on integer minutes, so 09:40:01 correctly yields `late_minutes = 10` while 09:40:00 yields 0.

**Concurrency.** Duplicate employee creates and duplicate punch-ins are prevented by unique MongoDB indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)`. The application never does a check-then-insert; it relies on the index and catches `DuplicateKeyError` → 409.

**Analytics.** Monthly summaries use MongoDB aggregation pipelines. The department trend fills date gaps in Python (one row per calendar day, including weekends) with a 7-day moving average computed over non-null attendance rates.

Full rationale: [`DECISIONS.md`](DECISIONS.md)  
Bug review of the starter code: [`REVIEW.md`](REVIEW.md)

## Project structure

```
app/
  main.py              # All application code (single-file per assignment spec)
tests/
  conftest.py          # Pytest fixtures and test DB setup
  test_helpers.py      # Unit tests for R1–R5 helper functions
  test_employees.py    # Employee create / list endpoints
  test_attendance.py   # Punch-in, punch-out, list, regularize
  test_analytics.py    # All analytics endpoints
  test_system.py       # Health and explain endpoints
sample_data/           # Nine sample documents (employees + logs)
sample_seed.py         # Loads sample_data/ into MongoDB
openapi.yaml           # Full API contract
DATA_MODEL.md          # MongoDB collection schema and sample documents
REVIEW.md              # Defects found and fixed in the starter code
DECISIONS.md           # Implementation decisions and trade-offs
requirements.txt       # Python dependencies
pytest.ini             # Pytest configuration
```

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (Atlas M0 free tier works)
- Dependencies: `fastapi`, `uvicorn[standard]`, `pymongo[srv]`, `pydantic`, `python-dotenv`
