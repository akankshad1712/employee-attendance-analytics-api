"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from .env if present)
"""
import os
import re
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# Database connection
# Resolved at import time so the test suite can override MONGO_DB via env
# before importing this module.
# ---------------------------------------------------------------------------
_MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
_MONGO_DB  = os.getenv("MONGO_DB",  "attendance_db")

client = MongoClient(_MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db     = client[_MONGO_DB]

employees_col = db["employees"]
logs_col      = db["attendance_logs"]

# ---------------------------------------------------------------------------
# Indexes — created idempotently at startup
# ---------------------------------------------------------------------------
def _create_indexes() -> None:
    # employees: unique on emp_code (enforces concurrent-create guarantee)
    employees_col.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    employees_col.create_index([("department", ASCENDING)], name="dept")

    # attendance_logs: unique on (emp_code, date) (enforces concurrent punch-in guarantee)
    logs_col.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="emp_date_unique",
    )
    # for list_attendance sort: date DESC, emp_code ASC
    logs_col.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp_asc",
    )
    # analytics filters
    logs_col.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], name="emp_date")
    logs_col.create_index([("date", ASCENDING)], name="date_asc")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# EpochMillis validation bounds (from openapi.yaml EpochMillis schema)
_MS_MIN = 100_000_000_000   # ~1973
_MS_MAX = 4_102_444_800_000 # 2100

# ---------------------------------------------------------------------------
# Helpers: epoch ms ↔ datetime
# ---------------------------------------------------------------------------
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds → UTC-aware datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """UTC-aware datetime → epoch milliseconds, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)  # treat naive as UTC (matches PyMongo default)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Helpers: IST date and overnight-shift logic (R1)
# ---------------------------------------------------------------------------
def attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    Compute the IST attendance date for a punch-in instant (R1).

    Overnight shift: shift_end <= shift_start (e.g. 22:00–06:00).
    If punch-in IST time is earlier than shift_end, the shift started the
    previous IST calendar day.
    """
    dt_ist = ms_to_dt(punch_in_ms).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    overnight = (eh * 60 + em) <= (sh * 60 + sm)

    if overnight:
        punch_time_min = dt_ist.hour * 60 + dt_ist.minute
        end_min        = eh * 60 + em
        if punch_time_min < end_min:
            # This punch is before shift_end → it belongs to the previous day's shift
            dt_ist = dt_ist - timedelta(days=1)

    return dt_ist.date().isoformat()


# ---------------------------------------------------------------------------
# Helpers: business rules R2–R5
# ---------------------------------------------------------------------------
def _half_up(value: float, decimals: int = 2) -> float:
    """Round half-up (not banker's rounding) to `decimals` places."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in_ms: int, shift_start: str) -> int:
    """
    R2: late_minutes = floor minutes since shift_start, only when strictly
    more than 10 minutes (600 seconds) after shift_start.
    Grace = exactly 10:00 → 0.  10:00:01 → 10.
    Example (09:30 shift): 09:40:00 → 0; 09:40:01 → 10; 10:15:59 → 45.
    """
    dt_ist = ms_to_dt(punch_in_ms).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    shift_dt = dt_ist.replace(hour=sh, minute=sm, second=0, microsecond=0)
    elapsed_sec = int((dt_ist - shift_dt).total_seconds())
    if elapsed_sec <= 600:   # 600 s = exactly 10 min → not late
        return 0
    return elapsed_sec // 60


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4: (punch_out - punch_in) / 3600, rounded half-up to 2 dp."""
    seconds = (punch_out_ms - punch_in_ms) / 1000
    raw = seconds / 3600
    return _half_up(raw, 2)


def compute_overtime_minutes(punch_out_ms: int, shift_end: str, date_str: str) -> int:
    """
    R3: floor minutes between shift_end and punch_out, only when >= 30.
    shift_end is on the attendance date (or next day for overnight).
    """
    eh, em = map(int, shift_end.split(":"))
    # Build shift_end as a UTC-aware datetime
    base = datetime.fromisoformat(date_str)  # naive date → naive datetime
    shift_end_ist = datetime(
        base.year, base.month, base.day, eh, em, 0,
        tzinfo=IST
    )
    # For overnight shifts shift_end falls on the next calendar day
    # detect: if punch_out is more than a day after date_str start, shift end is next day
    punch_out_dt = ms_to_dt(punch_out_ms)
    date_start_utc = datetime(base.year, base.month, base.day, 0, 0, 0, tzinfo=IST).astimezone(UTC)
    if (punch_out_dt - date_start_utc).total_seconds() > 86400:
        shift_end_ist += timedelta(days=1)

    diff_sec  = int((punch_out_dt - shift_end_ist.astimezone(UTC)).total_seconds())
    diff_min  = diff_sec // 60
    return diff_min if diff_min >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


# ---------------------------------------------------------------------------
# Helpers: serialise a MongoDB attendance doc to API response dict
# ---------------------------------------------------------------------------
def _serialise_record(doc: dict) -> dict:
    """Convert BSON dates to epoch ms and strip _id for API response."""
    out: dict = {}
    out["emp_code"]          = doc["emp_code"]
    out["date"]              = doc["date"]
    out["status"]            = doc["status"]
    out["punch_in"]          = dt_to_ms(doc.get("punch_in"))
    out["punch_out"]         = dt_to_ms(doc.get("punch_out"))
    out["work_hours"]        = doc.get("work_hours")
    out["late_minutes"]      = doc.get("late_minutes", 0)
    out["overtime_minutes"]  = doc.get("overtime_minutes", 0)
    out["half_day"]          = doc.get("half_day", False)
    out["history"]           = _serialise_history(doc.get("history") or [])
    return out


def _serialise_history(history: list) -> list:
    result = []
    for entry in history:
        h: dict = {
            "at":     dt_to_ms(entry["at"]),
            "by":     entry["by"],
            "reason": entry["reason"],
            "changes": _serialise_changes(entry.get("changes", {})),
        }
        result.append(h)
    return result


def _serialise_changes(changes: dict) -> dict:
    """Convert punch_in / punch_out from/to values to epoch ms; others pass through."""
    out = {}
    for field, diff in changes.items():
        from_val = diff.get("from")
        to_val   = diff.get("to")
        if field in ("punch_in", "punch_out"):
            from_val = dt_to_ms(from_val)
            to_val   = dt_to_ms(to_val)
        out[field] = {"from": from_val, "to": to_val}
    return out


def _serialise_employee(doc: dict) -> dict:
    """Convert created_at BSON date to epoch ms and strip _id."""
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  dt_to_ms(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
_EPOCH_MS_FIELD_DESCRIPTION = (
    "Epoch milliseconds (integer, 100000000000 – 4102444800000)"
)

def _validate_epoch_ms(v: Any) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("must be an integer (epoch milliseconds)")
    if v < _MS_MIN or v > _MS_MAX:
        raise ValueError(
            f"epoch ms must be between {_MS_MIN} and {_MS_MAX}"
        )
    return v


class EmployeeCreate(BaseModel):
    emp_code:   str
    name:       str
    email:      str
    department: str
    shift_start: str = "09:30"
    shift_end:   str = "18:30"
    joined_on:   str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) or len(v) > 120:
            raise ValueError("invalid email")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shift_start_ne_shift_end(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"PRESENT", "WFH", "ON_DUTY"}
        if v not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return _validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status:         Optional[str] = None
    punch_in:       Optional[int] = None
    punch_out:      Optional[int] = None
    reason:         str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        allowed = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
        if v not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Create indexes at startup (idempotent)."""
    _create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@app.get("/health", tags=["system"])
def health():
    try:
        db.command("ping", serverSelectionTimeoutMS=3000)
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="Database unavailable")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=UTC)
    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return JSONResponse(status_code=201, content=_serialise_employee(doc))


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1,  ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    skip  = (page - 1) * page_size
    total = employees_col.count_documents(q)
    items = list(
        employees_col.find(q, {"_id": 0})
                     .sort("emp_code", ASCENDING)
                     .skip(skip)
                     .limit(page_size)
    )
    return {
        "items":     [_serialise_employee(e) for e in items],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@app.get("/employees/{emp_code}", tags=["employees"])
def get_employee(emp_code: str):
    emp = employees_col.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")
    return _serialise_employee(emp)


# ---------------------------------------------------------------------------
# Attendance — punch-in
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    # Resolve timestamp (default = now), truncate to whole seconds per R1
    now_ms     = int(datetime.now(tz=UTC).timestamp()) * 1000
    punched_ms = body.punched_at if body.punched_at is not None else now_ms
    # Truncate to whole seconds
    punched_ms = (punched_ms // 1000) * 1000

    emp = employees_col.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    date_str   = attendance_date(punched_ms, emp["shift_start"], emp["shift_end"])
    late_min   = compute_late_minutes(punched_ms, emp["shift_start"])
    punch_in_dt = ms_to_dt(punched_ms)

    doc = {
        "emp_code":          body.emp_code,
        "date":              date_str,
        "status":            body.status,
        "punch_in":          punch_in_dt,
        "punch_out":         None,
        "work_hours":        None,
        "late_minutes":      late_min,
        "overtime_minutes":  0,
        "half_day":          False,
        "history":           [],
    }
    try:
        logs_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")

    return JSONResponse(status_code=201, content=_serialise_record(doc))


# ---------------------------------------------------------------------------
# Attendance — punch-out
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    now_ms      = int(datetime.now(tz=UTC).timestamp()) * 1000
    punched_ms  = body.punched_at if body.punched_at is not None else now_ms
    punched_ms  = (punched_ms // 1000) * 1000
    punch_out_dt = ms_to_dt(punched_ms)

    emp = employees_col.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    # Find the most recent open record (regardless of time — validate timing next)
    most_recent_open = logs_col.find_one(
        {"emp_code": body.emp_code, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )

    if most_recent_open is None:
        # No open record at all — check if there's a closed record to give a better error
        most_recent_any = logs_col.find_one(
            {"emp_code": body.emp_code},
            sort=[("punch_in", DESCENDING)],
        )
        if most_recent_any is not None and most_recent_any.get("punch_out") is not None:
            raise HTTPException(409, detail="Record already punched out")
        raise HTTPException(404, detail="No open punch-in found for this employee")

    punch_in_ms = dt_to_ms(most_recent_open["punch_in"])

    # Validate: punch_out must be after punch_in and within 24 hours
    if punched_ms <= punch_in_ms:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (punched_ms - punch_in_ms) > 86_400_000:
        raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    record = most_recent_open

    wh  = compute_work_hours(punch_in_ms, punched_ms)
    ot  = compute_overtime_minutes(punched_ms, emp["shift_end"], record["date"])
    hd  = compute_half_day(wh)

    # Atomic update: only update if punch_out is still None (handles concurrent punch-outs)
    result = logs_col.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out":        punch_out_dt,
            "work_hours":       wh,
            "overtime_minutes": ot,
            "half_day":         hd,
        }},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Record already punched out")

    return _serialise_record(result)


# ---------------------------------------------------------------------------
# Attendance — list
# ---------------------------------------------------------------------------
@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1,  ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate date formats
    for d, name in ((date_from, "date_from"), (date_to, "date_to")):
        if d is not None:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(422, detail=f"{name} must be YYYY-MM-DD")

    # Validate date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")

    # Validate status
    if status is not None:
        allowed = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
        if status not in allowed:
            raise HTTPException(422, detail=f"status must be one of {sorted(allowed)}")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip  = (page - 1) * page_size
    total = logs_col.count_documents(q)
    docs  = list(
        logs_col.find(q, {"_id": 0})
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip(skip)
                .limit(page_size)
    )
    return {
        "items":     [_serialise_record(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance — regularize (PATCH)
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    # Validate date path param
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, detail="date must be YYYY-MM-DD")

    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    record = logs_col.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, detail="No attendance record for that date")

    new_status    = body.status    if body.status    is not None else record["status"]
    cur_punch_in  = dt_to_ms(record.get("punch_in"))
    cur_punch_out = dt_to_ms(record.get("punch_out"))
    new_punch_in  = body.punch_in  if body.punch_in  is not None else cur_punch_in
    new_punch_out = body.punch_out if body.punch_out is not None else cur_punch_out

    # --- Validation ---
    absence_statuses = {"ABSENT", "LEAVE"}

    # ABSENT/LEAVE must have no punch times
    if new_status in absence_statuses:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                detail="Cannot supply punch_in or punch_out when setting ABSENT or LEAVE",
            )
        new_punch_in  = None
        new_punch_out = None

    # Presence status requires a punch_in
    if new_status not in absence_statuses and new_punch_in is None:
        raise HTTPException(422, detail="Presence status requires a punch_in")

    # punch_in must stay on the attendance date (R1)
    if new_punch_in is not None:
        pi_date = ms_to_dt(new_punch_in).astimezone(IST).date().isoformat()
        # For overnight shifts the punch-in IST time may be previous day
        expected = attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if expected != date:
            raise HTTPException(
                422,
                detail="punch_in does not fall on the record's attendance date",
            )

    # punch_out must be after punch_in and within 24 hours
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(422, detail="punch_out requires punch_in")
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, detail="punch_out must be after punch_in")
        if (new_punch_out - new_punch_in) > 86_400_000:
            raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status not in absence_statuses and new_punch_in is not None:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"])
    else:
        new_late = 0

    if new_punch_in is not None and new_punch_out is not None:
        new_wh = compute_work_hours(new_punch_in, new_punch_out)
        new_ot = compute_overtime_minutes(new_punch_out, emp["shift_end"], date)
        new_hd = compute_half_day(new_wh)
    else:
        new_wh = None
        new_ot = 0
        new_hd = False

    # --- Build the changes dict (only fields that actually changed) ---
    changes: dict = {}

    old_status    = record["status"]
    old_punch_in  = dt_to_ms(record.get("punch_in"))
    old_punch_out = dt_to_ms(record.get("punch_out"))
    old_wh        = record.get("work_hours")
    old_late      = record.get("late_minutes", 0)
    old_ot        = record.get("overtime_minutes", 0)
    old_hd        = record.get("half_day", False)

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="Request changes nothing")

    # Build history entry (stored as BSON dates for punch_in/punch_out values)
    def _ms_or_none_to_dt(ms: Optional[int]) -> Optional[datetime]:
        return ms_to_dt(ms) if ms is not None else None

    bson_changes: dict = {}
    for field, diff in changes.items():
        if field in ("punch_in", "punch_out"):
            bson_changes[field] = {
                "from": _ms_or_none_to_dt(diff["from"]),
                "to":   _ms_or_none_to_dt(diff["to"]),
            }
        else:
            bson_changes[field] = diff

    history_entry = {
        "at":      datetime.now(tz=UTC),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": bson_changes,
    }

    # Convert new punch times back to datetime for MongoDB
    new_punch_in_dt  = _ms_or_none_to_dt(new_punch_in)
    new_punch_out_dt = _ms_or_none_to_dt(new_punch_out)

    # Atomic update — optimistic concurrency on history length to prevent silent loss
    current_history_len = len(record.get("history") or [])
    result = logs_col.find_one_and_update(
        {
            "emp_code": emp_code,
            "date":     date,
            "$expr":    {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status":           new_status,
                "punch_in":         new_punch_in_dt,
                "punch_out":        new_punch_out_dt,
                "work_hours":       new_wh,
                "late_minutes":     new_late,
                "overtime_minutes": new_ot,
                "half_day":         new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent edit detected; please retry")

    return _serialise_record(result)


# ---------------------------------------------------------------------------
# Analytics — employee monthly summary
# ---------------------------------------------------------------------------
def _month_date_range(month: str):
    """Return (first_date_str, last_date_str) for a YYYY-MM string."""
    year, mon = int(month[:4]), int(month[5:])
    first = date_type(year, mon, 1)
    # last day of month
    if mon == 12:
        last = date_type(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date_type(year, mon + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _count_working_days(from_date: str, to_date: str, joined_on: str) -> int:
    """
    R7: Mon-Fri working days between max(from_date, joined_on) and to_date, inclusive.
    """
    start = max(
        datetime.strptime(from_date, "%Y-%m-%d").date(),
        datetime.strptime(joined_on, "%Y-%m-%d").date(),
    )
    end = datetime.strptime(to_date, "%Y-%m-%d").date()
    if start > end:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        current += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    first_str, last_str = _month_date_range(month)
    working_days = _count_working_days(first_str, last_str, emp["joined_on"])

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date":     {"$gte": first_str, "$lte": last_str},
        }},
        {"$addFields": {
            "is_weekday": {
                "$let": {
                    "vars": {
                        "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                    },
                    "in": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]}
                }
            },
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "safe_half_day": {"$ifNull": ["$half_day", False]},
            "safe_wh":       {"$ifNull": ["$work_hours", None]},
        }},
        {"$group": {
            "_id":                  None,
            "present_days":         {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_present", "$is_weekday"]},
                        {"$cond": ["$safe_half_day", 0.5, 1.0]},
                        0,
                    ]
                }
            },
            "leave_days":           {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":           {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes":   {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(logs_col.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days         = r["present_days"]
        leave_days           = r["leave_days"]
        late_count           = r["late_count"]
        total_late_minutes   = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
    else:
        present_days = leave_days = late_count = total_late_minutes = total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = _half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


# ---------------------------------------------------------------------------
# Analytics — department summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_str, last_str = _month_date_range(month)

    # Headcount per department: employees with joined_on <= last day of month (R9)
    emp_match: dict = {"joined_on": {"$lte": last_str}}
    if department:
        emp_match["department"] = department

    # Step 1: aggregate headcount and list of emp_codes per department from employees
    headcount_pipeline = [
        {"$match": emp_match},
        {"$group": {
            "_id":      "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": ASCENDING}},
    ]
    dept_rows = list(employees_col.aggregate(headcount_pipeline))
    if not dept_rows:
        return {"month": month, "items": []}

    dept_map = {r["_id"]: r for r in dept_rows}
    all_emp_codes = [ec for r in dept_rows for ec in r["emp_codes"]]

    # Step 2: aggregate attendance stats for those employees in the month
    logs_pipeline = [
        {"$match": {
            "emp_code": {"$in": all_emp_codes},
            "date":     {"$gte": first_str, "$lte": last_str},
        }},
        {"$addFields": {
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "is_weekday": {
                "$let": {
                    "vars": {
                        "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                    },
                    "in": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]}
                }
            },
            "safe_half_day": {"$ifNull": ["$half_day", False]},
        }},
        # Lookup employee to get department
        {"$lookup": {
            "from":         "employees",
            "localField":   "emp_code",
            "foreignField": "emp_code",
            "as":           "emp_info",
        }},
        {"$unwind": "$emp_info"},
        {"$group": {
            "_id": "$emp_info.department",
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_present", "$is_weekday"]},
                        {"$cond": ["$safe_half_day", 0.5, 1.0]},
                        0,
                    ]
                }
            },
            "avg_work_hours_sum":   {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                        "$work_hours",
                        0,
                    ]
                }
            },
            "avg_work_hours_count": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_present", {"$ne": ["$work_hours", None]}]},
                        1,
                        0,
                    ]
                }
            },
            "late_count":           {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes":   {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count":          {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count":        {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    log_rows = {r["_id"]: r for r in logs_col.aggregate(logs_pipeline)}

    items = []
    for dept_name in sorted(dept_map.keys()):
        hc  = dept_map[dept_name]["headcount"]
        lr  = log_rows.get(dept_name, {})
        avg_wh_sum   = lr.get("avg_work_hours_sum",   0)
        avg_wh_count = lr.get("avg_work_hours_count", 0)
        avg_wh = _half_up(avg_wh_sum / avg_wh_count, 2) if avg_wh_count > 0 else None
        items.append({
            "department":          dept_name,
            "headcount":           hc,
            "present_days":        lr.get("present_days",        0),
            "avg_work_hours":      avg_wh,
            "late_count":          lr.get("late_count",          0),
            "total_late_minutes":  lr.get("total_late_minutes",  0),
            "leave_count":         lr.get("leave_count",         0),
            "on_duty_count":       lr.get("on_duty_count",       0),
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics — late leaderboard
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int           = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_str, last_str = _month_date_range(month)

    # Determine emp_codes to consider (optionally filtered by department)
    emp_match: dict = {}
    if department:
        emp_match["department"] = department
    emp_codes_in_dept = [
        e["emp_code"] for e in employees_col.find(emp_match, {"emp_code": 1, "_id": 0})
    ]

    log_match: dict = {
        "date":              {"$gte": first_str, "$lte": last_str},
        "late_minutes":      {"$gt": 0},
        "emp_code":          {"$in": emp_codes_in_dept},
    }

    pipeline = [
        {"$match": log_match},
        {"$group": {
            "_id":                 "$emp_code",
            "total_late_minutes":  {"$sum": "$late_minutes"},
            "late_count":          {"$sum": 1},
        }},
        # Join employee info
        {"$lookup": {
            "from":         "employees",
            "localField":   "_id",
            "foreignField": "emp_code",
            "as":           "emp",
        }},
        {"$unwind": "$emp"},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        # Standard competition ranking
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {
                "rank": {
                    "$rank": {}
                }
            }
        }},
        # Apply limit: keep rows where rank <= limit
        {"$match": {"rank": {"$lte": limit}}},
        # Final sort: total_late_minutes DESC, emp_code ASC
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    ]

    rows = list(logs_col.aggregate(pipeline))
    items = [
        {
            "rank":                r["rank"],
            "emp_code":            r["_id"],
            "name":                r["emp"]["name"],
            "department":          r["emp"]["department"],
            "total_late_minutes":  r["total_late_minutes"],
            "late_count":          r["late_count"],
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics — department daily trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    # Validate dates
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt   = datetime.strptime(to_date,   "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, detail="from and to must be YYYY-MM-DD")

    if to_dt < from_dt:
        raise HTTPException(422, detail="to must not be before from")
    if (to_dt - from_dt).days > 91:
        raise HTTPException(422, detail="Date range must not exceed 92 days")

    # Verify department exists
    if not employees_col.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")

    # Build daily data in Python using DB queries
    # (MongoDB $densify requires 6.0+; we use Python gap-filling for reliability)
    # Fetch all employees in department with their joined_on
    dept_emps = list(employees_col.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1, "_id": 0},
    ))
    emp_codes = [e["emp_code"] for e in dept_emps]

    # Fetch all attendance logs for these employees in the range
    logs = list(logs_col.find(
        {
            "emp_code": {"$in": emp_codes},
            "date":     {"$gte": from_date, "$lte": to_date},
        },
        {"emp_code": 1, "date": 1, "status": 1, "late_minutes": 1, "half_day": 1, "_id": 0},
    ))

    # Index logs by date
    from collections import defaultdict
    logs_by_date: dict = defaultdict(list)
    for log in logs:
        logs_by_date[log["date"]].append(log)

    items = []
    all_attendance_rates: list = []  # for moving average

    current = from_dt
    while current <= to_dt:
        date_str    = current.isoformat()
        is_weekday  = current.weekday() < 5

        # Headcount: employees with joined_on <= current date (R9)
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= date_str)

        day_logs    = logs_by_date.get(date_str, [])
        present_count = 0.0
        late_count    = 0
        for log in day_logs:
            if log["status"] in ("PRESENT", "WFH", "ON_DUTY"):
                hd = log.get("half_day", False)
                present_count += 0.5 if hd else 1.0
            if (log.get("late_minutes") or 0) > 0:
                late_count += 1

        # attendance_rate: only on working days with headcount > 0
        if is_weekday and headcount > 0:
            attendance_rate = _half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        all_attendance_rates.append(attendance_rate)

        # moving_avg_7d: mean of non-null rates in current + up to 6 preceding rows
        window_start = max(0, len(all_attendance_rates) - 7)
        window       = [r for r in all_attendance_rates[window_start:] if r is not None]
        if window:
            raw_avg      = sum(window) / len(window)
            moving_avg   = _half_up(raw_avg, 4)
        else:
            moving_avg = None

        items.append({
            "date":           date_str,
            "is_working_day": is_weekday,
            "headcount":      headcount,
            "present_count":  present_count,
            "late_count":     late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d":  moving_avg,
        })
        current += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin — explain
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint:  str,
    emp_code:  Optional[str] = None,
    month:     Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:     int            = Query(default=10, ge=1, le=50),
    date_from: Optional[str]  = None,
    date_to:   Optional[str]  = None,
    status:    Optional[str]  = None,
    from_date: Optional[str]  = Query(default=None, alias="from"),
    to_date:   Optional[str]  = Query(default=None, alias="to"),
    page:      int            = Query(default=1,  ge=1),
    page_size: int            = Query(default=20, ge=1, le=100),
):
    valid_endpoints = {
        "attendance_list", "employee_monthly",
        "department_summary", "late_leaderboard", "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(valid_endpoints)}")

    explain_doc = None
    collection  = None

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find":       "attendance_logs",
                "filter":     q,
                "sort":       {"date": DESCENDING, "emp_code": ASCENDING},
                "skip":       skip,
                "limit":      page_size,
            },
            "verbosity": "executionStats",
        }
        explain_doc = db.command(cmd)
        collection  = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, detail="month is required for employee_monthly")
        first_str, last_str = _month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}}},
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline":  pipeline,
                "cursor":    {},
            },
            "verbosity": "executionStats",
        }
        explain_doc = db.command(cmd)
        collection  = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        first_str, last_str = _month_date_range(month)
        emp_match_d: dict = {"joined_on": {"$lte": last_str}}
        if department:
            emp_match_d["department"] = department
        pipeline = [{"$match": emp_match_d}]
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline":  pipeline,
                "cursor":    {},
            },
            "verbosity": "executionStats",
        }
        explain_doc = db.command(cmd)
        collection  = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        first_str, last_str = _month_date_range(month)
        emp_match_d = {}
        if department:
            emp_match_d["department"] = department
        emp_codes_in_dept = [
            e["emp_code"] for e in employees_col.find(emp_match_d, {"emp_code": 1, "_id": 0})
        ]
        log_match_d: dict = {
            "date":         {"$gte": first_str, "$lte": last_str},
            "late_minutes": {"$gt": 0},
            "emp_code":     {"$in": emp_codes_in_dept},
        }
        pipeline = [{"$match": log_match_d}]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline":  pipeline,
                "cursor":    {},
            },
            "verbosity": "executionStats",
        }
        explain_doc = db.command(cmd)
        collection  = "attendance_logs"

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(422, detail="from and to are required for department_trend")
        emp_codes_in_dept = [
            e["emp_code"] for e in employees_col.find(
                {"department": department}, {"emp_code": 1, "_id": 0}
            )
        ]
        log_match_d = {
            "emp_code": {"$in": emp_codes_in_dept},
            "date":     {"$gte": from_date, "$lte": to_date},
        }
        pipeline = [{"$match": log_match_d}]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline":  pipeline,
                "cursor":    {},
            },
            "verbosity": "executionStats",
        }
        explain_doc = db.command(cmd)
        collection  = "attendance_logs"

    # Strip non-serialisable BSON from the explain output
    def _sanitise(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _sanitise(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitise(v) for v in obj]
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "__str__") and not isinstance(obj, (int, float, bool, str)):
            return str(obj)
        return obj

    return {
        "endpoint":   endpoint,
        "collection": collection,
        "explain":    _sanitise(explain_doc),
    }
