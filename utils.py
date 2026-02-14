import time,asyncio, json,subprocess, pytz, httpx, modal
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
from fasthtml.core import viewport
from fasthtml.js import NotStr
import fasthtml.common as fh
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from redis.asyncio import Redis
import datetime as dt
import aiosqlite

CLIENT_GEO_TTL = 300.0
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

SQLITE_DB_PATH = "/data/visitors.db"

async def start_session(client_ip: str, user_agent: str, page: str, redis):
    """ start a new session for a visitor"""
    session_key = f"session:{client_ip}"

    session_data = {
        "ip": client_ip, "user_agent": user_agent, "start_time": time.time(), "last_activity": time.time(),
        "page_views": [{"page": page, "timestamp": time.time()}],# "actions": 0, "scroll_depth": 0
    }

    #store session with 1 hour expiry
    await redis.set(session_key, json.dumps(session_data), ex=3600)
    print(f"[SESSION] Started session for  {client_ip}")
    return session_data

async def update_session_activity(client_ip: str, redis):
    """ Update last activity timestamp"""
    session_key = f"session:{client_ip}"
    session_data = await redis.get(session_key)

    if session_data:
        data = json.loads(session_data)
        data["last_activity"] = time.time()
        await redis.set(session_key, json.dumps(data), ex=3600)
        return True
    return False

async def end_session(client_ip: str, redis):
    """ End session and calculate total time spent"""
    session_key = f"session:{client_ip}"
    session_data = await redis.get(session_key)

    if not session_data: return None

    data = json.loads(session_data)
    start_time = data.get("start_time")
    duration_seconds = time.time() - start_time

    #Update visitor record with session stats
    visitor_key = f"visitor:{client_ip}"
    visitor_data = await redis.get(visitor_key)

    if visitor_data:
        visitor = json.loads(visitor_data)
        visitor["total_time_spent"] = visitor.get("total_time_spent", 0) + duration_seconds
        visitor["last_session_duration"] = duration_seconds
        visitor["total_sessions"] = visitor.get("total_sessions", 0) + 1
        visitor["total_actions"] = visitor.get("total_actions", 0) + data.get("actions", 0)
        visitor["avg_session_duration"] = visitor["total_time_spent"] / visitor["total_sessions"]

        await redis.set(visitor_key, json.dumps(visitor))
        await save_visitor_to_sqlite(visitor)

        print(f"[SESSION] Ended session for {client_ip}: {duration_seconds:.1f}s, {data.get('actions', 0)} actions")

    #clean up session
    await redis.delete(session_key)
    return duration_seconds

#Event tracking functions
async def log_event(client_ip: str, event_type: str, event_data: Dict[str, Any], redis):
    """ Log user events/actions"""
    event = { "ip": client_ip, "type": event_type, "data": event_data, "timestamp": time.time() }

    #store in redis list (keep last 100 events per user)
    events_key = f"events:{client_ip}"
    await redis.lpush(events_key, json.dumps(event))
    await redis.ltrim(events_key, 0, 99) #keep only last 100

    #update session action count
    session_key = f"session:{client_ip}"
    session_data = await redis.get(session_key)
    if session_data:
        data = json.loads(session_data)
        data["actions"] = data.get("actions", 0) + 1
        await redis.set(session_key, json.dumps(data), ex=3600)

    #update visitor stats
    visitor_key = f"visitor:{client_ip}"
    visitor_data = await redis.get(visitor_key)
    if visitor_data:
        visitor = json.loads(visitor_data)
        visitor["total_actions"] = visitor.get("total_actions", 0) + 1
        visitor[f"{event_type}_count"] = visitor.get(f"{event_type}_count", 0) + 1
        visitor["last_action_type"] = event_type
        visitor["last_action_time"] = time.time()
        await redis.set(visitor_key, json.dumps(visitor))

