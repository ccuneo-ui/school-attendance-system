#!/usr/bin/env python3
"""
backfill_emergency_contacts.py

One-time (idempotent) backfill of students.emergency_contact_name / emergency_contact_phone
from the Finalsite master CSV.

Why: the 2026-27 roll-forward (roll_forward_import.py) imported households, parents, and
students, but never populated each student's NON-PARENT emergency contact fields. As a result
the Family Directory and Family Manager show those blank. The master CSV already carries this
data in emerg1_name / emerg1_rel / emerg1_phone for ~143 of 151 active students. This script
copies it into the students table, matched by finalsite_id (= Finalsite child_id).

Safety:
  - DRY-RUN by default: prints what would change and commits NOTHING. Add --apply to commit.
  - By default only fills fields that are currently EMPTY, so it will NOT clobber any edits made
    later in the Family Manager. Add --overwrite to force every value from the CSV.
  - Idempotent: safe to re-run.

Usage (mirror of roll_forward_import.py):
  Local practice DB, dry-run:
      DATABASE_URL=postgresql://localhost:5432/mizzentop_dev python3 backfill_emergency_contacts.py
  Local practice DB, apply:
      DATABASE_URL=postgresql://localhost:5432/mizzentop_dev python3 backfill_emergency_contacts.py --apply
  Production: paste the Render EXTERNAL Database URL in place of the local one, dry-run first.
"""
import os, sys, csv
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/mizzentop_dev")
CSV_PATH     = os.environ.get("CSV_PATH", "mizzentop_finalsite_2026-27_master_COMPLETE.csv")
APPLY        = "--apply" in sys.argv
OVERWRITE    = "--overwrite" in sys.argv


def clean_phone(p):
    """Light cleanup only: trim, and fix the obvious '845-206=1673' mistyped-dash pattern."""
    return (p or "").strip().replace("=", "-")


def build_name(name, rel):
    """Store the contact name, appending the relationship in parens when it's informative
    (e.g. 'Roxanne Prine (Grandmother)'). Skips a blank or unhelpful 'Other' relationship."""
    name = (name or "").strip()
    rel  = (rel or "").strip()
    if rel and rel.lower() != "other":
        return f"{name} ({rel})".strip()
    return name


def main():
    rows = list(csv.DictReader(open(CSV_PATH, newline="")))
    print("Backfill emergency contacts")
    print("  DB:  ", DATABASE_URL)
    print("  CSV: ", CSV_PATH, "(", len(rows), "rows )")
    print("  mode:", "APPLY (will commit)" if APPLY else "DRY-RUN (no writes)",
          "| overwrite existing:", OVERWRITE)
    print()

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    updated = skipped_existing = no_data = unmatched = 0
    shown = 0
    for r in rows:
        cid = (r.get("child_id") or "").strip()
        if not cid:
            continue
        name  = build_name(r.get("emerg1_name"), r.get("emerg1_rel"))
        phone = clean_phone(r.get("emerg1_phone"))
        if not name and not phone:
            no_data += 1
            continue
        cur.execute("""SELECT student_id, first_name, last_name,
                              emergency_contact_name, emergency_contact_phone
                       FROM students WHERE finalsite_id=%s""", (cid,))
        s = cur.fetchone()
        if not s:
            unmatched += 1
            continue
        has_existing = bool((s["emergency_contact_name"] or "").strip()
                            or (s["emergency_contact_phone"] or "").strip())
        if has_existing and not OVERWRITE:
            skipped_existing += 1
            continue
        cur.execute("""UPDATE students
                          SET emergency_contact_name=%s,
                              emergency_contact_phone=%s,
                              updated_at=NOW()
                        WHERE student_id=%s""",
                    (name or None, phone or None, s["student_id"]))
        updated += 1
        if shown < 12:
            print(f"  {s['first_name']} {s['last_name']:<16} -> {name}  |  {phone}")
            shown += 1

    print()
    print(f"  {'updated' if APPLY else 'would update'}: {updated}")
    print(f"  skipped (already had a value): {skipped_existing}")
    print(f"  no emergency data in CSV row:  {no_data}")
    print(f"  CSV rows with no matching student (finalsite_id): {unmatched}")

    if APPLY:
        conn.commit()
        print("\n  COMMITTED to", DATABASE_URL)
    else:
        conn.rollback()
        print("\n  DRY-RUN only — nothing written. Re-run with --apply to commit.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
