# Mizzentop Day School — Admin Portal

Custom-built School Information System (SIS) for Mizzentop Day School. Manages daily operations, attendance, dismissal planning, billing, and financial aid.

**Live URL:** https://admin.mizzentopdayschool.org
**Repo:** github.com/ccuneo-ui/school-attendance-system (main branch)

## Tech Stack

- **Backend:** Python 3 / Flask (~3,967 lines in single `app.py`)
- **Database:** PostgreSQL (managed by Render, connected via `DATABASE_URL`)
- **Auth:** Google OAuth 2.0 via Authlib, restricted to `@mizzentop.org`
- **Frontend:** Plain HTML/CSS/JS — no framework, no build step
- **Hosting:** Render.com with auto-deploy from GitHub
- **Email:** Resend API (for backup emails)

## Project Structure

All files live in the repo root. No subdirectories besides `static/brand/`.

### Core
- `app.py` — All routes, API endpoints, auth, DB schema, business logic
- `requirements.txt` — Flask, flask-cors, authlib, requests, psycopg2-binary, gunicorn
- `render.yaml` — Render deployment config

### HTML Pages (18 files, ~9,055 lines total)
Each page is self-contained with inline `<style>` and `<script>` tags.

| File | Purpose | Auth |
|------|---------|------|
| `home.html` | Portal dashboard with four permission-gated silos | `@login_required` |
| `login.html` | Google SSO login | None |
| `dismissal_planner.html` | Daily Ops — attendance + dismissal planning | `@login_required` |
| `dismissal_staff_view.html` | Read-only dismissal board for all staff | `@login_required` |
| `dismissal_options.html` | Manage activities & bus routes | `@people_required` |
| `bus_dashboard.html` | Students grouped by bus route | `@login_required` |
| `mcard_tracker.html` | Snack cart charge tracker | `@login_required` |
| `students.html` | Student directory & profiles | `@people_required` |
| `people.html` | Staff directory & permissions | `@people_required` |
| `program_attendance.html` | Billable program attendance | `@login_required` |
| `aftercare_attendance.html` | Before/aftercare check-in/out | `@login_required` |
| `billing_rates.html` | Billing rate configuration | `@login_required` |
| `billing_report.html` | Monthly billing reports | `@login_required` |
| `financial_aid.html` | Financial aid management | `@login_required` |
| `fa_tuition_rates.html` | Tuition rates by year/division | `@login_required` |
| `attendance_form.html` | General daily attendance | `@login_required` |
| `signature_generator.html` | Email signature generator | `@login_required` |

### Assets
- `logo.svg` — School logo (served at `/logo.svg`, used as favicon)
- `icon-facebook.svg`, `icon-instagram.svg`, `icon-linkedin.svg`

### Utility Scripts (not part of running app)
- `import_roster.py`, `simple_import.py`, `fix_and_import.py` — Roster import
- `create_general_attendance.py` — Bulk attendance creation
- `dismissal_migration.py` — Data migration
- `remove_test_students.py` — Test data cleanup

## Key Conventions

### Architecture
- **Single-file backend:** All server logic lives in `app.py`. No blueprints or modules.
- **Single-file frontend:** Each HTML page embeds its own CSS and JS. No external stylesheets or script files.
- **No build step:** HTML files are served directly via `send_from_directory(".", filename)`.
- **No test suite:** No pytest/unittest. Test manually against the live site.
- **No local git repo:** Files are pushed to GitHub via `gh api` (GitHub CLI) or web upload. Render auto-deploys on push to `main`.

### Database Patterns
- PostgreSQL via `psycopg2` with `RealDictCursor`
- Connection helper: `get_db_connection()` returns a connection with `autocommit=False`
- Helper functions: `fa(cur)` = fetchall, `fo(cur)` = fetchone
- Parameterized queries with `%s` placeholders (never f-strings for SQL)
- Schema created in `init_db()` on startup using `CREATE TABLE IF NOT EXISTS`
- Column migrations use `ALTER TABLE` wrapped in info_schema checks
- No ORM, no migration framework

