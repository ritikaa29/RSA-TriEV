from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from auth_guard import get_current_user
from utils import generate_ticket_id, calculate_distance, estimate_eta_minutes

router = APIRouter()


# ─────────────────────────────────────────
# Models
# ─────────────────────────────────────────

class AssignTicketBody(BaseModel):
    ticket_id:        str
    technician_name:  str
    tech_id:          Optional[int] = None

class ReassignTicketBody(BaseModel):
    ticket_id:         str
    new_tech_id:       int
    new_tech_name:     str
    reason:            Optional[str] = None

class CloseTicketBody(BaseModel):
    ticket_id:         str
    resolution_note:   Optional[str] = None
    resolution_time_min: Optional[int] = None

class ArrivalBody(BaseModel):
    ticket_id: str
    tech_id:   int


# ─────────────────────────────────────────
# POST /create_ticket
# ─────────────────────────────────────────

@router.post("/create_ticket")
def create_ticket(
    rider_id:       int,
    mobile_no:      str,
    calling_mobile: str,
    region:         str,
    issue_category: str,
    issue_note:     str,
    latitude:       float,
    longitude:      float,
):
    with get_db() as (conn, cursor):

        # Block duplicate open tickets
        cursor.execute("""
            SELECT rsa_ticket_id FROM rsa_ticket_master
            WHERE rider_id=%s AND ticket_status!='CLOSED'
        """, (rider_id,))
        existing = cursor.fetchone()
        if existing:
            raise HTTPException(400, f"Active ticket exists: {existing[0]}")

        # Get rider name
        cursor.execute("SELECT rider_name FROM riders_master WHERE id=%s", (rider_id,))
        r = cursor.fetchone()
        rider_name = r[0] if r else str(rider_id)

        ticket_id = generate_ticket_id()

        cursor.execute("""
            INSERT INTO rsa_ticket_master (
                rsa_ticket_id, rider_id, rider_name, mobile_no, calling_mobile_no,
                region, issue_category, issue_note, latitude, longitude,
                ticket_datetime, technician_name, tech_id, ticket_status,
                closed_at, resolution_note
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NULL,NULL,'OPEN',NULL,NULL)
        """, (ticket_id, rider_id, rider_name, mobile_no, calling_mobile,
               region, issue_category, issue_note, latitude, longitude))

        # Timeline
        cursor.execute("""
            INSERT INTO ticket_timeline (ticket_id, event, actor, note)
            VALUES (%s,'CREATED','Rider',%s)
        """, (ticket_id, f"Ticket raised by {rider_name} — {issue_category}"))

    return {
        "ticket_id":  ticket_id,
        "status":     "OPEN",
        "issue":      issue_category,
        "region":     region,
        "time":       datetime.now().strftime("%d %b %Y %H:%M"),
        "message":    "Ticket raised successfully"
    }


# ─────────────────────────────────────────
# GET /tickets
# ─────────────────────────────────────────

@router.get("/tickets")
def get_tickets(
    status:     Optional[str] = Query(None),
    region:     Optional[str] = Query(None),
    issue:      Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date:   Optional[str] = Query(None),
    tech_id:    Optional[int] = Query(None),
    limit:      int           = Query(500, le=2000),
):
    with get_db() as (conn, cursor):
        query = """
            SELECT
                t.rsa_ticket_id, t.rider_id,
                COALESCE(t.rider_name, r.rider_name, t.rider_id::text) AS rider_name,
                t.mobile_no, t.calling_mobile_no, t.region,
                t.issue_category, t.issue_note,
                t.technician_name, t.tech_id, t.ticket_status,
                t.ticket_datetime, t.assigned_at, t.closed_at,
                t.latitude, t.longitude, t.resolution_note,
                t.resolution_time_min, t.distance_to_tech_km, t.eta_min,
                t.rider_rating, t.rider_feedback, t.reassignment_reason,
                r.chassis_no
            FROM rsa_ticket_master t
            LEFT JOIN riders_master r ON r.id = t.rider_id
            WHERE 1=1
        """
        params = []

        if status:
            query += " AND t.ticket_status=%s"; params.append(status.upper())
        if region:
            query += " AND t.region=%s"; params.append(region)
        if issue:
            query += " AND t.issue_category=%s"; params.append(issue)
        if start_date:
            query += " AND DATE(t.ticket_datetime)>=%s"; params.append(start_date)
        if end_date:
            query += " AND DATE(t.ticket_datetime)<=%s"; params.append(end_date)
        if tech_id:
            query += " AND t.tech_id=%s"; params.append(tech_id)

        query += " ORDER BY t.ticket_datetime DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

    return [_fmt_ticket(r) for r in rows]


# ─────────────────────────────────────────
# GET /ticket/{ticket_id}  — full detail
# ─────────────────────────────────────────

