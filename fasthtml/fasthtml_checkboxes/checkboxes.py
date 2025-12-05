
import time
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
import modal
from modal import Image
import fasthtml.common as fh
import inflect
import httpx
import asyncio
import json
import subprocess
from redis.asyncio import Redis


N_CHECKBOXES=1000000

app = modal.App("fasthtml-checkboxes")

volume = modal.Volume.from_name("redis-data-vol", create_if_missing=True)

checkboxes_key = "checkboxes"
clients = {}
clients_mutex = Lock()

checkbox_cache = None
checkbox_cache_loaded_at = 0.0
CHECKBOX_CACHE_TTL = 5 #60 * 10 #keep for 10 minutes in memory

def make_hx_post(i, client_id):
    return f"/checkbox/toggle/{i}/{client_id}"

GEO_TTL_REDIS = 86400 
CLIENT_GEO_TTL = 30.0  #client level in memory small cache (30s)

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

async def get_geo(ip: str, redis_client: Redis):
    """Return geo info from ip using cache + fallback providers"""

    cached = await redis_client.get(f"geo:{ip}")
    if cached:
        return json.loads(cached)
    #fetch from providers and cache
    data = await get_geo_from_providers(ip)
    try:
        await redis_client.set(f"geo:{ip}", json.dumps(data), ex=GEO_TTL_REDIS)
    except Exception:
        pass
    return data

async def record_visitors(ip, user_agent, geo, redis):
    #use hash for faster loopups
    visitors_key = f"visitor:{ip}"

    entry = {
        "ip": ip,
        "user_agent": user_agent[:120],
        "city": geo.get("city"),
        "zip": geo.get("postal") or geo.get("zip"),
        "country": geo.get("country") or geo.get("country_name"),
        "timestamp": time.time(),
    }
    try: 
        await redis.setex(visitors_key, 86400, json.dumps(entry)) #store/update this visitor
        await redis.zadd("recent_visitors_sorted", {ip:time.time()}) #maintain a sorted set by timestamp
        await redis.zremrangebyrank("recent_visitors_sorted", 0,-101) 
    except Exception:
        pass

css_path_local = Path(__file__).parent / "style.css"
css_path_remote = "/assets/style.css"


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
    .pip_install("python-fasthtml==0.12.35", "inflect~=7.4.0", "httpx==0.27.0" ,"redis>=5.3.0")
    .apt_install("redis-server")
    .add_local_file(css_path_local,remote_path=css_path_remote)
    )

@app.function( 
        image = app_image, 
        max_containers=1,
        volumes={"/data": volume},)

