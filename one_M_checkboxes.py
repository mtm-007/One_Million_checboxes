import time
from asyncio import Lock
from pathlib import Path
#from turtle import width
from uuid import uuid4

from fasthtml.core import viewport
from fasthtml.js import NotStr
import modal
from modal import Image
import fasthtml.common as fh
import httpx
import asyncio
import json
import subprocess
import pytz
from datetime import datetime, timezone
from redis.asyncio import Redis


N_CHECKBOXES=1000000
VIEW_SIZE= 5000
LOAD_MORE_SIZE= 2000

app = modal.App("one-million-checkboxes")

volume = modal.Volume.from_name("redis-data-vol", create_if_missing=True)

checkboxes_key = "checkboxes"
checkboxes_bitmap_key= "checkboxes_bitmap"

clients = {}
clients_mutex = Lock()

checkbox_cache = {}
checkbox_cache_loaded_at = 0.0
CHECKBOX_CACHE_TTL = 600 #keep for 10 minutes in memory

GEO_TTL_REDIS = 86400 
CLIENT_GEO_TTL = 300.0  #client level in memory small cache (5min)
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

def utc_to_local(timestamp):
    utc_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    local_dt = utc_dt.astimezone(LOCAL_TIMEZONE)
    return local_dt

#New geolocation helper function
async def get_geo_from_providers(ip:str, redis):
    #primary provider
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"https://ipwho.is/{ip}?security=1")
        if r.status_code == 200:
            data = r.json()
            if data.get("success"):
                print(f"[GEO] ✅ ipwho.is succesfully resolved {ip} -> {data.get('city')}, {data.get('country')}")
                sec = data.get("security", {})
                conn = data.get("connection", {})
                
                #usage logic
                usage = "Residential"
                org_lower = conn.get("org", "").lower()
                if sec.get("hosting"): usage = "Data Center"
                elif any(x in org_lower for x in ["uni", "college", "school"]): usage = "Education"
                elif any(x in org_lower for x in ["corp", "inc", "ltd"]):usage = "Business"
                elif data.get("type") == "Mobile": usage = "Cellular"

                is_relay_val = sec.get("relay", False) or "icloud" in conn.get("isp", "").lower() or "apple" in conn.get("org", "").lower()

                #normalize the data to match entry format
                return{
                    "ip": ip,
                    "city": data.get("city"),
                    "postal": data.get("postal"),
                    "country": data.get("country"),
                    "region": data.get("region"),
                    "is_vpn": sec.get("vpn", False) or sec.get("proxy", False),
                    "isp": conn.get("isp"),#data.get("connection", {}).get("isp"),
                    "is_hosting": sec.get("hosting", False),#critical for bots
                    "org": conn.get("org"),
                    "asn": conn.get("asn"),
                    "usage_type": usage,
                    "is_relay": is_relay_val,#sec.get("relay", False) or "icloud private relay" in conn.get("isp", "").lower(),
                    "provider": "ipwho.is"
                }
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
                    "ip": ip,
                    "city": data.get("city"),
                    "isp": data.get("isp"),
                    "usage_type": "Privacy Relay" if is_relay_val else usage,
                    "is_vpn": data.get("proxy", False),
                    "is_hosting": data.get("hosting", False),
                    "is_relay": is_relay_val,#"icloud private relay" in data.get("isp", "").lower(),
                    "provider": "ip-api.com"
                }
                # await redis.set(f"geo:{ip}", json.dumps(data))#, ex=86400)
                # return data
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
        is_relay = geo.get("is_relay", False)

        #Known Good Bots (Search Engines)
        good_bots = ["googlebot", "bingbot", "yandexbot", "baiduspider", "duckduckbot"]
        is_good_bot = any(bot in ua_lower for bot in good_bots)
        
        #programming libraries/scrapers
        scripts = ["python-requests", "aiohttp", "curl", "wget", "postman", "headless"]
        is_script = any(s in ua_lower for s in scripts)

        #final classsification
        if is_good_bot: classification = "Good Bot (Search Engine)"
        elif is_relay: classification = "Human (Privacy/Relay)"
        elif is_hosting or is_script: classification = "Bot/Server"
        else: classification = "Human"

        visit_count = (json.loads(existing).get("visit_count", 1) + 1) if existing else 1
        
        entry = {
            "ip": ip,
            "device" : device_str,
            "user_agent": user_agent[:120],
            "classification": classification,
            "usage_type": geo.get("usage_type", "Unknown"),
            "isp": geo.get("isp") or "-",
            "city": geo.get("city") or geo.get("region", "Unknown"),
            "zip": geo.get("postal") or geo.get("zip") or "-",
            "is_vpn": geo.get("is_vpn", False),
            "country": geo.get("country") or geo.get("country_name"),
            "timestamp": time.time(),
            "visit_count" : visit_count,
        }

        await redis.set(visitors_key, json.dumps(entry)) #save permanently
        await redis.zadd("recent_visitors_sorted", {ip:time.time()}) #maintain a sorted set by timestamp
        #await redis.zremrangebyrank("recent_visitors_sorted", 0,-101) #keep only last 100

        if is_new_visitor:
            await redis.incr("total_visitors_count")
            print(f"[VISITOR] New visitor from {geo.get('city', 'Unknown',)}, {geo.get('country', 'Unknown')}")
        else:
            print(f"[VISITOR] Returning visitor {ip}  (visit #{visit_count}")
        
        status_icon = "👤" if "Human" in classification else "🤖"
        print(f"['VISITOR'] {status_icon} { ip} | {classification} | Type: {entry['usage_type']} | ISP: {entry['isp']}")

    except Exception as e:
        print(f"[ERROR] Failed to record visitor: {e}")

