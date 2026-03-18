import sys
import os
import logging

# Allow import from parent folder (backend/)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from database import get_db

# -----------------------------------------------
# Logging
# -----------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("sync_riders")

# -----------------------------------------------
# Google Sheets Auth
# -----------------------------------------------

CREDENTIALS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "google_credentials.json"
)

SHEET_NAME = "RSA Tickets Active Rider Data Base"

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]


def get_sheet_records() -> list:
    creds  = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, SCOPE)
    client = gspread.authorize(creds)
    sheet  = client.open(SHEET_NAME).sheet1
    records = sheet.get_all_records()
    logger.info(f"Fetched {len(records)} rows from Google Sheet")
    return records


# -----------------------------------------------
# Helpers
# -----------------------------------------------

def clean_mobile(raw) -> str:
    """Strip decimals added by Sheets (9876543210.0 → 9876543210)."""
    return str(raw).split(".")[0].strip()


def safe_str(val) -> str:
    return str(val).strip() if val not in (None, "", "None") else None


# -----------------------------------------------
# Main sync
# -----------------------------------------------

def sync():
    logger.info("Starting Rider Sync...")

    try:
        records = get_sheet_records()
    except Exception as e:
        logger.error(f"Failed to fetch Google Sheet: {e}")
        raise

    if not records:
        logger.warning("Sheet returned 0 rows — aborting sync")
        return

    success = 0
    failed  = 0
    skipped = 0

    with get_db() as (conn, cursor):

        for row in records:

            rider_id = row.get("RiderId")

            # Skip rows with no ID
            if not rider_id:
                skipped += 1
                continue

            mobile = clean_mobile(row.get("MobileNo", ""))

            # Accept both spelling variants in the sheet header
            reporting_manager = safe_str(
                row.get("Reporting Manager") or row.get("Reproting Manager")
            )

            # Parse balance — sheet stores as number or empty
            balance_raw = row.get("Balance", "")
            try:
                balance = float(str(balance_raw).split(".")[0]) if str(balance_raw).strip() not in ("", "None") else None
            except (ValueError, TypeError):
                balance = None

            # Parse vehicle issue date — sheet stores as "14/3/2026 14:29:37" or "14/3/2026"
            vid_raw = safe_str(row.get("Vehicle Issue Date") or row.get("vehicle issue date"))
            vehicle_issue_date = None
            if vid_raw:
                try:
                    from datetime import datetime
                    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d"):
                        try:
                            vehicle_issue_date = datetime.strptime(vid_raw, fmt).date()
                            break
                        except ValueError:
                            continue
                except Exception:
                    vehicle_issue_date = None

            rider_status = safe_str(row.get("Rider Status") or row.get("rider status")) or "Active"

            try:
                cursor.execute("""
                    INSERT INTO riders_master (
                        id,
                        rider_name,
                        mobile_no,
                        chassis_no,
                        tl_name,
                        reporting_manager,
                        skip_manager,
                        region,
                        balance,
                        vehicle_issue_date,
                        rider_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)

                    ON CONFLICT (id)
                    DO UPDATE SET
                        rider_name         = EXCLUDED.rider_name,
                        mobile_no          = EXCLUDED.mobile_no,
                        chassis_no         = EXCLUDED.chassis_no,
                        tl_name            = EXCLUDED.tl_name,
                        reporting_manager  = EXCLUDED.reporting_manager,
                        skip_manager       = EXCLUDED.skip_manager,
                        region             = EXCLUDED.region,
                        balance            = EXCLUDED.balance,
                        vehicle_issue_date = EXCLUDED.vehicle_issue_date,
                        rider_status       = EXCLUDED.rider_status
                """, (
                    rider_id,
                    safe_str(row.get("Rider Name")),
                    mobile,
                    safe_str(row.get("Chassis No")),
                    safe_str(row.get("TL Name")),
                    reporting_manager,
                    safe_str(row.get("Skip Manager1")),
                    safe_str(row.get("Region")),
                    balance,
                    vehicle_issue_date,
                    rider_status,
                ))

                success += 1

            except Exception as e:
                failed += 1
                logger.error(f"Failed row RiderId={rider_id} | Error: {e}")
                logger.debug(f"Row data: {row}")

    # get_db() commits automatically on exit
    logger.info("Sync complete")
    logger.info(f"  Success : {success}")
    logger.info(f"  Failed  : {failed}")
    logger.info(f"  Skipped : {skipped}")

    if failed:
        logger.warning(f"{failed} row(s) failed — check logs above")


# -----------------------------------------------
# Entry point
# -----------------------------------------------

if __name__ == "__main__":
    sync()