from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from typing import Optional
from database import get_db
from security import verify_password, create_access_token
from utils import calculate_distance, estimate_eta_minutes
import math

router = APIRouter()


# ─────────────────────────────────────────
# Models
# ─────────────────────────────────────────

class TechLoginBody(BaseModel):
    mobile:    str
    password:  str
    latitude:  Optional[float] = None
    longitude: Optional[float] = None

class LocationUpdateBody(BaseModel):
    tech_id:   int
    latitude:  float
    longitude: float
    speed_kmh: Optional[float] = None

class StatusUpdateBody(BaseModel):
    tech_id: int
    status:  str  # available | busy | offline | on_leave

class TechLogoutBody(BaseModel):
    tech_id: int

class CreateTechBody(BaseModel):
    name:         str
    mobile:       str
    password:     str
    region:       str
    capacity:     int = 4
    vehicle_no:   Optional[str] = None
    skill_tags:   Optional[str] = None
    joining_date: Optional[str] = None


# ─────────────────────────────────────────
# POST /technician_login
# ─────────────────────────────────────────

@router.post("/technician_login")
def technician_login(data: TechLoginBody):
    with get_db() as (conn, cursor):

        cursor.execute("""
            SELECT tech_id, name, region, status,
                   COALESCE(password_hash, ''),
                   COALESCE(is_active, TRUE),
                   COALESCE(password, '')
            FROM technicians WHERE mobile=%s
        """, (data.mobile,))
        tech = cursor.fetchone()

    if not tech:
        raise HTTPException(401, "Mobile number not found")
    if not tech[5]:
        raise HTTPException(403, "Account inactive — contact admin")

    # Verify password — check password_hash (bcrypt) first, fallback to plain password column
    stored_hash  = tech[4] or ""   # password_hash column
    stored_plain = tech[6] or ""   # password column (legacy)

    authenticated = False
    if stored_hash.startswith("$2"):
        # bcrypt hash
        authenticated = verify_password(data.password, stored_hash)
    elif stored_hash:
        # plain text in hash column
        authenticated = (data.password == stored_hash)

    if not authenticated and stored_plain:
        # fallback to legacy password column
        authenticated = (data.password == stored_plain)

    if not authenticated:
        raise HTTPException(401, "Incorrect password")

    with get_db() as (conn, cursor):
        # Mark online, set location if provided
        cursor.execute("""
            UPDATE technicians
            SET is_online=TRUE, status='available', login_at=NOW(),
                latitude=COALESCE(%s, latitude),
                longitude=COALESCE(%s, longitude),
                last_location_at=CASE WHEN %s IS NOT NULL THEN NOW() ELSE last_location_at END
            WHERE tech_id=%s
        """, (data.latitude, data.longitude, data.latitude, tech[0]))

        # Create session
        cursor.execute("""
            INSERT INTO tech_sessions (tech_id, login_at, login_lat, login_lng)
            VALUES (%s, NOW(), %s, %s) RETURNING id
        """, (tech[0], data.latitude, data.longitude))
        session_id = cursor.fetchone()[0]

        # Location history
        if data.latitude:
            cursor.execute("""
                INSERT INTO tech_location_history (tech_id, latitude, longitude)
                VALUES (%s, %s, %s)
            """, (tech[0], data.latitude, data.longitude))

    token = create_access_token({
        "sub":        str(tech[0]),
        "name":       tech[1],
        "region":     tech[2],
        "role":       "technician",
        "session_id": session_id,
    })

    return {
        "tech_id":    tech[0],
        "name":       tech[1],
        "region":     tech[2],
        "status":     "available",
        "session_id": session_id,
        "token":      token,
        "location_required": True,  # tells app to start sending GPS
    }


# ─────────────────────────────────────────
# POST /technician_logout
# ─────────────────────────────────────────