async def get_user_events(client_ip: str, redis, limit: int =20):
    """Retrieve recent events for a user"""
    events_key = f"events:{client_ip}"
    raw_events = await redis.lrange(events_key, 0, limit - 1)

    events = []
    for raw in raw_events: 
        events.append(json.loads(raw))
    return events
    
#page tracking functions
async def track_page_view(client_ip: str, page: str, referrer: str, redis):
    """Track which pages users visit"""
    #update session page views
    session_key = f"session:{client_ip}"
    session_data = await redis.get(session_key)

    if session_data:
        data = json.loads(session_data)
        data["page_views"].append({ "page": page, "timestamp": time.time() })
        data["last_activity"] = time.time()
        await redis.set(session_key, json.dumps(data), ex=3600)
    
    #update visitor record
    visitor_key = f"visitor:{client_ip}"
    visitor_data = await redis.get(visitor_key)

    if visitor_data:
        visitor = json.loads(visitor_data)

        #track page view count
        if "pages_viewed" not in visitor:
            visitor["pages_viewed"] = {}
        visitor["pages_viewed"][page] = visitor["pages_viewed"].get(page, 0) + 1 

        #track referrers
        if referrer and referrer != "":
            if "referrers" not in visitor:
                visitor["referrers"] = []
            if referrer not in visitor["referrers"]:
                visitor["referrers"].append(referrer)
        
        visitor["last_page"] = page
        visitor["total_page_views"] = visitor.get("total_page_views", 0) + 1

        await redis.set(visitor_key, json.dumps(visitor))
    
    print(f"[PAGE VIEW] {client_ip} viewed {page}")

#Analytics helper functions
async def get_visitor_analytics(client_ip: str, redis):
    """ Get comprehensive analytics for a visitor"""
    visitor_key = f"visitor:{client_ip}"
    visitor_data = await redis.get(visitor_key)

    if not visitor_data: return None

    visitor = json.loads(visitor_data)

    #get recent events
    events = await get_user_events(client_ip, redis, limit=10)

    #get active session info
    session_key = f"session:{client_ip}"
    session_data = await redis.get(session_key)
    current_session = json.loads(session_data) if session_data else None

    analytics = { "visitor": visitor, "recent_events": events, "current_session": current_session, "is_active": current_session is not None }
    return analytics

async def update_scroll_depth(client_ip: str, depth: float, redis):
    """Update max scroll depth for current session"""
    session_key = f"session:{client_ip}"
    session_data = await redis.get(session_key)

    if session_data:
        data = json.loads(session_data)
        data["scroll_depth"] = max(data.get("scroll_depth", 0), depth)
        await redis.set(session_key, json.dumps(data), ex=3600)


def parse_referrer(referrer: str) -> Dict[str, Any]:
    """Parse referrer URL to extract useful information"""
    if not referrer or referrer == "direct":
        return {
            "source": "Direct",
            "domain": None,
            "full_url": None,
            "type": "direct"
        }
    
    referrer_lower = referrer.lower()
    
    # Social media detection
    social_platforms = {
        "facebook.com": "Facebook",
        "fb.com": "Facebook",
        "twitter.com": "Twitter/X",
        "t.co": "Twitter/X",
        "x.com": "Twitter/X",
        "instagram.com": "Instagram",
        "linkedin.com": "LinkedIn",
        "reddit.com": "Reddit",
        "pinterest.com": "Pinterest",
        "tiktok.com": "TikTok",
        "youtube.com": "YouTube",
        "discord.com": "Discord",
        "snapchat.com": "Snapchat",
        "whatsapp.com": "WhatsApp",
        "telegram.org": "Telegram",
    }
    
    # Search engines
    search_engines = {
        "google.com": "Google Search",
        "bing.com": "Bing Search",
        "yahoo.com": "Yahoo Search",
        "duckduckgo.com": "DuckDuckGo",
        "baidu.com": "Baidu",
        "yandex.com": "Yandex",
        "ask.com": "Ask.com",
    }
    
    # Extract domain from referrer
    try:
        from urllib.parse import urlparse
        parsed = urlparse(referrer)
        domain = parsed.netloc or parsed.path.split('/')[0]
        domain = domain.replace('www.', '')
        
        # Check if it's social media
        for social_domain, social_name in social_platforms.items():
            if social_domain in referrer_lower:
                return {
                    "source": social_name,
                    "domain": domain,
                    "full_url": referrer[:200],  # Limit length
                    "type": "social"
                }
        
        # Check if it's a search engine
        for search_domain, search_name in search_engines.items():
            if search_domain in referrer_lower:
                return {
                    "source": search_name,
                    "domain": domain,
                    "full_url": referrer[:200],
                    "type": "search"
                }
        
        # Regular website referral
        return {
            "source": domain,
            "domain": domain,
            "full_url": referrer[:200],
            "type": "referral"
        }
    except:
        return {
            "source": "Unknown",
            "domain": None,
            "full_url": referrer[:200],
            "type": "unknown"
        }

