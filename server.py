import mimetypes
import os
import sqlite3
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

# Ensure .avif is recognised on Windows
mimetypes.add_type('image/avif', '.avif')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'yoga_classes.db')

app = Flask(__name__)   # Flask auto-serves static/ at /static and templates/ for render_template
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
    con.commit()
    con.close()


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


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
    con.commit()
    con.close()
    return jsonify({'ok': True}), 201


@app.route('/api/unregister/<int:reg_id>', methods=['DELETE'])
def unregister(reg_id):
    con = get_db()
    con.execute('DELETE FROM registrations WHERE id = ?', (reg_id,))
    con.commit()
    con.close()
    return jsonify({'ok': True})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print('השרת פועל על http://localhost:5000')
    app.run(debug=True, port=5000)
