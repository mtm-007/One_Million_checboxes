import time,asyncio, json,subprocess, pytz, httpx, modal, aiosqlite
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
import fasthtml.common as fh
from datetime import datetime, timezone
from typing import Optional, Dict, Any
#from redis.asyncio import Redis
import datetime as dt


CLIENT_GEO_TTL = 300.0
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")
SQLITE_DB_PATH = "/data/visitors.db"

# ------------------SHARED UI COMPONENTS (NEW - for DRY refactoring)-----------
def stat_card(label, value, subtitle=""):
    """Stat card - ultra compact"""
    return fh.Div(fh.Div(label, cls="stats-label"), fh.Div(value, cls="stats-number"),
                  fh.Div(subtitle, style="font-size:0.9em;opacity:0.8;") if subtitle else "", cls="stats-card")

def h_bar(label, count, total, color="#667eea"):
    """Single horizontal bar"""
    pct = (count / total * 100) if total > 0 else 0
    return fh.Div(fh.Span(label, cls="bar-label-horizontal"), 
                  fh.Div(fh.Div(fh.Span(f"{count} ({pct:.1f}%)" if count > 0 else "", 
                  style="color:white;font-size:0.9em;padding-left:8px;"),
                  style=f"width:{max(pct,2) if count>0 else 0}%;background:{color};", cls="bar-fill-horizontal"),
                  cls="bar-track-horizontal"), cls="bar-horizontal")

def h_chart(data, colors=None):
    """Horizontal bar chart"""
    data = dict(data) if isinstance(data, list) else data
    total = sum(data.values())
    return fh.Div(*[h_bar(k, v, total, colors.get(k, "#667eea") if colors else "#667eea") 
                    for k, v in data.items()], cls="chart-bars-container") if total else fh.P("No data", style="text-align:center;color:#999;")

def gradient_chart(data, grad="linear-gradient(90deg,#667eea 0%,#764ba2 100%)"):
    """Gradient bar chart for time series"""
    if not data: return fh.P("No data", style="text-align:center;color:#999;")
    mx = max([c[1] for c in data], default=1)
    return fh.Div(*[fh.Div(fh.Span(lbl, cls="bar-label-horizontal"),
                    fh.Div(fh.Div(fh.Span(f"{cnt}" if cnt>0 else "", style="color:white;font-size:0.8em;padding-left:8px;"),
                    style=f"width:{max((cnt/mx*100),2) if cnt>0 else 0}%;background:{grad};", cls="bar-fill-horizontal"),
                    cls="bar-track-horizontal"), cls="bar-horizontal") for lbl, cnt in data], cls="chart-bars-container")

def nav_links(*links):
    """Navigation links"""
    return fh.Div(*[fh.A(txt, href=url, cls="back-link", 
                    style=f"{'margin-left:20px;' if i>0 else ''}{stl if len(l)>2 else ''}") 
                    for i, l in enumerate(links) for txt, url, *rest in [l] for stl in [rest[0] if rest else ""]], 
                    style="text-align:center;margin-top:30px;")

def sec_badge(vpn, relay):
    """Security badge"""
    return fh.Span("iCloud Relay" if relay else "VPN/PROXY" if vpn else "Clean", 
                   style=f"background:{'#5856d6' if relay else '#ff3b30' if vpn else '#4cd964'};color:white;padding:2px 6px;border-radius:4px;font-size:0.8em;")

def class_badge(cls):
    """Classification badge"""
    h = "Human" in cls
    return fh.Span(f"{'👤' if h else '🤖'} {cls}", 
                   style=f"background:{'rgba(16,185,129,0.15)' if h else 'rgba(245,158,11,0.15)'};color:{'#10b981' if h else '#f59e0b'};padding:4px 8px;border-radius:4px;font-weight:600;font-size:0.85em;")

def ref_badge(src, typ):
    """Referrer badge"""
    return fh.Span(src[:20], style=f"background:{ {'direct':'#95a5a6','social':'#ff6b6b','search':'#4ecdc4','referral':'#45b7d1'}.get(typ,'#999')};"
                "color:white;padding:2px 6px;border-radius:4px;font-size:0.8em;" )

