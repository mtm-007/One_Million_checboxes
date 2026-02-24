import time,asyncio, json,subprocess, pytz, httpx, modal
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
from fasthtml.core import viewport
from fasthtml.js import NotStr
import fasthtml.common as fh
from datetime import datetime, timezone
from redis.asyncio import Redis
import datetime as dt
import aiosqlite

CLIENT_GEO_TTL = 300.0
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

SQLITE_DB_PATH = "/data/visitors.db"

class Client:
    def __init__(self):
        self.id = str(uuid4())
        self.diffs = []
        self.inactive_deadline = time.time() + 30
        self.geo = None
        self.geo_ts = 0.0
    
    def is_active(self): return time.time() < self.inactive_deadline
    
    def heartbeat(self): self.inactive_deadline = time.time() + 30

    def add_diff(self, i):
        if i not in self.diffs: self.diffs.append(i)

    def pull_diffs(self):
        #return a copy of the diffs and clear them
        diffs = self.diffs
        self.diffs = []
        return diffs
    def set_geo(self, geo_obj, now=None):
        self.geo = geo_obj
        self.geo_ts = now or time.time()

    def has_recent_geo(self, now=None):
        now = now or time.time()
        return (self.geo is not None) and ((now - self.geo_ts) <= CLIENT_GEO_TTL)

def utc_to_local(timestamp):
    utc_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    local_dt = utc_dt.astimezone(LOCAL_TIMEZONE)
    return local_dt

async def init_sqlite_db():
    """Initialize SQLite database with visitors table """
    async with aiosqlite.connect(SQLITE_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS visitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT, device TEXT, user_agent TEXT, classification TEXT, usage_type TEXT, isp TEXT, city TEXT,
                zip TEXT, is_vpn INTEGER, country TEXT, timestamp REAL , visit_count INTEGER, last_updated REAL )""")
        
        await db.execute(""" 
            CREATE INDEX IF NOT EXISTS idx_timestamp ON visitors(timestamp DESC)""")
        await db.commit()
        print("[SQLite] Database initialized succesfully")

async def save_visitor_to_sqlite(entry):
    """ Save or update visitor record in SQLite"""
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            await db.execute(""" 
                INSERT INTO visitors
                (ip, device, user_agent, classification, usage_type, isp, city, zip, is_vpn,  country, timestamp, visit_count, last_updated)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, ( entry["ip"], entry["device"], entry["user_agent"], entry["classification"], entry["usage_type"], entry["isp"], entry["city"], entry["zip"], 
                        1 if entry["is_vpn"] else 0, entry["country"], entry["timestamp"], entry["visit_count"], time.time() ))
            await db.commit()
            print(f"[SQLite] Saved visitor {entry['ip']}")
    except Exception as e:
        print(f"[SQLite ERROR] Failed to save visitor: {e}")


async def daily_flush_worker(redis):
    """Background task that flushes Redis visitors to SQLite once a day"""
    while True:
        await asyncio.sleep(86400) #waits 24hrs
        print("[CRON] Starting scheduled daily backup to SQLite...")
        await flush_redis_to_sqlite(redis)

async def flush_redis_to_sqlite(redis):
    """The actual logic to move/copy Redis visitor data into SQLite"""
    print("[BACKUP] Syncing Redis visitors to SQLite...")
    visitor_keys = await redis.keys("visitor:*")

    for key in visitor_keys:
        raw_data = await redis.get(key)
        if raw_data:
            record = json.loads(raw_data)
            # This uses your 'INSERT' logic (append-only)
            await save_visitor_to_sqlite(record)
    print(f"[BACKUP] Successfully backed up {len(visitor_keys)} records.")

