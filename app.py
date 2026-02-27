"""
School Attendance System — PostgreSQL Backend
"""

from flask import Flask, request, jsonify, send_from_directory, session, redirect
from flask_cors import CORS
from authlib.integrations.flask_client import OAuth
from functools import wraps
import psycopg2
import psycopg2.extras
from datetime import datetime
import os

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

# ── Google OAuth ──
oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

SUPERADMIN_EMAIL = "ccuneo@mizzentop.org"

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def fa(cursor):
    """fetchall as list of dicts"""
    return [dict(row) for row in cursor.fetchall()]


def fo(cursor):
    """fetchone as dict or None"""
    row = cursor.fetchone()
    return dict(row) if row else None


# ── Auth decorators ──

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_email"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_email"):
            return redirect("/login")
        if not session.get("is_superadmin"):
            return redirect("/?error=unauthorized")
        return f(*args, **kwargs)
    return decorated


def people_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_email"):
            return redirect("/login")
        if not session.get("is_superadmin") and not session.get("can_manage_people"):
            return redirect("/?error=unauthorized")
        return f(*args, **kwargs)
    return decorated


# ============================================
# STARTUP — create tables if missing
# ============================================

def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mcard_charges (
                    charge_id   SERIAL PRIMARY KEY,
                    student_id  INTEGER NOT NULL,
                    charge_date TEXT NOT NULL,
                    quantity    INTEGER NOT NULL DEFAULT 1,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dismissal_today (
                    dismissal_id  SERIAL PRIMARY KEY,
                    student_id    INTEGER NOT NULL,
                    plan_date     TEXT NOT NULL,
                    bus_route     TEXT,
                    activity      TEXT,
                    ends_in       TEXT DEFAULT 'homeroom',
                    elective_name TEXT,
                    notes         TEXT,
                    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_id, plan_date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS electives (
                    elective_id SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    active      INTEGER DEFAULT 1
                )
            """)
            cur.execute("SELECT COUNT(*) FROM electives")
            if cur.fetchone()[0] == 0:
                for name in ["Art","Music","PE","Library","Technology",
                             "Drama","Spanish","French","Mandarin","STEM"]:
                    cur.execute("INSERT INTO electives (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_dismissal (
                    dismissal_id   SERIAL PRIMARY KEY,
                    student_id     INTEGER NOT NULL,
                    dismissal_date TEXT NOT NULL,
                    dismissal_type TEXT NOT NULL,
                    destination    TEXT DEFAULT '',
                    notes          TEXT DEFAULT '',
                    is_override    INTEGER DEFAULT 0,
                    recorded_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_id, dismissal_date)
                )
            """)
        conn.commit()
        print("DB init OK")
    except Exception as e:
        conn.rollback()
        print(f"DB init error: {e}")
    finally:
        conn.close()


# ============================================
# PAGE ROUTES
# ============================================

@app.route("/")
@login_required
def index():
    return send_from_directory(".", "home.html")

@app.route("/login")
def login():
    if session.get("user_email"):
        return redirect("/")
    return send_from_directory(".", "login.html")

@app.route("/auth/google")
def auth_google():
    return google.authorize_redirect("https://admin.mizzentopdayschool.org/auth/callback")

@app.route("/auth/callback")
def auth_callback():
    try:
        token = google.authorize_access_token()
        user_info = token.get("userinfo")
        email = user_info.get("email", "").lower()
        if not email.endswith("@mizzentop.org"):
            return redirect("/login?error=domain")
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM staff WHERE email = %s AND status = %s", (email, "active"))
                staff = fo(cur)
        finally:
            conn.close()
        if not staff and email != SUPERADMIN_EMAIL:
            return redirect("/login?error=notfound")
        session["user_email"] = email
        session["user_name"] = user_info.get("name", "")
        session["is_superadmin"] = (email == SUPERADMIN_EMAIL)
        if staff:
            session["can_record_attendance"] = bool(staff.get("can_record_attendance"))
            session["can_manage_billing"]    = bool(staff.get("can_manage_billing"))
            session["can_manage_people"]     = bool(staff.get("can_manage_people"))
            session["user_role"]             = staff.get("role")
        else:
            session["can_record_attendance"] = True
            session["can_manage_billing"]    = True
            session["can_manage_people"]     = True
            session["user_role"]             = "superadmin"
        return redirect("/")
    except Exception as e:
        print(f"Auth error: {e}")
        return redirect("/login?error=auth")

@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/api/session")
def get_session():
    if not session.get("user_email"):
        return jsonify({"logged_in": False}), 401
    return jsonify({
        "logged_in": True,
        "email": session.get("user_email"),
        "name": session.get("user_name"),
        "is_superadmin": session.get("is_superadmin", False),
        "can_record_attendance": session.get("can_record_attendance", False),
        "can_manage_billing": session.get("can_manage_billing", False),
        "can_manage_people": session.get("can_manage_people", False),
        "role": session.get("user_role"),
    })

@app.route("/logo.svg")
def serve_logo():
    return send_from_directory(".", "logo.svg")

@app.route("/attendance")
@login_required
def attendance():
    return send_from_directory(".", "attendance_form.html")

@app.route("/dismissal")
@login_required
def dismissal():
    return send_from_directory(".", "dismissal_planner.html")

@app.route("/dismissal-staff")
@login_required
def dismissal_staff():
    return send_from_directory(".", "dismissal_staff_view.html")

@app.route("/bus-dashboard")
@login_required
def bus_dashboard():
    return send_from_directory(".", "bus_dashboard.html")

@app.route("/mcard")
@login_required
def mcard():
    return send_from_directory(".", "mcard_tracker.html")

@app.route("/students")
@people_required
def students():
    return send_from_directory(".", "students.html")

@app.route("/staff")
@app.route("/people")
@people_required
def people():
    return send_from_directory(".", "people.html")

@app.route("/api/test")
def test():
    return jsonify({"status": "ok", "db": "PostgreSQL"})


# ============================================
# CORE ATTENDANCE API
# ============================================

@app.route("/api/programs")
def get_programs():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT program_id, program_name, billing_rate, billing_type FROM programs WHERE status='active' ORDER BY program_name")
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/staff")
def get_staff():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT staff_id, first_name || ' ' || last_name as name, role FROM staff WHERE status='active' AND can_record_attendance=1 ORDER BY last_name, first_name")
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/enrollments/<int:program_id>")
def get_enrollments(program_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT e.enrollment_id, e.student_id,
                       s.first_name || ' ' || s.last_name as student_name,
                       s.first_name, s.last_name, s.grade, e.program_id, p.program_name
                FROM enrollments e
                JOIN students s ON e.student_id = s.student_id
                JOIN programs p ON e.program_id = p.program_id
                WHERE e.program_id=%s AND e.status='active' AND s.status='active'
                ORDER BY s.last_name, s.first_name
            """, (program_id,))
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/attendance", methods=["POST"])
def save_attendance():
    data = request.json
    program_id = data.get("program_id")
    date = data.get("date")
    staff_id = data.get("staff_id")
    attendance_data = data.get("attendance", {})
    if not all([program_id, date, staff_id, attendance_data]):
        return jsonify({"error": "Missing required fields"}), 400
    conn = get_db_connection()
    saved_count = 0
    errors = []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for student_id, record in attendance_data.items():
                cur.execute("SELECT enrollment_id FROM enrollments WHERE student_id=%s AND program_id=%s AND status='active'", (student_id, program_id))
                enrollment = fo(cur)
                if not enrollment:
                    errors.append(f"No enrollment for student {student_id}")
                    continue
                cur.execute("""
                    INSERT INTO attendance_records (enrollment_id, attendance_date, status, recorded_by, notes)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (enrollment_id, attendance_date) DO UPDATE SET
                        status=EXCLUDED.status, notes=EXCLUDED.notes,
                        recorded_by=EXCLUDED.recorded_by, recorded_at=CURRENT_TIMESTAMP
                """, (enrollment["enrollment_id"], date, record["status"], staff_id, record.get("note","")))
                saved_count += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "saved_count": saved_count, "errors": errors})

@app.route("/api/attendance/<int:program_id>/<date>")
def get_attendance(program_id, date):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT a.attendance_id, a.enrollment_id, e.student_id,
                       s.first_name || ' ' || s.last_name as student_name,
                       a.status, a.notes, a.recorded_at,
                       st.first_name || ' ' || st.last_name as recorded_by_name
                FROM attendance_records a
                JOIN enrollments e ON a.enrollment_id=e.enrollment_id
                JOIN students s ON e.student_id=s.student_id
                JOIN staff st ON a.recorded_by=st.staff_id
                WHERE e.program_id=%s AND a.attendance_date=%s
                ORDER BY s.last_name, s.first_name
            """, (program_id, date))
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/summary/<int:program_id>/<start_date>/<end_date>")
def get_summary(program_id, start_date, end_date):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.student_id, s.first_name || ' ' || s.last_name as student_name,
                       COUNT(*) as total_days,
                       SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) as present_count,
                       SUM(CASE WHEN a.status='absent'  THEN 1 ELSE 0 END) as absent_count,
                       SUM(CASE WHEN a.status='excused' THEN 1 ELSE 0 END) as excused_count
                FROM attendance_records a
                JOIN enrollments e ON a.enrollment_id=e.enrollment_id
                JOIN students s ON e.student_id=s.student_id
                WHERE e.program_id=%s AND a.attendance_date BETWEEN %s AND %s
                GROUP BY s.student_id, s.first_name, s.last_name
                ORDER BY s.last_name, s.first_name
            """, (program_id, start_date, end_date))
            return jsonify(fa(cur))
    finally:
        conn.close()


# ============================================
# M CARD
# ============================================

@app.route("/api/mcard/students")
def get_mcard_students():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT student_id, first_name, last_name, grade FROM students WHERE status='active' ORDER BY last_name, first_name")
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/mcard/charges")
def get_mcard_charges():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT m.charge_id, m.student_id,
                       s.first_name || ' ' || s.last_name AS student_name,
                       s.grade, m.charge_date, m.quantity, m.recorded_at
                FROM mcard_charges m JOIN students s ON m.student_id=s.student_id
                ORDER BY m.charge_date DESC, m.recorded_at DESC
            """)
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/mcard/charges", methods=["POST"])
def add_mcard_charge():
    data = request.json
    student_id  = data.get("student_id")
    charge_date = data.get("charge_date","")
    quantity    = int(data.get("quantity",1))
    if quantity not in [1,2]:
        return jsonify({"error":"Quantity must be 1 or 2"}),400
    if not student_id or not charge_date:
        return jsonify({"error":"Missing student_id or charge_date"}),400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT student_id FROM students WHERE student_id=%s AND status='active'",(student_id,))
            if not fo(cur):
                return jsonify({"error":"Student not found"}),404
            cur.execute("INSERT INTO mcard_charges (student_id,charge_date,quantity) VALUES (%s,%s,%s) RETURNING charge_id",(student_id,charge_date,quantity))
            charge_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success":True,"charge_id":charge_id})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/mcard/charges/<int:charge_id>", methods=["DELETE"])
