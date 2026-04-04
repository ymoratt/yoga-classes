import mimetypes
import os
import re
import secrets
import sqlite3
from functools import wraps
from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

mimetypes.add_type('image/avif', '.avif')

# Hebrew + Latin letters, spaces, hyphens and apostrophes only
NAME_RE = re.compile(r"^[\u0590-\u05FF\uFB1D-\uFB4Fa-zA-Z\s'\-]+$")

def valid_name(name):
    return bool(name and NAME_RE.match(name))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'yoga_classes.db')


def get_or_create_secret_key():
    key_path = os.path.join(BASE_DIR, '.secret_key')
    if os.path.exists(key_path):
        with open(key_path, 'rb') as f:
            return f.read()
    key = secrets.token_bytes(32)
    with open(key_path, 'wb') as f:
        f.write(key)
    return key


app = Flask(__name__)
app.secret_key = get_or_create_secret_key()
CORS(app)


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute('''
        CREATE TABLE IF NOT EXISTS registrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            phone      TEXT    NOT NULL,
            class_date TEXT    NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    con.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key       TEXT    NOT NULL UNIQUE,
            name           TEXT    NOT NULL,
            phone          TEXT    NOT NULL,
            lesson_count   INTEGER NOT NULL DEFAULT 0,
            first_seen     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    con.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL
        )
    ''')
    # Seed default admin if not present
    exists = con.execute(
        'SELECT 1 FROM admins WHERE username = ?', ('dhdarm_admin',)
    ).fetchone()
    if not exists:
        con.execute(
            'INSERT INTO admins (username, password_hash) VALUES (?, ?)',
            ('dhdarm_admin', generate_password_hash('sheshet'))
        )
    # Migration: rebuild users table if it has stale UNIQUE on phone or missing user_key
    cols      = {row[1] for row in con.execute('PRAGMA table_info(users)')}
    indexes   = con.execute('PRAGMA index_list(users)').fetchall()
    phone_unique = any(
        row[2] == 1  # unique flag
        and con.execute(f'PRAGMA index_info("{row[1]}")').fetchone()[2] == 'phone'
        for row in indexes
    )
    if 'user_key' not in cols or phone_unique:
        con.execute('ALTER TABLE users RENAME TO users_old')
        con.execute('''
            CREATE TABLE users (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key       TEXT    NOT NULL UNIQUE,
                name           TEXT    NOT NULL,
                phone          TEXT    NOT NULL,
                lesson_count   INTEGER NOT NULL DEFAULT 0,
                first_seen     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        for row in con.execute('SELECT * FROM users_old').fetchall():
            d = dict(row)
            key = d.get('user_key') or (normalize_name(d['name']) + normalize_phone(d['phone']))
            con.execute('''
                INSERT INTO users (user_key, name, phone, lesson_count, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key) DO UPDATE SET
                    lesson_count = MAX(lesson_count, excluded.lesson_count),
                    last_seen    = excluded.last_seen
            ''', (key, d['name'], d['phone'], d['lesson_count'],
                  d.get('first_seen'), d.get('last_seen')))
        con.execute('DROP TABLE users_old')
    # Migration: add admissions column if missing
    cols = {row[1] for row in con.execute('PRAGMA table_info(users)')}
    if 'admissions' not in cols:
        con.execute('ALTER TABLE users ADD COLUMN admissions INTEGER NOT NULL DEFAULT 0')
    con.commit()
    con.close()


def get_db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    return con


# ── Normalisation helpers ─────────────────────────────────────────────────────

def normalize_name(name):
    if any('\u0590' <= c <= '\u05FF' or '\uFB1D' <= c <= '\uFB4F' for c in name):
        return name
    return name.lower()


def normalize_phone(phone):
    digits = ''.join(c for c in phone if c.isdigit())
    if digits.startswith('972'):
        return '+' + digits
    if digits.startswith('0'):
        return '+972' + digits[1:]
    return '+972' + digits


def make_user_key(name, phone):
    return normalize_name(name) + normalize_phone(phone)


def upsert_user(con, name, phone):
    name     = normalize_name(name)
    phone    = normalize_phone(phone)
    user_key = name + phone
    con.execute('''
        INSERT INTO users (user_key, name, phone, lesson_count, admissions, last_seen)
        VALUES (?, ?, ?, 1, 0, CURRENT_TIMESTAMP)
        ON CONFLICT(user_key) DO UPDATE SET
            lesson_count = lesson_count + 1,
            admissions   = admissions - 1,
            last_seen    = CURRENT_TIMESTAMP
    ''', (user_key, name, phone))


def decrement_user(con, name, phone):
    user_key = make_user_key(name, phone)
    con.execute(
        'UPDATE users SET lesson_count = lesson_count - 1, admissions = admissions + 1 WHERE user_key = ?',
        (user_key,)
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


# ── Public page ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── Public API ────────────────────────────────────────────────────────────────

@app.route('/api/registrations', methods=['GET'])
def get_registrations():
    con = get_db()
    try:
        rows = con.execute('SELECT * FROM registrations ORDER BY created_at DESC').fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        con.close()


@app.route('/api/register', methods=['POST'])
def register():
    data       = request.get_json(silent=True) or {}
    name       = str(data.get('name',       '')).strip()
    phone      = str(data.get('phone',      '')).strip()
    class_date = str(data.get('class_date', '')).strip()
    if not name or not phone or not class_date:
        return jsonify({'error': 'שדות חסרים'}), 400
    if not valid_name(name):
        return jsonify({'error': 'השם יכול להכיל אותיות בעברית או באנגלית בלבד'}), 400
    con = get_db()
    try:
        already = con.execute(
            'SELECT 1 FROM registrations WHERE class_date = ? AND phone = ?',
            (class_date, normalize_phone(phone))
        ).fetchone()
        if already:
            return jsonify({'error': 'כבר רשומ/ה לשיעור זה'}), 409
        con.execute(
            'INSERT INTO registrations (name, phone, class_date) VALUES (?, ?, ?)',
            (name, phone, class_date)
        )
        upsert_user(con, name, phone)
        user_key = make_user_key(name, phone)
        row = con.execute(
            'SELECT admissions FROM users WHERE user_key = ?', (user_key,)
        ).fetchone()
        admissions = row['admissions'] if row else None
        con.commit()
        return jsonify({'ok': True, 'admissions': admissions}), 201
    finally:
        con.close()


@app.route('/api/unregister/<int:reg_id>', methods=['DELETE'])
def unregister(reg_id):
    con = get_db()
    try:
        row = con.execute(
            'SELECT name, phone FROM registrations WHERE id = ?', (reg_id,)
        ).fetchone()
        if row:
            con.execute('DELETE FROM registrations WHERE id = ?', (reg_id,))
            decrement_user(con, row['name'], row['phone'])
            con.commit()
        return jsonify({'ok': True})
    finally:
        con.close()


@app.route('/api/users', methods=['GET'])
def get_users():
    con = get_db()
    try:
        rows = con.execute(
            'SELECT * FROM users ORDER BY lesson_count DESC, last_seen DESC'
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        con.close()


# ── Admin pages ───────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        con = get_db()
        try:
            row = con.execute(
                'SELECT password_hash FROM admins WHERE username = ?', (username,)
            ).fetchone()
        finally:
            con.close()
        if row and check_password_hash(row['password_hash'], password):
            session['admin'] = username
            return redirect(url_for('admin_dashboard'))
        error = 'שם משתמש או סיסמה שגויים.'
    return render_template('admin_login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin_dashboard():
    return render_template('admin.html')


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.route('/admin/api/registrations', methods=['GET'])
@admin_required
def admin_get_registrations():
    con = get_db()
    try:
        rows = con.execute('SELECT * FROM registrations ORDER BY created_at DESC').fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        con.close()


@app.route('/admin/api/registrations/<int:reg_id>', methods=['PUT'])
@admin_required
def admin_update_registration(reg_id):
    data       = request.get_json(silent=True) or {}
    name       = str(data.get('name',       '')).strip()
    phone      = str(data.get('phone',      '')).strip()
    class_date = str(data.get('class_date', '')).strip()
    if not name or not phone or not class_date:
        return jsonify({'error': 'שדות חסרים'}), 400
    if not valid_name(name):
        return jsonify({'error': 'השם יכול להכיל אותיות בעברית או באנגלית בלבד'}), 400
    con = get_db()
    try:
        old = con.execute(
            'SELECT name, phone FROM registrations WHERE id = ?', (reg_id,)
        ).fetchone()
        if not old:
            return jsonify({'error': 'לא נמצא'}), 404
        # Adjust user counters: undo old, apply new
        decrement_user(con, old['name'], old['phone'])
        con.execute(
            'UPDATE registrations SET name=?, phone=?, class_date=? WHERE id=?',
            (name, phone, class_date, reg_id)
        )
        upsert_user(con, name, phone)
        con.commit()
        return jsonify({'ok': True})
    finally:
        con.close()


@app.route('/admin/api/registrations/<int:reg_id>', methods=['DELETE'])
@admin_required
def admin_delete_registration(reg_id):
    con = get_db()
    try:
        row = con.execute(
            'SELECT name, phone FROM registrations WHERE id = ?', (reg_id,)
        ).fetchone()
        if row:
            con.execute('DELETE FROM registrations WHERE id = ?', (reg_id,))
            decrement_user(con, row['name'], row['phone'])
            con.commit()
        return jsonify({'ok': True})
    finally:
        con.close()


@app.route('/admin/api/users', methods=['GET'])
@admin_required
def admin_get_users():
    con = get_db()
    try:
        rows = con.execute(
            'SELECT * FROM users ORDER BY lesson_count DESC, last_seen DESC'
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        con.close()


@app.route('/admin/api/users/<int:user_id>', methods=['PUT'])
@admin_required
def admin_update_user(user_id):
    data       = request.get_json(silent=True) or {}
    name       = str(data.get('name',         '')).strip()
    phone      = str(data.get('phone',        '')).strip()
    count      = data.get('lesson_count')
    admissions = data.get('admissions')
    if not name or not phone or count is None or admissions is None:
        return jsonify({'error': 'שדות חסרים'}), 400
    if not valid_name(name):
        return jsonify({'error': 'השם יכול להכיל אותיות בעברית או באנגלית בלבד'}), 400
    new_key = make_user_key(name, phone)
    con = get_db()
    try:
        conflict = con.execute(
            'SELECT id FROM users WHERE user_key = ? AND id != ?', (new_key, user_id)
        ).fetchone()
        if conflict:
            return jsonify({'error': 'משתמש עם מפתח זה כבר קיים'}), 409
        con.execute(
            'UPDATE users SET user_key=?, name=?, phone=?, lesson_count=?, admissions=? WHERE id=?',
            (new_key, normalize_name(name), normalize_phone(phone), int(count), int(admissions), user_id)
        )
        con.commit()
        return jsonify({'ok': True})
    finally:
        con.close()


@app.route('/admin/api/cancel-class', methods=['POST'])
@admin_required
def admin_cancel_class():
    data       = request.get_json(silent=True) or {}
    class_date = str(data.get('class_date', '')).strip()
    if not class_date:
        return jsonify({'error': 'שדה חסר'}), 400
    con = get_db()
    try:
        rows = con.execute(
            'SELECT id, name, phone FROM registrations WHERE class_date = ?',
            (class_date,)
        ).fetchall()
        cancelled = [dict(r) for r in rows]
        for row in rows:
            con.execute('DELETE FROM registrations WHERE id = ?', (row['id'],))
            decrement_user(con, row['name'], row['phone'])
        con.commit()
        return jsonify({'ok': True, 'cancelled': cancelled})
    finally:
        con.close()


@app.route('/admin/api/users/<int:user_id>', methods=['DELETE'])
@admin_required
def admin_delete_user(user_id):
    con = get_db()
    try:
        con.execute('DELETE FROM users WHERE id = ?', (user_id,))
        con.commit()
        return jsonify({'ok': True})
    finally:
        con.close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print('השרת פועל על http://localhost:5000')
    app.run(debug=True, port=5000)