def pagination(offset, limit, total, url, extra=None):
    """Pagination controls"""
    more = (offset + limit) < total
    def build(o): return f"{url}?offset={o}&limit={limit}" + (f"&{'&'.join(f'{k}={v}' for k,v in extra.items())}" if extra else "")
    return fh.Div(fh.Div(
        fh.A("← Prev", href=build(max(0,offset-limit)), cls="pagination-btn") if offset>0 else fh.Span("← Prev", cls="pagination-btn disabled"),
        fh.Span(f"Showing {offset+1}-{min(offset+limit,total)} of {total}", cls="pagination-info"),
        fh.A("Next →", href=build(offset+limit), cls="pagination-btn") if more else fh.Span("Next →", cls="pagination-btn disabled"),
        cls="pagination-controls"), fh.Div(fh.Span("Show: ", style="margin-right:10px;"),
        *[fh.A(str(l), href=f"{url}?offset=0&limit={l}" + (f"&{'&'.join(f'{k}={v}' for k,v in extra.items())}" if extra else ""),
        cls=f"limit-btn{' active' if limit==l else ''}") for l in [50,100,200,500]], cls="limit-controls"), cls="pagination-wrapper")

def range_sel(curr, limit, offset, url):
    """Range selector"""
    return fh.Div(fh.Span("Chart Range: ", style="margin-right:10px;font-weight:bold;color:#667eea;"),
                  *[fh.A(str(d), href=f"{url}?days={d}&limit={limit}&offset={offset}", 
                  cls=f"range-btn{' active' if curr==d else ''}", title=f"Last {d} days") for d in [7,14,30]], cls="range-selector")

def fmt_time(s):
    """Format time"""
    return f"{s:.0f}s" if s<60 else f"{s/60:.1f}m" if s<3600 else f"{s/3600:.1f}h"

#-------- TIME ANALYTICS-----------
async def get_time_stats(redis, lim=100):
    """Get time stats"""
    ips = await redis.zrevrange("recent_visitors_sorted", 0, lim-1)
    visitors, tot_time, tot_sess = [], 0, 0
    for ip in ips:
        if (data := await redis.get(f"visitor:{ip.decode('utf-8') if isinstance(ip, bytes) else ip}")):
            v = json.loads(data)
            if (t := v.get("total_time_spent", 0)) > 0: 
                visitors.append(v)
                tot_time += t
                tot_sess += v.get("total_sessions", 0)
    return {"visitors": sorted(visitors, key=lambda x: x.get("total_time_spent", 0), reverse=True),
            "total_time": tot_time, "total_sessions": tot_sess, 
            "avg_per_visitor": tot_time/len(visitors) if visitors else 0,
            "avg_per_session": tot_time/tot_sess if tot_sess else 0}

async def get_time_buckets(redis, lim=500):
    """Get time buckets"""
    ips = await redis.zrevrange("recent_visitors_sorted", 0, lim-1)
    buckets = {"0-10s":0, "10-30s":0, "30s-1m":0, "1-2m":0, "2-5m":0, "5-10m":0, "10-30m":0, "30m+":0}
    for ip in ips:
        if (data := await redis.get(f"visitor:{ip.decode('utf-8') if isinstance(ip, bytes) else ip}")):
            t = json.loads(data).get("total_time_spent", 0)
            if t < 10: buckets["0-10s"] += 1
            elif t < 30: buckets["10-30s"] += 1
            elif t < 60: buckets["30s-1m"] += 1
            elif t < 120: buckets["1-2m"] += 1
            elif t < 300: buckets["2-5m"] += 1
            elif t < 600: buckets["5-10m"] += 1
            elif t < 1800: buckets["10-30m"] += 1
            else: buckets["30m+"] += 1
    return buckets

async def start_session(client_ip: str, user_agent: str, page: str, redis):
    """ start a new session for a visitor"""
    session_key = f"session:{client_ip}"
    session_data = { "ip": client_ip, "user_agent": user_agent, "start_time": time.time(), 
                    "last_activity": time.time(), "page_views": [{"page": page, "timestamp": time.time()}],}# "actions": 0, "scroll_depth": 0
    await redis.set(session_key, json.dumps(session_data), ex=3600)#store session with 1 hour expiry
    print(f"[SESSION] Started session for  {client_ip}")
    return session_data

