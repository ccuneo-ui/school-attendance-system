"""
School Attendance System - Backend Server
This Flask app connects the HTML attendance form to your SQLite database
"""

from flask import Flask, request, jsonify, send_from_directory, render_template, session, redirect, url_for
from flask_cors import CORS
from authlib.integrations.flask_client import OAuth
from functools import wraps
import sqlite3
from datetime import datetime
import os

app = Flask(__name__)
CORS(app)

# Session secret key
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

# ── Google OAuth ──
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

SUPERADMIN_EMAIL = 'scaroppoli@mizzentop.org'  # Update to your email if needed

def get_current_user():
    email = session.get('user_email')
    if not email:
        return None
    conn = get_db_connection()
    user = conn.execute(
        'SELECT * FROM staff WHERE email = ? AND status = ?', (email, 'active')
    ).fetchone()
    conn.close()
    return user

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_email'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_email'):
            return redirect('/login')
        if not session.get('is_superadmin'):
            return redirect('/?error=unauthorized')
        return f(*args, **kwargs)
    return decorated

# Database file location - Uses persistent disk on Render, falls back to local for development
PERSISTENT_DB = '/var/data/school.db'  # Persistent disk path on Render
LOCAL_DB = 'school.db'                 # Local path for development

def get_database_path():
    """Use persistent disk if available, otherwise use local file"""
    import os
    # If persistent disk exists, use it
    if os.path.exists('/var/data'):
        # If database doesn't exist on persistent disk yet, copy it from local
        if not os.path.exists(PERSISTENT_DB):
            import shutil
            if os.path.exists(LOCAL_DB):
                shutil.copy2(LOCAL_DB, PERSISTENT_DB)
                print(f"✓ Copied database to persistent disk: {PERSISTENT_DB}")
            else:
                print(f"WARNING: No local database found to copy!")
        return PERSISTENT_DB
    return LOCAL_DB

DATABASE = get_database_path()

