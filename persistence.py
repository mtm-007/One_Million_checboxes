import aiosqlite
import json, time

SQLITE_DB_PATH = "/data/visitors.db"

async def init_sqlite_db():
    async with aiosqlite.connect(SQLITE_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS visitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT, device TEXT, user_agent TEXT, classification TEXT, usage_type TEXT, isp TEXT, city TEXT,
                zip TEXT, is_vpn INTEGER, country TEXT, timestamp REAL , visit_count INTEGER, last_updated REAL)""")             
        cursor = await db.execute("PRAGMA table_info(visitors)")
        columns = await cursor.fetchall()
        existing_columns = [col[1] for col in columns]  # col[1] is the column name
        # Add missing columns one by one
        new_columns = { "zip": "TEXT", "total_time_spent": "REAL DEFAULT 0", "last_session_duration": "REAL DEFAULT 0", 
                        "total_sessions": "INTEGER DEFAULT 0", "avg_session_duration": "REAL DEFAULT 0",
                        "total_actions": "INTEGER DEFAULT 0", "total_page_views": "INTEGER DEFAULT 0", 
                        "last_page": "TEXT", "last_action_type": "TEXT", "last_action_time": "REAL",
                        "first_referrer_source": "TEXT", "first_referrer_type": "TEXT",
                        "last_referrer_source": "TEXT", "last_referrer_type": "TEXT" , "max_scroll_depth": "REAL"}
        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                try:
                    await db.execute(f"ALTER TABLE visitors ADD COLUMN {column_name} {column_type}")
                    print(f"[MIGRATION] Added column: {column_name}")
                except Exception as e: print(f"[MIGRATION] Column {column_name} might already exist: {e}")
        await db.execute(""" CREATE INDEX IF NOT EXISTS idx_timestamp ON visitors(timestamp DESC)""")
        await db.execute( """ CREATE INDEX IF NOT EXISTS idx_ip ON visitors(ip)""")
        await db.execute(""" CREATE INDEX IF NOT EXISTS idx_referrer ON visitors(first_referrer_source)""")
        await db.commit()
        print("[SQLite] Database initialized succesfully")

async def save_visitor_to_sqlite(entry):
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            await db.execute("""INSERT OR REPLACE INTO visitors
                (ip, device, user_agent, classification, usage_type, isp, city, zip, is_vpn,  country, timestamp, visit_count, last_updated,
                total_time_spent, last_session_duration, total_sessions,
                avg_session_duration, total_actions, max_scroll_depth)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entry["ip"], entry["device"], entry["user_agent"], entry["classification"], entry["usage_type"], 
                entry["isp"], entry["city"], entry["zip"], 1 if entry["is_vpn"] else 0, entry["country"], 
                entry["timestamp"], entry["visit_count"], time.time(),
                entry.get("total_time_spent", 0), entry.get("last_session_duration", 0),
                entry.get("total_sessions", 0), entry.get("avg_session_duration", 0),
                entry.get("total_actions", 0), entry.get("max_scroll_depth", 0)))
            await db.commit()
            print(f"[SQLite] Saved visitor {entry['ip']}")
    except Exception as e: print(f"[SQLite ERROR] Failed to save visitor: {e}")

async def restore_visitors_from_sqlite(redis):
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            count = 0
            async with db.execute("SELECT * FROM visitors ORDER BY timestamp DESC") as cursor:
                async for row in cursor:
                    entry = {   "ip": row["ip"], "device": row["device"], "user_agent": row["user_agent"], "isp": row["isp"], "city": row["city"],
                                "zip": row["zip"], "is_vpn": bool(row["is_vpn"]), "country": row["country"], "timestamp":row["timestamp"], "visit_count": row["visit_count"] }
                    await redis.set(f"visitor:{entry["ip"]}", json.dumps(entry))
                    await redis.zadd("recent_visitors_sorted", {entry["ip"]: entry["timestamp"]})
                    count += 1
        await redis.set("total_visitors_count", count)
        print(f"[SQLite] Restore {count} visitors to Redis")
        return count
    except Exception as e: print(f"[SQLite ERROR] Failed to restore visitors: {e}"); return 0

async def get_visitor_count_sqlite():
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM visitors") as cur:
                row = await cur.fetchone(); return row[0] if row else 0
    except Exception  as e: print(f"[SQLite ERROR] Failed to get count: {e}"); return 0



# def resolve_referrer(request) -> str:
#     utm_source = request.query_params.get("utm_source")
#     if utm_source:
#         parts = [utm_source]
#         if medium := request.query_params.get("utm_medium"): parts.append(medium)
#         if campaign := request.query_params.get("utm_campaign"): parts.append(campaign)
#         return "utm:" + "/".join(parts)

#     referrer = request.headers.get("referer", "").strip()
#     if referrer: return referrer
#     return "direct"