@router.post("/technician_logout")
def technician_logout(data: TechLogoutBody):
    with get_db() as (conn, cursor):

        # Close session + compute duration
        cursor.execute("""
            UPDATE tech_sessions
            SET logout_at=NOW(),
                duration_min=EXTRACT(EPOCH FROM (NOW()-login_at))/60::integer
            WHERE tech_id=%s AND logout_at IS NULL
        """, (data.tech_id,))

        # Mark offline — KEEP last known location for map history
        cursor.execute("""
            UPDATE technicians
            SET is_online=FALSE, status='offline', logout_at=NOW()
            WHERE tech_id=%s
        """, (data.tech_id,))

    return {"status": "logged out", "location_cleared": False}


# ─────────────────────────────────────────
# POST /update_technician_location
# ─────────────────────────────────────────

@router.post("/update_technician_location")
def update_technician_location(data: LocationUpdateBody):
    with get_db() as (conn, cursor):

        # Get previous location to calculate distance travelled
        cursor.execute("""
            SELECT latitude, longitude FROM technicians WHERE tech_id=%s
        """, (data.tech_id,))
        prev = cursor.fetchone()

        dist_increment = 0.0
        if prev and prev[0] and prev[1]:
            dist_increment = calculate_distance(
                float(prev[0]), float(prev[1]),
                data.latitude, data.longitude
            )
            # Only count if moved more than 50m (filter GPS noise)
            if dist_increment < 0.05:
                dist_increment = 0.0

        # Update current location
        cursor.execute("""
            UPDATE technicians
            SET latitude=%s, longitude=%s, last_location_at=NOW(),
                total_distance_km = COALESCE(total_distance_km,0) + %s
            WHERE tech_id=%s
        """, (data.latitude, data.longitude, dist_increment, data.tech_id))

        # Location history (every ping)
        cursor.execute("""
            INSERT INTO tech_location_history (tech_id, latitude, longitude, speed_kmh)
            VALUES (%s, %s, %s, %s)
        """, (data.tech_id, data.latitude, data.longitude, data.speed_kmh))

        # Update session distance
        cursor.execute("""
            UPDATE tech_sessions
            SET distance_km = COALESCE(distance_km,0) + %s
            WHERE tech_id=%s AND logout_at IS NULL
        """, (dist_increment, data.tech_id))

    return {"status": "location updated", "distance_added_km": round(dist_increment, 3)}


# ─────────────────────────────────────────
# GET /technicians  — with distance to ticket
# ─────────────────────────────────────────

@router.get("/technicians")
def get_technicians(
    available_only: bool          = Query(False),
    region:         Optional[str] = Query(None),
    ticket_lat:     Optional[float] = Query(None),
    ticket_lng:     Optional[float] = Query(None),
    online_only:    bool          = Query(False),
):
    with get_db() as (conn, cursor):
        query = """
            SELECT t.tech_id, t.name, t.region, t.mobile, t.status,
                   t.max_active_tickets, t.active_tickets,
                   t.latitude, t.longitude, t.is_online, t.last_location_at,
                   t.total_cases_resolved, t.avg_resolution_min,
                   t.rating, t.vehicle_no, t.total_distance_km
            FROM technicians t
            WHERE t.is_active=TRUE
        """
        params = []
        if available_only: query += " AND t.status='available'"
        if region:         query += " AND t.region=%s"; params.append(region)
        if online_only:    query += " AND t.is_online=TRUE"
        query += " ORDER BY t.name"
        cursor.execute(query, params)
        rows = cursor.fetchall()

    result = []
    for r in rows:
        tech_lat = float(r[7]) if r[7] else None
        tech_lng = float(r[8]) if r[8] else None
        entry = {
            "id": r[0], "name": r[1], "region": r[2], "mobile": r[3],
            "status": r[4], "capacity": r[5], "active_tickets": r[6] or 0,
            "lat": tech_lat, "lng": tech_lng,
            "is_online": r[9],
            "last_seen": str(r[10]) if r[10] else None,
            "total_resolved": r[11] or 0,
            "avg_resolution_min": r[12],
            "rating": float(r[13]) if r[13] else None,
            "vehicle_no": r[14],
            "total_distance_km": float(r[15]) if r[15] else 0,
            "load_pct": round((r[6] or 0) / max(r[5] or 1, 1) * 100),
        }
        if ticket_lat and ticket_lng and tech_lat and tech_lng:
            dist = calculate_distance(ticket_lat, ticket_lng, tech_lat, tech_lng)
            entry["distance_km"] = dist
            entry["eta_min"]     = estimate_eta_minutes(dist)
        result.append(entry)

    # Sort by distance if coords given
    if ticket_lat and ticket_lng:
        result.sort(key=lambda x: x.get("distance_km", 9999))

    return result