async def update_session_activity(client_ip: str, redis):
    """ Update last activity timestamp"""
    if (session_data := await redis.get(f"session:{client_ip}")):
        data = json.loads(session_data)
        data["last_activity"] = time.time()
        await redis.set(f"session:{client_ip}", json.dumps(data), ex=3600)
        return True
    return False

async def end_session(client_ip: str, redis):
    """ End session and calculate total time spent"""
    if not (session_data := await redis.get(f"session:{client_ip}")): return None
    data = json.loads(session_data)
    start_time = data.get("start_time")
    duration_seconds = time.time() - start_time

    if (visitor_data := await redis.get(f"visitor:{client_ip}")):
        visitor = json.loads(visitor_data)
        visitor["total_time_spent"] = visitor.get("total_time_spent", 0) + duration_seconds
        visitor["last_session_duration"] = duration_seconds
        visitor["total_sessions"] = visitor.get("total_sessions", 0) + 1
        visitor["total_actions"] = visitor.get("total_actions", 0) + data.get("actions", 0)
        visitor["avg_session_duration"] = visitor["total_time_spent"] / visitor["total_sessions"]

        await redis.set(f"visitor:{client_ip}", json.dumps(visitor))
        await save_visitor_to_sqlite(visitor)
        print(f"[SESSION] Ended session for {client_ip}: {duration_seconds:.1f}s, {data.get('actions', 0)} actions")

    await redis.delete(f"visitor:{client_ip}")
    return duration_seconds

async def log_event(client_ip: str, event_type: str, event_data: Dict[str, Any], redis):
    """ Log user events/actions"""
    event = { "ip": client_ip, "type": event_type, "data": event_data, "timestamp": time.time() }
    await redis.lpush(f"events:{client_ip}", json.dumps(event))
    await redis.ltrim(f"events:{client_ip}", 0, 99) #keep only last 100

    if (session_data := await redis.get(f"session:{client_ip}")):
        data = json.loads(session_data)
        data["actions"] = data.get("actions", 0) + 1
        await redis.set(f"session:{client_ip}", json.dumps(data), ex=3600)

    if (visitor_data := await redis.get(f"visitor:{client_ip}")):
        visitor = json.loads(visitor_data)
        visitor["total_actions"] = visitor.get("total_actions", 0) + 1
        visitor[f"{event_type}_count"] = visitor.get(f"{event_type}_count", 0) + 1
        visitor["last_action_type"] = event_type
        visitor["last_action_time"] = time.time()
        await redis.set(f"visitor:{client_ip}", json.dumps(visitor))

async def get_user_events(client_ip: str, redis, limit: int =20):
    """Retrieve recent events for a user"""
    return [json.loads(raw) for raw in await redis.lrange(f"events:{client_ip}", 0, limit - 1)]

async def track_page_view(client_ip: str, page: str, referrer: str, redis):
    """Track which pages users visit"""
    if (session_data := await redis.get(f"session:{client_ip}")):
        data = json.loads(session_data)
        data["page_views"].append({ "page": page, "timestamp": time.time() })
        data["last_activity"] = time.time()
        await redis.set(f"session:{client_ip}", json.dumps(data), ex=3600)
    
    if (visitor_data := await redis.get(f"visitor:{client_ip}")):
        visitor = json.loads(visitor_data)
        if "pages_viewed" not in visitor: visitor["pages_viewed"] = {}
        visitor["pages_viewed"][page] = visitor["pages_viewed"].get(page, 0) + 1 
        if referrer and referrer != "":
            if "referrers" not in visitor: visitor["referrers"] = []
            if referrer not in visitor["referrers"]: visitor["referrers"].append(referrer)
        visitor["last_page"] = page
        visitor["total_page_views"] = visitor.get("total_page_views", 0) + 1
        await redis.set(f"visitor:{client_ip}", json.dumps(visitor))
    print(f"[PAGE VIEW] {client_ip} viewed {page}")

async def get_visitor_analytics(client_ip: str, redis):
    """ Get comprehensive analytics for a visitor"""
    if not (visitor_data := await redis.get(f"visitor:{client_ip}")): return None
    visitor = json.loads(visitor_data)
    events = await get_user_events(client_ip, redis, limit=10)
    session_data = await redis.get(f"session:{client_ip}")
    current_session = json.loads(session_data) if session_data else None
    return { "visitor": visitor, "recent_events": events, "current_session": current_session, "is_active": current_session is not None }
 
