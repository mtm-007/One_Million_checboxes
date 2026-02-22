import json
import time
import hashlib
from typing import Dict, Any, Tuple
import persistence
import config
import fasthtml.common as fh
import fasthtml_components
from datetime import datetime, timezone, timedelta
import datetime as dt

async def get_user_events(client_ip: str, redis, limit: int =20):
    return [json.loads(raw) for raw in await redis.lrange(f"events:{client_ip}", 0, limit - 1)]

def get_real_ip(request):
    return (request.headers.get('CF-Connecting-IP') or 
             (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip() or
             request.headers.get('X-Real-IP') or request.client.host)

async def start_session(client_ip: str, user_agent: str, page: str, redis):
    session_data = { "ip": client_ip, "user_agent": user_agent, "start_time": time.time(), 
                    "last_activity": time.time(), "page_views": [{"page": page, "timestamp": time.time()}],}
    await redis.set(f"session:{client_ip}", json.dumps(session_data), ex=3600)
    print(f"[SESSION] Started session for  {client_ip}")
    return session_data

def get_device_info(ua_string:str):
    ua = ua_string.lower()
    device = "Mobile" if "mobi" in ua or "iphone" in ua else"Tablet" if "ipad" in ua or "tablet" in ua else "Desktop"
    os = ("windows" if "windows" in ua else "macOS" if "macintosh" in ua or "mac os" in ua else 
         "iOS" if "iphone" in ua or "ipad" in ua else "Andriod" if "andriod" in ua else "Linux" if "linux" in ua else "Unknown") 
    return f"{device} ({os})"
    
async def record_visitors(ip, user_agent, geo, redis):
    try:
        existing = await redis.get(f"visitor:{ip}")
        ua_l = user_agent.lower()
        
        classification = (next((n for k,n in config.BOTS.items() if k in ua_l), None) or
                         ("Script/Scraper" if any(s in ua_l for s in ["python-requests","aiohttp","curl","wget","postman","headless"]) else
                          "Bot/Server" if geo.get("is_hosting") else
                          "Human (Privacy/Relay)" if geo.get("is_relay") else "Human"))
        existing_data = json.loads(existing) if existing else {}
        entry = {**existing_data, "ip":ip,"device":get_device_info(user_agent),"user_agent":user_agent[:120],
                 "classification":classification,"usage_type":geo.get("usage_type","Unknown"),
                 "isp":geo.get("isp") or "-","city":geo.get("city") or geo.get("region","Unknown"),
                 "zip":geo.get("postal") or geo.get("zip") or "-","is_vpn":geo.get("is_vpn",False),
                 "country":geo.get("country") or geo.get("country_name"),"timestamp":time.time(),
                 "visit_count":(existing_data.get("visit_count",1)+1) if existing else 1,
                 "first_referrer_source":existing_data.get("first_referrer",{}).get("source"),
                 "first_referrer_type":existing_data.get("first_referrer",{}).get("type"),
                 "last_referrer_source":existing_data.get("last_referrer",{}).get("source"),
                 "last_referrer_type":existing_data.get("last_referrer",{}).get("type")}
        await redis.set(f"visitor:{ip}", json.dumps(entry))
        await redis.zadd("recent_visitors_sorted", {ip: time.time()})
        await persistence.save_visitor_to_sqlite(entry)
        if not existing:
            await redis.incr("total_visitors_count")
            print(f"[VISITOR] New: {geo.get('city')}, {geo.get('country')} | {classification}")
    except Exception as e: print(f"[ERROR] record_visitors: {e}")

async def update_session_activity(client_ip: str, redis):
    if (session_data := await redis.get(f"session:{client_ip}")):
        data = json.loads(session_data)
        data["last_activity"] = time.time()
        await redis.set(f"session:{client_ip}", json.dumps(data), ex=3600); return True
    return False

async def log_event(client_ip: str, event_type: str, event_data: Dict[str, Any], redis):
    await redis.lpush(f"events:{client_ip}", json.dumps({ "ip": client_ip, "type": event_type, "data": event_data, "timestamp": time.time() }))
    await redis.ltrim(f"events:{client_ip}", 0, 99) #keep only last 100
    for key, update in [(f"session:{client_ip}", lambda d: d.update({"actions": d.get("actions",0)+1}) or d),
                        (f"visitor:{client_ip}", lambda d: d.update({"total_actions":d.get("total_actions",0)+1,
                            f"{event_type}_count":d.get(f"{event_type}_count",0)+1,
                            "last_action_type":event_type,"last_action_time":time.time()}) or d)]:
        if (raw := await redis.get(key)):
            d = update(json.loads(raw)); await redis.set(key, json.dumps(d), **({"ex":3600} if "session" in key else {}))

async def track_page_view(client_ip: str, page: str, referrer: str, redis):
    print(f"[DEBUG-TRACK] Called for path='{page}' ip={client_ip} referrer={referrer[:50]}")

    now = time.time()
    is_blog = page == "/blog"

    for key, updater in [
        (f"session:{client_ip}", lambda d: d.update({"page_views": d.get("page_views",[])+[{"page":page,"timestamp":time.time()}], "last_activity":time.time()}) or d),
        (f"visitor:{client_ip}", lambda d: d.update({"pages_viewed": {**d.get("pages_viewed",{}), page: d.get("pages_viewed",{}).get(page,0)+1},
            "last_page":page, "total_page_views":d.get("total_page_views",0)+1,
            **({"referrers": list(set(d.get("referrers",[])+[referrer]))} if referrer else {})}) or d)]:
        if (raw := await redis.get(key)):
            d = updater(json.loads(raw))
            await redis.set(key, json.dumps(d), **({"ex":3600} if "session" in key else {}))
    
    if is_blog:
        print(f"[DEBUG-TRACK] Adding to blog:visits:by_time for {client_ip}")

        pipe = redis.pipeline()
        pipe.incr("blog:total_views")
        pipe.sadd("blog:unique_ips", client_ip)
        visit_key = f"{client_ip}:{int(now)}"
        pipe.zadd("blog:visits:by_time", {visit_key: now})
        try:
            await pipe.execute()   # ← must be awaited!
            # Invalidate blog visitors cache so next load is fresh
            cache_keys = await redis.keys("cache:blog_visitors:*")
            for k in cache_keys:
                await redis.delete(k)
            print("[DEBUG-TRACK] Blog pipeline EXECUTED successfully")
            after_count = await redis.zcard("blog:visits:by_time")
            print(f"[DEBUG-TRACK] After write → blog:visits:by_time has {after_count} entries")
            print(f"[DEBUG-TRACK] Successfully added blog visit entry")
        except Exception as e:
            print(f"[ERROR-TRACK] Blog pipeline failed: {e}")
    print(f"[PAGE VIEW] {client_ip} viewed {page}{' (BLOG)' if is_blog else ''}")   # ← improved log


# async def track_referrer(client_ip: str, referrer: str, redis):
#     if not referrer: referrer = "direct"
#     parsed = parse_referrer(referrer)
#     if (vd := await redis.get(f"visitor:{client_ip}")):
#         v = json.loads(vd)
#         v.setdefault("first_referrer", parsed); v.setdefault("first_referrer_time", time.time())
#         v["last_referrer"] = parsed; v["last_referrer_time"] = time.time()
#         refs = v.setdefault("all_referrers", [])
#         if not refs or refs[-1]["source"] != parsed["source"]:
#             refs.append({"source":parsed["source"],"type":parsed["type"],"timestamp":time.time()})
#             v["all_referrers"] = refs[-20:]
#         await redis.set(f"visitor:{client_ip}", json.dumps(v))
#     await redis.incr(f"referrer_stats:{parsed['source']}")
#     print(f"[REFERRER] {client_ip} came from {parsed['source']} ({parsed['type']})")

async def track_referrer(client_ip: str, referrer: str, redis):
    if not referrer:
        referrer = "direct"

    parsed = parse_referrer(referrer)

    key = f"visitor:{client_ip}"
    pipe = redis.pipeline()

    # Get current data (or empty dict)
    current = await redis.get(key)
    v = json.loads(current) if current else {}

    # Set FIRST referrer only if not already present
    if "first_referrer" not in v:
        v["first_referrer"] = parsed
        v["first_referrer_time"] = time.time()

    # Always update LAST referrer
    v["last_referrer"] = parsed
    v["last_referrer_time"] = time.time()

    # Append to ALL_REFERRERS list (dedup consecutive, keep last 20)
    all_refs = v.get("all_referrers", [])
    if not all_refs or all_refs[-1]["source"] != parsed["source"]:
        all_refs.append({
            "source": parsed["source"],
            "type": parsed["type"],
            "timestamp": time.time()
        })
        v["all_referrers"] = all_refs[-20:]  # keep last 20

    # Save back
    pipe.set(key, json.dumps(v))
    await pipe.execute()

    # Increment global source counter (this is why stats page works)
    await redis.incr(f"referrer_stats:{parsed['source']}")

    print(f"[REFERRER] {client_ip} came from {parsed['source']} ({parsed['type']})")

async def end_session(client_ip: str, redis):
    """ End session and calculate total time spent"""
    if not (session_data := await redis.get(f"session:{client_ip}")): return None
    data = json.loads(session_data)
    duration_seconds = time.time() - data.get("start_time")
    if (visitor_data := await redis.get(f"visitor:{client_ip}")):
        visitor = json.loads(visitor_data)
        visitor["total_time_spent"] = visitor.get("total_time_spent", 0) + duration_seconds
        visitor["last_session_duration"] = duration_seconds
        visitor["total_sessions"] = visitor.get("total_sessions", 0) + 1
        visitor["total_actions"] = visitor.get("total_actions", 0) + data.get("actions", 0)
        visitor["max_scroll_depth"] = max(visitor.get("max_scroll_depth", 0), data.get("scroll_depth", 0) )
        visitor["avg_session_duration"] = visitor["total_time_spent"] / visitor["total_sessions"]
        await redis.set(f"visitor:{client_ip}", json.dumps(visitor)); await persistence.save_visitor_to_sqlite(visitor)
        print(f"[SESSION] Ended session for {client_ip}: {duration_seconds:.1f}s, {data.get('actions', 0)} actions")
    await redis.delete(f"session:{client_ip}")
    return duration_seconds

async def update_scroll_depth(client_ip: str, depth: float, redis):
    if (session_data := await redis.get(f"session:{client_ip}")):
        data = json.loads(session_data)
        data["scroll_depth"] = max(data.get("scroll_depth", 0), depth)
        await redis.set(f"session:{client_ip}", json.dumps(data), ex=604800)

async def get_time_stats(redis, lim=100):
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
    ips = await redis.zrevrange("recent_visitors_sorted", 0, lim-1)
    buckets = {"0-10s":0, "10-30s":0, "30s-1m":0, "1-2m":0, "2-5m":0, "5-10m":0, "10-30m":0, "30m+":0}
    thresholds = [(10,"0-10s"),(30,"10-30s"),(60,"30s-1m"),(120,"1-2m"),(300,"2-5m"),(600,"5-10m"),(1800,"10-30m"),(float('inf'),"30m+")]
    for ip in ips:
        if (data := await redis.get(f"visitor:{ip.decode('utf-8') if isinstance(ip, bytes) else ip}")):
            t = json.loads(data).get("total_time_spent", 0)
            buckets[next(k for thr, k in thresholds if t < thr)] += 1
    return buckets

async def get_referrer_stats(redis, limit: int = 20):
    keys = await redis.keys("referrer_stats:*")
    referrer_counts = []
    for key in keys:
        source = key.decode('utf-8') if isinstance(key, bytes) else key.replace("referrer_stats:", "")
        if (count := await redis.get(key)): referrer_counts.append({"source": source, "count": int(count)})
    return sorted(referrer_counts, key=lambda x: x["count"], reverse=True)[:limit]

async def get_referrer_type_stats(redis):
    return  {ref_type: int(count) if (count := await redis.get(f"referrer_type:{ref_type}")) else 0
                for ref_type in ["direct", "social", "search", "referral", "unknown"]}

def parse_referrer(referrer: str) -> Dict[str, Any]:
    if not referrer or referrer == "direct": 
        return { "source": "Direct", "domain": None, "full_url": None, "type": "direct"}
    
    try:
        from urllib.parse import urlparse
        domain = (urlparse(referrer).netloc or urlparse(referrer).path.split('/')[0]).replace('www.', '')
        referrer_lower = referrer.lower()
        for social_domain, social_name in config.social_platforms.items():
            if social_domain in referrer_lower: return { "source": social_name, "domain": domain, "full_url": referrer[:200], "type": "social" }# Limit length
        for search_domain, search_name in config.search_engines.items():
            if search_domain in referrer_lower: return { "source": search_name, "domain": domain,  "full_url": referrer[:200], "type": "search" }
        return { "source": domain, "domain": domain, "full_url": referrer[:200], "type": "referral" }
    except: return { "source": "Unknown", "domain": None, "full_url": referrer[:200], "type": "unknown" }

async def render_referrer_stats_page(redis):
        top_refs = await get_referrer_stats(redis, limit=30)
        type_stats = await get_referrer_type_stats(redis)
        total = sum(type_stats.values())
        COLORS = {"direct":"#667eea","social":"#ff6b6b","search":"#4ecdc4","referral":"#45b7d1","unknown":"#95a5a6"}
        type_bars = [fasthtml_components.h_bar(t.title(), cnt, total, COLORS.get(t,"#999")) for t, cnt in type_stats.items() if total]
        ref_rows = [fh.Tr(fh.Td(f"#{i}"), fh.Td(f"{'🔍' if 'Google' in r['source'] else '📱' if any(s in r['source'] for s in ['Facebook','Twitter','Reddit']) else '🔗' if r['source']=='Direct' else '🌐'} {r['source']}"),
                          fh.Td(r["count"]), fh.Td(f"{r['count']/total*100:.1f}%" if total else "0%"), cls="visitor-row")
                   for i, r in enumerate(top_refs, 1)]
        return (fh.Titled("Referrer Analytics", fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0")),
                fh.Main(fh.H1("Traffic Sources & Referrers", cls="dashboard-title"),
                    fasthtml_components.stat_card("Total Tracked Visitors", f"{total:,}"),
                    fh.Div(fh.H2("Traffic by Type", cls="section-title"),
                           fh.Div(*type_bars if type_bars else [fh.P("No data", style="text-align:center;color:#999;")], cls="chart-bars-container")),
                    fh.Div(fh.H2("Top Referrer Sources", cls="section-title"),
                           fh.Table(fh.Tr(fh.Th("Rank"),fh.Th("Source"),fh.Th("Visitors"),fh.Th("Percentage")),
                                    *(ref_rows or [fh.Tr(fh.Td("No data", colspan=4, style="text-align:center;color:#999;padding:20px;"))]),
                                    cls="table visitors-table"), style="margin-top:30px;"),
                    fasthtml_components.nav_links(("← Back to visitors","/visitors"),("← Back to checkboxes","/")), cls="visitors-container"))

async def render_time_spent_stats_page(redis):
        print("⏱️  GET /time-spent-stats")
        stats, buckets = await get_time_stats(redis, 100), await get_time_buckets(redis, 500)
        bkt_colors = {"0-10s": "#e74c3c", "10-30s": "#e67e22", "30s-1m": "#f39c12", "1-2m": "#f1c40f",
                      "2-5m": "#2ecc71", "5-10m": "#27ae60", "10-30m": "#3498db", "30m+": "#9b59b6"}
        
        return (fh.Titled("Time Spent Analytics", fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0")),
                fh.Main(fh.H1("⏱️ Time Spent Analytics", cls="dashboard-title"),
                    fh.Div(fasthtml_components.stat_card("Total Time", f"{stats['total_time']/3600:.1f}h", f"{stats['total_time']:.0f}s"),
                           fasthtml_components.stat_card("Avg per Visitor", f"{stats['avg_per_visitor']/60:.1f}m", f"{len(stats['visitors'])} visitors"),
                           fasthtml_components.stat_card("Avg per Session", f"{stats['avg_per_session']/60:.1f}m", f"{stats['total_sessions']} sessions"),
                           fasthtml_components.stat_card("Visitors Tracked", f"{len(stats['visitors'])}", "with time data"), cls="stats-grid"),
                    fh.Div(fh.H2("Time Spent Distribution", cls="section-title"),
                           fh.P("How long visitors stay", style="text-align:center;color:#94a3b8;margin-bottom:20px;"),
                           fh.Div(fasthtml_components.h_chart(buckets, bkt_colors), cls="chart-container")),
                    fh.Div(fh.H2("Most Engaged Visitors (Top 30)", cls="section-title"),
                        fh.Table(fh.Tr(fh.Th("Rank"), fh.Th("IP"), fh.Th("Total Time"), fh.Th("Sessions"), 
                                       fh.Th("Avg Session"), fh.Th("Last Session"), fh.Th("Device"), fh.Th("Type"), fh.Th("Location")),
                            *[fh.Tr(fh.Td(f"#{i}"), fh.Td(v.get("ip")), 
                                    fh.Td(fh.Span(fasthtml_components.fmt_time(t := v.get("total_time_spent", 0)), 
                                        style=f"background:{'#9b59b6' if t>300 else '#3498db' if t>120 else '#2ecc71' if t>60 else '#95a5a6'};color:white;padding:4px 8px;border-radius:4px;font-weight:600;")),
                                    fh.Td(v.get("total_sessions", 0)), fh.Td(fasthtml_components.fmt_time(v.get("avg_session_duration", 0))),
                                    fh.Td(fasthtml_components.fmt_time(v.get("last_session_duration", 0))), fh.Td(v.get("device", "Unknown")),
                                    fh.Td(fasthtml_components.class_badge(v.get("classification", "Human"))),
                                    fh.Td(f"{v.get('city','Unknown')}, {v.get('country','Unknown')}", style="font-size:0.85em;"), cls="visitor-row")
                              for i, v in enumerate(stats["visitors"][:30], 1)] if stats["visitors"] else 
                            [fh.Tr(fh.Td("No data", colspan=9, style="text-align:center;color:#999;padding:20px;"))],
                            cls="table visitors-table"), style="margin-top:30px;"),
                    fasthtml_components.nav_links(("← Back to visitors", "/visitors"), 
                                    ("View Referrer Stats", "/referrer-stats", "background:#4ecdc4;"),
                                    ("← Back to checkboxes", "/")), cls="visitors-container"))

async def handle_heartbeat(request, redis):
        """Track that user is still active and update duration"""
        client_ip = get_real_ip(request)
        try:
            data = await request.json()
            duration = data.get("duration", 0)  # Duration in seconds from frontend
        except: duration = 0
        await update_session_activity(client_ip, redis)
        if duration > 0 and (visitor_data:=await redis.get(f"visitor:{client_ip}")):
                visitor = json.loads(visitor_data)
                visitor["current_session_duration"] = duration
                visitor["last_activity_time"] = time.time()
                await redis.set(f"visitor:{client_ip}", json.dumps(visitor) )
        return {"status": "ok", "duration": duration}

async def handle_session_end(request, redis):
        """End session and save final time spent"""
        client_ip = get_real_ip(request)
        try: duration = (await request.json()).get("duration", 0)  # Duration in seconds from frontend
        except: duration = 0
        if visitor_data := await redis.get(f"visitor:{client_ip}"):
            visitor = json.loads(visitor_data)
            visitor["total_time_spent"] = visitor.get("total_time_spent", 0) + duration
            visitor["last_session_duration"] = duration
            visitor["total_sessions"] = visitor.get("total_sessions", 0) + 1
            
            if visitor["total_sessions"] > 0: visitor["avg_session_duration"] = visitor["total_time_spent"] / visitor["total_sessions"]

            if (session_data := await redis.get(f"session:{client_ip}")):
                session = json.loads(session_data)
                visitor["total_actions"] = visitor.get("total_actions", 0) + session.get("actions", 0)
                visitor["max_scroll_depth"] = max(visitor.get("max_scroll_depth", 0), session.get("scroll_depth", 0))

            await redis.set(f"visitor:{client_ip}", json.dumps(visitor))
            await persistence.save_visitor_to_sqlite(visitor)
            print(f"[SESSION END] {client_ip} spent {duration:.1f} seconds")
        await redis.delete(f"session:{client_ip}")
        return {"status": "ok", "duration": duration}

def utc_to_local(timestamp): return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(config.LOCAL_TIMEZONE)

async def get_cached_visitors_data( redis, offset: int, limit: int, days: int ) -> Tuple[Dict, str]:
    cache_key = f"cache:visitors:{offset}:{limit}:{days}"

    cached = await redis.get(cache_key)
    if cached:
        try: return json.loads(cached), "cache-hit"
        except: await redis.delete(cache_key)

    # Real computation
    recent_ips = await redis.zrange("recent_visitors_sorted", offset, offset + limit - 1, desc=True)
    print(f"[VISITORS] Found {len(recent_ips)} IPs")

    visitors = []
    for ip_bytes in recent_ips:
        ip_str = ip_bytes.decode('utf-8') if isinstance(ip_bytes, bytes) else str(ip_bytes)
        raw = await redis.get(f"visitor:{ip_str}")
        if raw:
            try:
                v = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
                v["timestamp"] = float(v.get("timestamp", time.time()))
                visitors.append(v)
            except Exception as e: print(f"[VISITORS] JSON error for {ip_str}: {e}")

    print(f"[VISITORS] Loaded {len(visitors)} records")

    total_in_db = await redis.zcard("recent_visitors_sorted")
    total_count_raw = await redis.get("total_visitors_count")
    total_count = int(total_count_raw) if total_count_raw else 0
    print(f"[VISITORS] Total: {total_count}, DB: {total_in_db}")

    # Group by day
    visitors_by_day = {}
    for v in visitors:
        day = utc_to_local(v["timestamp"]).strftime("%Y-%m-%d")
        visitors_by_day.setdefault(day, []).append(v)

    # Chart data
    now_local = utc_to_local(time.time())
    chart_days_data = []
    for i in range(days - 1, -1, -1):
        target_date = (now_local.date() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        count = sum(1 for v in visitors if utc_to_local(v["timestamp"]).strftime("%Y-%m-%d") == target_date)
        date_display = (now_local.date() - dt.timedelta(days=i)).strftime("%a-%b-%d")
        chart_days_data.append((date_display, count))

    # Extra stats
    humans = sum(1 for v in visitors if "Human" in v.get("classification", ""))
    bots = len(visitors) - humans
    vpn_users = sum(1 for v in visitors if v.get("is_vpn", False))

    result = { "total_count": total_count, "total_in_db": total_in_db, "visitors_by_day": visitors_by_day, "chart_days_data": chart_days_data,
               "visitor_count": len(visitors), "stats": {"humans": humans, "bots": bots, "vpn_users": vpn_users}, }
    await redis.set(cache_key, json.dumps(result), ex=45)
    return result, "cache-miss"

async def render_visitors_page(request, redis, offset: int = 0, limit: int = 5, days: int = 30):
    client_ip = get_real_ip(request)
    referrer = request.headers.get('referer', '')
    # UTM fallback for GitHub (strips Referer header)
    if not referrer and request.query_params.get('utm_source') == 'github':
        referrer = 'https://github.com'

    await track_page_view(client_ip, "/visitors", referrer, redis)
    await track_referrer(client_ip, referrer, redis)

    days = max(7, min(days, 30))
    print(f"[VISITORS] Loading dashboard: offset={offset}, limit={limit}, window={days}")

    data, cache_status = await get_cached_visitors_data(redis, offset, limit, days)
    print(f"[VISITORS] {cache_status.upper()} - {data['visitor_count']} visitors")

    total_count = data["total_count"]
    total_in_db = data["total_in_db"]
    visitors_by_day = data["visitors_by_day"]
    chart_days_data = data["chart_days_data"]
    stats = data["stats"]

    # ─── Raw HTML table ───────────────────────────────────────────
    table_rows = []
    for day_key in sorted(visitors_by_day.keys(), reverse=True):
        day_visitors = visitors_by_day[day_key]
        day_display = datetime.strptime(day_key, "%Y-%m-%d").strftime("%A, %B %d, %Y")
        count = len(day_visitors)

        table_rows.append(  f'<tr class="day-separator"><td colspan="15">'
                            f'<div style="padding:10px 0;">'
                            f'<strong>{day_display}</strong>'
                            f'<span style="color:#667eea;margin-left:10px;"> ({count} visitor{"s" if count != 1 else ""})</span>'
                            f'</div></td></tr>' )

        for v in day_visitors:
            is_vpn = v.get("is_vpn", False)
            is_relay = "Relay" in v.get("classification", "")
            c = v.get("classification", "Human")
            first_ref = v.get("first_referrer", {})
            last_ref = v.get("last_referrer", {})
    
            row = ( '<tr class="visitor-row">'
                    f'<td data-label="Time">{utc_to_local(v["timestamp"]).strftime("%m/%d %H:%M")}</td>'
                    f'<td data-label="IP">{v.get("ip", "-")}</td>'
                    f'<td data-label="Device">{v.get("device", "?")}</td>'
                    f'<td data-label="Security">{fasthtml_components.sec_badge(is_vpn, is_relay)}</td>'
                    f'<td data-label="Category">'
                    f'<div><div style="font-weight:bold;color:{"#ff9500" if "Bot" in c else "#007aff"};">{c}</div>'
                    f'<div style="font-size:0.8em;opacity:0.7;">{v.get("usage_type", "Residential")}</div></div></td>'
                    f'<td data-label="First Source">{fasthtml_components.ref_badge(first_ref.get("source", "Direct"), first_ref.get("type", "direct"))}</td>'
                    f'<td data-label="Last Source">{fasthtml_components.ref_badge(last_ref.get("source", "Direct"), last_ref.get("type", "direct"))}</td>'
                    f'<td data-label="ISP/Org" style="font-size:0.85em;">{(v.get("isp") or "-")[:40]}</td>'
                    f'<td data-label="City">{v.get("city", "-")}</td>'
                    f'<td data-label="Zip">{v.get("zip", "-")}</td>'
                    f'<td data-label="Country">{v.get("country", "-")}</td>'
                    f'<td data-label="Visits"><span class="visit-badge">{v.get("visit_count", 1)}</span></td>'
                    f'<td data-label="Last seen">{utc_to_local(v["timestamp"]).strftime("%H:%M:%S")}</td>'
                    f'<td data-label="Time Spent">{v.get("total_time_spent", 0)/60:.1f}m</td>'
                    f'<td data-label="Actions">{v.get("total_actions", 0)}</td>'
                    f'<td data-label="Scroll %">{v.get("max_scroll_depth", 0):.0f}%</td>'
                    f'<td data-label="Last Page">{v.get("last_page", "/")[:20]}</td>'
                    # In your row string
                    #f'<td data-label="Referrers">{", ".join(r["source"] for r in v.get("all_referrers", [])) or "-"}</td>'
                    '</tr>' )
            table_rows.append(row)

    table_html = "".join(table_rows)
    if not table_html:
        table_html = '<tr><td colspan="15" style="text-align:center;padding:2rem;color:#999;">No visitors</td></tr>'

    return (
        fh.Titled("Visitors Dashboard", fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0, maximum-scale=5.0")),
        fh.Main(
            fh.H1("Recent Visitors Dashboard", cls="dashboard-title"),
            fh.Div(
                fasthtml_components.stat_card("Total Visitors", f"{total_count:,}"),
                fasthtml_components.stat_card("Humans", f"{stats['humans']:,}"),
                fasthtml_components.stat_card("Bots", f"{stats['bots']:,}"),
                fasthtml_components.stat_card("VPN Users", f"{stats['vpn_users']:,}"),
                cls="stats-grid" ),
            fasthtml_components.pagination(offset, limit, total_in_db, "/visitors", {"days": days}),
            fh.Div( fh.H2(f"Visitors by Day - Central Time", cls="section-title", style="margin:0;"),
                    fasthtml_components.range_sel(days, limit, offset, "/visitors"),
                    style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:15px;"
                ),
            fh.Div( fh.Div(fasthtml_components.gradient_chart(chart_days_data), cls="chart-bars-container"), cls="chart-container" ),
            fasthtml_components.nav_links(  ("← Back to checkboxes", "/"),
                                            ("View Referrer Stats →", "/referrer-stats", "background:#4ecdc4;"),
                                            ("View Time Stats →", "/time-spent-stats", "background:#9b59b6;"),
                                            ("📝 How This Was Built →", "/blog", "background:#e67e22;"),
                                            ("Blog visitors stats →", "/blog_visitors", "background:#e67e22;")
            ),
            fh.Div( fh.H2(f"Visitors Dashboard (Last {limit} Visitors)", cls="section-title"),
                    fh.P("← Scroll horizontally to see all columns →", 
                    style="text-align:center;color:#667eea;font-size:0.9em;margin-bottom:10px;font-weight:600;"),
                    # ← NEW: Add the hint here
                    fh.P("← Scroll horizontally if needed (best on desktop)", cls="mobile-scroll-hint"),
                    fh.Div( fh.NotStr(
                                '<table class="table visitors-table">'
                                '<thead><tr>'
                                '<th>Time</th><th>IP</th><th>Device</th><th>Security</th><th>Category</th>'
                                '<th>First Source</th><th>Last Source</th><th>ISP/Org</th>'
                                '<th>City</th><th>Zip</th><th>Country</th><th>Visits</th><th>Last Seen</th>'
                                '<th>Time Spent</th><th>Actions</th><th>Scroll %</th><th>Last Page</th>'
                                '</tr></thead>'
                                '<tbody>'
                                f'{table_html}'
                                '</tbody></table>' ),
                            cls="table-wrapper", style="overflow-x:auto;-webkit-overflow-scrolling:touch;" ), cls="table-wrapper"
                ),
                fasthtml_components.pagination(offset, limit, total_in_db, "/visitors", {"days": days}),
                fasthtml_components.nav_links(("← Back to checkboxes", "/")), cls="visitors-container"))


async def get_cached_blog_visitors_data(redis, offset: int, limit: int, days: int) -> tuple[dict, str]:
    cache_key = f"cache:blog_visitors:{offset}:{limit}:{days}"

    cached = await redis.get(cache_key)
    if cached:
        try:
            return json.loads(cached), "cache-hit"
        except:
            await redis.delete(cache_key)

    # Fetch from GLOBAL recent_visitors_sorted (proven to persist)
    # Over-fetch to allow filtering
    fetch_limit = offset + limit * 5  # generous over-fetch
    recent_ips = await redis.zrange("recent_visitors_sorted", 0, fetch_limit - 1, desc=True)
    print(f"[BLOG VISITORS] Fetched {len(recent_ips)} global recent IPs for filtering")

    visitors = []
    for ip_bytes in recent_ips:
        ip = ip_bytes.decode('utf-8') if isinstance(ip_bytes, bytes) else str(ip_bytes)
        raw = await redis.get(f"visitor:{ip}")
        if raw:
            try:
                v = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
                # Filter: only include if they viewed /blog
                if v.get("pages_viewed", {}).get("/blog", 0) > 0:
                    ts = float(v.get("last_seen", v.get("timestamp", time.time())))
                    v["timestamp"] = ts
                    visitors.append(v)
                    print(f"[DEBUG-BLOG] Included visitor {ip} (pages_viewed has /blog)")
                # else:
                #     print(f"[DEBUG-BLOG] Skipped {ip} - no /blog in pages_viewed")
            except Exception as e:
                print(f"[BLOG VISITORS] JSON error for {ip}: {e}")

    # Apply offset & limit AFTER filtering
    visitors = visitors[offset : offset + limit]

    print(f"[BLOG VISITORS] Loaded {len(visitors)} valid blog visitors after filter")

    # ─── Identical processing as main visitors ──────────────────────────────────
    visitors_by_day = {}
    for v in visitors:
        day = utc_to_local(v["timestamp"]).strftime("%Y-%m-%d")
        visitors_by_day.setdefault(day, []).append(v)

    now_local = utc_to_local(time.time())
    chart_days_data = []
    for i in range(days - 1, -1, -1):
        target_date = (now_local.date() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        count = sum(1 for v in visitors if utc_to_local(v["timestamp"]).strftime("%Y-%m-%d") == target_date)
        date_display = (now_local.date() - dt.timedelta(days=i)).strftime("%a-%b-%d")
        chart_days_data.append((date_display, count))

    humans = sum(1 for v in visitors if "Human" in v.get("classification", ""))
    bots = len(visitors) - humans
    vpn_users = sum(1 for v in visitors if v.get("is_vpn", False))

    # Totals: filtered count for "showing", global unique blog for headline
    total_in_db = len(visitors)  # current slice
    total_unique_blog = await redis.scard("blog:unique_ips") or sum(1 for v in visitors if "/blog" in v.get("pages_viewed", {}))
    total_count = total_unique_blog

    result = {
        "total_count": total_count,
        "total_in_db": total_in_db,
        "visitors_by_day": visitors_by_day,
        "chart_days_data": chart_days_data,
        "visitor_count": len(visitors),
        "stats": {"humans": humans, "bots": bots, "vpn_users": vpn_users},
    }

    await redis.set(cache_key, json.dumps(result), ex=60)
    return result, "cache-miss"



async def render_blog_visitors_stats_page(request, redis, offset: int = 0, limit: int = 5, days: int = 30):
    days = max(7, min(days, 30))
    print(f"[BLOG VISITORS] Loading: offset={offset}, limit={limit}, days={days}")

    # Track view of stats page
    client_ip = get_real_ip(request)
    referrer = request.headers.get('referer', 'direct')
    await track_page_view(client_ip, "/blog_visitors", referrer, redis)
    await track_referrer(client_ip, referrer, redis)

    data, cache_status = await get_cached_blog_visitors_data(redis, offset, limit, days)
    print(f"[BLOG VISITORS] {cache_status.upper()} - {data['visitor_count']} blog visitors")

    total_count = data["total_count"]
    total_in_db = data["total_in_db"]
    visitors_by_day = data["visitors_by_day"]
    chart_days_data = data["chart_days_data"]
    stats = data["stats"]

    table_rows = []
    for day_key in sorted(visitors_by_day.keys(), reverse=True):
        day_visitors = visitors_by_day[day_key]
        day_display = datetime.strptime(day_key, "%Y-%m-%d").strftime("%A, %B %d, %Y")
        count = len(day_visitors)

        table_rows.append(  f'<tr class="day-separator"><td colspan="13">'
                            f'<div style="padding:10px 0;">'
                            f'<strong>{day_display}</strong>'
                            f'<span style="color:#667eea;margin-left:10px;"> ({count} blog visitor{"s" if count != 1 else ""})</span>'
                            f'</div></td></tr>'
        )
        for v in day_visitors:
            is_vpn = v.get("is_vpn", False)
            is_relay = "Relay" in v.get("classification", "")
            c = v.get("classification", "Human")
            first_ref = v.get("first_referrer", {})
            last_ref = v.get("last_referrer", {})

            row = ( '<tr class="visitor-row">'
                    f'<td data-label="Time">{utc_to_local(v["timestamp"]).strftime("%m/%d %H:%M")}</td>'
                    f'<td data-label="IP">{v.get("ip", "-")}</td>'
                    f'<td data-label="Device">{v.get("device", "?")}</td>'
                    f'<td data-label="Security">{fasthtml_components.sec_badge(is_vpn, is_relay)}</td>'
                    f'<td data-label="Category">'
                    f'<div><div style="font-weight:bold;color:{"#ff9500" if "Bot" in c else "#007aff"};">{c}</div>'
                    f'<div style="font-size:0.8em;opacity:0.7;">{v.get("usage_type", "Residential")}</div></div></td>'
                    f'<td data-label="First Source">{fasthtml_components.ref_badge(first_ref.get("source", "Direct"), first_ref.get("type", "direct"))}</td>'
                    f'<td data-label="Last Source">{fasthtml_components.ref_badge(last_ref.get("source", "Direct"), last_ref.get("type", "direct"))}</td>'
                    f'<td data-label="ISP/Org" style="font-size:0.85em;">{(v.get("isp") or "-")[:40]}</td>'
                    f'<td data-label="City">{v.get("city", "-")}</td>'
                    f'<td data-label="Zip">{v.get("zip", "-")}</td>'
                    f'<td data-label="Country">{v.get("country", "-")}</td>'
                    f'<td data-label="Visits"><span class="visit-badge">{v.get("visit_count", 1)}</span></td>'
                    f'<td data-label="Last seen">{utc_to_local(v["timestamp"]).strftime("%H:%M:%S")}</td>'
                    f'<td data-label="Time Spent">{v.get("total_time_spent", 0)/60:.1f}m</td>'
                    f'<td data-label="Actions">{v.get("total_actions", 0)}</td>'
                    f'<td data-label="Scroll %">{v.get("max_scroll_depth", 0):.0f}%</td>'
                    f'<td data-label="Last Page">{v.get("last_page", "/")[:20]}</td>'
                    '</tr>'
            )
            table_rows.append(row)

    table_html = "".join(table_rows)
    if not table_html:
        table_html = '<tr><td colspan="13" style="text-align:center;padding:2rem;color:#999;">No blog visitors recorded yet</td></tr>'

    # Safe recent title
    first_day_visitors_count = 0
    if visitors_by_day:
        first_day = list(visitors_by_day.keys())[0]
        first_day_visitors_count = len(visitors_by_day[first_day])

    recent_title = (f"Recent Blog Visitors (showing {first_day_visitors_count} of last {limit})"
                    if visitors_by_day else "Recent Blog Visitors (no data yet)" )

    return (
        fh.Titled("Blog Visitors Dashboard", fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0, maximum-scale=5.0")),
        fh.Main(
            fh.H1("Blog Visitors Dashboard", cls="dashboard-title"),
            # fh.Div( fasthtml_components.stat_card("Unique Blog Visitors", f"{total_count:,}"),
            #         fasthtml_components.stat_card("Humans (blog)", f"{stats['humans']:,}"),
            #         fasthtml_components.stat_card("Bots (blog)", f"{stats['bots']:,}"),
            #         fasthtml_components.stat_card("VPN Users (blog)", f"{stats['vpn_users']:,}"), cls="stats-grid"
            fh.Div( fasthtml_components.stat_card("Unique Blog Visitors", f"{data['visitor_count']:,}"),
                    fasthtml_components.stat_card("Humans (blog)", f"{stats['humans']:,}"),
                    fasthtml_components.stat_card("Bots (blog)", f"{stats['bots']:,}"),
                    fasthtml_components.stat_card("VPN Users (blog)", f"{stats['vpn_users']:,}"),cls="stats-grid")
            ),fasthtml_components.pagination(offset, limit, total_in_db, "/blog_visitors", {"days": days}),
            fh.Div( fh.H2(f"Blog Visitors by Day – Central Time", cls="section-title", style="margin:0;"),
                    fasthtml_components.range_sel(days, limit, offset, "/blog_visitors"),
                    style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:15px;"
            ),
            fh.Div( fh.Div(fasthtml_components.gradient_chart(chart_days_data), cls="chart-bars-container"), cls="chart-container" ),
            fh.Div(
                fh.H2(recent_title, cls="section-title"),
                fh.P("← Scroll horizontally to see all columns →", style="text-align:center;color:#667eea;font-size:0.9em;margin-bottom:10px;font-weight:600;"),
                fh.P("← Scroll horizontally if needed (best on desktop)", cls="mobile-scroll-hint"),
                fh.Div(
                    fh.NotStr(
                        '<table class="table visitors-table">'
                        '<thead><tr>'
                        '<th>Time</th><th>IP</th><th>Device</th><th>Security</th><th>Category</th>'
                        '<th>First Source</th><th>Last Source</th><th>ISP/Org</th>'
                        '<th>City</th><th>Country</th><th>Visits</th><th>Last seen</th>'
                        '<th>Time Spent</th><th>Actions</th><th>Scroll %</th><th>Last Page</th>'
                        '</tr></thead>'
                        '<tbody>'
                        f'{table_html}'
                        '</tbody></table>'
                    ), cls="table-wrapper",  style="overflow-x:auto;-webkit-overflow-scrolling:touch;"  ), cls="table-wrapper"
            ),
            fasthtml_components.pagination(offset, limit, total_in_db, "/blog_visitors", {"days": days}),
            fasthtml_components.nav_links(  ("← Back to checkboxes", "/"),
                                            ("All Visitors →", "/visitors", "background:#667eea;"),
                                            ("Referrers →", "/referrer-stats", "background:#4ecdc4;"),
                                            ("Time Stats →", "/time-spent-stats", "background:#9b59b6;"),
                                            ("📝 Blog Post →", "/blog", "background:#e67e22;")
            ),cls="visitors-container"
            ))

TRACKER_JS= """
    const tracker = { startTime: Date.now(), lastHeartbeat: Date.now(), scrollDepth: 0,
        init() {
            this.sendHeartbeat(); setInterval(() => { this.sendHeartbeat(); }, 10000);
            const activityEvents = ['click', 'scroll', 'keypress', 'mousemove', 'touchstart'];
            activityEvents.forEach(event => { document.addEventListener(event, () => { this.onUserActivity(); }, { passive: true }); });

            // chunk-based scroll depth tracking
            const TOTAL_CHUNKS = 500; // 1,000,000 / 2,000
            let chunksLoaded = 0;
            document.addEventListener('htmx:afterRequest', (e) => {
                if (e.detail.pathInfo?.requestPath?.includes('/chunk/')) {
                    chunksLoaded++;
                    const depth = Math.round((chunksLoaded / TOTAL_CHUNKS) * 100);
                    this.scrollDepth = Math.max(this.scrollDepth, depth);
                    fetch('/track-scroll', { method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({depth: depth}) 
                    }).catch(err => console.log('Scroll tracking failed:', err)); } });

            window.addEventListener('beforeunload', () => { this.endSession(); });

            document.addEventListener('visibilitychange', () => {
                if (document.hidden) { this.endSession(); } else { this.sendHeartbeat(); } });
            window.addEventListener('pagehide', () => { this.endSession(); }); },

        onUserActivity() { const now = Date.now(); if (now - this.lastHeartbeat > 3000) { this.sendHeartbeat(); } },  

        sendHeartbeat() { const duration = (Date.now() - this.startTime) / 1000;
            fetch('/heartbeat', { method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ duration: duration, timestamp: Date.now() }), keepalive: true
            }).catch(err => console.log('Heartbeat failed:', err)); this.lastHeartbeat = Date.now(); },

        endSession() { const duration = (Date.now() - this.startTime) / 1000;
            console.log('[TRACKER] Ending session:', duration.toFixed(1) + 's');
            const data = { duration: duration, scrollDepth: this.scrollDepth, timestamp: Date.now() };
            if (navigator.sendBeacon) { const blob = new Blob([JSON.stringify(data)], {type: 'application/json' });
            const sent = navigator.sendBeacon('/session-end', blob); console.log('[TRACKER] sendBeacon:',sent );
            } else { fetch('/session-end', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data), keepalive: true
                }).catch(err => console.log('Session end failed:', err));  } } };
    
    tracker.init(); 
    """