### Auth Model
- `@login_required` — Any authenticated `@mizzentop.org` staff
- `@superadmin_required` — Only `ccuneo@mizzentop.org` (hardcoded as `SUPERADMIN_EMAIL`)
- `@people_required` — Superadmin OR `can_manage_people` permission
- Permissions refresh from DB on every request via `@app.before_request`
- Three permission flags on staff: `can_record_attendance`, `can_manage_people`, `can_manage_billing`

### Frontend Conventions
- CSS variables for theming: `--navy`, `--green`, `--gold`, `--cream`, `--border`, `--muted`
- Fonts: Playfair Display (headings), DM Sans (body), DM Mono (monospace)
- Google Fonts loaded via CDN link in each HTML file
- All API calls use `fetch()` with JSON payloads
- Modals are inline HTML toggled via CSS classes (`.open`)
- No component library or framework

### API Conventions
- RESTful JSON endpoints under `/api/`
- Page routes return HTML via `send_from_directory`
- Responses use `jsonify()` with `{"success": True}` or `{"error": "message"}`
- Transactions: explicit `conn.commit()` / `conn.rollback()` in try/except/finally blocks

## Environment Variables

Set in Render dashboard, NOT in repo:

| Variable | Required | Purpose |
|----------|----------|---------|
| `DATABASE_URL` | Yes | PostgreSQL connection string (auto-set by Render) |
| `GOOGLE_CLIENT_ID` | Yes | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Yes | Google OAuth client secret |
| `SECRET_KEY` | Yes | Flask session signing key |
| `RESEND_API_KEY` | No | Email service for backup emails |
| `BACKUP_CRON_SECRET` | No | Auth key for automated backup endpoint |

## Database Tables

**Core:** `students`, `staff`
**Households (new):** `households`, `parents`, `household_members`, `student_households`
**Dismissal:** `dismissal_today`, `daily_dismissal`, `dismissal_options`, `electives`
**Attendance:** `program_attendance`, `aftercare_attendance`, `mcard_charges`
**Billing:** `billing_rates`
**Financial Aid:** `financial_aid_families`, `financial_aid_students`, `fa_tuition_rates`

### Household Schema Design (key decisions)

The households model was designed to support families where students may have multiple households (e.g. divorced parents). Key relationships:

- A student can belong to more than one household via `student_households` (the join table)
- `student_households.is_primary` flags the primary household for billing purposes
- `households` contains the family unit; `parents` contains individual parent/guardian records
- `household_members` links parents to households
- `staff.parent_id` is a nullable foreign key allowing a staff member to also be a parent record
- Billing rolls up through the primary household: `student -> student_households (is_primary) -> household -> invoice`
- Dismissal authorization follows: `student -> student_households -> household -> household_members -> parents (where can_pickup = true)`
- `financial_aid_families` is the legacy financial aid table and is intentionally kept separate from `households` for now pending a future migration decision

This system is **staff-only** — parents do not log in. Parent records exist as data, not as users.

## Deployment

1. Edit files locally
2. Push to GitHub: `gh api repos/ccuneo-ui/school-attendance-system/contents/FILENAME -X PUT -f message="msg" -f sha="CURRENT_SHA" -f content="$(base64 -i FILENAME)"`
3. Render auto-deploys (90s–3min)
4. Check Render dashboard Events tab for success

Backup: `GET /backup/download?key=school2026`

## Changelog

Maintain `CHANGELOG.md` at the repo root. At the end of any session that changed real behavior (not pure refactors, comment edits, or doc-only changes), append **one line at the top** of `CHANGELOG.md` in the format:

```
YYYY-MM-DD: <one-sentence description of what shipped>
```

- Reverse-chronological order — newest entry always at the top.
- One line per shipped change. Keep it terse and user-facing (what a staff member would notice), not implementation detail.
- Skip the entry entirely if the session shipped no behavior change.
- Don't backfill history — only log what this session shipped.