# ─────────────────────────────────────────
# GET /technician/{tech_id}/tickets  — active tickets
# ─────────────────────────────────────────

@router.get("/technician/{tech_id}/tickets")
def get_technician_tickets(tech_id: int):
    with get_db() as (conn, cursor):
        cursor.execute("SELECT name FROM technicians WHERE tech_id=%s", (tech_id,))
        tech = cursor.fetchone()
        if not tech: raise HTTPException(404, "Technician not found")

        cursor.execute("""
            SELECT t.rsa_ticket_id,
                   COALESCE(t.rider_name, r.rider_name) AS rider_name,
                   t.mobile_no, t.region, t.issue_category, t.issue_note,
                   t.ticket_status, t.ticket_datetime, t.latitude, t.longitude,
                   t.assigned_at, t.eta_min, t.distance_to_tech_km,
                   r.chassis_no, t.calling_mobile_no
            FROM rsa_ticket_master t
            LEFT JOIN riders_master r ON r.id=t.rider_id
            WHERE t.tech_id=%s AND t.ticket_status IN ('ASSIGNED','PORTER_PENDING')
            ORDER BY t.ticket_datetime DESC
        """, (tech_id,))
        rows = cursor.fetchall()

    return {
        "tech_id": tech_id, "tech_name": tech[0],
        "tickets": [{
            "ticket_id":  r[0],
            "rider":      r[1],
            "mobile":     r[2],
            "region":     r[3],
            "issue":      r[4],
            "note":       r[5],
            "status":     r[6],
            "time":       str(r[7]) if r[7] else None,
            "latitude":   float(r[8]) if r[8] else None,
            "longitude":  float(r[9]) if r[9] else None,
            "assigned_at":str(r[10]) if r[10] else None,
            "eta_min":    r[11],
            "distance_km":float(r[12]) if r[12] else None,
            "chassis_no": r[13] or "",
            "calling_mobile": r[14] or r[2] or "",
        } for r in rows]
    }


# ─────────────────────────────────────────
# GET /technician/{tech_id}/stats
# ─────────────────────────────────────────

@router.get("/technician/{tech_id}/stats")
def get_tech_stats(tech_id: int):
    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT name, region, total_cases_resolved, avg_resolution_min,
                   rating, total_distance_km, is_online, login_at, logout_at
            FROM technicians WHERE tech_id=%s
        """, (tech_id,))
        t = cursor.fetchone()
        if not t: raise HTTPException(404, "Not found")

        # Today's session
        cursor.execute("""
            SELECT duration_min, cases_handled, distance_km, login_at
            FROM tech_sessions
            WHERE tech_id=%s AND DATE(login_at)=CURRENT_DATE
            ORDER BY login_at DESC LIMIT 1
        """, (tech_id,))
        sess = cursor.fetchone()

        # Recent 30 days
        cursor.execute("""
            SELECT COUNT(*), ROUND(AVG(resolution_time_min))
            FROM rsa_ticket_master
            WHERE tech_id=%s AND ticket_status='CLOSED'
              AND ticket_datetime >= NOW()-INTERVAL '30 days'
        """, (tech_id,))
        monthly = cursor.fetchone()

        # By issue
        cursor.execute("""
            SELECT issue_category, COUNT(*),
                   ROUND(AVG(resolution_time_min))
            FROM rsa_ticket_master
            WHERE tech_id=%s AND ticket_status='CLOSED'
            GROUP BY issue_category ORDER BY COUNT(*) DESC
        """, (tech_id,))
        by_issue = [{"issue":r[0],"count":r[1],"avg_min":r[2]} for r in cursor.fetchall()]

    login_at = t[7]
    hours_today = 0
    if login_at and t[6]:  # is_online
        from datetime import datetime
        hours_today = round((datetime.now() - login_at).total_seconds() / 3600, 1)

    return {
        "name": t[0], "region": t[1],
        "total_resolved": t[2] or 0,
        "avg_resolution_min": t[3],
        "rating": float(t[4]) if t[4] else None,
        "total_distance_km": float(t[5]) if t[5] else 0,
        "is_online": t[6],
        "hours_online_today": hours_today,
        "session": {"duration_min":sess[0],"cases":sess[1],"distance_km":sess[2]} if sess else None,
        "monthly_resolved": monthly[0] or 0,
        "monthly_avg_min":  monthly[1],
        "by_issue": by_issue,
    }


# ─────────────────────────────────────────
# GET /technician/{tech_id}/trail  — GPS breadcrumb
# ─────────────────────────────────────────

@router.get("/technician/{tech_id}/trail")
def get_tech_trail(tech_id: int, hours: int = Query(8)):
    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT latitude, longitude, recorded_at, speed_kmh
            FROM tech_location_history
            WHERE tech_id=%s AND recorded_at >= NOW()-INTERVAL '1 hour' * %s
            ORDER BY recorded_at ASC
        """, (tech_id, hours))
        rows = cursor.fetchall()
    return [{
        "lat": float(r[0]), "lng": float(r[1]),
        "time": str(r[2]), "speed": r[3]
    } for r in rows]