@modal.concurrent(max_inputs=1000)
@modal.asgi_app()
def web():
    # ---------------------------
    # Start redis server locally inside the container (persisted to volume)
    # ---------------------------
    redis_process = subprocess.Popen(
        ["redis-server", 
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

    async def init_checkboxes():
        global checkbox_cache, checkbox_cache_loaded_at

        if checkbox_cache is None or time.time() - checkbox_cache_loaded_at > CHECKBOX_CACHE_TTL:#checkbox_cache_expiry:
            exists = await redis.exists(checkboxes_key)
            if not exists:
                await redis.rpush(checkboxes_key, *[json.dumps(False)] * N_CHECKBOXES)

            checkbox_raw = await redis.lrange(checkboxes_key, 0, -1)
            checkbox_cache = [json.loads(v) for v in checkbox_raw]
            checkbox_cache_loaded_at = time.time()

        return checkbox_cache

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
    
    @app.get("/grid/{client_id}")
    async def grid(client_id:str):
        #this runs once per client, returns ~450 KB instead of 2.2MB
        await init_checkboxes()
        boxes = [
            fh.Input( type="checkbox", id=f"cb-{i}",checked= val,
                #name ="cbs",
                hx_post= f"/checkbox/toggle/{i}/{client_id}",#make_hx_post(i, client.id), # when clicked, that checkbox will send a POST request to the server with its index
                hx_swap="none", cls="cb")
            for i, val in enumerate(checkbox_cache)
        ]
        return fh.Div(*boxes, cls="grid")
    
    @app.get("/")
    async def get(request):
    
        #log IP address
        client_ip = get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')

        #register a new client
        client = Client()
        async with  clients_mutex:
            clients[client.id] = client

        #Load checkboxes immediately
        await init_checkboxes()

        #fire and forget - truly non blocking, as before asgi waits the background task to finish
        #fire_and_forget(background_geo_logging(client_ip,user_agent, redis))

        #print(f"[HOME] Client {client.id[:8]} | IP : {client_ip} | Page served (geo logging in background)")

        #return page fast (no blocking by geo logging API) 
        return(
            fh.Titled(f"{N_CHECKBOXES}k Checkboxes"),
            fh.Main(
                fh.H1(
                    f"{inflect.engine().number_to_words(N_CHECKBOXES).title()} Checkboxes"),
                    #fh.Div( *checkbox_array, id="checkbox-array",cls="grid"),
                    #fh.Div( "Loading checkboxes...", id="loading",cls="loading htmx-request"),
                    fh.Div(
                        hx_get=f"/grid/{client.id}",
                        hx_trigger="load", #"every 800s", #poll every second
                        hx_swap="innerHTML", #dont replace the entire page
                        hx_indicator = "loading"
                    ),
                    cls="container",
                    # use HTMX to poll for diffs to apply 
                ),
        )

    #users submitting checkbox toggles
    @app.post("/checkbox/toggle/{i}/{client_id}")
    async def toggle(request, i:int, client_id:str):
        client_ip = get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')

        async with clients_mutex:
            client = clients.get(client_id)

        now = time.time()
        if client is None or not client.has_recent_geo(now):
            geo = await get_geo(client_ip, redis)
            if client:
                client.set_geo(geo, now)
        else:
            geo = client.geo

        await record_visitors(client_ip, user_agent, geo, redis)

        city = geo.get("city")
        zip_code = geo.get('postal')
        country = geo.get("country_name") or geo.get("country")
        isp = geo.get("org") or geo.get("isp")
        print(
            f"[TOGGLE] Checkbox {i} toggled by {client_id[:8]} | Checkbox {i} |"
            f"IP: {client_ip} | {city}, {zip_code}, {country} | ISP: {isp} | - User-Agent: {user_agent[:50]}...")

        async with clients_mutex:
            await init_checkboxes()
            try:
                current = checkbox_cache[i]
            except Exception:
                raw = await redis.lindex(checkboxes_key, i)
                current = json.loads(raw) if raw is not None else False

            new_value = not current
            checkbox_cache[i] = new_value

            try:
                await redis.lset(checkboxes_key, i, json.dumps(new_value))
            except Exception:
                print("warning: redis lset failed")

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
        return
    
    #clients polling for outstanding diffs
    @app.get("/diffs/{client_id}")
    async def diffs(request, client_id:str):
        async with clients_mutex:
            client = clients.get(client_id, None)
            if client is None or len(client.diffs) == 0:
                return
            
            client.heartbeat()
            diffs_list = client.pull_diffs()
        
        client_ip = get_real_ip(request)
        async with clients_mutex:
            client = clients.get(client_id)
        now = time.time()
        if client is None or not client.has_recent_geo(now):
            geo = await get_geo(client_ip, redis)
            if client:
                client.set_geo(geo, now)

        city = geo.get("city")
        zip_code = geo.get('postal')
        country = geo.get("country_name") or geo.get("country")
        isp = geo.get("org") or geo.get("isp")
        print(
            f"[DIFFS] Sending {len(diffs)} diffs to {client_id[:8]}| IP: {client_ip} |"
            f"{city}, {zip_code}, {country}, | ISP: {isp} diff sent"
            )

        await init_checkboxes()
        diff_values = {}
        for idx in diffs_list:
            try:
                diff_values[idx] = checkbox_cache[idx]
            except Exception:
                raw = await redis.lindex(checkboxes_key, idx)
                diff_values[idx] = json.loads(raw) if raw is not None else False

        # async with checkboxes_mutex:
        diff_array = [
            fh.CheckboxX(
                id=f"cb-{i}",
                checked= diff_values[i],
                # when clicked, that checkbox will send a POST request to the server with its index
                hx_post=f"/checkbox/toggle/{i}/{client_id}",
                hx_swap_oob="true",# allows us to later push diffs to arbitrary checkboxes by id
            )
            for i in diffs_list
        ]
        return diff_array
    
    @app.get("/visitors")
    async def visitors_page(request):
        recent_ips = await redis.zrange("recent_visitors_sorted", 0, 99, desc=True)
        visitors = []
        
        for ip in recent_ips:
            ip_str = ip.decode('utf-8') if isinstance(ip, bytes) else str(ip)
            visitors_raw = await redis.get(f"visitor:{ip_str}")
            if visitors_raw:
                v = json.loads(visitors_raw)
                v["timestamp"] = float(v["timestamp"])
                visitors.append(v)

        rows = [
            fh.Tr(
                fh.Td(v["ip"]),
                fh.Td(v["city"] or "-"),
                fh.Td(v.get("zip", "-")),
                fh.Td(v["country"] or "-"),
                fh.Td(time.strftime("%H:%M:%S", time.localtime(v["timestamp"]))),
            )
            for v in visitors
        ]

        return fh.Main(
            fh.H1("Recent Visitors"),
            fh.Table(
                fh.Tr(
                    fh.Th("IP"),
                    fh.Th("City"),
                    fh.Th("Zip"),
                    fh.Th("Country"),
                    fh.Th("Time"),
                ),
                *rows,
                cls="table"
            )
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