async def track_referrer(client_ip: str, referrer: str, redis):
    """Track and store referrer information"""
    if not referrer:
        referrer = "direct"
    
    parsed_ref = parse_referrer(referrer)
    
    # Update visitor record
    visitor_key = f"visitor:{client_ip}"
    visitor_data = await redis.get(visitor_key)
    
    if visitor_data:
        visitor = json.loads(visitor_data)
        
        # Store first referrer (acquisition source)
        if "first_referrer" not in visitor:
            visitor["first_referrer"] = parsed_ref
            visitor["first_referrer_time"] = time.time()
        
        # Store last referrer
        visitor["last_referrer"] = parsed_ref
        visitor["last_referrer_time"] = time.time()
        
        # Track all unique referrer sources
        if "all_referrers" not in visitor:
            visitor["all_referrers"] = []
        
        # Add to list if not already there (keep unique)
        ref_entry = {
            "source": parsed_ref["source"],
            "type": parsed_ref["type"],
            "timestamp": time.time()
        }
        
        # Only add if source is different from last one
        if not visitor["all_referrers"] or visitor["all_referrers"][-1]["source"] != parsed_ref["source"]:
            visitor["all_referrers"].append(ref_entry)
            # Keep only last 20 referrers
            visitor["all_referrers"] = visitor["all_referrers"][-20:]
        
        await redis.set(visitor_key, json.dumps(visitor))
        print(f"[REFERRER] {client_ip} came from {parsed_ref['source']} ({parsed_ref['type']})")
    
    # Track global referrer stats
    ref_stats_key = f"referrer_stats:{parsed_ref['source']}"
    await redis.incr(ref_stats_key)
    
    # Track referrer by type
    ref_type_key = f"referrer_type:{parsed_ref['type']}"
    await redis.incr(ref_type_key)

async def get_referrer_stats(redis, limit: int = 20):
    """Get top referrer sources"""
    # Get all referrer stat keys
    keys = await redis.keys("referrer_stats:*")
    
    referrer_counts = []
    for key in keys:
        key_str = key.decode('utf-8') if isinstance(key, bytes) else key
        source = key_str.replace("referrer_stats:", "")
        count = await redis.get(key)
        count = int(count) if count else 0
        
        if count > 0:
            referrer_counts.append({"source": source, "count": count})
    
    # Sort by count
    referrer_counts.sort(key=lambda x: x["count"], reverse=True)
    
    return referrer_counts[:limit]

