"""
School Attendance System - Backend Server
This Flask app connects the HTML attendance form to your SQLite database
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3
from datetime import datetime
import os

app = Flask(__name__)
CORS(app)  # Allow the HTML form to communicate with this server

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
def index():
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
    """Create mcard_charges table if it doesn't exist"""
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
    conn.commit()
    conn.close()

@app.route('/mcard')
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

# ============================================
# RUN SERVER
# ============================================

if __name__ == '__main__':
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