def delete_mcard_charge(charge_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mcard_charges WHERE charge_id=%s",(charge_id,))
        conn.commit()
        return jsonify({"success":True})
    finally:
        conn.close()


# ============================================
# ELECTIVES
# ============================================

@app.route("/api/electives")
def get_electives():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT elective_id, name FROM electives WHERE active=1 ORDER BY name")
            return jsonify(fa(cur))
    finally:
        conn.close()


# ============================================
# DISMISSAL TODAY (staff view)
# ============================================

@app.route("/api/dismissal/today")
def get_dismissal_today():
    date_param   = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    grade_filter = request.args.get("grade")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) FROM daily_dismissal WHERE dismissal_date=%s",(date_param,))
            filled = cur.fetchone()[0]
            source = "today" if filled > 0 else "default"
            if source == "today":
                grade_clause = "AND s.grade=%s" if grade_filter else ""
                params = [date_param]
                if grade_filter: params.append(grade_filter)
                cur.execute(f"""
                    SELECT s.student_id AS id, s.first_name AS "firstName",
                           s.last_name AS "lastName", s.grade,
                           d.dismissal_type AS dismissal, d.destination AS activity,
                           'homeroom' AS "endsIn", NULL AS elective, d.notes
                    FROM students s
                    LEFT JOIN daily_dismissal d ON d.student_id=s.student_id AND d.dismissal_date=%s
                    WHERE s.status='active' {grade_clause}
                    ORDER BY s.last_name, s.first_name
                """, params)
            else:
                from datetime import date as dt_date
                day_col_map = {"Monday":"dismissal_mon","Tuesday":"dismissal_tue",
                               "Wednesday":"dismissal_wed","Thursday":"dismissal_thu","Friday":"dismissal_fri"}
                day_name = dt_date.fromisoformat(date_param).strftime("%A")
                col = day_col_map.get(day_name,"dismissal_mon")
                grade_clause = "AND grade=%s" if grade_filter else ""
                params = [grade_filter] if grade_filter else []
                cur.execute(f"""
                    SELECT student_id AS id, first_name AS "firstName", last_name AS "lastName", grade,
                           {col} AS dismissal, NULL AS activity,
                           'homeroom' AS "endsIn", NULL AS elective, NULL AS notes
                    FROM students WHERE status='active' {grade_clause}
                    ORDER BY last_name, first_name
                """, params)
            rows = fa(cur)
    finally:
        conn.close()

    from datetime import date as dt_date
    day_name = dt_date.fromisoformat(date_param).strftime("%A")
    LOWER  = {"1","2","3","4"}
    MIDDLE = {"5","6","7","8"}
    def calc_ends_in(grade):
        g = str(grade or "").strip()
        if g in LOWER  and day_name=="Tuesday":  return "elective","Elective"
        if g in MIDDLE and day_name=="Tuesday":  return "elective","Advisory"
        if g in MIDDLE and day_name=="Thursday": return "elective","Elective"
        return "homeroom", None

    students = []
    for r in rows:
        r["name"] = f"{r['firstName']} {r['lastName']}"
        ends_in, elective = calc_ends_in(r.get("grade"))
        r["endsIn"]   = ends_in
        r["elective"] = elective
        students.append(r)
    return jsonify({"date":date_param,"source":source,"day":day_name,"students":students})

