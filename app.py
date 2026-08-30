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
import json

def parse_time_to_minutes(time_str):
    """Parse 'HH:MM', 'H:MM PM', etc. into total minutes since midnight."""
    s = str(time_str).strip().upper()
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
    elif not pm_offset and h == 12:
        h = 0  # 12:xx AM = 0:xx
    return h * 60 + m

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

# Always make HTML and JS revalidate, so a deploy takes effect on the next page load without a
# hard-refresh. (Admin pages were being served from browser cache, so staff kept running old code
# after a deploy — e.g. the scheduler generating with pre-update rules.) Files still cache but the
# browser must check with the server first, which returns a cheap 304 when nothing changed.
@app.after_request
def _revalidate_html_js(resp):
    ct = (resp.headers.get("Content-Type") or "").lower()
    if ct.startswith("text/html") or "javascript" in ct:
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        resp.headers.pop("Expires", None)
    return resp

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

# ============================================
# PERMISSION MODEL — fine-grained, per-page
# ============================================
# Every gated page has a stable permission KEY. A staff member's `permissions`
# column stores a JSON list of the keys they hold. Keys are grouped into silos
# purely for display (the "select all" tick in the staff editor toggles a silo).
#
# Two tiers sit above per-page keys:
#   * SUPERADMIN_EMAIL  — the hardcoded root; always holds every key. Its own
#     permissions can never be edited away.
#   * can_manage_permissions ("effective superadmin") — granted only by the
#     superadmin. Holders implicitly hold every page key AND may edit other
#     people's permissions (but not their own, and not the superadmin's).
#
# The Reference silo (family directory, dismissal staff view, bus dashboard,
# attendance report) is intentionally NOT gated: it is read-only and open to any
# signed-in staff member.

PERMISSION_SILOS = [
    {"key": "daily_input", "label": "Daily Input", "pages": [
        {"key": "homeroom_attendance", "label": "Homeroom Attendance",   "href": "/homeroom-attendance"},
        {"key": "daily_ops",           "label": "Daily Ops",             "href": "/dismissal"},
        {"key": "mcard",               "label": "M Card Snack Tracker",  "href": "/mcard"},
        {"key": "program_attendance",  "label": "Program Attendance",    "href": "/program-attendance"},
        {"key": "aftercare",           "label": "Before & Aftercare",    "href": "/aftercare"},
        {"key": "school_store",        "label": "School Store",          "href": "/school-store"},
        {"key": "dismissal_options",   "label": "Activities & Bus Routes","href": "/dismissal-options"},
    ]},
    {"key": "people", "label": "People", "pages": [
        {"key": "students",        "label": "Student Directory", "href": "/students"},
        {"key": "family_manager",  "label": "Family Manager",    "href": "/family-manager"},
        {"key": "staff_directory", "label": "Staff Directory",   "href": "/staff"},
        {"key": "classes",         "label": "Classes",           "href": "/classes"},
        {"key": "scheduler",       "label": "Scheduler",         "href": "/scheduler"},
        {"key": "special_services", "label": "Special Services", "href": "/special-services"},
        {"key": "rooms",           "label": "Rooms",             "href": "/rooms"},
        {"key": "report_card_templates", "label": "Report Card Templates", "href": "/report-card-templates"},
        {"key": "report_cards",          "label": "Report Cards",          "href": "/report-cards"},
    ]},
    {"key": "billing", "label": "Billing", "pages": [
        {"key": "billing_rates",   "label": "Billing Rates",   "href": "/billing-rates"},
        {"key": "school_calendar", "label": "School Calendar", "href": "/school-calendar"},
        {"key": "lunch_dashboard", "label": "Lunch Dashboard", "href": "/lunch-dashboard"},
        {"key": "billing_report",  "label": "Billing Reports", "href": "/billing-report"},
        {"key": "financial_aid",   "label": "Financial Aid",   "href": "/financial-aid"},
    ]},
]

ALL_PERMISSION_KEYS = [p["key"] for silo in PERMISSION_SILOS for p in silo["pages"]]
SILO_KEYS = {silo["key"]: [p["key"] for p in silo["pages"]] for silo in PERMISSION_SILOS}

# ── Global navigation menu ───────────────────────────────────────────────────
# The dropdown bar on every page (served by /nav.js, fed by /api/nav) is built
# from PERMISSION_SILOS above, so a new gated page shows up in the menu the
# moment it gets a permission key + href — no HTML edits anywhere.
#
# The Reference group lives here instead of in PERMISSION_SILOS because those
# pages are read-only and deliberately open to any signed-in staff member.
NAV_REFERENCE = {"key": "reference", "label": "Reference", "pages": [
    {"key": "family_directory",   "label": "Family Directory",    "href": "/family-directory"},
    {"key": "dismissal_staff",    "label": "Dismissal Staff View", "href": "/dismissal-staff"},
    {"key": "bus_dashboard",      "label": "Bus Dashboard",       "href": "/bus-dashboard"},
    {"key": "attendance_report",  "label": "Attendance Report",   "href": "/homeroom-attendance-report"},
    {"key": "person_schedules",   "label": "Student & Teacher Schedules", "href": "/schedule"},
]}

# Order the menus appear in the bar.
NAV_GROUP_ORDER = ["daily_input", "reference", "people", "billing"]


def permissions_for_staff(staff):
    """Return the list of permission keys a staff row holds.

    Prefers the JSON `permissions` column; falls back to deriving keys from the
    legacy boolean flags for any row not yet migrated. Always filtered to known
    keys so a stale/renamed key can never leak through.
    """
    raw = staff.get("permissions")
    if raw:
        try:
            keys = json.loads(raw) if isinstance(raw, str) else list(raw)
            if isinstance(keys, list):
                return [k for k in keys if k in ALL_PERMISSION_KEYS]
        except Exception:
            pass
    # Fallback for a row with no permissions list yet. Mirrors the init_db
    # backfill: the everyday daily-input pages were reachable by all staff, so
    # everyone keeps them; the People and Billing silos follow the old flags.
    keys = [k for k in SILO_KEYS["daily_input"] if k != "dismissal_options"]
    if staff.get("can_manage_people"):
        keys += SILO_KEYS["people"] + ["dismissal_options"]
    if staff.get("can_manage_billing"):
        keys += SILO_KEYS["billing"]
    return sorted(set(keys))


def legacy_flags_from_keys(keys):
    """Derive the legacy silo booleans from a permission-key list, so old code
    paths (people_required, billing back-dating, the recorder dropdown) stay
    consistent with the fine-grained model."""
    ks = set(keys)
    return {
        "can_record_attendance": 1 if ks & set(SILO_KEYS["daily_input"]) else 0,
        "can_manage_people":     1 if ks & set(SILO_KEYS["people"]) else 0,
        "can_manage_billing":    1 if ks & set(SILO_KEYS["billing"]) else 0,
    }


def has_perm(key):
    """True if the current session may access the given permission key."""
    if session.get("is_superadmin") or session.get("can_manage_permissions"):
        return True
    return key in (session.get("permissions") or [])


DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ── LOCAL DEVELOPMENT ONLY — never active on the live site ──
# When the DEV_LOGIN environment variable equals "1", the app skips Google
# sign-in and auto-logs in as the superadmin so you can test a local copy
# without OAuth setup. This is set ONLY by the local run script (run_local.command).
# Render does not set DEV_LOGIN, so on the live site this is always False and the
# normal Google login is fully enforced.
DEV_LOGIN = os.environ.get("DEV_LOGIN") == "1"


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


# ── LOCAL DEV auto-login (inert unless DEV_LOGIN=1) ──

@app.before_request
def dev_auto_login():
    # Guarded by DEV_LOGIN, which is never set on the live site. Auto-signs in
    # as the superadmin for local development only.
    if DEV_LOGIN and not session.get("user_email"):
        session["user_email"] = SUPERADMIN_EMAIL
        session["user_name"] = "Local Dev"
        session["is_superadmin"] = True
        session["can_record_attendance"] = True
        session["can_manage_billing"] = True
        session["can_manage_people"] = True
        session["can_manage_permissions"] = True
        session["permissions"] = list(ALL_PERMISSION_KEYS)
        session["user_role"] = "superadmin"


# ── Refresh permissions from DB on every request ──

@app.before_request
def refresh_session_permissions():
    email = session.get("user_email")
    if not email:
        return
    if email == SUPERADMIN_EMAIL:
        session["is_superadmin"] = True
        session["can_manage_permissions"] = True
        session["permissions"] = list(ALL_PERMISSION_KEYS)
        session["can_record_attendance"] = True
        session["can_manage_billing"] = True
        session["can_manage_people"] = True
        return
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT can_record_attendance, can_manage_billing, can_manage_people, can_manage_permissions, permissions, role FROM staff WHERE email=%s AND status='active'", (email,))
            staff = fo(cur)
        conn.close()
        if staff:
            perms = permissions_for_staff(staff)
            flags = legacy_flags_from_keys(perms)
            session["permissions"] = perms
            session["can_manage_permissions"] = bool(staff.get("can_manage_permissions"))
            session["can_record_attendance"] = bool(flags["can_record_attendance"])
            session["can_manage_billing"] = bool(flags["can_manage_billing"])
            session["can_manage_people"] = bool(flags["can_manage_people"])
            session["user_role"] = staff.get("role")
        else:
            session.clear()
    except Exception:
        pass

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


