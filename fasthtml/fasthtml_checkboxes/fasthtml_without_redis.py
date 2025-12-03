import time
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
import modal
import fasthtml.common as fh
import inflect
import httpx
import asyncio


N_CHECKBOXES=10000

app = modal.App("fasthtml-checkboxes")
db = modal.Dict.from_name("fasthtml-checkboxes-db", create_if_missing=True)

#new: Modal dict for caching IP geolocation results
geo_cache = modal.Dict.from_name("ip-geo-cache", create_if_missing=True)

recent_visitors = modal.Dict.from_name("fasthtml-recent-visitors", create_if_missing=True)

#New geolocation helper function
async def get_geo(ip:str):
    """Return geo info from ip using cache + fallback providers"""
    #check cache first
    if ip in geo_cache:
        return geo_cache[ip]
    #primary provider
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"https://ipapi.co/{ip}/json/")
        if r.status_code == 200:
            data = r.json()
            if "country_name" in data:
                geo_cache[ip] = data
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
                geo_cache[ip] = data
                return data
    except Exception:
        pass
    #last resort 
    data = {"ip": ip, "city": None, "country": None, "zip": None}
    #if all fail
    return data
async def record_visitors(ip,user_agent, geo):
    visitors = recent_visitors.get("list", [])
    for v in visitors:
        if v.get("ip") == ip:
            v["timestamp"] = time.time()
            recent_visitors["list"] = visitors[-100:]
    #if Ip not found, add new
    entry = {
        "ip": ip,
        "user_agent": user_agent[:120],
        "city": geo.get("city"),
        "zip": geo.get("postal") or geo.get("zip"),
        "country": geo.get("country") or geo.get("country_name"),
        "timestamp": time.time(),
    }
    visitors.append(entry)
    recent_visitors["list"] = visitors[-100:]

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

@app.function(
    image = modal.Image.debian_slim(python_version="3.12").pip_install(
        "python-fasthtml==0.12.35", "inflect~=7.4.0", "httpx==0.27.0")
    .add_local_file(css_path_local,remote_path=css_path_remote),
    max_containers=1,
)

@modal.concurrent(max_inputs=1000)
@modal.asgi_app()
def web():

    #connected clients are tracked in memory
    clients = {}
    clients_mutex = Lock()
    #keep all checkbox states in memory during operation, and persist to modal dict across restarts
    checkboxes = db.get("checkboxes", [])
    checkboxes_mutex = Lock()

    if len(checkboxes) == N_CHECKBOXES:
        print("Restored checkboxes state from previous session.")
    else:
        print("Initializing checkbox state.")
        checkboxes = [False] * N_CHECKBOXES
    
    async def on_shutdown():
        # Handle the shutdown event by persisting current state to modal dict
        async with checkboxes_mutex:
            db["checkboxes"]=checkboxes
        print("checkbox state persisted.")
    
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
        global request_count, last_throughput_log

        start = time.time()
        response = await call_next(request)
        duration = (time.time() - start) * 100 #ms
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
        #log IP address
        client_ip = get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')

        #geo location look up
        geo = await get_geo(client_ip)
        await record_visitors(client_ip, user_agent, geo)
        
        city = geo.get("city")
        zip = geo.get('postal')
        country = geo.get("country_name") or geo.get("country")
        isp = geo.get("org") or geo.get("isp")
        print(f"[HOME] New Client connected - IP: {client_ip} | {city}, {zip}, {country} | ISP: {isp} |- User-Agent: {user_agent[:80]}...")

        #register a new client
        client = Client()
        async with  clients_mutex:
            clients[client.id] =client

        checkbox_array = [ 
            fh.CheckboxX(
                id=f"cb-{i}",
                checked= val,
                # when clicked, that checkbox will send a POST request to the server with its index
                hx_post=f"/checkbox/toggle/{i}/{client.id}",  
            )
                for i,val in enumerate(checkboxes)
            ]
        
        return(
            fh.Titled(f"{N_CHECKBOXES // 1000}k Checkboxes"),
            fh.Main(
                fh.H1(
                    f"{inflect.engine().number_to_words(N_CHECKBOXES).title()} Checkboxes"),
                fh.Div( *checkbox_array,
                       id="checkbox-array",),
                cls="container",
                # use HTMX to poll for diffs to apply
                hx_trigger="every 1s", #poll every second
                hx_get=f"/diffs/{client.id}", #call the diffs  endpoint
                hx_swap="none", #dont replace the entire page
            ),
        )
    #users submitting checkbox toggles
    @app.post("/checkbox/toggle/{i}/{client_id}")
    async def toggle(request, i:int,client_id:str):
        client_ip = get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')

        geo = await get_geo(client_ip)
        await record_visitors(client_ip, user_agent, geo)
        city = geo.get("city")
        zip = geo.get('postal')
        country = geo.get("country_name") or geo.get("country")
        isp = geo.get("org") or geo.get("isp")
        print(
            f"[TOGGLE] Checkbox {i} toggled by {client_id[:8]} | Checkbox {i} |"
            f"IP: {client_ip} | {city}, {zip}, {country} | ISP: {isp} | - User-Agent: {user_agent[:50]}...")

        async with checkboxes_mutex:
            checkboxes[i]= not checkboxes[i]
        
        async with clients_mutex:
            expired = []
            for client in clients.values():
                if client.id == client_id:
                    #ignore self; keep our own diffs
                    continue
                #clean up old clients
                if not client.is_active():
                    expired.append(client.id)
                
                #add diff to client fpr when they next poll
                client.add_diff(i)

            for client_id in expired:
                del clients[client_id]
        return
    
    #clients polling for outstanding diffs
    @app.get("/diffs/{client_id}")
    async def diffs(request, client_id:str):
        # we use the `hx_swap_oob='true'` feature to
        # push updates only for the checkboxes that changed
        async with clients_mutex:
            client = clients.get(client_id, None)
            if client is None or len(client.diffs) == 0:
                return
            
            client.heartbeat()
            diffs = client.pull_diffs()
        
        client_ip = get_real_ip(request)
        geo = await get_geo(client_ip)
        city = geo.get("city")
        zip = geo.get('postal')
        country = geo.get("country_name") or geo.get("country")
        isp = geo.get("org") or geo.get("isp")
        print(
            f"[DIFFS] Sending {len(diffs)} diffs to {client_id[:8]}| IP: {client_ip} |"
            f"{city}, {zip}, {country}, | diff sent"
            )

        async with checkboxes_mutex:
            diff_array = [
                fh.CheckboxX(
                    id=f"cb-{i}",
                    checked= checkboxes[i],
                    # when clicked, that checkbox will send a POST request to the server with its index
                    hx_post=f"/checkbox/toggle/{i}/{client_id}",
                    hx_swap_oob="true",# allows us to later push diffs to arbitrary checkboxes by id
                )
                for i in diffs
            ]
        return diff_array
    @app.get("/visitors")
    async def visitors_page(request):
        visitors = recent_visitors.get("list", [])

        rows = [
            fh.Tr(
                fh.Td(v["ip"]),
                fh.Td(v["city"] or "-"),
                fh.Td(v.get("zip", "-")),
                fh.Td(v["country"] or "-"),
                fh.Td(time.strftime("%H:%M:%S", time.localtime(v["timestamp"]))),
            )
            for v in reversed(visitors)
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