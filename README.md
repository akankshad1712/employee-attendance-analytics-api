# Employee Attendance & Analytics API

> **HROne · Software Development Trainee Assignment**  
> Candidate: Akanksha Deshmukh &nbsp;|&nbsp; Deadline: 15 October 2026, 11:59 PM IST

A production-style REST API for employee attendance tracking and HR analytics.  
Built with **FastAPI**, **PyMongo**, and **MongoDB Atlas**.

---

## Table of contents

1. [Tech stack](#tech-stack)
2. [Local setup](#local-setup)
3. [Environment variables](#environment-variables)
4. [Running the server](#running-the-server)
5. [Seeding sample data](#seeding-sample-data)
6. [Running the tests](#running-the-tests)
7. [API endpoints](#api-endpoints)
8. [Business rules implemented](#business-rules-implemented)
9. [Project structure](#project-structure)
10. [Design decisions](#design-decisions)

---

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI 0.110+ |
| ASGI server | Uvicorn |
| Database | MongoDB Atlas 6.0+ |
| Database driver | PyMongo 4.7+ (synchronous) |
| Validation | Pydantic v2 |
| Configuration | python-dotenv |
| Testing | pytest + Starlette TestClient |

---

## Local setup

```bash
# 1. Clone the repository
git clone https://github.com/akankshad1712/employee-attendance-analytics-api.git
cd employee-attendance-analytics-api

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables  (see next section)
# 5. Start the server  (see Running the server)
```

---

## Environment variables

Create a `.env` file in the project root — it is **git-ignored and must never be committed**.

```env
MONGO_URI=<your MongoDB Atlas connection string>
MONGO_DB=sdt_attendance
```

| Variable | Required | Description |
|---|---|---|
| `MONGO_URI` | ✅ | MongoDB connection string (Atlas SRV or direct) |
| `MONGO_DB` | ✅ | Database name |

MongoDB **6.0 or newer** is required. Atlas free tier (M0) works.

---

## Running the server

```bash
uvicorn app.main:app --port 8000 --reload
```

| URL | Description |
|---|---|
| `http://localhost:8000` | Base API |
| `http://localhost:8000/docs` | Interactive Swagger UI |
| `http://localhost:8000/redoc` | ReDoc documentation |
| `http://localhost:8000/health` | Readiness probe |

---

## Seeding sample data

```bash
python sample_seed.py
```

Loads 6 employees and 9 attendance records from `sample_data/` into MongoDB.  
The grader seeds a much larger hidden dataset — passing on sample data does not guarantee analytics correctness.

---

## Running the tests

```bash
pytest tests/ -v
```

Tests run against a dedicated database (`sdt_attendance_test`) and wipe it clean before and after every test. MongoDB Atlas is required.

```
155 passed, 0 failed  ✅
```

---

## API endpoints

### System

| Method | Path | Status codes |
|---|---|---|
| `GET` | `/health` | 200 · 503 |

### Employees

| Method | Path | Description | Status codes |
|---|---|---|---|
| `POST` | `/employees` | Create an employee | 201 · 409 · 422 |
| `GET` | `/employees` | List employees (filter by department, paginated) | 200 · 422 |

### Attendance

| Method | Path | Description | Status codes |
|---|---|---|---|
| `POST` | `/attendance/punch-in` | Record a punch-in | 201 · 404 · 409 · 422 |
| `POST` | `/attendance/punch-out` | Record a punch-out | 200 · 404 · 409 · 422 |
| `GET` | `/attendance` | List records (filter + paginate) | 200 · 422 |
| `PATCH` | `/attendance/{emp_code}/{date}` | Manually correct a record | 200 · 404 · 409 · 422 |

### Analytics

| Method | Path | Description | Status codes |
|---|---|---|---|
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee | 200 · 404 · 422 |
| `GET` | `/analytics/departments/summary` | Department summary for a month | 200 · 422 |
| `GET` | `/analytics/leaderboard/late` | Late-comers leaderboard (competition ranking) | 200 · 422 |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend + 7-day moving average | 200 · 404 · 422 |

### Admin

| Method | Path | Description | Status codes |
|---|---|---|---|
| `GET` | `/admin/explain/{endpoint}` | MongoDB `explain` output for a named endpoint | 200 · 422 |

Full HTTP contract: [`openapi.yaml`](openapi.yaml)

---

## Business rules implemented

| Rule | Description |
|---|---|
| **R1** | Attendance date is the IST calendar date of `punch_in`. Overnight shifts (`shift_end ≤ shift_start`) attribute an early-morning punch to the previous day. |
| **R2** | Grace period is exactly 10 minutes. `late_minutes` = floor seconds since shift start ÷ 60, only when elapsed > 600 s. |
| **R3** | `overtime_minutes` = floor minutes beyond `shift_end`, counted only when ≥ 30. |
| **R4** | `work_hours` = (punch-out − punch-in) ÷ 3600, rounded **half-up** to 2 decimal places. |
| **R5** | `half_day` = true when rounded `work_hours` < 4.50. Counts as 0.5 of a present day. |
| **R6** | `PRESENT`, `WFH`, `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not. |
| **R7** | Working days = Mon–Fri from `joined_on` through end of month. |
| **R8** | All reported numbers rounded half-up (2 d.p.; rates 4 d.p.). |
| **R9** | Headcount = employees with `joined_on` ≤ last day of the period. |
| **R10** | Pagination: `page` starts at 1, `page_size` max 100, default 20. |

---

## Project structure

```
app/
  main.py              # All application code (single-file per spec)
tests/
  conftest.py          # Pytest fixtures and Atlas test-DB setup
  test_helpers.py      # Unit tests for R1–R5 helper functions   (30 tests)
  test_employees.py    # Employee create / list endpoints         (25 tests)
  test_attendance.py   # Punch-in, punch-out, list, regularize    (65 tests)
  test_analytics.py    # All four analytics endpoints             (25 tests)
  test_system.py       # Health and explain endpoints             (15 tests)
sample_data/
  employees.json       # 6 sample employee documents
  attendance_logs.json # 9 sample attendance records
sample_seed.py         # Loads sample_data/ into MongoDB
openapi.yaml           # Full OpenAPI 3.0 contract (source of truth)
DATA_MODEL.md          # MongoDB collection schema
REVIEW.md              # 21 defects found and fixed in the starter code
DECISIONS.md           # Implementation decisions and trade-offs
requirements.txt       # Python dependencies
pytest.ini             # Pytest configuration
.gitignore             # Excludes .env, .venv, __pycache__, etc.
```

---

## Design decisions

**No check-then-insert for concurrency.**  
Duplicate employees and duplicate punch-ins are prevented by unique MongoDB indexes (`employees.emp_code`, `attendance_logs.(emp_code, date)`). The API catches `DuplicateKeyError` and returns 409 — no race window.

**Stored derived fields are authoritative.**  
`work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` are written at punch-in/out and re-written after corrections. Analytics read the stored values instead of recomputing from raw timestamps.

**Half-up rounding via `Decimal`.**  
Python's built-in `round()` uses banker's rounding (round-half-to-even). `Decimal.quantize(ROUND_HALF_UP)` is used everywhere so `2.225` → `2.23` as specified.

**Correction audit trail.**  
Each `PATCH` appends exactly one `history` entry. Concurrent corrections are serialised by an optimistic-concurrency check on the history array length — the loser gets 409.

**Index-backed aggregations.**  
All analytics queries are covered by compound indexes. The `/admin/explain/{endpoint}` route confirms IXSCAN (no COLLSCAN) on a live dataset.

For full rationale see [`DECISIONS.md`](DECISIONS.md).  
For a detailed bug review of the starter code see [`REVIEW.md`](REVIEW.md).
