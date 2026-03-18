from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from security import hash_password, verify_password, create_access_token
from auth_guard import role_required, get_current_user
import csv, io, json

router = APIRouter()

# ── GET /me — verify token and return current user ──────────────
@router.get("/me")
@router.get("/api/me")
def get_me(current_user: dict = Depends(get_current_user)):
    import json as _j
    # Token uses 'sub' for user id
    uid = current_user.get("user_id") or current_user.get("sub")
    if not uid:
        raise HTTPException(401, "Invalid token")
    with get_db() as (conn, cursor):
        cursor.execute(
            "SELECT id, name, email, role, modules, is_active FROM users WHERE id=%s",
            (int(uid),)
        )
        u = cursor.fetchone()
    if not u:
        raise HTTPException(404, "User not found")
    if not u[5]:
        raise HTTPException(403, "Account deactivated")
    # Parse modules safely
    mods = u[4] if u[4] else []
    if isinstance(mods, str):
        try: mods = _j.loads(mods)
        except: mods = [m.strip() for m in mods.split(',') if m.strip()]
    if not isinstance(mods, list):
        mods = []
    # Super admin always gets all modules
    role = u[3] or ''
    if role in ('super_admin', 'admin', 'founder') and not mods:
        mods = ["dashboard","control_room","tickets","riders","technicians",
                "analytics","users","settings","performance","reports","zones"]
    return {
        "user_id":   u[0],
        "name":      u[1],
        "email":     u[2],
        "role":      role,
        "modules":   mods,
        "is_active": u[5]
    }


# -----------------------------------------------
# ROLES & MODULE PERMISSIONS
# -----------------------------------------------

ROLES = [
    "super_admin","admin","rsa_manager","service_manager",
    "technician","data_analyst","founder","viewer",
]

ROLE_DEFAULT_MODULES = {
    "super_admin":     ["dashboard","tickets","riders","technicians","users","analytics","settings","control_room"],
    "admin":           ["dashboard","tickets","riders","technicians","users","analytics","settings","control_room"],
    "rsa_manager":     ["dashboard","tickets","riders","technicians","control_room","analytics"],
    "service_manager": ["dashboard","tickets","technicians","control_room"],
    "technician":      ["tickets"],
    "data_analyst":    ["dashboard","analytics","tickets","riders"],
    "founder":         ["dashboard","analytics","tickets","riders","technicians","users","settings","control_room"],
    "viewer":          ["dashboard","analytics"],
}

ALL_MODULES = [
    {"key":"dashboard",    "label":"Dashboard"},
    {"key":"control_room", "label":"Control Room"},
    {"key":"tickets",      "label":"Tickets"},
    {"key":"riders",       "label":"Riders"},
    {"key":"technicians",  "label":"Technicians"},
    {"key":"analytics",    "label":"Analytics"},
    {"key":"users",        "label":"User Management"},
    {"key":"settings",     "label":"Settings"},
]

ADMIN_ROLES = ["super_admin","admin","founder"]


# -----------------------------------------------
# Pydantic models
# -----------------------------------------------

class LoginModel(BaseModel):
    email: str
    password: str

class CreateUser(BaseModel):
    name:         str
    email:        str
    password:     str
    role:         str
    position:     Optional[str] = None
    phone:        Optional[str] = None
    joining_date: Optional[str] = None
    kra:          Optional[str] = None
    kpi:          Optional[str] = None
    modules:      Optional[List[str]] = None

class UpdateUser(BaseModel):
    name:           Optional[str]       = None
    position:       Optional[str]       = None
    phone:          Optional[str]       = None
    role:           Optional[str]       = None
    joining_date:   Optional[str]       = None
    leaving_date:   Optional[str]       = None
    is_active:      Optional[bool]      = None
    kra:            Optional[str]       = None
    kpi:            Optional[str]       = None
    responsibility: Optional[str]       = None
    modules:        Optional[List[str]] = None

class ChangePassword(BaseModel):
    user_id:      int
    new_password: str

class SelfChangePassword(BaseModel):
    current_password: str
    new_password:     str


# -----------------------------------------------
# POST /login
# -----------------------------------------------