# ─────────────────────────────────────────
# POST /update_technician_status
# ─────────────────────────────────────────

@router.post("/update_technician_status")
def update_status(data: StatusUpdateBody):
    VALID = {"available","busy","offline","on_leave"}
    if data.status not in VALID:
        raise HTTPException(400, f"Status must be one of {VALID}")
    with get_db() as (conn, cursor):
        cursor.execute("""
            UPDATE technicians SET status=%s,
            is_online=CASE WHEN %s IN ('offline','on_leave') THEN FALSE ELSE is_online END
            WHERE tech_id=%s
        """, (data.status, data.status, data.tech_id))
    return {"tech_id": data.tech_id, "status": data.status}


# ─────────────────────────────────────────
# POST /create_technician  — admin
# ─────────────────────────────────────────

@router.post("/create_technician")
def create_technician(data: CreateTechBody):
    from security import hash_password
    try:
        with get_db() as (conn, cursor):
            # Check duplicate
            cursor.execute("SELECT tech_id FROM technicians WHERE mobile=%s", (data.mobile,))
            if cursor.fetchone():
                raise HTTPException(409, "Mobile number already registered")

            # Detect actual columns
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='technicians'
            """)
            existing_cols = {r[0] for r in cursor.fetchall()}

            hashed = hash_password(data.password) if data.password else None

            # Build INSERT dynamically based on what columns actually exist
            cols   = ["name", "mobile", "region", "max_active_tickets",
                      "active_tickets", "is_active", "status"]
            vals   = [data.name, data.mobile, data.region, data.capacity,
                      0, True, "available"]

            if "password_hash" in existing_cols:
                cols.append("password_hash"); vals.append(hashed)
            if "password" in existing_cols:
                cols.append("password"); vals.append(data.password)
            if "vehicle_no" in existing_cols and data.vehicle_no:
                cols.append("vehicle_no"); vals.append(data.vehicle_no)
            if "skill_tags" in existing_cols and data.skill_tags:
                cols.append("skill_tags"); vals.append(data.skill_tags)
            if "joining_date" in existing_cols and data.joining_date:
                cols.append("joining_date"); vals.append(data.joining_date)

            placeholders = ",".join(["%s"] * len(cols))
            col_str      = ",".join(cols)
            cursor.execute(
                f"INSERT INTO technicians ({col_str}) VALUES ({placeholders}) RETURNING tech_id",
                vals
            )
            new_id = cursor.fetchone()[0]

        return {"status": "created", "tech_id": new_id, "name": data.name}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {str(e)}")

# ─────────────────────────────────────────
# POST /reset_tech_password  — admin resets technician password
# ─────────────────────────────────────────

class ResetPasswordBody(BaseModel):
    tech_id:      int
    new_password: str

@router.post("/reset_tech_password")
def reset_tech_password(data: ResetPasswordBody):
    from security import hash_password
    if not data.new_password or len(data.new_password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")
    try:
        hashed = hash_password(data.new_password)
        with get_db() as (conn, cursor):
            # Detect columns
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='technicians'
            """)
            existing_cols = {r[0] for r in cursor.fetchall()}

            sets, params = [], []
            if "password_hash" in existing_cols:
                sets.append("password_hash=%s"); params.append(hashed)
            if "password" in existing_cols:
                # Also store plain in legacy column so old login still works
                sets.append("password=%s"); params.append(data.new_password)

            if not sets:
                raise HTTPException(500, "No password column found in technicians table")

            params.append(data.tech_id)
            cursor.execute(
                f"UPDATE technicians SET {', '.join(sets)} WHERE tech_id=%s RETURNING name",
                params
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(404, "Technician not found")
        return {"status": "password_reset", "tech_id": data.tech_id, "name": row[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {str(e)}")


# ─────────────────────────────────────────
# GET /technician/{tech_id}/login_info  — admin views login credentials
# ─────────────────────────────────────────

@router.get("/technician/{tech_id}/login_info")
def get_tech_login_info(tech_id: int):
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT tech_id, name, mobile, region, status, is_active FROM technicians WHERE tech_id=%s",
            (tech_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "Not found")
    return {
        "tech_id":   row[0],
        "name":      row[1],
        "mobile":    row[2],   # This IS the login ID
        "region":    row[3],
        "status":    row[4],
        "is_active": row[5],
        "login_id":  row[2],   # Mobile = Login ID
        "note":      "Login uses mobile number as username"
    }


# ─────────────────────────────────────────
# PUT /update_technician/{tech_id}  — admin edits technician
# ─────────────────────────────────────────

class UpdateTechBody(BaseModel):
    name:         Optional[str] = None
    mobile:       Optional[str] = None
    region:       Optional[str] = None
    capacity:     Optional[int] = None
    vehicle_no:   Optional[str] = None
    skill_tags:   Optional[str] = None
    new_password: Optional[str] = None   # only if admin wants to change it

@router.put("/update_technician/{tech_id}")
def update_technician(tech_id: int, data: UpdateTechBody):
    from security import hash_password
    try:
        with get_db() as (conn, cursor):

            # Check exists
            cursor.execute("SELECT tech_id, name FROM technicians WHERE tech_id=%s", (tech_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(404, "Technician not found")

            # Get actual columns in the table so we dont crash on missing ones
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='technicians'
            """)
            existing_cols = {r[0] for r in cursor.fetchall()}

            sets, params = [], []
            if data.name     is not None:
                sets.append("name=%s");   params.append(data.name)
            if data.mobile   is not None:
                sets.append("mobile=%s"); params.append(data.mobile)
            if data.region   is not None:
                sets.append("region=%s"); params.append(data.region)
            if data.capacity is not None:
                sets.append("max_active_tickets=%s"); params.append(data.capacity)
            if data.vehicle_no is not None and "vehicle_no" in existing_cols:
                sets.append("vehicle_no=%s"); params.append(data.vehicle_no or None)
            if data.skill_tags is not None and "skill_tags" in existing_cols:
                sets.append("skill_tags=%s"); params.append(data.skill_tags or None)

            # Password — update both password_hash and password columns
            if data.new_password and data.new_password.strip():
                hashed = hash_password(data.new_password.strip())
                if "password_hash" in existing_cols:
                    sets.append("password_hash=%s"); params.append(hashed)
                if "password" in existing_cols:
                    sets.append("password=%s"); params.append(data.new_password.strip())

            if not sets:
                return {"status": "nothing_to_update", "tech_id": tech_id}

            params.append(tech_id)
            cursor.execute(
                f"UPDATE technicians SET {', '.join(sets)} WHERE tech_id=%s RETURNING name",
                params
            )
            updated = cursor.fetchone()

        return {"status": "updated", "tech_id": tech_id, "name": updated[0] if updated else None}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {str(e)}")