def get_db_connection():
    """Create a connection to the SQLite database"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row  # Return rows as dictionaries
    return conn

# ============================================
# API ENDPOINTS
# ============================================

@app.route('/')
@login_required
def index():
    """Serve the home page"""
    return send_from_directory('.', 'home.html')

# ── AUTH ROUTES ──

@app.route('/login')
def login():
    """Serve the login page"""
    if session.get('user_email'):
        return redirect('/')
    return send_from_directory('.', 'login.html')

@app.route('/auth/google')
def auth_google():
    """Redirect to Google OAuth"""
    redirect_uri = 'https://admin.mizzentopdayschool.org/auth/callback'
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/callback')
def auth_callback():
    """Handle Google OAuth callback"""
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')
        email = user_info.get('email', '').lower()

        # Check if email is from mizzentop.org
        if not email.endswith('@mizzentop.org'):
            return redirect('/login?error=domain')

        # Check if staff record exists
        conn = get_db_connection()
        staff = conn.execute(
            'SELECT * FROM staff WHERE email = ? AND status = ?', (email, 'active')
        ).fetchone()
        conn.close()

        if not staff and email != SUPERADMIN_EMAIL:
            return redirect('/login?error=notfound')

        # Set session
        session['user_email'] = email
        session['user_name'] = user_info.get('name', '')
        session['is_superadmin'] = (email == SUPERADMIN_EMAIL)
        if staff:
            session['can_record_attendance'] = bool(staff['can_record_attendance'])
            session['can_manage_billing'] = bool(staff['can_manage_billing'])
            session['user_role'] = staff['role']
        else:
            session['can_record_attendance'] = True
            session['can_manage_billing'] = True
            session['user_role'] = 'superadmin'

        return redirect('/')
    except Exception as e:
        print(f"Auth error: {e}")
        return redirect('/login?error=auth')

@app.route('/auth/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/api/session')
def get_session():
    """Return current session info for frontend"""
    if not session.get('user_email'):
        return jsonify({'logged_in': False}), 401
    return jsonify({
        'logged_in': True,
        'email': session.get('user_email'),
        'name': session.get('user_name'),
        'is_superadmin': session.get('is_superadmin', False),
        'can_record_attendance': session.get('can_record_attendance', False),
        'can_manage_billing': session.get('can_manage_billing', False),
        'role': session.get('user_role'),
    })

@app.route('/logo.svg')
def serve_logo():
    return send_from_directory('.', 'logo.svg')

@app.route('/attendance')
@login_required
def attendance():
    """Serve the attendance form"""
    return send_from_directory('.', 'attendance_form.html')

@app.route('/api/programs', methods=['GET'])
def get_programs():
    """Get all active programs"""
    conn = get_db_connection()
    programs = conn.execute('''
        SELECT program_id, program_name, billing_rate, billing_type
        FROM programs
        WHERE status = 'active'
        ORDER BY program_name
    ''').fetchall()
    conn.close()
    
    return jsonify([dict(p) for p in programs])

@app.route('/api/staff', methods=['GET'])
def get_staff():
    """Get all staff members who can record attendance"""
    conn = get_db_connection()
    staff = conn.execute('''
        SELECT staff_id, first_name || ' ' || last_name as name, role
        FROM staff
        WHERE status = 'active' AND can_record_attendance = 1
        ORDER BY last_name, first_name
    ''').fetchall()
    conn.close()
    
    return jsonify([dict(s) for s in staff])

@app.route('/api/enrollments/<int:program_id>', methods=['GET'])
def get_enrollments(program_id):
    """Get all students enrolled in a specific program"""
    conn = get_db_connection()
    enrollments = conn.execute('''
        SELECT 
            e.enrollment_id,
            e.student_id,
            s.first_name || ' ' || s.last_name as student_name,
            s.first_name,
            s.last_name,
            s.grade,
            e.program_id,
            p.program_name
        FROM enrollments e
        JOIN students s ON e.student_id = s.student_id
        JOIN programs p ON e.program_id = p.program_id
        WHERE e.program_id = ? 
            AND e.status = 'active'
            AND s.status = 'active'
        ORDER BY s.last_name, s.first_name
    ''', (program_id,)).fetchall()
    conn.close()
    
    return jsonify([dict(e) for e in enrollments])

@app.route('/api/attendance', methods=['POST'])
def save_attendance():
    """Save attendance records"""
    data = request.json
    
    program_id = data.get('program_id')
    date = data.get('date')
    staff_id = data.get('staff_id')
    attendance = data.get('attendance', {})
    
    if not all([program_id, date, staff_id, attendance]):
        return jsonify({'error': 'Missing required fields'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    saved_count = 0
    errors = []
    
    for student_id, record in attendance.items():
        try:
            # Get enrollment_id
            enrollment = cursor.execute('''
                SELECT enrollment_id 
                FROM enrollments 
                WHERE student_id = ? AND program_id = ? AND status = 'active'
            ''', (student_id, program_id)).fetchone()
            
            if not enrollment:
                errors.append(f"No active enrollment found for student {student_id}")
                continue
            
            enrollment_id = enrollment['enrollment_id']
            
            # Check if attendance already exists for this date
            existing = cursor.execute('''
                SELECT attendance_id 
                FROM attendance_records 
                WHERE enrollment_id = ? AND attendance_date = ?
            ''', (enrollment_id, date)).fetchone()
            
            if existing:
                # Update existing record
                cursor.execute('''
                    UPDATE attendance_records
                    SET status = ?, 
                        notes = ?,
                        recorded_by = ?,
                        recorded_at = CURRENT_TIMESTAMP
                    WHERE attendance_id = ?
                ''', (record['status'], record.get('note', ''), staff_id, existing['attendance_id']))
            else:
                # Insert new record
                cursor.execute('''
                    INSERT INTO attendance_records 
                    (enrollment_id, attendance_date, status, recorded_by, notes)
                    VALUES (?, ?, ?, ?, ?)
                ''', (enrollment_id, date, record['status'], staff_id, record.get('note', '')))
            
            saved_count += 1
            
        except Exception as e:
            errors.append(f"Error saving attendance for student {student_id}: {str(e)}")
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'saved_count': saved_count,
        'errors': errors
    })

@app.route('/api/attendance/<int:program_id>/<date>', methods=['GET'])
def get_attendance(program_id, date):
    """Get attendance records for a specific program and date"""
    conn = get_db_connection()
    records = conn.execute('''
        SELECT 
            a.attendance_id,
            a.enrollment_id,
            e.student_id,
            s.first_name || ' ' || s.last_name as student_name,
            a.status,
            a.notes,
            a.recorded_at,
            st.first_name || ' ' || st.last_name as recorded_by_name
        FROM attendance_records a
        JOIN enrollments e ON a.enrollment_id = e.enrollment_id
        JOIN students s ON e.student_id = s.student_id
        JOIN staff st ON a.recorded_by = st.staff_id
        WHERE e.program_id = ? AND a.attendance_date = ?
        ORDER BY s.last_name, s.first_name
    ''', (program_id, date)).fetchall()
    conn.close()
    
    return jsonify([dict(r) for r in records])

@app.route('/api/summary/<int:program_id>/<start_date>/<end_date>', methods=['GET'])
def get_summary(program_id, start_date, end_date):
    """Get attendance summary for a program over a date range"""
    conn = get_db_connection()
    summary = conn.execute('''
        SELECT 
            s.student_id,
            s.first_name || ' ' || s.last_name as student_name,
            COUNT(*) as total_days,
            SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END) as present_count,
            SUM(CASE WHEN a.status = 'absent' THEN 1 ELSE 0 END) as absent_count,
            SUM(CASE WHEN a.status = 'excused' THEN 1 ELSE 0 END) as excused_count
        FROM attendance_records a
        JOIN enrollments e ON a.enrollment_id = e.enrollment_id
        JOIN students s ON e.student_id = s.student_id
        WHERE e.program_id = ?
            AND a.attendance_date BETWEEN ? AND ?
        GROUP BY s.student_id, s.first_name, s.last_name
        ORDER BY s.last_name, s.first_name
    ''', (program_id, start_date, end_date)).fetchall()
    conn.close()
    
    return jsonify([dict(r) for r in summary])

@app.route('/api/test', methods=['GET'])
def test():
    """Test endpoint to verify server is running"""
    return jsonify({
        'status': 'ok',
        'message': 'Server is running!',
        'database': DATABASE,
        'database_exists': os.path.exists(DATABASE)
    })

# ============================================
# M CARD ROUTES
# ============================================

def init_mcard_table():
    """Create mcard_charges table if it doesn't exist, and add quantity column if missing"""
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mcard_charges (
            charge_id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            charge_date TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(student_id)
        )
    ''')
    # Add quantity column if it doesn't exist (for tables created before this column was added)
    try:
        conn.execute('ALTER TABLE mcard_charges ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1')
    except Exception:
        pass  # Column already exists, fine
    conn.commit()
    conn.close()

@app.route('/mcard')
@login_required
def mcard():
    """Serve the M Card charge tracker"""
    return send_from_directory('.', 'mcard_tracker.html')

@app.route('/api/mcard/students', methods=['GET'])
def get_mcard_students():
    """Get all active students for the M Card dropdown"""
    conn = get_db_connection()
    students = conn.execute('''
        SELECT student_id, first_name, last_name, grade
        FROM students
        WHERE status = 'active'
        ORDER BY last_name, first_name
    ''').fetchall()
    conn.close()
    return jsonify([dict(s) for s in students])

@app.route('/api/mcard/charges', methods=['GET'])
def get_mcard_charges():
    """Get all M Card charges joined with student info"""
    init_mcard_table()
    conn = get_db_connection()
    charges = conn.execute('''
        SELECT 
            m.charge_id,
            m.student_id,
            s.first_name || ' ' || s.last_name AS student_name,
            s.grade,
            m.charge_date,
            m.quantity,
            m.recorded_at
        FROM mcard_charges m
        JOIN students s ON m.student_id = s.student_id
        ORDER BY m.charge_date DESC, m.recorded_at DESC
    ''').fetchall()
    conn.close()
    return jsonify([dict(c) for c in charges])

@app.route('/api/mcard/charges', methods=['POST'])
def add_mcard_charge():
    """Add a new M Card charge linked to a student record"""
    init_mcard_table()
    data = request.json
    student_id = data.get('student_id')
    charge_date = data.get('charge_date', '')
    quantity = int(data.get('quantity', 1))

    if quantity not in [1, 2]:
        return jsonify({'error': 'Quantity must be 1 or 2'}), 400

    if not student_id or not charge_date:
        return jsonify({'error': 'Missing student_id or charge_date'}), 400

    conn = get_db_connection()
    # Verify the student exists and is active
    student = conn.execute(
        'SELECT student_id FROM students WHERE student_id = ? AND status = ?',
        (student_id, 'active')
    ).fetchone()

    if not student:
        conn.close()
        return jsonify({'error': 'Student not found or inactive'}), 404

    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO mcard_charges (student_id, charge_date, quantity)
        VALUES (?, ?, ?)
    ''', (student_id, charge_date, quantity))
    charge_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'charge_id': charge_id})