async def update_scroll_depth(client_ip: str, depth: float, redis):
    """Update max scroll depth for current session"""
    if (session_data := await redis.get(f"session:{client_ip}")):
        data = json.loads(session_data)
        data["scroll_depth"] = max(data.get("scroll_depth", 0), depth)
        await redis.set(f"session:{client_ip}", json.dumps(data), ex=3600)

def parse_referrer(referrer: str) -> Dict[str, Any]:
    """Parse referrer URL to extract useful information"""
    if not referrer or referrer == "direct": return { "source": "Direct", "domain": None, "full_url": None, "type": "direct"}
    
    referrer_lower = referrer.lower()
    social_platforms = { "facebook.com": "Facebook", "fb.com": "Facebook", "twitter.com": "Twitter/X", "t.co": "Twitter/X","snapchat.com": "Snapchat",
                         "x.com": "Twitter/X", "instagram.com": "Instagram", "linkedin.com": "LinkedIn", "reddit.com": "Reddit","telegram.org": "Telegram",
                         "pinterest.com": "Pinterest",  "tiktok.com": "TikTok", "youtube.com": "YouTube", "discord.com": "Discord", "whatsapp.com": "WhatsApp" }
    search_engines = { "google.com": "Google Search","bing.com": "Bing Search", "yahoo.com": "Yahoo Search", 
                       "baidu.com": "Baidu", "yandex.com": "Yandex", "ask.com": "Ask.com", "duckduckgo.com": "DuckDuckGo"}
    
    try:
        from urllib.parse import urlparse
        domain = urlparse(referrer).netloc or urlparse(referrer).path.split('/')[0]
        domain = domain.replace('www.', '')
        
        for social_domain, social_name in social_platforms.items():
            if social_domain in referrer_lower:
                return { "source": social_name, "domain": domain, "full_url": referrer[:200], "type": "social" }# Limit length
                
        for search_domain, search_name in search_engines.items():
            if search_domain in referrer_lower:
                return { "source": search_name, "domain": domain,  "full_url": referrer[:200], "type": "search" }
        
        return { "source": domain, "domain": domain, "full_url": referrer[:200], "type": "referral" }
    except:
        return { "source": "Unknown", "domain": None, "full_url": referrer[:200], "type": "unknown" }

async def track_referrer(client_ip: str, referrer: str, redis):
    """Track and store referrer information"""
    if not referrer: referrer = "direct"
    parsed_ref = parse_referrer(referrer)
    
    if  (visitor_data := await redis.get(f"visitor:{client_ip}")):
        visitor = json.loads(visitor_data)
        if "first_referrer" not in visitor:
            visitor["first_referrer"] = parsed_ref
            visitor["first_referrer_time"] = time.time()
        visitor["last_referrer"] = parsed_ref
        visitor["last_referrer_time"] = time.time()
        
        if "all_referrers" not in visitor: visitor["all_referrers"] = []
        ref_entry = { "source": parsed_ref["source"], "type": parsed_ref["type"], "timestamp": time.time() }
        
        if not visitor["all_referrers"] or visitor["all_referrers"][-1]["source"] != parsed_ref["source"]:
            visitor["all_referrers"].append(ref_entry)
            visitor["all_referrers"] = visitor["all_referrers"][-20:]
        await redis.set(f"visitor:{client_ip}", json.dumps(visitor))
        print(f"[REFERRER] {client_ip} came from {parsed_ref['source']} ({parsed_ref['type']})")
    
    await redis.incr(f"referrer_stats:{parsed_ref['source']}")
    await redis.incr(f"referrer_stats:{parsed_ref['source']}")

async def get_referrer_stats(redis, limit: int = 20):
    """Get top referrer sources"""
    keys = await redis.keys("referrer_stats:*")
    referrer_counts = []
    for key in keys:
        key_str = key.decode('utf-8') if isinstance(key, bytes) else key
        source = key_str.replace("referrer_stats:", "")
        count = int(await redis.get(key)) if await redis.get(key) else 0
        if count > 0: referrer_counts.append({"source": source, "count": count})
    return sorted(referrer_counts, key=lambda x: x["count"], reverse=True)[:limit]
    
