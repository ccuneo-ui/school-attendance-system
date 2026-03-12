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
            # Program attendance for billable after-school programs
            cur.execute("""
                CREATE TABLE IF NOT EXISTS program_attendance (
                    record_id      SERIAL PRIMARY KEY,
                    student_id     INTEGER NOT NULL,
                    program_type   TEXT NOT NULL,
                    session_date   TEXT NOT NULL,
                    units          NUMERIC(3,1) NOT NULL DEFAULT 1,
                    teacher        TEXT DEFAULT '',
                    recorded_by    TEXT DEFAULT '',
                    recorded_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_id, program_type, session_date)
                )
            """)
            # Aftercare attendance — billed by check-in/check-out time
            cur.execute("""
                CREATE TABLE IF NOT EXISTS aftercare_attendance (
                    record_id      SERIAL PRIMARY KEY,
                    student_id     INTEGER NOT NULL,
                    session_date   TEXT NOT NULL,
                    checkin_time   TEXT NOT NULL DEFAULT '3:30 PM',
                    pickup_time    TEXT DEFAULT NULL,
                    recorded_by    TEXT DEFAULT '',
                    recorded_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_id, session_date)
                )
            """)
            # Migrate: add checkin_time if missing from existing table
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='aftercare_attendance' AND column_name='checkin_time'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE aftercare_attendance ADD COLUMN checkin_time TEXT NOT NULL DEFAULT '3:30 PM'")
            # Migrate: make pickup_time nullable (was NOT NULL in old schema)
            cur.execute("""
                ALTER TABLE aftercare_attendance ALTER COLUMN pickup_time DROP NOT NULL
            """)
            cur.execute("""
                ALTER TABLE aftercare_attendance ALTER COLUMN pickup_time SET DEFAULT NULL
            """)
            # Billing rates with effective dates for historical accuracy
            cur.execute("""
                CREATE TABLE IF NOT EXISTS billing_rates (
                    rate_id        SERIAL PRIMARY KEY,
                    rate_key       TEXT NOT NULL,
                    rate_value     NUMERIC(10,2) NOT NULL DEFAULT 0,
                    label          TEXT NOT NULL DEFAULT '',
                    unit           TEXT NOT NULL DEFAULT '',
                    effective_from TEXT NOT NULL DEFAULT '2025-09-01',
                    updated_by     TEXT DEFAULT '',
                    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(rate_key, effective_from)
                )
            """)
            # Migrate: if table exists but lacks effective_from column, add it
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='billing_rates' AND column_name='effective_from'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE billing_rates ADD COLUMN effective_from TEXT NOT NULL DEFAULT '2025-09-01'")
                cur.execute("ALTER TABLE billing_rates DROP CONSTRAINT IF EXISTS billing_rates_rate_key_key")
                cur.execute("ALTER TABLE billing_rates ADD CONSTRAINT billing_rates_rate_key_effective_from_key UNIQUE (rate_key, effective_from)")
            # Seed default rates if empty (effective from start of current school year)
            cur.execute("SELECT COUNT(*) FROM billing_rates")
            if cur.fetchone()[0] == 0:
                defaults = [
                    ('aftercare_hourly',    0, 'Aftercare',        'per hour'),
                    ('beforecare_session',  0, 'Before Care',      'per session'),
                    ('mcard_snack',         0, 'M Card Snack',     'per snack'),
                    ('tutoring_session',    0, '1-on-1 Tutoring',  'per session'),
                    ('og_session',          0, 'OG Tutoring',      'per session'),
                    ('homework_hourly',     0, 'Homework Center',  'per hour'),
                ]
                for key, val, label, unit in defaults:
                    cur.execute(
                        "INSERT INTO billing_rates (rate_key, rate_value, label, unit, effective_from) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (key, val, label, unit, '2025-09-01')
                    )
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

@app.route("/program-attendance")
@login_required
def program_attendance():
    return send_from_directory(".", "program_attendance.html")

@app.route("/aftercare")
@login_required
def aftercare():
    return send_from_directory(".", "aftercare_attendance.html")

@app.route("/billing-rates")
@login_required
def billing_rates():
    return send_from_directory(".", "billing_rates.html")

@app.route("/financial-aid")
@login_required
def financial_aid_page():
    return send_from_directory(".", "financial_aid.html")

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
            charge_id = cur.fetchone()["charge_id"]
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
            cur.execute("SELECT COUNT(*) AS c FROM daily_dismissal WHERE dismissal_date=%s",(date_param,))
            filled = cur.fetchone()["c"]
            source = "today" if filled > 0 else "default"
            # Subquery: pull today's General Attendance status per student
            # Used in both branches so the dismissal board can show Present/Absent/Excused next to each name
            att_join = """
                LEFT JOIN (
                    SELECT e.student_id, a.status AS att_status
                    FROM attendance_records a
                    JOIN enrollments e ON a.enrollment_id = e.enrollment_id
                    JOIN programs p    ON e.program_id    = p.program_id
                    WHERE p.program_name = 'General Attendance'
                      AND a.attendance_date = %s
                ) att ON att.student_id = s.student_id
            """

            if source == "today":
                grade_clause = "AND s.grade=%s" if grade_filter else ""
                # params order: dismissal date, attendance date, [grade]
                params = [date_param, date_param] + ([grade_filter] if grade_filter else [])
                cur.execute(f"""
                    SELECT s.student_id AS id, s.first_name AS "firstName",
                           s.last_name AS "lastName", s.grade,
                           d.dismissal_type AS dismissal, d.destination AS activity,
                           'homeroom' AS "endsIn", NULL AS elective, d.notes,
                           att.att_status AS "attStatus"
                    FROM students s
                    LEFT JOIN daily_dismissal d ON d.student_id=s.student_id AND d.dismissal_date=%s
                    {att_join}
                    WHERE s.status='active' {grade_clause}
                    ORDER BY s.last_name, s.first_name
                """, [date_param] + [date_param] + ([grade_filter] if grade_filter else []))
            else:
                from datetime import date as dt_date
                day_col_map = {"Monday":"dismissal_mon","Tuesday":"dismissal_tue",
                               "Wednesday":"dismissal_wed","Thursday":"dismissal_thu","Friday":"dismissal_fri"}
                day_name = dt_date.fromisoformat(date_param).strftime("%A")
                col = day_col_map.get(day_name,"dismissal_mon")
                grade_clause = "AND s.grade=%s" if grade_filter else ""
                params = [date_param] + ([grade_filter] if grade_filter else [])
                cur.execute(f"""
                    SELECT s.student_id AS id, s.first_name AS "firstName",
                           s.last_name AS "lastName", s.grade,
                           s.{col} AS dismissal, NULL AS activity,
                           'homeroom' AS "endsIn", NULL AS elective, NULL AS notes,
                           att.att_status AS "attStatus"
                    FROM students s
                    {att_join}
                    WHERE s.status='active' {grade_clause}
                    ORDER BY s.last_name, s.first_name
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
            cur.execute("SELECT COUNT(*) AS c FROM daily_dismissal WHERE dismissal_date=%s",(date_param,))
            filled = cur.fetchone()["c"]
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
# PROGRAM ATTENDANCE (OG, Homework Center, 1-1 Tutoring)
# ============================================

@app.route("/api/program-attendance/students")
@login_required
def get_program_attendance_students():
    """Get all active + guest students for program attendance"""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT student_id, first_name, last_name, grade, status
                FROM students WHERE status IN ('active','guest')
                ORDER BY last_name, first_name
            """)
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/program-attendance/records")
@login_required
def get_program_attendance_records():
    """Get attendance records filtered by program_type and date range"""
    program_type = request.args.get("program_type")
    start_date   = request.args.get("start_date")
    end_date     = request.args.get("end_date")
    date         = request.args.get("date")
    if not program_type:
        return jsonify({"error":"program_type required"}),400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if date:
                cur.execute("""
                    SELECT pa.record_id, pa.student_id, pa.program_type,
                           pa.session_date, pa.units, pa.teacher, pa.recorded_by, pa.recorded_at,
                           s.first_name, s.last_name, s.grade
                    FROM program_attendance pa
                    JOIN students s ON pa.student_id=s.student_id
                    WHERE pa.program_type=%s AND pa.session_date=%s
                    ORDER BY s.last_name, s.first_name
                """, (program_type, date))
            elif start_date and end_date:
                cur.execute("""
                    SELECT pa.record_id, pa.student_id, pa.program_type,
                           pa.session_date, pa.units, pa.teacher, pa.recorded_by, pa.recorded_at,
                           s.first_name, s.last_name, s.grade
                    FROM program_attendance pa
                    JOIN students s ON pa.student_id=s.student_id
                    WHERE pa.program_type=%s AND pa.session_date BETWEEN %s AND %s
                    ORDER BY pa.session_date DESC, s.last_name, s.first_name
                """, (program_type, start_date, end_date))
            else:
                cur.execute("""
                    SELECT pa.record_id, pa.student_id, pa.program_type,
                           pa.session_date, pa.units, pa.teacher, pa.recorded_by, pa.recorded_at,
                           s.first_name, s.last_name, s.grade
                    FROM program_attendance pa
                    JOIN students s ON pa.student_id=s.student_id
                    WHERE pa.program_type=%s
                    ORDER BY pa.session_date DESC, s.last_name, s.first_name
                """, (program_type,))
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/program-attendance", methods=["POST"])
@login_required
def save_program_attendance():
    """Save or update a program attendance record"""
    data = request.json
    student_id   = data.get("student_id")
    program_type = data.get("program_type")
    session_date = data.get("session_date")
    units        = data.get("units", 1)
    teacher      = data.get("teacher", "")
    recorded_by  = session.get("user_name", "")
    if not all([student_id, program_type, session_date]):
        return jsonify({"error":"Missing required fields"}),400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO program_attendance (student_id, program_type, session_date, units, teacher, recorded_by)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (student_id, program_type, session_date) DO UPDATE SET
                    units=EXCLUDED.units, teacher=EXCLUDED.teacher,
                    recorded_by=EXCLUDED.recorded_by, recorded_at=CURRENT_TIMESTAMP
                RETURNING record_id
            """, (student_id, program_type, session_date, units, teacher, recorded_by))
            record = fo(cur)
        conn.commit()
        return jsonify({"success":True, "record_id": record["record_id"] if record else None})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/program-attendance/<int:record_id>", methods=["DELETE"])
@login_required
def delete_program_attendance(record_id):
    """Remove a program attendance record"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM program_attendance WHERE record_id=%s",(record_id,))
        conn.commit()
        return jsonify({"success":True})
    finally:
        conn.close()

