import logging
import mimetypes
import os
import re
import secrets
from datetime import date
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
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


def get_or_create_secret_key():
    # Prefer an explicit env var (required on Railway where the filesystem is ephemeral)
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key.encode()
    # Fall back to a persisted file for local development
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

class _Conn:
    """Thin wrapper around a psycopg2 connection.

    Exposes the same con.execute(sql, params).fetch*() pattern used throughout
    the codebase so that the route handlers need no structural changes.
    """
    def __init__(self):
        url = os.environ.get('DATABASE_URL')
        if not url:
            raise RuntimeError(
                'DATABASE_URL environment variable is not set. '
                'In Railway: add a PostgreSQL service, then add a '
                'DATABASE_URL variable referencing ${{Postgres.DATABASE_URL}}.'
            )
        self._con = psycopg2.connect(
            url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )

    def execute(self, sql, params=()):
        cur = self._con.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._con.commit()

    def close(self):
        self._con.close()


def init_db():
    con = _Conn()
    try:
        con.execute('''
            CREATE TABLE IF NOT EXISTS registrations (
                id         SERIAL PRIMARY KEY,
                name       TEXT    NOT NULL,
                phone      TEXT    NOT NULL,
                class_date TEXT    NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id             SERIAL PRIMARY KEY,
                user_key       TEXT    NOT NULL UNIQUE,
                name           TEXT    NOT NULL,
                phone          TEXT    NOT NULL,
                lesson_count   INTEGER NOT NULL DEFAULT 0,
                admissions     INTEGER NOT NULL DEFAULT 0,
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
        # Idempotent migration: add admissions column to existing deployments
        con.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS admissions INTEGER NOT NULL DEFAULT 0
        ''')
        # Seed default admin if not present
        con.execute('''
            INSERT INTO admins (username, password_hash)
            VALUES (%s, %s)
            ON CONFLICT (username) DO NOTHING
        ''', ('dhdarm_admin', generate_password_hash('sheshet')))
        con.commit()
    finally:
        con.close()


def get_db():
    return _Conn()


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
        VALUES (%s, %s, %s, 1, 0, CURRENT_TIMESTAMP)
        ON CONFLICT (user_key) DO UPDATE SET
            lesson_count = users.lesson_count + 1,
            admissions   = users.admissions - 1,
            last_seen    = CURRENT_TIMESTAMP
    ''', (user_key, name, phone))


def decrement_user(con, name, phone):
    user_key = make_user_key(name, phone)
    con.execute(
        'UPDATE users SET lesson_count = lesson_count - 1, admissions = admissions + 1 WHERE user_key = %s',
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
    today = date.today()
    show_friday = date(2026, 4, 20) <= today <= date(2026, 4, 26)
    return render_template('index.html', show_friday=show_friday)


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
            'SELECT 1 FROM registrations WHERE class_date = %s AND phone = %s',
            (class_date, normalize_phone(phone))
        ).fetchone()
        if already:
            return jsonify({'error': 'כבר רשומ/ה לשיעור זה'}), 409
        count = con.execute(
            'SELECT COUNT(*) AS cnt FROM registrations WHERE class_date = %s',
            (class_date,)
        ).fetchone()['cnt']
        if count >= 15:
            return jsonify({'error': 'class_full'}), 409
        con.execute(
            'INSERT INTO registrations (name, phone, class_date) VALUES (%s, %s, %s)',
            (name, phone, class_date)
        )
        upsert_user(con, name, phone)
        user_key = make_user_key(name, phone)
        row = con.execute(
            'SELECT admissions FROM users WHERE user_key = %s', (user_key,)
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
            'SELECT name, phone FROM registrations WHERE id = %s', (reg_id,)
        ).fetchone()
        if row:
            con.execute('DELETE FROM registrations WHERE id = %s', (reg_id,))
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
                'SELECT password_hash FROM admins WHERE username = %s', (username,)
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
            'SELECT name, phone FROM registrations WHERE id = %s', (reg_id,)
        ).fetchone()
        if not old:
            return jsonify({'error': 'לא נמצא'}), 404
        decrement_user(con, old['name'], old['phone'])
        con.execute(
            'UPDATE registrations SET name=%s, phone=%s, class_date=%s WHERE id=%s',
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
            'SELECT name, phone FROM registrations WHERE id = %s', (reg_id,)
        ).fetchone()
        if row:
            con.execute('DELETE FROM registrations WHERE id = %s', (reg_id,))
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
            'SELECT id FROM users WHERE user_key = %s AND id != %s', (new_key, user_id)
        ).fetchone()
        if conflict:
            return jsonify({'error': 'משתמש עם מפתח זה כבר קיים'}), 409
        con.execute(
            'UPDATE users SET user_key=%s, name=%s, phone=%s, lesson_count=%s, admissions=%s WHERE id=%s',
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
            'SELECT id, name, phone FROM registrations WHERE class_date = %s',
            (class_date,)
        ).fetchall()
        cancelled = [dict(r) for r in rows]
        for row in rows:
            con.execute('DELETE FROM registrations WHERE id = %s', (row['id'],))
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
        con.execute('DELETE FROM users WHERE id = %s', (user_id,))
        con.commit()
        return jsonify({'ok': True})
    finally:
        con.close()


# ── Entry point ───────────────────────────────────────────────────────────────

# Initialise the DB on the first request so this never runs at import/build time.
_db_ready = False

@app.before_request
def _ensure_db():
    global _db_ready
    if not _db_ready:
        try:
            init_db()
            _db_ready = True
            log.info('Database initialised successfully')
        except Exception as exc:
            log.exception('Database initialisation failed: %s', exc)
            raise


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'השרת פועל על http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
