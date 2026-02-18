"""
Dismissal Migration Script
Run this ONCE to add dismissal tables to your existing school.db
"""
import sqlite3
import csv
import os

# Use same DB path logic as app.py
PERSISTENT_DB = '/var/data/school.db'
LOCAL_DB = 'school.db'
DATABASE = PERSISTENT_DB if os.path.exists('/var/data') else LOCAL_DB

def run_migration():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print(f"Connected to database: {DATABASE}")

    # 1. Add dismissal default columns to students table
    columns_to_add = [
        ('dismissal_mon', 'TEXT DEFAULT NULL'),
        ('dismissal_tue', 'TEXT DEFAULT NULL'),
        ('dismissal_wed', 'TEXT DEFAULT NULL'),
        ('dismissal_thu', 'TEXT DEFAULT NULL'),
        ('dismissal_fri', 'TEXT DEFAULT NULL'),
        ('before_care',   'INTEGER DEFAULT 0'),
    ]
    for col, col_type in columns_to_add:
        try:
            cursor.execute(f'ALTER TABLE students ADD COLUMN {col} {col_type}')
            print(f"  Added column: {col}")
        except sqlite3.OperationalError:
            print(f"  Column already exists: {col}")

    # 2. Create daily_dismissal table (what admin assigns each day)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_dismissal (
            dismissal_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id      INTEGER NOT NULL,
            dismissal_date  TEXT NOT NULL,
            dismissal_type  TEXT NOT NULL,  -- 'pickup', 'bus', 'activity'
            destination     TEXT,           -- specific bus route or activity name
            notes           TEXT,
            is_override     INTEGER DEFAULT 0,  -- 1 if differs from default
            recorded_by     TEXT,
            recorded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(student_id),
            UNIQUE(student_id, dismissal_date)
        )
    ''')
    print("  Created table: daily_dismissal")

    conn.commit()
    conn.close()
    print("\nMigration complete!")

def import_finalsite_defaults(csv_path):
    """Import dismissal defaults from Finalsite CSV export"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    def map_dismissal(raw):
        """Map Finalsite value to our type"""
        if not raw:
            return None
        raw = raw.strip()
        if raw == 'Bus':
            return 'bus'
        elif raw == 'Pick Up':
            return 'pickup'
        elif raw in ('After Care', 'After Care, Pick Up', 'After Care, Bus'):
            return 'activity'  # After Care treated as activity
        elif raw == 'Pick Up, Bus':
            return 'bus'  # Edge case - default to bus, admin can adjust
        return raw.lower()

    updated = 0
    not_found = []

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader)  # skip group header row
        next(reader)  # skip column header row

        for row in reader:
            if len(row) < 14:
                continue

            first_name  = row[2].strip()
            last_name   = row[4].strip()
            same_plan   = row[7].strip()  # 'Yes' or 'No'
            same_ms     = row[8].strip()  # plan if same every day
            mon         = row[9].strip()
            tue         = row[10].strip()
            wed         = row[11].strip()
            thu         = row[12].strip()
            fri         = row[13].strip()
            before_care = 1 if (len(row) > 35 and row[35].strip() == 'Yes') else 0

            # Resolve per-day plans
            if same_plan == 'Yes':
                mon = tue = wed = thu = fri = same_ms

            # Map to our types
            d_mon = map_dismissal(mon)
            d_tue = map_dismissal(tue)
            d_wed = map_dismissal(wed)
            d_thu = map_dismissal(thu)
            d_fri = map_dismissal(fri)

            # Match student in DB by name
            student = cursor.execute('''
                SELECT student_id FROM students
                WHERE LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?)
                AND status = 'active'
            ''', (first_name, last_name)).fetchone()

            if not student:
                not_found.append(f"{first_name} {last_name}")
                continue

            cursor.execute('''
                UPDATE students SET
                    dismissal_mon = ?,
                    dismissal_tue = ?,
                    dismissal_wed = ?,
                    dismissal_thu = ?,
                    dismissal_fri = ?,
                    before_care   = ?
                WHERE student_id = ?
            ''', (d_mon, d_tue, d_wed, d_thu, d_fri, before_care, student['student_id']))
            updated += 1

    conn.commit()
    conn.close()

    print(f"\nImport complete!")
    print(f"  Updated: {updated} students")
    if not_found:
        print(f"  Not matched ({len(not_found)}): {', '.join(not_found)}")

if __name__ == '__main__':
    run_migration()
    csv_file = 'Transportation__Before_Care_And_After_Care_Forms.csv'
    if os.path.exists(csv_file):
        print(f"\nImporting Finalsite defaults from {csv_file}...")
        import_finalsite_defaults(csv_file)
    else:
        print(f"\nCSV not found at {csv_file} - run migration only for now.")
        print("Upload the CSV to your server and re-run to import defaults.")