@app.route('/api/mcard/charges/<int:charge_id>', methods=['DELETE'])
def delete_mcard_charge(charge_id):
    """Delete an M Card charge"""
    init_mcard_table()
    conn = get_db_connection()
    conn.execute('DELETE FROM mcard_charges WHERE charge_id = ?', (charge_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ============================================
# DISMISSAL ROUTES
# ============================================

def init_dismissal_tables():
    """
    Create dismissal-related tables if they don't exist.

    dismissal_today  — one row per student per date, holds today's actual
                       bus/pickup/activity assignment plus where they end the day.
    electives        — lookup table of elective class names (Art, Music, PE, etc.)
    """
    conn = get_db_connection()

    # Today's working dismissal plan (reset each day by admin)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS dismissal_today (
            dismissal_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id     INTEGER NOT NULL,
            plan_date      TEXT NOT NULL,           -- YYYY-MM-DD
            bus_route      TEXT,                    -- e.g. "POK", "Arlington", "Pick Up"
            activity       TEXT,                    -- e.g. "Aftercare", "JV Soccer"
            ends_in        TEXT DEFAULT 'homeroom', -- 'homeroom' | 'elective'
            elective_name  TEXT,                    -- e.g. "Art", "PE" (if ends_in = 'elective')
            notes          TEXT,                    -- one-off override notes
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(student_id),
            UNIQUE(student_id, plan_date)           -- one record per student per day
        )
    ''')

    # Electives lookup (so the dropdown stays consistent)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS electives (
            elective_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE,
            active        INTEGER DEFAULT 1
        )
    ''')

    # Seed electives if table is empty
    existing = conn.execute('SELECT COUNT(*) as c FROM electives').fetchone()['c']
    if existing == 0:
        default_electives = [
            'Art', 'Music', 'PE', 'Library', 'Technology',
            'Drama', 'Spanish', 'French', 'Mandarin', 'STEM'
        ]
        for name in default_electives:
            conn.execute('INSERT OR IGNORE INTO electives (name) VALUES (?)', (name,))

    conn.commit()
    conn.close()


