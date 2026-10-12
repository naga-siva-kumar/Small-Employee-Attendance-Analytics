"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import math
import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any, Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    # Unique index on emp_code — enforces concurrent duplicate creates
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)

    # Unique compound index on (emp_code, date) — one record per employee per
    # day; prevents concurrent duplicate punch-ins
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )

    # Index for listing attendance: sort by date desc, emp_code asc
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )

    # Index for analytics that filter on date range first
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)]
    )

    # Index on employees.department for department-based look-ups
    db.employees.create_index([("department", ASCENDING)])

    # Index on employees.joined_on for headcount queries (R9)
    db.employees.create_index([("joined_on", ASCENDING)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int = 2) -> float:
    """Round using ROUND_HALF_UP (not Python's default banker's rounding)."""
    d = Decimal(str(value))
    if decimals == 0:
        result = d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    else:
        quant = Decimal("0." + "0" * decimals)
        result = d.quantize(quant, rounding=ROUND_HALF_UP)
    return float(result)


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate a datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime, truncated to whole seconds."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return truncate_to_seconds(dt)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to epoch milliseconds, or None if dt is None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Make a datetime timezone-aware (UTC) if naive; passthrough for None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def get_attendance_date(punch_in_utc: datetime, shift_start: str,
                        shift_end: str) -> str:
    """
    Determine the attendance date per R1.
    - Default: IST calendar date of punch_in.
    - Overnight shift (shift_end <= shift_start): if punch-in IST time is
      earlier than shift_end, the record belongs to the *previous* day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    att_date = punch_in_ist.date()

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    # Overnight shift?
    if eh < sh or (eh == sh and em <= sm):
        punch_time = punch_in_ist.hour * 60 + punch_in_ist.minute
        end_time = eh * 60 + em
        if punch_time < end_time:
            att_date = att_date - timedelta(days=1)

    return att_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """
    R2: Grace period is 10 min.  Late only if *strictly more* than 10:00
    after shift_start.  When late, late_minutes = floor(minutes since
    shift_start).  Otherwise 0.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))

    # Build shift-start on the same IST day
    start_ist = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_seconds = (punch_in_ist - start_ist).total_seconds()

    # Handle overnight wrap
    if diff_seconds < -12 * 3600:
        start_ist -= timedelta(days=1)
        diff_seconds = (punch_in_ist - start_ist).total_seconds()
    elif diff_seconds > 12 * 3600:
        start_ist += timedelta(days=1)
        diff_seconds = (punch_in_ist - start_ist).total_seconds()

    if diff_seconds <= 0:
        return 0

    # Grace: strictly more than 10 minutes
    if diff_seconds <= 600:
        return 0

    return int(diff_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out − punch_in) / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out_utc: datetime, shift_end: str,
                     att_date_str: str, shift_start: str) -> int:
    """
    R3: floor(minutes between shift_end and punch_out), counted only when
    >= 30; otherwise 0.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))

    att_date = date_type.fromisoformat(att_date_str)
    end_ist = datetime(att_date.year, att_date.month, att_date.day,
                       eh, em, 0, 0, tzinfo=IST)

    # Overnight shift: shift_end falls on the next calendar day
    if eh < sh or (eh == sh and em <= sm):
        end_ist += timedelta(days=1)

    diff_seconds = (punch_out_ist - end_ist).total_seconds()
    if diff_seconds < 0:
        return 0
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    return work_hours < 4.50


# ---- response formatters ------------------------------------------------- #

def format_employee(doc: dict) -> dict:
    """Format an employees document for the API."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def format_attendance_record(doc: dict) -> dict:
    """Format an attendance_logs document for the API."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _format_history(doc.get("history", [])),
    }


def _format_history(history: list) -> list:
    """Convert history BSON dates to epoch ms."""
    out = []
    for entry in history:
        formatted = {
            "at": datetime_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field, change in entry["changes"].items():
            f = change["from"]
            t = change["to"]
            if field in ("punch_in", "punch_out"):
                f = datetime_to_epoch_ms(f) if isinstance(f, datetime) else f
                t = datetime_to_epoch_ms(t) if isinstance(t, datetime) else t
            formatted["changes"][field] = {"from": f, "to": t}
        out.append(formatted)
    return out


def parse_month(month_str: str) -> tuple:
    """Parse 'YYYY-MM' → (year, month, first_day_str, last_day_str)."""
    m = re.match(r'^(\d{4})-(0[1-9]|1[0-2])$', month_str)
    if not m:
        raise HTTPException(422, detail="month must be YYYY-MM")
    year, month = int(m.group(1)), int(m.group(2))
    first = date_type(year, month, 1)
    last = date_type(year, month, calendar.monthrange(year, month)[1])
    return year, month, first.isoformat(), last.isoformat()


def count_working_days(start_str: str, end_str: str) -> int:
    """Count Mon–Fri days between two dates inclusive."""
    start = date_type.fromisoformat(start_str)
    end = date_type.fromisoformat(end_str)
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(strict=True)

    emp_code: str = Field(pattern=r'^EMP\d{4,6}$')
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field("18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _validate_joined_on(cls, v):
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInIn(BaseModel):
    model_config = ConfigDict(strict=True)

    emp_code: str
    punched_at: Optional[int] = Field(
        None, ge=100000000000, le=4102444800000
    )
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(strict=True)

    emp_code: str
    punched_at: Optional[int] = Field(
        None, ge=100000000000, le=4102444800000
    )


class RegularizeIn(BaseModel):
    model_config = ConfigDict(strict=True)

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = Field(
        None, ge=100000000000, le=4102444800000
    )
    punch_out: Optional[int] = Field(
        None, ge=100000000000, le=4102444800000
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# ---- Health --------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")
    return {"status": "ok"}


# ---- Employees ------------------------------------------------------------ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---- Punch In ------------------------------------------------------------ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(
            422, detail=f"status must be one of {sorted(PRESENCE_STATUSES)}"
        )

    ts = (epoch_ms_to_datetime(body.punched_at)
          if body.punched_at is not None
          else truncate_to_seconds(datetime.now(timezone.utc)))

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    att_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    doc.pop("_id", None)
    return format_attendance_record(doc)


# ---- Punch Out ----------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    ts = (epoch_ms_to_datetime(body.punched_at)
          if body.punched_at is not None
          else truncate_to_seconds(datetime.now(timezone.utc)))

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    # Most recent open record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        already = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": ts},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if already:
            raise HTTPException(409, detail="already punched out")
        raise HTTPException(404, detail="no open punch-in found")

    pi = _ensure_utc(record["punch_in"])

    if ts <= pi:
        raise HTTPException(422, detail="punch_out must be after punch_in")
    if (ts - pi).total_seconds() > 24 * 3600:
        raise HTTPException(
            422, detail="punch_out must be within 24 hours of punch_in"
        )

    wh = compute_work_hours(pi, ts)
    ot = compute_overtime(ts, emp["shift_end"], record["date"],
                          emp["shift_start"])
    hd = compute_half_day(wh)

    # Atomic CAS – prevents concurrent double punch-out
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
    )
    if result.modified_count == 0:
        raise HTTPException(409, detail="already punched out (concurrent)")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_record(updated)


# ---- List Attendance ------------------------------------------------------ #
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(
            422, detail=f"status must be one of {sorted(ALL_STATUSES)}"
        )

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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_attendance_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---- Regularize ----------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="no attendance record for this date")

    # ---- snapshot old values ----
    old_status = record["status"]
    old_pi = _ensure_utc(record.get("punch_in"))
    old_po = _ensure_utc(record.get("punch_out"))
    old_wh = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)

    # ---- resolve new values ----
    new_status = body.status if body.status is not None else old_status
    new_pi, new_po = old_pi, old_po

    if new_status not in ALL_STATUSES:
        raise HTTPException(
            422, detail=f"status must be one of {sorted(ALL_STATUSES)}"
        )

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                detail="cannot supply punch times with ABSENT/LEAVE status",
            )
        new_pi = None
        new_po = None
    else:
        if body.punch_in is not None:
            new_pi = epoch_ms_to_datetime(body.punch_in)
        if body.punch_out is not None:
            new_po = epoch_ms_to_datetime(body.punch_out)

        if new_pi is None:
            raise HTTPException(
                422, detail="presence status requires a punch_in"
            )

        # punch_in must stay on the record's attendance date (R1)
        if body.punch_in is not None:
            computed = get_attendance_date(
                new_pi, emp["shift_start"], emp["shift_end"]
            )
            if computed != date:
                raise HTTPException(
                    422,
                    detail="punch_in must fall on the record's attendance date",
                )

        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(
                    422, detail="punch_out must be after punch_in"
                )
            if (new_po - new_pi).total_seconds() > 24 * 3600:
                raise HTTPException(
                    422,
                    detail="punch_out must be within 24 hours of punch_in",
                )

    # ---- detect actual change ----
    status_changed = new_status != old_status
    pi_changed = new_pi != old_pi
    po_changed = new_po != old_po

    if not status_changed and not pi_changed and not po_changed:
        raise HTTPException(422, detail="no changes requested")

    # ---- recompute derived fields ----
    if new_status in ("ABSENT", "LEAVE"):
        new_late, new_wh, new_ot, new_hd = 0, None, 0, False
    else:
        new_late = compute_late_minutes(new_pi, emp["shift_start"])
        if new_po is not None:
            new_wh = compute_work_hours(new_pi, new_po)
            new_ot = compute_overtime(
                new_po, emp["shift_end"], date, emp["shift_start"]
            )
            new_hd = compute_half_day(new_wh)
        else:
            new_wh, new_ot, new_hd = None, 0, False

    # ---- build changes dict (only fields that actually changed) ----
    changes: dict = {}
    if status_changed:
        changes["status"] = {"from": old_status, "to": new_status}
    if pi_changed:
        changes["punch_in"] = {"from": old_pi, "to": new_pi}
    if po_changed:
        changes["punch_out"] = {"from": old_po, "to": new_po}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="no effective changes")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: match current history length so concurrent
    # edits don't silently overwrite each other.
    old_history = record.get("history", [])
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": {"$size": len(old_history)}},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi,
                "punch_out": new_po,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
    )
    if result.modified_count == 0:
        raise HTTPException(409, detail="concurrent edit detected, retry")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

# ---- Employee Monthly ----------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    _, _, first_day, last_day = parse_month(month)

    # R7: working days counted from joined_on if mid-month
    effective_start = max(first_day, emp["joined_on"])
    working_days = (
        count_working_days(effective_start, last_day)
        if effective_start <= last_day else 0
    )

    # MongoDB aggregation for attendance metrics
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": first_day, "$lte": last_day},
        }},
        {"$facet": {
            "present": [
                {"$match": {
                    "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]}
                }},
                {"$addFields": {
                    "day_value": {
                        "$cond": [
                            {"$ifNull": ["$half_day", False]},
                            0.5, 1.0,
                        ]
                    },
                    "is_weekday": {
                        "$in": [
                            {"$dayOfWeek": {"$dateFromString": {
                                "dateString": "$date",
                                "format": "%Y-%m-%d",
                            }}},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                }},
                {"$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": ["$is_weekday", "$day_value", 0]
                        }
                    },
                }},
            ],
            "leave": [
                {"$match": {"status": "LEAVE"}},
                {"$count": "count"},
            ],
            "late": [
                {"$match": {"late_minutes": {"$gt": 0}}},
                {"$group": {
                    "_id": None,
                    "count": {"$sum": 1},
                    "total": {"$sum": "$late_minutes"},
                }},
            ],
            "overtime": [
                {"$group": {
                    "_id": None,
                    "total": {"$sum": {
                        "$ifNull": ["$overtime_minutes", 0]
                    }},
                }},
            ],
        }},
    ]

    facets = list(db.attendance_logs.aggregate(pipeline))
    facets = facets[0] if facets else {}

    present_data = facets.get("present", [])
    present_days = present_data[0]["present_days"] if present_data else 0.0

    leave_data = facets.get("leave", [])
    leave_days = leave_data[0]["count"] if leave_data else 0

    late_data = facets.get("late", [])
    late_count = late_data[0]["count"] if late_data else 0
    total_late = late_data[0]["total"] if late_data else 0

    ot_data = facets.get("overtime", [])
    total_ot = ot_data[0]["total"] if ot_data else 0

    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


# ---- Department Summary -------------------------------------------------- #
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
):
    _, _, first_day, last_day = parse_month(month)

    # R9: headcount = employees whose joined_on <= last day of month
    emp_match: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    employees = list(db.employees.find(emp_match, {"_id": 0}))
    if not employees:
        return {"month": month, "items": []}

    # Group by department for headcount
    dept_emps: dict = {}
    for e in employees:
        dept_emps.setdefault(e["department"], []).append(e)

    relevant_codes = [e["emp_code"] for e in employees]

    # Aggregation for attendance metrics per department
    pipeline = [
        {"$match": {
            "date": {"$gte": first_day, "$lte": last_day},
            "emp_code": {"$in": relevant_codes},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp_info",
        }},
        {"$unwind": "$emp_info"},
        {"$addFields": {
            "is_weekday": {
                "$in": [
                    {"$dayOfWeek": {"$dateFromString": {
                        "dateString": "$date", "format": "%Y-%m-%d",
                    }}},
                    [2, 3, 4, 5, 6],
                ]
            },
        }},
        {"$group": {
            "_id": "$emp_info.department",
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        "$is_weekday",
                    ]},
                    {"$cond": [
                        {"$ifNull": ["$half_day", False]}, 0.5, 1.0
                    ]},
                    0,
                ]
            }},
            "work_hours_sum": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$work_hours", None]},
                    ]},
                    "$work_hours", 0,
                ]
            }},
            "work_hours_count": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$work_hours", None]},
                    ]},
                    1, 0,
                ]
            }},
            "late_count": {"$sum": {
                "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
            }},
            "total_late_minutes": {"$sum": {
                "$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]
            }},
            "leave_count": {"$sum": {
                "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
            }},
            "on_duty_count": {"$sum": {
                "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
            }},
        }},
        {"$sort": {"_id": 1}},
    ]

    agg = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    items = []
    for dept_name in sorted(dept_emps):
        hc = len(dept_emps[dept_name])
        if hc == 0:
            continue
        a = agg.get(dept_name, {})
        whc = a.get("work_hours_count", 0)
        items.append({
            "department": dept_name,
            "headcount": hc,
            "present_days": a.get("present_days", 0.0),
            "avg_work_hours": (
                round_half_up(a["work_hours_sum"] / whc, 2)
                if whc > 0 else None
            ),
            "late_count": a.get("late_count", 0),
            "total_late_minutes": a.get("total_late_minutes", 0),
            "leave_count": a.get("leave_count", 0),
            "on_duty_count": a.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


# ---- Late Leaderboard ---------------------------------------------------- #
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    _, _, first_day, last_day = parse_month(month)

    pipeline: list = [
        {"$match": {
            "date": {"$gte": first_day, "$lte": last_day},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp_info",
        }},
        {"$unwind": "$emp_info"},
    ]

    if department:
        pipeline.append(
            {"$match": {"emp_info.department": department}}
        )

    # Competition ranking: sortBy only on total_late_minutes so ties share rank
    pipeline += [
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp_info.name",
            "department": "$emp_info.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": results}


# ---- Department Trend ----------------------------------------------------- #
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="department not found")

    try:
        from_d = date_type.fromisoformat(date_from)
        to_d = date_type.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(422, detail="invalid date format")

    if to_d < from_d:
        raise HTTPException(422, detail="'to' must not be before 'from'")
    if (to_d - from_d).days > 92:
        raise HTTPException(422, detail="range must not exceed 92 days")

    # Fetch department employees (for headcount per day)
    dept_emps = list(db.employees.find(
        {"department": department},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    ))
    emp_codes = [e["emp_code"] for e in dept_emps]

    # Aggregate attendance data from MongoDB
    pipeline = [
        {"$match": {
            "date": {"$gte": from_d.isoformat(), "$lte": to_d.isoformat()},
            "emp_code": {"$in": emp_codes},
        }},
        {"$addFields": {
            "present_value": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                    0,
                ]
            },
            "is_late": {
                "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
            },
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": "$present_value"},
            "late_count": {"$sum": "$is_late"},
        }},
    ]

    day_data = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    # Build one row per calendar day, filling gaps
    items: list = []
    rates_window: list = []
    cur = from_d
    while cur <= to_d:
        ds = cur.isoformat()
        is_wd = cur.weekday() < 5
        hc = sum(1 for e in dept_emps if e["joined_on"] <= ds)

        d = day_data.get(ds, {})
        pc = d.get("present_count", 0)
        lc = d.get("late_count", 0)

        rate = (
            round_half_up(pc / hc, 4)
            if is_wd and hc > 0
            else None
        )
        rates_window.append(rate)

        # moving_avg_7d: mean of non-null rates in last 7 rows
        window = rates_window[-7:]
        nn = [r for r in window if r is not None]
        mavg = round_half_up(sum(nn) / len(nn), 4) if nn else None

        items.append({
            "date": ds,
            "is_working_day": is_wd,
            "headcount": hc,
            "present_count": pc,
            "late_count": lc,
            "attendance_rate": rate,
            "moving_avg_7d": mavg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin – explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[str] = None,
    query_from: Optional[str] = Query(None, alias="from"),
    query_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in valid:
        raise HTTPException(
            422, detail=f"endpoint must be one of {sorted(valid)}"
        )

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
        explain = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip).limit(page_size)
            .explain()
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required")
        if not month:
            raise HTTPException(422, detail="month is required")
        _, _, fd, ld = parse_month(month)
        pipeline = [{"$match": {
            "emp_code": emp_code,
            "date": {"$gte": fd, "$lte": ld},
        }}]
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        _, _, fd, ld = parse_month(month)
        match_stage: dict = {"date": {"$gte": fd, "$lte": ld}}
        pipeline = [
            {"$match": match_stage},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }},
            {"$unwind": "$emp_info"},
        ]
        if department:
            pipeline.append(
                {"$match": {"emp_info.department": department}}
            )
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        _, _, fd, ld = parse_month(month)
        pipeline = [
            {"$match": {
                "date": {"$gte": fd, "$lte": ld},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id": "$emp_code",
                "total": {"$sum": "$late_minutes"},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp_info",
            }},
            {"$unwind": "$emp_info"},
        ]
        if department:
            pipeline.append(
                {"$match": {"emp_info.department": department}}
            )
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    else:  # department_trend
        if not department:
            raise HTTPException(422, detail="department is required")
        if not query_from or not query_to:
            raise HTTPException(422, detail="from and to are required")
        codes = [
            e["emp_code"]
            for e in db.employees.find(
                {"department": department}, {"emp_code": 1, "_id": 0}
            )
        ]
        pipeline = [
            {"$match": {
                "date": {"$gte": query_from, "$lte": query_to},
                "emp_code": {"$in": codes},
            }},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }
