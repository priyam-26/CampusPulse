from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, session, flash, abort
import psycopg2
import psycopg2.extras
import qrcode
import os
import csv
import io
import json
import re
import uuid
import base64
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.units import inch
from functools import wraps
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf import CSRFProtect

# Load environment variables (no-op on Vercel, useful for local dev)
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError("SECRET_KEY environment variable is not set")

# Vercel sits behind a proxy — without this, request.host_url / share links
# come back as http:// and the wrong host, since Flask trusts the raw
# connection info instead of the X-Forwarded-* headers Vercel sets.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Harden session cookies in production, but don't break local http testing
IS_PROD = os.environ.get('FLASK_ENV') != 'development'
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_PROD,
)

# CSRF protection for every state-changing request (form POSTs and the
# JSON fetch() call from the check-in scanner both carry a token — see
# base.html's <meta name="csrf-token"> and script.js).
csrf = CSRFProtect(app)

# Supabase Postgres connection string, e.g.
# postgresql://postgres.xxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres
# IMPORTANT: use Supabase's *pooled* (pgbouncer, port 6543) connection string here,
# not the direct one — Vercel spins up many short-lived function instances, and each
# one opens its own connection, so the direct connection limit gets exhausted fast.
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

# Staff accounts are lower-privilege: they can only reach the QR check-in
# page, not the rest of the admin panel. Seeded the same way as the admin
# account, via env vars, so no one has to hand-create a row in the DB.
STAFF_USERNAME = os.environ.get('STAFF_USERNAME')
STAFF_PASSWORD = os.environ.get('STAFF_PASSWORD')

# ─── Database Setup ────────────────────────────────────────────────────────────

class DBWrapper:
    def __init__(self):
        self.conn = psycopg2.connect(DATABASE_URL, sslmode='require')

    def execute(self, query, params=()):
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

def get_db():
    return DBWrapper()

def init_db():
    conn = get_db()

    conn.execute('''CREATE TABLE IF NOT EXISTS events (
        id SERIAL PRIMARY KEY,
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
        id SERIAL PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        student_name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT NOT NULL,
        college_id TEXT NOT NULL,
        qr_code TEXT,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id SERIAL PRIMARY KEY,
        registration_id INTEGER NOT NULL UNIQUE REFERENCES registrations(id) ON DELETE CASCADE,
        event_id INTEGER NOT NULL,
        student_name TEXT,
        college_id TEXT,
        checked_in_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS admins (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )''')

    # Login lockout bookkeeping — added with IF NOT EXISTS so this also
    # patches databases created before this feature existed.
    conn.execute('ALTER TABLE admins ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0')
    conn.execute('ALTER TABLE admins ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP')

    # Seed / refresh the admin account from env vars. Password is hashed —
    # never stored or compared in plain text.
    if ADMIN_USERNAME and ADMIN_PASSWORD:
        conn.execute(
            '''INSERT INTO admins (username, password) VALUES (%s, %s)
               ON CONFLICT (username) DO UPDATE SET password = EXCLUDED.password''',
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD))
        )

    # Staff table mirrors admins (same lockout bookkeeping), but is a
    # separate table so a staff account can never accidentally end up with
    # admin-panel access just because the code path is shared.
    conn.execute('''CREATE TABLE IF NOT EXISTS staff (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until TIMESTAMP
    )''')

    if STAFF_USERNAME and STAFF_PASSWORD:
        conn.execute(
            '''INSERT INTO staff (username, password) VALUES (%s, %s)
               ON CONFLICT (username) DO UPDATE SET password = EXCLUDED.password''',
            (STAFF_USERNAME, generate_password_hash(STAFF_PASSWORD))
        )

    conn.commit()
    conn.close()

# ─── Auth Decorator ─────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('login', type='admin'))
        return f(*args, **kwargs)
    return decorated

def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('staff_logged_in'):
            return redirect(url_for('login', type='staff'))
        return f(*args, **kwargs)
    return decorated

def checkin_access_required(f):
    """QR check-in is the one page both roles can reach: admins have it as
    part of the full panel, staff have it as their entire dashboard."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not (session.get('admin_logged_in') or session.get('staff_logged_in')):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ─── Login Lockout ───────────────────────────────────────────────────────────
# After this many failed attempts in a row, the account is locked for a
# cooldown period. Storing the counter in the DB (rather than in memory)
# means it works correctly even across the many short-lived serverless
# instances Vercel spins up.
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

# ─── Input Validation ────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHONE_RE = re.compile(r'^[0-9+\-\s()]{7,15}$')

def validate_registration_fields(name, email, phone, college_id):
    """Returns an error string if any field is invalid, else None.
    The HTML form already nudges users with `required`, but that's
    client-side only and trivially bypassed, so everything is re-checked
    here before it touches the database."""
    if not name or len(name) > 120:
        return "Please enter a valid name."
    if not email or len(email) > 254 or not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    if not phone or not PHONE_RE.match(phone):
        return "Please enter a valid phone number."
    if not college_id or len(college_id) > 50:
        return "Please enter a valid college ID / roll number."
    return None

# ─── Lookup helpers (404 instead of a raw crash on a bad ID) ────────────────

def get_event_or_404(conn, event_id):
    event = conn.execute('SELECT * FROM events WHERE id = %s', (event_id,)).fetchone()
    if not event:
        conn.close()
        abort(404, description="Event not found.")
    return event

# ─── QR Code Generation ──────────────────────────────────────────────────────
# Vercel's filesystem is read-only outside /tmp, and /tmp itself doesn't
# persist between invocations of a serverless function — so QR codes can no
# longer be saved as files on disk. Instead we generate them as base64 and
# store that string directly in the registrations row.

def generate_qr_base64(registration_id, student_name, event_id):
    """Generate a QR code and return it as a base64 data URI."""
    data = json.dumps({
        "registration_id": registration_id,
        "student_name": student_name,
        "event_id": event_id
    })
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

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
    event = get_event_or_404(conn, event_id)

    reg_count = conn.execute('SELECT COUNT(*) FROM registrations WHERE event_id = %s', (event_id,)).fetchone()['count']

    if request.method == 'POST':
        name = request.form['name'].strip()
        email = request.form['email'].strip()
        phone = request.form['phone'].strip()
        college_id = request.form['college_id'].strip()

        field_error = validate_registration_fields(name, email, phone, college_id)
        if field_error:
            conn.close()
            return render_template('register.html', event=event, reg_count=reg_count, error=field_error)

        # Lock this event's row for the rest of the transaction. If two
        # people submit the last open spot at the same instant, the second
        # transaction blocks here until the first commits or rolls back —
        # so the capacity check below can never be fooled by a concurrent
        # registration that hasn't landed yet.
        conn.execute('SELECT id FROM events WHERE id = %s FOR UPDATE', (event_id,))

        # Check duplicate
        existing = conn.execute(
            'SELECT id FROM registrations WHERE event_id=%s AND (email=%s OR college_id=%s)',
            (event_id, email, college_id)
        ).fetchone()
        if existing:
            conn.rollback()
            conn.close()
            return render_template('register.html', event=event, reg_count=reg_count,
                                   error="You are already registered for this event!")

        reg_count = conn.execute('SELECT COUNT(*) FROM registrations WHERE event_id = %s', (event_id,)).fetchone()['count']
        if reg_count >= event['max_participants']:
            conn.rollback()
            conn.close()
            return render_template('register.html', event=event, reg_count=reg_count,
                                   error="Event is full. No more registrations allowed.")

        row = conn.execute(
            '''INSERT INTO registrations (event_id, student_name, email, phone, college_id)
               VALUES (%s,%s,%s,%s,%s) RETURNING id''',
            (event_id, name, email, phone, college_id)
        ).fetchone()
        reg_id = row['id']
        qr_code = generate_qr_base64(reg_id, name, event_id)
        conn.execute('UPDATE registrations SET qr_code=%s WHERE id=%s', (qr_code, reg_id))
        conn.commit()
        conn.close()
        return redirect(url_for('confirmation', reg_id=reg_id))

    conn.close()
    return render_template('register.html', event=event, reg_count=reg_count, error=None)

@app.route('/event/<share_link>', methods=['GET', 'POST'])
def register_via_link(share_link):
    """Register for an event using a shareable link"""
    conn = get_db()
    event = conn.execute('SELECT * FROM events WHERE share_link = %s', (share_link,)).fetchone()
    conn.close()
    if not event:
        abort(404, description="Event not found.")

    # Redirect to normal registration
    return redirect(url_for('register', event_id=event['id']))

@app.route('/confirmation/<int:reg_id>')
def confirmation(reg_id):
    conn = get_db()
    reg = conn.execute('''
        SELECT r.*, e.name as event_name, e.date, e.time, e.venue
        FROM registrations r JOIN events e ON r.event_id = e.id
        WHERE r.id = %s
    ''', (reg_id,)).fetchone()
    conn.close()
    if not reg:
        abort(404, description="Registration not found.")
    return render_template('confirmation.html', reg=reg)

@app.route('/qr/<int:reg_id>')
def get_qr(reg_id):
    conn = get_db()
    reg = conn.execute('SELECT qr_code FROM registrations WHERE id=%s', (reg_id,)).fetchone()
    conn.close()
    if reg and reg['qr_code']:
        header, encoded = reg['qr_code'].split(',', 1)
        img_bytes = base64.b64decode(encoded)
        return send_file(io.BytesIO(img_bytes), mimetype='image/png')
    return "QR not found", 404

# ─── Auth (Admin + Staff) ────────────────────────────────────────────────────
# One shared login page with a role dropdown. Admin and staff live in
# separate tables (see init_db) so the two roles can never blur together —
# staff sessions only ever unlock the QR check-in page.

def _attempt_login(role, username, password):
    """Runs the lockout-aware credential check against the admins or staff
    table and returns (success, error_message)."""
    table = 'admins' if role == 'admin' else 'staff'
    conn = get_db()
    account = conn.execute(f'SELECT * FROM {table} WHERE username=%s', (username,)).fetchone()

    now = datetime.utcnow()
    if account and account['locked_until'] and account['locked_until'] > now:
        wait_min = max(1, int((account['locked_until'] - now).total_seconds() // 60) + 1)
        conn.close()
        return False, f"Too many failed attempts. Try again in {wait_min} minute(s)."

    if account and check_password_hash(account['password'], password):
        conn.execute(
            f'UPDATE {table} SET failed_attempts=0, locked_until=NULL WHERE id=%s',
            (account['id'],)
        )
        conn.commit()
        conn.close()
        return True, None

    if account:
        attempts = account['failed_attempts'] + 1
        locked_until = now + timedelta(minutes=LOCKOUT_MINUTES) if attempts >= MAX_LOGIN_ATTEMPTS else None
        conn.execute(
            f'UPDATE {table} SET failed_attempts=%s, locked_until=%s WHERE id=%s',
            (attempts, locked_until, account['id'])
        )
        conn.commit()
    conn.close()
    return False, "Invalid credentials"

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    if session.get('staff_logged_in'):
        return redirect(url_for('checkin_page'))

    error = None
    # Which option the dropdown should show selected: from the query string
    # on a fresh GET (e.g. a staff_required redirect), or from the form on
    # a failed POST so the user doesn't lose their selection.
    login_type = request.values.get('type', 'admin')
    if login_type not in ('admin', 'staff'):
        login_type = 'admin'

    if request.method == 'POST':
        login_type = request.form.get('login_type', 'admin')
        if login_type not in ('admin', 'staff'):
            login_type = 'admin'
        username = request.form['username']
        password = request.form['password']

        success, err = _attempt_login(login_type, username, password)
        if success:
            if login_type == 'admin':
                session['admin_logged_in'] = True
                session['admin_username'] = username
                return redirect(url_for('admin_dashboard'))
            else:
                session['staff_logged_in'] = True
                session['staff_username'] = username
                return redirect(url_for('checkin_page'))
        error = err

    return render_template('login.html', error=error, login_type=login_type)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    # Legacy URL, kept so old bookmarks/links still work — the login page
    # now handles both admin and staff sign-in.
    if request.method == 'POST':
        return login()
    return redirect(url_for('login', type='admin'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin/logout')
def admin_logout():
    return logout()

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

def validate_event_fields(name, description, venue, max_participants_raw):
    """Returns an error string if any field is invalid, else None."""
    if not name or len(name) > 150:
        return "Please enter an event name (under 150 characters)."
    if description and len(description) > 2000:
        return "Description is too long (max 2000 characters)."
    if not venue or len(venue) > 150:
        return "Please enter a venue (under 150 characters)."
    try:
        max_p = int(max_participants_raw)
    except (TypeError, ValueError):
        return "Max participants must be a whole number."
    if max_p < 1 or max_p > 100000:
        return "Max participants must be between 1 and 100,000."
    return None

@app.route('/admin/event/create', methods=['GET', 'POST'])
@admin_required
def create_event():
    if request.method == 'POST':
        name = request.form['name'].strip()
        description = request.form.get('description', '').strip()
        date = request.form['date']
        time = request.form['time']
        venue = request.form['venue'].strip()
        max_participants_raw = request.form['max_participants']

        field_error = validate_event_fields(name, description, venue, max_participants_raw)
        if field_error:
            flash(field_error, "error")
            return render_template('event_form.html', event=None, action='Create')

        try:
            event_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            flash("Please enter a valid date.", "error")
            return render_template('event_form.html', event=None, action='Create')
        today = datetime.today().date()

        if event_date < today:
            flash("Event date cannot be in the past!", "error")
            return render_template(
                'event_form.html',
                event=None,
                action='Create'
            )
        max_p = int(max_participants_raw)
        share_link = str(uuid.uuid4())[:8]  # Generate unique 8-char code
        conn = get_db()
        existing = conn.execute(
            '''
            SELECT id FROM events
            WHERE name = %s
            AND date = %s
            AND time = %s
            AND venue = %s
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
        conn.execute('INSERT INTO events (name, description, date, time, venue, max_participants, share_link) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                     (name, description, date, time, venue, max_p, share_link))
        conn.commit()
        conn.close()
        return redirect(url_for('admin_dashboard'))
    return render_template('event_form.html', event=None, action='Create')

@app.route('/admin/event/edit/<int:event_id>', methods=['GET', 'POST'])
@admin_required
def edit_event(event_id):
    conn = get_db()
    event = get_event_or_404(conn, event_id)
    if request.method == 'POST':
        name = request.form['name'].strip()
        description = request.form.get('description', '').strip()
        date = request.form['date']
        time = request.form['time']
        venue = request.form['venue'].strip()
        max_participants_raw = request.form['max_participants']

        field_error = validate_event_fields(name, description, venue, max_participants_raw)
        if field_error:
            conn.close()
            flash(field_error, "error")
            return render_template('event_form.html', event=event, action='Edit')

        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            conn.close()
            flash("Please enter a valid date.", "error")
            return render_template('event_form.html', event=event, action='Edit')

        conn.execute('''UPDATE events SET name=%s, description=%s, date=%s, time=%s, venue=%s, max_participants=%s
                        WHERE id=%s''',
                     (name, description, date, time, venue, int(max_participants_raw), event_id))
        conn.commit()
        conn.close()
        return redirect(url_for('admin_dashboard'))
    conn.close()
    return render_template('event_form.html', event=event, action='Edit')

@app.route('/admin/event/delete/<int:event_id>', methods=['POST'])
@admin_required
def delete_event(event_id):
    conn = get_db()
    get_event_or_404(conn, event_id)
    # registrations/attendance clean up automatically via ON DELETE CASCADE,
    # but deleting explicitly first keeps this safe even if the FK is ever removed.
    conn.execute('DELETE FROM attendance WHERE event_id=%s', (event_id,))
    conn.execute('DELETE FROM registrations WHERE event_id=%s', (event_id,))
    conn.execute('DELETE FROM events WHERE id=%s', (event_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/event/<int:event_id>/registrations')
@admin_required
def event_registrations(event_id):
    conn = get_db()
    event = get_event_or_404(conn, event_id)
    registrations = conn.execute('''
        SELECT r.*, 
               CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END as checked_in,
               a.checked_in_at
        FROM registrations r LEFT JOIN attendance a ON r.id = a.registration_id
        WHERE r.event_id = %s
        ORDER BY r.registered_at DESC
    ''', (event_id,)).fetchall()
    conn.close()
    return render_template('registrations.html', event=event, registrations=registrations)

# ─── QR Entry / Check-in ─────────────────────────────────────────────────────

@app.route('/admin/checkin')
@checkin_access_required
def checkin_page():
    conn = get_db()
    events = conn.execute('SELECT * FROM events ORDER BY date DESC').fetchall()
    conn.close()
    return render_template('checkin.html', events=events)

@app.route('/admin/checkin/verify', methods=['POST'])
@checkin_access_required
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
        WHERE r.id = %s
    ''', (reg_id,)).fetchone()

    if not reg:
        conn.close()
        return jsonify({'status': 'invalid', 'message': '❌ Registration not found'})

    already = conn.execute('SELECT id FROM attendance WHERE registration_id=%s', (reg_id,)).fetchone()
    if already:
        conn.close()
        return jsonify({'status': 'duplicate', 'message': f'⚠️ {reg["student_name"]} already checked in!'})

    conn.execute('''INSERT INTO attendance (registration_id, event_id, student_name, college_id)
                    VALUES (%s,%s,%s,%s)''',
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
    event = get_event_or_404(conn, event_id)
    attendance = conn.execute('''
        SELECT a.*, r.email, r.phone
        FROM attendance a JOIN registrations r ON a.registration_id = r.id
        WHERE a.event_id = %s
        ORDER BY a.checked_in_at DESC
    ''', (event_id,)).fetchall()
    conn.close()
    return render_template('attendance.html', event=event, attendance=attendance)

# ─── Export Routes ───────────────────────────────────────────────────────────

@app.route('/admin/event/<int:event_id>/export/csv')
@admin_required
def export_csv(event_id):
    conn = get_db()
    event = get_event_or_404(conn, event_id)
    rows = conn.execute('''
        SELECT r.student_name, r.email, r.phone, r.college_id, r.registered_at,
               CASE WHEN a.id IS NOT NULL THEN 'Yes' ELSE 'No' END as attended,
               a.checked_in_at
        FROM registrations r LEFT JOIN attendance a ON r.id = a.registration_id
        WHERE r.event_id = %s
    ''', (event_id,)).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Email', 'Phone', 'Roll Number', 'Registered At', 'Attended', 'Check-in Time'])
    for row in rows:
        writer.writerow(list(row.values()))
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode()),
                     mimetype='text/csv',
                     as_attachment=True,
                     download_name=f'{event["name"]}_attendance.csv')

@app.route('/admin/event/<int:event_id>/export/pdf')
@admin_required
def export_pdf(event_id):
    conn = get_db()
    event = get_event_or_404(conn, event_id)
    rows = conn.execute('''
        SELECT r.student_name, r.college_id, r.email,
               CASE WHEN a.id IS NOT NULL THEN 'Present' ELSE 'Absent' END as status,
               COALESCE(a.checked_in_at::text, '-') as check_in
        FROM registrations r LEFT JOIN attendance a ON r.id = a.registration_id
        WHERE r.event_id = %s
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
        table_data.append(list(row.values()))

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
    reg = conn.execute('SELECT COUNT(*) FROM registrations WHERE event_id=%s', (event_id,)).fetchone()['count']
    att = conn.execute('SELECT COUNT(*) FROM attendance WHERE event_id=%s', (event_id,)).fetchone()['count']
    conn.close()
    return jsonify({'registrations': reg, 'attendance': att})

# ─── Error Handlers ──────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    message = getattr(e, 'description', None) or "The page you're looking for doesn't exist."
    return render_template('not_found.html', message=message), 404

# Initialize the database (creates tables if missing, refreshes admin account)
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))