@app.route('/dismissal-staff')
@login_required
def dismissal_staff():
    """Serve the read-only staff dismissal dashboard"""
    return send_from_directory('.', 'dismissal_staff_view.html')


@app.route('/api/electives', methods=['GET'])
def get_electives():
    """Return list of elective names for dropdowns"""
    init_dismissal_tables()
    conn = get_db_connection()
    rows = conn.execute(
        'SELECT elective_id, name FROM electives WHERE active = 1 ORDER BY name'
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/dismissal/today', methods=['GET'])
def get_dismissal_today():
    """
    Return today's dismissal list for the staff dashboard.

    Query params:
      date  — YYYY-MM-DD (defaults to server's today)
      grade — filter to one grade (optional)

    Reads from daily_dismissal (the admin planner table).
    Falls back to per-day defaults if no plan has been entered yet.
    """
    init_dismissal_tables()

    date_param = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    grade_filter = request.args.get('grade', None)

    conn = get_db_connection()

    # Check whether the admin planner has been filled in for this date
    filled = conn.execute(
        'SELECT COUNT(*) as c FROM daily_dismissal WHERE dismissal_date = ?',
        (date_param,)
    ).fetchone()['c']

    source = 'today' if filled > 0 else 'default'

    if source == 'today':
        # Pull from daily_dismissal (admin planner table) joined with students
        grade_clause = 'AND s.grade = ?' if grade_filter else ''
        params = [date_param]
        if grade_filter:
            params.append(grade_filter)

        rows = conn.execute(f'''
            SELECT
                s.student_id            AS id,
                s.first_name            AS firstName,
                s.last_name             AS lastName,
                s.grade,
                d.dismissal_type        AS dismissal,
                d.destination           AS activity,
                'homeroom'              AS endsIn,
                NULL                    AS elective,
                d.notes
            FROM students s
            LEFT JOIN daily_dismissal d
                   ON d.student_id = s.student_id AND d.dismissal_date = ?
            WHERE s.status = 'active'
            {grade_clause}
            ORDER BY s.last_name, s.first_name
        ''', params).fetchall()

    else:
        # Fall back to per-day defaults stored on the students table
        day_col_map = {
            'Monday':    'dismissal_mon',
            'Tuesday':   'dismissal_tue',
            'Wednesday': 'dismissal_wed',
            'Thursday':  'dismissal_thu',
            'Friday':    'dismissal_fri',
        }
        from datetime import date as dt_date
        d_obj = dt_date.fromisoformat(date_param)
        day_name = d_obj.strftime('%A')
        col = day_col_map.get(day_name, 'dismissal_mon')

        grade_clause = 'AND grade = ?' if grade_filter else ''
        params = []
        if grade_filter:
            params.append(grade_filter)

        rows = conn.execute(f'''
            SELECT
                student_id   AS id,
                first_name   AS firstName,
                last_name    AS lastName,
                grade,
                {col}        AS dismissal,
                NULL         AS activity,
                'homeroom'   AS endsIn,
                NULL         AS elective,
                NULL         AS notes
            FROM students
            WHERE status = 'active'
            {grade_clause}
            ORDER BY last_name, first_name
        ''', params).fetchall()

    conn.close()

    # ── Elective schedule rules ──────────────────────────────────────────────
    # Lower school  (grades 1-4): elective every Tuesday
    # Middle school (grades 5-8): advisory/elective every Thursday
    # JPK, SPK, K:  always end in homeroom
    from datetime import date as dt_date
    d_obj    = dt_date.fromisoformat(date_param)
    day_name = d_obj.strftime('%A')   # e.g. 'Tuesday'

    LOWER_SCHOOL  = {'1', '2', '3', '4'}
    MIDDLE_SCHOOL = {'5', '6', '7', '8'}

    def calc_ends_in(grade):
        g = str(grade or '').strip()
        if g in LOWER_SCHOOL and day_name == 'Tuesday':
            return 'elective', 'Elective'
        if g in MIDDLE_SCHOOL and day_name == 'Tuesday':
            return 'elective', 'Advisory'
        if g in MIDDLE_SCHOOL and day_name == 'Thursday':
            return 'elective', 'Elective'
        return 'homeroom', None

    students = []
    for r in rows:
        row = dict(r)
        row['name'] = f"{row['firstName']} {row['lastName']}"
        ends_in, elective = calc_ends_in(row.get('grade'))
        row['endsIn']   = ends_in
        row['elective'] = elective
        students.append(row)

    return jsonify({
        'date':     date_param,
        'source':   source,
        'day':      day_name,
        'students': students
    })


@app.route('/api/dismissal/today', methods=['POST'])
def save_dismissal_today():
    """
    Admin endpoint — upsert one or more students' dismissal assignments for a date.

    Body (JSON):
      {
        "date": "2026-02-18",
        "records": [
          {
            "student_id":    42,
            "bus_route":     "POK",
            "activity":      null,
            "ends_in":       "elective",
            "elective_name": "Art",
            "notes":         ""
          },
          ...
        ]
      }
    """
    init_dismissal_tables()
    data = request.json
    plan_date = data.get('date', datetime.now().strftime('%Y-%m-%d'))
    records   = data.get('records', [])

    if not records:
        return jsonify({'error': 'No records provided'}), 400

    conn = get_db_connection()
    saved = 0
    errors = []

    for rec in records:
        sid = rec.get('student_id')
        if not sid:
            errors.append('Missing student_id in record')
            continue
        try:
            conn.execute('''
                INSERT INTO dismissal_today
                    (student_id, plan_date, bus_route, activity, ends_in, elective_name, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(student_id, plan_date) DO UPDATE SET
                    bus_route      = excluded.bus_route,
                    activity       = excluded.activity,
                    ends_in        = excluded.ends_in,
                    elective_name  = excluded.elective_name,
                    notes          = excluded.notes,
                    updated_at     = CURRENT_TIMESTAMP
            ''', (
                sid,
                plan_date,
                rec.get('bus_route'),
                rec.get('activity'),
                rec.get('ends_in', 'homeroom'),
                rec.get('elective_name'),
                rec.get('notes', '')
            ))
            saved += 1
        except Exception as e:
            errors.append(f"Student {sid}: {str(e)}")

    conn.commit()
    conn.close()

    return jsonify({'success': True, 'saved': saved, 'errors': errors})


@app.route('/api/dismissal/today', methods=['DELETE'])
def clear_dismissal_today():
    """
    Admin endpoint — clear all dismissal assignments for a given date
    (e.g. called at end of day to reset for tomorrow).

    Query param:  date=YYYY-MM-DD   (required)
    """
    init_dismissal_tables()
    plan_date = request.args.get('date')
    if not plan_date:
        return jsonify({'error': 'date param required'}), 400

    conn = get_db_connection()
    conn.execute('DELETE FROM dismissal_today WHERE plan_date = ?', (plan_date,))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'cleared_date': plan_date})