async def restore_visitors_from_sqlite(redis):
    """ Restore all visitor records from  SQLite to Redis"""
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM visitors ORDER BY timestamp DESC") as cursor:
                count = 0
            async for row in cursor:
                entry = {   "ip": row["ip"], "device": row["device"], "user_agent": row["usage_type"], "isp": row["isp"], "city": row["city"],
                             "zip": row["zip"], "is_vpn": bool(row["is_vpn"]), "country": row["country"], "timestamp":row["timestamp"], "visit_count": row["visit_count"] }
                await redis.set(f"visitor: {entry['ip']}", json.dumps(entry))
                await redis.zadd("recent_visitors_sorted", {entry["ip"]: entry["timestamp"]})
                count += 1
            #Restore to Redis
            await redis.set(f"visitor: {entry['ip']}", json.dumps(entry))
            await redis.zadd("recent_visitors_sorted", {entry['ip']: entry['timestamp']})
            count += 1
        #update total count
        await redis.set("total_visitors_count", count)
        print(f"[SQLite] Restore {count} visitors tot Redis")
        return count

    except Exception as e:
        print(f"[SQLite ERROR] Failed to restore visitors: {e}")
        return 0

async def get_visitor_count_sqlite():
    """ Get total visitor count from SQLite """
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM visitors") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    except Exception  as e:
        print(f"[SQLite ERROR] Failed to get count: {e}")
        return 0
        
#New geolocation helper function
async def get_geo_from_providers(ip:str, redis):
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"https://ipwho.is/{ip}?security=1")
        if r.status_code == 200:
            data = r.json()
            if data.get("success"):
                print(f"[GEO] ✅ ipwho.is succesfully resolved {ip} -> {data.get('city')}, {data.get('country')}")
                sec = data.get("security", {})
                conn = data.get("connection", {})
                
                usage = "Residential"
                org_lower = conn.get("org", "").lower()
                if sec.get("hosting"): usage = "Data Center"
                elif any(x in org_lower for x in ["uni", "college", "school"]): usage = "Education"
                elif any(x in org_lower for x in ["corp", "inc", "ltd"]):usage = "Business"
                elif data.get("type") == "Mobile": usage = "Cellular"

                is_relay_val = sec.get("relay", False) or "icloud" in conn.get("isp", "").lower() or "apple" in conn.get("org", "").lower()

                #normalize the data to match entry format
                return{ "ip": ip, "city": data.get("city"), "postal": data.get("postal"), "country": data.get("country"), "region": data.get("region"),
                        "is_vpn": sec.get("vpn", False) or sec.get("proxy", False), "isp": conn.get("isp"), "is_hosting": sec.get("hosting", False),
                        "org": conn.get("org"), "asn": conn.get("asn"), "usage_type": usage, "is_relay": is_relay_val, "provider": "ipwho.is" }
    except Exception: pass
    #second provider
    try:
        #fields=66842239 gets: hosting, mobile, proxy, isp, org, city, country, zip
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://ip-api.com/json/{ip}?fields=66842239")
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                usage = "Residential"
                if data.get("hosting"): usage = "Data Center"
                elif data.get("mobile"): usage = "Cellular"
                isp_lower = data.get("isp", "").lower()
                org_lower = data.get("org", "").lower()
                is_relay_val = data.get("proxy", False) or any(x in isp_lower or x in org_lower for x in ["icloud", "apple relay", "apple inc"])
                print(f"[GEO] ✅ ip-api.com succesfully resolved {ip}")# -> {data.get('city')}, {data.get('country')}")
                
                return {
                    "ip": ip, "city": data.get("city"), "isp": data.get("isp"), "usage_type": "Privacy Relay" if is_relay_val else usage,
                    "is_vpn": data.get("proxy", False), "is_hosting": data.get("hosting", False), "is_relay": is_relay_val, "provider": "ip-api.com"
                }
                
    except Exception as e: 
        print(f"[GEO] ❌ ip-api.com failed for  {ip}: {e}")

    #last resort 
    return {"ip": ip, "usage_type": "Unknown", "city": None, "country": None, "zip": None}

async def get_geo(ip: str, redis):
    """Return geo info from ip using cache + fallback providers"""
    cached = await redis.get(f"geo:{ip}")
    if cached:
        print(f"[GEO] 💾 Cache hit for {ip}")
        return json.loads(cached)
    #fetch from providers and cache
    print(f"[GEO]  🔍 Cache miss for {ip}, fetching from providers...")
    data = await get_geo_from_providers(ip,redis)
    try:
        await redis.set(f"geo:{ip}", json.dumps(data)) #save get_geo api calls to providers #, ex=GEO_TTL_REDIS)
        print(f"[GEO] 💾 Cached geo data for {ip}")
    except Exception as e:
        print(f"[GEO] ⚠️  Failed to cache geo data for {ip}: {e}")
    return data