@app.route("/api/dismissal/today", methods=["POST"])
def save_dismissal_today():
    data = request.json
    plan_date = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    records   = data.get("records",[])
    if not records:
        return jsonify({"error":"No records"}),400
    conn = get_db_connection()
    saved=0; errors=[]
    try:
        with conn.cursor() as cur:
            for rec in records:
                sid = rec.get("student_id")
                if not sid: errors.append("Missing student_id"); continue
                cur.execute("""
                    INSERT INTO dismissal_today (student_id,plan_date,bus_route,activity,ends_in,elective_name,notes,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                    ON CONFLICT (student_id,plan_date) DO UPDATE SET
                        bus_route=EXCLUDED.bus_route, activity=EXCLUDED.activity,
                        ends_in=EXCLUDED.ends_in, elective_name=EXCLUDED.elective_name,
                        notes=EXCLUDED.notes, updated_at=CURRENT_TIMESTAMP
                """, (sid,plan_date,rec.get("bus_route"),rec.get("activity"),
                      rec.get("ends_in","homeroom"),rec.get("elective_name"),rec.get("notes","")))
                saved+=1
        conn.commit()
    except Exception as e:
        conn.rollback(); errors.append(str(e))
    finally:
        conn.close()
    return jsonify({"success":True,"saved":saved,"errors":errors})