# ============================================
# DISMISSAL PLANNER ROUTES (admin planner page)
# ============================================

@app.route('/dismissal')
@login_required
def dismissal():
    """Serve the admin dismissal planner page"""
    return send_from_directory('.', 'dismissal_planner.html')

@app.route('/api/dismissal/attendance/<date>', methods=['GET'])
def get_dismissal_attendance(date):
    conn = get_db_connection()
    program = conn.execute("SELECT program_id FROM programs WHERE program_name = 'General Attendance' AND status = 'active' LIMIT 1").fetchone()
    if not program:
        conn.close()
        return jsonify([])
    records = conn.execute('''
        SELECT e.student_id, a.status
        FROM attendance_records a
        JOIN enrollments e ON a.enrollment_id = e.enrollment_id
        WHERE e.program_id = ? AND a.attendance_date = ? AND e.status = 'active'
    ''', (program['program_id'], date)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in records])


@app.route('/api/dismissal/attendance', methods=['POST'])
def save_dismissal_attendance():
    data = request.json
    student_id = data.get('student_id')
    date = data.get('date')
    status = data.get('status', '')
    if not student_id or not date:
        return jsonify({'error': 'Missing student_id or date'}), 400
    conn = get_db_connection()
    program = conn.execute("SELECT program_id FROM programs WHERE program_name = 'General Attendance' AND status = 'active' LIMIT 1").fetchone()
    if not program:
        conn.close()
        return jsonify({'error': 'General Attendance program not found'}), 404
    enrollment = conn.execute("SELECT enrollment_id FROM enrollments WHERE student_id = ? AND program_id = ? AND status = 'active'", (student_id, program['program_id'])).fetchone()
    if not enrollment:
        conn.close()
        return jsonify({'error': 'Student not enrolled in General Attendance'}), 404
    enrollment_id = enrollment['enrollment_id']
    if not status:
        conn.execute('DELETE FROM attendance_records WHERE enrollment_id = ? AND attendance_date = ?', (enrollment_id, date))
    else:
        existing = conn.execute('SELECT attendance_id FROM attendance_records WHERE enrollment_id = ? AND attendance_date = ?', (enrollment_id, date)).fetchone()
        if existing:
            conn.execute('UPDATE attendance_records SET status=?, recorded_at=CURRENT_TIMESTAMP WHERE attendance_id=?', (status, existing['attendance_id']))
        else:
            conn.execute('INSERT INTO attendance_records (enrollment_id, attendance_date, status, recorded_by, notes) VALUES (?,?,?,1,"")', (enrollment_id, date, status))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/dismissal/students', methods=['GET'])