async def get_referrer_type_stats(redis):
    """Get breakdown by referrer type"""
    type_stats = {}
    for ref_type in ["direct", "social", "search", "referral", "unknown"]:
        count = await redis.get(f"referrer_type:{ref_type}")
        type_stats[ref_type] = int(count) if count else 0
    return type_stats

def utc_to_local(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(LOCAL_TIMEZONE)
 
async def get_geo_from_providers(ip:str, redis):
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"https://ipwho.is/{ip}?security=1")
        if r.status_code == 200 and (data := r.json()).get("success"):
            print(f"[GEO] ✅ ipwho.is succesfully resolved {ip} -> {data.get('city')}, {data.get('country')}")
            sec ,conn = data.get("security", {}), data.get("connection", {})
            org_lower = conn.get("org", "").lower()
            usage =("Data Center" if sec.get("hosting") else "Education" if any(x in org_lower for x in ["uni", "college", "school"])
                     else "Business" if any(x in org_lower for x in ["corp", "inc", "ltd"]) else "Cellular" if data.get("type") =="Mobile" else "Residentail")
            is_relay_val = sec.get("relay", False) or "icloud" in conn.get("isp", "").lower() or "apple" in org_lower
            return{ "ip": ip, "city": data.get("city"), "postal": data.get("postal"), "country": data.get("country"), "region": data.get("region"),
                    "is_vpn": sec.get("vpn", False) or sec.get("proxy", False), "isp": conn.get("isp"), "is_hosting": sec.get("hosting", False),
                    "org": conn.get("org"), "asn": conn.get("asn"), "usage_type": usage, "is_relay": is_relay_val, "provider": "ipwho.is" }
    except Exception: pass
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://ip-api.com/json/{ip}?fields=66842239")
        if r.status_code == 200 and (data := r.json()).get("status") == "success":
            usage = "Data Center" if data.get("hosting") else "Cellular" if data.get("mobile") else "Residential"
            org_lower, isp_lower = data.get("isp", "").lower(), data.get("org", "").lower()
            is_relay_val = data.get("proxy", False) or any(x in isp_lower or x in org_lower for x in ["icloud", "apple relay", "apple inc"])
            print(f"[GEO] ✅ ip-api.com succesfully resolved {ip}")
            return { "ip": ip, "city": data.get("city"), "isp": data.get("isp"), "usage_type": "Privacy Relay" if is_relay_val else usage,
                     "is_vpn": data.get("proxy", False), "is_hosting": data.get("hosting", False), "is_relay": is_relay_val, "provider": "ip-api.com" }
    except Exception as e: 
        print(f"[GEO] ❌ ip-api.com failed for  {ip}: {e}")

    return {"ip": ip, "usage_type": "Unknown", "city": None, "country": None, "zip": None}

async def get_geo(ip: str, redis):
    """Return geo info from ip using cache + fallback providers"""
    if (cached := await redis.get(f"geo:{ip}")):
        print(f"[GEO] 💾 Cache hit for {ip}")
        return json.loads(cached)
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
    device = "Mobile" if "mobi" in ua or "iphone" in ua else"Tablet" if "ipad" in ua or "tablet" in ua else "Desktop"
    os = ("windows" if "windows" in ua else "macOS" if "macintosh" in ua or "mac os" in ua else 
         "iOS" if "iphone" in ua or "ipad" in ua else "Andriod" if "andriod" in ua else "Linux" if "linux" in ua else "Unknown") 
    return f"{device} ({os})"