def require_perm(key):
    """Gate a route on a single per-page permission key. Returns a JSON 403 for
    API routes (path starts with /api/) and a redirect for page routes."""
    def deco(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("user_email"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Not signed in"}), 401
                return redirect("/login")
            if not has_perm(key):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "You don't have permission for this."}), 403
                return redirect("/?error=unauthorized")
            return f(*args, **kwargs)
        return decorated
    return deco


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

            # Dismissal options (bus routes & activities) managed by admins
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dismissal_options (
                    option_id      SERIAL PRIMARY KEY,
                    name           TEXT NOT NULL,
                    type           TEXT NOT NULL,
                    active         BOOLEAN NOT NULL DEFAULT TRUE,
                    display_order  INTEGER NOT NULL DEFAULT 0,
                    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(name, type)
                )
            """)
            cur.execute("SELECT COUNT(*) FROM dismissal_options")
            if cur.fetchone()[0] == 0:
                bus_routes = ['Arlington','Brewster','Carmel','Dover','HOPE','Minivan','Pawling','POK','Wappingers']
                activities = ['Aftercare','Piano Lessons','LS HW Club','LS MiST','MS HW Club','MS MiST',
                              'JV Soccer','V Soccer','Basketball','Tutoring','KABARET','Book Club','Other']
                for i, name in enumerate(bus_routes):
                    cur.execute("INSERT INTO dismissal_options (name, type, display_order) VALUES (%s, 'bus', %s) ON CONFLICT DO NOTHING", (name, i))
                for i, name in enumerate(activities):
                    cur.execute("INSERT INTO dismissal_options (name, type, display_order) VALUES (%s, 'activity', %s) ON CONFLICT DO NOTHING", (name, i))

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
                    record_id        SERIAL PRIMARY KEY,
                    student_id       INTEGER NOT NULL,
                    program_type     TEXT NOT NULL,
                    session_date     TEXT NOT NULL,
                    units            NUMERIC(3,1) NOT NULL DEFAULT 1,
                    teacher          TEXT DEFAULT '',
                    duration_minutes INTEGER NOT NULL DEFAULT 60,
                    recorded_by      TEXT DEFAULT '',
                    recorded_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_id, program_type, session_date)
                )
            """)
            # Migrate: add duration_minutes column on existing tables (backfills to 60 for all rows)
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='program_attendance' AND column_name='duration_minutes'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE program_attendance ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 60")
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
            # Lunch rates — seeded idempotently so they land on redeploy even when the
            # billing_rates table is already populated. Pizza bills at the regular per-day rate.
            lunch_rate_defaults = [
                ('lunch_rate_ec',  4.50,   'EC Lunch (JPK/SPK/K)', 'per day'),
                ('lunch_rate_1_8', 5.50,   'Lunch (Grades 1–8)',   'per day'),
                ('lunch_fy_ec',    742.50, 'Full Year — EC',       'per year'),
                ('lunch_fy_1_8',   876.75, 'Full Year — Grades 1–8','per year'),
            ]
            for key, val, label, unit in lunch_rate_defaults:
                cur.execute(
                    "INSERT INTO billing_rates (rate_key, rate_value, label, unit, effective_from) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (rate_key, effective_from) DO NOTHING",
                    (key, val, label, unit, '2025-09-01')
                )
            # ── Lunch enrollment: one row per student per school year ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lunch_enrollment (
                    enrollment_id           SERIAL PRIMARY KEY,
                    student_id              INTEGER NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                    school_year             TEXT NOT NULL,
                    grade_at_time_of_record TEXT NOT NULL DEFAULT '',
                    months                  JSONB NOT NULL DEFAULT '{}'::jsonb,
                    notes                   TEXT DEFAULT '',
                    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by              TEXT DEFAULT '',
                    UNIQUE(student_id, school_year)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_lunch_enrollment_year ON lunch_enrollment(school_year)")
            # ── Comp Rates (staff compensation) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS comp_rates (
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
            cur.execute("SELECT COUNT(*) FROM comp_rates")
            if cur.fetchone()[0] == 0:
                comp_defaults = [
                    ('comp_og_session',       70, 'OG Tutoring',      'per session'),
                    ('comp_homework_hourly',  20, 'Homework Center',  'per hour'),
                    ('comp_tutoring_session', 50, '1-on-1 Tutoring',  'per session'),
                ]
                for key, val, label, unit in comp_defaults:
                    cur.execute(
                        "INSERT INTO comp_rates (rate_key, rate_value, label, unit, effective_from) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (key, val, label, unit, '2025-09-01')
                    )
            # ── School Calendar: categories + per-day tags ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS calendar_categories (
                    category_id   SERIAL PRIMARY KEY,
                    key           TEXT NOT NULL UNIQUE,
                    label         TEXT NOT NULL,
                    color         TEXT NOT NULL DEFAULT '#c8992a',
                    sort_order    INTEGER NOT NULL DEFAULT 0,
                    active        BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS calendar_day_tags (
                    tag_id       SERIAL PRIMARY KEY,
                    day_date     DATE NOT NULL,
                    category_key TEXT NOT NULL REFERENCES calendar_categories(key) ON DELETE CASCADE ON UPDATE CASCADE,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_by   TEXT,
                    UNIQUE(day_date, category_key)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_calendar_day_tags_date ON calendar_day_tags(day_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_calendar_day_tags_cat_date ON calendar_day_tags(category_key, day_date)")
            # Seed / upsert default categories (idempotent — new defaults land on redeploy)
            seed_cats = [
                ('lunch_day_prek_k',     'Lunch Day (PreK–K)',    '#d4a63d',  1),
                ('lunch_day_1_8',        'Lunch Day (1–8)',       '#8a6a1a',  2),
                ('school_day',           'School Day',            '#1a2744',  3),
                ('half_day',             'Half Day',              '#6b7280',  4),
                ('holiday',              'Holiday',               '#4a1a2c',  5),
                ('teacher_conference',   'Teacher Conference',    '#3b6b4a',  6),
                ('teacher_development',  'Teacher Development',   '#a0522d',  7),
            ]
            for key, label, color, sort_order in seed_cats:
                cur.execute(
                    "INSERT INTO calendar_categories (key, label, color, sort_order) VALUES (%s,%s,%s,%s) ON CONFLICT (key) DO NOTHING",
                    (key, label, color, sort_order)
                )
            # Retire legacy single 'lunch_day' category — cascades to any tags that were applied to it.
            cur.execute("DELETE FROM calendar_categories WHERE key = 'lunch_day'")
            # ── App settings (generic key/value) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by TEXT
                )
            """)
            # School year rolls over July 1 by default (MM-DD). Editable on the calendar page.
            cur.execute("""
                INSERT INTO app_settings (key, value) VALUES ('school_year_rollover', '07-01')
                ON CONFLICT (key) DO NOTHING
            """)
            # ── School years: single source of truth for "what year is it" + trimester windows ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS school_years (
                    start_year  INTEGER PRIMARY KEY,
                    first_day   DATE,
                    last_day    DATE,
                    t1_start    DATE,
                    t1_end      DATE,
                    t2_start    DATE,
                    t2_end      DATE,
                    t3_start    DATE,
                    t3_end      DATE,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by  TEXT
                )
            """)
            # Seed the known 2025-26 windows (previously hard-coded) + a blank 2026-27 to fill in.
            cur.execute("""
                INSERT INTO school_years
                    (start_year, first_day, last_day, t1_start, t1_end, t2_start, t2_end, t3_start, t3_end)
                VALUES
                    (2025, '2025-09-02', '2026-06-12',
                     '2025-09-02', '2025-11-21', '2025-11-22', '2026-02-22', '2026-02-23', '2026-06-12')
                ON CONFLICT (start_year) DO NOTHING
            """)
            cur.execute("""
                INSERT INTO school_years (start_year) VALUES (2026)
                ON CONFLICT (start_year) DO NOTHING
            """)
            # ── Rooms, Sections (classes), and section rosters ──
            # A "section" unifies homeroom / advisory / subject class / elective:
            # one teacher, one room, a term, and a roster. Homeroom stays readable via
            # students.homeroom_teacher_id (kept as a synced shadow column for now), so
            # the attendance / dismissal / bus pages keep working while we build this out.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    room_id     SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    active      BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order  INTEGER NOT NULL DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sections (
                    section_id        SERIAL PRIMARY KEY,
                    school_year_start INTEGER NOT NULL,
                    type              TEXT NOT NULL DEFAULT 'subject',
                    name              TEXT NOT NULL,
                    subject           TEXT,
                    grade             TEXT,
                    term              TEXT NOT NULL DEFAULT 'year',
                    teacher_id        INTEGER REFERENCES staff(staff_id) ON DELETE SET NULL,
                    room_id           INTEGER REFERENCES rooms(room_id) ON DELETE SET NULL,
                    active            BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by        TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sections_year_type ON sections(school_year_start, type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sections_teacher ON sections(teacher_id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS section_enrollments (
                    section_id  INTEGER NOT NULL REFERENCES sections(section_id) ON DELETE CASCADE,
                    student_id  INTEGER NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(section_id, student_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_section_enroll_student ON section_enrollments(student_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_section_enroll_section ON section_enrollments(section_id)")
            # ── In-session special services ──
            # A second, much simpler schedule for during-the-school-day support
            # (OG tutoring, push-in, pull-out). One row per staff+student+period+day
            # session. The service list itself is staff-managed on the same page.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS special_service_types (
                    service_id  SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE,
                    active      BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order  INTEGER NOT NULL DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for _i, _nm in enumerate(("OG Tutoring", "Push-In Support", "Pull-Out Support")):
                cur.execute("""INSERT INTO special_service_types (name, sort_order)
                               VALUES (%s, %s) ON CONFLICT (name) DO NOTHING""", (_nm, _i))
            cur.execute("""
                CREATE TABLE IF NOT EXISTS special_service_sessions (
                    ss_id             SERIAL PRIMARY KEY,
                    school_year_start INTEGER NOT NULL,
                    day               TEXT NOT NULL,
                    period            TEXT NOT NULL,
                    staff_id          INTEGER REFERENCES staff(staff_id) ON DELETE SET NULL,
                    student_id        INTEGER REFERENCES students(student_id) ON DELETE CASCADE,
                    room_id           INTEGER REFERENCES rooms(room_id) ON DELETE SET NULL,
                    service_id        INTEGER REFERENCES special_service_types(service_id) ON DELETE SET NULL,
                    notes             TEXT,
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by        TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ss_sessions_slot ON special_service_sessions(school_year_start, day, period)")
            # Marks a staff member as available in the special-services staff dropdown.
            cur.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS is_special_services INTEGER NOT NULL DEFAULT 0")
            # ── Scheduler state ──
            # The master-schedule generator (Scheduler page) persists its full working
            # state — inputs, the generated grid, and manual edits — as one JSON blob per
            # school year, so it lives on the server instead of downloaded files.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_state (
                    school_year_start INTEGER PRIMARY KEY,
                    data              TEXT,
                    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_by        TEXT
                )
            """)
            # Optimistic-concurrency version counter (bumps on every save) so a stale
            # browser tab can't silently overwrite a newer save from another session.
            cur.execute("ALTER TABLE scheduler_state ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
            _seed_report_cards(cur)
            # ── Households, Parents, and linking tables ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS households (
                    household_id   SERIAL PRIMARY KEY,
                    family_name    TEXT NOT NULL,
                    address_line_1 TEXT,
                    address_line_2 TEXT,
                    city           TEXT,
                    state          TEXT,
                    zip            TEXT,
                    primary_phone  TEXT,
                    primary_email  TEXT,
                    status         TEXT NOT NULL DEFAULT 'active',
                    billing_notes  TEXT,
                    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS parents (
                    parent_id         SERIAL PRIMARY KEY,
                    first_name        TEXT NOT NULL,
                    last_name         TEXT NOT NULL,
                    email             TEXT,
                    phone             TEXT,
                    relationship_type TEXT,
                    can_pickup        BOOLEAN DEFAULT TRUE,
                    portal_status     TEXT NOT NULL DEFAULT 'inactive',
                    password_hash     TEXT,
                    email_verified    BOOLEAN DEFAULT FALSE,
                    last_login        TIMESTAMP,
                    notes             TEXT,
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS household_members (
                    household_id INTEGER NOT NULL REFERENCES households(household_id) ON DELETE CASCADE,
                    parent_id    INTEGER NOT NULL REFERENCES parents(parent_id) ON DELETE CASCADE,
                    role         TEXT NOT NULL DEFAULT 'primary',
                    PRIMARY KEY (household_id, parent_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS student_households (
                    student_id   INTEGER NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                    household_id INTEGER NOT NULL REFERENCES households(household_id) ON DELETE CASCADE,
                    is_primary   BOOLEAN DEFAULT TRUE,
                    custody_notes TEXT,
                    PRIMARY KEY (student_id, household_id)
                )
            """)
            # Migrate: add directory opt-out flag to households. Families who opt out are
            # excluded from the shareable/public Family Directory PDF only — they remain
            # visible to staff on the directory page and on staff emergency sheets.
            cur.execute("ALTER TABLE households ADD COLUMN IF NOT EXISTS directory_opt_out BOOLEAN NOT NULL DEFAULT FALSE")
            # Migrate: add parent_id to staff if missing (for staff who are also parents)
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='staff' AND column_name='parent_id'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE staff ADD COLUMN parent_id INTEGER REFERENCES parents(parent_id) ON DELETE SET NULL")
            # Migrate: fine-grained per-page permissions.
            #   - `permissions` holds a JSON list of page keys (source of truth).
            #   - `can_manage_permissions` marks an "effective superadmin".
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='staff' AND column_name='permissions'
            """)
            has_permissions_col = cur.fetchone()
            if not has_permissions_col:
                cur.execute("ALTER TABLE staff ADD COLUMN permissions TEXT")
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='staff' AND column_name='can_manage_permissions'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE staff ADD COLUMN can_manage_permissions INTEGER NOT NULL DEFAULT 0")
            # Backfill: for any staff row without a permissions list yet, derive one
            # from the legacy booleans so nobody loses the access they have today.
            # Daily-input pages (except Activities & Bus Routes) were reachable by
            # every signed-in staff member, so they are granted to everyone here.
            cur.execute("""
                SELECT staff_id, can_record_attendance, can_manage_people, can_manage_billing
                FROM staff WHERE permissions IS NULL OR permissions = ''
            """)
            rows_to_backfill = cur.fetchall()
            for sid, rec_att, man_ppl, man_bill in rows_to_backfill:
                keys = [k for k in SILO_KEYS["daily_input"] if k != "dismissal_options"]
                if man_ppl:
                    keys += SILO_KEYS["people"] + ["dismissal_options"]
                if man_bill:
                    keys += SILO_KEYS["billing"]
                keys = sorted(set(keys))
                cur.execute("UPDATE staff SET permissions=%s WHERE staff_id=%s",
                            (json.dumps(keys), sid))
            # Migrate: add household_id to financial_aid_families if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_families' AND column_name='household_id'
            """)
            if not cur.fetchone():
                cur.execute("""
                    ALTER TABLE financial_aid_families ADD COLUMN household_id INTEGER
                    REFERENCES households(household_id) ON DELETE SET NULL
                """)
            # Add homeroom_teacher_id to students if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='students' AND column_name='homeroom_teacher_id'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE students ADD COLUMN homeroom_teacher_id INTEGER")

            # Add advisory_teacher_id to students if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='students' AND column_name='advisory_teacher_id'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE students ADD COLUMN advisory_teacher_id INTEGER REFERENCES staff(staff_id)")

            # Add cohort_color to students if missing. Holds a per-student Gold/Blue split
            # assignment ('gold' | 'blue' | NULL) used by the "Gold & Blue" tab on the
            # Classes page to bulk-populate subject-class rosters. Convenience default only —
            # section_enrollments stays the source of truth, so any roster can still be
            # hand-edited afterward.
            cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS cohort_color TEXT")

            # Add student health/safety + emergency-contact fields if missing (teacher dashboard)
            for col, typedef in [('allergies', 'TEXT'), ('medical_alert', 'TEXT'),
                                 ('medications', 'TEXT'), ('dietary', 'TEXT'),
                                 ('emergency_contact_name', 'TEXT'), ('emergency_contact_phone', 'TEXT')]:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name='students' AND column_name=%s
                """, (col,))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE students ADD COLUMN {col} {typedef}")

            # Student notes (teacher dashboard)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS student_notes (
                    note_id         SERIAL PRIMARY KEY,
                    student_id      INTEGER NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                    author_staff_id INTEGER REFERENCES staff(staff_id) ON DELETE SET NULL,
                    author_name     TEXT,
                    body            TEXT NOT NULL,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_student_notes_student ON student_notes(student_id)")

            # Add trimester, instructor_id, division columns to electives if missing
            for col, typedef in [('trimester', 'INTEGER'), ('instructor_id', 'INTEGER REFERENCES staff(staff_id)'), ('division', 'TEXT')]:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name='electives' AND column_name=%s
                """, (col,))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE electives ADD COLUMN {col} {typedef}")

            # Create student_electives junction table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS student_electives (
                    id SERIAL PRIMARY KEY,
                    student_id INTEGER REFERENCES students(student_id),
                    elective_id INTEGER REFERENCES electives(elective_id),
                    trimester INTEGER NOT NULL,
                    UNIQUE(student_id, trimester)
                )
            """)

            # Create store_items table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS store_items (
                    item_id        SERIAL PRIMARY KEY,
                    name           TEXT NOT NULL,
                    default_price  NUMERIC(10,2) NOT NULL,
                    available_colors TEXT,
                    available_sizes  TEXT,
                    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order     INTEGER NOT NULL DEFAULT 0
                )
            """)

            # Create store_purchases table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS store_purchases (
                    purchase_id   SERIAL PRIMARY KEY,
                    student_id    INTEGER NOT NULL,
                    item_id       INTEGER NOT NULL,
                    color         TEXT,
                    size          TEXT,
                    quantity      INTEGER NOT NULL DEFAULT 1,
                    unit_price    NUMERIC(10,2) NOT NULL,
                    purchase_date TEXT NOT NULL,
                    recorded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    recorded_by   TEXT
                )
            """)

            # Seed store items if empty
            cur.execute("SELECT COUNT(*) FROM store_items")
            if cur.fetchone()[0] == 0:
                seed_items = [
                    ("Water Bottle", 40.00, None, None, 1),
                    ("T-Shirt", 30.00, "Navy,White,Yellow", "YXS,YS,YM,YL,YXL,AS,AM,AL,AXXL", 2),
                    ("LS Shirt", 35.00, "Navy,White", "YXS,YS,YM,YL,YXL,AS,AM,AL,AXXL", 3),
                    ("LS Arm Logo", 35.00, "White", "YXS,YS,YM,YL,YXL,AS,AM,AL,AXXL", 4),
                    ("Hoodie", 40.00, None, "YXS,YS,YM,YL,YXL,AS,AM,AL,AXXL", 5),
                    ("Sweatpants", 45.00, "Navy", "YS,YM,YL,YXL,AS,AM,AL,AXXL", 6),
                    ("Shorts", 30.00, None, "YXS,YS,YM,YL,YXL,AS,AM,AL,AXXL", 7),
                    ("1/4 Zip", 40.00, "Navy", "AS,AM,AL,AXXL", 8),
                ]
                for name, price, colors, sizes, sort in seed_items:
                    cur.execute(
                        "INSERT INTO store_items (name, default_price, available_colors, available_sizes, sort_order) VALUES (%s,%s,%s,%s,%s)",
                        (name, price, colors, sizes, sort)
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
    # Teachers (anyone with an assigned section or a homeroom this year) land on
    # their own dashboard. Superadmin and office staff keep the portal home.
    if not session.get("is_superadmin") and _staff_is_teacher(session.get("user_email")):
        return redirect("/my-classroom")
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
        if staff and email != SUPERADMIN_EMAIL:
            perms = permissions_for_staff(staff)
            flags = legacy_flags_from_keys(perms)
            session["permissions"]           = perms
            session["can_manage_permissions"] = bool(staff.get("can_manage_permissions"))
            session["can_record_attendance"] = bool(flags["can_record_attendance"])
            session["can_manage_billing"]    = bool(flags["can_manage_billing"])
            session["can_manage_people"]     = bool(flags["can_manage_people"])
            session["user_role"]             = staff.get("role")
        else:
            session["permissions"]           = list(ALL_PERMISSION_KEYS)
            session["can_manage_permissions"] = True
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
        "can_manage_permissions": session.get("can_manage_permissions", False),
        "permissions": session.get("permissions", []),
        "can_record_attendance": session.get("can_record_attendance", False),
        "can_manage_billing": session.get("can_manage_billing", False),
        "can_manage_people": session.get("can_manage_people", False),
        "role": session.get("user_role"),
    })

@app.route("/logo.svg")
def serve_logo():
    return send_from_directory(".", "logo.svg")


# ============================================
# GLOBAL NAV BAR
# ============================================

@app.route("/nav.js")
def serve_nav_js():
    """The shared dropdown navigation bar. Included by every portal page."""
    return send_from_directory(".", "nav.js", mimetype="application/javascript")


@app.route("/api/nav")
def api_nav():
    """Menu structure for the global nav, filtered to what this user may open.

    Built from PERMISSION_SILOS + NAV_REFERENCE so the menu can never drift
    away from the permission model.
    """
    if not session.get("user_email"):
        return jsonify({"logged_in": False}), 401

    by_key = {silo["key"]: silo for silo in PERMISSION_SILOS}
    by_key[NAV_REFERENCE["key"]] = NAV_REFERENCE

    groups = []
    for group_key in NAV_GROUP_ORDER:
        silo = by_key.get(group_key)
        if not silo:
            continue
        if group_key == "reference":
            pages = [dict(p) for p in silo["pages"]]          # ungated
        else:
            pages = [dict(p) for p in silo["pages"] if has_perm(p["key"])]
        if pages:
            groups.append({"key": silo["key"], "label": silo["label"], "pages": pages})

    # Teachers get a direct link back to their own classroom dashboard.
    try:
        is_teacher = _staff_is_teacher(session.get("user_email"))
    except Exception:
        is_teacher = False
    if is_teacher:
        groups.insert(0, {"key": "teaching", "label": "My Classroom", "pages": [
            {"key": "my_classroom", "label": "My Classroom", "href": "/my-classroom"},
            {"key": "my_report_cards", "label": "Report Cards", "href": "/report-cards"},
        ]})

    return jsonify({
        "groups": groups,
        "user": {
            "name": session.get("user_name"),
            "email": session.get("user_email"),
        },
    })

@app.route("/attendance")
@login_required
def attendance():
    return send_from_directory(".", "attendance_form.html")

@app.route("/dismissal")
@require_perm("daily_ops")
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
@require_perm("mcard")
def mcard():
    return send_from_directory(".", "mcard_tracker.html")

@app.route("/students")
@require_perm("students")
def students():
    return send_from_directory(".", "students.html")

@app.route("/classes")
@require_perm("classes")
def classes_page():
    return send_from_directory(".", "classes.html")

@app.route("/reconcile")
@require_perm("classes")
def reconcile_page():
    # Read-only diagnostic: compares the Scheduler's timetabled classes against the
    # sections on the Classes page. Pulls only /api/scheduler and /api/sections; writes nothing.
    return send_from_directory(".", "reconcile.html")

@app.route("/schedule")
@login_required
def schedule_page():
    # Read-only weekly schedule viewer for any student or teacher. Any signed-in staff
    # may view; nothing is editable. Reads the generated timetable + section rosters.
    return send_from_directory(".", "schedule.html")

@app.route("/scheduler")
@login_required
def scheduler_page():
    # Viewable by any signed-in staff (read-only sandbox). Saving is gated separately by the
    # "scheduler" permission, enforced on the POST endpoint and reflected via /api/scheduler's can_edit.
    return send_from_directory(".", "scheduler.html")

@app.route("/rooms")
@require_perm("rooms")
def rooms_page():
    return send_from_directory(".", "rooms.html")

@app.route("/special-services")
@require_perm("special_services")
def special_services_page():
    return send_from_directory(".", "special_services.html")

@app.route("/staff")
@app.route("/people")
@require_perm("staff_directory")
def people():
    return send_from_directory(".", "people.html")

@app.route("/dismissal-options")
@require_perm("dismissal_options")
def dismissal_options_page():
    return send_from_directory(".", "dismissal_options.html")

@app.route("/homeroom-attendance")
@require_perm("homeroom_attendance")
def homeroom_attendance_page():
    return send_from_directory(".", "homeroom_attendance.html")

@app.route("/homeroom-attendance-report")
@login_required
def homeroom_attendance_report_page():
    return send_from_directory(".", "homeroom_attendance_report.html")

@app.route("/program-attendance")
@require_perm("program_attendance")
def program_attendance():
    return send_from_directory(".", "program_attendance.html")

@app.route("/aftercare")
@require_perm("aftercare")
def aftercare():
    return send_from_directory(".", "aftercare_attendance.html")

@app.route("/school-store")
@require_perm("school_store")
def school_store():
    return send_from_directory(".", "school_store.html")

@app.route("/billing-rates")
@require_perm("billing_rates")
def billing_rates():
    return send_from_directory(".", "billing_rates.html")

@app.route("/financial-aid")
@require_perm("financial_aid")
def financial_aid_page():
    return send_from_directory(".", "financial_aid.html")

@app.route("/school-calendar")
@require_perm("school_calendar")
def school_calendar_page():
    return send_from_directory(".", "school_calendar.html")

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
    try:
        from datetime import date as _date
        charge_dt = _date.fromisoformat(charge_date)
    except ValueError:
        return jsonify({"error":"Invalid date format"}),400
    first_of_month = datetime.today().date().replace(day=1)
    if charge_dt < first_of_month and not session.get("can_manage_billing"):
        return jsonify({"error":"This month is closed for changes or additions. Please contact the billing office at businessoffice@mizzentop.org."}),403
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
    from datetime import date as _date
    first_of_month = datetime.today().date().replace(day=1)
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT charge_date FROM mcard_charges WHERE charge_id=%s",(charge_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error":"Charge not found"}),404
            try:
                charge_dt = _date.fromisoformat(str(row["charge_date"]))
            except ValueError:
                charge_dt = datetime.today().date()
            if charge_dt < first_of_month and not session.get("can_manage_billing"):
                return jsonify({"error":"This month is closed for changes or additions. Please contact the billing office at businessoffice@mizzentop.org."}),403
            cur.execute("DELETE FROM mcard_charges WHERE charge_id=%s",(charge_id,))
        conn.commit()
        return jsonify({"success":True})
    finally:
        conn.close()


# ============================================
# SCHOOL STORE
# ============================================

@app.route("/api/store/items")
@login_required
def get_store_items():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM store_items ORDER BY sort_order, name")
            items = cur.fetchall()
            for item in items:
                if "default_price" in item:
                    item["default_price"] = float(item["default_price"])
        return jsonify(items)
    finally:
        conn.close()


@app.route("/api/store/items", methods=["POST"])
@login_required
def add_store_item():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    price = data.get("default_price")
    if not name or price is None:
        return jsonify({"error": "Name and price are required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO store_items (name, default_price, available_colors, available_sizes, sort_order)
                VALUES (%s, %s, %s, %s, COALESCE((SELECT MAX(sort_order)+1 FROM store_items), 1))
                RETURNING *
            """, (name, price, data.get("available_colors"), data.get("available_sizes")))
            item = cur.fetchone()
            conn.commit()
            if "default_price" in item:
                item["default_price"] = float(item["default_price"])
        return jsonify(item), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/store/items/<int:item_id>", methods=["PUT"])
@login_required
def update_store_item(item_id):
    data = request.get_json()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE store_items
                SET name=%s, default_price=%s, available_colors=%s, available_sizes=%s, is_active=%s
                WHERE item_id=%s RETURNING *
            """, (
                data.get("name"), data.get("default_price"),
                data.get("available_colors"), data.get("available_sizes"),
                data.get("is_active", True), item_id
            ))
            item = cur.fetchone()
            conn.commit()
        if not item:
            return jsonify({"error": "Item not found"}), 404
        if "default_price" in item:
            item["default_price"] = float(item["default_price"])
        return jsonify(item)
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/store/purchases")
@login_required
def get_store_purchases():
    month = request.args.get("month", "")  # YYYY-MM format
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if month:
                cur.execute("""
                    SELECT sp.*, s.first_name, s.last_name, si.name AS item_name
                    FROM store_purchases sp
                    JOIN students s ON sp.student_id = s.student_id
                    JOIN store_items si ON sp.item_id = si.item_id
                    WHERE sp.purchase_date LIKE %s
                    ORDER BY sp.purchase_date DESC, sp.recorded_at DESC
                """, (month + "%",))
            else:
                cur.execute("""
                    SELECT sp.*, s.first_name, s.last_name, si.name AS item_name
                    FROM store_purchases sp
                    JOIN students s ON sp.student_id = s.student_id
                    JOIN store_items si ON sp.item_id = si.item_id
                    ORDER BY sp.purchase_date DESC, sp.recorded_at DESC
                    LIMIT 200
                """)
            purchases = cur.fetchall()
        for p in purchases:
            if "unit_price" in p:
                p["unit_price"] = float(p["unit_price"])
        return jsonify(purchases)
    finally:
        conn.close()


@app.route("/api/store/purchases", methods=["POST"])
@login_required
def add_store_purchase():
    data = request.get_json()
    student_id = data.get("student_id")
    item_id = data.get("item_id")
    quantity = int(data.get("quantity", 1))
    unit_price = data.get("unit_price")
    purchase_date = data.get("purchase_date")
    color = data.get("color") or None
    size = data.get("size") or None

    if not all([student_id, item_id, unit_price, purchase_date]):
        return jsonify({"error": "Missing required fields"}), 400

    from datetime import date as dt_date
    try:
        pd = dt_date.fromisoformat(purchase_date)
        today = dt_date.today()
        first_of_month = today.replace(day=1)
        if pd < first_of_month and not session.get("can_manage_billing"):
            return jsonify({"error": "This month is closed for changes or additions. Please contact the billing office at businessoffice@mizzentop.org."}), 403
    except ValueError:
        pass

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO store_purchases (student_id, item_id, color, size, quantity, unit_price, purchase_date, recorded_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING purchase_id
            """, (student_id, item_id, color, size, quantity, unit_price, purchase_date, session.get("user_name", "unknown")))
            row = cur.fetchone()
            conn.commit()
        return jsonify({"purchase_id": row["purchase_id"]}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/store/purchases/<int:purchase_id>", methods=["DELETE"])
@login_required
def delete_store_purchase(purchase_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT purchase_date FROM store_purchases WHERE purchase_id=%s", (purchase_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Not found"}), 404

            from datetime import date as dt_date
            try:
                pd = dt_date.fromisoformat(row["purchase_date"])
                today = dt_date.today()
                first_of_month = today.replace(day=1)
                if pd < first_of_month and not session.get("can_manage_billing"):
                    return jsonify({"error": "This month is closed for changes or additions. Please contact the billing office at businessoffice@mizzentop.org."}), 403
            except ValueError:
                pass

            cur.execute("DELETE FROM store_purchases WHERE purchase_id=%s", (purchase_id,))
            conn.commit()
        return jsonify({"deleted": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
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
            cur.execute("SELECT elective_id, name, division, trimester FROM electives WHERE active=1 ORDER BY division, name")
            return jsonify(fa(cur))
    finally:
        conn.close()


# ============================================
# DISMISSAL OPTIONS (activities & bus routes)
# ============================================

@app.route("/api/dismissal-options")
@login_required
def get_dismissal_options():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if request.args.get("active") == "true":
                cur.execute("SELECT * FROM dismissal_options WHERE active=TRUE ORDER BY type, display_order, name")
            else:
                cur.execute("SELECT * FROM dismissal_options ORDER BY type, display_order, name")
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/dismissal-options", methods=["POST"])
@people_required
def create_dismissal_option():
    d = request.json
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(MAX(display_order),0)+1 AS next_order FROM dismissal_options WHERE type=%s
            """, (d['type'],))
            next_order = cur.fetchone()['next_order']
            cur.execute("""
                INSERT INTO dismissal_options (name, type, display_order)
                VALUES (%s, %s, %s) RETURNING option_id
            """, (d['name'], d['type'], next_order))
            option_id = cur.fetchone()['option_id']
            conn.commit()
            return jsonify({"success": True, "option_id": option_id}), 201
    finally:
        conn.close()

@app.route("/api/dismissal-options/<int:option_id>", methods=["PUT"])
@people_required
def update_dismissal_option(option_id):
    d = request.json
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            allowed = {'name', 'type', 'active', 'display_order'}
            fields = []
            vals = []
            for k, v in d.items():
                if k in allowed:
                    fields.append(f"{k}=%s")
                    vals.append(v)
            if not fields:
                return jsonify({"error": "No valid fields"}), 400
            fields.append("updated_at=CURRENT_TIMESTAMP")
            vals.append(option_id)
            cur.execute(f"UPDATE dismissal_options SET {','.join(fields)} WHERE option_id=%s", vals)
            conn.commit()
            return jsonify({"success": True})
    finally:
        conn.close()

@app.route("/api/dismissal-options/reorder", methods=["PUT"])
@people_required
def reorder_dismissal_options():
    d = request.json
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for item in d.get('orders', []):
                cur.execute("UPDATE dismissal_options SET display_order=%s, updated_at=CURRENT_TIMESTAMP WHERE option_id=%s",
                            (item['display_order'], item['option_id']))
            conn.commit()
            return jsonify({"success": True})
    finally:
        conn.close()

@app.route("/api/dismissal-options/<int:option_id>", methods=["DELETE"])
@people_required
def delete_dismissal_option(option_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dismissal_options WHERE option_id=%s", (option_id,))
            conn.commit()
            return jsonify({"success": True})
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

            from datetime import date as dt_date
            day_col_map = {"Monday":"dismissal_mon","Tuesday":"dismissal_tue",
                           "Wednesday":"dismissal_wed","Thursday":"dismissal_thu","Friday":"dismissal_fri"}
            day_name = dt_date.fromisoformat(date_param).strftime("%A")
            col = day_col_map.get(day_name,"dismissal_mon")

            # Single query: always LEFT JOIN daily_dismissal.
            # If a student has a daily record, use it. Otherwise fall back to student defaults.
            grade_clause = "AND s.grade=%s" if grade_filter else ""
            # Params: 1) daily_dismissal date, 2) attendance date, 3) optional grade
            query_params = [date_param, date_param] + ([grade_filter] if grade_filter else [])
            cur.execute(f"""
                SELECT s.student_id AS id, s.first_name AS "firstName",
                       s.last_name AS "lastName", s.grade,
                       COALESCE(d.dismissal_type, s.{col}) AS dismissal,
                       d.destination AS activity,
                       CASE WHEN d.dismissal_id IS NOT NULL THEN TRUE ELSE FALSE END AS confirmed,
                       'homeroom' AS "endsIn", NULL AS elective, d.notes,
                       att.att_status AS "attStatus",
                       COALESCE(st.first_name || ' ' || st.last_name, '') AS "homeroomTeacher",
                       COALESCE(adv.first_name || ' ' || adv.last_name, '') AS "advisoryTeacher",
                       el.name AS "currentElective"
                FROM students s
                LEFT JOIN daily_dismissal d ON d.student_id=s.student_id AND d.dismissal_date=%s
                LEFT JOIN staff st  ON st.staff_id  = s.homeroom_teacher_id
                LEFT JOIN staff adv ON adv.staff_id = s.advisory_teacher_id
                LEFT JOIN student_electives se ON se.student_id = s.student_id AND se.trimester = 3
                LEFT JOIN electives el ON el.elective_id = se.elective_id
                {att_join}
                WHERE s.status='active' {grade_clause}
                ORDER BY s.last_name, s.first_name
            """, query_params)
            rows = fa(cur)
    finally:
        conn.close()

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

# ============================================
# HOMEROOM ATTENDANCE API
# ============================================

@app.route("/api/homeroom-attendance/teachers")
@login_required
def get_homeroom_teachers():
    """Return staff who have at least one active student assigned as homeroom."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT st.staff_id, st.first_name, st.last_name, st.email
                FROM staff st
                JOIN students s ON s.homeroom_teacher_id = st.staff_id
                WHERE s.status = 'active'
                ORDER BY st.last_name, st.first_name
            """)
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/homeroom-attendance/students")
@login_required
def get_homeroom_students():
    teacher_id = request.args.get("teacher_id")
    if not teacher_id:
        return jsonify({"error": "teacher_id required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT student_id, first_name, last_name, grade
                FROM students
                WHERE homeroom_teacher_id = %s AND status = 'active'
                ORDER BY last_name, first_name
            """, (teacher_id,))
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/homeroom-attendance/<date>")
@login_required
def get_homeroom_attendance(date):
    teacher_id = request.args.get("teacher_id")
    if not teacher_id:
        return jsonify({"error": "teacher_id required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT program_id FROM programs WHERE program_name='General Attendance' AND status='active' LIMIT 1")
            program = fo(cur)
            if not program:
                return jsonify([])
            cur.execute("""
                SELECT s.student_id, a.status, a.notes
                FROM attendance_records a
                JOIN enrollments e ON a.enrollment_id = e.enrollment_id
                JOIN students s ON e.student_id = s.student_id
                WHERE e.program_id = %s AND a.attendance_date = %s
                  AND s.homeroom_teacher_id = %s AND s.status = 'active'
            """, (program["program_id"], date, teacher_id))
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/homeroom-attendance", methods=["POST"])
@login_required
def save_homeroom_attendance():
    data = request.json
    date = data.get("date")
    staff_id = data.get("staff_id")
    attendance_data = data.get("attendance", {})
    if not date or not staff_id or not attendance_data:
        return jsonify({"error": "Missing required fields"}), 400
    conn = get_db_connection()
    saved_count = 0
    errors = []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT program_id FROM programs WHERE program_name='General Attendance' AND status='active' LIMIT 1")
            program = fo(cur)
            if not program:
                return jsonify({"error": "General Attendance program not found"}), 404
            program_id = program["program_id"]
            for student_id, record in attendance_data.items():
                status = record.get("status", "")
                note = record.get("note", "")
                if not status:
                    continue
                cur.execute("SELECT enrollment_id FROM enrollments WHERE student_id=%s AND program_id=%s AND status='active'", (student_id, program_id))
                enrollment = fo(cur)
                if not enrollment:
                    errors.append(f"No enrollment for student {student_id}")
                    continue
                cur.execute("""
                    INSERT INTO attendance_records (enrollment_id, attendance_date, status, recorded_by, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (enrollment_id, attendance_date) DO UPDATE SET
                        status = EXCLUDED.status, notes = EXCLUDED.notes,
                        recorded_by = EXCLUDED.recorded_by, recorded_at = CURRENT_TIMESTAMP
                """, (enrollment["enrollment_id"], date, status, staff_id, note))
                saved_count += 1
        conn.commit()
        return jsonify({"success": True, "saved_count": saved_count, "errors": errors})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# ============================================
# TEACHER DASHBOARD ("My Classroom")
# ============================================

def _staff_row_for_email(cur, email):
    cur.execute("SELECT staff_id, first_name, last_name FROM staff WHERE email=%s AND status='active'", (email,))
    return fo(cur)


def _staff_is_teacher(email):
    """True if this staff member has any assigned section this year or a homeroom."""
    if not email:
        return False
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = _staff_row_for_email(cur, email)
            if not row:
                return False
            sid = row["staff_id"]
            year = current_school_year_start()
            cur.execute("SELECT 1 FROM sections WHERE teacher_id=%s AND school_year_start=%s AND active=TRUE LIMIT 1", (sid, year))
            if cur.fetchone():
                return True
            cur.execute("SELECT 1 FROM students WHERE homeroom_teacher_id=%s AND status='active' LIMIT 1", (sid,))
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        conn.close()


@app.route("/my-classroom")
@login_required
def my_classroom_page():
    return send_from_directory(".", "teacher_dashboard.html")


@app.route("/portal")
@login_required
def staff_portal_page():
    # Always serves the portal home, even for teachers (who are redirected off "/").
    return send_from_directory(".", "home.html")


@app.route("/api/my-classroom")
@login_required
def api_my_classroom():
    email = session.get("user_email")
    today = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            staff = _staff_row_for_email(cur, email)
            if not staff:
                return jsonify({"error": "No staff record found for your account."}), 404
            sid = staff["staff_id"]
            year = current_school_year_start()

            cur.execute("SELECT program_id FROM programs WHERE program_name='General Attendance' AND status='active' LIMIT 1")
            prog = fo(cur)
            prog_id = prog["program_id"] if prog else None

            from datetime import date as _d
            day_col_map = {"Monday": "dismissal_mon", "Tuesday": "dismissal_tue",
                           "Wednesday": "dismissal_wed", "Thursday": "dismissal_thu", "Friday": "dismissal_fri"}
            try:
                day_name = _d.fromisoformat(today).strftime("%A")
            except Exception:
                day_name = "Monday"
            dcol = day_col_map.get(day_name, "dismissal_mon")

            # Homeroom roster — keyed by homeroom_teacher_id so it matches the attendance system.
            cur.execute(f"""
                SELECT s.student_id, s.first_name, s.last_name, s.grade,
                       s.allergies, s.medical_alert, s.medications, s.dietary,
                       s.emergency_contact_name, s.emergency_contact_phone,
                       att.status AS att_status,
                       COALESCE(d.dismissal_type, s.{dcol}) AS dismissal,
                       d.destination AS dismissal_dest,
                       (SELECT COUNT(*) FROM student_notes n WHERE n.student_id = s.student_id) AS note_count
                FROM students s
                LEFT JOIN daily_dismissal d ON d.student_id = s.student_id AND d.dismissal_date = %s
                LEFT JOIN (
                    SELECT e.student_id, a.status
                    FROM attendance_records a
                    JOIN enrollments e ON a.enrollment_id = e.enrollment_id
                    WHERE e.program_id = %s AND a.attendance_date = %s
                ) att ON att.student_id = s.student_id
                WHERE s.homeroom_teacher_id = %s AND s.status = 'active'
                ORDER BY s.last_name, s.first_name
            """, (today, prog_id, today, sid))
            hr_rows = fa(cur)

            homeroom_students = []
            attendance_taken = False
            for r in hr_rows:
                if r.get("att_status"):
                    attendance_taken = True
                homeroom_students.append({
                    "student_id": r["student_id"],
                    "name": f'{r["first_name"]} {r["last_name"]}',
                    "grade": r.get("grade") or "",
                    "allergies": r.get("allergies") or "",
                    "medical_alert": r.get("medical_alert") or "",
                    "medications": r.get("medications") or "",
                    "dietary": r.get("dietary") or "",
                    "emergency_contact_name": r.get("emergency_contact_name") or "",
                    "emergency_contact_phone": r.get("emergency_contact_phone") or "",
                    "att_status": r.get("att_status") or "",
                    "dismissal": r.get("dismissal") or "",
                    "dismissal_dest": r.get("dismissal_dest") or "",
                    "note_count": r.get("note_count") or 0,
                })

            # Homeroom section meta (name / room), if one is defined in sections.
            cur.execute("""
                SELECT sec.name, r.name AS room
                FROM sections sec LEFT JOIN rooms r ON r.room_id = sec.room_id
                WHERE sec.teacher_id=%s AND sec.school_year_start=%s AND sec.type='homeroom' AND sec.active=TRUE
                LIMIT 1
            """, (sid, year))
            hm = fo(cur) or {}

            # Non-homeroom sections this teacher is assigned to.
            cur.execute("""
                SELECT sec.section_id, sec.name, sec.subject, sec.grade, sec.term, sec.type, r.name AS room
                FROM sections sec LEFT JOIN rooms r ON r.room_id = sec.room_id
                WHERE sec.teacher_id=%s AND sec.school_year_start=%s AND sec.active=TRUE AND sec.type <> 'homeroom'
                ORDER BY sec.type, sec.name
            """, (sid, year))
            sec_rows = fa(cur)
            sections = []
            for sec in sec_rows:
                cur.execute("""
                    SELECT s.student_id, s.first_name, s.last_name, s.grade, s.allergies, s.medical_alert
                    FROM section_enrollments se JOIN students s ON s.student_id = se.student_id
                    WHERE se.section_id=%s AND s.status='active'
                    ORDER BY s.last_name, s.first_name
                """, (sec["section_id"],))
                roster = [{
                    "student_id": x["student_id"],
                    "name": f'{x["first_name"]} {x["last_name"]}',
                    "grade": x.get("grade") or "",
                    "allergies": x.get("allergies") or "",
                    "medical_alert": x.get("medical_alert") or "",
                } for x in fa(cur)]
                sections.append({
                    "section_id": sec["section_id"],
                    "name": sec["name"],
                    "subject": sec.get("subject") or "",
                    "grade": sec.get("grade") or "",
                    "term": sec.get("term") or "",
                    "type": sec.get("type") or "subject",
                    "room": sec.get("room") or "",
                    "students": roster,
                })

            return jsonify({
                "teacher": {"staff_id": sid, "name": f'{staff["first_name"]} {staff["last_name"]}'},
                "date": today,
                "day_name": day_name,
                "school_year": sy_long(year),
                "has_homeroom": len(homeroom_students) > 0,
                "homeroom": {
                    "name": hm.get("name") or "Homeroom",
                    "room": hm.get("room") or "",
                    "attendance_taken": attendance_taken,
                    "students": homeroom_students,
                },
                "sections": sections,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/student-notes")
@login_required
def api_get_student_notes():
    student_id = request.args.get("student_id")
    if not student_id:
        return jsonify({"error": "student_id required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT note_id, author_name, body, created_at
                FROM student_notes WHERE student_id=%s
                ORDER BY created_at DESC
            """, (student_id,))
            return jsonify(fa(cur))
    finally:
        conn.close()


@app.route("/api/student-notes", methods=["POST"])
@login_required
def api_add_student_note():
    data = request.json or {}
    student_id = data.get("student_id")
    body = (data.get("body") or "").strip()
    if not student_id or not body:
        return jsonify({"error": "student_id and body are required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            staff = _staff_row_for_email(cur, session.get("user_email"))
            author_id = staff["staff_id"] if staff else None
            author_name = f'{staff["first_name"]} {staff["last_name"]}' if staff else (session.get("user_name") or "")
            cur.execute("""
                INSERT INTO student_notes (student_id, author_staff_id, author_name, body)
                VALUES (%s, %s, %s, %s)
                RETURNING note_id, author_name, body, created_at
            """, (student_id, author_id, author_name, body))
            row = fo(cur)
            conn.commit()
            return jsonify({"success": True, "note": row}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# HOMEROOM ATTENDANCE REPORT (Trimester tally)
# ============================================

# Fallback trimester windows (the original 2025–26 dates). Used ONLY when a
# school year has no trimester dates set on the calendar page. Going forward,
# trimester dates come from the school_years table via get_trimester_windows().
T1_START_DATE = "2025-09-02"
T1_END_DATE   = "2025-11-21"
T2_START_DATE = "2025-11-22"
T2_END_DATE   = "2026-02-22"
T3_START_DATE = "2026-02-23"
T3_END_DATE   = "2026-06-12"


def _fallback_trimester_windows(start_year):
    """The hard-coded 2025–26 windows, shifted to the requested school year."""
    shift = start_year - 2025
    def sh(iso):
        y, md = iso.split("-", 1)
        return f"{int(y) + shift}-{md}"
    return [
        ("t1", "Trimester 1", sh(T1_START_DATE), sh(T1_END_DATE)),
        ("t2", "Trimester 2", sh(T2_START_DATE), sh(T2_END_DATE)),
        ("t3", "Trimester 3", sh(T3_START_DATE), sh(T3_END_DATE)),
    ]


def get_trimester_windows(start_year=None):
    """
    Return [(key, label, start_iso, end_iso), ...] for a school year.
    Reads the school_years table; any window not set falls back to the
    hard-coded defaults shifted to that year. Defaults to the current year.
    """
    if start_year is None:
        start_year = current_school_year_start()
    fb = {k: (s, e) for k, _l, s, e in _fallback_trimester_windows(start_year)}
    row = {}
    try:
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT t1_start, t1_end, t2_start, t2_end, t3_start, t3_end
                    FROM   school_years
                    WHERE  start_year = %s
                """, (start_year,))
                row = fo(cur) or {}
        finally:
            conn.close()
    except Exception:
        row = {}
    labels = {"t1": "Trimester 1", "t2": "Trimester 2", "t3": "Trimester 3"}
    cols   = {"t1": ("t1_start", "t1_end"), "t2": ("t2_start", "t2_end"), "t3": ("t3_start", "t3_end")}
    out = []
    for k in ("t1", "t2", "t3"):
        sc, ec = cols[k]
        s, e = row.get(sc), row.get(ec)
        s = s.isoformat() if s else fb[k][0]
        e = e.isoformat() if e else fb[k][1]
        out.append((k, labels[k], s, e))
    return out


def _trimester_unexcused_counts(cur, program_id, teacher_id, start_date, end_date):
    """
    Return per-student unexcused counts for one trimester window:
      {student_id: {"absent": n, "tardy": n, "ed": n}, ...}

    Per Karin: excused absences (status='excused') and excused tardies
    (status='excused_tardy') are NOT counted in any column. Only the three
    unexcused statuses below are surfaced in the report.
    """
    cur.execute("""
        SELECT s.student_id, a.status, COUNT(*) AS n
        FROM attendance_records a
        JOIN enrollments e ON a.enrollment_id = e.enrollment_id
        JOIN students s ON e.student_id = s.student_id
        WHERE e.program_id = %s
          AND s.homeroom_teacher_id = %s
          AND a.attendance_date BETWEEN %s AND %s
          AND a.status IN ('absent', 'tardy', 'ed')
        GROUP BY s.student_id, a.status
    """, (program_id, teacher_id, start_date, end_date))
    out = {}
    for r in fa(cur):
        out.setdefault(r["student_id"], {"absent": 0, "tardy": 0, "ed": 0})[r["status"]] = r["n"]
    return out


def _build_homeroom_report(teacher_id, start_year=None):
    """
    Whole-year homeroom attendance report. Returns one row per active student
    in the homeroom with unexcused absent/tardy/ED counts for each trimester
    laid out side-by-side. Trimester dates come from the calendar settings
    (school_years) for the given year, defaulting to the current school year.

    One parameterized counts query, invoked once per trimester window. Results
    are merged in Python by student_id.
    """
    windows = get_trimester_windows(start_year)
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT program_id FROM programs WHERE program_name='General Attendance' AND status='active' LIMIT 1")
            program = fo(cur)
            if not program:
                return {"error": "General Attendance program not found"}, 404
            program_id = program["program_id"]

            cur.execute("""
                SELECT student_id, first_name, last_name
                FROM students
                WHERE homeroom_teacher_id = %s AND status = 'active'
                ORDER BY last_name, first_name
            """, (teacher_id,))
            roster = fa(cur)

            # One counts query per trimester; merge by student_id
            counts_by_trimester = {
                key: _trimester_unexcused_counts(cur, program_id, teacher_id, start, end)
                for key, _label, start, end in windows
            }

            results = []
            for s in roster:
                sid = s["student_id"]
                row = {
                    "student_id": sid,
                    "first_name": s["first_name"],
                    "last_name":  s["last_name"],
                }
                for key, _label, _start, _end in windows:
                    c = counts_by_trimester[key].get(sid, {})
                    row[f"{key}_absent"] = c.get("absent", 0)
                    row[f"{key}_tardy"]  = c.get("tardy",  0)
                    row[f"{key}_ed"]     = c.get("ed",     0)
                results.append(row)

            return {
                "trimesters": [
                    {"key": key, "label": label, "start_date": start, "end_date": end}
                    for key, label, start, end in windows
                ],
                "students": results,
            }, 200
    finally:
        conn.close()

@app.route("/api/homeroom-attendance-report")
@login_required
def get_homeroom_attendance_report():
    teacher_id = request.args.get("teacher_id")
    if not teacher_id:
        return jsonify({"error": "teacher_id required"}), 400
    sy = request.args.get("school_year")
    start_year = int(sy) if (sy or "").isdigit() else None
    data, code = _build_homeroom_report(teacher_id, start_year)
    return jsonify(data), code

@app.route("/api/homeroom-attendance-report.csv")
@login_required
def get_homeroom_attendance_report_csv():
    import csv, io
    from flask import Response
    teacher_id = request.args.get("teacher_id")
    if not teacher_id:
        return ("teacher_id required", 400)

    sy = request.args.get("school_year")
    start_year = int(sy) if (sy or "").isdigit() else None
    data, code = _build_homeroom_report(teacher_id, start_year)
    if code != 200:
        return (data.get("error", "error"), code)

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT first_name, last_name FROM staff WHERE staff_id = %s", (teacher_id,))
            t = fo(cur) or {}
    finally:
        conn.close()

    teacher_slug = f"{t.get('last_name','teacher')}_{t.get('first_name','')}".replace(" ", "")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Homeroom Attendance Report — Full Year (Trimesters 1, 2, and 3)"])
    w.writerow([f"Teacher: {t.get('first_name','')} {t.get('last_name','')}"])
    for tri in data["trimesters"]:
        w.writerow([f"{tri['label']}: {tri['start_date']} to {tri['end_date']}"])
    w.writerow(["Counts shown are unexcused only (excused absences and excused tardies are excluded)."])
    w.writerow([])
    w.writerow(["Last Name", "First Name",
                "T1 Absent", "T1 Tardy", "T1 ED",
                "T2 Absent", "T2 Tardy", "T2 ED",
                "T3 Absent", "T3 Tardy", "T3 ED"])
    for s in data["students"]:
        w.writerow([s["last_name"], s["first_name"],
                    s["t1_absent"], s["t1_tardy"], s["t1_ed"],
                    s["t2_absent"], s["t2_tardy"], s["t2_ed"],
                    s["t3_absent"], s["t3_tardy"], s["t3_ed"]])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=attendance_report_{teacher_slug}_full_year.csv"}
    )

@app.route("/api/homeroom-attendance-report/student/<int:student_id>")
@login_required
def get_student_attendance_detail(student_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT program_id FROM programs WHERE program_name='General Attendance' AND status='active' LIMIT 1")
            program = fo(cur)
            if not program:
                return jsonify({"error": "General Attendance program not found"}), 404
            program_id = program["program_id"]

            cur.execute("""
                SELECT s.student_id, s.first_name, s.last_name, s.grade, s.homeroom_teacher_id
                FROM students s
                WHERE s.student_id = %s
            """, (student_id,))
            student = fo(cur)
            if not student:
                return jsonify({"error": "Student not found"}), 404

            # Scoped to the T3 window of the requested school year (default current), from calendar settings.
            _sy = request.args.get("school_year")
            _sy = int(_sy) if (_sy or "").isdigit() else None
            _win = {k: (s, e) for k, _l, s, e in get_trimester_windows(_sy)}
            detail_start, detail_end = _win["t3"]

            cur.execute("""
                SELECT a.attendance_date, a.status, a.notes
                FROM attendance_records a
                JOIN enrollments e ON a.enrollment_id = e.enrollment_id
                WHERE e.student_id = %s
                  AND e.program_id = %s
                  AND a.attendance_date BETWEEN %s AND %s
                ORDER BY a.attendance_date
            """, (student_id, program_id, detail_start, detail_end))
            records = [
                {"date": str(r["attendance_date"]), "status": r["status"], "notes": r["notes"] or ""}
                for r in fa(cur)
            ]

            cur.execute("""
                SELECT DISTINCT a.attendance_date
                FROM attendance_records a
                JOIN enrollments e ON a.enrollment_id = e.enrollment_id
                JOIN students s ON e.student_id = s.student_id
                WHERE s.homeroom_teacher_id = %s
                  AND e.program_id = %s
                  AND a.attendance_date BETWEEN %s AND %s
            """, (student["homeroom_teacher_id"], program_id, detail_start, detail_end))
            school_dates = sorted([str(r["attendance_date"]) for r in fa(cur)])

            return jsonify({
                "student": {
                    "student_id": student["student_id"],
                    "first_name": student["first_name"],
                    "last_name":  student["last_name"],
                    "grade":      student["grade"],
                },
                "start_date":   detail_start,
                "end_date":     detail_end,
                "records":      records,
                "school_dates": school_dates,
            })
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
    d_type      = data.get("dismissal_type", "") or ""
    destination = data.get("destination","")
    notes       = data.get("notes","")
    is_override = data.get("is_override",0)
    if not all([student_id, date]):
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
                    SELECT s.student_id,s.first_name,s.last_name,s.grade,d.destination AS bus_route,
                           st.first_name AS hr_first, st.last_name AS hr_last
                    FROM students s JOIN daily_dismissal d ON d.student_id=s.student_id AND d.dismissal_date=%s
                    LEFT JOIN staff st ON st.staff_id = s.homeroom_teacher_id
                    WHERE s.status='active' AND d.dismissal_type='bus'
                      AND d.destination IS NOT NULL AND d.destination!=''
                    ORDER BY d.destination, s.last_name, s.first_name
                """, [date_param])
            else:
                from datetime import date as dt_date
                day_col_map = {"Monday":"dismissal_mon","Tuesday":"dismissal_tue",
                               "Wednesday":"dismissal_wed","Thursday":"dismissal_thu","Friday":"dismissal_fri"}
                col = day_col_map.get(dt_date.fromisoformat(date_param).strftime("%A"),"dismissal_mon")
                cur.execute(f"""SELECT s.student_id,s.first_name,s.last_name,s.grade,s.{col} AS bus_route,
                           st.first_name AS hr_first, st.last_name AS hr_last
                    FROM students s LEFT JOIN staff st ON st.staff_id = s.homeroom_teacher_id
                    WHERE s.status='active' AND s.{col}='bus' ORDER BY s.last_name,s.first_name""")
            rows = fa(cur)
    finally:
        conn.close()

    # Fallback map for students without an assigned homeroom teacher
    homeroom_map = {"JPK":"Wipperman","SPK":"Vorolieff","K":"Olsen","1":"Alfonso",
                    "2":"Szeghy","3":"Vales","4":"Oxer / Donnelly","5":"Tucci",
                    "6":"Poon","7":"Ballard","8":"Duthie","--":"—"}
    grouped = {}
    for r in rows:
        route = r["bus_route"] if source=="today" else "Default Bus"
        if route not in grouped: grouped[route] = []
        # Use assigned teacher if set, otherwise fall back to grade map
        if r.get("hr_last"):
            teacher = r["hr_last"]
        else:
            teacher = homeroom_map.get(r["grade"],"—")
        grouped[route].append({"student_id":r["student_id"],"first_name":r["first_name"],
            "last_name":r["last_name"],"grade":r["grade"],"bus_route":route,
            "homeroom_teacher":teacher})
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
                SELECT s.student_id,s.first_name,s.last_name,s.grade,s.status,
                       s.date_of_birth,s.email,s.phone,s.address,
                       s.emergency_contact_name,s.emergency_contact_phone,
                       s.enrollment_date,s.notes,s.before_care,
                       s.dismissal_mon,s.dismissal_tue,s.dismissal_wed,s.dismissal_thu,s.dismissal_fri,
                       s.homeroom_teacher_id,
                       s.advisory_teacher_id,
                       adv.first_name || ' ' || adv.last_name AS advisory_teacher_name,
                       se.elective_id AS current_elective_id,
                       e.name AS current_elective_name
                FROM students s
                LEFT JOIN staff adv ON s.advisory_teacher_id = adv.staff_id
                LEFT JOIN student_electives se ON s.student_id = se.student_id AND se.trimester = 3
                LEFT JOIN electives e ON se.elective_id = e.elective_id
                ORDER BY s.last_name, s.first_name
            """)
            return jsonify(fa(cur))
    finally:
        conn.close()

@app.route("/api/students", methods=["POST"])
@people_required
def create_student():
    data = request.json
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            hr_id = data.get("homeroom_teacher_id")
            hr_id = int(hr_id) if hr_id else None
            cur.execute("""
                INSERT INTO students (first_name, last_name, grade, status,
                    date_of_birth, email, phone, address,
                    emergency_contact_name, emergency_contact_phone,
                    enrollment_date, notes, before_care,
                    dismissal_mon, dismissal_tue, dismissal_wed,
                    dismissal_thu, dismissal_fri, homeroom_teacher_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING student_id
            """, (data.get("first_name"),data.get("last_name"),data.get("grade"),
                  data.get("status","active"),
                  data.get("date_of_birth"),data.get("email"),data.get("phone"),data.get("address"),
                  data.get("emergency_contact_name"),data.get("emergency_contact_phone"),
                  data.get("enrollment_date"),data.get("notes"),
                  1 if data.get("before_care") else 0,
                  data.get("dismissal_mon"),data.get("dismissal_tue"),data.get("dismissal_wed"),
                  data.get("dismissal_thu"),data.get("dismissal_fri"),hr_id))
            new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success":True,"student_id":new_id}),201
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/students/<int:student_id>", methods=["PUT"])
@people_required
def update_student(student_id):
    data = request.json
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            hr_id = data.get("homeroom_teacher_id")
            hr_id = int(hr_id) if hr_id else None
            adv_id = data.get("advisory_teacher_id")
            adv_id = int(adv_id) if adv_id else None
            cur.execute("""
                UPDATE students SET
                    first_name=%s, last_name=%s, grade=%s, status=%s,
                    date_of_birth=%s, email=%s, phone=%s, address=%s,
                    emergency_contact_name=%s, emergency_contact_phone=%s,
                    enrollment_date=%s, notes=%s, before_care=%s,
                    dismissal_mon=%s, dismissal_tue=%s, dismissal_wed=%s,
                    dismissal_thu=%s, dismissal_fri=%s,
                    homeroom_teacher_id=%s, advisory_teacher_id=%s,
                    updated_at=CURRENT_TIMESTAMP
                WHERE student_id=%s
            """, (data.get("first_name"),data.get("last_name"),data.get("grade"),data.get("status"),
                  data.get("date_of_birth"),data.get("email"),data.get("phone"),data.get("address"),
                  data.get("emergency_contact_name"),data.get("emergency_contact_phone"),
                  data.get("enrollment_date"),data.get("notes"),
                  1 if data.get("before_care") else 0,
                  data.get("dismissal_mon"),data.get("dismissal_tue"),data.get("dismissal_wed"),
                  data.get("dismissal_thu"),data.get("dismissal_fri"),hr_id,adv_id,student_id))
            # Update student_electives if elective_id provided
            elective_id = data.get("elective_id")
            if elective_id is not None:
                if elective_id:
                    cur.execute("""
                        INSERT INTO student_electives (student_id, elective_id, trimester)
                        VALUES (%s, %s, 3)
                        ON CONFLICT (student_id, trimester) DO UPDATE SET elective_id=EXCLUDED.elective_id
                    """, (student_id, int(elective_id)))
                else:
                    cur.execute("DELETE FROM student_electives WHERE student_id=%s AND trimester=3", (student_id,))
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
@require_perm("staff_directory")
def get_people_staff():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT staff_id,first_name,last_name,email,role,status,can_record_attendance,can_manage_billing,can_manage_people,can_manage_permissions,permissions,title,is_special_services FROM staff ORDER BY last_name,first_name")
            rows = fa(cur)
        # Normalize permissions to a resolved key list for the frontend, so the
        # editor shows the right boxes even for legacy rows.
        for r in rows:
            r["permissions"] = permissions_for_staff(r)
            r["can_manage_permissions"] = bool(r.get("can_manage_permissions"))
            r["is_special_services"] = bool(r.get("is_special_services"))
        return jsonify(rows)
    finally:
        conn.close()


def _editor_perm_context():
    """Return (is_super, can_manage_perms, editor_email) for the current session."""
    is_super = bool(session.get("is_superadmin"))
    return (is_super,
            is_super or bool(session.get("can_manage_permissions")),
            (session.get("user_email") or "").lower())


@app.route("/api/people/staff", methods=["POST"])
@require_perm("staff_directory")
def add_people_staff():
    data = request.get_json() or {}
    is_super, can_manage_perms, _ = _editor_perm_context()
    # Permissions on a new staff member may only be set by someone who can manage
    # permissions. Everyone else creates the person with a safe default (the
    # everyday daily-input pages), then a permissions manager grants the rest.
    if can_manage_perms and isinstance(data.get("permissions"), list):
        keys = sorted(set(k for k in data["permissions"] if k in ALL_PERMISSION_KEYS))
    else:
        keys = [k for k in SILO_KEYS["daily_input"] if k != "dismissal_options"]
    flags = legacy_flags_from_keys(keys)
    cmp_flag = 1 if (can_manage_perms and data.get("can_manage_permissions")) else 0
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO staff (first_name,last_name,email,role,title,status,
                                   can_record_attendance,can_manage_billing,can_manage_people,
                                   can_manage_permissions,permissions,hire_date,is_special_services)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (data.get("first_name"),data.get("last_name"),data.get("email"),
                  data.get("role","staff"),data.get("title",""),data.get("status","active"),
                  flags["can_record_attendance"],flags["can_manage_billing"],flags["can_manage_people"],
                  cmp_flag,json.dumps(keys),"2025-09-01",
                  1 if data.get("is_special_services") else 0))
        conn.commit()
        return jsonify({"success":True}),201
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}),500
    finally:
        conn.close()

@app.route("/api/people/staff/<int:staff_id>", methods=["PUT"])
@require_perm("staff_directory")
def update_people_staff(staff_id):
    data = request.get_json() or {}
    is_super, can_manage_perms, editor_email = _editor_perm_context()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT email FROM staff WHERE staff_id=%s", (staff_id,))
            target = fo(cur)
        if not target:
            return jsonify({"error":"Staff member not found"}),404
        target_email = (target.get("email") or "").lower()

        # Profile fields: editable by anyone with staff-directory access.
        profile_fields = ["first_name","last_name","email","role","title","status"]
        fields = [f + " = %s" for f in profile_fields if f in data]
        values = [data[f] for f in profile_fields if f in data]
        # The Special Services Role tick box is an ordinary profile attribute: it only
        # decides whether the person appears in the special-services staff dropdown.
        if "is_special_services" in data:
            fields.append("is_special_services = %s")
            values.append(1 if data["is_special_services"] else 0)

        # Permission changes are privileged and never allowed on your own account
        # or on the superadmin. This is the server-side guard that stops someone
        # with staff access from granting themselves (or anyone) new powers.
        wants_perm_change = ("permissions" in data) or ("can_manage_permissions" in data)
        if wants_perm_change:
            if not can_manage_perms:
                return jsonify({"error":"You don't have permission to change access permissions."}),403
            if target_email and target_email == editor_email:
                return jsonify({"error":"You can't change your own permissions."}),403
            if target_email == SUPERADMIN_EMAIL:
                return jsonify({"error":"The superadmin's permissions can't be modified."}),403
            if "permissions" in data:
                keys = data["permissions"] if isinstance(data["permissions"], list) else []
                keys = sorted(set(k for k in keys if k in ALL_PERMISSION_KEYS))
                flags = legacy_flags_from_keys(keys)
                fields += ["permissions = %s","can_record_attendance = %s",
                           "can_manage_billing = %s","can_manage_people = %s"]
                values += [json.dumps(keys),flags["can_record_attendance"],
                           flags["can_manage_billing"],flags["can_manage_people"]]
            if "can_manage_permissions" in data:
                # Any permission-manager may grant or revoke effective-superadmin
                # on other people (self-edits and the superadmin row are already
                # blocked above).
                fields.append("can_manage_permissions = %s")
                values.append(1 if data["can_manage_permissions"] else 0)

        if not fields:
            return jsonify({"error":"No changes"}),400
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
@require_perm("staff_directory")
def delete_people_staff(staff_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT email FROM staff WHERE staff_id=%s",(staff_id,))
            target = fo(cur)
        # Never allow deleting the superadmin row.
        if target and (target.get("email") or "").lower() == SUPERADMIN_EMAIL:
            return jsonify({"error":"The superadmin account can't be deleted."}),403
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
                           pa.session_date, pa.units, pa.teacher, pa.duration_minutes,
                           pa.recorded_by, pa.recorded_at,
                           s.first_name, s.last_name, s.grade
                    FROM program_attendance pa
                    JOIN students s ON pa.student_id=s.student_id
                    WHERE pa.program_type=%s AND pa.session_date=%s
                    ORDER BY s.last_name, s.first_name
                """, (program_type, date))
            elif start_date and end_date:
                cur.execute("""
                    SELECT pa.record_id, pa.student_id, pa.program_type,
                           pa.session_date, pa.units, pa.teacher, pa.duration_minutes,
                           pa.recorded_by, pa.recorded_at,
                           s.first_name, s.last_name, s.grade
                    FROM program_attendance pa
                    JOIN students s ON pa.student_id=s.student_id
                    WHERE pa.program_type=%s AND pa.session_date BETWEEN %s AND %s
                    ORDER BY pa.session_date DESC, s.last_name, s.first_name
                """, (program_type, start_date, end_date))
            else:
                cur.execute("""
                    SELECT pa.record_id, pa.student_id, pa.program_type,
                           pa.session_date, pa.units, pa.teacher, pa.duration_minutes,
                           pa.recorded_by, pa.recorded_at,
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
    duration_minutes = data.get("duration_minutes", 60)
    if duration_minutes not in (30, 60):
        duration_minutes = 60
    recorded_by  = session.get("user_name", "")
    if not all([student_id, program_type, session_date]):
        return jsonify({"error":"Missing required fields"}),400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO program_attendance (student_id, program_type, session_date, units, teacher, duration_minutes, recorded_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (student_id, program_type, session_date) DO UPDATE SET
                    units=EXCLUDED.units, teacher=EXCLUDED.teacher,
                    duration_minutes=EXCLUDED.duration_minutes,
                    recorded_by=EXCLUDED.recorded_by, recorded_at=CURRENT_TIMESTAMP
                RETURNING record_id
            """, (student_id, program_type, session_date, units, teacher, duration_minutes, recorded_by))
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

@app.route("/api/program-attendance/my-monthly-overview")
@login_required
def get_my_program_monthly_overview():
    """Get the logged-in teacher's sessions for a given month and program type"""
    program_type = request.args.get("program_type")
    month = request.args.get("month")  # format: YYYY-MM
    if not program_type or not month:
        return jsonify({"error": "program_type and month required"}), 400

    user_name = session.get("user_name", "")
    start_date = f"{month}-01"

    import calendar as cal_mod
    year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    last_day = cal_mod.monthrange(year, mon)[1]
    end_date = f"{month}-{last_day:02d}"

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pa.session_date, pa.student_id, pa.units,
                       s.first_name, s.last_name, s.grade
                FROM program_attendance pa
                JOIN students s ON pa.student_id = s.student_id
                WHERE pa.program_type = %s
                  AND pa.session_date BETWEEN %s AND %s
                  AND (pa.teacher = %s OR pa.recorded_by = %s)
                ORDER BY pa.session_date, s.last_name, s.first_name
            """, (program_type, start_date, end_date, user_name, user_name))
            rows = fa(cur)

        from collections import defaultdict
        by_date = defaultdict(list)
        for r in rows:
            by_date[r["session_date"]].append({
                "name": f"{r['first_name']} {r['last_name']}",
                "grade": r["grade"],
                "units": float(r["units"])
            })

        result = []
        for d, students in sorted(by_date.items()):
            result.append({
                "date": d,
                "count": len(students),
                "students": students
            })

        return jsonify({"month": month, "program_type": program_type, "user_name": user_name, "days": result})
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
# LUNCH BILLING
# ============================================

LUNCH_EC_GRADES = {"JPK", "SPK", "K"}
LUNCH_STATUSES = {"home", "monthly", "fullYearPaid"}


# ============================================
# SCHOOL YEAR — single source of truth for "what year is it"
# Rollover date is configurable on the calendar page (default July 1).
# ============================================
def get_app_setting(key, default=None):
    """Read a value from app_settings; returns default on any error (e.g. pre-migration)."""
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key=%s", (key,))
                row = cur.fetchone()
                return row[0] if row else default
        finally:
            conn.close()
    except Exception:
        return default


def _rollover_md():
    """(month, day) the school year ticks over. Default July 1."""
    raw = get_app_setting("school_year_rollover", "07-01") or "07-01"
    try:
        mm, dd = raw.split("-")
        mm, dd = int(mm), int(dd)
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return mm, dd
    except Exception:
        pass
    return 7, 1


def current_school_year_start(today=None):
    """Integer start-year of the current school year, per the configured rollover date."""
    from datetime import date as _d
    t = today or _d.today()
    rm, rd = _rollover_md()
    return t.year if (t.month, t.day) >= (rm, rd) else t.year - 1


def sy_long(start_year):
    """e.g. 2026 -> '2026-2027' (lunch/enrollment convention)."""
    return f"{start_year}-{start_year + 1}"


def sy_short(start_year):
    """e.g. 2026 -> '2026-27' (financial-aid/tuition convention)."""
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def _lunch_default_year():
    return sy_long(current_school_year_start())


def _lunch_school_year_months(school_year):
    """Ordered 'YYYY-MM' keys for Sep..Jun of a '2025-2026' school year."""
    try:
        start = int(str(school_year)[:4])
    except (ValueError, TypeError):
        start = int(_lunch_default_year()[:4])
    seq = [(start, m) for m in (9, 10, 11, 12)] + [(start + 1, m) for m in (1, 2, 3, 4, 5, 6)]
    return [f"{y:04d}-{m:02d}" for y, m in seq]


def _lunch_school_year_for(year, month):
    return f"{year}-{year + 1}" if month >= 9 else f"{year - 1}-{year}"


def _lunch_is_ec(grade):
    return str(grade).upper() in LUNCH_EC_GRADES


def _lunch_clean_cell(cell):
    cell = cell or {}
    status = cell.get("status", "home")
    if status not in LUNCH_STATUSES:
        status = "home"
    try:
        pizza = int(cell.get("pizzaCount", 0) or 0)
    except (ValueError, TypeError):
        pizza = 0
    return {"status": status, "pizzaCount": max(0, pizza)}


def _lunch_rates(cur, as_of_date):
    cur.execute("""
        SELECT DISTINCT ON (rate_key) rate_key, rate_value
        FROM billing_rates
        WHERE rate_key IN ('lunch_rate_ec','lunch_rate_1_8','lunch_fy_ec','lunch_fy_1_8')
          AND effective_from::date <= %s
        ORDER BY rate_key, effective_from::date DESC
    """, (as_of_date,))
    rates = {r["rate_key"]: float(r["rate_value"]) for r in cur.fetchall()}
    for k, v in (('lunch_rate_ec', 4.50), ('lunch_rate_1_8', 5.50),
                 ('lunch_fy_ec', 742.50), ('lunch_fy_1_8', 876.75)):
        rates.setdefault(k, v)
    return rates


def _lunch_day_counts(cur, month_keys):
    """{month_key: {'ec': n, 'g18': n}} of calendar lunch-day tags per month."""
    import calendar as _cal
    out = {mk: {"ec": 0, "g18": 0} for mk in month_keys}
    if not month_keys:
        return out
    first = month_keys[0] + "-01"
    ly, lm = month_keys[-1].split("-")
    last = f"{ly}-{lm}-{_cal.monthrange(int(ly), int(lm))[1]:02d}"
    cur.execute("""
        SELECT category_key, to_char(day_date, 'YYYY-MM') AS mk, COUNT(*) AS n
        FROM calendar_day_tags
        WHERE category_key IN ('lunch_day_prek_k','lunch_day_1_8')
          AND day_date >= %s AND day_date <= %s
        GROUP BY category_key, to_char(day_date, 'YYYY-MM')
    """, (first, last))
    for r in cur.fetchall():
        mk = r["mk"]
        if mk in out:
            if r["category_key"] == "lunch_day_prek_k":
                out[mk]["ec"] = int(r["n"])
            else:
                out[mk]["g18"] = int(r["n"])
    return out


def _lunch_month_charge(grade, status, pizza_count, day_count, rates):
    """Returns (lunch_days, rate, status_charge, pizza_charge) for one student-month."""
    ec = _lunch_is_ec(grade)
    lunch_days = day_count.get("ec", 0) if ec else day_count.get("g18", 0)
    rate = rates["lunch_rate_ec"] if ec else rates["lunch_rate_1_8"]
    status_charge = lunch_days * rate if status == "monthly" else 0.0
    pizza_charge = max(0, int(pizza_count or 0)) * rate
    return lunch_days, rate, round(status_charge, 2), round(pizza_charge, 2)


def _lunch_year_total(grade, months_doc, day_counts_by_month, rates, month_keys):
    """Year total: full-year price (+pizza) if any month is fullYearPaid, else sum of monthly charges."""
    ec = _lunch_is_ec(grade)
    rate = rates["lunch_rate_ec"] if ec else rates["lunch_rate_1_8"]
    fy_price = rates["lunch_fy_ec"] if ec else rates["lunch_fy_1_8"]
    months_doc = months_doc or {}
    any_fy = False
    pizza_total = 0.0
    monthly_total = 0.0
    for mk in month_keys:
        cell = months_doc.get(mk) or {}
        status = cell.get("status", "home")
        dc = day_counts_by_month.get(mk, {"ec": 0, "g18": 0})
        _, _, status_charge, pizza_charge = _lunch_month_charge(grade, status, cell.get("pizzaCount", 0), dc, rates)
        pizza_total += pizza_charge
        if status == "fullYearPaid":
            any_fy = True
        monthly_total += status_charge
    if any_fy:
        return round(fy_price + pizza_total, 2)
    return round(monthly_total + pizza_total, 2)


@app.route("/lunch-dashboard")
@require_perm("lunch_dashboard")
def lunch_dashboard_page():
    return send_from_directory(".", "lunch_dashboard.html")


@app.route("/api/lunch/enrollment")
@login_required
def api_lunch_enrollment():
    school_year = request.args.get("school_year") or _lunch_default_year()
    month_keys = _lunch_school_year_months(school_year)
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            rates = _lunch_rates(cur, month_keys[0] + "-01")
            day_counts = _lunch_day_counts(cur, month_keys)
            cur.execute("""
                SELECT student_id, first_name, last_name, grade
                FROM students WHERE status='active'
                ORDER BY last_name, first_name
            """)
            students = fa(cur)
            cur.execute(
                "SELECT student_id, grade_at_time_of_record, months, notes FROM lunch_enrollment WHERE school_year=%s",
                (school_year,))
            enr = {r["student_id"]: r for r in fa(cur)}
            out = []
            for s in students:
                e = enr.get(s["student_id"])
                months_doc = (e["months"] if e else {}) or {}
                out.append({
                    "student_id": s["student_id"],
                    "first_name": s["first_name"],
                    "last_name": s["last_name"],
                    "grade": str(s["grade"]),
                    "months": months_doc,
                    "notes": (e["notes"] if e else "") or "",
                    "year_total": _lunch_year_total(s["grade"], months_doc, day_counts, rates, month_keys),
                })
            return jsonify({
                "school_year": school_year,
                "month_keys": month_keys,
                "lunch_days": day_counts,
                "rates": rates,
                "students": out,
            })
    finally:
        conn.close()


@app.route("/api/lunch/enrollment", methods=["POST"])
@login_required
def api_lunch_enrollment_save():
    data = request.json or {}
    student_id = data.get("student_id")
    school_year = data.get("school_year")
    grade = data.get("grade", "") or ""
    if not student_id or not school_year:
        return jsonify({"error": "student_id and school_year required"}), 400
    updated_by = session.get("user_name", "")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT months FROM lunch_enrollment WHERE student_id=%s AND school_year=%s",
                (student_id, school_year))
            row = cur.fetchone()
            months_doc = (row["months"] if row else {}) or {}
            if isinstance(data.get("months"), dict):
                for mk, cell in data["months"].items():
                    months_doc[mk] = _lunch_clean_cell(cell)
            elif data.get("month"):
                mk = data["month"]
                prev = months_doc.get(mk, {})
                months_doc[mk] = _lunch_clean_cell({
                    "status": data.get("status", prev.get("status", "home")),
                    "pizzaCount": data.get("pizzaCount", prev.get("pizzaCount", 0)),
                })
            else:
                return jsonify({"error": "month or months required"}), 400
            notes = data.get("notes")
            cur.execute("""
                INSERT INTO lunch_enrollment
                    (student_id, school_year, grade_at_time_of_record, months, notes, updated_by, updated_at)
                VALUES (%s,%s,%s,%s,COALESCE(%s,''),%s,CURRENT_TIMESTAMP)
                ON CONFLICT (student_id, school_year) DO UPDATE SET
                    grade_at_time_of_record=EXCLUDED.grade_at_time_of_record,
                    months=EXCLUDED.months,
                    notes=COALESCE(%s, lunch_enrollment.notes),
                    updated_by=EXCLUDED.updated_by,
                    updated_at=CURRENT_TIMESTAMP
            """, (student_id, school_year, grade, psycopg2.extras.Json(months_doc),
                  notes, updated_by, notes))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()



# ============================================
# COMP RATES (Staff Compensation)
# ============================================

@app.route("/api/comp/rates")
@login_required
def get_comp_rates():
    """Get current comp rates (latest effective_from <= today for each key)"""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (rate_key)
                    rate_id, rate_key, rate_value, label, unit,
                    effective_from, updated_by, updated_at
                FROM comp_rates
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

@app.route("/api/comp/rates/history")
@login_required
def get_comp_rates_history():
    """Get full comp rate history"""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT rate_id, rate_key, rate_value, label, unit,
                       effective_from, updated_by, updated_at
                FROM comp_rates
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

@app.route("/api/comp/rates", methods=["POST"])
@login_required
def save_comp_rates():
    """Save new comp rates with an effective date."""
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
                cur.execute("SELECT label, unit FROM comp_rates WHERE rate_key=%s LIMIT 1", (r.get("rate_key"),))
                existing = cur.fetchone()
                label = existing[0] if existing else r.get("rate_key")
                unit = existing[1] if existing else ""
                cur.execute("""
                    INSERT INTO comp_rates (rate_key, rate_value, label, unit, effective_from, updated_by)
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


@app.route("/api/comp/report")
@login_required
def api_comp_report():
    """
    Monthly comp totals per teacher for tutoring programs.
    OG and 1-on-1 are per session; Homework Center is in 0.5-hr increments.
    Query params: month (1-12), year (e.g. 2026)
    """
    import calendar as cal_mod
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

                # 1. Comp rates active as of first of month
                cur.execute("""
                    SELECT DISTINCT ON (rate_key)
                           rate_key, rate_value
                    FROM   comp_rates
                    WHERE  effective_from::date <= %s
                    ORDER  BY rate_key, effective_from::date DESC
                """, (first_day,))
                rate_rows = cur.fetchall()
                rates = {r["rate_key"]: float(r["rate_value"]) for r in rate_rows}
                defaults = {
                    "comp_og_session":       70.00,
                    "comp_homework_hourly":  20.00,
                    "comp_tutoring_session": 50.00,
                }
                for k, v in defaults.items():
                    rates.setdefault(k, v)

                # 2. Program attendance for the 3 tutoring programs, grouped by teacher
                #    For 1-on-1 tutoring, sessions are weighted by duration (30 min = 0.5, 60 min = 1).
                cur.execute("""
                    SELECT teacher, program_type, SUM(units) AS total_units,
                           COUNT(*) AS session_count,
                           SUM(duration_minutes) AS total_minutes
                    FROM   program_attendance
                    WHERE  session_date::date >= %s AND session_date::date <= %s
                      AND  program_type IN ('og', 'homework', 'tutoring')
                      AND  teacher IS NOT NULL AND teacher != ''
                    GROUP  BY teacher, program_type
                """, (first_day, last_day))

                teachers = {}
                for r in cur.fetchall():
                    name = r["teacher"]
                    if name not in teachers:
                        teachers[name] = {
                            "name": name,
                            "og_sessions": 0, "og_comp": 0,
                            "homework_units": 0, "homework_comp": 0,
                            "tutoring_sessions": 0, "tutoring_comp": 0,
                            "total_comp": 0
                        }
                    t = teachers[name]
                    pt = r["program_type"]
                    units = float(r["total_units"])
                    sessions = int(r["session_count"])
                    total_minutes = int(r["total_minutes"] or 0)

                    if pt == "og":
                        t["og_sessions"] = sessions
                        t["og_comp"] = sessions * rates["comp_og_session"]
                    elif pt == "homework":
                        t["homework_units"] = units
                        t["homework_comp"] = units * rates["comp_homework_hourly"]
                    elif pt == "tutoring":
                        weighted = total_minutes / 60.0
                        t["tutoring_sessions"] = round(weighted, 2)
                        t["tutoring_comp"] = weighted * rates["comp_tutoring_session"]

                for t in teachers.values():
                    t["total_comp"] = round(t["og_comp"] + t["homework_comp"] + t["tutoring_comp"], 2)
                    t["og_comp"] = round(t["og_comp"], 2)
                    t["homework_comp"] = round(t["homework_comp"], 2)
                    t["tutoring_comp"] = round(t["tutoring_comp"], 2)

                result = sorted(teachers.values(), key=lambda t: t["name"])

        finally:
            conn.close()

        return jsonify({
            "teachers": result,
            "rates": rates,
            "month": month,
            "year": year,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================
# BACKUP
# ============================================

BACKUP_PASSWORD = "school2026"
BACKUP_RECIPIENTS = ["ccuneo@mizzentop.org", "ecuneo@mizzentop.org", "kshultz@mizzentop.org"]
BACKUP_SENDER    = "noreply@mizzentopdayschool.org"


def _build_backup_json():
    """Build the backup dict from all key tables and return (json_str, filename)."""
    import json
    from decimal import Decimal
    conn = get_db_connection()
    try:
        backup = {}
        tables = [
            "students", "staff", "programs", "enrollments",
            "attendance_records", "mcard_charges", "electives",
            "daily_dismissal", "dismissal_today", "program_attendance",
            "aftercare_attendance", "billing_rates",
            "households", "parents", "household_members",
            "student_households"
        ]
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for table in tables:
                cur.execute(f"SELECT * FROM {table}")
                rows = fa(cur)
                for row in rows:
                    for k, v in row.items():
                        if hasattr(v, 'isoformat'):
                            row[k] = v.isoformat()
                        elif isinstance(v, Decimal):
                            row[k] = float(v)
                backup[table] = rows
    finally:
        conn.close()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = f"mizzentop_backup_{timestamp}.json"
    return json.dumps(backup, indent=2), filename


@app.route("/backup/download")
def download_backup():
    """Export all key tables as a JSON backup file."""
    if request.args.get("key", "") != BACKUP_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401

    from flask import Response
    json_str, filename = _build_backup_json()
    return Response(
        json_str,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/backup/send-email")
def send_backup_email():
    """Generate backup and email it via Resend. Called by Render Cron Job."""
    import os, json, base64, urllib.request

    cron_secret = os.environ.get("BACKUP_CRON_SECRET", "")
    if request.args.get("key", "") != cron_secret:
        return jsonify({"error": "Unauthorized"}), 401

    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return jsonify({"error": "RESEND_API_KEY not configured"}), 500

    try:
        json_str, filename = _build_backup_json()
    except Exception as e:
        return jsonify({"error": f"Backup generation failed: {e}"}), 500

    # Base64-encode the attachment for Resend API
    attachment_b64 = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")

    today = datetime.now().strftime("%B %d, %Y")
    payload = {
        "from": f"Mizzentop Admin <{BACKUP_SENDER}>",
        "to": BACKUP_RECIPIENTS,
        "subject": f"Mizzentop Database Backup - {today}",
        "html": f"""
            <p>Hello,</p>
            <p>Please find attached the weekly database backup for the Mizzentop Day School Admin Portal,
            generated on <strong>{today}</strong>.</p>
            <p>This backup includes all student records, attendance, dismissal, billing, and program data.</p>
            <p>This is an automated message. To download a backup manually, visit:<br>
            <a href="https://admin.mizzentopdayschool.org/backup/download?key={BACKUP_PASSWORD}">
            admin.mizzentopdayschool.org/backup/download</a></p>
            <p>Mizzentop Day School Admin Portal</p>
        """,
        "attachments": [
            {
                "filename": filename,
                "content":  attachment_b64
            }
        ]
    }

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type":  "application/json",
            "User-Agent":    "MizzentopAdminPortal/1.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return jsonify({"success": True, "resend_id": result.get("id")}), 200
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        return jsonify({"error": f"Resend API error: {error_body}"}), 500


# ============================================
# BILLING REPORT
# ============================================

@app.route("/billing-report")
@require_perm("billing_report")
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
                #    For 1-on-1 tutoring, units are weighted by duration_minutes/60 so a
                #    30-min record counts as 0.5 of a session in billing totals.
                cur.execute("""
                    SELECT student_id, program_type,
                           SUM(CASE WHEN program_type='tutoring'
                                    THEN duration_minutes / 60.0
                                    ELSE units
                               END) AS total_units
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

                # 5. Aftercare — compute hours from actual checkin_time to pickup_time
                cur.execute("""
                    SELECT student_id, session_date, checkin_time, pickup_time
                    FROM   aftercare_attendance
                    WHERE  session_date::date >= %s AND session_date::date <= %s
                      AND  pickup_time IS NOT NULL
                """, (first_day, last_day))
                aftercare_hours = {}
                aftercare_days_d = {}

                def pickup_hours(checkin_str, pickup_str):
                    try:
                        start_min = parse_time_to_minutes(checkin_str) if checkin_str else 16 * 60 + 30
                        end_min   = parse_time_to_minutes(pickup_str)
                        elapsed   = max(0, end_min - start_min)
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
                    hrs = pickup_hours(r["checkin_time"], r["pickup_time"])
                    aftercare_hours[sid] = aftercare_hours.get(sid, 0.0) + hrs
                    if sid not in aftercare_days_d:
                        aftercare_days_d[sid] = set()
                    aftercare_days_d[sid].add(str(r["session_date"]))

                # 5b. School Store purchases
                cur.execute("""
                    SELECT student_id, SUM(quantity * unit_price) AS store_total
                    FROM   store_purchases
                    WHERE  purchase_date::date >= %s AND purchase_date::date <= %s
                    GROUP  BY student_id
                """, (first_day, last_day))
                store_totals = {r["student_id"]: float(r["store_total"]) for r in cur.fetchall()}

                # 6. All active + guest students
                #    Guests (non-enrolled students who attend programs like tutoring)
                #    are included so their charges appear on the full-school report;
                #    the frontend groups them into their own "Guests" section.
                cur.execute("""
                    SELECT student_id, first_name, last_name, grade, status
                    FROM   students
                    WHERE  status IN ('active', 'guest')
                    ORDER  BY grade, last_name, first_name
                """)
                student_rows = cur.fetchall()

                # 7. Lunch-day counts for the month (from school calendar)
                cur.execute("""
                    SELECT category_key, COUNT(*) AS n FROM calendar_day_tags
                    WHERE category_key IN ('lunch_day_prek_k', 'lunch_day_1_8')
                      AND day_date >= %s AND day_date <= %s
                    GROUP BY category_key
                """, (first_day, last_day))
                _lc = {r["category_key"]: int(r["n"]) for r in cur.fetchall()}
                lunch_days_prek_k = _lc.get("lunch_day_prek_k", 0)
                lunch_days_1_8    = _lc.get("lunch_day_1_8", 0)

                # 8. Lunch enrollment for the school year covering this month
                lunch_mk          = f"{year}-{month:02d}"
                lunch_school_year = _lunch_school_year_for(year, month)
                lunch_rates       = _lunch_rates(cur, first_day)
                lunch_dc          = {"ec": lunch_days_prek_k, "g18": lunch_days_1_8}
                cur.execute("""
                    SELECT student_id, months, grade_at_time_of_record
                    FROM   lunch_enrollment
                    WHERE  school_year = %s
                """, (lunch_school_year,))
                lunch_enr = {r["student_id"]: r for r in cur.fetchall()}

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
            store_amt = store_totals.get(sid, 0.0)

            # Lunch — monthly charge (status + pizza) from lunch_enrollment, same math helpers
            le = lunch_enr.get(sid)
            lunch_amt = 0.0
            if le:
                grade_used  = (le.get("grade_at_time_of_record") or "").strip() or s["grade"]
                lunch_cell  = (le.get("months") or {}).get(lunch_mk) or {}
                _, _, l_status_charge, l_pizza_charge = _lunch_month_charge(
                    grade_used, lunch_cell.get("status", "home"),
                    lunch_cell.get("pizzaCount", 0), lunch_dc, lunch_rates)
                lunch_amt = round(l_status_charge + l_pizza_charge, 2)

            if not any([mc_qty, bc_days, ac_hours, og_units, hw_units, oo_units, store_amt, lunch_amt]):
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
                "is_guest":         (s.get("status") == "guest"),
                "mcard":            round(mcard_amt, 2),
                "mcard_qty":        mc_qty,
                "beforecare":       round(before_amt, 2),
                "aftercare":        round(after_amt, 2),
                "og_tutoring":      round(og_amt, 2),
                "homework_center":  round(hw_amt, 2),
                "one_on_one":       round(oo_amt, 2),
                "school_store":     round(store_amt, 2),
                "lunch":            lunch_amt,
                "program_sessions": round(og_units + hw_units + oo_units, 2),
                "care_days":        bc_days + ac_days,
            })

        return jsonify({"month": month, "year": year, "students": results, "rates": rates,
                        "lunch_days_prek_k": lunch_days_prek_k, "lunch_days_1_8": lunch_days_1_8})

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
                    SELECT session_date, program_type, units, teacher, duration_minutes, recorded_by
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
                    if pt == "tutoring":
                        dm = int(r.get("duration_minutes") or 60)
                        weight = dm / 60.0
                        amount = weight * rates[rate_key]
                        dur_str = "1 hr" if dm == 60 else f"{dm} min"
                        detail = f"{dur_str} session"
                    else:
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

                def ac_hours(checkin_str, pickup_str):
                    try:
                        start_min = parse_time_to_minutes(checkin_str) if checkin_str else 16 * 60 + 30
                        end_min   = parse_time_to_minutes(pickup_str)
                        elapsed   = max(0, end_min - start_min)
                        return 1.0 if elapsed <= 60 else 1.0 + math.ceil((elapsed-60)/15)*15/60.0
                    except Exception:
                        return 0.0

                for r in cur.fetchall():
                    hrs    = ac_hours(r.get("checkin_time"), r["pickup_time"])
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

                # 4. School Store purchases
                cur.execute("""
                    SELECT sp.purchase_date, si.name AS item_name, sp.color, sp.size,
                           sp.quantity, sp.unit_price, sp.recorded_by
                    FROM   store_purchases sp
                    JOIN   store_items si ON sp.item_id = si.item_id
                    WHERE  sp.student_id = %s
                      AND  sp.purchase_date::date >= %s AND sp.purchase_date::date <= %s
                    ORDER  BY sp.purchase_date
                """, (student_id, first_day, last_day))
                for r in cur.fetchall():
                    qty = int(r["quantity"])
                    price = float(r["unit_price"])
                    parts = [r["item_name"]]
                    if r.get("color"):
                        parts.append(r["color"])
                    if r.get("size"):
                        parts.append(r["size"])
                    detail = f"{qty}x {' / '.join(parts)} @ ${price:.2f}"
                    rows.append({
                        "date": str(r["purchase_date"]), "program_key": "store",
                        "program_label": "School Store",
                        "detail": detail,
                        "recorded_by": r.get("recorded_by") or "—",
                        "amount": round(qty * price, 2),
                    })

                # 5. Lunch — single monthly line item (status + pizza), same math as report
                lunch_mk          = f"{year}-{month:02d}"
                lunch_school_year = _lunch_school_year_for(year, month)
                lunch_rates       = _lunch_rates(cur, first_day)
                lunch_dc          = _lunch_day_counts(cur, [lunch_mk]).get(lunch_mk, {"ec": 0, "g18": 0})
                cur.execute("""
                    SELECT student_id, grade
                    FROM   students
                    WHERE  student_id = %s
                """, (student_id,))
                _sr = cur.fetchone()
                cur.execute("""
                    SELECT months, grade_at_time_of_record
                    FROM   lunch_enrollment
                    WHERE  student_id = %s AND school_year = %s
                """, (student_id, lunch_school_year))
                _le = cur.fetchone()
                if _le:
                    grade_used = (_le.get("grade_at_time_of_record") or "").strip() or (
                        (_sr or {}).get("grade") or "")
                    lunch_cell = (_le.get("months") or {}).get(lunch_mk) or {}
                    status     = lunch_cell.get("status", "home")
                    pizza_ct   = int(lunch_cell.get("pizzaCount", 0) or 0)
                    l_days, l_rate, l_status_charge, l_pizza_charge = _lunch_month_charge(
                        grade_used, status, pizza_ct, lunch_dc, lunch_rates)
                    lunch_amt = round(l_status_charge + l_pizza_charge, 2)
                    if lunch_amt:
                        bits = []
                        if l_status_charge:
                            bits.append(f"Monthly · {l_days} lunch day{'s' if l_days != 1 else ''} @ ${l_rate:.2f}")
                        if l_pizza_charge:
                            bits.append(f"{pizza_ct} pizza @ ${l_rate:.2f}")
                        rows.append({
                            "date": str(first_day), "program_key": "lunch",
                            "program_label": "Lunch",
                            "detail": " · ".join(bits) or "Lunch",
                            "recorded_by": "—",
                            "amount": lunch_amt,
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
CREATE TABLE IF NOT EXISTS fa_tuition_rates (
    id          SERIAL PRIMARY KEY,
    school_year TEXT NOT NULL,
    division    TEXT NOT NULL,
    tuition     NUMERIC(10,2) NOT NULL,
    UNIQUE(school_year, division)
);

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
    first_name          TEXT,
    grade               TEXT,
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
    'PreK - 3 Half Days': 7980,
    'PreK - 4 Half Days': 10320,
    'PreK - 5 Half Days': 11580,
    'PreK - 3 Extended': 9900,
    'PreK - 4 Extended': 12780,
    'PreK - 5 Extended': 14340,
    'PreK - 3 Full': 11760,
    'PreK - 4 Full': 15240,
    'PreK - 5 Full': 17460,
    'Kindergarten': 19338,
    'Lower School': 24200,
    'Middle School': 26299,
    'Eighth Grade': 27017,
}

def get_tuition_map(school_year=None):
    """Return tuition map for a given school year from DB, falling back to hardcoded defaults."""
    try:
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if school_year:
                    cur.execute("SELECT division, tuition FROM fa_tuition_rates WHERE school_year=%s", (school_year,))
                else:
                    cur.execute("""
                        SELECT DISTINCT ON (division) division, tuition
                        FROM fa_tuition_rates ORDER BY division, school_year DESC
                    """)
                rows = cur.fetchall()
                if rows:
                    return {r['division']: float(r['tuition']) for r in rows}
        finally:
            conn.close()
    except Exception:
        pass
    return dict(TUITION_MAP)

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
                'prior_family_id': row["prior_family_id"],
                'parent_letter': row.get("parent_letter"),
                'students': []
            }
        if row["student_id"]:
            def _f(v):
                return float(v) if v is not None else None
            families[fid]['students'].append({
                'id': row["student_id"],
                'first_name': row["first_name"],
                'grade': row["grade"],
                'school': row["school"],
                'tuition': _f(row["tuition"]),
                'max_discount': _f(row["max_discount"]),
                'fast_aid_rec': _f(row["fast_aid_rec"]),
                'appeal_letter': row["appeal_letter"],
                'family_can_pay': _f(row["family_can_pay"]),
                'mds_aid_amount': _f(row["mds_aid_amount"]),
                'aid_type': row["aid_type"],
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


@app.route('/api/financial-aid/search-families')
@login_required
def api_financial_aid_search_families():
    """Search families across all years except the given one, for prior year linking."""
    q          = request.args.get('q', '').strip()
    exclude_yr = request.args.get('exclude_year', '').strip()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, family_name, fast_id, school_year
                FROM financial_aid_families
                WHERE (%s = '' OR family_name ILIKE %s)
                  AND (%s = '' OR school_year != %s)
                ORDER BY school_year DESC, family_name
                LIMIT 30
            """, (q, f'%{q}%', exclude_yr, exclude_yr))
            results = [dict(r) for r in cur.fetchall()]
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/financial-aid')
@login_required
def api_financial_aid_list():
    """Return all families with their students for a given school year."""
    school_year = request.args.get('year') or sy_short(current_school_year_start())
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT f.id, f.family_name, f.fast_id, f.contract_sent, f.status, f.school_year,
                       f.prior_family_id, f.parent_letter,
                       s.id as student_id, s.first_name, s.grade, s.school, s.tuition, s.max_discount,
                       s.fast_aid_rec, s.appeal_letter, s.family_can_pay,
                       s.mds_aid_amount, s.aid_type, s.net_tuition, s.prior_year_tuition,
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
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Build dynamic update for family table
            fam_fields = []
            fam_vals = []
            for col in ['contract_sent', 'status', 'prior_family_id', 'parent_letter']:
                if col in data:
                    fam_fields.append(f"{col} = %s")
                    fam_vals.append(data[col])
            if fam_fields:
                fam_vals.append(family_id)
                cur.execute(f"UPDATE financial_aid_families SET {', '.join(fam_fields)}, updated_at=NOW() WHERE id=%s", fam_vals)

            # When setting a prior_family_id, back-fill prior_year_tuition on existing student rows
            if 'prior_family_id' in data and data['prior_family_id']:
                prior_fam_id = data['prior_family_id']
                cur.execute("""
                    SELECT first_name, school, net_tuition FROM financial_aid_students
                    WHERE family_id=%s AND net_tuition IS NOT NULL
                """, (prior_fam_id,))
                prior_rows = cur.fetchall()
                # Build lookup: by name (preferred) and by division (fallback)
                prior_by_name = {r['first_name'].strip().lower(): float(r['net_tuition'])
                                 for r in prior_rows if r['first_name']}
                prior_by_div  = {}
                for r in prior_rows:
                    if r['school'] and r['school'] not in prior_by_div:
                        prior_by_div[r['school']] = float(r['net_tuition'])

                if prior_by_name or prior_by_div:
                    cur.execute("SELECT id, first_name, school FROM financial_aid_students WHERE family_id=%s", (family_id,))
                    for stu in cur.fetchall():
                        name_key = (stu['first_name'] or '').strip().lower()
                        prior = prior_by_name.get(name_key)
                        if prior is None:
                            prior = prior_by_div.get(stu['school'])
                        if prior is not None:
                            cur.execute("""
                                UPDATE financial_aid_students
                                SET prior_year_tuition=%s, updated_at=NOW()
                                WHERE id=%s
                            """, (prior, stu['id']))

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

    def n(key):
        v = data.get(key)
        if v is None or v == '': return None
        try: return float(str(v).replace(',', '').replace('$', '').strip())
        except: return None

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO financial_aid_students
                (family_id, school, tuition, max_discount, fast_aid_rec, appeal_letter,
                 family_can_pay, aid_type, mds_aid_amount, net_tuition, prior_year_tuition,
                 family_total, family_total_prior, parent_notes, school_notes, karins_notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (family_id,
                  data.get('school'), n('tuition'), n('max_discount'),
                  n('fast_aid_rec'), data.get('appeal_letter'),
                  n('family_can_pay'), data.get('aid_type'), n('mds_aid_amount'),
                  n('net_tuition'), n('prior_year_tuition'),
                  n('family_total'), n('family_total_prior'),
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

    def n(key):
        """Parse numeric field, stripping commas and dollar signs."""
        v = data.get(key)
        if v is None or v == '': return None
        try: return float(str(v).replace(',', '').replace('$', '').strip())
        except: return None

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE financial_aid_students SET
                    first_name=%s, grade=%s,
                    school=%s, tuition=%s, max_discount=%s, fast_aid_rec=%s,
                    appeal_letter=%s, family_can_pay=%s, aid_type=%s, mds_aid_amount=%s,
                    net_tuition=%s, prior_year_tuition=%s,
                    family_total=%s, family_total_prior=%s,
                    parent_notes=%s, school_notes=%s, karins_notes=%s,
                    updated_at=NOW()
                WHERE id=%s
            """, (data.get('first_name'), data.get('grade'),
                  data.get('school'), n('tuition'), n('max_discount'),
                  n('fast_aid_rec'), data.get('appeal_letter'),
                  n('family_can_pay'), data.get('aid_type'), n('mds_aid_amount'),
                  n('net_tuition'), n('prior_year_tuition'),
                  n('family_total'), n('family_total_prior'),
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


@app.route('/api/financial-aid/families/<int:family_id>', methods=['DELETE'])
@login_required
def api_financial_aid_delete_family(family_id):
    """Delete a family and all its students (cascade)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM financial_aid_families WHERE id=%s", (family_id,))
            if cur.rowcount == 0:
                return jsonify({'error': 'Family not found'}), 404
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
    school_year = (data.get('school_year') or sy_short(current_school_year_start())).strip()
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
    Expected ISMFast columns:
      ApplicantLastNames, AnonymousIdentifier, Grade, TotalRecommendedAward

    Logic:
      - New FAST ID -> create family as active, create student row(s)
      - Existing FAST ID + inactive -> activate, populate financials
      - Existing FAST ID + active -> skip (protect mid-season edits)
    """
    import csv, io
    school_year = request.form.get('school_year') or sy_short(current_school_year_start())
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400

    tuition_map = get_tuition_map(school_year)

    # Grade -> Mizzentop division mapping
    # Note: PreK students (JPK/SPK) map to 'PreK - 5 Full' as default;
    # actual schedule should be set per-student in financial aid.
    GRADE_TO_DIVISION = {
        'pre-k':         'PreK - 5 Full',
        'prek':          'PreK - 5 Full',
        'junior pre-k':  'PreK - 5 Full',
        'senior pre-k':  'PreK - 5 Full',
        'jpk':           'PreK - 5 Full',
        'spk':           'PreK - 5 Full',
        'kindergarten':  'Kindergarten',
        'first grade':   'Lower School',
        'second grade':  'Lower School',
        'third grade':   'Lower School',
        'fourth grade':  'Lower School',
        'fifth grade':   'Middle School',
        'sixth grade':   'Middle School',
        'seventh grade': 'Middle School',
        'eighth grade':  'Eighth Grade',
    }

    def parse_students(grade_str, first_str):
        """
        Parse comma-separated ISMFast grades and first names into
        a list of (first_name, grade_label, division) tuples.
        Each student gets their own entry regardless of shared division.
        """
        if not grade_str:
            return []
        grades  = [g.strip() for g in grade_str.split(',')]
        firsts  = [f.strip().title() for f in first_str.split(',')] if first_str else []
        result = []
        for i, g in enumerate(grades):
            div = GRADE_TO_DIVISION.get(g.lower())
            if not div:
                continue
            fname = firsts[i] if i < len(firsts) else None
            result.append((fname, g.title(), div))
        return result

    def clean_family_name(name):
        """Strip duplicate last names like 'Welch, Welch' -> 'Welch'. Title-case result."""
        if not name:
            return name
        parts = [p.strip().title() for p in name.split(',')]
        unique = list(dict.fromkeys(parts))
        return unique[0] if len(unique) == 1 else ', '.join(unique)

    def money(v):
        if v is None: return None
        try: return float(str(v).replace('$','').replace(',','').strip())
        except: return None

    try:
        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))

        conn = get_db_connection()
        added = skipped = activated = errors_count = 0
        error_names = []
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Fetch existing families: fast_id -> {id, status}
                cur.execute("""
                    SELECT id, fast_id, status
                    FROM financial_aid_families
                    WHERE school_year=%s AND fast_id IS NOT NULL
                """, (school_year,))
                existing = {r['fast_id']: {'id': r['id'], 'status': r['status']} for r in cur.fetchall()}

                # Fetch prior year net_tuition: keyed by fast_id AND by family id (for prior_family_id links)
                cur.execute("""
                    SELECT f.id, f.fast_id, s.school, s.net_tuition
                    FROM financial_aid_families f
                    JOIN financial_aid_students s ON s.family_id = f.id
                    WHERE f.school_year < %s
                      AND s.net_tuition IS NOT NULL
                    ORDER BY f.school_year DESC
                """, (school_year,))
                # Build: fast_id -> {division -> net_tuition}  AND  family_id -> {division -> net_tuition}
                prior_net_by_fastid = {}
                prior_net_by_id     = {}
                for pr in cur.fetchall():
                    fid_key = pr['fast_id']
                    if fid_key and fid_key not in prior_net_by_fastid:
                        prior_net_by_fastid[fid_key] = {}
                    if fid_key and pr['school'] and pr['school'] not in prior_net_by_fastid[fid_key]:
                        prior_net_by_fastid[fid_key][pr['school']] = float(pr['net_tuition'])
                    id_key = pr['id']
                    if id_key not in prior_net_by_id:
                        prior_net_by_id[id_key] = {}
                    if pr['school'] and pr['school'] not in prior_net_by_id[id_key]:
                        prior_net_by_id[id_key][pr['school']] = float(pr['net_tuition'])

                # Also fetch prior_family_id links for current year families
                cur.execute("""
                    SELECT id, fast_id, prior_family_id
                    FROM financial_aid_families
                    WHERE school_year=%s AND prior_family_id IS NOT NULL
                """, (school_year,))
                prior_family_links = {r['fast_id']: r['prior_family_id'] for r in cur.fetchall() if r['fast_id']}

                for raw_row in reader:
                    row = {k.strip(): (v.strip() if v else '') for k, v in raw_row.items() if k and k.strip()}

                    raw_name   = row.get('ApplicantLastNames', '').strip()
                    first_str  = row.get('ApplicantFirstNames', '').strip()
                    fast_id    = row.get('AnonymousIdentifier', '').strip() or None
                    grade_str  = row.get('Grade', '').strip()
                    award_raw  = row.get('TotalRecommendedAward', '').strip()

                    if not raw_name:
                        continue

                    family_name = clean_family_name(raw_name)
                    students    = parse_students(grade_str, first_str)
                    fast_aid    = money(award_raw)  # family-level total from ISMFast

                    try:
                        if fast_id and fast_id in existing:
                            fam_rec = existing[fast_id]
                            if fam_rec['status'] == 'active':
                                # Already active — skip to protect mid-season edits
                                skipped += 1
                                continue

                            # Inactive (carried over) — activate and populate
                            fam_id = fam_rec['id']
                            cur.execute("""
                                UPDATE financial_aid_families
                                SET status='active', updated_at=NOW()
                                WHERE id=%s
                            """, (fam_id,))
                            existing[fast_id]['status'] = 'active'

                            # Replace placeholder student rows with fresh data
                            cur.execute("DELETE FROM financial_aid_students WHERE family_id=%s", (fam_id,))
                            per_student_aid = round(fast_aid / len(students), 2) if fast_aid and students else fast_aid
                            for fname, grade_label, div in students:
                                linked_id = prior_family_links.get(fast_id)
                                prior = (prior_net_by_id.get(linked_id) or {}).get(div) if linked_id else None
                                if prior is None:
                                    prior = (prior_net_by_fastid.get(fast_id) or {}).get(div)
                                cur.execute("""
                                    INSERT INTO financial_aid_students
                                    (family_id, first_name, grade, school, tuition, fast_aid_rec, prior_year_tuition)
                                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                                """, (fam_id, fname, grade_label, div, tuition_map.get(div), per_student_aid, prior))
                            if not students:
                                cur.execute("""
                                    INSERT INTO financial_aid_students (family_id, fast_aid_rec)
                                    VALUES (%s,%s)
                                """, (fam_id, fast_aid))
                            activated += 1

                        else:
                            # Brand new family — create as active
                            cur.execute("""
                                INSERT INTO financial_aid_families
                                (family_name, fast_id, school_year, contract_sent, status)
                                VALUES (%s,%s,%s,false,'active') RETURNING id
                            """, (family_name, fast_id, school_year))
                            fam_id = cur.fetchone()['id']
                            if fast_id:
                                existing[fast_id] = {'id': fam_id, 'status': 'active'}

                            per_student_aid2 = round(fast_aid / len(students), 2) if fast_aid and students else fast_aid
                            for fname, grade_label, div in students:
                                linked_id = prior_family_links.get(fast_id)
                                prior = (prior_net_by_id.get(linked_id) or {}).get(div) if linked_id else None
                                if prior is None:
                                    prior = (prior_net_by_fastid.get(fast_id) or {}).get(div)
                                cur.execute("""
                                    INSERT INTO financial_aid_students
                                    (family_id, first_name, grade, school, tuition, fast_aid_rec, prior_year_tuition)
                                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                                """, (fam_id, fname, grade_label, div, tuition_map.get(div), per_student_aid2, prior))
                            if not students:
                                cur.execute("""
                                    INSERT INTO financial_aid_students (family_id, fast_aid_rec)
                                    VALUES (%s,%s)
                                """, (fam_id, fast_aid))
                            added += 1

                    except Exception as row_err:
                        errors_count += 1
                        error_names.append(family_name)
                        import traceback; traceback.print_exc()
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
    """Download a CSV template matching ISMFast export format."""
    from flask import Response
    import csv, io
    headers = ['ApplicantLastNames', 'ApplicantFirstNames', 'AnonymousIdentifier', 'Grade', 'TotalRecommendedAward']
    examples = [
        ['Smith', 'Emma', '123456', 'Third Grade', '8500'],
        ['Jones, Jones', 'Olivia, Liam', '123457', 'First Grade, Sixth Grade', '12000'],
        ['Garcia', 'Sofia', '123458', 'Kindergarten', '0'],
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for ex in examples:
        w.writerow(ex)
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=financial_aid_upload_template.csv'}
    )


@app.route('/api/financial-aid/clear-year', methods=['DELETE'])
@login_required
def api_financial_aid_clear_year():
    """Delete all families (and their students via cascade) for a given school year."""
    school_year = request.args.get('year', '').strip()
    if not school_year:
        return jsonify({'error': 'year parameter required'}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM financial_aid_families WHERE school_year=%s", (school_year,))
            deleted = cur.rowcount
        conn.commit()
        return jsonify({'ok': True, 'deleted': deleted, 'year': school_year})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


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
                    tuition = get_tuition_map(to_year).get(s['school'])
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
                    tuition = get_tuition_map('2025-26').get(school)
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
# HOUSEHOLDS API
# ============================================

@app.route("/api/households")
@login_required
def get_households():
    status = request.args.get("status", "")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status:
                cur.execute("SELECT * FROM households WHERE status = %s ORDER BY family_name", (status,))
            else:
                cur.execute("SELECT * FROM households ORDER BY family_name")
            households = fa(cur)
            # Attach students and parents for each household
            for h in households:
                cur.execute("""
                    SELECT s.student_id, s.first_name, s.last_name, s.grade, s.status,
                           sh.is_primary, sh.custody_notes
                    FROM student_households sh
                    JOIN students s ON s.student_id = sh.student_id
                    WHERE sh.household_id = %s
                    ORDER BY s.last_name, s.first_name
                """, (h["household_id"],))
                h["students"] = fa(cur)
                cur.execute("""
                    SELECT p.parent_id, p.first_name, p.last_name, p.email, p.phone,
                           p.relationship_type, p.can_pickup, hm.role
                    FROM household_members hm
                    JOIN parents p ON p.parent_id = hm.parent_id
                    WHERE hm.household_id = %s
                    ORDER BY hm.role, p.last_name
                """, (h["household_id"],))
                h["parents"] = fa(cur)
        return jsonify(households)
    finally:
        conn.close()


@app.route("/api/households/<int:household_id>")
@login_required
def get_household(household_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM households WHERE household_id = %s", (household_id,))
            h = fo(cur)
            if not h:
                return jsonify({"error": "Household not found"}), 404
            cur.execute("""
                SELECT s.student_id, s.first_name, s.last_name, s.grade, s.status,
                       sh.is_primary, sh.custody_notes
                FROM student_households sh
                JOIN students s ON s.student_id = sh.student_id
                WHERE sh.household_id = %s
                ORDER BY s.last_name, s.first_name
            """, (household_id,))
            h["students"] = fa(cur)
            cur.execute("""
                SELECT p.parent_id, p.first_name, p.last_name, p.email, p.phone,
                       p.relationship_type, p.can_pickup, hm.role
                FROM household_members hm
                JOIN parents p ON p.parent_id = hm.parent_id
                WHERE hm.household_id = %s
                ORDER BY hm.role, p.last_name
            """, (household_id,))
            h["parents"] = fa(cur)
        return jsonify(h)
    finally:
        conn.close()


# ============================================
# FAMILY DIRECTORY
# ============================================

@app.route("/family-directory")
@login_required
def family_directory_page():
    return send_from_directory(".", "family_directory.html")


@app.route("/api/family-directory")
@login_required
def get_family_directory():
    """Active families with full contact info + emergency contacts, for staff lookup
    and printable per-student emergency sheets. Only includes households that have at
    least one active student, and lists only the active students within each."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT h.household_id, h.family_name,
                       h.address_line_1, h.address_line_2, h.city, h.state, h.zip,
                       h.primary_phone, h.primary_email, h.status, h.directory_opt_out
                FROM households h
                JOIN student_households sh ON sh.household_id = h.household_id
                JOIN students s ON s.student_id = sh.student_id
                WHERE s.status = 'active'
                ORDER BY h.family_name
            """)
            households = fa(cur)
            for h in households:
                cur.execute("""
                    SELECT s.student_id, s.first_name, s.last_name, s.grade, s.status,
                           s.date_of_birth, s.emergency_contact_name, s.emergency_contact_phone,
                           sh.is_primary, sh.custody_notes,
                           hr.first_name  || ' ' || hr.last_name  AS homeroom_teacher_name,
                           adv.first_name || ' ' || adv.last_name AS advisory_teacher_name
                    FROM student_households sh
                    JOIN students s ON s.student_id = sh.student_id
                    LEFT JOIN staff hr  ON s.homeroom_teacher_id  = hr.staff_id
                    LEFT JOIN staff adv ON s.advisory_teacher_id  = adv.staff_id
                    WHERE sh.household_id = %s AND s.status = 'active'
                    ORDER BY s.grade, s.last_name, s.first_name
                """, (h["household_id"],))
                h["students"] = fa(cur)
                cur.execute("""
                    SELECT p.parent_id, p.first_name, p.last_name, p.email, p.phone,
                           p.relationship_type, p.can_pickup, hm.role
                    FROM household_members hm
                    JOIN parents p ON p.parent_id = hm.parent_id
                    WHERE hm.household_id = %s
                    ORDER BY hm.role, p.last_name, p.first_name
                """, (h["household_id"],))
                h["parents"] = fa(cur)
        return jsonify(households)
    finally:
        conn.close()


@app.route("/family-manager")
@require_perm("family_manager")
def family_manager_page():
    return send_from_directory(".", "family_manager.html")


@app.route("/api/family-directory/manage")
@people_required
def get_family_directory_manage():
    """Editable Family Manager data: every household that has at least one student
    (with billing/status fields), plus its students (incl. emergency contact + custody,
    active and inactive) and its parents. Superset of /api/family-directory."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT h.household_id, h.family_name,
                       h.address_line_1, h.address_line_2, h.city, h.state, h.zip,
                       h.primary_phone, h.primary_email, h.status, h.billing_notes,
                       h.directory_opt_out
                FROM households h
                WHERE EXISTS (
                    SELECT 1 FROM student_households sh WHERE sh.household_id = h.household_id
                )
                ORDER BY h.family_name
            """)
            households = fa(cur)
            for h in households:
                cur.execute("""
                    SELECT s.student_id, s.first_name, s.last_name, s.grade, s.status,
                           s.date_of_birth, s.emergency_contact_name, s.emergency_contact_phone,
                           sh.is_primary, sh.custody_notes
                    FROM student_households sh
                    JOIN students s ON s.student_id = sh.student_id
                    WHERE sh.household_id = %s
                    ORDER BY s.status, s.grade, s.last_name, s.first_name
                """, (h["household_id"],))
                h["students"] = fa(cur)
                cur.execute("""
                    SELECT p.parent_id, p.first_name, p.last_name, p.email, p.phone,
                           p.relationship_type, p.can_pickup, p.notes, hm.role
                    FROM household_members hm
                    JOIN parents p ON p.parent_id = hm.parent_id
                    WHERE hm.household_id = %s
                    ORDER BY hm.role, p.last_name, p.first_name
                """, (h["household_id"],))
                h["parents"] = fa(cur)
        return jsonify(households)
    finally:
        conn.close()


@app.route("/api/households", methods=["POST"])
@login_required
def create_household():
    data = request.json or {}
    if not data.get("family_name"):
        return jsonify({"error": "family_name is required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO households (family_name, address_line_1, address_line_2,
                    city, state, zip, primary_phone, primary_email, status, billing_notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
            """, (
                data["family_name"],
                data.get("address_line_1", ""),
                data.get("address_line_2", ""),
                data.get("city", ""),
                data.get("state", ""),
                data.get("zip", ""),
                data.get("primary_phone", ""),
                data.get("primary_email", ""),
                data.get("status", "active"),
                data.get("billing_notes", ""),
            ))
            household = fo(cur)
        conn.commit()
        return jsonify(household), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/households/<int:household_id>", methods=["PUT"])
@login_required
def update_household(household_id):
    data = request.json or {}
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE households SET
                    family_name    = COALESCE(%s, family_name),
                    address_line_1 = COALESCE(%s, address_line_1),
                    address_line_2 = COALESCE(%s, address_line_2),
                    city           = COALESCE(%s, city),
                    state          = COALESCE(%s, state),
                    zip            = COALESCE(%s, zip),
                    primary_phone  = COALESCE(%s, primary_phone),
                    primary_email  = COALESCE(%s, primary_email),
                    status         = COALESCE(%s, status),
                    billing_notes  = COALESCE(%s, billing_notes),
                    directory_opt_out = COALESCE(%s, directory_opt_out),
                    updated_at     = CURRENT_TIMESTAMP
                WHERE household_id = %s
                RETURNING *
            """, (
                data.get("family_name"),
                data.get("address_line_1"),
                data.get("address_line_2"),
                data.get("city"),
                data.get("state"),
                data.get("zip"),
                data.get("primary_phone"),
                data.get("primary_email"),
                data.get("status"),
                data.get("billing_notes"),
                data.get("directory_opt_out"),
                household_id,
            ))
            household = fo(cur)
            if not household:
                return jsonify({"error": "Household not found"}), 404
        conn.commit()
        return jsonify(household)
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/households/<int:household_id>", methods=["DELETE"])
@superadmin_required
def delete_household(household_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM households WHERE household_id = %s", (household_id,))
            if cur.rowcount == 0:
                return jsonify({"error": "Household not found"}), 404
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# PARENTS API
# ============================================

@app.route("/api/parents")
@login_required
def get_parents():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*,
                    COALESCE(json_agg(json_build_object(
                        'household_id', h.household_id,
                        'family_name', h.family_name,
                        'role', hm.role
                    )) FILTER (WHERE h.household_id IS NOT NULL), '[]') AS households
                FROM parents p
                LEFT JOIN household_members hm ON hm.parent_id = p.parent_id
                LEFT JOIN households h ON h.household_id = hm.household_id
                GROUP BY p.parent_id
                ORDER BY p.last_name, p.first_name
            """)
            parents = fa(cur)
        return jsonify(parents)
    finally:
        conn.close()


@app.route("/api/parents/<int:parent_id>")
@login_required
def get_parent(parent_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM parents WHERE parent_id = %s", (parent_id,))
            parent = fo(cur)
            if not parent:
                return jsonify({"error": "Parent not found"}), 404
            cur.execute("""
                SELECT h.household_id, h.family_name, h.status, hm.role
                FROM household_members hm
                JOIN households h ON h.household_id = hm.household_id
                WHERE hm.parent_id = %s
            """, (parent_id,))
            parent["households"] = fa(cur)
        return jsonify(parent)
    finally:
        conn.close()


@app.route("/api/parents", methods=["POST"])
@login_required
def create_parent():
    data = request.json or {}
    if not data.get("first_name") or not data.get("last_name"):
        return jsonify({"error": "first_name and last_name are required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO parents (first_name, last_name, email, phone,
                    relationship_type, can_pickup, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
            """, (
                data["first_name"],
                data["last_name"],
                data.get("email", ""),
                data.get("phone", ""),
                data.get("relationship_type", ""),
                data.get("can_pickup", True),
                data.get("notes", ""),
            ))
            parent = fo(cur)
        conn.commit()
        return jsonify(parent), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/parents/<int:parent_id>", methods=["PUT"])
@login_required
def update_parent(parent_id):
    data = request.json or {}
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE parents SET
                    first_name        = COALESCE(%s, first_name),
                    last_name         = COALESCE(%s, last_name),
                    email             = COALESCE(%s, email),
                    phone             = COALESCE(%s, phone),
                    relationship_type = COALESCE(%s, relationship_type),
                    can_pickup        = COALESCE(%s, can_pickup),
                    notes             = COALESCE(%s, notes),
                    updated_at        = CURRENT_TIMESTAMP
                WHERE parent_id = %s
                RETURNING *
            """, (
                data.get("first_name"),
                data.get("last_name"),
                data.get("email"),
                data.get("phone"),
                data.get("relationship_type"),
                data.get("can_pickup"),
                data.get("notes"),
                parent_id,
            ))
            parent = fo(cur)
            if not parent:
                return jsonify({"error": "Parent not found"}), 404
        conn.commit()
        return jsonify(parent)
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/parents/<int:parent_id>", methods=["DELETE"])
@superadmin_required
def delete_parent(parent_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM parents WHERE parent_id = %s", (parent_id,))
            if cur.rowcount == 0:
                return jsonify({"error": "Parent not found"}), 404
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# HOUSEHOLD LINKING API (students & parents)
# ============================================

@app.route("/api/households/<int:household_id>/students", methods=["POST"])
@login_required
def link_student_to_household(household_id):
    """Add a student to a household"""
    data = request.json or {}
    student_id = data.get("student_id")
    if not student_id:
        return jsonify({"error": "student_id is required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO student_households (student_id, household_id, is_primary, custody_notes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (student_id, household_id) DO UPDATE
                    SET is_primary = EXCLUDED.is_primary, custody_notes = EXCLUDED.custody_notes
                RETURNING *
            """, (
                student_id,
                household_id,
                data.get("is_primary", True),
                data.get("custody_notes", ""),
            ))
            link = fo(cur)
        conn.commit()
        return jsonify(link), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/households/<int:household_id>/students/<int:student_id>", methods=["DELETE"])
@login_required
def unlink_student_from_household(household_id, student_id):
    """Remove a student from a household"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM student_households
                WHERE student_id = %s AND household_id = %s
            """, (student_id, household_id))
            if cur.rowcount == 0:
                return jsonify({"error": "Link not found"}), 404
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/households/<int:household_id>/parents", methods=["POST"])
@login_required
def link_parent_to_household(household_id):
    """Add a parent to a household"""
    data = request.json or {}
    parent_id = data.get("parent_id")
    if not parent_id:
        return jsonify({"error": "parent_id is required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO household_members (household_id, parent_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (household_id, parent_id) DO UPDATE SET role = EXCLUDED.role
                RETURNING *
            """, (
                household_id,
                parent_id,
                data.get("role", "primary"),
            ))
            link = fo(cur)
        conn.commit()
        return jsonify(link), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/households/<int:household_id>/parents/<int:parent_id>", methods=["DELETE"])
@login_required
def unlink_parent_from_household(household_id, parent_id):
    """Remove a parent from a household"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM household_members
                WHERE household_id = %s AND parent_id = %s
            """, (household_id, parent_id))
            if cur.rowcount == 0:
                return jsonify({"error": "Link not found"}), 404
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/households/seed-from-students", methods=["POST"])
@superadmin_required
def seed_households_from_students():
    """One-time utility: auto-create households from student last names and link them"""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get all active students not yet in a household
            cur.execute("""
                SELECT s.student_id, s.first_name, s.last_name
                FROM students s
                LEFT JOIN student_households sh ON sh.student_id = s.student_id
                WHERE s.status = 'active' AND sh.student_id IS NULL
                ORDER BY s.last_name, s.first_name
            """)
            unlinked = fa(cur)
            if not unlinked:
                return jsonify({"message": "All active students already linked", "created": 0})

            # Group by last name
            families = {}
            for s in unlinked:
                families.setdefault(s["last_name"], []).append(s)

            created = 0
            for last_name, students in families.items():
                # Create household
                cur.execute("""
                    INSERT INTO households (family_name, status)
                    VALUES (%s, 'active')
                    RETURNING household_id
                """, (last_name,))
                hid = cur.fetchone()["household_id"]
                # Link students
                for s in students:
                    cur.execute("""
                        INSERT INTO student_households (student_id, household_id, is_primary)
                        VALUES (%s, %s, TRUE)
                        ON CONFLICT DO NOTHING
                    """, (s["student_id"], hid))
                created += 1

        conn.commit()
        return jsonify({"message": f"Created {created} households", "created": created})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# ONE-TIME MIGRATIONS
# ============================================

@app.route('/admin/update-tuition-from-rates', methods=['POST'])
@login_required
def update_tuition_from_rates():
    """
    Update all student tuition values for a given school year
    based on current fa_tuition_rates entries.
    """
    data = request.json or {}
    school_year = data.get('school_year', '2026-27').strip()
    tuition_map = get_tuition_map(school_year)
    if not tuition_map:
        return jsonify({'error': f'No tuition rates found for {school_year}'}), 400
    conn = get_db_connection()
    try:
        updated = 0
        with conn.cursor() as cur:
            for division, tuition in tuition_map.items():
                cur.execute("""
                    UPDATE financial_aid_students s
                    SET tuition=%s, updated_at=NOW()
                    FROM financial_aid_families f
                    WHERE s.family_id=f.id
                      AND f.school_year=%s
                      AND s.school=%s
                """, (tuition, school_year, division))
                updated += cur.rowcount
        conn.commit()
        return jsonify({'ok': True, 'updated': updated, 'school_year': school_year, 'rates': tuition_map})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


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

            # Add first_name to students if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_students' AND column_name='first_name'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_students ADD COLUMN first_name TEXT")
                results.append("Added first_name column to financial_aid_students")
            else:
                results.append("first_name column already exists (skipped)")

            # Add grade to students if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_students' AND column_name='grade'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_students ADD COLUMN grade TEXT")
                results.append("Added grade column to financial_aid_students")
            else:
                results.append("grade column already exists (skipped)")

            # Add prior_family_id to families if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_families' AND column_name='prior_family_id'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_families ADD COLUMN prior_family_id INTEGER REFERENCES financial_aid_families(id) ON DELETE SET NULL")
                results.append("Added prior_family_id column to financial_aid_families")
            else:
                results.append("prior_family_id column already exists (skipped)")

            # Add aid_type to students if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_students' AND column_name='aid_type'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_students ADD COLUMN aid_type TEXT")
                results.append("Added aid_type column to financial_aid_students")
            else:
                results.append("aid_type column already exists (skipped)")

            # Add parent_letter to families if missing
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='financial_aid_families' AND column_name='parent_letter'
            """)
            if not cur.fetchone():
                cur.execute("ALTER TABLE financial_aid_families ADD COLUMN parent_letter TEXT")
                results.append("Added parent_letter column to financial_aid_families")
            else:
                results.append("parent_letter column already exists (skipped)")

            # Create fa_tuition_rates table if missing
            cur.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name='fa_tuition_rates')")
            if not cur.fetchone()[0]:
                cur.execute("""
                    CREATE TABLE fa_tuition_rates (
                        id          SERIAL PRIMARY KEY,
                        school_year TEXT NOT NULL,
                        division    TEXT NOT NULL,
                        tuition     NUMERIC(10,2) NOT NULL,
                        UNIQUE(school_year, division)
                    )
                """)
                results.append("Created fa_tuition_rates table")
            else:
                results.append("fa_tuition_rates table already exists (skipped)")

            # Seed tuition rates if table is empty
            cur.execute("SELECT COUNT(*) FROM fa_tuition_rates")
            if cur.fetchone()[0] == 0:
                rates = [
                    ('2025-26', 'PreK - 3 Half Days', 7980),
                    ('2025-26', 'PreK - 4 Half Days', 10320),
                    ('2025-26', 'PreK - 5 Half Days', 11580),
                    ('2025-26', 'PreK - 3 Extended', 9900),
                    ('2025-26', 'PreK - 4 Extended', 12780),
                    ('2025-26', 'PreK - 5 Extended', 14340),
                    ('2025-26', 'PreK - 3 Full', 11760),
                    ('2025-26', 'PreK - 4 Full', 15240),
                    ('2025-26', 'PreK - 5 Full', 17460),
                    ('2025-26', 'Kindergarten', 19338),
                    ('2025-26', 'Lower School', 24200),
                    ('2025-26', 'Middle School', 26299),
                    ('2025-26', 'Eighth Grade', 27017),
                    ('2026-27', 'PreK - 3 Half Days', 8459),
                    ('2026-27', 'PreK - 4 Half Days', 10939),
                    ('2026-27', 'PreK - 5 Half Days', 12275),
                    ('2026-27', 'PreK - 3 Extended', 10494),
                    ('2026-27', 'PreK - 4 Extended', 13547),
                    ('2026-27', 'PreK - 5 Extended', 15200),
                    ('2026-27', 'PreK - 3 Full', 12466),
                    ('2026-27', 'PreK - 4 Full', 16154),
                    ('2026-27', 'PreK - 5 Full', 18508),
                    ('2026-27', 'Kindergarten', 20498),
                    ('2026-27', 'Lower School', 25651),
                    ('2026-27', 'Middle School', 27877),
                    ('2026-27', 'Eighth Grade', 28638),
                ]
                for yr, div, amt in rates:
                    cur.execute("""
                        INSERT INTO fa_tuition_rates (school_year, division, tuition)
                        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                    """, (yr, div, amt))
                results.append("Seeded tuition rates for 2025-26 and 2026-27")
            else:
                results.append("Tuition rates already seeded (skipped)")

        conn.commit()
        return jsonify({'ok': True, 'results': results})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()



@app.route('/admin/populate-advisory-electives', methods=['POST'])
@superadmin_required
def populate_advisory_electives():
    """One-time route to populate advisory teachers and T3 electives for all students."""
    conn = get_db_connection()
    results = []
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # --- A. Clear old placeholder electives and insert real T3 electives ---
            cur.execute("DELETE FROM student_electives")
            cur.execute("DELETE FROM electives")
            results.append("Cleared old electives and student_electives")

            t3_electives = [
                ('World Travelers', 'LS', 3),
                ('Walking Buddies', 'LS', 3),
                ('Puzzle Masters', 'LS', 3),
                ('Reading Club', 'LS', 3),
                ('Crafting Corner', 'LS', 3),
                ('Goal Getters', 'LS', 3),
                ('Canva Pros Jr.', 'LS', 3),
                ('Engineering/Robotics', 'MS', 3),
                ('Reading Club', 'MS', 3),
                ('Shark Tank Jr', 'MS', 3),
                ('Wall Street Warriors', 'MS', 3),
                ('Pasta Pals', 'MS', 3),
                ('Canva Pros', 'MS', 3),
                ('Financial Literacy', 'MS', 3),
                ('Sabores y Colores', 'MS', 3),
            ]
            # Drop unique constraint on name since "Reading Club" appears twice (LS + MS)
            cur.execute("ALTER TABLE electives DROP CONSTRAINT IF EXISTS electives_name_key")

            elective_map = {}  # (name, division) -> elective_id
            for name, division, trimester in t3_electives:
                cur.execute("""
                    INSERT INTO electives (name, active, trimester, division)
                    VALUES (%s, 1, %s, %s) RETURNING elective_id
                """, (name, trimester, division))
                eid = cur.fetchone()['elective_id']
                elective_map[(name, division)] = eid
            results.append(f"Inserted {len(t3_electives)} T3 electives")

            # --- Helper: match student by first + last name ---
            name_corrections = {
                'LJ Holly': ('John', 'Holly'),
                'Mary Clare Englehart': ('Mary Clare', 'Englehart'),
                'Mikey Gerosa': ('Michael', 'Gerosa'),
                'Cody Pisciarino': ('Cody', 'Piscarino'),
                'Gigi Ponzini': ('Giuliana', 'Ponzini'),
                'Paulie Desatnik': ('Paul', 'Desatnik'),
                'Lexi Modupe': ('Alexandria', 'Modupe'),
                'Geo Kumar': ('Gotham "Geo"', 'Kumar'),
                'Scarlet Lent': ('Scarlett', 'Lent'),
                'John William Salterelli': ('John William', 'Saltarelli'),
                'Keira Philips': ('Keira', 'Phillips'),
                'Coleman Cynamon Ferris': ('Coleman', 'Cynamon-Ferris'),
                'Lilliana Welsh': ('Lilliana', 'Welch'),
                'Maddie Byrne': ('Madeleine', 'Byrne'),
                'Cat Taylor': ('Catherine', 'Taylor'),
                'Eli Rodriguez': ('Elijah', 'Rodriguez'),
                'Diana Furhman': ('Diana', 'Fuhrman'),
                'Gabby Modupe': ('Gabriella', 'Modupe'),
            }

            def find_student(full_name):
                if full_name in name_corrections:
                    first, last = name_corrections[full_name]
                else:
                    parts = full_name.strip().split()
                    if len(parts) < 2:
                        return None
                    first = parts[0]
                    last = ' '.join(parts[1:])
                cur.execute(
                    "SELECT student_id FROM students WHERE first_name ILIKE %s AND last_name ILIKE %s AND status='active'",
                    (first, last)
                )
                row = cur.fetchone()
                return row['student_id'] if row else None

            # --- B. Find staff by name ---
            def find_staff(full_name):
                parts = full_name.strip().split()
                first = parts[0]
                last = ' '.join(parts[1:])
                cur.execute(
                    "SELECT staff_id FROM staff WHERE first_name ILIKE %s AND last_name ILIKE %s AND status='active'",
                    (first, last)
                )
                row = cur.fetchone()
                return row['staff_id'] if row else None

            # --- C. Populate advisory_teacher_id for MS students ---
            advisory_assignments = {
                'Erica Jordan': [
                    'Meredith Burlington', 'Sonali Julka', 'Lucas Garland', 'Adam Hanafi',
                    'Mark Jennings', 'Victor Danieluc', 'Adam Wetterhorn'
                ],
                'Lissannia Fermin': [
                    'Lukas Cuppek', 'Andrew Williams', 'Alex Mack', 'Addison Ceballos',
                    'Aubrey Murphy', 'Marcellus Johnson'
                ],
                'Shane Caroppoli': [
                    'Dylan Mack', 'Ben Catalano', 'LJ Holly', 'Vivek Suseelan',
                    'Mary Clare Englehart', 'Lily Linquist'
                ],
                'Jane Colbert': [
                    'Isabella Anderson', 'William Hardisty', 'Victoria Braham', 'Tyler Mazzucca',
                    'Andrew Arena', 'Graham Fritts', 'Mikey Gerosa'
                ],
                'Noelle Semenza': [
                    'Elias Garay', 'Remy Bell', 'Anna Yacoub', 'Aurora DeHaarte',
                    'Liam Mutchler', 'Cameron Martin'
                ],
                'Kelly Curra': [
                    'Cody Pisciarino', 'Gigi Ponzini', 'Paulie Desatnik', 'Jason Hampton',
                    'Lexi Modupe', 'Geo Kumar'
                ],
                'Jancy McLeod': [
                    'Blake Sukow', 'Nicolas Barahona', 'Kailani Broderick', 'Luca Parisi',
                    'Scarlet Lent', 'Noah Garay'
                ],
                'Eliza Goff': [
                    'Eddie Huron', 'Logan Ceballos', 'Joaquin Johnson', 'Ava Perry',
                    'Nicole Cuppek', 'Konstantinos Bellos'
                ],
                'Lara Keenan': [
                    'Michael Botter', 'Jase Boardman', 'Noah DuPlessis', "Caroline O'Keefe",
                    'Kacey McLaughlin', 'Samuel Shiller'
                ],
                'Mindy Poon': [
                    'Silas Fitzpatrick', 'Jayden Garland', 'Christopher Argueta',
                    'Maxwell Mezzapelle', 'John William Salterelli', 'Dakota Holly',
                    'Keira Philips', 'Coleman Cynamon Ferris', 'Layla Eyring',
                    'AJ Ferro', 'Teo Danieluc', 'Annie Fritts', 'Dylan Choi',
                    'Jake Ceballos', 'Gabriel Oludoja', 'Sydney Johnson', 'Brooke Hampton'
                ],
            }

            advisory_count = 0
            advisory_errors = []
            for advisor_name, student_names in advisory_assignments.items():
                staff_id = find_staff(advisor_name)
                if not staff_id:
                    advisory_errors.append(f"Advisor not found: {advisor_name}")
                    continue
                for sname in student_names:
                    sid = find_student(sname)
                    if sid:
                        cur.execute("UPDATE students SET advisory_teacher_id=%s WHERE student_id=%s", (staff_id, sid))
                        advisory_count += 1
                    else:
                        advisory_errors.append(f"Student not found: {sname}")
            results.append(f"Set advisory teacher for {advisory_count} students")
            if advisory_errors:
                results.append(f"Advisory errors: {advisory_errors}")

            # --- D. Populate student_electives for T3 ---
            ls_elective_roster = {
                ('World Travelers', 'LS'): [
                    'Noah Nelson', 'Jake Broderick', 'Ryan Kerr', 'Arden Englehart',
                    'Lucas Berg', 'Deacon Lent', 'Noah Johnson'
                ],
                ('Walking Buddies', 'LS'): [
                    'Lilliana Welsh', 'Olivia Berg', 'Ella Steiner', 'Peter Danieluc',
                    'Danelyn Coyle', 'Brady Gillman', 'Layla Penn', 'Maddie Byrne'
                ],
                ('Puzzle Masters', 'LS'): [
                    'Sophia Garay', 'Thomas Horan', 'Chengduo Zou', 'James Lallouz',
                    'Bowie Brody', 'David Friedman', 'Lia Steiner', 'Owen Denley'
                ],
                ('Reading Club', 'LS'): [
                    'Charlotte Roe', 'Sabrina Hobson', 'Ella Jennings', 'Tommy Beck',
                    'Noel Mutchler', 'Zephram Thomas', 'Kay Friedman', 'Miles Dolan'
                ],
                ('Crafting Corner', 'LS'): [
                    'Danny Braham', 'Abigail Gavin', 'Will Huron', 'Cat Taylor',
                    'Colt Laino', 'Zara Intwala', 'Carly Hildenbrand', 'Reign Myint'
                ],
                ('Goal Getters', 'LS'): [
                    'Finn Kelsay', 'Hudson Sorrentino', 'Derek Kelsay', 'Eli Rodriguez',
                    "Owen O'Keefe", 'Ezra Johnson', 'Cameron Pool', 'Grant McCarthy'
                ],
                ('Canva Pros Jr.', 'LS'): [
                    'Grace Loosen', 'Diana Furhman', 'Aurelia Nelson', 'Gabby Modupe',
                    'Leo Hardisty', 'Andy Harrison', 'Olivier Suseelan'
                ],
            }
            ms_elective_roster = {
                ('Engineering/Robotics', 'MS'): [
                    'Silas Fitzpatrick', 'Jayden Garland', 'Christopher Argueta',
                    'Maxwell Mezzapelle', 'John William Salterelli', 'Dylan Mack',
                    'Sonali Julka', 'Vivek Suseelan', 'LJ Holly', 'Remy Bell'
                ],
                ('Reading Club', 'MS'): [
                    'Meredith Burlington', 'Lily Linquist', 'Anna Yacoub',
                    'Noah DuPlessis', 'Liam Mutchler', 'Aurora DeHaarte'
                ],
                ('Shark Tank Jr', 'MS'): [
                    'Dakota Holly', 'Keira Philips', 'Elias Garay', 'Tyler Mazzucca',
                    'Joaquin Johnson', 'Lucas Garland', 'Ben Catalano', 'Samuel Shiller'
                ],
                ('Wall Street Warriors', 'MS'): [
                    'Coleman Cynamon Ferris', 'Gigi Ponzini', 'Andrew Williams',
                    'Lexi Modupe', 'Victoria Braham', 'Geo Kumar', 'Marcellus Johnson',
                    'Graham Fritts', 'Noah Garay'
                ],
                ('Pasta Pals', 'MS'): [
                    'Layla Eyring', 'AJ Ferro', 'Teo Danieluc', 'William Hardisty',
                    'Michael Botter', 'Mikey Gerosa', 'Luca Parisi', 'Jason Hampton',
                    'Kacey McLaughlin', 'Cameron Martin'
                ],
                ('Canva Pros', 'MS'): [
                    'Annie Fritts', 'Dylan Choi', 'Isabella Anderson', 'Eddie Huron',
                    'Lukas Cuppek', 'Nicolas Barahona', 'Ava Perry', 'Paulie Desatnik',
                    'Jase Boardman', 'Nicole Cuppek', 'Mary Clare Englehart'
                ],
                ('Financial Literacy', 'MS'): [
                    'Jake Ceballos', 'Gabriel Oludoja', 'Blake Sukow', 'Logan Ceballos',
                    'Victor Danieluc', 'Konstantinos Bellos', "Caroline O'Keefe", 'Adam Hanafi'
                ],
                ('Sabores y Colores', 'MS'): [
                    'Sydney Johnson', 'Brooke Hampton', 'Addison Ceballos',
                    'Cody Pisciarino', 'Mark Jennings', 'Alex Mack', 'Kailani Broderick',
                    'Aubrey Murphy', 'Adam Wetterhorn', 'Andrew Arena', 'Scarlet Lent'
                ],
            }

            elective_count = 0
            elective_errors = []
            for roster in [ls_elective_roster, ms_elective_roster]:
                for (elective_name, division), student_names in roster.items():
                    eid = elective_map.get((elective_name, division))
                    if not eid:
                        elective_errors.append(f"Elective not found: {elective_name} ({division})")
                        continue
                    for sname in student_names:
                        sid = find_student(sname)
                        if sid:
                            cur.execute("""
                                INSERT INTO student_electives (student_id, elective_id, trimester)
                                VALUES (%s, %s, 3) ON CONFLICT (student_id, trimester) DO UPDATE SET elective_id=EXCLUDED.elective_id
                            """, (sid, eid))
                            elective_count += 1
                        else:
                            elective_errors.append(f"Student not found for elective: {sname}")
            results.append(f"Assigned {elective_count} student elective enrollments")
            if elective_errors:
                results.append(f"Elective errors: {elective_errors}")

        conn.commit()
        return jsonify({'ok': True, 'results': results})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/financial-aid/tuition-rates')
@login_required
def fa_tuition_rates_page():
    return send_from_directory('.', 'fa_tuition_rates.html')

@app.route('/api/fa-tuition-rates')
@login_required
def api_fa_tuition_rates_get():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT school_year, division, tuition FROM fa_tuition_rates ORDER BY school_year DESC, division")
            rows = cur.fetchall()
        years = {}
        for r in rows:
            yr = r['school_year']
            if yr not in years: years[yr] = {}
            years[yr][r['division']] = float(r['tuition'])
        return jsonify(years)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/fa-tuition-rates', methods=['POST'])
@login_required
def api_fa_tuition_rates_set():
    data = request.json or {}
    school_year = data.get('school_year', '').strip()
    rates = data.get('rates', {})
    if not school_year or not rates:
        return jsonify({'error': 'school_year and rates required'}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for division, tuition in rates.items():
                try: tuition_val = float(str(tuition).replace(',','').replace('$','').strip())
                except: continue
                cur.execute("""
                    INSERT INTO fa_tuition_rates (school_year, division, tuition)
                    VALUES (%s,%s,%s) ON CONFLICT (school_year, division) DO UPDATE SET tuition=EXCLUDED.tuition
                """, (school_year, division, tuition_val))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/fa-tuition-rates/<year>', methods=['DELETE'])
@login_required
def api_fa_tuition_rates_delete_year(year):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fa_tuition_rates WHERE school_year=%s", (year,))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/icon-facebook.svg')
def icon_facebook():
    return send_from_directory('.', 'icon-facebook.svg', mimetype='image/svg+xml')

@app.route('/icon-instagram.svg')
def icon_instagram():
    return send_from_directory('.', 'icon-instagram.svg', mimetype='image/svg+xml')

@app.route('/icon-linkedin.svg')
def icon_linkedin():
    return send_from_directory('.', 'icon-linkedin.svg', mimetype='image/svg+xml')

@app.route('/icon-facebook.png')
def icon_facebook_png():
    return send_from_directory('.', 'icon-facebook.png', mimetype='image/png')

@app.route('/icon-instagram.png')
def icon_instagram_png():
    return send_from_directory('.', 'icon-instagram.png', mimetype='image/png')

@app.route('/icon-linkedin.png')
def icon_linkedin_png():
    return send_from_directory('.', 'icon-linkedin.png', mimetype='image/png')

@app.route('/signature')
@login_required
def signature_generator():
    return send_from_directory('.', 'signature_generator.html')


# ============================================
# SCHOOL CALENDAR
# ============================================

@app.route("/api/calendar/categories")
@login_required
def api_calendar_categories():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT category_id, key, label, color, sort_order, active
                FROM calendar_categories
                ORDER BY sort_order, label
            """)
            return jsonify({"categories": cur.fetchall()})
    finally:
        conn.close()


@app.route("/api/calendar/categories", methods=["POST"])
@people_required
def api_calendar_categories_create():
    data = request.get_json() or {}
    key   = (data.get("key")   or "").strip().lower().replace(" ", "_")
    label = (data.get("label") or "").strip()
    color = (data.get("color") or "#c8992a").strip()
    sort_order = int(data.get("sort_order") or 99)
    if not key or not label:
        return jsonify({"error": "key and label required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO calendar_categories (key, label, color, sort_order)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (key) DO UPDATE
                    SET label=EXCLUDED.label, color=EXCLUDED.color,
                        sort_order=EXCLUDED.sort_order, active=TRUE
                RETURNING category_id
            """, (key, label, color, sort_order))
            conn.commit()
            return jsonify({"success": True, "category_id": cur.fetchone()[0]})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/calendar/categories/<int:category_id>", methods=["PUT"])
@people_required
def api_calendar_categories_update(category_id):
    data = request.get_json() or {}
    fields, values = [], []
    for col in ("label", "color", "sort_order", "active"):
        if col in data:
            fields.append(f"{col} = %s")
            values.append(data[col])
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(category_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE calendar_categories SET {', '.join(fields)} WHERE category_id = %s",
                values
            )
            conn.commit()
            return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# SCHOOL YEAR SETTINGS  (rollover + per-year dates / trimesters)
# ============================================
@app.route("/api/school-year")
@login_required
def api_school_year():
    """Current school year + rollover + all defined years (for the calendar settings panel)."""
    rm, rd = _rollover_md()
    cur_start = current_school_year_start()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT start_year, first_day, last_day,
                       t1_start, t1_end, t2_start, t2_end, t3_start, t3_end
                FROM   school_years
                ORDER  BY start_year DESC
            """)
            rows = cur.fetchall()
    finally:
        conn.close()

    date_cols = ("first_day", "last_day", "t1_start", "t1_end",
                 "t2_start", "t2_end", "t3_start", "t3_end")

    def fmt(r):
        y = r["start_year"]
        out = {"start_year": y, "long": sy_long(y), "short": sy_short(y)}
        for k in date_cols:
            out[k] = r[k].isoformat() if r[k] else None
        return out

    return jsonify({
        "rollover": f"{rm:02d}-{rd:02d}",
        "current_start_year": cur_start,
        "current_long": sy_long(cur_start),
        "current_short": sy_short(cur_start),
        "years": [fmt(r) for r in rows],
    })


@app.route("/api/school-year/rollover", methods=["POST"])
@people_required
def api_school_year_rollover():
    import re as _re
    data = request.get_json() or {}
    raw = (data.get("rollover") or "").strip()
    if not _re.match(r"^\d{2}-\d{2}$", raw):
        return jsonify({"error": "rollover must be MM-DD"}), 400
    mm, dd = int(raw[:2]), int(raw[3:])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return jsonify({"error": "invalid month/day"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_settings (key, value, updated_by, updated_at)
                VALUES ('school_year_rollover', %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE
                    SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by,
                        updated_at=CURRENT_TIMESTAMP
            """, (f"{mm:02d}-{dd:02d}", session.get("user_email")))
            conn.commit()
        return jsonify({"success": True, "rollover": f"{mm:02d}-{dd:02d}"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/school-years", methods=["POST"])
@people_required
def api_school_years_upsert():
    """Create or update one school year's first/last day and trimester windows."""
    data = request.get_json() or {}
    try:
        start_year = int(data.get("start_year"))
    except (TypeError, ValueError):
        return jsonify({"error": "start_year (integer) required"}), 400
    if not (2000 <= start_year <= 2100):
        return jsonify({"error": "start_year out of range"}), 400

    date_cols = ("first_day", "last_day", "t1_start", "t1_end",
                 "t2_start", "t2_end", "t3_start", "t3_end")
    vals = {c: ((data.get(c) or "").strip() or None) for c in date_cols}

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO school_years (start_year, {", ".join(date_cols)}, updated_by, updated_at)
                VALUES (%s, {", ".join(["%s"] * len(date_cols))}, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (start_year) DO UPDATE SET
                    {", ".join(f"{c}=EXCLUDED.{c}" for c in date_cols)},
                    updated_by=EXCLUDED.updated_by, updated_at=CURRENT_TIMESTAMP
            """, (start_year, *[vals[c] for c in date_cols], session.get("user_email")))
            conn.commit()
        return jsonify({"success": True, "start_year": start_year})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# ROOMS + SECTIONS (classes) + ROSTERS
# A section = one teacher + room + term + roster. Homeroom/advisory sections keep
# students.homeroom_teacher_id / advisory_teacher_id synced (the "shadow column"),
# so the attendance / dismissal / bus pages keep working off those fields.
# ============================================
_SHADOW_COL = {"homeroom": "homeroom_teacher_id", "advisory": "advisory_teacher_id"}


def _sync_shadow_for_section(cur, section_id):
    """Homeroom/advisory only: set each enrolled student's shadow column to this
    section's teacher. No-op for other section types or a section with no teacher."""
    cur.execute("SELECT type, teacher_id FROM sections WHERE section_id=%s", (section_id,))
    row = cur.fetchone()
    if not row:
        return
    stype, teacher_id = row[0], row[1]
    col = _SHADOW_COL.get(stype)
    if not col or not teacher_id:
        return
    cur.execute(
        f"""UPDATE students SET {col}=%s, updated_at=NOW()
            WHERE student_id IN (SELECT student_id FROM section_enrollments WHERE section_id=%s)""",
        (teacher_id, section_id),
    )


# ---- Rooms ----
@app.route("/api/rooms")
@login_required
def api_rooms():
    active_only = request.args.get("active") in ("1", "true")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""SELECT room_id, name, active, sort_order FROM rooms
                            {'WHERE active' if active_only else ''}
                            ORDER BY sort_order, name""")
            return jsonify({"rooms": fa(cur)})
    finally:
        conn.close()


@app.route("/api/rooms", methods=["POST"])
@people_required
def api_rooms_create():
    name = (request.get_json() or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO rooms (name) VALUES (%s)
                           ON CONFLICT (name) DO UPDATE SET active=TRUE
                           RETURNING room_id""", (name,))
            conn.commit()
            return jsonify({"success": True, "room_id": cur.fetchone()[0]})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/rooms/<int:room_id>", methods=["PUT"])
@people_required
def api_rooms_update(room_id):
    data = request.get_json() or {}
    fields, vals = [], []
    for col in ("name", "active", "sort_order"):
        if col in data:
            fields.append(f"{col}=%s")
            vals.append(data[col])
    if not fields:
        return jsonify({"error": "no fields"}), 400
    vals.append(room_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE rooms SET {', '.join(fields)} WHERE room_id=%s", vals)
            conn.commit()
            return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ---- Section dropdown options (active teachers, active rooms, grades, current year) ----
@app.route("/api/sections/options")
@login_required
def api_sections_options():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT staff_id, first_name, last_name
                           FROM staff WHERE status='active'
                           ORDER BY last_name, first_name""")
            teachers = [{"staff_id": r["staff_id"],
                         "name": f'{r["first_name"]} {r["last_name"]}'.strip()} for r in fa(cur)]
            cur.execute("SELECT room_id, name FROM rooms WHERE active ORDER BY sort_order, name")
            rooms = fa(cur)
            cur.execute("""SELECT DISTINCT grade FROM students
                           WHERE status='active' AND grade IS NOT NULL AND grade<>''
                           ORDER BY grade""")
            grades = [r["grade"] for r in fa(cur)]
        return jsonify({"teachers": teachers, "rooms": rooms, "grades": grades,
                        "current_school_year_start": current_school_year_start()})
    finally:
        conn.close()


# ---- Sections ----
@app.route("/api/sections")
@login_required
def api_sections_list():
    year = request.args.get("school_year")
    year = int(year) if (year or "").isdigit() else current_school_year_start()
    stype = request.args.get("type")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            q = """SELECT s.section_id, s.school_year_start, s.type, s.name, s.subject,
                          s.grade, s.term, s.teacher_id, s.room_id, s.active,
                          (st.first_name || ' ' || st.last_name) AS teacher_name,
                          r.name AS room_name,
                          (SELECT COUNT(*) FROM section_enrollments e WHERE e.section_id=s.section_id) AS roster_count
                   FROM sections s
                   LEFT JOIN staff st ON st.staff_id = s.teacher_id
                   LEFT JOIN rooms r ON r.room_id = s.room_id
                   WHERE s.school_year_start=%s"""
            params = [year]
            if stype:
                q += " AND s.type=%s"
                params.append(stype)
            q += " ORDER BY s.type, s.name"
            cur.execute(q, params)
            return jsonify({"school_year_start": year, "sections": fa(cur)})
    finally:
        conn.close()


@app.route("/api/schedule/rosters")
@login_required
def api_schedule_rosters():
    # Read-only bundle powering the /schedule viewer: every active section for the
    # current year (with teacher/room) plus its enrolled active-student ids, and the
    # roster of active students. Lets the page resolve student<->section<->class and
    # render any student's or teacher's week in one round trip. Writes nothing.
    year = current_school_year_start()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.section_id, s.type, s.name, s.subject, s.grade,
                       s.teacher_id, (st.first_name || ' ' || st.last_name) AS teacher_name,
                       st.last_name AS teacher_last, st.status AS teacher_status, r.name AS room_name
                FROM sections s
                LEFT JOIN staff st ON st.staff_id = s.teacher_id
                LEFT JOIN rooms r ON r.room_id = s.room_id
                WHERE s.school_year_start = %s AND s.active = TRUE
                ORDER BY s.type, s.name
            """, (year,))
            sections = {row["section_id"]: dict(row, student_ids=[]) for row in fa(cur)}
            cur.execute("""
                SELECT se.section_id, se.student_id
                FROM section_enrollments se
                JOIN sections s ON s.section_id = se.section_id
                JOIN students st ON st.student_id = se.student_id
                WHERE s.school_year_start = %s AND s.active = TRUE AND st.status = 'active'
            """, (year,))
            for e in fa(cur):
                sec = sections.get(e["section_id"])
                if sec is not None:
                    sec["student_ids"].append(e["student_id"])
            cur.execute("""
                SELECT student_id, first_name, last_name, grade, cohort_color
                FROM students WHERE status = 'active'
                ORDER BY grade, last_name, first_name
            """)
            students = fa(cur)
        return jsonify({
            "school_year_start": year,
            "sections": list(sections.values()),
            "students": students,
        })
    finally:
        conn.close()


@app.route("/api/sections", methods=["POST"])
@people_required
def api_sections_create():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    year = d.get("school_year_start") or current_school_year_start()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO sections
                           (school_year_start, type, name, subject, grade, term, teacher_id, room_id, updated_by, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) RETURNING section_id""",
                        (int(year), d.get("type") or "subject", name, d.get("subject") or None,
                         d.get("grade") or None, d.get("term") or "year",
                         d.get("teacher_id") or None, d.get("room_id") or None,
                         session.get("user_email")))
            sid = cur.fetchone()[0]
            _sync_shadow_for_section(cur, sid)
            conn.commit()
            return jsonify({"success": True, "section_id": sid})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/sections/<int:section_id>", methods=["PUT"])
@people_required
def api_sections_update(section_id):
    d = request.get_json() or {}
    fields, vals = [], []
    for col in ("name", "subject", "grade", "term", "type", "teacher_id", "room_id", "active"):
        if col in d:
            fields.append(f"{col}=%s")
            vals.append(d[col] if d[col] != "" else None)
    if not fields:
        return jsonify({"error": "no fields"}), 400
    fields.append("updated_at=NOW()")
    fields.append("updated_by=%s")
    vals.append(session.get("user_email"))
    vals.append(section_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE sections SET {', '.join(fields)} WHERE section_id=%s", vals)
            # if teacher/type changed, re-sync the shadow column for the whole roster
            _sync_shadow_for_section(cur, section_id)
            conn.commit()
            return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/sections/<int:section_id>", methods=["DELETE"])
@people_required
def api_sections_delete(section_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sections WHERE section_id=%s", (section_id,))
            conn.commit()
            return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ---- Section rosters ----
@app.route("/api/sections/<int:section_id>/roster")
@login_required
def api_section_roster(section_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT s.student_id, s.first_name, s.last_name, s.grade
                           FROM section_enrollments e
                           JOIN students s ON s.student_id = e.student_id
                           WHERE e.section_id=%s
                           ORDER BY s.last_name, s.first_name""", (section_id,))
            return jsonify({"roster": fa(cur)})
    finally:
        conn.close()


@app.route("/api/sections/<int:section_id>/roster", methods=["POST"])
@people_required
def api_section_roster_add(section_id):
    ids = (request.get_json() or {}).get("student_ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "student_ids (list) required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            added = 0
            for sid in ids:
                cur.execute("""INSERT INTO section_enrollments (section_id, student_id)
                               VALUES (%s,%s) ON CONFLICT (section_id, student_id) DO NOTHING""",
                            (section_id, sid))
                added += cur.rowcount
            _sync_shadow_for_section(cur, section_id)
            conn.commit()
            return jsonify({"success": True, "added": added})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/sections/<int:section_id>/roster/<int:student_id>", methods=["DELETE"])
@people_required
def api_section_roster_remove(section_id, student_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # For homeroom/advisory, clear the student's shadow column when removed.
            cur.execute("SELECT type FROM sections WHERE section_id=%s", (section_id,))
            row = cur.fetchone()
            stype = row[0] if row else None
            cur.execute("DELETE FROM section_enrollments WHERE section_id=%s AND student_id=%s",
                        (section_id, student_id))
            col = _SHADOW_COL.get(stype)
            if col:
                cur.execute(f"UPDATE students SET {col}=NULL, updated_at=NOW() WHERE student_id=%s",
                            (student_id,))
            conn.commit()
            return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ---- Active students for roster-building (optional grade filter) ----
@app.route("/api/sections/students")
@login_required
def api_sections_students():
    grade = request.args.get("grade")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            q = """SELECT student_id, first_name, last_name, grade
                   FROM students WHERE status='active'"""
            params = []
            if grade and grade != "all":
                q += " AND grade=%s"
                params.append(grade)
            q += " ORDER BY last_name, first_name"
            cur.execute(q, params)
            return jsonify({"students": fa(cur)})
    finally:
        conn.close()


# ============================================
# In-Session Special Services Schedule
# --------------------------------------------
# A second, deliberately simple schedule for during-the-school-day support
# (OG tutoring, push-in, pull-out). Periods down, days across, exactly like the
# scheduler's master poster; each period/day cell holds any number of sessions,
# and each session is one staff member + one student + one room + one service.
# Staff only appear in the dropdown if their staff record has the "Special
# Services Role" box ticked (staff.is_special_services). The service list itself
# is managed from the Services tab on the same page.
# ============================================
SS_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
SS_PERIODS = [
    {"id": "P1", "time": "8:25–9:09"},
    {"id": "P2", "time": "9:12–9:56"},
    {"id": "P3", "time": "9:59–10:43"},
    {"id": "P4", "time": "10:46–11:35"},
    {"id": "P5", "time": "11:33–12:22"},
    {"id": "P6", "time": "12:25–1:09"},
    {"id": "P7", "time": "1:12–1:56"},
    {"id": "P8", "time": "1:59–2:43"},
]
SS_PERIOD_IDS = [pd["id"] for pd in SS_PERIODS]


def _ss_year(default=None):
    """School year for a special-services request (?school_year=2026), defaulting
    to the portal-wide current year."""
    raw = request.args.get("school_year")
    if (raw or "").isdigit():
        return int(raw)
    return default if default is not None else current_school_year_start()


def _ss_int(v):
    """Coerce a dropdown value to an int id, treating blank/none as NULL."""
    if v in (None, "", "null"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@app.route("/api/special-services/options")
@login_required
def api_ss_options():
    """Everything the page's dropdowns need, in one call."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT staff_id, first_name, last_name, title
                           FROM staff
                           WHERE status='active' AND COALESCE(is_special_services,0)=1
                           ORDER BY last_name, first_name""")
            staff = [{"staff_id": r["staff_id"],
                      "name": (r["first_name"] + " " + r["last_name"]).strip(),
                      "title": r.get("title") or ""} for r in fa(cur)]
            cur.execute("""SELECT student_id, first_name, last_name, grade
                           FROM students WHERE status='active'
                           ORDER BY last_name, first_name""")
            students = [{"student_id": r["student_id"],
                         "name": (r["first_name"] + " " + r["last_name"]).strip(),
                         "grade": r.get("grade") or ""} for r in fa(cur)]
            cur.execute("SELECT room_id, name FROM rooms WHERE active ORDER BY sort_order, name")
            rooms = fa(cur)
            cur.execute("""SELECT service_id, name FROM special_service_types
                           WHERE active ORDER BY sort_order, name""")
            services = fa(cur)
        return jsonify({
            "days": SS_DAYS,
            "periods": SS_PERIODS,
            "staff": staff,
            "students": students,
            "rooms": rooms,
            "services": services,
            "school_year_start": current_school_year_start(),
            "school_year": sy_long(current_school_year_start()),
            "can_edit": has_perm("special_services"),
        })
    finally:
        conn.close()


@app.route("/api/special-services/sessions")
@login_required
def api_ss_sessions():
    year = _ss_year()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ss.ss_id, ss.school_year_start, ss.day, ss.period, ss.notes,
                       ss.staff_id, ss.student_id, ss.room_id, ss.service_id,
                       (st.first_name || ' ' || st.last_name) AS staff_name,
                       (sd.first_name || ' ' || sd.last_name) AS student_name,
                       sd.grade AS student_grade,
                       r.name AS room_name,
                       t.name AS service_name
                FROM special_service_sessions ss
                LEFT JOIN staff    st ON st.staff_id   = ss.staff_id
                LEFT JOIN students sd ON sd.student_id = ss.student_id
                LEFT JOIN rooms    r  ON r.room_id     = ss.room_id
                LEFT JOIN special_service_types t ON t.service_id = ss.service_id
                WHERE ss.school_year_start = %s
                ORDER BY ss.period, ss.day, t.name, sd.last_name
            """, (year,))
            return jsonify({"school_year_start": year, "sessions": fa(cur)})
    finally:
        conn.close()


def _ss_validate(d):
    """Return an error string, or None if the payload's slot is valid."""
    if d.get("day") not in SS_DAYS:
        return "Pick a day."
    if d.get("period") not in SS_PERIOD_IDS:
        return "Pick a period."
    if not _ss_int(d.get("student_id")):
        return "Pick a student."
    return None


@app.route("/api/special-services/sessions", methods=["POST"])
@require_perm("special_services")
def api_ss_session_create():
    d = request.get_json() or {}
    err = _ss_validate(d)
    if err:
        return jsonify({"error": err}), 400
    year = _ss_int(d.get("school_year_start")) or current_school_year_start()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO special_service_sessions
                    (school_year_start, day, period, staff_id, student_id, room_id,
                     service_id, notes, updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING ss_id
            """, (year, d["day"], d["period"], _ss_int(d.get("staff_id")),
                  _ss_int(d.get("student_id")), _ss_int(d.get("room_id")),
                  _ss_int(d.get("service_id")), (d.get("notes") or "").strip() or None,
                  session.get("user_email")))
            ss_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success": True, "ss_id": ss_id})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/special-services/sessions/<int:ss_id>", methods=["PUT"])
@require_perm("special_services")
def api_ss_session_update(ss_id):
    d = request.get_json() or {}
    # A drag-and-drop move sends only day/period; the full form sends everything.
    if "day" in d and d["day"] not in SS_DAYS:
        return jsonify({"error": "Pick a day."}), 400
    if "period" in d and d["period"] not in SS_PERIOD_IDS:
        return jsonify({"error": "Pick a period."}), 400
    fields, vals = [], []
    for col in ("day", "period"):
        if col in d:
            fields.append(col + "=%s")
            vals.append(d[col])
    for col in ("staff_id", "student_id", "room_id", "service_id"):
        if col in d:
            fields.append(col + "=%s")
            vals.append(_ss_int(d[col]))
    if "notes" in d:
        fields.append("notes=%s")
        vals.append((d.get("notes") or "").strip() or None)
    if not fields:
        return jsonify({"error": "No changes"}), 400
    fields += ["updated_at=CURRENT_TIMESTAMP", "updated_by=%s"]
    vals.append(session.get("user_email"))
    vals.append(ss_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE special_service_sessions SET " + ", ".join(fields) +
                        " WHERE ss_id=%s", vals)
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/special-services/sessions/<int:ss_id>", methods=["DELETE"])
@require_perm("special_services")
def api_ss_session_delete(ss_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM special_service_sessions WHERE ss_id=%s", (ss_id,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ---- The service list behind the Service dropdown (Services tab) ----
@app.route("/api/special-services/types")
@login_required
def api_ss_types():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT t.service_id, t.name, t.active, t.sort_order,
                                  (SELECT COUNT(*) FROM special_service_sessions ss
                                   WHERE ss.service_id = t.service_id) AS use_count
                           FROM special_service_types t
                           ORDER BY t.sort_order, t.name""")
            return jsonify({"services": fa(cur)})
    finally:
        conn.close()


@app.route("/api/special-services/types", methods=["POST"])
@require_perm("special_services")
def api_ss_type_create():
    name = ((request.get_json() or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Re-adding a name that was retired simply brings it back.
            cur.execute("""INSERT INTO special_service_types (name, sort_order)
                           VALUES (%s, COALESCE((SELECT MAX(sort_order)+1
                                                 FROM special_service_types), 0))
                           ON CONFLICT (name) DO UPDATE SET active=TRUE
                           RETURNING service_id""", (name,))
            sid = cur.fetchone()[0]
        conn.commit()
        return jsonify({"success": True, "service_id": sid})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/special-services/types/<int:service_id>", methods=["PUT"])
@require_perm("special_services")
def api_ss_type_update(service_id):
    d = request.get_json() or {}
    fields, vals = [], []
    if "name" in d:
        nm = (d.get("name") or "").strip()
        if not nm:
            return jsonify({"error": "Name required"}), 400
        fields.append("name=%s")
        vals.append(nm)
    for col in ("active", "sort_order"):
        if col in d:
            fields.append(col + "=%s")
            vals.append(d[col])
    if not fields:
        return jsonify({"error": "No changes"}), 400
    vals.append(service_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE special_service_types SET " + ", ".join(fields) +
                        " WHERE service_id=%s", vals)
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# Gold / Blue cohorts — bulk color assignment + roster auto-populate
# --------------------------------------------
# A student carries a per-grade split color (students.cohort_color: 'gold'|'blue'|NULL).
# The "Gold & Blue" tab on the Classes page sets those colors and then bulk-fills the
# grade's SUBJECT-class rosters from them: Gold classes get the Gold kids, Blue classes
# the Blue kids, and whole-grade / ungrouped classes get everyone in the grade. The
# third math group ("… White") is left untouched. Populate fully overwrites each
# affected roster — this is a once-a-year setup action, so hand edits since the last run
# are intentionally replaced. Homeroom / advisory / elective sections are never touched.
# ============================================
def _classify_section_split(name):
    """Map a subject-section name to who it should hold:
       'gold'/'blue' -> that color; 'whole'/'plain' -> everyone in the grade;
       'white' -> skip (manual third math group)."""
    n = (name or "").strip().lower()
    if n.endswith(" white"):
        return "white"
    if n.endswith(" gold"):
        return "gold"
    if n.endswith(" blue"):
        return "blue"
    if n.endswith(" whole grade"):
        return "whole"
    return "plain"


def _save_cohort_colors(cur, grade, colors):
    """colors = {student_id: 'gold'|'blue'|falsy}. Scoped to the grade for safety."""
    for sid, color in (colors or {}).items():
        c = color if color in ("gold", "blue") else None
        cur.execute("UPDATE students SET cohort_color=%s WHERE student_id=%s AND grade=%s",
                    (c, int(sid), grade))


@app.route("/api/cohorts/<grade>")
@login_required
def api_cohorts_get(grade):
    year = current_school_year_start()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT student_id, first_name, last_name, cohort_color
                           FROM students WHERE status='active' AND grade=%s
                           ORDER BY last_name, first_name""", (grade,))
            students = fa(cur)
            cur.execute("""SELECT section_id, name FROM sections
                           WHERE school_year_start=%s AND type='subject' AND active AND grade=%s
                           ORDER BY name""", (year, grade))
            buckets = {"gold": [], "blue": [], "whole": [], "plain": [], "white": []}
            for r in fa(cur):
                buckets[_classify_section_split(r["name"])].append(r["name"])
        return jsonify({"students": students, "sections": buckets})
    finally:
        conn.close()


@app.route("/api/cohorts/<grade>", methods=["POST"])
@people_required
def api_cohorts_save(grade):
    colors = (request.get_json() or {}).get("colors") or {}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            _save_cohort_colors(cur, grade, colors)
            conn.commit()
            return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/cohorts/<grade>/populate", methods=["POST"])
@people_required
def api_cohorts_populate(grade):
    """Overwrite this grade's subject-class rosters from students' cohort_color.
       Optionally accepts fresh {colors} and saves them first. White sections are skipped."""
    colors = (request.get_json() or {}).get("colors")
    year = current_school_year_start()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if colors:
                _save_cohort_colors(cur, grade, colors)
            cur.execute("""SELECT student_id, cohort_color FROM students
                           WHERE status='active' AND grade=%s""", (grade,))
            studs = fa(cur)
            all_ids = [s["student_id"] for s in studs]
            gold_ids = [s["student_id"] for s in studs if s["cohort_color"] == "gold"]
            blue_ids = [s["student_id"] for s in studs if s["cohort_color"] == "blue"]
            cur.execute("""SELECT section_id, name FROM sections
                           WHERE school_year_start=%s AND type='subject' AND active AND grade=%s""",
                        (year, grade))
            report = {"sections_updated": 0, "skipped_white": 0, "placements": 0}
            for sec in fa(cur):
                kind = _classify_section_split(sec["name"])
                if kind == "white":
                    report["skipped_white"] += 1
                    continue
                target = gold_ids if kind == "gold" else blue_ids if kind == "blue" else all_ids
                cur.execute("DELETE FROM section_enrollments WHERE section_id=%s", (sec["section_id"],))
                for st in target:
                    cur.execute("""INSERT INTO section_enrollments (section_id, student_id)
                                   VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                                (sec["section_id"], st))
                report["sections_updated"] += 1
                report["placements"] += len(target)
            conn.commit()
            return jsonify({"success": True, **report})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# Scheduler — server-side state for the master-schedule generator.
# One JSON blob per school year (inputs + generated grid + manual edits).
# ============================================
@app.route("/api/scheduler")
@login_required
def api_scheduler_get():
    # Any signed-in staff can read the schedule; can_edit tells the page whether to allow saving.
    year = current_school_year_start()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT data, updated_at, updated_by, version FROM scheduler_state WHERE school_year_start=%s", (year,))
            row = cur.fetchone()
        data = None
        if row and row[0]:
            try:
                data = json.loads(row[0])
            except Exception:
                data = None
        return jsonify({"school_year_start": year, "data": data,
                        "can_edit": has_perm("scheduler"),
                        "version": (row[3] if row and row[3] is not None else 0),
                        "updated_at": (row[1].isoformat() if row and row[1] else None) if row else None,
                        "updated_by": row[2] if row else None})
    finally:
        conn.close()


@app.route("/api/scheduler", methods=["POST"])
@require_perm("scheduler")
def api_scheduler_save():
    body = request.get_json(silent=True) or {}
    data = body.get("data")
    if data is None:
        return jsonify({"error": "data required"}), 400
    base_version = body.get("base_version")
    year = current_school_year_start()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Lock the row for this year so concurrent saves serialise, then check the
            # caller's base_version against what's stored. If the caller loaded an older
            # version (someone else saved in between), reject instead of clobbering.
            cur.execute("SELECT version, updated_by, updated_at FROM scheduler_state WHERE school_year_start=%s FOR UPDATE", (year,))
            row = cur.fetchone()
            cur_version = row[0] if row and row[0] is not None else 0
            if row is not None and base_version is not None and int(base_version) != cur_version:
                conn.rollback()
                return jsonify({"error": "conflict", "current_version": cur_version,
                                "updated_by": row[1],
                                "updated_at": (row[2].isoformat() if row[2] else None)}), 409
            new_version = cur_version + 1
            email = session.get("user_email")
            if row is not None:
                cur.execute("""UPDATE scheduler_state SET data=%s, updated_at=NOW(), updated_by=%s, version=%s
                               WHERE school_year_start=%s""",
                            (json.dumps(data), email, new_version, year))
            else:
                cur.execute("""INSERT INTO scheduler_state (school_year_start, data, updated_at, updated_by, version)
                               VALUES (%s,%s,NOW(),%s,%s)""",
                            (year, json.dumps(data), email, new_version))
            conn.commit()
        return jsonify({"success": True, "version": new_version})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()




# ============================================
# REPORT CARDS — Phase 1: template config (grading scales + editable templates)
# A template's whole structure lives in a JSON `structure` column so it stays
# editable as the drafts get feedback, without schema changes. Two layouts:
#   "checklist"    — sections of skill rows, one mark per trimester (skills/standards scales)
#   "departmental" — subject rows with letter grades + a per-subject behavior matrix (middle school)
# ============================================
REPORT_SCALES = {
    "skills_dev": {"name": "Skills — PreK & Kindergarten", "levels": [
        {"code": "N", "label": "Not introduced", "desc": "skill not yet introduced"},
        {"code": "I", "label": "Introduced", "desc": "skill introduced, not yet developing"},
        {"code": "D", "label": "Developing", "desc": "beginning understanding"},
        {"code": "P", "label": "Practicing", "desc": "growing understanding"},
        {"code": "S", "label": "Secure", "desc": "grade-level understanding"},
        {"code": "E", "label": "Enriched", "desc": "enriched understanding"},
    ]},
    "standards_4": {"name": "Standards 1–4", "levels": [
        {"code": "1", "label": "Does Not Meet Standards"},
        {"code": "2", "label": "Approaches Standards"},
        {"code": "3", "label": "Meets Standards"},
        {"code": "4", "label": "Exceeds Standards"},
    ]},
    "letter": {"name": "Letter Grades", "levels": [
        {"code": "A+", "range": "97+"}, {"code": "A", "range": "93–96"}, {"code": "A-", "range": "90–92"},
        {"code": "B+", "range": "87–89"}, {"code": "B", "range": "83–86"}, {"code": "B-", "range": "80–82"},
        {"code": "C+", "range": "77–79"}, {"code": "C", "range": "73–76"}, {"code": "C-", "range": "70–72"},
        {"code": "D", "range": "65–69"}, {"code": "F", "range": "0–64"},
    ]},
    "behavior_4": {"name": "Behavior 4–1", "levels": [
        {"code": "4", "label": "Always"}, {"code": "3", "label": "Usually"},
        {"code": "2", "label": "Sometimes"}, {"code": "1", "label": "Rarely"},
    ]},
}

REPORT_TEMPLATE_SEEDS = [
    {
        "key": "kindergarten_skills", "name": "Kindergarten Skills Report",
        "grades": ["K"], "layout": "checklist",
        "structure": {"sections": [
            {"title": "Math Skills", "kind": "skills", "scale": "skills_dev", "groups": [
                {"heading": "Numeration", "rows": ["Demonstrates oral counting skills", "Counts using 1:1 correspondence", "Identifies and names numbers", "Demonstrates understanding of place value", "Identifies fractional parts", "Reads and represents data in graphs"]},
                {"heading": "Operations", "rows": ["Solves addition problems", "Solves subtraction problems", "Solves story problems"]},
                {"heading": "Measurement, Money, and Time", "rows": ["Demonstrates measurement skills", "Identifies and counts coins", "Reads calendar", "Reads clock"]},
                {"heading": "Geometry / Attributes", "rows": ["Demonstrates a growing understanding of shapes", "Demonstrates patterning skills"]},
            ]},
            {"title": "Learning Behaviors / Social Skills", "kind": "skills", "scale": "skills_dev", "rows": ["Follows classroom routines", "Transitions efficiently between learning activities", "Able to attend during instruction", "Follows visual and verbal directions", "Completes independent work", "Listens to and is attentive to peers", "Participates during group instruction", "Works cooperatively with others", "Solves problems with peers"]},
            {"title": "Reading Skills", "kind": "skills", "scale": "skills_dev", "rows": ["Identifies upper / lower case letters", "Knows letter sounds", "Demonstrates phonemic awareness skills", "Reads basic sight vocabulary", "Blends letters into words", "Attends to text when reading", "Uses a variety of decoding skills"]},
            {"title": "Writing Skills", "kind": "skills", "scale": "skills_dev", "rows": ["Generates / develops writing ideas", "Represents ideas using pictures", "Writes complete sentences", "Writes using invented spelling", "Uses conventional spelling for sight words", "Applies writing conventions correctly"]},
            {"title": "Handwriting Skills", "kind": "skills", "scale": "skills_dev", "rows": ["Forms letters and numbers correctly", "Fine motor development"]},
            {"title": "Science / Social Studies", "kind": "skills", "scale": "skills_dev", "rows": ["Shows interest in subject matter", "Follows activity directions", "Connects activities and concepts"]},
            {"title": "Attendance", "kind": "attendance", "fields": ["Absences", "Days Late"]},
        ]},
    },
    {
        "key": "third_grade", "name": "Third Grade Report Card",
        "grades": ["3"], "layout": "checklist",
        "structure": {"sections": [
            {"title": "Academic Development", "kind": "skills", "scale": "standards_4", "rows": ["Follows directions", "Works well independently", "Is neat and organized", "Listens attentively", "Completes homework", "Displays effort", "Focuses on and completes the task at hand", "Works well in groups", "Participates in class"]},
            {"title": "Personal Development", "kind": "skills", "scale": "standards_4", "rows": ["Follows the rules", "Exercises self-control", "Cooperates with others", "Has a positive attitude", "Shows respect"]},
            {"title": "Reading", "kind": "skills", "scale": "standards_4", "rows": ["Reading on grade level", "Applies reading strategies", "Comprehends material", "Reads with fluency", "Recalls sight words", "Self-corrects", "Displays an understanding of vocabulary in text"]},
            {"title": "Spelling", "kind": "skills", "scale": "standards_4", "rows": ["Consistently spells grade level words", "Applies spelling patterns", "Uses a variety of strategies to spell words correctly"]},
            {"title": "Grammar", "kind": "skills", "scale": "standards_4", "rows": ["Comprehends terminology and concepts", "Applies concepts"]},
            {"title": "Social Studies", "kind": "skills", "scale": "standards_4", "rows": ["Recognizes the importance of traditions, values and beliefs in the countries that we study", "Uses map skills to locate landmarks and geographic features", "Demonstrates understanding of concepts"]},
            {"title": "Writing Skills", "kind": "skills", "scale": "standards_4", "rows": ["Forms letters correctly", "Spaces words correctly", "Applies the rules of capitalization", "Uses punctuation correctly", "Writes using complete sentences", "Arranges ideas in a logical order", "Adds details", "Writes independently", "Follows the steps in the writing process", "Writes neatly and takes care with presentation"]},
            {"title": "Math", "kind": "skills", "scale": "standards_4", "groups": [
                {"heading": "", "rows": ["Basic knowledge of math facts (addition/subtraction)", "Multiplication", "Division", "Completes class work independently", "Uses a variety of problem-solving techniques"]},
                {"heading": "Understands and applies concepts in", "rows": ["Word Problems", "Money", "Fractions", "Time", "Measurement / Data", "Geometry"]},
            ]},
            {"title": "Science", "kind": "skills", "scale": "standards_4", "rows": ["Asks questions, analyzes and makes observations", "Applies vocabulary to explain understanding of content", "Demonstrates understanding of concepts"]},
            {"title": "Attendance", "kind": "attendance", "fields": ["Absences", "Lates", "Early Dismissals"]},
        ]},
    },
    {
        "key": "middle_school", "name": "Middle School Report Card",
        "grades": ["5", "6", "7", "8"], "layout": "departmental",
        "structure": {"sections": [
            {"title": "Subjects", "kind": "subjects", "scale": "letter",
             "columns": ["Trimester 1", "Trimester 2", "Trimester 3", "Final Exam", "Final Average"],
             "note": "Subjects come from the student's scheduled classes; this list is the default order.",
             "rows": ["English Language Arts", "Math", "Social Studies", "World Language", "Science"]},
            {"title": "Behaviors that Support Learning", "kind": "behavior_matrix", "scale": "behavior_4",
             "note": "Marked per subject, per trimester.",
             "rows": ["Comes to class prepared", "Organizes self and materials", "Exhibits positive attitudes", "Seeks help when appropriate", "Accepts constructive feedback", "Maintains focus during class", "Follows classroom rules", "Puts forth effort", "Manages time efficiently", "Follows directions", "Completes work with care", "Participates in class discussions"]},
            {"title": "Attendance", "kind": "attendance", "fields": ["Absences", "Lates", "Early Dismissals"]},
        ]},
    },
    {
        "key": "junior_prek", "name": "Junior Pre-Kindergarten Skills Report",
        "grades": ["JPK"], "layout": "checklist",
        "structure": {"sections": [
            {"title": "Learning Behaviors / Social Skills", "kind": "skills", "scale": "skills_dev", "rows": ["Uses appropriate body space", "Engages in cooperative play", "Verbally expresses needs and desires", "Accepts responsibility for actions", "Handles transitions from one activity to another", "Listens to and is attentive to speaker", "Follows 1-2 step oral directions", "Can sit for an appropriate length of time", "Exhibits control of impulses", "Follows safety rules"]},
            {"title": "Readiness Skills", "kind": "skills", "scale": "skills_dev", "rows": ["Grips writing tools correctly", "Uses scissors correctly", "Demonstrates appropriate fine motor skills", "Uses the bathroom independently", "Puts on coat and outdoor clothing independently"]},
            {"title": "Language Arts", "kind": "skills", "scale": "skills_dev", "rows": ["Recognizes first name", "Can recite the alphabet", "Identifies uppercase letters", "Knows letter sound association", "Beginning to rhyme in context", "Participates in stories, songs and poems", "Recognizes colors"]},
            {"title": "Math", "kind": "skills", "scale": "skills_dev", "rows": ["Counts to:", "Counts using 1:1 correspondence", "Identifies and names numbers 1-5", "Recognizes sets of objects 1-5", "Recognizes and labels shapes", "Sorts and classifies objects"]},
            {"title": "Science / Social Studies", "kind": "skills", "scale": "skills_dev", "rows": ["Makes connections between activity and concepts", "Participates in group activities and discussions"]},
            {"title": "Attendance", "kind": "attendance", "fields": ["Absences", "Days Late"]},
        ]},
    },
    {
        "key": "senior_prek", "name": "Senior Pre-Kindergarten Skills Report",
        "grades": ["SPK"], "layout": "checklist",
        "structure": {"sections": [
            {"title": "Learning Behaviors / Social Skills", "kind": "skills", "scale": "skills_dev", "rows": ["Uses appropriate body space", "Engages in cooperative play", "Verbally expresses needs and desires", "Accepts responsibility for actions", "Handles transitions from one activity to another", "Listens to and is attentive to speaker", "Follows 2-3 step oral directions", "Can sit for appropriate length of time", "Exhibits control of impulses", "Follows safety rules"]},
            {"title": "Readiness Skills", "kind": "skills", "scale": "skills_dev", "rows": ["Grips writing tools correctly", "Uses scissors correctly", "Demonstrates appropriate fine motor skills", "Puts on coat and outdoor clothing independently"]},
            {"title": "Language Arts", "kind": "skills", "scale": "skills_dev", "rows": ["Recognizes first name", "Can recite the alphabet", "Identifies uppercase letters", "Identifies lowercase letters", "Knows letter sound association", "Responds to journal prompts successfully", "Beginning to rhyme in context", "Participates in stories, songs and poems", "Recognizes colors"]},
            {"title": "Math", "kind": "skills", "scale": "skills_dev", "rows": ["Counts to:", "Counts using 1:1 correspondence", "Identifies and names numbers 1-10", "Recognizes sets of objects 1-10", "Recognizes and labels shapes", "Sorts and classifies objects"]},
            {"title": "Science / Social Studies", "kind": "skills", "scale": "skills_dev", "rows": ["Makes connections between activity and concepts", "Participates in group activities and discussions"]},
            {"title": "Attendance", "kind": "attendance", "fields": ["Absences", "Days Late"]},
        ]},
    },
    {
        "key": "first_grade", "name": "First Grade Report Card",
        "grades": ["1"], "layout": "checklist",
        "structure": {"sections": [
            {"title": "Academic Development", "kind": "skills", "scale": "standards_4", "rows": ["Follows directions", "Works well independently", "Is neat and organized", "Listens attentively", "Completes homework", "Displays effort", "Focuses on and completes the task at hand", "Works well in groups", "Participates in class"]},
            {"title": "Personal Development", "kind": "skills", "scale": "standards_4", "rows": ["Follows the rules", "Exercises self-control", "Cooperates with others", "Has a positive attitude", "Shows respect"]},
            {"title": "Reading", "kind": "skills", "scale": "standards_4", "rows": ["Reading on grade level", "Applies reading strategies", "Comprehends material", "Reads with fluency", "Recalls sight words", "Self-corrects", "Displays an understanding of vocabulary in text"]},
            {"title": "Spelling", "kind": "skills", "scale": "standards_4", "rows": ["Consistently spells grade level words", "Applies spelling patterns", "Uses a variety of strategies to spell words correctly"]},
            {"title": "Science", "kind": "skills", "scale": "standards_4", "rows": ["Comprehends terminology and concepts"]},
            {"title": "Social Studies", "kind": "skills", "scale": "standards_4", "rows": ["Comprehends terminology and concepts"]},
            {"title": "Writing Skills", "kind": "skills", "scale": "standards_4", "rows": ["Forms letters correctly", "Spaces words correctly", "Applies the rules of capitalization", "Uses punctuation correctly", "Writes using complete sentences", "Arranges ideas in a logical order", "Adds details", "Writes independently", "Follows the steps in the writing process", "Writes neatly and takes care with presentation"]},
            {"title": "Math", "kind": "skills", "scale": "standards_4", "groups": [
                {"heading": "", "rows": ["Uses a variety of problem-solving techniques", "Completes class work independently"]},
                {"heading": "Understands and applies concepts in", "rows": ["Addition", "Subtraction", "Place Value", "Double digit addition", "Double digit subtraction", "Data and Graphs", "Measurement", "Time", "Geometry/Fractions"]},
            ]},
            {"title": "Attendance", "kind": "attendance", "fields": ["Absences", "Lates", "Early Dismissals"]},
        ]},
    },
    {
        "key": "second_grade", "name": "Second Grade Report Card",
        "grades": ["2"], "layout": "checklist",
        "structure": {"sections": [
            {"title": "Academic Development", "kind": "skills", "scale": "standards_4", "rows": ["Follows directions", "Works well independently", "Is neat and organized", "Listens attentively", "Completes homework", "Displays effort", "Focuses on and completes the task at hand", "Works well in groups", "Participates in class"]},
            {"title": "Personal Development", "kind": "skills", "scale": "standards_4", "rows": ["Follows the rules", "Exercises self-control", "Cooperates with others", "Has a positive attitude", "Shows respect"]},
            {"title": "Reading", "kind": "skills", "scale": "standards_4", "rows": ["Reading on grade level", "Applies reading strategies", "Comprehends material", "Reads with fluency", "Recalls sight words", "Self-corrects", "Displays an understanding of vocabulary in text"]},
            {"title": "Spelling", "kind": "skills", "scale": "standards_4", "rows": ["Consistently spells grade level words", "Applies spelling patterns", "Uses a variety of strategies to spell words correctly"]},
            {"title": "Science", "kind": "skills", "scale": "standards_4", "rows": ["Comprehends terminology and concepts"]},
            {"title": "Social Studies", "kind": "skills", "scale": "standards_4", "rows": ["Comprehends terminology and concepts"]},
            {"title": "Writing Skills", "kind": "skills", "scale": "standards_4", "rows": ["Forms letters correctly", "Spaces words correctly", "Applies the rules of capitalization", "Uses punctuation correctly", "Writes using complete sentences", "Arranges ideas in a logical order", "Adds details", "Writes independently", "Follows the steps in the writing process", "Writes neatly and takes care with presentation"]},
            {"title": "Math", "kind": "skills", "scale": "standards_4", "groups": [
                {"heading": "", "rows": ["Uses a variety of problem-solving techniques", "Completes class work independently"]},
                {"heading": "Understands and applies concepts in", "rows": ["Addition", "Subtraction", "Number Patterns", "Place Value", "Money", "Data & Graphs", "Time", "Measurement", "Geometry/Fractions"]},
            ]},
            {"title": "Attendance", "kind": "attendance", "fields": ["Absences", "Lates", "Early Dismissals"]},
        ]},
    },
    {
        "key": "fourth_grade", "name": "Fourth Grade Report Card",
        "grades": ["4"], "layout": "checklist",
        "structure": {"sections": [
            {"title": "Behaviors That Support Learning", "kind": "skills", "scale": "standards_4", "rows": ["Comes to class prepared", "Organizes self and materials", "Exhibits positive attitudes", "Seeks help when appropriate", "Accepts suggestions and corrections", "Maintains focus during class", "Follows school rules", "Puts forth effort", "Manages time and projects effectively", "Follows directions", "Completes work with care", "Participates in class discussions"]},
            {"title": "Reading", "kind": "skills", "scale": "standards_4", "rows": ["Reading on grade level", "Applies reading strategies", "Comprehends material", "Reads with fluency"]},
            {"title": "Spelling", "kind": "skills", "scale": "standards_4", "rows": ["Consistently spells grade level words", "Uses a variety of strategies to spell words correctly"]},
            {"title": "Grammar", "kind": "skills", "scale": "standards_4", "rows": ["Comprehends concepts", "Applies concepts"]},
            {"title": "Writing Skills", "kind": "skills", "scale": "standards_4", "rows": ["Applies the rules of capitalization", "Uses punctuation correctly", "Writes using complete sentences", "Arranges ideas in a logical order", "Adds details", "Writes independently", "Follows the steps in the writing process"]},
            {"title": "Subject Grades", "kind": "subjects", "scale": "letter",
             "columns": ["Trimester 1", "Trimester 2", "Trimester 3"],
             "note": "Overall letter grade per subject.",
             "rows": ["Grammar", "Morphology/Vocabulary", "Spelling", "Writing", "Science", "Math", "Social Studies"]},
            {"title": "Attendance", "kind": "attendance", "fields": ["Absences", "Lates", "Early Dismissals"]},
        ]},
    },
]


def _seed_report_cards(cur):
    """Create report-card config tables + seed grading scales and starter templates.
    Idempotent: ON CONFLICT DO NOTHING, so admin edits made later are never clobbered."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS report_grading_scales (
            scale_id   SERIAL PRIMARY KEY,
            key        TEXT NOT NULL UNIQUE,
            name       TEXT NOT NULL,
            levels     TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS report_templates (
            template_id       SERIAL PRIMARY KEY,
            key               TEXT NOT NULL UNIQUE,
            name              TEXT NOT NULL,
            grades            TEXT NOT NULL DEFAULT '[]',
            layout            TEXT NOT NULL DEFAULT 'checklist',
            structure         TEXT NOT NULL DEFAULT '{"sections":[]}',
            active            BOOLEAN NOT NULL DEFAULT TRUE,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by        TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS report_entries (
            entry_id          SERIAL PRIMARY KEY,
            student_id        INTEGER NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
            template_id       INTEGER NOT NULL REFERENCES report_templates(template_id),
            school_year_start INTEGER NOT NULL,
            term              TEXT NOT NULL,
            data              TEXT NOT NULL DEFAULT '{}',
            status            TEXT NOT NULL DEFAULT 'draft',
            updated_by        TEXT,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, template_id, school_year_start, term)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_report_entries_lookup ON report_entries(school_year_start, term)")
    for key, sc in REPORT_SCALES.items():
        cur.execute("INSERT INTO report_grading_scales (key, name, levels) VALUES (%s,%s,%s) ON CONFLICT (key) DO NOTHING",
                    (key, sc["name"], json.dumps(sc["levels"])))
    for t in REPORT_TEMPLATE_SEEDS:
        cur.execute("""INSERT INTO report_templates (key, name, grades, layout, structure)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (key) DO NOTHING""",
                    (t["key"], t["name"], json.dumps(t["grades"]), t["layout"], json.dumps(t["structure"])))


@app.route("/report-card-templates")
@require_perm("report_card_templates")
def report_card_templates_page():
    return send_from_directory(".", "report_card_templates.html")


@app.route("/api/report/scales")
@login_required
def api_report_scales():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT key, name, levels FROM report_grading_scales ORDER BY name")
            out = []
            for r in fa(cur):
                out.append({"key": r["key"], "name": r["name"], "levels": json.loads(r["levels"] or "[]")})
            return jsonify({"scales": out})
    finally:
        conn.close()


@app.route("/api/report/templates")
@login_required
def api_report_templates_list():
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT template_id, key, name, grades, layout, active FROM report_templates ORDER BY name")
            out = []
            for r in fa(cur):
                r = dict(r)
                r["grades"] = json.loads(r["grades"] or "[]")
                out.append(r)
            return jsonify({"templates": out})
    finally:
        conn.close()


@app.route("/api/report/templates/<int:template_id>")
@login_required
def api_report_template_get(template_id):
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT template_id, key, name, grades, layout, structure, active FROM report_templates WHERE template_id=%s", (template_id,))
            r = fo(cur)
            if not r:
                return jsonify({"error": "not found"}), 404
            r["grades"] = json.loads(r["grades"] or "[]")
            r["structure"] = json.loads(r["structure"] or '{"sections":[]}')
            return jsonify(r)
    finally:
        conn.close()


@app.route("/api/report/templates", methods=["POST"])
@require_perm("report_card_templates")
def api_report_template_create():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    key = (d.get("key") or name).strip().lower().replace(" ", "_")
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO report_templates (key, name, grades, layout, structure, updated_by, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,NOW()) RETURNING template_id""",
                        (key, name, json.dumps(d.get("grades") or []), d.get("layout") or "checklist",
                         json.dumps(d.get("structure") or {"sections": []}), session.get("user_email")))
            tid = cur.fetchone()[0]
            conn.commit()
            return jsonify({"success": True, "template_id": tid})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/report/templates/<int:template_id>", methods=["PUT"])
@require_perm("report_card_templates")
def api_report_template_update(template_id):
    d = request.get_json() or {}
    sets, vals = [], []
    if "name" in d:
        sets.append("name=%s"); vals.append((d["name"] or "").strip())
    if "grades" in d:
        sets.append("grades=%s"); vals.append(json.dumps(d["grades"] or []))
    if "layout" in d:
        sets.append("layout=%s"); vals.append(d["layout"] or "checklist")
    if "structure" in d:
        sets.append("structure=%s"); vals.append(json.dumps(d["structure"] or {"sections": []}))
    if "active" in d:
        sets.append("active=%s"); vals.append(bool(d["active"]))
    if not sets:
        return jsonify({"error": "no fields"}), 400
    sets.append("updated_at=NOW()"); sets.append("updated_by=%s"); vals.append(session.get("user_email"))
    vals.append(template_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE report_templates SET {', '.join(sets)} WHERE template_id=%s", vals)
            conn.commit()
            return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ============================================
# REPORT CARDS — TEACHER ENTRY (Phase 2)
# ============================================
# A student's report card = the active template whose `grades` list contains the
# student's grade. Homeroom teachers fill the whole card for each homeroom student
# (decided with Colin, 2026-08). Admins (superadmin / can_manage_people) may fill
# for anyone. One report_entries row per (student, template, school_year, term);
# all marks + comments live in its `data` JSON, keyed by section/row index so it
# maps straight back onto the template structure.

def _report_current_term(year=None):
    from datetime import date as _d
    wins = get_trimester_windows(year)
    today = _d.today().isoformat()
    for k, _l, st, en in wins:
        if st <= today <= en:
            return k
    if today < wins[0][2]:
        return "t1"
    return "t3"

def _report_term_label(term):
    return {"t1": "Trimester 1", "t2": "Trimester 2", "t3": "Trimester 3"}.get(term, term)

def _active_templates(cur):
    cur.execute("SELECT template_id, name, layout, grades FROM report_templates WHERE active=TRUE ORDER BY name")
    out = []
    for r in fa(cur):
        out.append({"template_id": r["template_id"], "name": r["name"], "layout": r["layout"],
                    "grades": [str(x).strip().lower() for x in json.loads(r["grades"] or "[]")]})
    return out

def _match_template(templates, grade):
    g = (grade or "").strip().lower()
    if not g:
        return None
    for t in templates:
        if g in t["grades"]:
            return t
    return None

def _report_can_edit_student(cur, email, student_id):
    """Homeroom teacher of the student, or an admin, may edit."""
    if session.get("is_superadmin") or session.get("can_manage_people"):
        return True
    staff = _staff_row_for_email(cur, email)
    if not staff:
        return False
    cur.execute("SELECT homeroom_teacher_id FROM students WHERE student_id=%s", (student_id,))
    row = fo(cur)
    return bool(row and row.get("homeroom_teacher_id") == staff["staff_id"])


def _student_term_attendance(cur, student_id, start_iso, end_iso):
    """Unexcused absent/tardy/ED counts for one student in a date window, from the
    General Attendance program — same rule as the homeroom attendance report
    (excused absences/tardies are deliberately NOT counted, per Karin)."""
    cur.execute("SELECT program_id FROM programs WHERE program_name='General Attendance' AND status='active' LIMIT 1")
    prog = fo(cur)
    if not prog:
        return {"absent": 0, "tardy": 0, "ed": 0}
    cur.execute("""
        SELECT a.status, COUNT(*) AS n
        FROM attendance_records a
        JOIN enrollments e ON a.enrollment_id = e.enrollment_id
        WHERE e.program_id = %s AND e.student_id = %s
          AND a.attendance_date BETWEEN %s AND %s
          AND a.status IN ('absent', 'tardy', 'ed')
        GROUP BY a.status
    """, (prog["program_id"], student_id, start_iso, end_iso))
    out = {"absent": 0, "tardy": 0, "ed": 0}
    for r in fa(cur):
        out[r["status"]] = r["n"]
    return out


def _attendance_auto_for_template(cur, student_id, structure, term, year):
    """Live count for each attendance-section field in a template, for one term.
    Maps field labels (Absences / Lates / Days Late / Early Dismissals) onto the
    unexcused tally so the report card's attendance block fills itself in."""
    wins = {k: (st, en) for k, _l, st, en in get_trimester_windows(year)}
    if term not in wins:
        return {}
    st, en = wins[term]
    counts = _student_term_attendance(cur, student_id, st, en)
    def val_for(label):
        t = (label or "").lower()
        if "absen" in t: return counts["absent"]
        if "late" in t or "tard" in t: return counts["tardy"]
        if "early" in t or "dismiss" in t: return counts["ed"]
        return None
    auto = {}
    for sec in (structure.get("sections") or []):
        if sec.get("kind") == "attendance":
            for f in (sec.get("fields") or []):
                v = val_for(f)
                if v is not None:
                    auto[f] = v
    return auto


@app.route("/report-cards")
@login_required
def report_cards_entry_page():
    return send_from_directory(".", "report_card_entry.html")


@app.route("/api/report/entry/context")
@login_required
def api_report_entry_context():
    email = session.get("user_email")
    year = current_school_year_start()
    is_admin = bool(session.get("is_superadmin") or session.get("can_manage_people"))
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            staff = _staff_row_for_email(cur, email)
            teacher = None
            if staff:
                teacher = {"staff_id": staff["staff_id"], "name": f'{staff["first_name"]} {staff["last_name"]}'}
            homerooms = []
            if is_admin:
                cur.execute("""
                    SELECT st.staff_id, st.first_name, st.last_name, COUNT(s.student_id) AS n
                    FROM staff st JOIN students s ON s.homeroom_teacher_id = st.staff_id AND s.status='active'
                    GROUP BY st.staff_id, st.first_name, st.last_name
                    ORDER BY st.last_name, st.first_name
                """)
                homerooms = [{"staff_id": r["staff_id"], "name": f'{r["first_name"]} {r["last_name"]}', "count": r["n"]} for r in fa(cur)]
            cur_term = _report_current_term(year)
            terms = [{"key": k, "label": l, "current": (k == cur_term)} for k, l, st, en in get_trimester_windows(year)]
            return jsonify({
                "school_year": sy_long(year),
                "is_admin": is_admin,
                "teacher": teacher,
                "homerooms": homerooms,
                "terms": terms,
                "current_term": cur_term,
            })
    finally:
        conn.close()


@app.route("/api/report/entry/roster")
@login_required
def api_report_entry_roster():
    email = session.get("user_email")
    year = current_school_year_start()
    term = (request.args.get("term") or _report_current_term(year)).strip()
    is_admin = bool(session.get("is_superadmin") or session.get("can_manage_people"))
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            staff = _staff_row_for_email(cur, email)
            teacher_id = request.args.get("teacher_id", type=int)
            if teacher_id and not is_admin:
                teacher_id = staff["staff_id"] if staff else None
            if not teacher_id:
                teacher_id = staff["staff_id"] if staff else None
            if not teacher_id:
                return jsonify({"error": "No homeroom is linked to your account. An admin can pick a homeroom to enter for."}), 404
            templates = _active_templates(cur)
            cur.execute("""
                SELECT student_id, first_name, last_name, grade
                FROM students WHERE homeroom_teacher_id=%s AND status='active'
                ORDER BY last_name, first_name
            """, (teacher_id,))
            students = fa(cur)
            cur.execute("""
                SELECT e.student_id, e.template_id, e.status
                FROM report_entries e JOIN students s ON s.student_id=e.student_id
                WHERE s.homeroom_teacher_id=%s AND e.school_year_start=%s AND e.term=%s
            """, (teacher_id, year, term))
            emap = {r["student_id"]: r for r in fa(cur)}
            out = []
            for stu in students:
                tpl = _match_template(templates, stu.get("grade"))
                ent = emap.get(stu["student_id"])
                out.append({
                    "student_id": stu["student_id"],
                    "name": f'{stu["first_name"]} {stu["last_name"]}',
                    "grade": stu.get("grade") or "",
                    "template_id": tpl["template_id"] if tpl else None,
                    "template_name": tpl["name"] if tpl else None,
                    "status": (ent["status"] if ent else "none"),
                })
            cur.execute("SELECT first_name, last_name FROM staff WHERE staff_id=%s", (teacher_id,))
            tr = fo(cur) or {}
            return jsonify({
                "term": term, "term_label": _report_term_label(term),
                "school_year": sy_long(year),
                "teacher": {"staff_id": teacher_id, "name": f'{tr.get("first_name","")} {tr.get("last_name","")}'.strip()},
                "students": out,
            })
    finally:
        conn.close()


@app.route("/api/report/entry/student")
@login_required
def api_report_entry_student():
    email = session.get("user_email")
    year = current_school_year_start()
    student_id = request.args.get("student_id", type=int)
    term = (request.args.get("term") or _report_current_term(year)).strip()
    if not student_id:
        return jsonify({"error": "student_id required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT student_id, first_name, last_name, grade FROM students WHERE student_id=%s", (student_id,))
            stu = fo(cur)
            if not stu:
                return jsonify({"error": "student not found"}), 404
            can_edit = _report_can_edit_student(cur, email, student_id)
            templates = _active_templates(cur)
            tpl = _match_template(templates, stu.get("grade"))
            if not tpl:
                return jsonify({"error": 'No report card template exists yet for grade "%s".' % (stu.get("grade") or "?")}), 404
            cur.execute("SELECT template_id, name, layout, structure FROM report_templates WHERE template_id=%s", (tpl["template_id"],))
            trow = fo(cur)
            structure = json.loads(trow["structure"] or '{"sections":[]}')
            cur.execute("SELECT key, name, levels FROM report_grading_scales")
            scales = {r["key"]: {"name": r["name"], "levels": json.loads(r["levels"] or "[]")} for r in fa(cur)}
            cur.execute("""SELECT data, status FROM report_entries
                           WHERE student_id=%s AND template_id=%s AND school_year_start=%s AND term=%s""",
                        (student_id, trow["template_id"], year, term))
            ent = fo(cur)
            attendance_auto = _attendance_auto_for_template(cur, student_id, structure, term, year)
            return jsonify({
                "student": {"student_id": stu["student_id"], "name": f'{stu["first_name"]} {stu["last_name"]}', "grade": stu.get("grade") or ""},
                "term": term, "term_label": _report_term_label(term), "school_year": sy_long(year),
                "template": {"template_id": trow["template_id"], "name": trow["name"], "layout": trow["layout"], "structure": structure},
                "scales": scales,
                "entry": {"data": (json.loads(ent["data"] or "{}") if ent else {}), "status": (ent["status"] if ent else "none")},
                "attendance_auto": attendance_auto,
                "can_edit": can_edit,
            })
    finally:
        conn.close()


@app.route("/api/report/entry/save", methods=["POST"])
@login_required
def api_report_entry_save():
    email = session.get("user_email")
    year = current_school_year_start()
    d = request.get_json() or {}
    student_id = d.get("student_id")
    template_id = d.get("template_id")
    term = (d.get("term") or "").strip()
    data = d.get("data")
    status = (d.get("status") or "draft").strip()
    if not (student_id and template_id and term):
        return jsonify({"error": "student_id, template_id, term required"}), 400
    if status not in ("draft", "complete"):
        status = "draft"
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not _report_can_edit_student(cur, email, student_id):
                return jsonify({"error": "You can only enter report cards for your own homeroom students."}), 403
            cur.execute("""
                INSERT INTO report_entries (student_id, template_id, school_year_start, term, data, status, updated_by, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (student_id, template_id, school_year_start, term)
                DO UPDATE SET data=EXCLUDED.data, status=EXCLUDED.status, updated_by=EXCLUDED.updated_by, updated_at=NOW()
            """, (student_id, template_id, year, term, json.dumps(data or {}), status, email))
            conn.commit()
            return jsonify({"success": True, "status": status})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/calendar/days")
@login_required
def api_calendar_days():
    """
    Return all tags in a date range.
    Query params: start=YYYY-MM-DD, end=YYYY-MM-DD
    OR: month=1..12, year=YYYY (returns the whole month)
    """
    import calendar as cal_mod
    from datetime import date as dt_date

    start = request.args.get("start")
    end   = request.args.get("end")
    if not (start and end):
        try:
            m = int(request.args.get("month", 0))
            y = int(request.args.get("year",  0))
            if not (1 <= m <= 12) or y < 2020:
                return jsonify({"error": "provide start+end or month+year"}), 400
            start = dt_date(y, m, 1).isoformat()
            end   = dt_date(y, m, cal_mod.monthrange(y, m)[1]).isoformat()
        except Exception:
            return jsonify({"error": "invalid params"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT day_date, category_key
                FROM   calendar_day_tags
                WHERE  day_date >= %s AND day_date <= %s
                ORDER  BY day_date, category_key
            """, (start, end))
            out = {}
            for r in cur.fetchall():
                d = r["day_date"].isoformat()
                out.setdefault(d, []).append(r["category_key"])
            return jsonify({"start": start, "end": end, "days": out})
    finally:
        conn.close()


@app.route("/api/calendar/days", methods=["POST"])
@people_required
def api_calendar_days_toggle():
    """
    Toggle a single (date, category) tag.
    Body: { "date": "YYYY-MM-DD", "category_key": "lunch_day", "on": true/false }
    If "on" is omitted, the endpoint flips the current state.
    """
    data = request.get_json() or {}
    day  = (data.get("date") or "").strip()
    cat  = (data.get("category_key") or "").strip()
    on   = data.get("on", None)
    if not day or not cat:
        return jsonify({"error": "date and category_key required"}), 400
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if on is None:
                cur.execute(
                    "SELECT 1 FROM calendar_day_tags WHERE day_date=%s AND category_key=%s",
                    (day, cat)
                )
                on = cur.fetchone() is None

            if on:
                cur.execute("""
                    INSERT INTO calendar_day_tags (day_date, category_key, created_by)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (day_date, category_key) DO NOTHING
                """, (day, cat, session.get("user_email", "")))
            else:
                cur.execute(
                    "DELETE FROM calendar_day_tags WHERE day_date=%s AND category_key=%s",
                    (day, cat)
                )
            conn.commit()
            return jsonify({"success": True, "on": bool(on)})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/calendar/days/bulk", methods=["POST"])
@people_required
def api_calendar_days_bulk():
    """
    Bulk apply or clear a category for a date range.
    """
    from datetime import date as dt_date, timedelta
    data = request.get_json() or {}
    cat    = (data.get("category_key") or "").strip()
    start  = (data.get("start") or "").strip()
    end    = (data.get("end")   or "").strip()
    weekdays_only = bool(data.get("weekdays_only", False))
    action = (data.get("action") or "apply").strip()

    if not (cat and start and end):
        return jsonify({"error": "category_key, start, end required"}), 400
    try:
        d0 = dt_date.fromisoformat(start)
        d1 = dt_date.fromisoformat(end)
    except Exception:
        return jsonify({"error": "invalid date format"}), 400
    if d1 < d0:
        return jsonify({"error": "end before start"}), 400

    dates = []
    cur_d = d0
    while cur_d <= d1:
        if (not weekdays_only) or cur_d.weekday() < 5:
            dates.append(cur_d.isoformat())
        cur_d += timedelta(days=1)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if action == "apply":
                for d in dates:
                    cur.execute("""
                        INSERT INTO calendar_day_tags (day_date, category_key, created_by)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (day_date, category_key) DO NOTHING
                    """, (d, cat, session.get("user_email", "")))
            elif action == "clear":
                cur.execute("""
                    DELETE FROM calendar_day_tags
                    WHERE  category_key = %s
                      AND  day_date >= %s AND day_date <= %s
                      AND  (%s = FALSE OR EXTRACT(ISODOW FROM day_date) < 6)
                """, (cat, d0, d1, weekdays_only))
            else:
                return jsonify({"error": "action must be 'apply' or 'clear'"}), 400
            conn.commit()
            return jsonify({"success": True, "affected": len(dates)})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/calendar/copy-month", methods=["POST"])
@people_required
def api_calendar_copy_month():
    """
    Copy all tags from one month into another (shifted by day-of-month).
    """
    import calendar as cal_mod
    from datetime import date as dt_date
    data = request.get_json() or {}
    try:
        fm = int(data["from_month"]); fy = int(data["from_year"])
        tm = int(data["to_month"]);   ty = int(data["to_year"])
    except Exception:
        return jsonify({"error": "from_month/from_year/to_month/to_year required"}), 400
    overwrite = bool(data.get("overwrite", False))

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            f_first = dt_date(fy, fm, 1)
            f_last  = dt_date(fy, fm, cal_mod.monthrange(fy, fm)[1])
            cur.execute("""
                SELECT day_date, category_key
                FROM calendar_day_tags
                WHERE day_date >= %s AND day_date <= %s
            """, (f_first, f_last))
            src = cur.fetchall()

            if overwrite:
                t_first = dt_date(ty, tm, 1)
                t_last  = dt_date(ty, tm, cal_mod.monthrange(ty, tm)[1])
                cur.execute(
                    "DELETE FROM calendar_day_tags WHERE day_date >= %s AND day_date <= %s",
                    (t_first, t_last)
                )

            last_day_target = cal_mod.monthrange(ty, tm)[1]
            inserted = 0
            for r in src:
                dom = r["day_date"].day
                if dom > last_day_target:
                    continue
                target = dt_date(ty, tm, dom).isoformat()
                cur.execute("""
                    INSERT INTO calendar_day_tags (day_date, category_key, created_by)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (day_date, category_key) DO NOTHING
                """, (target, r["category_key"], session.get("user_email", "")))
                if cur.rowcount:
                    inserted += 1
            conn.commit()
            return jsonify({"success": True, "inserted": inserted})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/calendar/month-summary")
@login_required
def api_calendar_month_summary():
    """
    Counts of each category in a month. Used by billing and dashboards.
    """
    import calendar as cal_mod
    from datetime import date as dt_date
    try:
        m = int(request.args.get("month", 0))
        y = int(request.args.get("year",  0))
        if not (1 <= m <= 12) or y < 2020:
            return jsonify({"error": "invalid month/year"}), 400
    except Exception:
        return jsonify({"error": "invalid month/year"}), 400

    first = dt_date(y, m, 1)
    last  = dt_date(y, m, cal_mod.monthrange(y, m)[1])
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT category_key, COUNT(*) AS n
                FROM   calendar_day_tags
                WHERE  day_date >= %s AND day_date <= %s
                GROUP  BY category_key
            """, (first, last))
            counts = {r["category_key"]: int(r["n"]) for r in cur.fetchall()}
            return jsonify({"month": m, "year": y, "counts": counts})
    finally:
        conn.close()


# ============================================
# STARTUP + RUN
# ============================================

init_db()

if __name__ == "__main__":
    print("Mizzentop Admin — PostgreSQL mode")
    # Port defaults to 5000 (unchanged for the live site). The local run script
    # sets LOCAL_PORT to avoid clashing with macOS AirPlay Receiver on port 5000.
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("LOCAL_PORT", "5000")))