@app.route("/api/program-attendance/summary")
@login_required
def get_program_attendance_summary():
    """Get summary totals by student for a program within a date range"""
    program_type = request.args.get("program_type")
    start_date   = request.args.get("start_date")
    end_date     = request.args.get("end_date")
    if not all([program_type, start_date, end_date]):
        return jsonify({"error":"program_type, start_date, end_date required"}),400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.student_id, s.first_name, s.last_name, s.grade,
                       pa.teacher,
                       COUNT(*) as session_count,
                       SUM(pa.units) as total_units
                FROM program_attendance pa
                JOIN students s ON pa.student_id=s.student_id
                WHERE pa.program_type=%s AND pa.session_date BETWEEN %s AND %s
                GROUP BY s.student_id, s.first_name, s.last_name, s.grade, pa.teacher
                ORDER BY s.last_name, s.first_name
            """, (program_type, start_date, end_date))
            return jsonify(fa(cur))
    finally:
        conn.close()


# ============================================
# AFTERCARE ATTENDANCE
# ============================================

@app.route("/api/aftercare/records")
@login_required
def get_aftercare_records():
    date = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if date:
                cur.execute("""
                    SELECT a.record_id, a.student_id, a.session_date,
                           a.checkin_time, a.pickup_time,
                           a.recorded_by, a.recorded_at,
                           s.first_name, s.last_name, s.grade
                    FROM aftercare_attendance a
                    JOIN students s ON a.student_id=s.student_id
                    WHERE a.session_date=%s
                    ORDER BY a.checkin_time, s.last_name, s.first_name
                """, (date,))
            elif start_date and end_date:
                cur.execute("""
                    SELECT a.record_id, a.student_id, a.session_date,
                           a.checkin_time, a.pickup_time,
                           a.recorded_by, a.recorded_at,
                           s.first_name, s.last_name, s.grade
                    FROM aftercare_attendance a
                    JOIN students s ON a.student_id=s.student_id
                    WHERE a.session_date BETWEEN %s AND %s
                    ORDER BY a.session_date DESC, a.checkin_time, s.last_name
                """, (start_date, end_date))
            else:
                cur.execute("""
                    SELECT a.record_id, a.student_id, a.session_date,
                           a.checkin_time, a.pickup_time,
                           a.recorded_by, a.recorded_at,
                           s.first_name, s.last_name, s.grade
                    FROM aftercare_attendance a
                    JOIN students s ON a.student_id=s.student_id
                    ORDER BY a.session_date DESC, a.checkin_time, s.last_name
                    LIMIT 200
                """)
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/aftercare", methods=["POST"])
@login_required
def save_aftercare():
    data = request.json
    student_id   = data.get("student_id")
    session_date = data.get("session_date")
    checkin_time = data.get("checkin_time")
    pickup_time  = data.get("pickup_time")  # Can be None on check-in
    recorded_by  = session.get("user_name", "")
    if not all([student_id, session_date, checkin_time]):
        return jsonify({"error": "Missing required fields"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO aftercare_attendance (student_id, session_date, checkin_time, pickup_time, recorded_by)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (student_id, session_date) DO UPDATE SET
                    checkin_time=EXCLUDED.checkin_time,
                    pickup_time=EXCLUDED.pickup_time,
                    recorded_by=EXCLUDED.recorded_by,
                    recorded_at=CURRENT_TIMESTAMP
                RETURNING record_id
            """, (student_id, session_date, checkin_time, pickup_time, recorded_by))
            record = fo(cur)
        conn.commit()
        return jsonify({"success": True, "record_id": record["record_id"] if record else None})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/aftercare/<int:record_id>/checkout", methods=["POST"])
@login_required
def checkout_aftercare(record_id):
    """Update just the pickup/checkout time for an existing record"""
    data = request.json
    pickup_time = data.get("pickup_time")
    if not pickup_time:
        return jsonify({"error": "pickup_time required"}), 400
    recorded_by = session.get("user_name", "")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE aftercare_attendance SET pickup_time=%s, recorded_by=%s, recorded_at=CURRENT_TIMESTAMP
                WHERE record_id=%s
            """, (pickup_time, recorded_by, record_id))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/aftercare/<int:record_id>", methods=["DELETE"])
@login_required
def delete_aftercare(record_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM aftercare_attendance WHERE record_id=%s", (record_id,))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


# ============================================
# BILLING RATES (effective-date based)
# ============================================

@app.route("/api/billing/rates")
@login_required
def get_billing_rates():
    """Get current rates (latest effective_from <= today for each key)"""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (rate_key)
                    rate_id, rate_key, rate_value, label, unit,
                    effective_from, updated_by, updated_at
                FROM billing_rates
                WHERE effective_from <= CURRENT_DATE::text
                ORDER BY rate_key, effective_from DESC
            """)
            rows = fa(cur)
            for r in rows:
                if hasattr(r.get('rate_value'), '__float__'):
                    r['rate_value'] = float(r['rate_value'])
                if hasattr(r.get('updated_at'), 'isoformat'):
                    r['updated_at'] = r['updated_at'].isoformat()
            return jsonify(rows)
    finally:
        conn.close()

