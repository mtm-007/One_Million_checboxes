import os 
import sqlite3
import threading 
from uuid import uuid4

DB_FILE = os.environ.get("DB_FILE", "sqlite3_database.db")
_local = threading.local()

def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(
            DB_FILE, 
            check_same_thread=False, # Required for thread-local reuse in webapps
            timeout=15.0)            #prevents hainging of db is locked
        
        _local.conn.row_factory= sqlite3.Row
        # WAL mode allows multiple readers and one writer at the same time
        _local.conn.execute("PRAGMA journal_mode=WAL")  #better concurrency,
        _local.conn.execute("PRAGMA foreign_keys=ON")   #enforces FKs if added later
    return _local.conn

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        #table for content/prompts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content (
                file_id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT,
                image_url TEXT
            )
        """)
        #ordes table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                session_id TEXT PRIMARY KEY,
                file_id TEXT,
                email TEXT NOT NULL,
                processed INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        # conn.close()
    print("Database initialized successfully.")

def add_content(file_id, email, prompt):
    conn = get_conn()
    #unique_id = str(uuid4())
    conn.execute(
        "INSERT INTO content (file_id, email, prompt, status) VALUES (?, ?, ?,?)",
        (file_id, email, prompt, "pending")
    )
    conn.commit()
    return file_id

def get_content(file_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM content WHERE file_id= ?", (file_id,)).fetchone()
    return dict(row) if row else None

def update_content_image(file_id, image_url):
    conn = get_conn()
    conn.execute("UPDATE content SET image_url = ?, status = ? WHERE file_id = ?",
              (image_url, "complete", file_id))
    conn.commit()
   
def add_order(session_id, file_id, email):
    conn = get_conn()
    conn.execute("INSERT INTO orders (session_id, file_id, email, processed) VALUES (?,?,?,?)",
              (session_id, file_id,email, 0))
    conn.commit()
   
def get_order(session_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM orders WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None

def mark_order_processed(session_id):
    conn = get_conn()
    conn.execute("UPDATE orders SET processed = 1 WHERE session_id = ?", (session_id,))
    conn.commit()
 