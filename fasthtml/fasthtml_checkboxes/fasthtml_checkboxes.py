import time
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
import modal
from modal import Image, Sandbox
import fasthtml.common as fh
import inflect
import httpx
import asyncio
import json
import subprocess
from redis.asyncio import Redis


N_CHECKBOXES=10000

redis_image = (Image.debian_slim(python_version="3.12").apt_install("redis-server"))

app = modal.App("fasthtml-checkboxes")

checkboxes_key = "checkboxes"
clients = {}
clients_mutex = Lock()

#New geolocation helper function
async def get_geo(ip:str,redis):
    """Return geo info from ip using cache + fallback providers"""
    #check cache first
    cached = await redis.get(f"geo:{ip}")
    if cached:
        return json.loads(cached)
    
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
    data = {"ip": ip, "city": None, "country": None, "zip": None}
    #if all fail
    return data

async def record_visitors(ip,user_agent, geo, redis):
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
    
    await redis.setex(visitors_key, 86400, json.dumps(entry)) #store/update this visitor
    await redis.zadd("recent_visitors_sorted", {ip:time.time()}) #maintain a sorted set by timestamp
    await redis.zremrangebyrank("recent_visitors_sorted", 0,-101) 


css_path_local = Path(__file__).parent / "style.css"
css_path_remote = "/assets/styles.css"


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

@app.function( image = app_image, max_containers=1,)
@modal.concurrent(max_inputs=1000)
@modal.asgi_app()
def web():
    
    redis_process = subprocess.Popen(
        ["redis-server", "--protect-mode", "no","--bind","127.0.0.1", "--port", "6379"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    time.sleep(2)

    redis = Redis.from_url("redis://127.0.0.1:6379")
    print("Redis sidecar started succesfully")


    async def init_checkboxes():
        exists = await redis.exists(checkboxes_key)
        if not exists:
            await redis.rpush(checkboxes_key, *[json.dumps(False)]*N_CHECKBOXES)
            print("initialized checkboxes in Redis")

    async def on_shutdown():
        print("Redis-backed checkbox state persisted automatically.")
        await redis.close() #not necessarily needed here just best practice
        redis_process.terminate()
        redis_process.wait()

    style= open(css_path_remote, "r").read()
    app, _ = fh.fast_app(
        on_shutdown=[on_shutdown],
        hdrs=[fh.Style(style)],
    )

    metrics_for_count = {
        "request_count" : 0,
        "last_throughput_log" : time.time()
    }

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
    
    @app.get("/")
    async def get(request):
        await init_checkboxes()

        #log IP address
        client_ip = get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')

        #geo location look up
        geo = await get_geo(client_ip, redis)
        await record_visitors(client_ip, user_agent, geo, redis)
        
        city = geo.get("city")
        zip_code = geo.get('postal')
        country = geo.get("country_name") or geo.get("country")
        isp = geo.get("org") or geo.get("isp")
        print(f"[HOME] New Client connected - IP: {client_ip} | {city}, {zip_code}, {country} | ISP: {isp} |- User-Agent: {user_agent[:80]}...")

        #register a new client
        client = Client()
        async with  clients_mutex:
            clients[client.id] =client

        checkbox_raw = await redis.lrange(checkboxes_key, 0, -1)
        checkboxes_values = [json.loads(v) for v in checkbox_raw]

        checkbox_array = [ 
            fh.CheckboxX(
                id=f"cb-{i}",
                checked= val,
                hx_post=f"/checkbox/toggle/{i}/{client.id}",  # when clicked, that checkbox will send a POST request to the server with its index
            )
                for i,val in enumerate(checkboxes_values)
            ]
        
        return(
            fh.Titled(f"{N_CHECKBOXES // 1000}k Checkboxes"),
            fh.Main(
                fh.H1(
                    f"{inflect.engine().number_to_words(N_CHECKBOXES).title()} Checkboxes"),
                    fh.Div( *checkbox_array, id="checkbox-array",),
                    cls="container",
                    # use HTMX to poll for diffs to apply
                    hx_trigger="every 1s", #poll every second
                    hx_get=f"/diffs/{client.id}", #call the diffs  endpoint
                    hx_swap="none", #dont replace the entire page
                ),
                #fh.A("view visitors , href="/visitors")
        )

    #users submitting checkbox toggles
    @app.post("/checkbox/toggle/{i}/{client_id}")
    async def toggle(request, i:int, client_id:str):
        client_ip = get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')

        geo = await get_geo(client_ip, redis)
        await record_visitors(client_ip, user_agent, geo, redis)

        city = geo.get("city")
        zip_code = geo.get('postal')
        country = geo.get("country_name") or geo.get("country")
        isp = geo.get("org") or geo.get("isp")
        print(
            f"[TOGGLE] Checkbox {i} toggled by {client_id[:8]} | Checkbox {i} |"
            f"IP: {client_ip} | {city}, {zip_code}, {country} | ISP: {isp} | - User-Agent: {user_agent[:50]}...")

        async with clients_mutex:
            current = await redis.lindex(checkboxes_key, i)
            new_value = not json.loads(current)
            await redis.lset(checkboxes_key, i, json.dumps(new_value))
        
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
        geo = await get_geo(client_ip, redis)
        city = geo.get("city")
        zip_code = geo.get('postal')
        country = geo.get("country_name") or geo.get("country")
        isp = geo.get("org") or geo.get("isp")
        print(
            f"[DIFFS] Sending {len(diffs)} diffs to {client_id[:8]}| IP: {client_ip} |"
            f"{city}, {zip_code}, {country}, | ISP: {isp} diff sent"
            )

        checkbox_raw = await redis.lrange(checkboxes_key, 0, -1)
        checkboxes_values = [json.loads(v) for v in checkbox_raw]

        # async with checkboxes_mutex:
        diff_array = [
            fh.CheckboxX(
                id=f"cb-{i}",
                checked= checkboxes_values[i],
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
            ip_str = str(ip, 'utf-8')
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
                    fh.Th("zip"),
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
        self.diffs=[]
        return diffs