import sqlite3
import os
import json
import threading
from datetime import datetime, timezone

BASE_DIR = os.path.join(os.getenv("LOCALAPPDATA"), "MyDesktopApp")
os.makedirs(BASE_DIR, exist_ok=True)
DB_FILE = os.path.join(BASE_DIR, "local_data.db")

# Thread-safe lock
db_lock = threading.Lock()

def get_connection():
    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False,
        timeout=30  # 30 sec tak wait karega lock release hone ka
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")       # ✅ WAL mode: Read aur Write saath chal sake
    conn.execute("PRAGMA synchronous = NORMAL")     # ✅ Performance better hogi
    conn.execute("PRAGMA busy_timeout = 30000")     # ✅ 30 sec busy timeout
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()

    # client_info table (multi-site)
    c.execute("""
        CREATE TABLE IF NOT EXISTS client_info (
            client_id TEXT PRIMARY KEY,
            site_id1 TEXT,
            site_id2 TEXT,
            site_id3 TEXT,
            site_id4 TEXT
        )
    """)

    # sensor_info table
    c.execute("""
        CREATE TABLE IF NOT EXISTS sensor_info (
            sensor_id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id TEXT,
            sensor_name TEXT,
            sensor_location TEXT,
            api TEXT,
            polling_interval INTEGER
        )
    """)

    # sensor_data table — timestamp ab TEXT mein UTC format mein save hoga
    c.execute("""
        CREATE TABLE IF NOT EXISTS sensor_data (
            raw_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id INTEGER,
            raw_value REAL,
            json_payload TEXT,
            timestamp TEXT,
            FOREIGN KEY(sensor_id) REFERENCES sensor_info(sensor_id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()

init_db()

# ---------------- CLIENT ----------------
def save_client_info(client_id, site_ids):
    with db_lock:
        conn = get_connection()
        try:
            c = conn.cursor()
            sites = site_ids + [None] * (4 - len(site_ids))
            c.execute("""
                INSERT INTO client_info (client_id, site_id1, site_id2, site_id3, site_id4)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    site_id1=excluded.site_id1,
                    site_id2=excluded.site_id2,
                    site_id3=excluded.site_id3,
                    site_id4=excluded.site_id4
            """, (client_id, sites[0], sites[1], sites[2], sites[3]))
            conn.commit()
        finally:
            conn.close()

# ---------------- SENSOR ----------------
def create_sensor(site_id, name, location, api, interval):
    with db_lock:
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("""
                INSERT INTO sensor_info (site_id, sensor_name, sensor_location, api, polling_interval)
                VALUES (?, ?, ?, ?, ?)
            """, (site_id, name, location, api, interval))
            conn.commit()
            sensor_id = c.lastrowid
        finally:
            conn.close()
    return sensor_id

def save_sensor_data(sensor_id, raw_value, json_payload):
    with db_lock:
        conn = get_connection()
        try:
            c = conn.cursor()

            # ✅ UTC+0 timestamp generate karo jab value fetch ho
            utc_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

            c.execute("""
                INSERT INTO sensor_data (sensor_id, raw_value, json_payload, timestamp)
                VALUES (?, ?, ?, ?)
            """, (sensor_id, raw_value, json.dumps(json_payload), utc_timestamp))
            conn.commit()
        finally:
            conn.close()

def get_latest_sensor_value(sensor_id):
    # ✅ Read operation - WAL mode ki wajah se lock nahi lagega
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT raw_value FROM sensor_data
            WHERE sensor_id=?
            ORDER BY raw_id DESC
            LIMIT 1
        """, (sensor_id,))
        row = c.fetchone()
    finally:
        conn.close()
    return row[0] if row else None