#!/usr/bin/env python3
"""
build_sections_from_homeroom.py

Phase 1 of the unified "sections" model. One-time (idempotent) build of homeroom and
advisory SECTIONS + rosters from the existing per-student columns
students.homeroom_teacher_id / advisory_teacher_id.

Why: going forward a homeroom / advisory / subject class / elective is all one thing —
a "section" (one teacher, a room, a term, a roster). This seeds the new sections /
section_enrollments tables from the current homeroom & advisory assignments so nothing is
lost. students.homeroom_teacher_id is left exactly as-is (a synced shadow column for now),
so the attendance report / dismissal / bus pages keep working unchanged.

Safety:
  - DRY-RUN by default: prints what would be created and commits NOTHING. Add --apply.
  - Idempotent: reuses an existing (year, type, teacher) section and only adds the
    enrollments that are missing. Safe to re-run.
  - Creates the rooms / sections / section_enrollments tables if they don't exist yet, so
    it can run against a local dev DB before the app has been redeployed.

Usage (mirror of backfill_emergency_contacts.py):
  Local practice DB, dry-run:
      DATABASE_URL=postgresql://localhost:5432/mizzentop_dev python3 build_sections_from_homeroom.py
  Local practice DB, apply:
      DATABASE_URL=postgresql://localhost:5432/mizzentop_dev python3 build_sections_from_homeroom.py --apply
  Pick a specific school year (start year), e.g. 2026 for 2026-27:
      ... python3 build_sections_from_homeroom.py --year 2026
  Production: paste the Render EXTERNAL Database URL in place of the local one, dry-run first.
"""
import os, sys
from datetime import date
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/mizzentop_dev")
APPLY = "--apply" in sys.argv


def arg_year():
    for i, a in enumerate(sys.argv):
        if a == "--year" and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    # Default: current school year with a July-1 rollover (matches the app default).
    t = date.today()
    return t.year if t.month >= 7 else t.year - 1


DDL = [
    """CREATE TABLE IF NOT EXISTS rooms (
        room_id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        active BOOLEAN NOT NULL DEFAULT TRUE, sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS sections (
        section_id SERIAL PRIMARY KEY, school_year_start INTEGER NOT NULL,
        type TEXT NOT NULL DEFAULT 'subject', name TEXT NOT NULL, subject TEXT, grade TEXT,
        term TEXT NOT NULL DEFAULT 'year',
        teacher_id INTEGER REFERENCES staff(staff_id) ON DELETE SET NULL,
        room_id INTEGER REFERENCES rooms(room_id) ON DELETE SET NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_by TEXT)""",
    """CREATE TABLE IF NOT EXISTS section_enrollments (
        section_id INTEGER NOT NULL REFERENCES sections(section_id) ON DELETE CASCADE,
        student_id INTEGER NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(section_id, student_id))""",
]


def find_or_create_section(cur, year, stype, teacher_id, name):
    cur.execute("""SELECT section_id FROM sections
                   WHERE school_year_start=%s AND type=%s AND teacher_id=%s""",
                (year, stype, teacher_id))
    row = cur.fetchone()
    if row:
        return row["section_id"], False
    cur.execute("""INSERT INTO sections (school_year_start, type, name, teacher_id, term)
                   VALUES (%s,%s,%s,%s,'year') RETURNING section_id""",
                (year, stype, name, teacher_id))
    return cur.fetchone()["section_id"], True


def build_kind(cur, year, col, stype, label):
    """Build sections of one kind (homeroom / advisory) from students.<col>."""
    cur.execute(f"""
        SELECT s.{col} AS teacher_id, st.first_name, st.last_name
        FROM   students s
        JOIN   staff st ON st.staff_id = s.{col}
        WHERE  s.{col} IS NOT NULL AND s.status = 'active'
        GROUP  BY s.{col}, st.first_name, st.last_name
        ORDER  BY st.last_name, st.first_name
    """)
    teachers = cur.fetchall()
    sections_created = enroll_added = 0
    print(f"\n{label} sections:")
    if not teachers:
        print("  (none — no students have this assignment)")
    for t in teachers:
        tid = t["teacher_id"]
        name = f"{label} - {t['last_name']}"
        sec_id, created = find_or_create_section(cur, year, stype, tid, name)
        if created:
            sections_created += 1
        cur.execute(f"""SELECT student_id FROM students
                        WHERE {col}=%s AND status='active'""", (tid,))
        sids = [r["student_id"] for r in cur.fetchall()]
        added = 0
        for sid in sids:
            cur.execute("""INSERT INTO section_enrollments (section_id, student_id)
                           VALUES (%s,%s) ON CONFLICT (section_id, student_id) DO NOTHING""",
                        (sec_id, sid))
            added += cur.rowcount
        enroll_added += added
        flag = "NEW   " if created else "exists"
        print(f"  [{flag}] {name:<26} teacher #{tid}   roster {len(sids):>2}   (+{added} enrolled)")
    return sections_created, enroll_added


def main():
    year = arg_year()
    print("Build homeroom / advisory sections")
    print("  DB:   ", DATABASE_URL)
    print("  year: ", f"{year}-{year + 1}  (school_year_start={year})")
    print("  mode: ", "APPLY (will commit)" if APPLY else "DRY-RUN (no writes)")

    conn = psycopg2.connect(DATABASE_URL)
    conn.set_client_encoding("UTF8")  # so accented names encode regardless of locale
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    for stmt in DDL:
        cur.execute(stmt)

    hs, he = build_kind(cur, year, "homeroom_teacher_id", "homeroom", "Homeroom")
    ads, ae = build_kind(cur, year, "advisory_teacher_id", "advisory", "Advisory")

    print("\nSummary:")
    print(f"  homeroom: {hs} sections {'created' if APPLY else 'would be created'}, {he} enrollments")
    print(f"  advisory: {ads} sections {'created' if APPLY else 'would be created'}, {ae} enrollments")

    if APPLY:
        conn.commit()
        print("\n  COMMITTED to", DATABASE_URL)
    else:
        conn.rollback()
        print("\n  DRY-RUN only — nothing written. Re-run with --apply to commit.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