@router.get("/ticket/{ticket_id}")
def get_ticket(ticket_id: str):
    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT
                t.rsa_ticket_id, t.rider_id,
                COALESCE(t.rider_name, r.rider_name) AS rider_name,
                t.mobile_no, t.calling_mobile_no, t.region,
                t.issue_category, t.issue_note, t.technician_name, t.tech_id,
                t.ticket_status, t.ticket_datetime, t.assigned_at, t.closed_at,
                t.latitude, t.longitude, t.resolution_note, t.resolution_time_min,
                t.distance_to_tech_km, t.eta_min, t.rider_rating, t.rider_feedback,
                r.chassis_no, r.tl_name, t.reassignment_reason, t.tech_arrived_at
            FROM rsa_ticket_master t
            LEFT JOIN riders_master r ON r.id = t.rider_id
            WHERE t.rsa_ticket_id=%s
        """, (ticket_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(404, "Ticket not found")

        # Timeline
        cursor.execute("""
            SELECT event, actor, note, lat, lng, created_at
            FROM ticket_timeline WHERE ticket_id=%s ORDER BY created_at ASC
        """, (ticket_id,))
        timeline = [{"event":r[0],"actor":r[1],"note":r[2],"lat":r[3],"lng":r[4],"time":str(r[5])} for r in cursor.fetchall()]

    result = _fmt_ticket(row[:23])
    result["chassis_no"]    = row[22]
    result["tl_name"]       = row[23]
    result["reassign_reason"]= row[24]
    result["tech_arrived_at"]= str(row[25]) if row[25] else None
    result["timeline"]      = timeline
    return result


# ─────────────────────────────────────────
# POST /assign_ticket
# ─────────────────────────────────────────

@router.post("/assign_ticket")
def assign_ticket(data: AssignTicketBody):
    with get_db() as (conn, cursor):

        cursor.execute("SELECT ticket_status, latitude, longitude FROM rsa_ticket_master WHERE rsa_ticket_id=%s", (data.ticket_id,))
        row = cursor.fetchone()
        if not row: raise HTTPException(404, "Ticket not found")
        if row[0] == 'CLOSED': raise HTTPException(400, "Cannot assign closed ticket")

        t_lat, t_lng = row[1], row[2]
        dist = eta = None

        if data.tech_id and t_lat and t_lng:
            cursor.execute("SELECT latitude, longitude FROM technicians WHERE tech_id=%s", (data.tech_id,))
            tech_loc = cursor.fetchone()
            if tech_loc and tech_loc[0]:
                dist = calculate_distance(float(t_lat), float(t_lng), float(tech_loc[0]), float(tech_loc[1]))
                eta  = estimate_eta_minutes(dist)

        cursor.execute("""
            UPDATE rsa_ticket_master
            SET technician_name=%s, tech_id=%s, ticket_status='ASSIGNED',
                assigned_at=NOW(), distance_to_tech_km=%s, eta_min=%s
            WHERE rsa_ticket_id=%s
        """, (data.technician_name, data.tech_id, dist, eta, data.ticket_id))

        cursor.execute("""
            INSERT INTO ticket_timeline (ticket_id, event, actor, actor_id, note)
            VALUES (%s,'ASSIGNED',%s,%s,%s)
        """, (data.ticket_id, data.technician_name, data.tech_id,
              f"Assigned to {data.technician_name}" + (f" · ETA {eta} min ({dist} km)" if eta else "")))

    return {"ticket_id": data.ticket_id, "technician": data.technician_name,
            "status": "ASSIGNED", "eta_min": eta, "distance_km": dist}


# ─────────────────────────────────────────
# POST /reassign_ticket
# ─────────────────────────────────────────

@router.post("/reassign_ticket")
def reassign_ticket(data: ReassignTicketBody):
    with get_db() as (conn, cursor):

        cursor.execute("""
            SELECT ticket_status, technician_name, tech_id, latitude, longitude
            FROM rsa_ticket_master WHERE rsa_ticket_id=%s
        """, (data.ticket_id,))
        row = cursor.fetchone()
        if not row: raise HTTPException(404, "Ticket not found")
        if row[0] == 'CLOSED': raise HTTPException(400, "Ticket already closed")

        old_tech = row[1]
        t_lat, t_lng = row[3], row[4]
        dist = eta = None

        if t_lat and t_lng:
            cursor.execute("SELECT latitude, longitude FROM technicians WHERE tech_id=%s", (data.new_tech_id,))
            tech_loc = cursor.fetchone()
            if tech_loc and tech_loc[0]:
                dist = calculate_distance(float(t_lat), float(t_lng), float(tech_loc[0]), float(tech_loc[1]))
                eta  = estimate_eta_minutes(dist)

        cursor.execute("""
            UPDATE rsa_ticket_master
            SET technician_name=%s, tech_id=%s, ticket_status='ASSIGNED',
                reassigned_from_tech_id=(SELECT tech_id FROM technicians WHERE name=%s LIMIT 1),
                reassignment_reason=%s, reassigned_at=NOW(),
                distance_to_tech_km=%s, eta_min=%s
            WHERE rsa_ticket_id=%s
        """, (data.new_tech_name, data.new_tech_id, old_tech,
              data.reason, dist, eta, data.ticket_id))

        cursor.execute("""
            INSERT INTO ticket_timeline (ticket_id, event, actor, actor_id, note)
            VALUES (%s,'REASSIGNED','Dispatcher',%s,%s)
        """, (data.ticket_id, data.new_tech_id,
              f"Reassigned from {old_tech} to {data.new_tech_name}. Reason: {data.reason or 'N/A'}"))

    return {"ticket_id": data.ticket_id, "new_technician": data.new_tech_name,
            "eta_min": eta, "distance_km": dist}


# ─────────────────────────────────────────
# POST /tech_arrived
# ─────────────────────────────────────────

@router.post("/tech_arrived")
def tech_arrived(data: ArrivalBody):
    with get_db() as (conn, cursor):
        cursor.execute("""
            UPDATE rsa_ticket_master SET tech_arrived_at=NOW()
            WHERE rsa_ticket_id=%s
        """, (data.ticket_id,))
        cursor.execute("""
            INSERT INTO ticket_timeline (ticket_id, event, actor, actor_id, note)
            VALUES (%s,'ARRIVED',(SELECT name FROM technicians WHERE tech_id=%s LIMIT 1),%s,'Technician arrived at location')
        """, (data.ticket_id, data.tech_id, data.tech_id))
    return {"status": "arrival recorded"}


# ─────────────────────────────────────────
# POST /close_ticket
# ─────────────────────────────────────────

@router.post("/close_ticket")
def close_ticket(data: CloseTicketBody):
    with get_db() as (conn, cursor):

        cursor.execute("""
            SELECT ticket_status, tech_id, technician_name, ticket_datetime
            FROM rsa_ticket_master WHERE rsa_ticket_id=%s
        """, (data.ticket_id,))
        row = cursor.fetchone()
        if not row: raise HTTPException(404, "Ticket not found")
        if row[0] == 'CLOSED': raise HTTPException(400, "Already closed")

        opened = row[3]
        res_time = data.resolution_time_min
        if not res_time and opened:
            diff = datetime.now() - opened
            res_time = int(diff.total_seconds() / 60)

        cursor.execute("""
            UPDATE rsa_ticket_master
            SET ticket_status='CLOSED', closed_at=NOW(),
                resolution_note=%s, resolution_time_min=%s
            WHERE rsa_ticket_id=%s
        """, (data.resolution_note, res_time, data.ticket_id))

        # Update tech stats
        if row[1]:
            cursor.execute("""
                UPDATE technicians
                SET total_cases_resolved = COALESCE(total_cases_resolved,0) + 1,
                    active_tickets       = GREATEST(0, COALESCE(active_tickets,0) - 1),
                    avg_resolution_min   = (
                        SELECT ROUND(AVG(resolution_time_min))
                        FROM rsa_ticket_master
                        WHERE tech_id=%s AND ticket_status='CLOSED' AND resolution_time_min IS NOT NULL
                    )
                WHERE tech_id=%s
            """, (row[1], row[1]))

        cursor.execute("""
            INSERT INTO ticket_timeline (ticket_id, event, actor, actor_id, note)
            VALUES (%s,'CLOSED',%s,%s,%s)
        """, (data.ticket_id, row[2] or 'System', row[1],
              f"Resolved in {res_time} min. {data.resolution_note or ''}"))

    return {"ticket_id": data.ticket_id, "status": "CLOSED",
            "resolution_time_min": res_time,
            "closed_at": datetime.now().strftime("%d %b %Y %H:%M")}


# ─────────────────────────────────────────
# GET /live_map_data
# ─────────────────────────────────────────

@router.get("/live_map_data")
def live_map_data():
    with get_db() as (conn, cursor):

        cursor.execute("""
            SELECT t.rsa_ticket_id,
                   COALESCE(t.rider_name, r.rider_name) AS rider_name,
                   t.region, t.issue_category, t.ticket_status,
                   t.technician_name, t.latitude, t.longitude,
                   t.ticket_datetime, t.eta_min, t.distance_to_tech_km
            FROM rsa_ticket_master t
            LEFT JOIN riders_master r ON r.id = t.rider_id
            WHERE t.ticket_status != 'CLOSED' AND t.latitude IS NOT NULL
            ORDER BY t.ticket_datetime DESC
        """)
        ticket_rows = cursor.fetchall()

        # Technicians with live location (online in last 5 min)
        cursor.execute("""
            SELECT tech_id, name, region, status, is_online,
                   latitude, longitude, last_location_at,
                   active_tickets, max_active_tickets, avg_resolution_min
            FROM technicians
            WHERE is_active = TRUE AND latitude IS NOT NULL
        """)
        tech_rows = cursor.fetchall()

    tickets = [{
        "ticket_id":    r[0], "rider":        r[1],
        "region":       r[2], "issue":        r[3],
        "status":       r[4], "technician":   r[5],
        "latitude":     float(r[6]), "longitude": float(r[7]),
        "time":         str(r[8]) if r[8] else None,
        "eta_min":      r[9],  "distance_km": r[10],
    } for r in ticket_rows]

    technicians = [{
        "tech_id":      r[0], "name":         r[1],
        "region":       r[2], "status":       r[3],
        "is_online":    r[4],
        "latitude":     float(r[5]), "longitude": float(r[6]),
        "last_seen":    str(r[7]) if r[7] else None,
        "active":       r[8],  "capacity":    r[9],
        "avg_min":      r[10],
    } for r in tech_rows]

    return {"tickets": tickets, "technicians": technicians}


# ─────────────────────────────────────────
# GET /dashboard_stats
# ─────────────────────────────────────────

@router.get("/dashboard_stats")
def dashboard_stats():
    with get_db() as (conn, cursor):

        # Overall counts
        cursor.execute("""
            SELECT
                COUNT(*) FILTER (WHERE ticket_status='OPEN')                           AS open_now,
                COUNT(*) FILTER (WHERE ticket_status='ASSIGNED')                       AS assigned_now,
                COUNT(*) FILTER (WHERE ticket_status='CLOSED' AND DATE(closed_at)=CURRENT_DATE) AS closed_today,
                ROUND(AVG(resolution_time_min) FILTER (
                    WHERE ticket_status='CLOSED' AND DATE(closed_at)=CURRENT_DATE))    AS avg_res_today,
                COUNT(*) FILTER (WHERE DATE(ticket_datetime)=CURRENT_DATE)             AS raised_today
            FROM rsa_ticket_master
        """)
        s = cursor.fetchone()

        # Available techs
        cursor.execute("SELECT COUNT(*) FROM technicians WHERE status='available' AND is_active=TRUE")
        avail = cursor.fetchone()[0]

        # Issue breakdown (today + 30d)
        cursor.execute("""
            SELECT issue_category,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE ticket_status='CLOSED') AS resolved,
                   ROUND(AVG(resolution_time_min) FILTER (WHERE ticket_status='CLOSED')) AS avg_min,
                   ROUND(COUNT(*) FILTER (WHERE ticket_status='CLOSED')::numeric / NULLIF(COUNT(*),0)*100) AS pct
            FROM rsa_ticket_master
            WHERE ticket_datetime >= NOW() - INTERVAL '30 days'
            GROUP BY issue_category ORDER BY total DESC
        """)
        issues = [{"issue":r[0],"total":r[1],"resolved":r[2],"avg_min":r[3],"pct":r[4]} for r in cursor.fetchall()]

        # Region load
        cursor.execute("""
            SELECT region, COUNT(*) AS cnt
            FROM rsa_ticket_master
            WHERE ticket_status IN ('OPEN','ASSIGNED')
            GROUP BY region ORDER BY cnt DESC
        """)
        regions = [{"region":r[0],"count":r[1]} for r in cursor.fetchall()]

        # Daily volume (last 14 days)
        cursor.execute("""
            SELECT DATE(ticket_datetime) AS day, COUNT(*) AS cnt
            FROM rsa_ticket_master
            WHERE ticket_datetime >= NOW() - INTERVAL '14 days'
            GROUP BY day ORDER BY day
        """)
        daily = [{"date":str(r[0]),"count":r[1]} for r in cursor.fetchall()]

        # Tech performance
        cursor.execute("""
            SELECT t.tech_id, t.name, t.region, t.status, t.active_tickets,
                   t.max_active_tickets, t.total_cases_resolved,
                   t.avg_resolution_min, t.rating, t.is_online
            FROM technicians t WHERE t.is_active=TRUE ORDER BY t.total_cases_resolved DESC NULLS LAST
        """)
        techs = [{
            "tech_id":r[0],"name":r[1],"region":r[2],"status":r[3],
            "active":r[4],"capacity":r[5],"resolved":r[6],
            "avg_min":r[7],"rating":r[8],"is_online":r[9]
        } for r in cursor.fetchall()]

        # Zone stats
        cursor.execute("""
            SELECT zone, active_tickets, open_tickets, available_techs, total_techs
            FROM v_zone_load
        """)
        zones = [{"zone":r[0],"active":r[1],"open":r[2],"avail_techs":r[3],"total_techs":r[4]} for r in cursor.fetchall()]

    return {
        "summary": {
            "open": s[0] or 0, "assigned": s[1] or 0,
            "closed_today": s[2] or 0, "raised_today": s[4] or 0,
            "avg_resolution_min": int(s[3]) if s[3] else None,
            "available_techs": avail or 0,
        },
        "issue_breakdown": issues,
        "region_load":     regions,
        "daily_volume":    daily,
        "tech_performance":techs,
        "zone_stats":      zones,
    }


# ─────────────────────────────────────────
# GET /analytics  — deeper report
# ─────────────────────────────────────────

@router.get("/analytics")
def analytics(
    days:   int           = Query(30),
    region: Optional[str] = Query(None),
    tech_id:Optional[int] = Query(None),
):
    with get_db() as (conn, cursor):

        base = f"WHERE ticket_datetime >= NOW() - INTERVAL '{days} days'"
        if region:  base += f" AND region='{region}'"
        if tech_id: base += f" AND tech_id={tech_id}"

        # Issue breakdown with avg resolution
        cursor.execute(f"""
            SELECT issue_category, COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE ticket_status='CLOSED') AS resolved,
                   ROUND(AVG(resolution_time_min) FILTER (WHERE ticket_status='CLOSED')) AS avg_min,
                   MIN(resolution_time_min) FILTER (WHERE ticket_status='CLOSED') AS min_min,
                   MAX(resolution_time_min) FILTER (WHERE ticket_status='CLOSED') AS max_min
            FROM rsa_ticket_master {base}
            GROUP BY issue_category ORDER BY total DESC
        """)
        issues = [{"issue":r[0],"total":r[1],"resolved":r[2],"avg_min":r[3],"min_min":r[4],"max_min":r[5]} for r in cursor.fetchall()]

        # Zone-wise
        cursor.execute(f"""
            SELECT region, COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE ticket_status='CLOSED') AS resolved,
                   ROUND(AVG(resolution_time_min) FILTER (WHERE ticket_status='CLOSED')) AS avg_min
            FROM rsa_ticket_master {base}
            GROUP BY region ORDER BY total DESC
        """)
        zones = [{"region":r[0],"total":r[1],"resolved":r[2],"avg_min":r[3]} for r in cursor.fetchall()]

        # Hourly distribution
        cursor.execute(f"""
            SELECT EXTRACT(HOUR FROM ticket_datetime) AS hr, COUNT(*) AS cnt
            FROM rsa_ticket_master {base}
            GROUP BY hr ORDER BY hr
        """)
        hourly = [{"hour":int(r[0]),"count":r[1]} for r in cursor.fetchall()]

        # Tech performance
        cursor.execute(f"""
            SELECT t.name, t.region,
                   COUNT(m.rsa_ticket_id) AS total,
                   COUNT(m.rsa_ticket_id) FILTER (WHERE m.ticket_status='CLOSED') AS resolved,
                   ROUND(AVG(m.resolution_time_min) FILTER (WHERE m.ticket_status='CLOSED')) AS avg_min,
                   t.rating
            FROM technicians t
            LEFT JOIN rsa_ticket_master m ON m.tech_id=t.tech_id
                AND m.ticket_datetime >= NOW() - INTERVAL '{days} days'
            WHERE t.is_active=TRUE
            GROUP BY t.tech_id, t.name, t.region, t.rating
            ORDER BY resolved DESC NULLS LAST
        """)
        techs = [{"name":r[0],"region":r[1],"total":r[2],"resolved":r[3],"avg_min":r[4],"rating":r[5]} for r in cursor.fetchall()]

        # Daily volume
        cursor.execute(f"""
            SELECT DATE(ticket_datetime), COUNT(*),
                   COUNT(*) FILTER (WHERE ticket_status='CLOSED')
            FROM rsa_ticket_master {base}
            GROUP BY DATE(ticket_datetime) ORDER BY 1
        """)
        daily = [{"date":str(r[0]),"total":r[1],"closed":r[2]} for r in cursor.fetchall()]

    return {"issue_breakdown":issues,"zone_analysis":zones,"hourly_distribution":hourly,"tech_performance":techs,"daily_volume":daily,"period_days":days}


# Helper
def _fmt_ticket(r):
    return {
        "id":r[0], "ticket_id":r[0],
        "rider_id":r[1],"rider":r[2],"mobile":r[3],"calling_mobile":r[4],
        "region":r[5],"issue":r[6],"note":r[7],"technician":r[8],"tech_id":r[9],
        "status":r[10],
        "ticket_time":str(r[11]) if r[11] else None,
        "assigned_at":str(r[12]) if r[12] else None,
        "closed_at":str(r[13]) if r[13] else None,
        "latitude":float(r[14]) if r[14] else None,
        "longitude":float(r[15]) if r[15] else None,
        "resolution_note":r[16],"resolution_time_min":r[17],
        "distance_km":float(r[18]) if r[18] else None,
        "eta_min":r[19],"rider_rating":r[20],"rider_feedback":r[21],
        "reassignment_reason":r[22] if len(r)>22 else None,
        "chassis_no": r[23] if len(r)>23 else None,
    }


# ─────────────────────────────────────────
# POST /create_porter_request
# ─────────────────────────────────────────

class PorterRequestBody(BaseModel):
    ticket_id:     str
    tech_id:       int
    issues:        Optional[str] = None
    porter_amount: Optional[float] = None
    note:          Optional[str] = None

@router.post("/create_porter_request")
def create_porter_request(data: PorterRequestBody):
    with get_db() as (conn, cursor):
        # Reopen ticket as PORTER_PENDING so RSA manager can see it
        cursor.execute("""
            UPDATE rsa_ticket_master
            SET ticket_status = 'PORTER_PENDING',
                resolution_note = %s
            WHERE rsa_ticket_id = %s
        """, (
            f"PORTER REQUEST — Issues: {data.issues}. Amount: ₹{data.porter_amount}. {data.note or ''}",
            data.ticket_id
        ))
        cursor.execute("""
            INSERT INTO ticket_timeline (ticket_id, event, actor, actor_id, note)
            VALUES (%s, 'PORTER_REQUESTED', %s, %s, %s)
        """, (
            data.ticket_id,
            f"Tech ID {data.tech_id}",
            data.tech_id,
            f"Porter needed. Est. amt: ₹{data.porter_amount}. Issues: {data.issues}"
        ))
    return {"status": "porter_request_created", "ticket_id": data.ticket_id}


# ─────────────────────────────────────────
# POST /ticket_call_log  — log first call timestamp
# ─────────────────────────────────────────

class CallLogBody(BaseModel):
    ticket_id: str
    tech_id:   int
    called_at: str
    mobile:    Optional[str] = None

@router.post("/ticket_call_log")
def ticket_call_log(data: CallLogBody):
    with get_db() as (conn, cursor):
        cursor.execute("""
            INSERT INTO ticket_timeline (ticket_id, event, actor, actor_id, note)
            VALUES (%s, 'CALLED_RIDER', %s, %s, %s)
        """, (
            data.ticket_id,
            f"Tech ID {data.tech_id}",
            data.tech_id,
            f"Called rider {data.mobile or ''} at {data.called_at}"
        ))
    return {"status": "logged"}


# ─────────────────────────────────────────
# GET /tickets/report  — enriched report with timeline data
# ─────────────────────────────────────────

@router.get("/tickets/report")
def tickets_report(
    start_date: Optional[str] = Query(None),
    end_date:   Optional[str] = Query(None),
    region:     Optional[str] = Query(None),
    status:     Optional[str] = Query(None),
    tech_id:    Optional[int] = Query(None),
    limit:      int           = Query(2000, le=5000),
):
    with get_db() as (conn, cursor):

        # ── Main tickets ──────────────────────────────────────────────
        q = """
            SELECT
                t.rsa_ticket_id,
                COALESCE(t.rider_name, r.rider_name, '') AS rider_name,
                r.chassis_no,
                t.mobile_no, t.region,
                t.issue_category, t.issue_note,
                t.technician_name, t.tech_id, t.ticket_status,
                t.ticket_datetime, t.assigned_at, t.tech_arrived_at, t.closed_at,
                t.resolution_note, t.resolution_time_min,
                t.distance_to_tech_km, t.eta_min,
                t.reassignment_reason,
                t.resolution_type,
                t.porter_amount, t.porter_received_at
            FROM rsa_ticket_master t
            LEFT JOIN riders_master r ON r.id = t.rider_id
            WHERE 1=1
        """
        params = []
        if start_date: q += " AND DATE(t.ticket_datetime)>=%s"; params.append(start_date)
        if end_date:   q += " AND DATE(t.ticket_datetime)<=%s"; params.append(end_date)
        if region:     q += " AND t.region=%s";                  params.append(region)
        if status:     q += " AND t.ticket_status=%s";           params.append(status.upper())
        if tech_id:    q += " AND t.tech_id=%s";                 params.append(tech_id)
        q += " ORDER BY t.ticket_datetime DESC LIMIT %s"; params.append(limit)
        cursor.execute(q, params)
        ticket_rows = cursor.fetchall()

        if not ticket_rows:
            return []

        # ── Timeline events for all fetched tickets ───────────────────
        ticket_ids = [r[0] for r in ticket_rows]
        placeholders = ','.join(['%s'] * len(ticket_ids))
        cursor.execute(f"""
            SELECT ticket_id, event, note, created_at
            FROM ticket_timeline
            WHERE ticket_id IN ({placeholders})
            ORDER BY created_at ASC
        """, ticket_ids)
        tl_rows = cursor.fetchall()

    # Index timeline by ticket_id
    from collections import defaultdict
    tl_map = defaultdict(list)
    for row in tl_rows:
        tl_map[row[0]].append({'event': row[1], 'note': row[2], 'at': row[3]})

    results = []
    for r in ticket_rows:
        tid        = r[0]
        tl         = tl_map.get(tid, [])

        # First response time = CALLED_RIDER event
        call_event  = next((e for e in tl if e['event']=='CALLED_RIDER'), None)
        first_call  = call_event['at'] if call_event else None

        # First response time in minutes (from ticket raised to first call)
        raised_at   = r[10]
        first_resp_min = ''
        if first_call and raised_at:
            first_resp_min = round((first_call - raised_at).total_seconds() / 60, 1)

        # Time from assignment to arrival
        assign_to_arrive = ''
        if r[11] and r[12]:  # assigned_at, tech_arrived_at
            assign_to_arrive = round((r[12] - r[11]).total_seconds() / 60, 1)

        # Porter details from timeline note
        porter_event = next((e for e in tl if e['event']=='PORTER_REQUESTED'), None)
        porter_note  = porter_event['note'] if porter_event else ''
        porter_raised_at = porter_event['at'] if porter_event else None

        # Parse resolution note for parts/issues/cost
        res_note = r[14] or ''

        results.append({
            'ticket_id':         tid,
            'rider':             r[1],
            'chassis_no':        r[2] or '',
            'mobile':            r[3],
            'region':            r[4],
            'issue_category':    r[5],
            'issue_note':        r[6] or '',
            'technician':        r[7] or '',
            'status':            r[9],
            'raised_at':         str(r[10]) if r[10] else '',
            'assigned_at':       str(r[11]) if r[11] else '',
            'first_call_at':     str(first_call) if first_call else '',
            'arrived_at':        str(r[12]) if r[12] else '',
            'closed_at':         str(r[13]) if r[13] else '',
            'first_response_min':first_resp_min,
            'assign_to_arrive_min': assign_to_arrive,
            'resolution_time_min': r[15] or '',
            'resolution_note':   res_note,
            'resolution_type':   r[19] or '',
            'distance_km':       float(r[16]) if r[16] else '',
            'eta_min':           r[17] or '',
            'reassigned':        'Yes' if r[18] else 'No',
            'reassign_reason':   r[18] or '',
            'porter_raised_at':  str(porter_raised_at) if porter_raised_at else '',
            'porter_received_at':str(r[21]) if r[21] else '',
            'porter_amount':     float(r[20]) if r[20] else '',
            'porter_note':       porter_note,
        })

    return results


# ─────────────────────────────────────────
# GET /porter/report  — porter-specific report
# ─────────────────────────────────────────

@router.get("/porter/report")
def porter_report(
    start_date: Optional[str] = Query(None),
    end_date:   Optional[str] = Query(None),
):
    with get_db() as (conn, cursor):
        q = """
            SELECT
                t.rsa_ticket_id,
                COALESCE(t.rider_name, r.rider_name, '') AS rider_name,
                r.chassis_no, t.mobile_no, t.region,
                t.issue_category, t.technician_name,
                t.ticket_datetime, t.closed_at,
                t.resolution_note, t.porter_amount, t.porter_received_at,
                t.resolution_time_min
            FROM rsa_ticket_master t
            LEFT JOIN riders_master r ON r.id = t.rider_id
            WHERE (t.ticket_status IN ('PORTER_PENDING','CLOSED')
                   AND (t.resolution_type='porter_requested'
                        OR t.resolution_note LIKE '%PORTER REQUEST%'
                        OR t.ticket_status='PORTER_PENDING'))
        """
        params = []
        if start_date: q += " AND DATE(t.ticket_datetime)>=%s"; params.append(start_date)
        if end_date:   q += " AND DATE(t.ticket_datetime)<=%s"; params.append(end_date)
        q += " ORDER BY t.ticket_datetime DESC"
        cursor.execute(q, params)
        rows = cursor.fetchall()

        # Timeline porter events
        if rows:
            tids = [r[0] for r in rows]
            ph   = ','.join(['%s']*len(tids))
            cursor.execute(f"""
                SELECT ticket_id, event, note, created_at
                FROM ticket_timeline
                WHERE ticket_id IN ({ph})
                  AND event IN ('PORTER_REQUESTED','CALLED_RIDER')
                ORDER BY created_at
            """, tids)
            tl = cursor.fetchall()
        else:
            tl = []

    from collections import defaultdict
    tl_map = defaultdict(list)
    for row in tl: tl_map[row[0]].append({'event':row[1],'note':row[2],'at':row[3]})

    results = []
    for r in rows:
        tid = r[0]
        events = tl_map.get(tid, [])
        porter_ev = next((e for e in events if e['event']=='PORTER_REQUESTED'), None)
        results.append({
            'ticket_id':        tid,
            'rider':            r[1],
            'chassis_no':       r[2] or '',
            'mobile':           r[3],
            'region':           r[4],
            'issue':            r[5],
            'technician':       r[6] or '',
            'raised_at':        str(r[7]) if r[7] else '',
            'porter_requested_at': str(porter_ev['at']) if porter_ev else '',
            'porter_received_at':  str(r[11]) if r[11] else '',
            'porter_amount':    float(r[10]) if r[10] else '',
            'closed_at':        str(r[8]) if r[8] else '',
            'resolution_note':  r[9] or '',
            'resolution_min':   r[12] or '',
            'porter_note':      porter_ev['note'] if porter_ev else '',
        })
    return results


# ─────────────────────────────────────────
# POST /update_porter_received
# ─────────────────────────────────────────

class PorterReceivedBody(BaseModel):
    ticket_id:          str
    porter_amount:      Optional[float] = None
    porter_received_at: Optional[str]   = None

@router.post("/update_porter_received")
def update_porter_received(data: PorterReceivedBody):
    with get_db() as (conn, cursor):
        cursor.execute("""
            UPDATE rsa_ticket_master
            SET porter_amount      = COALESCE(%s, porter_amount),
                porter_received_at = COALESCE(%s::timestamp, porter_received_at)
            WHERE rsa_ticket_id = %s
        """, (data.porter_amount, data.porter_received_at, data.ticket_id))
        cursor.execute("""
            INSERT INTO ticket_timeline (ticket_id, event, actor, note)
            VALUES (%s, 'PORTER_RECEIVED', 'RSA Manager', %s)
        """, (data.ticket_id, f"Porter received at hub. Amount: ₹{data.porter_amount}"))
    return {"status": "porter_received_updated", "ticket_id": data.ticket_id}


# ─────────────────────────────────────────
# GET/POST /settings/helpline  — admin-configurable helpline
# ─────────────────────────────────────────

@router.get("/settings/helpline")
def get_helpline():
    with get_db() as (conn, cursor):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("SELECT key, value FROM system_settings WHERE key IN ('helpline_number','helpline_enabled')")
        rows = {r[0]: r[1] for r in cursor.fetchall()}
    return {
        "helpline_number":  rows.get("helpline_number", "8344191404"),
        "helpline_enabled": rows.get("helpline_enabled", "true") != "false"
    }


class HelplineBody(BaseModel):
    helpline_number: str

@router.post("/settings/helpline")
def update_helpline(data: HelplineBody):
    if not data.helpline_number.replace('+','').replace('-','').replace(' ','').isdigit():
        raise HTTPException(400, "Invalid phone number")
    with get_db() as (conn, cursor):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ('helpline_number', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()
        """, (data.helpline_number, data.helpline_number))
    return {"status": "updated", "helpline_number": data.helpline_number}