async def record_visitors(ip, user_agent, geo, redis):
    """ Record visitor with visit count tracking"""
    try:
        existing = await redis.get(f"visitor:{ip}")
        is_new_visitor = existing is None
        ua_lower = user_agent.lower()
        device_str = get_device_info(user_agent)
        
        known_bots = { "googlebot": "Googlebot", "bingbot": "Bingbot", "twitterbot": "Twitterbot", "facebookexternalhit": "FacebookBot", "duckduckbot": "DuckDuckBot", 
                       "baiduspider": "Baiduspider", "yandexbot": "YandexBot","ia_archiver": "Alexa/Archive.org", "gptbot": "ChatGPT-Bot", "perplexitbot": "PerplexityAI" }
        detected_bot_name = next((name for key, name in known_bots.items() if key in ua_lower), None)
        
        classification = (detected_bot_name if detected_bot_name else 
                         "Script/Scraper" if any(s in ua_lower for s in ["python-requests", "aiohttp", "curl", "wget", "postman", "headless"]) else
                         "Bot/Server" if geo.get("is_hosting") else
                         "Human (Privacy/Relay)" if geo.get("is_relay") else "Human")
        
        visit_count = (json.loads(existing).get("visit_count", 1) + 1) if existing else 1

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

        await redis.set(f"visitor:{ip}", json.dumps(entry)) #save permanently
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
    """get real client IP"""
    return (request.headers.get('CF-Connecting-IP') or 
             (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip() or
             request.headers.get('X-Real-IP') or request.client.host)
    
async def init_sqlite_db():
    """Initialize SQLite database with visitors table """
    async with aiosqlite.connect(SQLITE_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS visitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT, device TEXT, user_agent TEXT, classification TEXT, usage_type TEXT, isp TEXT, city TEXT,
                zip TEXT, is_vpn INTEGER, country TEXT, timestamp REAL , visit_count INTEGER, last_updated REAL)""")
                         
        cursor = await db.execute("PRAGMA table_info(visitors)")
        columns = await cursor.fetchall()
        existing_columns = [col[1] for col in columns]  # col[1] is the column name
        
        # Add missing columns one by one
        new_columns = { "total_time_spent": "REAL DEFAULT 0", "last_session_duration": "REAL DEFAULT 0", 
                        "total_sessions": "INTEGER DEFAULT 0", "avg_session_duration": "REAL DEFAULT 0",
                        "total_actions": "INTEGER DEFAULT 0", "total_page_views": "INTEGER DEFAULT 0", 
                        "last_page": "TEXT", "last_action_type": "TEXT", "last_action_time": "REAL",
                        "first_referrer_source": "TEXT", "first_referrer_type": "TEXT",
                        "last_referrer_source": "TEXT", "last_referrer_type": "TEXT" }
        
        for column_name, column_type in new_columns.items():
            if column_name not in existing_columns:
                try:
                    await db.execute(f"ALTER TABLE visitors ADD COLUMN {column_name} {column_type}")
                    print(f"[MIGRATION] Added column: {column_name}")
                except Exception as e:
                    print(f"[MIGRATION] Column {column_name} might already exist: {e}")
        
        await db.execute(""" CREATE INDEX IF NOT EXISTS idx_timestamp ON visitors(timestamp DESC)""")
        await db.execute( """ CREATE INDEX IF NOT EXISTS idx_ip ON visitors(ip)""")
        await db.execute(""" CREATE INDEX IF NOT EXISTS idx_referrer ON visitors(first_referrer_source)""")
        await db.commit()
        print("[SQLite] Database initialized succesfully")

async def save_visitor_to_sqlite(entry):
    """ Save or update visitor record in SQLite"""
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            await db.execute("""INSERT INTO visitors
                (ip, device, user_agent, classification, usage_type, isp, city, zip, is_vpn,  country, timestamp, visit_count, last_updated)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry["ip"], entry["device"], entry["user_agent"], entry["classification"], entry["usage_type"], 
                entry["isp"], entry["city"], entry["zip"], 1 if entry["is_vpn"] else 0, entry["country"], 
                entry["timestamp"], entry["visit_count"], time.time() ))
            await db.commit()
            print(f"[SQLite] Saved visitor {entry['ip']}")
    except Exception as e:
        print(f"[SQLite ERROR] Failed to save visitor: {e}")

async def restore_visitors_from_sqlite(redis):
    """ Restore all visitor records from  SQLite to Redis"""
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
    except Exception as e:
        print(f"[SQLite ERROR] Failed to restore visitors: {e}")
        return 0

async def get_visitor_count_sqlite():
    """ Get total visitor count from SQLite """
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM visitors") as cursor:
                return (await cursor.fetchone())[0] if (row:= await cursor.fetchone())else 0
    except Exception  as e:
        print(f"[SQLite ERROR] Failed to get count: {e}")
        return 0


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
