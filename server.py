import mimetypes
import os
import sqlite3
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

mimetypes.add_type('image/avif', '.avif')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'yoga_classes.db')

app = Flask(__name__)
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
    con.commit()
    con.close()


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def normalize_name(name):
    """Lowercase English names; leave Hebrew names unchanged."""
    # If the name contains any Hebrew character, treat it as Hebrew
    if any('\u0590' <= c <= '\u05FF' or '\uFB1D' <= c <= '\uFB4F' for c in name):
        return name
    return name.lower()


def normalize_phone(phone):
    """Return digits-only string with +972 country code prefix."""
    digits = ''.join(c for c in phone if c.isdigit())
    # +972XXXXXXXXX → already has country code as digits (972...)
    if digits.startswith('972'):
        return '+' + digits
    # 0XXXXXXXXX → strip leading 0, prepend +972
    if digits.startswith('0'):
        return '+972' + digits[1:]
    return '+972' + digits


def make_user_key(name, phone):
    return normalize_name(name) + normalize_phone(phone)


def upsert_user(con, name, phone):
    """Insert user if new (keyed by name+phone), or increment lesson counter."""
    name     = normalize_name(name)
    phone    = normalize_phone(phone)
    user_key = name + phone
    con.execute('''
        INSERT INTO users (user_key, name, phone, lesson_count, last_seen)
        VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(user_key) DO UPDATE SET
            lesson_count = lesson_count + 1,
            last_seen    = CURRENT_TIMESTAMP
    ''', (user_key, name, phone))


def decrement_user(con, name, phone):
    """Decrement lesson counter; remove user row if it reaches zero."""
    user_key = make_user_key(name, phone)
    con.execute('''
        UPDATE users SET lesson_count = lesson_count - 1
        WHERE user_key = ?
    ''', (user_key,))
    con.execute('DELETE FROM users WHERE user_key = ? AND lesson_count <= 0', (user_key,))


# ── Page ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── API ───────────────────────────────────────────────────────────────────────

@app.route('/api/registrations', methods=['GET'])
def get_registrations():
    con  = get_db()
    rows = con.execute(
        'SELECT * FROM registrations ORDER BY created_at DESC'
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/register', methods=['POST'])
def register():
    data       = request.get_json(silent=True) or {}
    name       = str(data.get('name',       '')).strip()
    phone      = str(data.get('phone',      '')).strip()
    class_date = str(data.get('class_date', '')).strip()

    if not name or not phone or not class_date:
        return jsonify({'error': 'שדות חסרים'}), 400

    con = get_db()
    con.execute(
        'INSERT INTO registrations (name, phone, class_date) VALUES (?, ?, ?)',
        (name, phone, class_date)
    )
    upsert_user(con, name, phone)
    con.commit()
    con.close()
    return jsonify({'ok': True}), 201


@app.route('/api/unregister/<int:reg_id>', methods=['DELETE'])
def unregister(reg_id):
    con = get_db()
    row = con.execute(
        'SELECT name, phone FROM registrations WHERE id = ?', (reg_id,)
    ).fetchone()
    if row:
        con.execute('DELETE FROM registrations WHERE id = ?', (reg_id,))
        decrement_user(con, row['name'], row['phone'])
        con.commit()
    con.close()
    return jsonify({'ok': True})


@app.route('/api/users', methods=['GET'])
def get_users():
    con  = get_db()
    rows = con.execute(
        'SELECT * FROM users ORDER BY lesson_count DESC, last_seen DESC'
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print('השרת פועל על http://localhost:5000')
    app.run(debug=True, port=5000)