@app.route("/api/dismissal/today", methods=["DELETE"])
def clear_dismissal_today():
    plan_date = request.args.get("date")
    if not plan_date:
        return jsonify({"error":"date param required"}),400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dismissal_today WHERE plan_date=%s",(plan_date,))
        conn.commit()
        return jsonify({"success":True,"cleared_date":plan_date})
    finally:
        conn.close()


# ============================================
# DISMISSAL PLANNER (admin)
# ============================================

@app.route("/api/dismissal/attendance/<date>")
def get_dismissal_attendance(date):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT program_id FROM programs WHERE program_name='General Attendance' AND status='active' LIMIT 1")
            program = fo(cur)
            if not program: return jsonify([])
            cur.execute("""
                SELECT e.student_id, a.status
                FROM attendance_records a JOIN enrollments e ON a.enrollment_id=e.enrollment_id
                WHERE e.program_id=%s AND a.attendance_date=%s AND e.status='active'
            """, (program["program_id"],date))
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/dismissal/attendance", methods=["POST"])
def save_dismissal_attendance():
    data = request.json
    student_id = data.get("student_id")
    date       = data.get("date")
    status     = data.get("status","")
    if not student_id or not date:
        return jsonify({"error":"Missing student_id or date"}),400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT program_id FROM programs WHERE program_name='General Attendance' AND status='active' LIMIT 1")
            program = fo(cur)
            if not program: return jsonify({"error":"Program not found"}),404
            cur.execute("SELECT enrollment_id FROM enrollments WHERE student_id=%s AND program_id=%s AND status='active'",(student_id,program["program_id"]))
            enrollment = fo(cur)
            if not enrollment: return jsonify({"error":"Not enrolled"}),404
            enrollment_id = enrollment["enrollment_id"]
            if not status:
                cur.execute("DELETE FROM attendance_records WHERE enrollment_id=%s AND attendance_date=%s",(enrollment_id,date))
            else:
                cur.execute("""
                    INSERT INTO attendance_records (enrollment_id,attendance_date,status,recorded_by,notes)
                    VALUES (%s,%s,%s,1,'')
                    ON CONFLICT (enrollment_id,attendance_date) DO UPDATE SET
                        status=EXCLUDED.status, recorded_at=CURRENT_TIMESTAMP
                """, (enrollment_id,date,status))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/dismissal/students")