async def get_referrer_type_stats(redis):
    """Get breakdown by referrer type"""
    types = ["direct", "social", "search", "referral", "unknown"]
    type_stats = {}
    
    for ref_type in types:
        key = f"referrer_type:{ref_type}"
        count = await redis.get(key)
        type_stats[ref_type] = int(count) if count else 0
    
    return type_stats


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
                zip TEXT, is_vpn INTEGER, country TEXT, timestamp REAL , visit_count INTEGER, last_updated REAL)""")
                         
                # total_time_spent REAL DEFAULT 0, last_session_duration REAL DEFAULT 0, total_sessions INTEGER DEFAULT 0, avg_session_duration REAL DEFAULT 0,
                # total_actions INTEGER DEFAULT 0, total_page_views INTEGER DEFAULT 0, last_page TEXT, last_action_type TEXT, last_action_time REAL ,
                # first_referrer_source TEXT, first_referrer_type TEXT, last_referrer_source TEXT, last_referrer_source TEXT, last_referrer_type TEXT)""")
        
        
        # Migration: Add new columns if they don't exist
        # This is safe to run multiple times - it will only add columns that are missing
        
        # Check which columns exist
        cursor = await db.execute("PRAGMA table_info(visitors)")
        columns = await cursor.fetchall()
        existing_columns = [col[1] for col in columns]  # col[1] is the column name
        
        # Add missing columns one by one
        new_columns = {
            "total_time_spent": "REAL DEFAULT 0",
            "last_session_duration": "REAL DEFAULT 0",
            "total_sessions": "INTEGER DEFAULT 0",
            "avg_session_duration": "REAL DEFAULT 0",
            "total_actions": "INTEGER DEFAULT 0",
            "total_page_views": "INTEGER DEFAULT 0",
            "last_page": "TEXT",
            "last_action_type": "TEXT",
            "last_action_time": "REAL",
            "first_referrer_source": "TEXT",
            "first_referrer_type": "TEXT",
            "last_referrer_source": "TEXT",
            "last_referrer_type": "TEXT"
        }
        
        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                try:
                    await db.execute(f"ALTER TABLE visitors ADD COLUMN {column_name} {column_type}")
                    print(f"[MIGRATION] Added column: {column_name}")
                except Exception as e:
                    print(f"[MIGRATION] Column {column_name} might already exist: {e}")
        
        
        await db.execute(""" 
            CREATE INDEX IF NOT EXISTS idx_timestamp ON visitors(timestamp DESC)""")
        await db.execute( """   
            CREATE INDEX IF NOT EXISTS idx_ip ON visitors(ip)""")
        await db.execute(""" 
            CREATE INDEX IF NOT EXISTS idx_referrer ON visitors(first_referrer_source)""")
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
                entry = {   "ip": row["ip"], "device": row["device"], "user_agent": row["user_agent"], "isp": row["isp"], "city": row["city"],
                             "zip": row["zip"], "is_vpn": bool(row["is_vpn"]), "country": row["country"], "timestamp":row["timestamp"], "visit_count": row["visit_count"] }
                await redis.set(f"visitor:{entry["ip"]}", json.dumps(entry))
                await redis.zadd("recent_visitors_sorted", {entry["ip"]: entry["timestamp"]})
                count += 1
            
        #update total count
        await redis.set("total_visitors_count", count)
        print(f"[SQLite] Restore {count} visitors to Redis")
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

        #get existing referrer data if visitor exists
        first_ref_source = None
        first_ref_type = None
        last_ref_source = None
        last_ref_type = None
        
        if existing:
            existing_data = json.loads(existing)
            first_ref_source = existing_data.get("first_referrer", {}).get("source")
            first_ref_type = existing_data.get("first_referrer", {}).get("type")
            last_ref_source = existing_data.get("last_referrer", {}).get("source")
            last_ref_type = existing_data.get("last_referrer", {}).get("type")

        entry = {   "ip": ip, "device" : device_str, "user_agent": user_agent[:120], "classification": classification, "usage_type": geo.get("usage_type", "Unknown"),
                    "isp": geo.get("isp") or "-", "city": geo.get("city") or geo.get("region", "Unknown"), "zip": geo.get("postal") or geo.get("zip") or "-",
                    "is_vpn": geo.get("is_vpn", False), "country": geo.get("country") or geo.get("country_name"), "timestamp": time.time(), "visit_count" : visit_count, 
                    "first_referrer_source": first_ref_source, "first_referrer_type": first_ref_type, "last_referrer_source": last_ref_source, "last_referrer_type": last_ref_type,}

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
