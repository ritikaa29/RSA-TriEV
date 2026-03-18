from fastapi import APIRouter, Query
from typing import Optional
from database import get_db

router = APIRouter()


# -----------------------------------------------
# GET /analytics  — admin dashboard data
# -----------------------------------------------

@router.get("/analytics")
def get_analytics(
    days: int = Query(30, description="Look-back window in days")
):
    with get_db() as (conn, cursor):

        # --- Summary counts ---
        cursor.execute("""
            SELECT
                COUNT(*) FILTER (WHERE ticket_status = 'OPEN')     AS open_now,
                COUNT(*) FILTER (WHERE ticket_status = 'ASSIGNED') AS assigned_now,
                COUNT(*) FILTER (
                    WHERE ticket_status = 'CLOSED'
                      AND DATE(closed_at) = CURRENT_DATE
                )                                                   AS closed_today,
                COUNT(*) FILTER (
                    WHERE ticket_datetime >= NOW() - INTERVAL '1 day' * %s
                )                                                   AS total_period,
                ROUND(AVG(
                    EXTRACT(EPOCH FROM (closed_at - ticket_datetime)) / 60
                ) FILTER (
                    WHERE ticket_status = 'CLOSED'
                      AND closed_at >= NOW() - INTERVAL '1 day' * %s
                ))                                                  AS avg_resolution_min
            FROM rsa_ticket_master
        """, (days, days))
        summary = cursor.fetchone()

        # --- Issue breakdown ---
        cursor.execute("""
            SELECT issue_category, COUNT(*) AS cnt
            FROM rsa_ticket_master
            WHERE ticket_datetime >= NOW() - INTERVAL '1 day' * %s
            GROUP BY issue_category
            ORDER BY cnt DESC
        """, (days,))
        issue_rows = cursor.fetchall()

        # --- Region load (all open/assigned) ---
        cursor.execute("""
            SELECT region, COUNT(*) AS cnt
            FROM rsa_ticket_master
            WHERE ticket_status IN ('OPEN', 'ASSIGNED')
            GROUP BY region
            ORDER BY cnt DESC
        """)
        region_rows = cursor.fetchall()

        # --- Daily volume (last 7 days) ---
        cursor.execute("""
            SELECT
                DATE(ticket_datetime) AS day,
                COUNT(*)              AS cnt
            FROM rsa_ticket_master
            WHERE ticket_datetime >= NOW() - INTERVAL '7 days'
            GROUP BY day
            ORDER BY day ASC
        """)
        daily_rows = cursor.fetchall()

        # --- Technician performance ---
        cursor.execute("""
            SELECT
                t.name,
                COUNT(m.rsa_ticket_id)                              AS total,
                COUNT(m.rsa_ticket_id) FILTER (
                    WHERE m.ticket_status = 'CLOSED'
                )                                                   AS completed,
                ROUND(AVG(
                    EXTRACT(EPOCH FROM (m.closed_at - m.ticket_datetime)) / 60
                ) FILTER (WHERE m.ticket_status = 'CLOSED'))        AS avg_min
            FROM technicians t
            LEFT JOIN rsa_ticket_master m
                   ON m.tech_id = t.tech_id
                  AND m.ticket_datetime >= NOW() - INTERVAL '1 day' * %s
            GROUP BY t.tech_id, t.name
            ORDER BY completed DESC NULLS LAST
        """, (days,))
        tech_rows = cursor.fetchall()

        # --- Available technician count ---
        cursor.execute("""
            SELECT COUNT(*) FROM technicians WHERE status = 'available'
        """)
        avail_count = cursor.fetchone()[0]

    return {
        "summary": {
            "open_now":           summary[0] or 0,
            "assigned_now":       summary[1] or 0,
            "closed_today":       summary[2] or 0,
            "total_period":       summary[3] or 0,
            "avg_resolution_min": int(summary[4]) if summary[4] else None,
            "available_techs":    avail_count or 0,
            "period_days":        days,
        },
        "issue_breakdown": [
            {"issue": r[0], "count": r[1]} for r in issue_rows
        ],
        "region_load": [
            {"region": r[0], "count": r[1]} for r in region_rows
        ],
        "daily_volume": [
            {"date": str(r[0]), "count": r[1]} for r in daily_rows
        ],
        "technician_performance": [
            {
                "name":        r[0],
                "total":       r[1] or 0,
                "completed":   r[2] or 0,
                "avg_min":     int(r[3]) if r[3] else None,
            }
            for r in tech_rows
        ],
    }