def get_dismissal_students():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT student_id, first_name, last_name, grade,
                       dismissal_mon, dismissal_tue, dismissal_wed, dismissal_thu, dismissal_fri, before_care
                FROM students WHERE status='active' ORDER BY last_name, first_name
            """)
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/dismissal/plan/<date>")
def get_dismissal_plan(date):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT dismissal_id,student_id,dismissal_type,destination,notes,is_override,recorded_at FROM daily_dismissal WHERE dismissal_date=%s ORDER BY recorded_at",(date,))
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/dismissal/plan", methods=["POST"])
def save_dismissal_plan():
    data = request.json
    student_id  = data.get("student_id")
    date        = data.get("dismissal_date")
    d_type      = data.get("dismissal_type")
    destination = data.get("destination","")
    notes       = data.get("notes","")
    is_override = data.get("is_override",0)
    if not all([student_id, date, d_type]):
        return jsonify({"error":"Missing fields"}),400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_dismissal (student_id,dismissal_date,dismissal_type,destination,notes,is_override)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (student_id,dismissal_date) DO UPDATE SET
                    dismissal_type=EXCLUDED.dismissal_type, destination=EXCLUDED.destination,
                    notes=EXCLUDED.notes, is_override=EXCLUDED.is_override,
                    recorded_at=CURRENT_TIMESTAMP
            """, (student_id,date,d_type,destination,notes,is_override))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/dismissal/plan/<date>/<int:student_id>", methods=["DELETE"])