@router.post("/login")
@router.post("/api/login")
def login(data: LoginModel):
    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT id, name, role, password_hash, is_active, modules
            FROM users WHERE email = %s
        """, (data.email,))
        user = cursor.fetchone()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user[4]:
        raise HTTPException(status_code=403, detail="Account is inactive. Contact your admin.")

    stored = user[3] or ""
    if stored.startswith("$2"):
        if not verify_password(data.password, stored):
            raise HTTPException(status_code=401, detail="Invalid credentials")
    else:
        if data.password != stored:
            raise HTTPException(status_code=401, detail="Invalid credentials")

    modules = user[5]
    if isinstance(modules, str):
        try: modules = json.loads(modules)
        except: modules = []
    modules = modules or ROLE_DEFAULT_MODULES.get(user[2], [])

    token = create_access_token({
        "sub":     str(user[0]),
        "name":    user[1],
        "role":    user[2],
        "modules": modules,
    })

    return {"user_id": user[0], "name": user[1], "role": user[2], "modules": modules, "token": token}


# -----------------------------------------------
# GET /roles
# -----------------------------------------------

@router.get("/roles")
def get_roles():
    return {"roles": ROLES, "modules": ALL_MODULES, "defaults": ROLE_DEFAULT_MODULES}


# -----------------------------------------------
# POST /create_user
# -----------------------------------------------

@router.post("/create_user")
def create_user(user: CreateUser, current_user: dict = Depends(role_required(ADMIN_ROLES))):
    if user.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Choose from: {ROLES}")

    modules = user.modules or ROLE_DEFAULT_MODULES.get(user.role, [])

    with get_db() as (conn, cursor):
        cursor.execute("SELECT id FROM users WHERE email = %s", (user.email,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Email already registered")

        cursor.execute("""
            INSERT INTO users (name, email, password_hash, role, position, phone,
                joining_date, kra, kpi, modules, is_active, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,NOW()) RETURNING id
        """, (user.name, user.email, hash_password(user.password), user.role,
              user.position, user.phone, user.joining_date or None,
              user.kra, user.kpi, json.dumps(modules)))
        new_id = cursor.fetchone()[0]

    return {"status": "user created", "user_id": new_id, "role": user.role}


# -----------------------------------------------
# GET /users
# -----------------------------------------------

@router.get("/users")
def get_users(current_user: dict = Depends(role_required(ADMIN_ROLES))):
    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT id, name, email, role, position, phone,
                   joining_date, leaving_date, is_active,
                   kra, kpi, modules, responsibility, created_at
            FROM users ORDER BY id
        """)
        rows = cursor.fetchall()
    return [_fmt(r) for r in rows]


# -----------------------------------------------
# GET /user/{user_id}
# -----------------------------------------------