# ─────────────────────────────────────────
# DELETE /delete_ticket/{ticket_id}
# ─────────────────────────────────────────

@router.delete("/delete_ticket/{ticket_id}")
def delete_ticket(ticket_id: str):
    with get_db() as (conn, cursor):
        cursor.execute("SELECT rsa_ticket_id FROM rsa_ticket_master WHERE rsa_ticket_id=%s", (ticket_id,))
        if not cursor.fetchone():
            raise HTTPException(404, "Ticket not found")
        # Delete timeline first (foreign key)
        cursor.execute("DELETE FROM ticket_timeline WHERE ticket_id=%s", (ticket_id,))
        # Delete ticket
        cursor.execute("DELETE FROM rsa_ticket_master WHERE rsa_ticket_id=%s", (ticket_id,))
    return {"status": "deleted", "ticket_id": ticket_id}


# ─────────────────────────────────────────
# POST /settings/helpline_toggle
# ─────────────────────────────────────────

class HelplineToggleBody(BaseModel):
    enabled: bool

@router.post("/settings/helpline_toggle")
def toggle_helpline(data: HelplineToggleBody):
    with get_db() as (conn, cursor):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ('helpline_enabled', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()
        """, (str(data.enabled).lower(), str(data.enabled).lower()))
    return {"status": "updated", "helpline_enabled": data.enabled}