def delete_dismissal_plan(date, student_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM daily_dismissal WHERE student_id=%s AND dismissal_date=%s",(student_id,date))
        conn.commit()
        return jsonify({"success":True})
    finally:
        conn.close()

@app.route("/api/dismissal/plan/bulk", methods=["POST"])
def save_dismissal_bulk():
    data        = request.json
    student_ids = data.get("student_ids",[])
    date        = data.get("dismissal_date")
    d_type      = data.get("dismissal_type")
    destination = data.get("destination","")
    if not all([student_ids, date, d_type]):
        return jsonify({"error":"Missing fields"}),400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for sid in student_ids:
                cur.execute("""
                    INSERT INTO daily_dismissal (student_id,dismissal_date,dismissal_type,destination,notes,is_override)
                    VALUES (%s,%s,%s,%s,'',0)
                    ON CONFLICT (student_id,dismissal_date) DO UPDATE SET
                        dismissal_type=EXCLUDED.dismissal_type, destination=EXCLUDED.destination,
                        recorded_at=CURRENT_TIMESTAMP
                """, (sid,date,d_type,destination))
        conn.commit()
        return jsonify({"success":True,"updated":len(student_ids)})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/dismissal/load-defaults", methods=["POST"])
def load_dismissal_defaults():
    data    = request.json
    date    = data.get("date")
    day_key = data.get("day_key")
    if not date or not day_key:
        return jsonify({"error":"Missing date or day_key"}),400
    if day_key not in ["mon","tue","wed","thu","fri"]:
        return jsonify({"error":"Invalid day"}),400
    col = f"dismissal_{day_key}"
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT student_id FROM daily_dismissal WHERE dismissal_date=%s",(date,))
            existing = set(r["student_id"] for r in fa(cur))
            cur.execute(f"SELECT student_id, {col} as default_type FROM students WHERE status='active' AND {col} IS NOT NULL AND {col}!=''")
            students_list = fa(cur)
            inserted = 0
            for s in students_list:
                if s["student_id"] not in existing:
                    dest = "Aftercare" if s["default_type"]=="activity" else ""
                    cur.execute("""
                        INSERT INTO daily_dismissal (student_id,dismissal_date,dismissal_type,destination,notes,is_override)
                        VALUES (%s,%s,%s,%s,'',0) ON CONFLICT DO NOTHING
                    """, (s["student_id"],date,s["default_type"],dest))
                    inserted += 1
        conn.commit()
        return jsonify({"success":True,"inserted":inserted})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/dismissal/buses")
@login_required
def get_bus_dashboard():
    date_param = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) FROM daily_dismissal WHERE dismissal_date=%s",(date_param,))
            filled = cur.fetchone()[0]
            source = "today" if filled > 0 else "default"
            if source == "today":
                cur.execute("""
                    SELECT s.student_id,s.first_name,s.last_name,s.grade,d.destination AS bus_route
                    FROM students s JOIN daily_dismissal d ON d.student_id=s.student_id AND d.dismissal_date=%s
                    WHERE s.status='active' AND d.dismissal_type='bus'
                      AND d.destination IS NOT NULL AND d.destination!=''
                    ORDER BY d.destination, s.last_name, s.first_name
                """, [date_param])
            else:
                from datetime import date as dt_date
                day_col_map = {"Monday":"dismissal_mon","Tuesday":"dismissal_tue",
                               "Wednesday":"dismissal_wed","Thursday":"dismissal_thu","Friday":"dismissal_fri"}
                col = day_col_map.get(dt_date.fromisoformat(date_param).strftime("%A"),"dismissal_mon")
                cur.execute(f"SELECT student_id,first_name,last_name,grade,{col} AS bus_route FROM students WHERE status='active' AND {col}='bus' ORDER BY last_name,first_name")
            rows = fa(cur)
    finally:
        conn.close()

    homeroom_map = {"JPK":"Wipperman","SPK":"Vorolieff","K":"Olsen","1":"Alfonso",
                    "2":"Szeghy","3":"Vales","4":"Oxer / Donnelly","5":"Tucci",
                    "6":"Poon","7":"Ballard","8":"Duthie","--":"—"}
    grouped = {}
    for r in rows:
        route = r["bus_route"] if source=="today" else "Default Bus"
        if route not in grouped: grouped[route] = []
        grouped[route].append({"student_id":r["student_id"],"first_name":r["first_name"],
            "last_name":r["last_name"],"grade":r["grade"],"bus_route":route,
            "homeroom_teacher":homeroom_map.get(r["grade"],"—")})
    buses = [{"route":k,"students":v,"count":len(v)} for k,v in sorted(grouped.items())]
    return jsonify({"buses":buses,"source":source,"date":date_param})


# ============================================
# STUDENTS
# ============================================

