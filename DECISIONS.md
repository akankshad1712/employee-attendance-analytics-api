# DECISIONS.md

## 1. Indexes

I created five indexes at startup (idempotently, using `create_index` which is a no-op if the index already exists):

| Index | Collection | Purpose |
|---|---|---|
| `emp_code` (unique) | `employees` | Enforces uniqueness at the DB layer so two concurrent POST /employees with the same code always yield exactly one 201 and one 409, regardless of Python-level timing. Also speeds up all employee lookups by `emp_code`. |
| `department` | `employees` | Speeds up department-filter queries in `GET /employees` and the analytics aggregations that group by department. |
| `(emp_code, date)` unique | `attendance_logs` | Enforces the one-record-per-employee-per-day constraint atomically (concurrent punch-ins → exactly one succeeds). Covers the regularize `PATCH` lookup and the employee-monthly aggregation `$match`. |
| `(date DESC, emp_code ASC)` | `attendance_logs` | Matches the exact sort order required by `GET /attendance`, so MongoDB can satisfy the sort and the skip/limit using the index alone (IXSCAN, no in-memory sort). |
| `date ASC` | `attendance_logs` | Covers date-range filters used in all analytics aggregations and the department trend query. |

**Index I considered and rejected:** a partial index on `late_minutes > 0`. It would speed up the leaderboard's `{late_minutes: {$gt: 0}}` filter, but the leaderboard already hits a small filtered set via `emp_code` and `date` range, and a partial index adds storage cost for a low-frequency admin endpoint. I kept it simple.

---

## 2. Punch-in race condition

Two identical punch-in requests arrive at the same instant. Here is exactly what happens:

Both requests pass Pydantic validation and both call `logs_col.insert_one(doc)` with the same `emp_code` and `date`. MongoDB receives both writes. Because there is a **unique compound index on `(emp_code, date)`**, the database engine serialises the two writes at the storage layer. One write succeeds and creates the document. The second write fails immediately with a `DuplicateKeyError`. Python catches that error and the endpoint returns HTTP 409. No `find_one` check precedes the insert, so there is no time window in which both requests could observe an empty collection and both proceed.

---

## 3. Ties in the late leaderboard

Two employees tied on `total_late_minutes` at the cutoff of `limit`:

The aggregation pipeline uses MongoDB's `$setWindowFields` with `$rank`. `$rank` assigns the same rank to tied rows and skips the next rank (standard competition ranking: 1, 2, 2, 4). The final `$match` stage keeps every row whose `rank <= limit`. So if two employees are tied at rank `limit`, **both are included in the response** even though the result set has more than `limit` rows. This is explicitly required by the spec: *"The response can therefore contain more than `limit` rows when employees tie at a rank ≤ `limit`."*

---

## 4. Headcount for employees with zero logs

The department summary aggregation runs **two separate pipelines**:

1. Against `employees`: groups by `department`, counts headcount, collects `emp_codes`. The match filter is `joined_on <= last day of month`, so employees who have never logged a day are still counted.
2. Against `attendance_logs`: groups attendance statistics by department using a `$lookup` to resolve the department name.

The two results are merged in Python by department name. If a department has employees but zero log records, the log pipeline simply returns no row for that department, and the merge fills in zeroes for all numeric fields. This guarantees that headcount always reflects employees, never log volume.

---

## 5. One thing I would change at 100× the data

The department trend endpoint currently fetches all employee and log records for the department+range into Python memory and loops day-by-day. At 100× scale (millions of log records, hundreds of departments, large date ranges) this becomes a bottleneck. I would push the entire aggregation into MongoDB using `$group` by date, then use a `$densify` stage (available in MongoDB 5.1+) to fill gaps, followed by a `$setWindowFields` stage for the 7-day moving average — all computed server-side. Combined with a compound index on `(emp_code, date)` and a supporting index on `(department, date)` via a lookup join, this would keep memory flat and let MongoDB parallelise the work across shards.
