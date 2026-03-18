from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from database import get_db

router = APIRouter()


# -----------------------------------------------
# POST /verify_rider
# Returns full rider profile + open ticket + history
# -----------------------------------------------

@router.post("/verify_rider")
def verify_rider(mobile_no: str):

    with get_db() as (conn, cursor):

        cursor.execute("""
            SELECT
                id, rider_name, mobile_no, chassis_no,
                tl_name, reporting_manager, skip_manager,
                region, balance, vehicle_issue_date, rider_status
            FROM riders_master
            WHERE mobile_no = %s
        """, (mobile_no,))

        rider = cursor.fetchone()
        if not rider:
            raise HTTPException(status_code=404, detail="Mobile number not registered")

        rider_id = rider[0]

        # Full open ticket
        cursor.execute("""
            SELECT
                t.rsa_ticket_id, t.issue_category, t.issue_note, t.region,
                t.ticket_status, t.technician_name, t.calling_mobile_no,
                t.latitude, t.longitude, t.ticket_datetime,
                t.assigned_at, t.closed_at, t.resolution_note,
                t.tech_id, tk.mobile AS tech_mobile
            FROM rsa_ticket_master t
            LEFT JOIN technicians tk ON tk.tech_id = t.tech_id
            WHERE t.rider_id = %s AND t.ticket_status != 'CLOSED'
            ORDER BY t.ticket_datetime DESC LIMIT 1
        """, (rider_id,))
        open_row = cursor.fetchone()

        # Full history
        cursor.execute("""
            SELECT
                rsa_ticket_id, issue_category, issue_note, region,
                ticket_status, technician_name, ticket_datetime,
                closed_at, resolution_note, calling_mobile_no,
                latitude, longitude
            FROM rsa_ticket_master
            WHERE rider_id = %s
            ORDER BY ticket_datetime DESC
        """, (rider_id,))
        history_rows = cursor.fetchall()

    open_ticket = None
    if open_row:
        open_ticket = {
            "ticket_id":       open_row[0],
            "issue":           open_row[1],
            "note":            open_row[2],
            "region":          open_row[3],
            "status":          open_row[4],
            "technician":      open_row[5],
            "calling_mobile":  open_row[6],
            "latitude":        float(open_row[7]) if open_row[7] else None,
            "longitude":       float(open_row[8]) if open_row[8] else None,
            "ticket_time":     str(open_row[9])  if open_row[9]  else None,
            "assigned_at":     str(open_row[10]) if open_row[10] else None,
            "closed_at":       str(open_row[11]) if open_row[11] else None,
            "resolution_note": open_row[12],
            "tech_mobile":     open_row[14] or "",
        }

    ticket_history = [
        {
            "ticket_id":       r[0],
            "issue":           r[1],
            "note":            r[2],
            "region":          r[3],
            "status":          r[4],
            "technician":      r[5],
            "ticket_time":     str(r[6])  if r[6]  else None,
            "closed_at":       str(r[7])  if r[7]  else None,
            "resolution_note": r[8],
            "calling_mobile":  r[9],
            "latitude":        float(r[10]) if r[10] else None,
            "longitude":       float(r[11]) if r[11] else None,
        }
        for r in history_rows
    ]

    return {
        "rider_id":           rider[0],
        "rider_name":         rider[1],
        "mobile_no":          rider[2],
        "chassis_no":         rider[3],
        "tl_name":            rider[4],
        "reporting_manager":  rider[5],
        "skip_manager":       rider[6],
        "region":             rider[7],
        "balance":            rider[8],
        "vehicle_issue_date": str(rider[9]) if rider[9] else None,
        "rider_status":       rider[10],
        "open_ticket":        open_ticket,
        "ticket_history":     ticket_history,
        "total_tickets":      len(ticket_history),
    }


# -----------------------------------------------
# POST /update_ticket  — edit open ticket fields
# -----------------------------------------------

@router.post("/update_ticket")
def update_ticket(
    ticket_id:      str,
    region:         Optional[str]   = None,
    issue_category: Optional[str]   = None,
    issue_note:     Optional[str]   = None,
    calling_mobile: Optional[str]   = None,
    latitude:       Optional[float] = None,
    longitude:      Optional[float] = None,
):
    with get_db() as (conn, cursor):

        cursor.execute(
            "SELECT ticket_status FROM rsa_ticket_master WHERE rsa_ticket_id = %s",
            (ticket_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if row[0] == 'CLOSED':
            raise HTTPException(status_code=400, detail="Cannot edit a closed ticket")

        updates, params = [], []
        if region         is not None: updates.append("region = %s");            params.append(region)
        if issue_category is not None: updates.append("issue_category = %s");    params.append(issue_category)
        if issue_note     is not None: updates.append("issue_note = %s");         params.append(issue_note)
        if calling_mobile is not None: updates.append("calling_mobile_no = %s"); params.append(calling_mobile)
        if latitude       is not None: updates.append("latitude = %s");           params.append(latitude)
        if longitude      is not None: updates.append("longitude = %s");          params.append(longitude)

        if not updates:
            return {"status": "nothing to update"}

        params.append(ticket_id)
        cursor.execute(
            f"UPDATE rsa_ticket_master SET {', '.join(updates)} WHERE rsa_ticket_id = %s",
            params
        )

    return {"status": "updated", "ticket_id": ticket_id}


# -----------------------------------------------
# GET /riders
# -----------------------------------------------

@router.get("/riders")
def get_riders(
    search: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
):
    with get_db() as (conn, cursor):
        query = """
            SELECT id, rider_name, mobile_no, chassis_no,
                   tl_name, reporting_manager, region, rider_status
            FROM riders_master WHERE 1=1
        """
        params = []
        if search:
            query += " AND (rider_name ILIKE %s OR mobile_no ILIKE %s)"
            params += [f"%{search}%", f"%{search}%"]
        if region:
            query += " AND region = %s"
            params.append(region)
        query += " ORDER BY rider_name"
        cursor.execute(query, params)
        rows = cursor.fetchall()

    return [
        {
            "id": r[0], "name": r[1], "mobile": r[2], "chassis_no": r[3],
            "tl_name": r[4], "reporting_manager": r[5],
            "region": r[6], "rider_status": r[7],
        }
        for r in rows
    ]