def get_dismissal_students():
    """Get all active students with their dismissal defaults"""
    conn = get_db_connection()
    students = conn.execute('''
        SELECT student_id, first_name, last_name, grade,
               dismissal_mon, dismissal_tue, dismissal_wed,
               dismissal_thu, dismissal_fri, before_care
        FROM students
        WHERE status = 'active'
        ORDER BY last_name, first_name
    ''').fetchall()
    conn.close()
    return jsonify([dict(s) for s in students])

@app.route('/api/dismissal/plan/<date>', methods=['GET'])
def get_dismissal_plan(date):
    """Get the daily dismissal plan for a specific date"""
    conn = get_db_connection()
    plan = conn.execute('''
        SELECT d.dismissal_id, d.student_id, d.dismissal_type,
               d.destination, d.notes, d.is_override, d.recorded_at
        FROM daily_dismissal d
        WHERE d.dismissal_date = ?
        ORDER BY d.recorded_at
    ''', (date,)).fetchall()
    conn.close()
    return jsonify([dict(p) for p in plan])

@app.route('/api/dismissal/plan', methods=['POST'])
def save_dismissal_plan():
    """Save or update a single student's dismissal for a date"""
    data = request.json
    student_id  = data.get('student_id')
    date        = data.get('dismissal_date')
    d_type      = data.get('dismissal_type')
    destination = data.get('destination', '')
    notes       = data.get('notes', '')
    is_override = data.get('is_override', 0)

    if not all([student_id, date, d_type]):
        return jsonify({'error': 'Missing required fields'}), 400

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO daily_dismissal
            (student_id, dismissal_date, dismissal_type, destination, notes, is_override)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(student_id, dismissal_date)
        DO UPDATE SET
            dismissal_type = excluded.dismissal_type,
            destination    = excluded.destination,
            notes          = excluded.notes,
            is_override    = excluded.is_override,
            recorded_at    = CURRENT_TIMESTAMP
    ''', (student_id, date, d_type, destination, notes, is_override))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/dismissal/plan/bulk', methods=['POST'])
def save_dismissal_bulk():
    """Bulk assign a destination to multiple students"""
    data        = request.json
    student_ids = data.get('student_ids', [])
    date        = data.get('dismissal_date')
    d_type      = data.get('dismissal_type')
    destination = data.get('destination', '')

    if not all([student_ids, date, d_type]):
        return jsonify({'error': 'Missing required fields'}), 400

    conn = get_db_connection()
    for sid in student_ids:
        conn.execute('''
            INSERT INTO daily_dismissal
                (student_id, dismissal_date, dismissal_type, destination, notes, is_override)
            VALUES (?, ?, ?, ?, '', 0)
            ON CONFLICT(student_id, dismissal_date)
            DO UPDATE SET
                dismissal_type = excluded.dismissal_type,
                destination    = excluded.destination,
                recorded_at    = CURRENT_TIMESTAMP
        ''', (sid, date, d_type, destination))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'updated': len(student_ids)})

@app.route('/api/dismissal/load-defaults', methods=['POST'])
def load_dismissal_defaults():
    """Auto-populate today's plan from each student's weekly defaults.
       Only fills in students who don't already have a record for this date."""
    data    = request.json
    date    = data.get('date')
    day_key = data.get('day_key')  # 'mon', 'tue', etc.

    if not date or not day_key:
        return jsonify({'error': 'Missing date or day_key'}), 400

    valid_days = ['mon', 'tue', 'wed', 'thu', 'fri']
    if day_key not in valid_days:
        return jsonify({'error': 'Invalid day'}), 400

    col  = f'dismissal_{day_key}'
    conn = get_db_connection()

    existing = set(row[0] for row in conn.execute(
        'SELECT student_id FROM daily_dismissal WHERE dismissal_date = ?', (date,)
    ).fetchall())

    students = conn.execute(f'''
        SELECT student_id, {col} as default_type
        FROM students
        WHERE status = 'active' AND {col} IS NOT NULL AND {col} != ''
    ''').fetchall()

    inserted = 0
    for s in students:
        if s['student_id'] not in existing:
            dest = 'Aftercare' if s['default_type'] == 'activity' else ''
            conn.execute('''
                INSERT OR IGNORE INTO daily_dismissal
                    (student_id, dismissal_date, dismissal_type, destination, notes, is_override)
                VALUES (?, ?, ?, ?, '', 0)
            ''', (s['student_id'], date, s['default_type'], dest))
            inserted += 1

    conn.commit()
    conn.close()
    return jsonify({'success': True, 'inserted': inserted})

# ============================================
# PEOPLE — Staff Management
# ============================================

@app.route('/people')
@superadmin_required
def people():
    """Serve the People management page"""
    conn = get_db_connection()
    try:
        conn.execute('ALTER TABLE staff ADD COLUMN title TEXT')
        conn.commit()
    except Exception:
        pass
    conn.close()
    return send_from_directory('.', 'people.html')

@app.route('/api/people/staff', methods=['GET'])
@login_required
def get_people_staff():
    """Return all staff members"""
    conn = get_db_connection()
    try:
        conn.execute('ALTER TABLE staff ADD COLUMN title TEXT')
        conn.commit()
    except Exception:
        pass
    rows = conn.execute('''
        SELECT staff_id, first_name, last_name, email, role, status,
               can_record_attendance, can_manage_billing, title
        FROM staff ORDER BY last_name, first_name
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/people/staff', methods=['POST'])
@superadmin_required
def add_people_staff():
    """Add a new staff member"""
    data = request.get_json()
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO staff (first_name, last_name, email, role, title, status,
                           can_record_attendance, can_manage_billing, hire_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('first_name'), data.get('last_name'), data.get('email'),
        data.get('role', 'staff'), data.get('title', ''),
        data.get('status', 'active'),
        data.get('can_record_attendance', 0), data.get('can_manage_billing', 0),
        '2025-09-01'
    ))
    conn.commit()
    conn.close()
    return jsonify({'success': True}), 201

