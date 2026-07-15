from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, session, flash
import sqlite3
import qrcode
import os
import csv
import io
import json
import uuid
import base64
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.units import inch
from functools import wraps
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')

# Use PostgreSQL DATABASE_URL
QR_DIR = 'qr_codes'
os.makedirs(QR_DIR, exist_ok=True)

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

# ─── Database Setup ────────────────────────────────────────────────────────────

class DBWrapper:
    def __init__(self):
        self.conn = sqlite3.connect("database.db")
        self.conn.row_factory = sqlite3.Row

    def execute(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

def get_db():
    return DBWrapper()

def init_db():
    conn = get_db()

    conn.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        venue TEXT NOT NULL,
        max_participants INTEGER NOT NULL,
        share_link TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        student_name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT NOT NULL,
        college_id TEXT NOT NULL,
        qr_path TEXT,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        registration_id INTEGER NOT NULL UNIQUE,
        event_id INTEGER NOT NULL,
        student_name TEXT,
        college_id TEXT,
        checked_in_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(registration_id) REFERENCES registrations(id) ON DELETE CASCADE
    )''')

    # Seed admin
    conn.execute('''CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )''')
    conn.execute(
    "INSERT OR IGNORE INTO admins (username, password) VALUES (?, ?)",
    (ADMIN_USERNAME, ADMIN_PASSWORD)
    )

    conn.commit()
    conn.close()

# ─── Auth Decorator ─────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

# ─── QR Code Generation ──────────────────────────────────────────────────────

def generate_qr(registration_id, student_name, event_id):
    """Generate QR code and save as file"""
    data = json.dumps({
        "registration_id": registration_id,
        "student_name": student_name,
        "event_id": event_id
    })
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    path = os.path.join(QR_DIR, f"reg_{registration_id}.png")
    img.save(path)
    return path

def generate_qr_base64(registration_id, student_name, event_id):
    """Generate QR code as base64 string for web display"""
    data = json.dumps({
        "registration_id": registration_id,
        "student_name": student_name,
        "event_id": event_id
    })
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to base64
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    qr_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return f"data:image/png;base64,{qr_base64}"

# ─── Public Routes ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    conn = get_db()
    events = conn.execute('''
        SELECT e.*, 
               COUNT(r.id) as registered_count
        FROM events e
        LEFT JOIN registrations r ON e.id = r.event_id
        GROUP BY e.id
        ORDER BY e.date ASC
    ''').fetchall()
    conn.close()
    return render_template('index.html', events=events)

@app.route('/register/<int:event_id>', methods=['GET', 'POST'])
def register(event_id):
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    if not event:
        conn.close()
        return "Event not found", 404

    reg_count = conn.execute('SELECT COUNT(*) FROM registrations WHERE event_id = ?', (event_id,)).fetchone()[0]

    if request.method == 'POST':
        name = request.form['name'].strip()
        email = request.form['email'].strip()
        phone = request.form['phone'].strip()
        college_id = request.form['college_id'].strip()

        # Check duplicate
        existing = conn.execute(
            'SELECT id FROM registrations WHERE event_id=? AND (email=? OR college_id=?)',
            (event_id, email, college_id)
        ).fetchone()
        if existing:
            conn.close()
            return render_template('register.html', event=event, reg_count=reg_count,
                                   error="You are already registered for this event!")

        if reg_count >= event['max_participants']:
            conn.close()
            return render_template('register.html', event=event, reg_count=reg_count,
                                   error="Event is full. No more registrations allowed.")
        
        conn.execute(
            'INSERT INTO registrations (event_id, student_name, email, phone, college_id) VALUES (?,?,?,?,?)',
            (event_id, name, email, phone, college_id)
        )
        conn.commit()
        reg_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        qr_path = generate_qr(reg_id, name, event_id)
        conn.execute('UPDATE registrations SET qr_path=? WHERE id=?', (qr_path, reg_id))
        conn.commit()
        conn.close()
        return redirect(url_for('confirmation', reg_id=reg_id))

    conn.close()
    return render_template('register.html', event=event, reg_count=reg_count, error=None)

@app.route('/event/<share_link>', methods=['GET', 'POST'])
def register_via_link(share_link):
    """Register for an event using a shareable link"""
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE share_link = ?', (share_link,)).fetchone()
    if not event:
        conn.close()
        return "Event not found", 404
    
    # Redirect to normal registration
    conn.close()
    return redirect(url_for('register', event_id=event['id']))

@app.route('/confirmation/<int:reg_id>')
def confirmation(reg_id):
    conn = get_db()
    reg = conn.execute('''
        SELECT r.*, e.name as event_name, e.date, e.time, e.venue
        FROM registrations r JOIN events e ON r.event_id = e.id
        WHERE r.id = ?
    ''', (reg_id,)).fetchone()
    conn.close()
    return render_template('confirmation.html', reg=reg)

@app.route('/qr/<int:reg_id>')
def get_qr(reg_id):
    conn = get_db()
    reg = conn.execute('SELECT qr_path FROM registrations WHERE id=?', (reg_id,)).fetchone()
    conn.close()
    if reg and reg['qr_path'] and os.path.exists(reg['qr_path']):
        return send_file(reg['qr_path'], mimetype='image/png')
    return "QR not found", 404

# ─── Admin Auth ──────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db()
        admin = conn.execute('SELECT * FROM admins WHERE username=? AND password=?',
                             (username, password)).fetchone()
        conn.close()
        if admin:
            session['admin_logged_in'] = True
            session['admin_username'] = username
            return redirect(url_for('admin_dashboard'))
        error = "Invalid credentials"
    return render_template('admin_login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

# ─── Admin Panel ─────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin_dashboard():
    conn = get_db()
    events = conn.execute('''
        SELECT e.*, COUNT(r.id) as reg_count,
               (SELECT COUNT(*) FROM attendance a WHERE a.event_id = e.id) as att_count
        FROM events e LEFT JOIN registrations r ON e.id = r.event_id
        GROUP BY e.id ORDER BY e.date DESC
    ''').fetchall()
    total_events = len(events)
    total_registrations = sum(e['reg_count'] for e in events)
    total_attendance = sum(e['att_count'] for e in events)
    conn.close()
    
    # Build shareable URLs
    base_url = request.host_url.rstrip('/')
    events_with_links = []
    for event in events:
        share_url = f"{base_url}/event/{event['share_link']}"
        event_dict = dict(event)
        event_dict['share_url'] = share_url
        events_with_links.append(event_dict)
    
    return render_template('admin.html', events=events_with_links,
                           total_events=total_events,
                           total_registrations=total_registrations,
                           total_attendance=total_attendance)

@app.route('/admin/event/create', methods=['GET', 'POST'])
@admin_required
def create_event():
    if request.method == 'POST':
        name = request.form['name']
        description = request.form.get('description', '')
        date = request.form['date']
        event_date = datetime.strptime(date, "%Y-%m-%d").date()
        today = datetime.today().date()

        if event_date < today:
            flash("Event date cannot be in the past!", "error")
            return render_template(
                'event_form.html',
                event=None,
                action='Create'
            )
        time = request.form['time']
        venue = request.form['venue']
        max_p = int(request.form['max_participants'])
        share_link = str(uuid.uuid4())[:8]  # Generate unique 8-char code
        conn = get_db()
        existing = conn.execute(
            '''
            SELECT id FROM events
            WHERE name = ?
            AND date = ?
            AND time = ?
            AND venue = ?
            ''',
            (name, date, time, venue)
        ).fetchone()
        if existing:
            flash("An event with the same name, date, time and venue already exists!", "error")
            conn.close()
            return render_template(
                'event_form.html',
                event=None,
                action='Create'
            )
        conn.execute('INSERT INTO events (name, description, date, time, venue, max_participants, share_link) VALUES (?,?,?,?,?,?,?)',
                     (name, description, date, time, venue, max_p, share_link))
        conn.commit()
        conn.close()
        return redirect(url_for('admin_dashboard'))
    return render_template('event_form.html', event=None, action='Create')

@app.route('/admin/event/edit/<int:event_id>', methods=['GET', 'POST'])
@admin_required
def edit_event(event_id):
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    if request.method == 'POST':
        conn.execute('''UPDATE events SET name=?, description=?, date=?, time=?, venue=?, max_participants=?
                        WHERE id=?''',
                     (request.form['name'], request.form.get('description',''),
                      request.form['date'], request.form['time'],
                      request.form['venue'], int(request.form['max_participants']), event_id))
        conn.commit()
        conn.close()
        return redirect(url_for('admin_dashboard'))
    conn.close()
    return render_template('event_form.html', event=event, action='Edit')

@app.route('/admin/event/delete/<int:event_id>', methods=['POST'])
@admin_required
def delete_event(event_id):
    conn = get_db()
    conn.execute('DELETE FROM events WHERE id=?', (event_id,))
    conn.execute('DELETE FROM registrations WHERE event_id=?', (event_id,))
    conn.execute('DELETE FROM attendance WHERE event_id=?', (event_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/event/<int:event_id>/registrations')
@admin_required
def event_registrations(event_id):
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    registrations = conn.execute('''
        SELECT r.*, 
               CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END as checked_in,
               a.checked_in_at
        FROM registrations r LEFT JOIN attendance a ON r.id = a.registration_id
        WHERE r.event_id = ?
        ORDER BY r.registered_at DESC
    ''', (event_id,)).fetchall()
    conn.close()
    return render_template('registrations.html', event=event, registrations=registrations)

# ─── QR Entry / Check-in ─────────────────────────────────────────────────────

@app.route('/admin/checkin')
@admin_required
def checkin_page():
    conn = get_db()
    events = conn.execute('SELECT * FROM events ORDER BY date DESC').fetchall()
    conn.close()
    return render_template('checkin.html', events=events)

@app.route('/admin/checkin/verify', methods=['POST'])
@admin_required
def verify_qr():
    data = request.json
    qr_data = data.get('qr_data', '')
    try:
        info = json.loads(qr_data)
        reg_id = info['registration_id']
        event_id = info['event_id']
    except Exception:
        return jsonify({'status': 'invalid', 'message': '❌ Invalid QR Code'})

    conn = get_db()
    reg = conn.execute('''
        SELECT r.*, e.name as event_name
        FROM registrations r JOIN events e ON r.event_id = e.id
        WHERE r.id = ?
    ''', (reg_id,)).fetchone()

    if not reg:
        conn.close()
        return jsonify({'status': 'invalid', 'message': '❌ Registration not found'})

    already = conn.execute('SELECT id FROM attendance WHERE registration_id=?', (reg_id,)).fetchone()
    if already:
        conn.close()
        return jsonify({'status': 'duplicate', 'message': f'⚠️ {reg["student_name"]} already checked in!'})

    conn.execute('''INSERT INTO attendance (registration_id, event_id, student_name, college_id)
                    VALUES (?,?,?,?)''',
                 (reg_id, event_id, reg['student_name'], reg['college_id']))
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'message': f'✅ Entry Allowed',
        'name': reg['student_name'],
        'college_id': reg['college_id'],
        'event': reg['event_name']
    })

# ─── Attendance Tracking ──────────────────────────────────────────────────────

@app.route('/admin/event/<int:event_id>/attendance')
@admin_required
def event_attendance(event_id):
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    attendance = conn.execute('''
        SELECT a.*, r.email, r.phone
        FROM attendance a JOIN registrations r ON a.registration_id = r.id
        WHERE a.event_id = ?
        ORDER BY a.checked_in_at DESC
    ''', (event_id,)).fetchall()
    conn.close()
    return render_template('attendance.html', event=event, attendance=attendance)

# ─── Export Routes ───────────────────────────────────────────────────────────

@app.route('/admin/event/<int:event_id>/export/csv')
@admin_required
def export_csv(event_id):
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    rows = conn.execute('''
        SELECT r.student_name, r.email, r.phone, r.college_id, r.registered_at,
               CASE WHEN a.id IS NOT NULL THEN 'Yes' ELSE 'No' END as attended,
               a.checked_in_at
        FROM registrations r LEFT JOIN attendance a ON r.id = a.registration_id
        WHERE r.event_id = ?
    ''', (event_id,)).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Email', 'Phone', 'Roll Number', 'Registered At', 'Attended', 'Check-in Time'])
    for row in rows:
        writer.writerow(list(row))
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode()),
                     mimetype='text/csv',
                     as_attachment=True,
                     download_name=f'{event["name"]}_attendance.csv')

@app.route('/admin/event/<int:event_id>/export/pdf')
@admin_required
def export_pdf(event_id):
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    rows = conn.execute('''
        SELECT r.student_name, r.college_id, r.email,
               CASE WHEN a.id IS NOT NULL THEN 'Present' ELSE 'Absent' END as status,
               COALESCE(a.checked_in_at, '-') as check_in
        FROM registrations r LEFT JOIN attendance a ON r.id = a.registration_id
        WHERE r.event_id = ?
    ''', (event_id,)).fetchall()
    conn.close()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []
    elements.append(Paragraph(f"Attendance Report: {event['name']}", styles['Title']))
    elements.append(Paragraph(f"Date: {event['date']} | Venue: {event['venue']}", styles['Normal']))
    elements.append(Spacer(1, 0.2 * inch))

    table_data = [['Name', 'Roll Number', 'Email', 'Status', 'Check-in Time']]
    for row in rows:
        table_data.append(list(row))

    t = Table(table_data, colWidths=[1.4*inch, 1.1*inch, 1.8*inch, 0.9*inch, 1.5*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a1a2e')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#dee2e6')),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('PADDING', (0,0), (-1,-1), 5),
    ]))
    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', as_attachment=True,
                     download_name=f'{event["name"]}_report.pdf')

# ─── API: Live Stats ─────────────────────────────────────────────────────────

@app.route('/api/event/<int:event_id>/stats')
def event_stats(event_id):
    conn = get_db()
    reg = conn.execute('SELECT COUNT(*) FROM registrations WHERE event_id=?', (event_id,)).fetchone()[0]
    att = conn.execute('SELECT COUNT(*) FROM attendance WHERE event_id=?', (event_id,)).fetchone()[0]
    conn.close()
    return jsonify({'registrations': reg, 'attendance': att})

# Initialize the database
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