css_path_local = Path(__file__).parent / "style_v2.css"
css_path_remote = "/assets/style_v2.css"


#helper function for ip address
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

app_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("python-fasthtml==0.12.36", "httpx==0.27.0" ,"redis>=5.3.0", "pytz")
    .apt_install("redis-server").add_local_file(css_path_local,remote_path=css_path_remote)
    )

@app.function( 
        image = app_image, 
        max_containers=1,
        volumes={"/data": volume},
        #keep_warm=1,
        timeout=3600,)

@modal.concurrent(max_inputs=1000)
@modal.asgi_app()
def web():
    # Start redis server locally inside the container (persisted to volume)
    
    redis_process = subprocess.Popen(
        [   "redis-server", 
            "--protected-mode", "no",
            "--bind","127.0.0.1", 
            "--port", "6379", 
            "--dir", "/data", #store data in persistent volume
            "--save", "60", "1", #save every minute, if 1 change
            "--save", "" ] #disable all other automatic saves
        ,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    time.sleep(1)

    redis = Redis.from_url("redis://127.0.0.1:6379")
    print("Redis server started succesfully with persistent storage")

    async def startup_migration():
        """Run migration on startup"""
        #await migrate_litst_to_bitmap()
        #await diagnose_redis_state()
        await redis.setbit(checkboxes_bitmap_key, N_CHECKBOXES - 1, 0)
        print("[STARTUP] Bitmap initialized/verified")
        print("[STARTUP] Migration check complete")

    
    async def init_bitmap():
        """Ensure bitmap exists (optional -Redis creare)"""
        #await redis.setbit(checkboxes_bitmap_key, N_CHECKBOXES - 1, 0)

    async def get_checkbox_range_cached(start_idx: int, end_idx:int):
        """ Load a specific range of chekcboxes, with caching"""
        #check if we have them in cache
        cached_values = []
        missing_indices = []
        #global checkbox_cache, checkbox_cache_loaded_at

        for i in range(start_idx, end_idx):
            if i in checkbox_cache:
                cached_values.append((i, checkbox_cache[i]))
            else:
                missing_indices.append(i)
        
        if missing_indices:
            #use pipeline for batch loading
            pipe = redis.pipeline()
            for idx in missing_indices:
                pipe.getbit(checkboxes_bitmap_key, idx)
            
            results = await pipe.execute()

            #cache the results
            for idx, result in zip(missing_indices, results):
                value = bool(result) #json.loads(result) if result is not None else False
                checkbox_cache[idx] = value
                cached_values.append((idx, value))

        #sort by index to maintain order
        cached_values.sort(key=lambda x:x[0])
        return [v for _, v in cached_values]
         
    async def get_status():
        """ Get checked/unchecked counts - use redis directly, not cache"""
        checked = await redis.bitcount(checkboxes_bitmap_key)
        unchecked = N_CHECKBOXES - checked
        return checked,unchecked

    async def on_shutdown():
        print("Shutting down... Saving Redis data")
        try:
            await redis.save()
            print("Redis data saved succesfully")
        except Exception as e:
            print(f"Error saving Redis data: {e}")

        await redis.close() #not necessarily needed here just best practice
        redis_process.terminate()
        redis_process.wait()
        volume.commit()
        print("Volume committed  -data persisted")

    style= open(css_path_remote, "r").read()
    app, _= fh.fast_app(
        on_startup=[startup_migration],
        on_shutdown=[on_shutdown],
        hdrs=[fh.Style(style)],
    )

    metrics_for_count = { "request_count" : 0,  "last_throughput_log" : time.time() }
    throughput_lock = asyncio.Lock()

    #ASGI Middleware for latency + throughput logging
    @app.middleware("http")
    async def metrics_middleware(request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = (time.time() - start) * 1000 #ms
        #----log latency
        print(f"[Latency] {request.url.path} -> {duration:.2f} ms")

        #update throughput counter
        async with throughput_lock:
            metrics_for_count["request_count"] +=1
            now = time.time()

            #log throughput every 5 seconds
            if now - metrics_for_count["last_throughput_log"] >=5:
                rsp = metrics_for_count["request_count"] / (now - metrics_for_count["last_throughput_log"])
                print(f"[THROUGHPUT] {rsp:.2f} req/sec over last 5s")
                metrics_for_count["request_count"] = 0
                metrics_for_count["last_throughput_log"] = now
        return response

    @app.get("/fix-my-data")
    async def fix_data():
        print("[MIGRATION] Starting visitor data migration for legacy record...")
        visitor_keys = await redis.keys("visitor:*")
        print(f"[MIGRATION] Found {len(visitor_keys)} records to check.")
        updated_count = 0

        for key in visitor_keys:
            raw_data = await redis.get(key)
            if not raw_data: continue
            record = json.loads(raw_data)
            record["device"] = get_device_info(record.get("user_agent", ""))

            if "isp" not in record or record.get("isp") in ["-", "Unknown", "Unknown (Legacy)"]:
            #if "classification" not in record:
                ip = record.get("ip")
                print(f"[MIGRATION] Fetching missing data for: {ip}")
                try:
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        r = await client.get(f"http://ip-api.com/json/{ip}?fields=66842239")
                    if r.status_code == 200:
                        geo = r.json()
                        isp = geo.get("isp") or geo.get("org") or "Unknown"
                        is_hosting = geo.get("is_hosting",False)
                        
                        #check redis cache first, then hit api
                        #geo_data = await get_geo_from_providers(ip, redis)

                        #re-run bot detection logic on old records
                        ua_lower = record.get("user_agent", "").lower()
                        is_cloud = "icloud" in isp or "apple relay" in isp
                        #is_hosting = geo_data.get("is_hosting",False)
                        #is_relay = geo_data.get("is_relay",False)

                        if any(bot in ua_lower for bot in ["googlebot", "bingbot", "yandexbot", "baiduspider", "duckduckbot"]): classification = "Good Bot (Search Engine)"
                        #elif is_relay: classification = "Human (Privacy/Relay)"
                        elif is_cloud: classification = "Human (Privacy/Relay)"
                        elif is_hosting or any(s in ua_lower for s in ["python-requests", "aiohttp", "curl", "wget", "postman", "headless"]): classification = "Bot/Server"
                        else: classification = "Human"

                        #update record while preserving old data, only over write whats new
                        updated_record = { **record, "isp": isp, "org": geo.get("org", isp),
                                        "is_hosting": is_hosting, "classification": classification,
                                        "usage_type": "Data Center" if is_hosting else "Residential"
                                        }

                        await redis.set(key, json.dumps(updated_record))
                        updated_count +=1
                        
                        print(f"[MIGRATION]Success! updated {ip}: {isp}.")

                        # Very important: Also update the GEO CACHE so future visits are fast
                        await redis.set(f"geo:{ip}", json.dumps(updated_record))#,ex=86400 * 7)
                except Exception as e:
                    print(f"[MIGRATION] Error updating {ip}: {e}")
                await asyncio.sleep(0.5)

        return f"Success! {updated_count} records enriched."
    
    @app.get("/stats")
    async def stats():
        checked, unchecked = await get_status()
        print(f"[STATS] Checked: {checked:,}, Unchecked: {unchecked:,}")
        return fh.Div(
            fh.Span(f"{checked:,}", cls="status-checked"),
            " checked • ",
            fh.Span(f"{unchecked:,}",cls="status-unchecked"),
            " unchecked", cls="stats", id="stats", hx_get="every 2s", hx_swap="outerHTML")
    
    @app.get("/chunk/{client_id}/{offset}")
    async def chunk(client_id:str, offset:int):
        html = await _render_chunk(client_id,offset)
        return fh.NotStr(html)
    
    async def _render_chunk(client_id:str, offset:int)->str:
        #lazy load a chunk of checkboxes
        #await init_checkboxes()

        start_idx = offset
        end_idx = min(offset + LOAD_MORE_SIZE, N_CHECKBOXES)
        print(f"[CHUNK] Loading {start_idx:,}-{end_idx:,} for {client_id[:8]}")

        #load only this range
        checked_values = await get_checkbox_range_cached(start_idx, end_idx)

        parts =[]
        for i, is_checked in enumerate(checked_values, start=start_idx):
            checked_attr = "checked" if is_checked else ''
            parts.append(
                f'<input type="checkbox" id="cb-{i}" class="cb" {checked_attr} '
                f'hx-post="/toggle/{i}/{client_id}" hx-swap="none">'
            )
        html = "".join(parts)

        if end_idx < N_CHECKBOXES:
            next_offset = end_idx
            trigger = (
                '<span class="lazy-trigger" '
                f'hx-get="/chunk/{client_id}/{next_offset}" '
                'hx-trigger="intersect once" '
                'hx-target="#grid-container" '
                'hx-swap="beforeend">' 
                '</span>'
            )
            html += trigger

        return html
    
    @app.get("/")
    async def get(request):
        client_ip = get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')

        #register a new client
        client = Client()
        async with  clients_mutex:
            clients[client.id] = client

        #Load checkboxes immediately
        #await init_checkboxes()
        checked, unchecked = await get_status()

        geo = await get_geo(client_ip, redis)
        await record_visitors(client_ip,user_agent, geo, redis)

        first_chunk_html= await _render_chunk(client.id, offset=0)

        return( 
            fh.Titled(f"One Million Checkboxes"),
            fh.Main(
                fh.Div(
                    NotStr("""
                        <script data-name="BMC-Widget" data-cfasync="false" 
                            src="https://cdnjs.buymeacoffee.com/1.0.0/widget.prod.min.js" 
                            data-id="gptagent.unlock" 
                            data-description="Support me on Buy me a coffee!" 
                            data-message="" 
                            data-color="#FFDD00" 
                            data-position="top" 
                            data-x_margin="0" 
                            data-y_margin="0">
                        </script>
                    """),
                    #style="display: flex; justify-content: center; margin-bottom: 20px;"
                #),
                    # fh.A(
                    #     fh.Img(
                    #         src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png", #or external link from github
                    #         alt = "Buy Me A Coffee",
                    #         style= "height: 60px !important; width: 217px !important;"
                    #         ),
                    #     href="https://buymeacoffee.com/gptagent.unlock/checkout",
                    #     target="_blank"
                    #     ),
                    fh.H1(f" One Million Checkboxes"),
                    style="display: flex; flex-direction: column; align-items: center; gap: 10px;" 
                ),
                fh.Div( 
                    fh.Span(f"{checked:,}", cls="status-checked"), " checked • ",
                    fh.Span(f"{unchecked:,}",cls="status-unchecked"), " unchecked", 
                    cls="stats", id="stats", hx_get="/stats",
                    hx_trigger="every 1s",hx_swap="outerHTML"
                ),
                fh.Div(
                    fh.NotStr(first_chunk_html), #preload first chunk
                    cls="grid-container", id="grid-container",
                    hx_get=f"/diffs/{client.id}",#critical for poll diffs
                    hx_trigger="every 500ms",hx_swap="none"
                ),
                fh.Div("Made with FastHTML + Redis deployed with Modal", cls="footer"), 
                cls="container", 
                ))

    #users submitting checkbox toggles
    @app.post("/toggle/{i}/{client_id}")
    async def toggle(request, i:int, client_id:str):
        async with clients_mutex:
            client = clients.get(client_id)
                
            if i in checkbox_cache:
                current = checkbox_cache[i]
            else:
                bit = await redis.getbit(checkboxes_bitmap_key, i)
                current = bool(bit)#json.loads(raw) if raw is not None else False

            new_value = not current
            checkbox_cache[i] = new_value #Update cache

            print(f"[TOGGLE] index{i}: {current} -> {new_value}")

            try:
                await redis.setbit(checkboxes_bitmap_key, i, 1 if new_value else 0)

                bit_value = await redis.getbit(checkboxes_bitmap_key, i)
                print(f"[TOGGLE] Verified bitmap[{i}] = {bit_value}")
            except Exception as e:
                print(f"[TOGGLE ERROR] Failed to update Redis: {e}")

            expired = []
            for client in clients.values():
                if client.id == client_id:
                    continue

                #clean up old clients
                if not client.is_active():
                    expired.append(client.id)
                
                client.add_diff(i)#add diff to client fpr when they next poll

            for client_id in expired:
                del clients[client_id]

        checked, unchecked = await get_status()

        return fh.Div( 
                    fh.Span(f"{checked:,}", cls="status-checked"), " checked • ",
                    fh.Span(f"{unchecked:,}",cls="status-unchecked"), " unchecked", 
                    cls="stats", id="stats", hx_get="/stats",
                    hx_trigger="every 1s",hx_swap="outerHTML", hx_swap_oob="true"
                    )
    
    #clients polling for outstanding diffs
    @app.get("/diffs/{client_id}")
    async def diffs(request, client_id:str):
        async with clients_mutex:
            client = clients.get(client_id, None)
            if client is None or len(client.diffs) == 0:
                return ""
            
            client.heartbeat()
            diffs_list = client.pull_diffs()
        
        #await init_checkboxes()
        diff_array = []
        for i in diffs_list:
            #get fresh value from bitmap
            bit = await redis.getbit(checkboxes_bitmap_key, i)
            is_checked = bool(bit)

            diff_array.append(
                fh.Input(   type="checkbox",
                            id=f"cb-{i}",
                            #checked= checkbox_cache[i], deprecated for list
                            checked = is_checked, #uses bitmap
                            hx_post=f"/toggle/{i}/{client_id}", hx_swap="none",
                            hx_swap_oob="true",# allows us to later push diffs to arbitrary checkboxes by id
                            cls= "cb"
                            )
            #for i in diffs_list
        )
        return diff_array
    
    @app.get("/visitors")
    async def visitors_page(request, offset: int = 0, limit: int = 5):#100):
        print(f"[VISITORS] Loading visitors page (offset={offset}, limit={limit})..")
        #get visitors with pagination
        recent_ips = await redis.zrange("recent_visitors_sorted", offset, offset + limit - 1, desc=True)
        print(f"[VISITORS] Found {len(recent_ips)} IPs in sorted set")

        visitors = []
        for ip in recent_ips:
            ip_str = ip.decode('utf-8') if isinstance(ip, bytes) else str(ip)
            visitors_raw = await redis.get(f"visitor:{ip_str}")
            if visitors_raw:
                v = json.loads(visitors_raw)
                v["timestamp"] = float(v.get("timestamp", time.time()))
                visitors.append(v)
        print(f"[VISITORS] Loaded {len(visitors)} visitor records")

        #Get total count from sorted set
        total_in_db = await redis.zcard("recent_visitors_sorted")
        total_visitors = await redis.get("total_visitors_count")
        total_count = int(total_visitors) if total_visitors else 0
        print(f"[VISITORS] Total unique visitors: {total_count}, in DB: {total_in_db}")

        #calculate if there are more visitors to load
        has_more = (offset + limit) < total_in_db
        next_offset = offset + limit if has_more else None
        prev_offset = max(0, offset - limit) if offset > 0 else None

        #Day status
        day_stats = {}
        for v in visitors:
            local_dt = utc_to_local(v["timestamp"])
            day = local_dt.strftime("%Y-%m-%d"), time.localtime(v["timestamp"])
            day_stats[day] = day_stats.get(day, 0) + 1

        sorted_days = sorted(day_stats.items(), key=lambda x:x[0], reverse=True)

        #group visitors by day for the table
        visitors_by_day = {}
        for v in visitors:
            local_dt = utc_to_local(v["timestamp"])
            day = local_dt.strftime("%Y-%m-%d")#, time.localtime(v["timestamp"]))
            if day not in visitors_by_day:
                visitors_by_day[day] = []
            visitors_by_day[day].append(v)
    
        sorted_day_keys = sorted(visitors_by_day.keys(), reverse=True)

        #Create table rows grouped by day
        table_content = []
        for day_key in sorted_day_keys:
            day_visitors = visitors_by_day[day_key]
            day_display = datetime.strptime(day_key, "%Y-%m-%d").strftime("%A, %B %d, %Y")
            visitor_count = len(day_visitors)

            table_content.append(
                fh.Tr( fh.Td( fh.Div(
                            fh.Strong(day_display),
                            fh.Span(f" ({visitor_count} visitor{'s' if visitor_count != 1 else ''})",
                                    style="color: #667eea; margin-left: 10px;"),
                            style="padding: 10px 0;" ), colspan=9, cls="day-separator" )))

            #add visitors rows for this day
            for v in day_visitors:
                is_vpn = v.get("is_vpn", False)
                is_relay = "Relay" in  v.get("classification", "")

                if is_relay:
                    security_badge = fh.Span("iCloud Relay", cls="badge badge-relay", style="background:#5856d6; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em;")
                elif is_vpn:
                    security_badge = fh.Span("VPN/PROXY", cls="badge badge-vpn", style="background:#ff3b30; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em;")
                else:
                    security_badge = fh.Span("Clean", cls="badge badge-relay", style="background:#4cd964; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em;")
                #security_badge = fh.Span("VPN/Proxy", cls="badge badge-clear") if is_vpn else fh.Span("clean", cls="badge badge-clear")
                
                #classification and usage label
                usage = v.get("usage_type", "Residential")
                classification = v.get("classification", "Human")
                class_color = "#ff9500" if "Bot" in classification else "#007aff"
                category_cell = fh.Div(
                    fh.Div(classification, style=f"font-weight:bold; color:{class_color};"),
                    fh.Div(usage, style = "font-size:0.8em opacity:0.7;"),
                )

                local_dt = utc_to_local(v["timestamp"])
                local_time_str = local_dt.strftime("%H:%H:%S")
        
                table_content.append(
                    fh.Tr(  fh.Td(v.get("ip")), fh.Td(v.get("device", "Unknown ?")), fh.Td(security_badge), fh.Td(category_cell), fh.Td(v.get("isp") or "-", style="max-width:150px;overflow:hidden;text-overflow:ellipsis; white-space:nowrap; font-size:0.85em;"),
                            fh.Td(v.get("city") or "-"), fh.Td(v.get("zip", "-")), fh.Td(v.get("country") or "-"), 
                            fh.Td(fh.Span(f"{v.get('visit_count', 1)}", cls="visit-badge")), fh.Td(local_time_str), cls="visitor-row" ))
        #Day chart bars ( last 7 days)
        max_count_days = max([count for _,count in sorted_days], default=1) if sorted_days else 1
        now_ts = time.time()
        now_local = utc_to_local(now_ts)
        max_count_days = 1
        last_7_days = []
        for i in range(6,-1,-1):
            #calculate day in local timezone
            day_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            day_local = day_local - pytz.timezone('America/Chicago').localize(datetime.fromtimestamp(i*86400)
            .replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0))
            
            import datetime as dt
            target_date = now_local.date() - dt.timedelta(days=i)
            day_key = target_date.strftime("%Y-%m-%d")

            count = sum(1 for v in visitors
                         if utc_to_local(v["timestamp"]).strftime("%Y-%m-%d") == day_key)#, time.localtime(v["timestamp"])) == day_key)
            last_7_days.append((day_key, count))
            max_count_days = max(max_count_days, count)

        chart_bars_days = []
        for date_str, count in last_7_days:
            display_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a,%b %d")
            percentage = (count/ max_count_days) * 100
            chart_bars_days.append(
                fh.Div(
                    fh.Div(
                        fh.Span(f"{count}", cls="bar-value") if count > 0 else "",
                        style=f"height: {max(percentage,2)}%",  cls="bar-fill-vertical" ),
                    fh.Span(display_date, cls="bar-label-vertical"), cls="bar-vertical" 
                ) )
        
        pagination_controls = fh.Div(
            fh.Div(
                fh.A("<- Previous", href=f"/visitors?offset={prev_offset}&limit={limit}", cls="pagination-btn"
                ) if prev_offset is not None else fh.Span("<- Previous", cls="pagination-btn disabled"),
                fh.Span(
                    f"Showing {offset + 1}-{min(offset + limit, total_in_db)} of {total_in_db} visitors", cls="pagination-info"),
                fh.A("Next ->", href=f"/visitors?offset={next_offset}&limit={limit}", cls="pagination-btn"
                ) if has_more else fh.Span("Next ->", cls="pagination-btn disabled"), cls="pagination-controls" ),
                
            fh.Div(
                fh.Span("show: ", style="margin-right: 10px;"),
                fh.A("50", href=f"/visitors?offset=0&limit=50", cls="limit-btn" + (" active" if limit == 50 else "")),
                fh.A("100", href=f"/visitors?offset=0&limit=100", cls="limit-btn" + (" active" if limit == 100 else "")),
                fh.A("200", href=f"/visitors?offset=0&limit=200", cls="limit-btn" + (" active" if limit == 200 else "")), 
                fh.A("500", href=f"/visitors?offset=0&limit=500", cls="limit-btn" + (" active" if limit == 500 else "")),
                cls="limit-controls" ), cls="pagination-wrapper")
        return (
            fh.Titled("Visitors Page Records",
            #add mobile-friendly meta tags
            fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0, maximum-scale=5.0")),
            fh.Main( fh.H1("Recent Visitors Dashboard", cls="dashboard-title"),
                #Total visitors card
                fh.Div(
                    fh.Div("Total Unique Visitors", cls="stats-label"), fh.Div(f"{total_count:,}", cls="stats-number"),
                    fh.Div(f"Database contains {total_in_db:,} Visitor Records",
                            style="font-size: 0.9em; opacity: 0.8;"), cls="stats-card"),
                pagination_controls,
                #vertical table with day grouping
                fh.Div(
                    fh.H2("Visitors by Day (Last 7 days - Central Time)", cls="section-title"),
                    fh.Div(
                        *chart_bars_days if chart_bars_days else [
                            fh.P("No visitors data yet", style="text-align: center; color:#999;")],
                        cls="chart-bars-container" ), cls="chart-container" ),
                #visitors table with day grouping
                fh.Div(
                    fh.H2(f"Visitors Dashboard (Last {limit} Visitors)", cls="section-title"),
                    fh.Div(fh.P("<- Scroll horizontal to see all columns ->",
                        style="text-align: center; color:#999; font-size: 0.85em; margin-bottom: 10px; display: none;",cls="mobile-control-hint"),
                    fh.Table(
                        fh.Tr( fh.Th("IP"), fh.Th("device"), fh.Th("Security"), fh.Th("Category"), fh.Th("ISP/Org"), fh.Th("City"), fh.Th("Zip"), fh.Th("Country"), fh.Th("Visits"), fh.Th("Last seen"), ),
                        *table_content, cls="table visitors-table"
                    )if table_content else fh.P("No visitors to display", style="text-align: center; color:#999; padding: 20px;"),
                    style="overflow-x: auto; -webkit-overflow-scrolling: touch;")),
                pagination_controls,
                fh.Div(
                    fh.A("<- Back to checkboxes", href="/", cls="back-link"),
                    style="text-align: center; margin-top: 30px;"
                ), cls="visitors-container" ))
    return app

class Client:
    def __init__(self):
        self.id = str(uuid4())
        self.diffs = []
        self.inactive_deadline = time.time() + 30
        self.geo = None
        self.geo_ts = 0.0
    
    def is_active(self):
        return time.time() < self.inactive_deadline
    
    def heartbeat(self):
        self.inactive_deadline = time.time() + 30

    def add_diff(self, i):
        if i not in self.diffs:
            self.diffs.append(i)

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