@app.route("/api/billing/rates/for-date")
@login_required
def get_billing_rates_for_date():
    """Get rates that were active on a specific date (for billing reports)"""
    target_date = request.args.get("date")
    if not target_date:
        return jsonify({"error": "date param required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (rate_key)
                    rate_id, rate_key, rate_value, label, unit,
                    effective_from, updated_by
                FROM billing_rates
                WHERE effective_from <= %s
                ORDER BY rate_key, effective_from DESC
            """, (target_date,))
            rows = fa(cur)
            for r in rows:
                if hasattr(r.get('rate_value'), '__float__'):
                    r['rate_value'] = float(r['rate_value'])
            return jsonify(rows)
    finally:
        conn.close()

@app.route("/api/billing/rates/history")
@login_required
def get_billing_rates_history():
    """Get full rate history for all programs"""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT rate_id, rate_key, rate_value, label, unit,
                       effective_from, updated_by, updated_at
                FROM billing_rates
                ORDER BY rate_key, effective_from DESC
            """)
            rows = fa(cur)
            for r in rows:
                if hasattr(r.get('rate_value'), '__float__'):
                    r['rate_value'] = float(r['rate_value'])
                if hasattr(r.get('updated_at'), 'isoformat'):
                    r['updated_at'] = r['updated_at'].isoformat()
            return jsonify(rows)
    finally:
        conn.close()

@app.route("/api/billing/rates", methods=["POST"])
@login_required
def save_billing_rates():
    """Save new rates with an effective date. Creates new rows — never overwrites old ones."""
    data = request.json
    rates = data.get("rates", [])
    effective_from = data.get("effective_from")
    if not rates or not effective_from:
        return jsonify({"error": "rates and effective_from required"}), 400
    updated_by = session.get("user_name", "")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for r in rates:
                cur.execute("SELECT label, unit FROM billing_rates WHERE rate_key=%s LIMIT 1", (r.get("rate_key"),))
                existing = cur.fetchone()
                label = existing[0] if existing else r.get("rate_key")
                unit = existing[1] if existing else ""
                cur.execute("""
                    INSERT INTO billing_rates (rate_key, rate_value, label, unit, effective_from, updated_by)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (rate_key, effective_from) DO UPDATE SET
                        rate_value=EXCLUDED.rate_value, updated_by=EXCLUDED.updated_by,
                        updated_at=CURRENT_TIMESTAMP
                """, (r.get("rate_key"), r.get("rate_value", 0), label, unit, effective_from, updated_by))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/billing/rates/<int:rate_id>", methods=["DELETE"])
@login_required
def delete_billing_rate(rate_id):
    """Delete a future rate entry"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM billing_rates WHERE rate_id=%s", (rate_id,))
        conn.commit()
        return jsonify({"success": True})
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
            "daily_dismissal", "dismissal_today", "program_attendance",
            "aftercare_attendance", "billing_rates"
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
                        elif isinstance(v, __import__('decimal').Decimal):
                            row[k] = float(v)
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
# BILLING REPORT
# ============================================

@app.route("/billing-report")
@login_required
def billing_report():
    """Billing report page (billing silo)."""
    return send_from_directory(".", "billing_report.html")


@app.route("/api/billing/report")
@login_required
def api_billing_report():
    """
    Monthly billing totals per student, broken out by program.
    Query params: month (1-12), year (e.g. 2026)
    """
    import calendar as cal_mod
    import math
    from datetime import date as dt_date

    try:
        month = int(request.args.get("month", 0))
        year  = int(request.args.get("year",  0))
        if not (1 <= month <= 12) or year < 2020:
            return jsonify({"error": "Invalid month or year"}), 400

        first_day = dt_date(year, month, 1)
        last_day  = dt_date(year, month, cal_mod.monthrange(year, month)[1])

        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                # 1. Billing rates active as of first of month
                cur.execute("""
                    SELECT DISTINCT ON (rate_key)
                           rate_key, rate_value
                    FROM   billing_rates
                    WHERE  effective_from::date <= %s
                    ORDER  BY rate_key, effective_from::date DESC
                """, (first_day,))
                rate_rows = cur.fetchall()
                rates = {r["rate_key"]: float(r["rate_value"]) for r in rate_rows}
                defaults = {
                    "mcard_snack":        1.50,
                    "beforecare_session": 5.00,
                    "aftercare_hourly":  15.00,
                    "og_session":        30.00,
                    "homework_hourly":   15.00,
                    "tutoring_session":  30.00,
                }
                for k, v in defaults.items():
                    rates.setdefault(k, v)

                # 2. M Card charges
                cur.execute("""
                    SELECT student_id, SUM(quantity) AS qty
                    FROM   mcard_charges
                    WHERE  charge_date::date >= %s AND charge_date::date <= %s
                    GROUP  BY student_id
                """, (first_day, last_day))
                mcard = {r["student_id"]: int(r["qty"]) for r in cur.fetchall()}

                # 3. Program attendance (beforecare + tutoring programs)
                cur.execute("""
                    SELECT student_id, program_type, SUM(units) AS total_units
                    FROM   program_attendance
                    WHERE  session_date::date >= %s AND session_date::date <= %s
                    GROUP  BY student_id, program_type
                """, (first_day, last_day))
                prog = {}
                for r in cur.fetchall():
                    sid = r["student_id"]
                    if sid not in prog:
                        prog[sid] = {}
                    prog[sid][r["program_type"]] = float(r["total_units"])

                # 4. Before care distinct days
                cur.execute("""
                    SELECT student_id, COUNT(DISTINCT session_date) AS days
                    FROM   program_attendance
                    WHERE  program_type = 'beforecare'
                      AND  session_date::date >= %s AND session_date::date <= %s
                    GROUP  BY student_id
                """, (first_day, last_day))
                before_days = {r["student_id"]: int(r["days"]) for r in cur.fetchall()}

                # 5. Aftercare — compute hours from pickup_time vs 4:30 PM start
                cur.execute("""
                    SELECT student_id, session_date, pickup_time
                    FROM   aftercare_attendance
                    WHERE  session_date::date >= %s AND session_date::date <= %s
                      AND  pickup_time IS NOT NULL
                """, (first_day, last_day))
                aftercare_hours = {}
                aftercare_days_d = {}

                def pickup_hours(pickup_str):
                    try:
                        # Handles "HH:MM" or "H:MM PM" style
                        s = str(pickup_str).strip().upper()
                        pm_offset = 0
                        if "PM" in s:
                            pm_offset = 12
                            s = s.replace("PM", "").strip()
                        if "AM" in s:
                            s = s.replace("AM", "").strip()
                        parts = s.split(":")
                        h, m = int(parts[0]), int(parts[1])
                        if pm_offset and h != 12:
                            h += pm_offset
                        total_min = h * 60 + m
                        start_min = 16 * 60 + 30  # 4:30 PM
                        elapsed   = max(0, total_min - start_min)
                        # Minimum 1 hour charge; beyond 1 hour billed in 15-min increments
                        if elapsed <= 60:
                            return 1.0
                        else:
                            over = elapsed - 60
                            return 1.0 + math.ceil(over / 15) * 15 / 60.0
                    except Exception:
                        return 0.0

                for r in cur.fetchall():
                    sid = r["student_id"]
                    hrs = pickup_hours(r["pickup_time"])
                    aftercare_hours[sid] = aftercare_hours.get(sid, 0.0) + hrs
                    if sid not in aftercare_days_d:
                        aftercare_days_d[sid] = set()
                    aftercare_days_d[sid].add(str(r["session_date"]))

                # 6. All active students
                cur.execute("""
                    SELECT student_id, first_name, last_name, grade
                    FROM   students
                    WHERE  status = 'active'
                    ORDER  BY grade, last_name, first_name
                """)
                student_rows = cur.fetchall()

        finally:
            conn.close()

        # 7. Build results — only students with activity this month
        results = []
        for s in student_rows:
            sid = s["student_id"]

            mc_qty   = mcard.get(sid, 0)
            bc_days  = before_days.get(sid, 0)
            ac_hours = aftercare_hours.get(sid, 0.0)
            ac_days  = len(aftercare_days_d.get(sid, set()))
            sp       = prog.get(sid, {})

            # Match program_type strings stored by program_attendance.html
            og_units = sp.get("og", 0.0)
            hw_units = sp.get("homework", 0.0)
            oo_units = sp.get("tutoring", 0.0)

            if not any([mc_qty, bc_days, ac_hours, og_units, hw_units, oo_units]):
                continue

            mcard_amt  = mc_qty   * rates["mcard_snack"]
            before_amt = bc_days  * rates["beforecare_session"]
            after_amt  = ac_hours * rates["aftercare_hourly"]
            og_amt     = og_units * rates["og_session"]
            hw_amt     = hw_units * rates["homework_hourly"]
            oo_amt     = oo_units * rates["tutoring_session"]

            results.append({
                "student_id":       sid,
                "name":             f"{s['last_name']}, {s['first_name']}",
                "grade":            str(s["grade"]),
                "mcard":            round(mcard_amt, 2),
                "mcard_qty":        mc_qty,
                "beforecare":       round(before_amt, 2),
                "aftercare":        round(after_amt, 2),
                "og_tutoring":      round(og_amt, 2),
                "homework_center":  round(hw_amt, 2),
                "one_on_one":       round(oo_amt, 2),
                "program_sessions": round(og_units + hw_units + oo_units, 2),
                "care_days":        bc_days + ac_days,
            })

        return jsonify({"month": month, "year": year, "students": results, "rates": rates})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/billing/student-detail")
@login_required
def api_billing_student_detail():
    """Day-by-day charge breakdown for a single student in a given month."""
    import calendar as cal_mod
    import math
    from datetime import date as dt_date

    try:
        student_id = int(request.args.get("student_id", 0))
        month      = int(request.args.get("month", 0))
        year       = int(request.args.get("year",  0))
        if not student_id or not (1 <= month <= 12) or year < 2020:
            return jsonify({"error": "Invalid parameters"}), 400

        first_day = dt_date(year, month, 1)
        last_day  = dt_date(year, month, cal_mod.monthrange(year, month)[1])

        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                # Rates for this month
                cur.execute("""
                    SELECT DISTINCT ON (rate_key) rate_key, rate_value
                    FROM   billing_rates
                    WHERE  effective_from::date <= %s
                    ORDER  BY rate_key, effective_from::date DESC
                """, (first_day,))
                rates = {r["rate_key"]: float(r["rate_value"]) for r in cur.fetchall()}
                defaults = {
                    "mcard_snack": 1.50, "beforecare_session": 5.00,
                    "aftercare_hourly": 15.00, "og_session": 30.00,
                    "homework_hourly": 15.00, "tutoring_session": 30.00,
                }
                for k, v in defaults.items():
                    rates.setdefault(k, v)

                rows = []

                # 1. M Card charges
                cur.execute("""
                    SELECT charge_date, quantity, recorded_at
                    FROM   mcard_charges
                    WHERE  student_id = %s
                      AND  charge_date::date >= %s AND charge_date::date <= %s
                    ORDER  BY charge_date, recorded_at
                """, (student_id, first_day, last_day))
                for r in cur.fetchall():
                    qty = int(r["quantity"])
                    rows.append({
                        "date": str(r["charge_date"]), "program_key": "mcard",
                        "program_label": "M Card Snack",
                        "detail": f"{qty} snack{'s' if qty != 1 else ''}",
                        "recorded_by": "—",
                        "amount": round(qty * rates["mcard_snack"], 2),
                    })

                # 2. Program attendance
                cur.execute("""
                    SELECT session_date, program_type, units, teacher, recorded_by
                    FROM   program_attendance
                    WHERE  student_id = %s
                      AND  session_date::date >= %s AND session_date::date <= %s
                    ORDER  BY session_date, program_type
                """, (student_id, first_day, last_day))
                prog_labels = {
                    "og":         ("OG Tutoring",    "og",         "og_session",         "session"),
                    "homework":   ("Homework Center","homework",   "homework_hourly",     "hr"),
                    "tutoring":   ("1-on-1 Tutoring","tutoring",   "tutoring_session",   "session"),
                    "beforecare": ("Before Care",    "beforecare", "beforecare_session",  "session"),
                }
                for r in cur.fetchall():
                    pt    = r["program_type"]
                    units = float(r["units"])
                    label, key, rate_key, unit_word = prog_labels.get(
                        pt, (pt.replace("_"," ").title(), pt, "og_session", "unit"))
                    amount = units * rates[rate_key]
                    detail = f"{units:g} {unit_word}{'s' if units != 1 else ''}"
                    if r.get("teacher"):
                        detail += f" · Teacher: {r['teacher']}"
                    rows.append({
                        "date": str(r["session_date"]), "program_key": key,
                        "program_label": label, "detail": detail,
                        "recorded_by": r.get("recorded_by") or "—",
                        "amount": round(amount, 2),
                    })

                # 3. Aftercare
                cur.execute("""
                    SELECT session_date, checkin_time, pickup_time, recorded_by
                    FROM   aftercare_attendance
                    WHERE  student_id = %s
                      AND  session_date::date >= %s AND session_date::date <= %s
                      AND  pickup_time IS NOT NULL
                    ORDER  BY session_date
                """, (student_id, first_day, last_day))

                def ac_hours(pickup_str):
                    try:
                        s = str(pickup_str).strip().upper()
                        pm = 12 if "PM" in s else 0
                        s = s.replace("PM","").replace("AM","").strip()
                        h, m = int(s.split(":")[0]), int(s.split(":")[1])
                        if pm and h != 12: h += pm
                        elapsed = max(0, h*60 + m - (16*60+30))
                        return 1.0 if elapsed <= 60 else 1.0 + math.ceil((elapsed-60)/15)*15/60.0
                    except Exception:
                        return 0.0

                for r in cur.fetchall():
                    hrs    = ac_hours(r["pickup_time"])
                    amount = hrs * rates["aftercare_hourly"]
                    checkin = r.get("checkin_time") or "4:30 PM"
                    pickup  = r.get("pickup_time")  or "—"
                    rows.append({
                        "date": str(r["session_date"]), "program_key": "aftercare",
                        "program_label": "Aftercare",
                        "detail": f"In: {checkin} · Out: {pickup} ({hrs:g} hr{'s' if hrs!=1 else ''})",
                        "recorded_by": r.get("recorded_by") or "—",
                        "amount": round(amount, 2),
                    })

        finally:
            conn.close()

        rows.sort(key=lambda x: x["date"])
        return jsonify({
            "student_id": student_id, "month": month, "year": year,
            "rows": rows, "total": round(sum(r["amount"] for r in rows), 2),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================
# FINANCIAL AID
# ============================================

FINANCIAL_AID_MIGRATION = """
CREATE TABLE IF NOT EXISTS financial_aid_families (
    id               SERIAL PRIMARY KEY,
    family_name      TEXT NOT NULL,
    fast_id          TEXT,
    school_year      TEXT NOT NULL DEFAULT '2025-26',
    contract_sent    BOOLEAN DEFAULT FALSE,
    status           TEXT NOT NULL DEFAULT 'active',
    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS financial_aid_students (
    id                  SERIAL PRIMARY KEY,
    family_id           INTEGER NOT NULL REFERENCES financial_aid_families(id) ON DELETE CASCADE,
    school              TEXT,
    tuition             NUMERIC(10,2),
    max_discount        NUMERIC(10,2),
    fast_aid_rec        NUMERIC(10,2),
    appeal_letter       TEXT,
    family_can_pay      NUMERIC(10,2),
    mds_aid_amount      NUMERIC(10,2),
    net_tuition         NUMERIC(10,2),
    prior_year_tuition  NUMERIC(10,2),
    family_total        NUMERIC(10,2),
    family_total_prior  NUMERIC(10,2),
    parent_notes        TEXT,
    school_notes        TEXT,
    karins_notes        TEXT,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);
"""

TUITION_MAP = {
    'Kindergarten': 19338,
    'Lower School': 24200,
    'Middle School': 26299,
    'Eighth Grade': 27017,
}

SEED_DATA = [
    ("Anderson", "951677", True, [
        ("Middle School", 26299, "Y", 11000, 15299, 11000, 11000, 11000, 11000,
         "I am a widow of ten years. I have three dependent children, one that is in college. Mizzontop Day School has proved to be the best educational and social environment for X. Any financial aid would be greatly appreciated.",
         None, None),
    ]),
    ("Lent", "952078", True, [
        ("Lower School", 0, "Y, plus additional letter", 30000, 7000, 17200, 20900, None, None,
         "Our family has had a large reduction in W2 earnings in 2025.",
         "Second letter submitted and forwarded to you. I don't know if they will be able to do $38k.",
         "ok"),
        ("Middle School", None, None, None, 7000, 17200, 17900, None, None,
         None, None, "ok"),
    ]),
    ("Laino", "952222", True, [
        ("Lower School", 0, "Y", 15000, 9200, 15000, 17869, 15000, 17860,
         "2024 was a tough year for the family. With X being out of work for an extended period of time due to a work injury, he was unable to work overtime as he does every other year.",
         "This student's class will be down to 6 students without this enrollment",
         None),
    ]),
    ("Selca", "952223", True, [
        ("Middle School", 16299, "Y", 20000, 16299, 10000, 14280, None, None,
         "I co-own an apartment building with my brother which has had us face many financial hardships over the last several years. Our mortgage rate has adjusted and my payment increased by almost $9,000 a month.",
         None, None),
        ("Middle School", 16299, None, None, 16299, 9700, 13390, None, None,
         None, None, None),
    ]),
    ("Eyring", "952323", False, [
        ("Eighth Grade", 27017, "Y", 7000, 16789, 10228, 9298, 10228, 9298,
         "X has had an exceptional experience at Mizzentop and I am so thankful that she has been provided this opportunity.",
         "This student is an asset to her class and has been with MDS since PreK. Her father is retired and on limited income.",
         None),
    ]),
    ("Braham", "952339", True, [
        ("Lower School", 0, "Y", 32250, 4000, 20200, 17900, None, None,
         "We made plans, life had other plans. Not far into 2024, I was diagnosed with breast cancer which required multiple surgeries and extensive treatment.",
         None, None),
        ("Middle School", None, None, None, 6000, 20299, 17800, None, None,
         None, None, None),
    ]),
    ("Botter", "952340", True, [
        ("Middle School", 12275, "Y", 8000, 12275, 14024, 12300, 14024, 12300,
         "We would like our son to continue his educational experience at Mizzentop Day School because its mission, vision, and educational approach aligns with our own.",
         None, None),
    ]),
    ("Argueta", "952373", True, [
        ("Eighth Grade", 0, "Y", 20000, 3800, 23217, 18800, 23217, 18800,
         "X has been going to Mizzentop since JPK and I would love for him to finish his last year at a place where he loves to be.",
         None, "$0 aid"),
    ]),
    ("Boardman", "952480", True, [
        ("Middle School", 0, "Y", 10000, 2500, 23799, 22300, 23799, 22300,
         "This school has made an incredible impact on my son's life and even his self confidence.",
         None, None),
    ]),
    ("Welch", "952488", True, [
        ("Lower School", 18291, "Y", 7000, 14520, 9680, 7000, None, None,
         "Mizzentop has provided a safe and loving environment for my children to thrive.",
         "These girls tragically lost their father 2 years ago, and it has been our mission to support their education.",
         None),
        ("Kindergarten", 18291, None, None, 10000, 9338, 10932, None, None,
         None, None, None),
    ]),
    ("Linquist", "952525", True, [
        ("Lower School", 26299, "Y", 4000, 19200, 5000, 3000, 5000, 3000,
         "We greatly appreciate any and all help, and look forward to continuing to be part of the Mizzentop family.",
         "Mizzentop took this child in when her mother was tragically killed 5 years ago, and we are committed to supporting her education through 8th grade.",
         None),
    ]),
    ("Oludoja", "952542", True, [
        ("Eighth Grade", 0, "Y", 12000, 8500, 18517, 16600, 18517, 16600,
         "We are so glad that X is returning to Mizzentop Day School. He has come into his own and is doing so well overall.",
         None, None),
    ]),
    ("Modupe", "952594", False, [
        ("Middle School", 13702, "Y", 30000, 10450, 15849, 15050, None, None,
         "We certainly love having all 3 of our girls at Mizzentop, but having 3 girls at Mizzentop also holds its own financial strain.",
         None, "I think we should do $45 total?"),
        ("Lower School", None, None, None, 8450, 15750, 14250, None, None,
         None, None, "I think we should do $45 total?"),
        ("Kindergarten", None, None, None, 7500, 11838, 14550, None, None,
         None, None, "I think we should do $45 total?"),
    ]),
    ("Dolan", "952614", True, [
        ("Lower School", 0, "Y", 14250, 9500, 14700, 13000, 14700, 13000,
         "Both our boys started at Mizzentop in the Fall, and we feel so fortunate that they were able to attend such a wonderful institution.",
         "1 student in preK not considered on this application, baby on the way.",
         "They don't qualify?"),
    ]),
    ("Taylor", "952667", True, [
        ("Lower School", 0, "Y", 18000, 6200, 18000, 8000, 18000, 32550,
         "X enjoys being at this school and would like to continue. However we would appreciate financial aid as her brothers both will be in private high schools.",
         None, None),
    ]),
    ("Cuppek", "951263", True, [
        ("Middle School", 28598, "Y", 24000, 14900, 11399, 13215, None, None,
         "Our two children have been enrolled at Mizzentop Day School since the Fall of 2020. This upcoming school year, we fear that with another tuition increase, we will not be able to afford to enroll our children.",
         None, None),
        ("Middle School", None, None, None, 13500, 12799, 14280, None, None,
         None, None, None),
    ]),
    ("Sun", "950113", True, [
        ("Kindergarten", 9338, "Y", 10000, 7735, 11603, None, 11603, None,
         "We are a family deeply dedicated to serving in Christian mission fields. Joe's father is a pastor currently volunteering at an Australian church for a three-year term.",
         None, None),
    ]),
    ("Pool", "951468", True, [
        ("Lower School", 0, "Y", 17200, 7000, 17200, 14900, 17200, 14900,
         "We appreciate everything that Mizzentop has provided for Cameron and our family over the past few years.",
         None, None),
    ]),
    ("Conner", "951506", True, [
        ("Kindergarten", 19338, "Y", 7000, 6338, None, None, None, None,
         "As business owners, some years we do well and others we barely make it by.",
         None, None),
    ]),
    ("Hildenbrand", "951527", True, [
        ("Lower School", 0, "Y", 20000, 0, 24200, 17500, 24200, 17500,
         "We all love Mizzentop. Carly has been thriving since enrolling. We are seeking any available financial aid to assist with the tuition.",
         None, None),
    ]),
    ("Mazzucca", "951667", True, [
        ("Middle School", 4799, "Y", 21500, 2500, 23799, 20000, 23799, 20000,
         "My husband and I are both disabled retired police officers on a fixed income.",
         None, None),
    ]),
    ("Gavin", "951792", True, [
        ("Lower School", 14200, "Y", 10000, 4860, 19340, 15461, 19340, 15461,
         "I am a single parent trying to keep my daughter in a safe nurturing school environment.",
         None, None),
    ]),
    ("Wetterhorn", "951943", True, [
        ("Middle School", 6299, "Y", 20000, 6000, 20299, 21900, 20299, 21900,
         "I have never applied for financial aid before. I changed employers and knew it would be a difficult year taking a giant step backward financially.",
         None, None),
    ]),
    ("De Harte", "952308", True, [
        ("Middle School", 0, "Y", 0, 11845, 14454, 13140, 14454, 13140,
         "I am trying to get my son out of Poughkeepsie Middle School.",
         "The aid app does not show they have a severely special needs child at home who requires a lot of care.",
         "Why did she not qualify for aid? Do we know about TP?"),
    ]),
    ("Johnson", "952368", True, [
        ("Middle School", 0, "Y", 25000, 9900, 16399, 14280, None, None,
         "The biggest factor in requesting financial aid is that my oldest daughter will be attending college in the Fall.",
         "We gave the daughter a discount to recruit a girl last year, and she has been a gift!",
         None),
        ("Middle School", None, None, None, 9900, 16399, 21900, None, None,
         None, None, None),
    ]),
    ("Englehart", "952405", True, [
        ("Middle School", 14523, "Y", 30000, 10000, 16299, 15360, None, None,
         "Thank you for your consideration of our financial aid application and hope our family will be able to continue to send our daughters to Mizzentop.",
         None, None),
        ("Lower School", None, None, None, 9200, 15000, 13140, None, None,
         None, None, None),
    ]),
    ("Hampton", "952414", True, [
        ("Eighth Grade", 35419, "Y", 10000, 13698, 13319, 16600, None, None,
         "We would like to express how much our family appreciates being a part of the Mizzentop Family.",
         None, None),
        ("Middle School", None, None, None, 10520, 15779, 10000, None, None,
         None, None, None),
    ]),
    ("Sukow", "952644", True, [
        ("Middle School", 16299, "N", 10000, 8000, 18299, 15990, 18299, 15990,
         None, None, None),
    ]),
    ("Nelson", "953108", True, [
        ("Lower School", 38400, "Y", 10000, 12800, 11400, 11449, None, None,
         "Mizzentop has been a continuous support for us and a great foundation for our children's education.",
         None, None),
        ("Lower School", None, None, None, 12800, 11400, 11449, None, None,
         None, None, None),
    ]),
    ("Duplessis", "953352", True, [
        ("Middle School", 14299, "Y", 12000, 12000, 14299, 13130, 14299, 13130,
         "Noah is thriving at Mizzentop and I unfortunately cannot afford the school on my own finances.",
         None, None),
    ]),
    ("Phillips", "953839", True, [
        ("Eighth Grade", 6337, "Y", 15000, 10288, 16729, 13130, 16729, 13130,
         None, None, None),
    ]),
    ("Lallouz", "953763", True, [
        ("Lower School", 0, "Y", 13500, 4500, 19700, 13130, 19700, 13130,
         "We are writing to respectfully request your consideration for financial aid for our son, James. Both of us work in the television and entertainment industry, which was significantly impacted by the SAG-AFTRA and WGA strikes.",
         None, None),
    ]),
    ("Sorrentino", "956025", True, [
        ("Lower School", None, "Y", 5000, 9680, 14520, 13130, 14520, 13130,
         "I am a single working mother who wants my son to enjoy school. He hasn't had a positive experience in public school.",
         None, None),
    ]),
    ("Myint", "955840", True, [
        ("Lower School", 0, "Y", 10000, 2000, 22200, 13130, 22200, 13130,
         "We are writing to respectfully request your consideration for financial aid for our son. The past two years have been incredibly challenging for our family.",
         None, None),
    ]),
    ("Ball", "955900", False, [
        ("Lower School", 19200, "N", 5000, 9680, 14520, 13130, 14520, 13130,
         None, None, None),
    ]),
    ("Fitz Henley", "956127", False, [
        ("Lower School", 5000, "N", 25000, 9680, 14520, None, None, None,
         None, None, None),
        ("Middle School", None, None, None, 10520, 15779, None, None, None,
         None, None, None),
    ]),
    ("Garay", "955489", True, [
        ("Middle School", 39438, None, 30000, 12500, 13799, 14750, None, 43550,
         None, None, None),
        ("Middle School", None, None, None, 12200, 14099, 14950, None, None,
         None, None, None),
        ("Lower School", None, None, None, 6600, 17600, 13850, None, None,
         None, None, None),
    ]),
    ("Douglass", "957109", False, [
        ("Middle School", 0, "Y", 8000, 10000, 16299, None, None, None,
         None, None, None),
    ]),
]


def _fa_rows_to_families(rows):
    """Convert flat DB rows into grouped family dicts."""
    families = {}
    order = []
    for row in rows:
        fid = row["id"]
        if fid not in families:
            order.append(fid)
            families[fid] = {
                'id': fid,
                'family_name': row["family_name"],
                'fast_id': row["fast_id"],
                'contract_sent': row["contract_sent"],
                'status': row["status"],
                'school_year': row["school_year"],
                'students': []
            }
        if row["student_id"]:
            def _f(v):
                return float(v) if v is not None else None
            families[fid]['students'].append({
                'id': row["student_id"],
                'school': row["school"],
                'tuition': _f(row["tuition"]),
                'max_discount': _f(row["max_discount"]),
                'fast_aid_rec': _f(row["fast_aid_rec"]),
                'appeal_letter': row["appeal_letter"],
                'family_can_pay': _f(row["family_can_pay"]),
                'mds_aid_amount': _f(row["mds_aid_amount"]),
                'net_tuition': _f(row["net_tuition"]),
                'prior_year_tuition': _f(row["prior_year_tuition"]),
                'family_total': _f(row["family_total"]),
                'family_total_prior': _f(row["family_total_prior"]),
                'parent_notes': row["parent_notes"],
                'school_notes': row["school_notes"],
                'karins_notes': row["karins_notes"],
            })
    return [families[fid] for fid in order]


@app.route('/api/financial-aid/years')
@login_required
def api_financial_aid_years():
    """Return list of school years that have data."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT school_year FROM financial_aid_families
                ORDER BY school_year DESC
            """)
            years = [r[0] for r in cur.fetchall()]
        return jsonify(years)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/financial-aid')
@login_required
def api_financial_aid_list():
    """Return all families with their students for a given school year."""
    school_year = request.args.get('year', '2025-26')
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT f.id, f.family_name, f.fast_id, f.contract_sent, f.status, f.school_year,
                       s.id as student_id, s.school, s.tuition, s.max_discount,
                       s.fast_aid_rec, s.appeal_letter, s.family_can_pay,
                       s.mds_aid_amount, s.net_tuition, s.prior_year_tuition,
                       s.family_total, s.family_total_prior,
                       s.parent_notes, s.school_notes, s.karins_notes
                FROM financial_aid_families f
                LEFT JOIN financial_aid_students s ON s.family_id = f.id
                WHERE f.school_year = %s
                ORDER BY f.status ASC, f.family_name, s.id
            """, (school_year,))
            rows = cur.fetchall()
        return jsonify(_fa_rows_to_families(rows))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/financial-aid/<int:family_id>', methods=['PATCH'])
@login_required
def api_financial_aid_update(family_id):
    """Update family-level fields: notes, contract_sent, status."""
    data = request.json or {}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Build dynamic update for family table
            fam_fields = []
            fam_vals = []
            for col in ['contract_sent', 'status']:
                if col in data:
                    fam_fields.append(f"{col} = %s")
                    fam_vals.append(data[col])
            if fam_fields:
                fam_vals.append(family_id)
                cur.execute(f"UPDATE financial_aid_families SET {', '.join(fam_fields)}, updated_at=NOW() WHERE id=%s", fam_vals)

            # Karin's notes — stored per student but edited at family level
            if 'karins_notes' in data:
                cur.execute("UPDATE financial_aid_students SET karins_notes=%s, updated_at=NOW() WHERE family_id=%s",
                            (data['karins_notes'], family_id))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/financial-aid/<int:family_id>/students', methods=['POST'])
@login_required
def api_financial_aid_add_student(family_id):
    """Add a student to a family."""
    data = request.json or {}
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO financial_aid_students
                (family_id, school, tuition, max_discount, fast_aid_rec, appeal_letter,
                 family_can_pay, mds_aid_amount, net_tuition, prior_year_tuition,
                 family_total, family_total_prior, parent_notes, school_notes, karins_notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (family_id,
                  data.get('school'), data.get('tuition'), data.get('max_discount'),
                  data.get('fast_aid_rec'), data.get('appeal_letter'),
                  data.get('family_can_pay'), data.get('mds_aid_amount'),
                  data.get('net_tuition'), data.get('prior_year_tuition'),
                  data.get('family_total'), data.get('family_total_prior'),
                  data.get('parent_notes'), data.get('school_notes'), data.get('karins_notes')))
            new_id = cur.fetchone()['id']
        conn.commit()
        return jsonify({'ok': True, 'id': new_id}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/financial-aid/students/<int:student_id>', methods=['PUT'])
@login_required
def api_financial_aid_update_student(student_id):
    """Update all editable fields on a student row."""
    data = request.json or {}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE financial_aid_students SET
                    school=%s, tuition=%s, max_discount=%s, fast_aid_rec=%s,
                    appeal_letter=%s, family_can_pay=%s, mds_aid_amount=%s,
                    net_tuition=%s, prior_year_tuition=%s,
                    family_total=%s, family_total_prior=%s,
                    parent_notes=%s, school_notes=%s, karins_notes=%s,
                    updated_at=NOW()
                WHERE id=%s
            """, (data.get('school'), data.get('tuition'), data.get('max_discount'),
                  data.get('fast_aid_rec'), data.get('appeal_letter'),
                  data.get('family_can_pay'), data.get('mds_aid_amount'),
                  data.get('net_tuition'), data.get('prior_year_tuition'),
                  data.get('family_total'), data.get('family_total_prior'),
                  data.get('parent_notes'), data.get('school_notes'),
                  data.get('karins_notes'), student_id))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/financial-aid/students/<int:student_id>', methods=['DELETE'])
@login_required
def api_financial_aid_delete_student(student_id):
    """Remove a student row."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM financial_aid_students WHERE id=%s", (student_id,))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/financial-aid/families', methods=['POST'])
@login_required
def api_financial_aid_add_family():
    """Add a single new family manually."""
    data = request.json or {}
    family_name = (data.get('family_name') or '').strip()
    fast_id     = (data.get('fast_id') or '').strip()
    school_year = (data.get('school_year') or '2025-26').strip()
    if not family_name:
        return jsonify({'error': 'family_name required'}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check for duplicate FAST ID in same year
            if fast_id:
                cur.execute("SELECT id FROM financial_aid_families WHERE fast_id=%s AND school_year=%s",
                            (fast_id, school_year))
                if cur.fetchone():
                    return jsonify({'error': f'FAST ID {fast_id} already exists for {school_year}'}), 409
            cur.execute("""
                INSERT INTO financial_aid_families (family_name, fast_id, school_year, contract_sent, status)
                VALUES (%s,%s,%s,false,'active') RETURNING id
            """, (family_name, fast_id or None, school_year))
            new_id = cur.fetchone()['id']
        conn.commit()
        return jsonify({'ok': True, 'id': new_id}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/financial-aid/upload', methods=['POST'])
@login_required
def api_financial_aid_upload():
    """
    Bulk upload families from ISMFast CSV export.
    Expected columns (case-insensitive):
      Family Name, FAST ID, School, FAST Aid Rec, Appeal Letter,
      Family Can Pay, MDS Aid Amount, Net Tuition, Prior Year Tuition,
      Parent Notes, School Notes
    school_year passed as form field.
    Skips rows where FAST ID already exists for that year.
    """
    import csv, io
    school_year = request.form.get('school_year', '2025-26')
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400

    try:
        content = file.read().decode('utf-8-sig')  # strip BOM if present
        reader = csv.DictReader(io.StringIO(content))
        # Normalize headers to lowercase stripped
        headers = [h.strip().lower() for h in (reader.fieldnames or [])]

        def col(row, *names):
            for n in names:
                v = row.get(n, '').strip()
                if v: return v
            return None

        def money(v):
            if not v: return None
            try: return float(str(v).replace('$','').replace(',','').strip())
            except: return None

        conn = get_db_connection()
        added = skipped = activated = errors_count = 0
        error_names = []
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Fetch existing families for this year: fast_id -> {id, status}
                cur.execute("SELECT id, fast_id, status FROM financial_aid_families WHERE school_year=%s AND fast_id IS NOT NULL", (school_year,))
                existing = {r['fast_id']: {'id': r['id'], 'status': r['status']} for r in cur.fetchall()}

                # Normalize row keys
                for raw_row in reader:
                    row = {k.strip().lower(): v.strip() for k, v in raw_row.items()}
                    family_name = col(row, 'family name', 'family_name', 'last name', 'name')
                    fast_id     = col(row, 'fast id', 'fast_id', 'ismfast id', 'id')
                    school      = col(row, 'school', 'division', 'grade level')
                    if not family_name:
                        continue

                    try:
                        if fast_id and fast_id in existing:
                            fam_rec = existing[fast_id]
                            if fam_rec['status'] == 'active':
                                # Already active — skip to protect mid-season edits
                                skipped += 1
                                continue
                            else:
                                # Inactive (carried over) — activate and populate financials
                                fam_id = fam_rec['id']
                                cur.execute("""
                                    UPDATE financial_aid_families
                                    SET status='active', updated_at=NOW()
                                    WHERE id=%s
                                """, (fam_id,))
                                existing[fast_id]['status'] = 'active'
                                if school:
                                    tuition_val = TUITION_MAP.get(school) or money(col(row, 'tuition'))
                                    # Update existing student row if present, else insert
                                    cur.execute("SELECT id FROM financial_aid_students WHERE family_id=%s LIMIT 1", (fam_id,))
                                    stu = cur.fetchone()
                                    if stu:
                                        cur.execute("""
                                            UPDATE financial_aid_students SET
                                                school=%s, tuition=%s, fast_aid_rec=%s, appeal_letter=%s,
                                                family_can_pay=%s, mds_aid_amount=%s, net_tuition=%s,
                                                prior_year_tuition=COALESCE(prior_year_tuition, %s),
                                                parent_notes=%s, school_notes=%s, updated_at=NOW()
                                            WHERE id=%s
                                        """, (school, tuition_val,
                                              money(col(row, 'fast aid rec', 'fast_aid_rec', 'fast rec')),
                                              col(row, 'appeal letter', 'appeal'),
                                              money(col(row, 'family can pay', 'family_can_pay')),
                                              money(col(row, 'mds aid amount', 'mds_aid_amount', 'aid amount')),
                                              money(col(row, 'net tuition', 'net_tuition')),
                                              money(col(row, 'prior year tuition', 'prior_year_tuition', 'prior tuition')),
                                              col(row, 'parent notes', 'parent_notes'),
                                              col(row, 'school notes', 'school_notes'),
                                              stu['id']))
                                    else:
                                        cur.execute("""
                                            INSERT INTO financial_aid_students
                                            (family_id, school, tuition, fast_aid_rec, appeal_letter,
                                             family_can_pay, mds_aid_amount, net_tuition,
                                             prior_year_tuition, parent_notes, school_notes)
                                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                        """, (fam_id, school, tuition_val,
                                              money(col(row, 'fast aid rec', 'fast_aid_rec', 'fast rec')),
                                              col(row, 'appeal letter', 'appeal'),
                                              money(col(row, 'family can pay', 'family_can_pay')),
                                              money(col(row, 'mds aid amount', 'mds_aid_amount', 'aid amount')),
                                              money(col(row, 'net tuition', 'net_tuition')),
                                              money(col(row, 'prior year tuition', 'prior_year_tuition', 'prior tuition')),
                                              col(row, 'parent notes', 'parent_notes'),
                                              col(row, 'school notes', 'school_notes')))
                                activated += 1
                        else:
                            # Brand new family — create as active
                            cur.execute("""
                                INSERT INTO financial_aid_families (family_name, fast_id, school_year, contract_sent, status)
                                VALUES (%s,%s,%s,false,'active') RETURNING id
                            """, (family_name, fast_id or None, school_year))
                            fam_id = cur.fetchone()['id']
                            if fast_id:
                                existing[fast_id] = {'id': fam_id, 'status': 'active'}
                            if school:
                                tuition_val = TUITION_MAP.get(school) or money(col(row, 'tuition'))
                                cur.execute("""
                                    INSERT INTO financial_aid_students
                                    (family_id, school, tuition, fast_aid_rec, appeal_letter,
                                     family_can_pay, mds_aid_amount, net_tuition,
                                     prior_year_tuition, parent_notes, school_notes)
                                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                """, (fam_id, school, tuition_val,
                                      money(col(row, 'fast aid rec', 'fast_aid_rec', 'fast rec')),
                                      col(row, 'appeal letter', 'appeal'),
                                      money(col(row, 'family can pay', 'family_can_pay')),
                                      money(col(row, 'mds aid amount', 'mds_aid_amount', 'aid amount')),
                                      money(col(row, 'net tuition', 'net_tuition')),
                                      money(col(row, 'prior year tuition', 'prior_year_tuition', 'prior tuition')),
                                      col(row, 'parent notes', 'parent_notes'),
                                      col(row, 'school notes', 'school_notes')))
                            added += 1
                    except Exception as row_err:
                        errors_count += 1
                        error_names.append(family_name)
                        print(f"Upload row error for {family_name}: {row_err}")
            conn.commit()
        finally:
            conn.close()

        return jsonify({
            'ok': True,
            'added': added,
            'activated': activated,
            'skipped': skipped,
            'errors': errors_count,
            'error_names': error_names
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/financial-aid/template')
@login_required
def api_financial_aid_template():
    """Download a blank CSV template for ISMFast bulk upload."""
    from flask import Response
    headers = [
        'Family Name', 'FAST ID', 'School', 'FAST Aid Rec', 'Appeal Letter',
        'Family Can Pay', 'MDS Aid Amount', 'Net Tuition', 'Prior Year Tuition',
        'Parent Notes', 'School Notes'
    ]
    example = [
        'Smith', '123456', 'Lower School', '5000', 'Y',
        '18000', '6200', '18000', '17000', 'We love Mizzentop.', ''
    ]
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerow(example)
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=financial_aid_upload_template.csv'}
    )


@app.route('/api/financial-aid/new-season', methods=['POST'])
@login_required
def api_financial_aid_new_season():
    """
    Create a new school year by rolling forward active families from a prior year.
    - Copies family name + FAST ID
    - Rolls net_tuition → prior_year_tuition for each student
    - Blanks all other financial figures
    - Skips inactive families
    - Skips families that already exist in the new year (by FAST ID)
    """
    data = request.json or {}
    from_year = data.get('from_year', '').strip()
    to_year   = data.get('to_year', '').strip()
    if not from_year or not to_year:
        return jsonify({'error': 'from_year and to_year required'}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check new year doesn't already exist
            cur.execute("SELECT COUNT(*) as cnt FROM financial_aid_families WHERE school_year=%s", (to_year,))
            if cur.fetchone()['cnt'] > 0:
                return jsonify({'error': f'{to_year} already has data. Cannot overwrite.'}), 409

            # Fetch active families from source year
            cur.execute("""
                SELECT f.id, f.family_name, f.fast_id,
                       s.id as sid, s.school, s.net_tuition, s.karins_notes
                FROM financial_aid_families f
                LEFT JOIN financial_aid_students s ON s.family_id = f.id
                WHERE f.school_year = %s AND f.status = 'active'
                ORDER BY f.family_name, s.id
            """, (from_year,))
            rows = cur.fetchall()

            # Group by family
            fam_map = {}
            fam_order = []
            for r in rows:
                fid = r['id']
                if fid not in fam_map:
                    fam_order.append(fid)
                    fam_map[fid] = {'family_name': r['family_name'], 'fast_id': r['fast_id'], 'students': []}
                if r['sid']:
                    fam_map[fid]['students'].append({
                        'school': r['school'],
                        'prior_year_tuition': float(r['net_tuition']) if r['net_tuition'] else None,
                        'karins_notes': r['karins_notes'],
                    })

            carried = 0
            for fid in fam_order:
                f = fam_map[fid]
                cur.execute("""
                    INSERT INTO financial_aid_families (family_name, fast_id, school_year, contract_sent, status)
                    VALUES (%s,%s,%s,false,'inactive') RETURNING id
                """, (f['family_name'], f['fast_id'], to_year))
                new_fam_id = cur.fetchone()['id']
                for s in f['students']:
                    tuition = TUITION_MAP.get(s['school'])
                    cur.execute("""
                        INSERT INTO financial_aid_students
                        (family_id, school, tuition, prior_year_tuition)
                        VALUES (%s,%s,%s,%s)
                    """, (new_fam_id, s['school'], tuition, s['prior_year_tuition']))
                carried += 1

        conn.commit()
        return jsonify({'ok': True, 'families_carried': carried, 'to_year': to_year})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/admin/seed-financial-aid', methods=['POST'])
@login_required
def seed_financial_aid():
    """Seed the financial aid tables from spreadsheet data. Run once."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Create tables (run migration statements individually)
            for stmt in FINANCIAL_AID_MIGRATION.strip().split(';'):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            # Add status column if migrating from old schema
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_families' AND column_name='status'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_families ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            # Rename net_tuition_2526 → net_tuition if needed
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_students' AND column_name='net_tuition_2526'
            """)
            if cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_students RENAME COLUMN net_tuition_2526 TO net_tuition")
            # Rename family_total_2526 → family_total if needed
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_students' AND column_name='family_total_2526'
            """)
            if cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_students RENAME COLUMN family_total_2526 TO family_total")
            # Guard against double-seeding
            cur.execute("SELECT COUNT(*) FROM financial_aid_families WHERE school_year = '2025-26'")
            count = cur.fetchone()[0]
            if count > 0:
                return jsonify({'error': 'Already seeded. Delete rows first to re-seed.'}), 400
            for (fname, fast_id, contract_sent, students) in SEED_DATA:
                cur.execute("""
                    INSERT INTO financial_aid_families (family_name, fast_id, school_year, contract_sent, status)
                    VALUES (%s, %s, '2025-26', %s, 'active')
                    RETURNING id
                """, (fname, fast_id, contract_sent))
                fam_id = cur.fetchone()[0]
                for s in students:
                    school = s[0]
                    tuition = TUITION_MAP.get(school)
                    cur.execute("""
                        INSERT INTO financial_aid_students
                        (family_id, school, tuition, fast_aid_rec, appeal_letter,
                         family_can_pay, mds_aid_amount, net_tuition,
                         prior_year_tuition, family_total, family_total_prior,
                         parent_notes, school_notes, karins_notes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (fam_id, school, tuition, s[1], s[2], s[3], s[4],
                          s[5], s[6], s[7], s[8], s[9], s[10], s[11]))
        conn.commit()
        return jsonify({'ok': True, 'families_seeded': len(SEED_DATA)})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ============================================
# ONE-TIME MIGRATIONS
# ============================================

@app.route('/admin/migrate-financial-aid', methods=['POST'])
@login_required
def migrate_financial_aid():
    """
    One-time migration: rename old year-specific columns to generic names
    and add status column. Safe to run multiple times.
    """
    conn = get_db_connection()
    results = []
    try:
        with conn.cursor() as cur:
            # Rename net_tuition_2526 -> net_tuition
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_students' AND column_name='net_tuition_2526'
            """)
            if cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_students RENAME COLUMN net_tuition_2526 TO net_tuition")
                results.append("Renamed net_tuition_2526 to net_tuition")
            else:
                results.append("net_tuition already correct (skipped)")

            # Rename family_total_2526 -> family_total
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_students' AND column_name='family_total_2526'
            """)
            if cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_students RENAME COLUMN family_total_2526 TO family_total")
                results.append("Renamed family_total_2526 to family_total")
            else:
                results.append("family_total already correct (skipped)")

            # Add status column to families if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_families' AND column_name='status'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_families ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
                results.append("Added status column to financial_aid_families")
            else:
                results.append("status column already exists (skipped)")

        conn.commit()
        return jsonify({'ok': True, 'results': results})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()



# ============================================
# SIGNATURE GENERATOR
# ============================================

@app.route('/signature')
@login_required
def signature_generator():
    return send_from_directory('.', 'signature_generator.html')

# ============================================
# STARTUP + RUN
# ============================================

init_db()

if __name__ == "__main__":
    print("Mizzentop Admin — PostgreSQL mode")
    app.run(debug=True, host="0.0.0.0", port=5000)
