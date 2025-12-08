
import time
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
import modal
from modal import Image
import fasthtml.common as fh
import httpx
import asyncio
import json
import subprocess
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


#New geolocation helper function
async def get_geo_from_providers(ip:str, redis):
    #primary provider
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"https://ipapi.co/{ip}/json/")
        if r.status_code == 200:
            data = r.json()
            if "country_name" in data:
                await redis.set(f"geo:{ip}", json.dumps(data), ex=86400)
                return data
    except Exception:
        pass

    #Fallback provider
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://ip-api.com/json/{ip}")
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                await redis.set(f"geo:{ip}", json.dumps(data), ex=86400)
                return data
    except Exception:
        pass
    #last resort 
    return {"ip": ip, "city": None, "country": None, "zip": None}

async def get_geo(ip: str, redis):
    """Return geo info from ip using cache + fallback providers"""

    cached = await redis.get(f"geo:{ip}")
    if cached:
        return json.loads(cached)
    #fetch from providers and cache
    data = await get_geo_from_providers(ip,redis)
    try:
        await redis.set(f"geo:{ip}", json.dumps(data), ex=GEO_TTL_REDIS)
    except Exception:
        pass
    return data


async def record_visitors(ip, user_agent, geo, redis):
    """ Record visitor with visit count tracking"""
    try:
        visitors_key = f"visitor:{ip}"

        existing = await redis.get(visitors_key)
        is_new_visitor = existing is None

        if existing:
            existing_data = json.loads(existing)
            visit_count = existing_data.get("visit_count", 1) + 1
        else:
            visit_count = 1

        entry = {
            "ip": ip,
            "user_agent": user_agent[:120],
            "city": geo.get("city"),
            "zip": geo.get("postal") or geo.get("zip"),
            "country": geo.get("country") or geo.get("country_name"),
            "timestamp": time.time(),
            "visit_count" : visit_count,
        }

        await redis.setex(visitors_key, 86400, json.dumps(entry)) #store/update this visitor, expired in 24 hrs
        await redis.zadd("recent_visitors_sorted", {ip:time.time()}) #maintain a sorted set by timestamp
        await redis.zremrangebyrank("recent_visitors_sorted", 0,-101) #keep only last 100

        if is_new_visitor:
            await redis.incr("total_visitors_count")
            print(f"[VISITOR]New visitor from {geo.get('city', 'Unknown',)}, {geo.get('country', 'Unknown')}")
        else:
            print(f"[VISITOR]Returning visitor {ip}  (visit #{visit_count}")

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
    .pip_install("python-fasthtml==0.12.36", "httpx==0.27.0" ,"redis>=5.3.0")
    .apt_install("redis-server")
    .add_local_file(css_path_local,remote_path=css_path_remote)
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
            "--save", "86400", "1", #save once per day
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
        print("[STARTUP] Migration check complete")

    async def migrate_litst_to_bitmap():
        """one-time migration from redis to bitmap"""

        old_key = "checkboxes"
        new_key= "checkboxes_bitmap"

        #temporarily: Force re-migration
        # await redis.delete(new_key)
        # print("[MIGRATE] Deleted existing bitmap to force fresh migration")

        #check if migration is needed
        list_exists= await redis.exists(old_key)
        bitmap_exists = await redis.exists(new_key)

        if not list_exists:
            print("[MIGRATE] No old data")
            return
        if bitmap_exists:
            print(f"[MIGRATE] Bitmap already exists,skipping migration")
            return
        
        print(f"[MIGRATE] Starting migration from list to bitmap...")
        start_time = time.time()

        #load all values from list
        list_len = await redis.llen(old_key)
        print(f"[MIGRATE] found {list_len:,} checkboxes in list")

        #process in batches
        batch_size = 10000
        pipe = redis.pipeline()
        batch_count = 0

        for batch_start in range(0, list_len, batch_size):
            batch_end = min(batch_start + batch_size, list_len)

            values = await redis.lrange(old_key, batch_start, batch_end - 1)

            #set bits in bitmap
            for i, value in enumerate(values):
                idx = batch_start + i
                is_checked = json.loads(value) if value else False
                if is_checked:
                    pipe.setbit(new_key, idx, 1)
                    batch_count += 1

            #execute batch
            await pipe.execute()
            pipe = redis.pipeline() #reset pipeline

            if batch_start % 100000 ==0:
                print(f"[MIGRATE] Progress: {batch_start:,}/{list_len:,}")

        elapsed_time = time.time() - start_time
        print(f"[MIGRATE] Migration complete in {elapsed_time:.2f}s")

        #verify
        bitmap_count = await redis.bitcount(new_key)
        print(f"[MIGRATE] Bitmap has {bitmap_count:,} checked checkboxes")
        print(f"[MIGRATE] Bitmap has {batch_count:,} checked checkboxes")

        if bitmap_count != batch_count:
            print(f"[MIGRATE] ⚠️ Warning: Mismatch! Expected {batch_count}, got {bitmap_count}")

        #delete old list to save space
        # await redis.delete(old_key)
        # print(f"[MIGRATE] Deleted old list")

    async def diagnose_redis_state():
        """Check what's in Redis"""
        print("\n" + "="*50)
        print("REDIS DIAGNOSTIC")
        print("="*50)
        
        # Check old list
        list_exists = await redis.exists("checkboxes")
        if list_exists:
            list_len = await redis.llen("checkboxes")
            print(f"✓ Old list 'checkboxes' exists: {list_len:,} items")
            
            # Sample first 10 values
            sample = await redis.lrange("checkboxes", 0, 9)
            checked_in_sample = sum(1 for v in sample if json.loads(v))
            print(f"  Sample (first 10): {checked_in_sample} checked")
            
            # Count all checked (slow but accurate)
            print("  Counting all checked (this may take a moment)...")
            all_values = await redis.lrange("checkboxes", 0, -1)
            total_checked = sum(1 for v in all_values if json.loads(v))
            print(f"  Total checked in list: {total_checked:,}")
        else:
            print("✗ Old list 'checkboxes' does not exist")
        
        # Check bitmap
        bitmap_exists = await redis.exists("checkboxes_bitmap")
        if bitmap_exists:
            bitmap_count = await redis.bitcount("checkboxes_bitmap")
            print(f"✓ Bitmap 'checkboxes_bitmap' exists")
            print(f"  Checked in bitmap: {bitmap_count:,}")
        else:
            print("✗ Bitmap 'checkboxes_bitmap' does not exist")
        
        print("="*50 + "\n")
    
    async def init_checkboxes():
        """Initilize Redis list if needed, but DON'T load all into memory """
        current_len = await redis.llen(checkboxes_key)
        if current_len < N_CHECKBOXES:
            #print(f"[CACHE] Initializing {N_CHECKBOXES:,} checkboxes...")
            print(f"[INIT] Redis list too short ({current_len:,}), padding to {N_CHECKBOXES:,}...")
            missing = N_CHECKBOXES - current_len
            pipe = redis.pipeline()
            batch_size = 10000
            for i in range(0, missing, batch_size):
                batch = [json.dumps(False)] * min(batch_size, missing -i)
                pipe.rpush(checkboxes_key, *batch)
            await pipe.execute()
            print(f"[INIT] added {missing:,} missing checkboxes")

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
    app, _ = fh.fast_app(
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
        await init_checkboxes()

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
        await init_checkboxes()
        checked, unchecked = await get_status()

        geo = await get_geo(client_ip, redis)
        await record_visitors(client_ip,user_agent, geo, redis)

        first_chunk_html= await _render_chunk(client.id, offset=0)

        return( 
            fh.Titled(f"One Million Checkboxes"),
            fh.Main(
                fh.H1(f" One Million Checkboxes"),
                fh.Div( 
                    fh.Span(f"{checked:,}", cls="status-checked"), " checked • ",
                    fh.Span(f"{unchecked:,}",cls="status-unchecked"), " unchecked", 
                    cls="stats", id="stats", hx_get="/stats",
                    hx_trigger="every 1s",hx_swap="outerHTML"
                    ),
                fh.Div(
                    fh.NotStr(first_chunk_html), #preload first chunk
                    cls="grid-container",
                    id="grid-container",
                ),
                fh.Div("Made with FastHTML + Redis deployed with Modal", cls="footer"), 
                cls="container", 
                ),
        )

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
                await redis.lset(checkboxes_key, i, json.dumps(new_value))
                #update bitmap
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
    
    async def cleanup_cache_task():
        """periodically clean up old cache entries"""
        while True:
            await asyncio.sleep(300)

            #keep only the most recently accessed entries
            if len(checkbox_cache) > 50000: # keep max 50k in memory
                print(f"[CACHE] Clean up,current size: {len(checkbox_cache):,}")
                #Remove random half of entries
                keys_to_remove = list(checkbox_cache.keys())[::2]
                for key in keys_to_remove:
                    del checkbox_cache[key]
                print(f"[CACHE] Cleanup to: {len(checkbox_cache):,}")

    #clients polling for outstanding diffs
    @app.get("/diffs/{client_id}")
    async def diffs(request, client_id:str):
        async with clients_mutex:
            client = clients.get(client_id, None)
            if client is None or len(client.diffs) == 0:
                return ""
            
            client.heartbeat()
            diffs_list = client.pull_diffs()
        
        await init_checkboxes()

        diff_array = [
            fh.Input(   type="checkbox",
                        id=f"cb-{i}",
                        checked= checkbox_cache[i],
                        # when clicked, that checkbox will send a POST request to the server with its index
                        hx_post=f"/toggle/{i}/{client_id}", hx_swap="none",
                        hx_swap_oob="true",# allows us to later push diffs to arbitrary checkboxes by id
                        cls= "cb"
                        )
            for i in diffs_list
        ]
        return diff_array
    
    @app.get("/visitors")
    async def visitors_page(request):
        print("[VISITORS] Loading visitors page ...")

        recent_ips = await redis.zrange("recent_visitors_sorted", 0, 99, desc=True)
        print("[VISITORS] Found {len(recent_ips)} IPs in sorted set")

        visitors = []
        for ip in recent_ips:
            ip_str = ip.decode('utf-8') if isinstance(ip, bytes) else str(ip)
            visitors_raw = await redis.get(f"visitor:{ip_str}")
            if visitors_raw:
                v = json.loads(visitors_raw)
                v["timestamp"] = float(v["timestamp"])
                visitors.append(v)

        print(f"[VISITORS] Loaded {len(visitors)} visitor records")

        total_visitors = await redis.get("total_visitors_count")
        total_count = int(total_visitors) if total_visitors else 0
        print(f"[VISITORS] Total unique visitors: {total_count}")

        hour_stats = {}
        for v in visitors:
            hour = time.strftime("%Y-%m-%d %H:00", time.localtime(v["timestamp"]))
            hour_stats[hour] = hour_stats.get(hour, 0) + 1

        sorted_hours = sorted(hour_stats.items(), key=lambda x:x[0], reverse=True)

        day_stats = {}
        for v in visitors:
            day = time.strftime("%Y-%m-%d", time.localtime(v["timestamp"]))
            day_stats[day] = day_stats.get(day, 0) + 1

        sorted_days = sorted(day_stats.items(), key=lambda x:x[0], reverse=True)

        rows = [
            fh.Tr(
                fh.Td(v["ip"]),
                fh.Td(v["city"] or "-"),
                fh.Td(v.get("zip", "-")),
                fh.Td(v["country"] or "-"),
                fh.Td(time.strftime("%b %d, %H:%M:%S", time.localtime(v["timestamp"]))),
                fh.Td(fh.Span(f"{v.get('visit_count', 1)}", cls="visit-badge")),
            )
            for v in visitors
        ]

        max_count = max([count for _,count in sorted_hours], default=1) if sorted_hours else 1
        chart_bars = []

        for datetime_str, count in sorted_hours[:24]:
            display_hour = time.strftime("%b %d, %H:00", time.strptime(datetime_str, "%Y-%m-%d %H:00"))
            percentage = (count/ max_count) * 100

            chart_bars.append(
                fh.Div(
                    fh.Div(
                        fh.Span(display_hour, cls="countrt-label"),
                        fh.Span(f"{count} visitor{'s' if count != 1 else ''}", cls="country-count"),
                        cls="bar-labels"
                    ),
                    fh.Div(
                        fh.Div(
                            style=f"width: {percentage}%",
                            cls="bar-fill"
                        ),
                        cls="bar-container"
                ),
                cls="chart-row"
            )
        )

        max_count = max([count for _,count in sorted_days], default=1) if sorted_days else 1
        chart_bars_days = []
        for date_str, count in sorted_days[:7]:
            display_date = time.strftime("%a,%b %d", time.strptime(date_str, "%Y-%m-%d")),
            percentage = (count/ max_count) * 100

            chart_bars_days.append(
                fh.Div(
                    fh.Div(
                        style=f"height: {percentage}%",
                        cls="bar-fill-vertical"
                    ),
                    fh.Span(display_date, cls="bar-label-vertical"),
                    cls="bar-vertical"
                )    
        )

        return fh.Main(
            fh.H1("Recent Visitors Dashboard", cls="dashboard-title"),

            #Total visitors card
            fh.Div(
                fh.Div("Total Unique Visitors", cls="stats-label"),
                fh.Div(f"{total_count:,}", cls="stats-number"),
                fh.Div("Last 100 Visitors Shown below", style="font-size: 0.9em; opacity: 0.8;"),
                cls="stats-card"
            ),

            #vertical bar chart
            fh.Div(
                fh.H2("Visitors by Day (Last 7 days)", cls="section-title"),
                fh.Div(
                    *chart_bars_days if chart_bars_days else [fh.P("No visitors data yet", style="text-align: center; color:#999;")],
                    cls="chart-bars-container"
                ), 
                cls="chart-container" 
            ),

            fh.Div(
                fh.H2("Visitors by Hour (Last 24 Hours)", cls="section-title"),
                *chart_bars if chart_bars else [fh.P("No visitors data yet", style="text-align: center; color:#999;")],
                cls="chart-container"),

            #visitors table
            fh.Div(
                fh.H2("Recent visitors", cls="sectioin-title"),
                fh.Table(
                    fh.Tr(
                        fh.Th("IP"),
                        fh.Th("City"),
                        fh.Th("Zip"),
                        fh.Th("Country"),
                        fh.Th("Visits"),
                        fh.Th("Last seen"),
                    ),
                    *rows,
                    cls="table"
                )
            ),
            fh.Div(
                fh.A("<- Back to checkboxes", href="/", cls="back-link"),
                style="text-align: center;"
            ),
            cls="visitors-container"
        )
        
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