@app.route("/api/students")
@login_required
def get_students_list():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT student_id,first_name,last_name,grade,status,
                       date_of_birth,email,phone,address,
                       emergency_contact_name,emergency_contact_phone,
                       enrollment_date,notes,before_care,
                       dismissal_mon,dismissal_tue,dismissal_wed,dismissal_thu,dismissal_fri
                FROM students ORDER BY last_name, first_name
            """)
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/students/<int:student_id>", methods=["PUT"])
@people_required
def update_student(student_id):
    data = request.json
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE students SET
                    first_name=%s, last_name=%s, grade=%s, status=%s,
                    date_of_birth=%s, email=%s, phone=%s, address=%s,
                    emergency_contact_name=%s, emergency_contact_phone=%s,
                    enrollment_date=%s, notes=%s, before_care=%s,
                    dismissal_mon=%s, dismissal_tue=%s, dismissal_wed=%s,
                    dismissal_thu=%s, dismissal_fri=%s,
                    updated_at=CURRENT_TIMESTAMP
                WHERE student_id=%s
            """, (data.get("first_name"),data.get("last_name"),data.get("grade"),data.get("status"),
                  data.get("date_of_birth"),data.get("email"),data.get("phone"),data.get("address"),
                  data.get("emergency_contact_name"),data.get("emergency_contact_phone"),
                  data.get("enrollment_date"),data.get("notes"),
                  1 if data.get("before_care") else 0,
                  data.get("dismissal_mon"),data.get("dismissal_tue"),data.get("dismissal_wed"),
                  data.get("dismissal_thu"),data.get("dismissal_fri"),student_id))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()


# ============================================
# STAFF MANAGEMENT
# ============================================

@app.route("/api/people/staff")
@login_required
def get_people_staff():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT staff_id,first_name,last_name,email,role,status,can_record_attendance,can_manage_billing,can_manage_people,title FROM staff ORDER BY last_name,first_name")
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/people/staff", methods=["POST"])
@people_required
def add_people_staff():
    data = request.get_json()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO staff (first_name,last_name,email,role,title,status,
                                   can_record_attendance,can_manage_billing,can_manage_people,hire_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (data.get("first_name"),data.get("last_name"),data.get("email"),
                  data.get("role","staff"),data.get("title",""),data.get("status","active"),
                  data.get("can_record_attendance",0),data.get("can_manage_billing",0),
                  data.get("can_manage_people",0),"2025-09-01"))
        conn.commit()
        return jsonify({"success":True}),201
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/people/staff/<int:staff_id>", methods=["PUT"])
@people_required
def update_people_staff(staff_id):
    data = request.get_json()
    allowed = ["first_name","last_name","email","role","title","status",
               "can_record_attendance","can_manage_billing","can_manage_people"]
    fields = [f + " = %s" for f in allowed if f in data]
    values = [data[f] for f in allowed if f in data]
    if not fields:
        return jsonify({"error":"No fields"}),400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            values.append(staff_id)
            cur.execute("UPDATE staff SET " + ", ".join(fields) + " WHERE staff_id=%s", values)
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/people/staff/<int:staff_id>", methods=["DELETE"])
@people_required
def delete_people_staff(staff_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM staff WHERE staff_id=%s",(staff_id,))
        conn.commit()
        return jsonify({"success":True})
    finally:
        conn.close()


# ============================================
# BACKUP
# ============================================

BACKUP_PASSWORD = "school2026"

@app.route("/backup/download")
def download_backup():
    """Export all key tables as a JSON backup file."""
    if request.args.get("key", "") != BACKUP_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    try:
        backup = {}
        tables = [
            "students", "staff", "programs", "enrollments",
            "attendance_records", "mcard_charges", "electives",
            "daily_dismissal", "dismissal_today"
        ]
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for table in tables:
                cur.execute(f"SELECT * FROM {table}")
                rows = fa(cur)
                # Convert any non-serializable types
                for row in rows:
                    for k, v in row.items():
                        if hasattr(v, 'isoformat'):
                            row[k] = v.isoformat()
                backup[table] = rows
    finally:
        conn.close()

    from flask import Response
    import json
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"mizzentop_backup_{timestamp}.json"
    return Response(
        json.dumps(backup, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ============================================
# STARTUP + RUN
# ============================================

init_db()

if __name__ == "__main__":
    print("Mizzentop Admin — PostgreSQL mode")
    app.run(debug=True, host="0.0.0.0", port=5000)