@app.route('/api/people/staff/<int:staff_id>', methods=['PUT'])
@superadmin_required
def update_people_staff(staff_id):
    """Update a staff member"""
    data = request.get_json()
    conn = get_db_connection()
    fields = []
    values = []
    allowed = ['first_name', 'last_name', 'email', 'role', 'title', 'status',
               'can_record_attendance', 'can_manage_billing']
    for field in allowed:
        if field in data:
            fields.append(f'{field} = ?')
            values.append(data[field])
    if not fields:
        return jsonify({'error': 'No fields to update'}), 400
    values.append(staff_id)
    conn.execute(f'UPDATE staff SET {", ".join(fields)} WHERE staff_id = ?', values)
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/people/staff/<int:staff_id>', methods=['DELETE'])
@superadmin_required
def delete_people_staff(staff_id):
    """Delete a staff member"""
    conn = get_db_connection()
    conn.execute('DELETE FROM staff WHERE staff_id = ?', (staff_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ============================================
# BACKUP ENDPOINT
# ============================================

BACKUP_PASSWORD = 'school2026'  # Change this to something only you know!

@app.route('/backup/download', methods=['GET'])
def download_backup():
    """Download the live database file - password protected"""
    password = request.args.get('key', '')
    
    if password != BACKUP_PASSWORD:
        return jsonify({'error': 'Unauthorized - invalid key'}), 401
    
    if not os.path.exists(DATABASE):
        return jsonify({'error': 'Database not found'}), 404
    
    directory = os.path.dirname(os.path.abspath(DATABASE))
    filename = os.path.basename(DATABASE)
    
    from flask import send_file
    from datetime import datetime
    backup_name = f"school_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    
    return send_file(
        DATABASE,
        as_attachment=True,
        download_name=backup_name,
        mimetype='application/octet-stream'
    )

@app.route('/backup/restore', methods=['GET', 'POST'])
def restore_backup():
    """Upload and restore a database file - password protected"""
    if request.method == 'GET':
        password = request.args.get('key', '')
        if password != BACKUP_PASSWORD:
            return jsonify({'error': 'Unauthorized - invalid key'}), 401
        # Show a simple upload form
        return f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Restore Database</title>
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 500px; margin: 80px auto; padding: 20px; }}
                h2 {{ color: #1a3a5c; }}
                .warning {{ background: #fff3cd; border: 1px solid #ffc107; padding: 12px; border-radius: 6px; margin-bottom: 20px; }}
                input[type=file] {{ margin: 10px 0 20px; display: block; }}
                button {{ background: #1a3a5c; color: white; padding: 10px 24px; border: none; border-radius: 6px; cursor: pointer; font-size: 16px; }}
                button:hover {{ background: #c8a84b; }}
            </style>
        </head>
        <body>
            <h2>🗄️ Restore Database</h2>
            <div class="warning">
                ⚠️ <strong>Warning:</strong> This will completely replace the live database. Make sure you have a backup first.
            </div>
            <form method="POST" enctype="multipart/form-data">
                <input type="hidden" name="key" value="{password}">
                <label>Select .db file to restore:</label>
                <input type="file" name="db_file" accept=".db" required>
                <button type="submit">Upload & Restore</button>
            </form>
        </body>
        </html>
        '''

    # POST - handle the upload
    password = request.form.get('key', '')
    if password != BACKUP_PASSWORD:
        return jsonify({'error': 'Unauthorized - invalid key'}), 401

    if 'db_file' not in request.files:
        return 'No file uploaded', 400

    db_file = request.files['db_file']
    if not db_file.filename.endswith('.db'):
        return 'File must be a .db file', 400

    # Save to a temp location first, then replace
    import tempfile, shutil
    with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp:
        db_file.save(tmp.name)
        tmp_path = tmp.name

    # Verify it's a valid SQLite file
    try:
        import sqlite3
        test_conn = sqlite3.connect(tmp_path)
        test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        test_conn.close()
    except Exception as e:
        os.remove(tmp_path)
        return f'Invalid database file: {e}', 400

    # Replace the live database
    shutil.move(tmp_path, DATABASE)

    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Restore Complete</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 500px; margin: 80px auto; padding: 20px; }
            .success { background: #d4edda; border: 1px solid #28a745; padding: 16px; border-radius: 6px; }
            a { color: #1a3a5c; }
        </style>
    </head>
    <body>
        <div class="success">
            ✅ <strong>Database restored successfully!</strong><br><br>
            The live database has been replaced.<br><br>
            <a href="/">← Back to Admin Portal</a>
        </div>
    </body>
    </html>
    '''

# ============================================
# RUN SERVER
# ============================================

if __name__ == '__main__':
    # Initialize dismissal tables on startup
    init_dismissal_tables()

    # Check if database exists
    if not os.path.exists(DATABASE):
        print(f"WARNING: Database file '{DATABASE}' not found!")
        print("Please make sure your school.db file is in the same folder as this script.")
        print("Or update the DATABASE variable at the top of this file with the correct path.")
    else:
        print(f"✓ Database found: {DATABASE}")
    
    print("\n" + "="*50)
    print("🚀 School Attendance System Server Starting...")
    print("="*50)
    print("\nServer will be available at: http://localhost:5000")
    print("\nTo use the attendance form:")
    print("1. Open your web browser")
    print("2. Go to: http://localhost:5000")
    print("\nPress Ctrl+C to stop the server")
    print("="*50 + "\n")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