@router.get("/user/{user_id}")
def get_user(user_id: int, current_user: dict = Depends(get_current_user)):
    if str(user_id) != current_user.get("sub") and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT id, name, email, role, position, phone,
                   joining_date, leaving_date, is_active,
                   kra, kpi, modules, responsibility, created_at
            FROM users WHERE id = %s
        """, (user_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return _fmt(row)


# -----------------------------------------------
# PUT /update_user/{user_id}
# -----------------------------------------------

@router.put("/update_user/{user_id}")
def update_user(user_id: int, data: UpdateUser, current_user: dict = Depends(role_required(ADMIN_ROLES))):
    with get_db() as (conn, cursor):
        cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")

        updates, params = [], []
        if data.name           is not None: updates.append("name=%s");           params.append(data.name)
        if data.position       is not None: updates.append("position=%s");        params.append(data.position)
        if data.phone          is not None: updates.append("phone=%s");           params.append(data.phone)
        if data.role           is not None:
            if data.role not in ROLES: raise HTTPException(400, "Invalid role")
            updates.append("role=%s"); params.append(data.role)
        if data.joining_date   is not None: updates.append("joining_date=%s");    params.append(data.joining_date or None)
        if data.leaving_date   is not None: updates.append("leaving_date=%s");    params.append(data.leaving_date or None)
        if data.is_active      is not None: updates.append("is_active=%s");       params.append(data.is_active)
        if data.kra            is not None: updates.append("kra=%s");             params.append(data.kra)
        if data.kpi            is not None: updates.append("kpi=%s");             params.append(data.kpi)
        if data.responsibility is not None: updates.append("responsibility=%s");  params.append(data.responsibility)
        if data.modules        is not None: updates.append("modules=%s");         params.append(json.dumps(data.modules))

        if not updates:
            return {"status": "nothing to update"}
        params.append(user_id)
        cursor.execute(f"UPDATE users SET {','.join(updates)} WHERE id=%s", params)

    return {"status": "updated", "user_id": user_id}


# -----------------------------------------------
# POST /change_password  — admin resets any
# -----------------------------------------------

@router.post("/change_password")
def change_password(data: ChangePassword, current_user: dict = Depends(role_required(ADMIN_ROLES))):
    with get_db() as (conn, cursor):
        cursor.execute("SELECT id FROM users WHERE id=%s", (data.user_id,))
        if not cursor.fetchone():
            raise HTTPException(404, "User not found")
        cursor.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                       (hash_password(data.new_password), data.user_id))
    return {"status": "password changed"}


# -----------------------------------------------
# POST /change_my_password  — self-service
# -----------------------------------------------

@router.post("/change_my_password")
def change_my_password(data: SelfChangePassword, current_user: dict = Depends(get_current_user)):
    user_id = int(current_user["sub"])
    with get_db() as (conn, cursor):
        cursor.execute("SELECT password_hash FROM users WHERE id=%s", (user_id,))
        row = cursor.fetchone()
        if not row: raise HTTPException(404, "User not found")
        stored = row[0] or ""
        if stored.startswith("$2"):
            if not verify_password(data.current_password, stored):
                raise HTTPException(401, "Current password is incorrect")
        else:
            if data.current_password != stored:
                raise HTTPException(401, "Current password is incorrect")
        cursor.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                       (hash_password(data.new_password), user_id))
    return {"status": "password changed"}


# -----------------------------------------------
# DELETE /delete_user/{user_id}
# -----------------------------------------------

@router.delete("/delete_user/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(role_required(ADMIN_ROLES))):
    if str(user_id) == current_user.get("sub"):
        raise HTTPException(400, "Cannot delete your own account")
    with get_db() as (conn, cursor):
        cursor.execute("SELECT id FROM users WHERE id=%s", (user_id,))
        if not cursor.fetchone(): raise HTTPException(404, "User not found")
        cursor.execute("DELETE FROM users WHERE id=%s", (user_id,))
    return {"status": "deleted", "user_id": user_id}


# -----------------------------------------------
# GET /export_users  — download CSV
# -----------------------------------------------

@router.get("/export_users")
def export_users(current_user: dict = Depends(role_required(ADMIN_ROLES))):
    with get_db() as (conn, cursor):
        cursor.execute("""
            SELECT id,name,email,role,position,phone,
                   joining_date,leaving_date,is_active,kra,kpi,created_at
            FROM users ORDER BY id
        """)
        rows = cursor.fetchall()

    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["id","name","email","role","position","phone",
                "joining_date","leaving_date","is_active","kra","kpi","created_at"])
    for r in rows:
        w.writerow(r)
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=users_export.csv"})


# -----------------------------------------------
# GET /user_import_template
# -----------------------------------------------

@router.get("/user_import_template")
def user_import_template():
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["name","email","password","role","position","phone","joining_date","kra","kpi"])
    w.writerow(["John Doe","john@triev.in","pass123","rsa_manager","RSA Manager","9999999999","2025-01-01","Manage RSA ops","95% resolution rate"])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=user_import_template.csv"})


# -----------------------------------------------
# POST /import_users  — bulk CSV
# -----------------------------------------------

@router.post("/import_users")
async def import_users(
    file: UploadFile = File(...),
    current_user: dict = Depends(role_required(ADMIN_ROLES))
):
    content = await file.read()
    reader  = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    success, failed, skipped = 0, 0, []

    with get_db() as (conn, cursor):
        for row in reader:
            name     = (row.get("name")     or "").strip()
            email    = (row.get("email")    or "").strip().lower()
            password = (row.get("password") or "").strip()
            role     = (row.get("role")     or "viewer").strip().lower()

            if not name or not email or not password:
                skipped.append({"email": email, "reason": "Missing required fields"}); continue
            if role not in ROLES:
                skipped.append({"email": email, "reason": f"Invalid role: {role}"}); continue
            try:
                cursor.execute("SELECT id FROM users WHERE email=%s", (email,))
                if cursor.fetchone():
                    skipped.append({"email": email, "reason": "Already exists"}); continue
                modules = ROLE_DEFAULT_MODULES.get(role, [])
                cursor.execute("""
                    INSERT INTO users (name,email,password_hash,role,position,phone,
                        joining_date,kra,kpi,modules,is_active,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,NOW())
                """, (name, email, hash_password(password), role,
                      row.get("position","").strip() or None,
                      row.get("phone","").strip() or None,
                      row.get("joining_date","").strip() or None,
                      row.get("kra","").strip() or None,
                      row.get("kpi","").strip() or None,
                      json.dumps(modules)))
                success += 1
            except Exception as e:
                failed += 1
                skipped.append({"email": email, "reason": str(e)})

    return {"status":"import complete","success":success,"failed":failed,"skipped":skipped}


# -----------------------------------------------
# Helper
# -----------------------------------------------

def _fmt(r):
    modules = r[11]
    if isinstance(modules, str):
        try: modules = json.loads(modules)
        except: modules = []
    return {
        "id": r[0], "name": r[1], "email": r[2], "role": r[3],
        "position": r[4], "phone": r[5],
        "joining_date":   str(r[6])  if r[6]  else None,
        "leaving_date":   str(r[7])  if r[7]  else None,
        "is_active":      r[8],
        "kra":            r[9],
        "kpi":            r[10],
        "modules":        modules or ROLE_DEFAULT_MODULES.get(r[3], []),
        "responsibility": r[12],
        "created_at":     str(r[13]) if r[13] else None,
    }