def get_device_info(ua_string:str):
    ua = ua_string.lower()
    if "mobi" in ua or "iphone" in ua: device = "Mobile"
    elif "ipad" in ua or "tablet" in ua: device = "Tablet"
    else: device = "Desktop"

    #os detection
    if "windows" in ua: os = "windows"
    elif "macintosh" in ua or "mac os" in ua: os = "macOS"
    elif "iphone" in ua or "ipad" in ua: os= "iOS"
    elif "andriod" in ua: os ="Andriod"
    elif "linux" in ua:os = "Linux"
    else: os = "Unknown"
    return f"{device} ({os})"

async def record_visitors(ip, user_agent, geo, redis):
    """ Record visitor with visit count tracking"""
    try:
        visitors_key = f"visitor:{ip}"
        existing = await redis.get(visitors_key)
        is_new_visitor = existing is None
        #---bot detection
        ua_lower = user_agent.lower()
        device_str = get_device_info(user_agent)
        is_hosting = geo.get("is_hosting", False)
       
        #Known Good Bots (Search Engines)
        known_bots = {
            "googlebot": "Googlebot", "bingbot": "Bingbot", "twitterbot": "Twitterbot", "facebookexternalhit": "FacebookBot", "duckduckbot": "DuckDuckBot", 
            "baiduspider": "Baiduspider", "yandexbot": "YandexBot","ia_archiver": "Alexa/Archive.org", "gptbot": "ChatGPT-Bot", "perplexitbot": "PerplexityAI"
        }
        detected_bot_name = None
        for bot_key, display_name in known_bots.items():
            if bot_key in ua_lower:
                detected_bot_name = display_name
                break
        
        if detected_bot_name:classification = detected_bot_name
        elif any(s in ua_lower for s in ["python-requests", "aiohttp", "curl", "wget", "postman", "headless"]): classification = "Script/Scraper"
        elif is_hosting: classification = "Bot/Server"
        elif geo.get("is_relay", False): classification = "Human (Privacy/Relay)"
        else: classification = "Human"

        visit_count = (json.loads(existing).get("visit_count", 1) + 1) if existing else 1
        
        entry = {   "ip": ip, "device" : device_str, "user_agent": user_agent[:120], "classification": classification, "usage_type": geo.get("usage_type", "Unknown"),
                    "isp": geo.get("isp") or "-", "city": geo.get("city") or geo.get("region", "Unknown"), "zip": geo.get("postal") or geo.get("zip") or "-",
                    "is_vpn": geo.get("is_vpn", False), "country": geo.get("country") or geo.get("country_name"), "timestamp": time.time(), "visit_count" : visit_count, }

        await redis.set(visitors_key, json.dumps(entry)) #save permanently
        await redis.zadd("recent_visitors_sorted", {ip:time.time()}) #maintain a sorted set by timestamp
        await save_visitor_to_sqlite(entry)

        if is_new_visitor:
            await redis.incr("total_visitors_count")
            print(f"[VISITOR] New visitor from {geo.get('city', 'Unknown',)}, {geo.get('country', 'Unknown')}")
        else:
            print(f"[VISITOR] Returning visitor {ip}  (visit #{visit_count}")
        
        status_icon = "👤" if "Human" in classification else "🤖"
        print(f"['VISITOR'] {status_icon} { ip} | {classification} | Type: {entry['usage_type']} | ISP: {entry['isp']}")

    except Exception as e:
        print(f"[ERROR] Failed to record visitor: {e}")

def get_real_ip(request):
    """get real client IP,accounting for proxies and cloudflare"""
    #cloudflare-specific header (most reliable)
    cf_connecting_ip = request.headers.get('CF-Connecting-IP')
    if cf_connecting_ip:
        return cf_connecting_ip
    
    #standard proxy headers
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        #X-forwarded can cantain multiple IPs, first one is the client
        return forwarded_for.split(',')[0].strip()
    
    real_ip = request.headers.get('X-Real-IP')
    if real_ip:
        return real_ip
    
    #fallback to direct client host 
    return request.client.host
