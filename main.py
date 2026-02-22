import time,asyncio,subprocess, modal
from asyncio import Lock
from pathlib import Path
from fasthtml.js import NotStr
import fasthtml.common as fh
from redis.asyncio import Redis
from uuid import uuid4
import logging
from logging.handlers import RotatingFileHandler
import geo, config, persistence, analytics, fasthtml_components

checkboxes_bitmap_key, checkbox_cache, clients, clients_mutex= "checkboxes_bitmap", {}, {}, Lock()
N_CHECKBOXES, LOAD_MORE_SIZE = 1000000, 2000

css_path_local = Path(__file__).parent / "style_v2.css"
css_path_remote = "/assets/style_v2.css"

app = modal.App("one-million-checkboxes")

volume = modal.Volume.from_name("redis-data-vol", create_if_missing=True)
logs_volume = modal.Volume.from_name("checkbox-app-logs", create_if_missing=True)
LOGS_DIR = "/logs"
_logger = None

app_image = (modal.Image.debian_slim(python_version="3.12")
    .pip_install("python-fasthtml==0.12.36", "httpx==0.27.0" ,"redis>=5.3.0", "pytz", "aiosqlite","markdown==3.10.2")
    .apt_install("redis-server").add_local_file(css_path_local,remote_path=css_path_remote, )
    #.add_local_file("blog_post.md", remote_path="/root/blog_post.md")
    .add_local_file("static/blog.html", remote_path="/root/static/blog.html")
    .add_local_python_source("utils","geo", "config", "fasthtml_components", "persistence", "analytics") )# This is the key: it adds utils.py and makes it importable

def setup_logging():
    """Setup file and console logging + capture print statements"""
    global _logger
    Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("checkbox_app")
    logger.setLevel(logging.INFO)
    logger.handlers = []

    # File handler with rotation (max 10MB per file, keep 5 backups)
    file_handler = RotatingFileHandler( f"{LOGS_DIR}/app.log", maxBytes=10*1024*1024, backupCount=5) # 10MB
    file_handler.setFormatter( logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s'))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter( logging.Formatter('[%(levelname)s] %(message)s') )

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    _logger = logger
    import builtins
    original_print = builtins.print
    def logged_print(*args, **kwargs):
        """Print that also logs to file"""
        message = " ".join(str(arg) for arg in args)
        original_print(*args, **kwargs)
        if _logger and message.strip(): _logger.info(message)
    builtins.print = logged_print
    return logger

@app.function( 
    image = app_image, max_containers=3, volumes={"/data": volume, LOGS_DIR: logs_volume }, timeout=3600,) #keep_warm=1,

@modal.concurrent(max_inputs=1000)
@modal.asgi_app()
def web():# Start redis server locally inside the container (persisted to volume)
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("🚀 One Million Checkboxes App Starting")
    logger.info("=" * 60)

    redis_process = subprocess.Popen(
        [   "redis-server", "--protected-mode", "no", "--bind","127.0.0.1", "--port", "6379", "--dir", "/data", #store data in persistent volume
            "--save", "60", "1","--save", "" ] #save every minute, if 1 change, #disable all other automatic saves
        , stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL )
    time.sleep(1)

    redis = Redis.from_url("redis://127.0.0.1:6379")
    print("Redis server started succesfully with persistent storage")
    
    async def startup_migration():
        await persistence.init_sqlite_db()
        if not (redis_count := await redis.get("total_visitors_count"))or int(redis_count) == 0:
            sqlite_count = await persistence.get_visitor_count_sqlite()
            if sqlite_count > 0: print(f"[STARTUP] Redis empty, restoring {sqlite_count} visitors from SQLite...")
        await redis.setbit(checkboxes_bitmap_key, N_CHECKBOXES - 1, 0)
        print("[STARTUP] Bitmap initialized/verified,... Migration check complete")

    async def on_shutdown():
        print("Shutting down... Saving Redis data")
        try:
            await redis.save()
            print("Redis data saved succesfully")
        except Exception as e: print(f"Error saving Redis data: {e}")
        await redis.close() #not necessarily needed here just best practice
        redis_process.terminate()
        redis_process.wait()
        await volume.commit.aio()
        await logs_volume.commit.aio()
        print("Logs and Volume committed -data persisted")
       
    async def get_checkbox_range_cached(start_idx: int, end_idx:int):
        """ Load a specific range of chekcboxes, with caching"""
        missing = [i for i in range(start_idx, end_idx) if i not in checkbox_cache]
        if missing:
            pipe = redis.pipeline()
            for idx in missing: pipe.getbit(checkboxes_bitmap_key, idx)
            for idx, res in zip(missing, await pipe.execute()): checkbox_cache[idx] = bool(res)
        return [checkbox_cache[i] for i in range(start_idx, end_idx)]
         
    async def get_status():
        """ Get checked/unchecked counts - use redis directly, not cache"""
        checked = await redis.bitcount(checkboxes_bitmap_key)
        return checked,N_CHECKBOXES - checked

    web_app = fh.FastHTML( on_startup=[startup_migration], on_shutdown=[on_shutdown], hdrs=[fh.Style(open(css_path_remote, "r").read()),],)
                                                                                            
    metrics_for_count = { "request_count" : 0,  "last_throughput_log" : time.time() }
    throughput_lock = asyncio.Lock()


    @web_app.middleware("http")#ASGI Middleware for latency + throughput logging
    async def metrics_middleware(request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = (time.time() - start) * 1000 #ms
        print(f"[Latency] {request.url.path} -> {duration:.2f} ms")
        
        async with throughput_lock:
            metrics_for_count["request_count"] +=1
            now = time.time()

            if now - metrics_for_count["last_throughput_log"] >=5: #log throughput every 5 seconds
                rsp = metrics_for_count["request_count"] / (now - metrics_for_count["last_throughput_log"])
                print(f"[THROUGHPUT] {rsp:.2f} req/sec over last 5s")
                metrics_for_count["request_count"] = 0
                metrics_for_count["last_throughput_log"] = now
            
            if metrics_for_count["request_count"] % 100 == 0:
                try: await logs_volume.commit.aio()
                except: pass  # Don't fail requests if log commit fails
        return response

    @web_app.get("/")
    async def get(request):
        logger.info("📄 GET / - Homepage accessed")
        client_ip = analytics.get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')
        logger.info(f"Homepage view | IP: {client_ip} | UA: {user_agent[:50]}")
        referrer = request.headers.get('referer', 'direct')

        await analytics.start_session(client_ip, user_agent, "/", redis)
        await analytics.track_page_view(client_ip, "/", referrer, redis)
        await analytics.track_referrer(client_ip, referrer, redis)

        client = Client()  #register a new client
        async with  clients_mutex: clients[client.id] = client

        checked, unchecked = await get_status()
        await analytics.record_visitors(client_ip,user_agent, await geo.get_geo(client_ip, redis), redis)
        first_chunk_html= await _render_chunk(client.id, offset=0)
        return( 
            fh.Titled(f"One Million Checkboxes"),
            fh.Main(
                fh.Script(analytics.TRACKER_JS),
                fh.Div(NotStr("""<script data-name="BMC-Widget" data-cfasync="false" 
                    src="https://cdnjs.buymeacoffee.com/1.0.0/widget.prod.min.js" 
                    data-id="gptagent.unlock"  data-description="Support me!" data-message="" 
                    data-color="#FFDD00"  data-position="top" data-x_margin="0" data-y_margin="0"></script> """),
                    fh.H1(f" One Million Checkboxes"), style="display: flex; flex-direction: column; align-items: center; gap: 10px;" ),
                fh.Div( fh.Span(f"{checked:,}", cls="status-checked"), " checked • ",
                        fh.Span(f"{unchecked:,}",cls="status-unchecked"), " unchecked", 
                        cls="stats", id="stats", hx_get="/stats", hx_trigger="every 1s",hx_swap="outerHTML" ),
                fh.Div( fh.NotStr(first_chunk_html), cls="grid-container", id="grid-container",
                        hx_get=f"/diffs/{client.id}", hx_trigger="every 500ms",hx_swap="none"),
                fh.Div("Made with FastHTML + Redis deployed with Modal", cls="footer"), cls="container"))

    @web_app.get("/stats")
    async def stats():
        checked, unchecked = await get_status()
        print(f"[STATS] Checked: {checked:,}, Unchecked: {unchecked:,}")
        return fh.Div(  fh.Span(f"{checked:,}", cls="status-checked"), " checked • ",
                        fh.Span(f"{unchecked:,}",cls="status-unchecked"), " unchecked", cls="stats", id="stats", hx_get="every 2s", hx_swap="outerHTML")
    
    @web_app.get("/chunk/{client_id}/{offset}")
    async def chunk(client_id:str, offset:int):
        return fh.NotStr(await _render_chunk(client_id,offset) )
         
    async def _render_chunk(client_id:str, offset:int)->str:
        start_idx, end_idx = offset, min(offset + LOAD_MORE_SIZE, N_CHECKBOXES)
        print(f"[CHUNK] Loading {start_idx:,}-{end_idx:,} for {client_id[:8]}")
        checked_values = await get_checkbox_range_cached(start_idx, end_idx)
        parts =[f'<input type="checkbox" id="cb-{i}" class="cb" {"checked" if is_checked else ''} '
                f'hx-post="/toggle/{i}/{client_id}" hx-swap="none">' 
                for i, is_checked in enumerate(checked_values, start=start_idx)]
        if end_idx < N_CHECKBOXES:
            parts.append( f'<span class="lazy-trigger" hx-get="/chunk/{client_id}/{end_idx}" '
                          f'hx-trigger="intersect once" hx-target="#grid-container" hx-swap="beforeend"></span>' )
        return "".join(parts)
    
    @web_app.post("/toggle/{i}/{client_id}")
    async def toggle(request, i: int, client_id: str):
        client_ip = analytics.get_real_ip(request)
        await analytics.log_event(client_ip, "checkbox_toggle", {"checkbox_id": i, "client_id": client_id, "timestamp": time.time()}, redis)
        async with clients_mutex:
            current = checkbox_cache.get(i, bool(await redis.getbit(checkboxes_bitmap_key, i)))
            new_val = not current; checkbox_cache[i] = new_val
            try:
                await redis.setbit(checkboxes_bitmap_key, i, 1 if new_val else 0)
                print(f"[TOGGLE] {i}: {current} -> {new_val}")
            except Exception as e: print(f"[TOGGLE ERROR] {e}")
            expired = [cid for cid, cl in clients.items() if cid != client_id and (not cl.is_active() or (lambda: cl.add_diff(i) or False)())]
            for cid in expired: del clients[cid]
        c, u = await get_status()
        return fh.Div(fh.Span(f"{c:,}", cls="status-checked"), " checked • ", fh.Span(f"{u:,}", cls="status-unchecked"),
                      " unchecked", cls="stats", id="stats", hx_get="/stats", hx_trigger="every 1s", hx_swap="outerHTML", hx_swap_oob="true")

    @web_app.get("/diffs/{client_id}") #clients polling for outstanding diffs
    async def diffs(request, client_id:str):
        async with clients_mutex:
            client = clients.get(client_id, None)
            if client is None or len(client.diffs) == 0: return ""
            client.heartbeat()
            diffs_list = client.pull_diffs()
        return [fh.Input(type="checkbox", id=f"cb-{i}", checked = bool(await redis.getbit(checkboxes_bitmap_key, i)), 
                         hx_post=f"/toggle/{i}/{client_id}", hx_swap="none", hx_swap_oob="true", cls= "cb" ) for i in diffs_list ]
      
    @web_app.get("/referrer-stats")
    async def referrer_stats_page(request):
        return await analytics.render_referrer_stats_page(redis)

    @web_app.get("/time-spent-stats")
    async def time_spent_stats_page(request):
        return await analytics.render_time_spent_stats_page(redis)

    @web_app.post("/heartbeat")
    async def heartbeat(request):
        return await analytics.handle_heartbeat(request, redis)
    
    @web_app.post("/track-scroll")
    async def track_scroll(request):
        await analytics.update_scroll_depth(analytics.get_real_ip(request), (await request.json()).get("depth", 0), redis)
        return {"status": "ok"}
    
    @web_app.post("/session-end")
    async def session_end(request):
        return await analytics.handle_session_end(request, redis)

    @web_app.get("/visitors")
    async def visitors_page(request, offset: int = 0, limit: int = 5, days: int= 30):
        return await analytics.render_visitors_page(request, redis, offset, limit, days)
    
    @web_app.get("/blog_visitors")
    async def blog_visitors_page(request, offset: int = 0, limit: int = 5, days: int = 30):
        return await analytics.render_blog_visitors_stats_page(request, redis, offset, limit, days)
    
    logger.info("✅ One Million Checkboxes App initialized successfully")

    # ↓ THIS GOES LAST, replaces `return web_app`
    from starlette.applications import Starlette
    from starlette.responses import FileResponse, HTMLResponse
    from starlette.routing import Route, Mount

    # async def raw_blog(request):
    #     return FileResponse("/root/static/blog.html")
    async def raw_blog(request):
        client_ip   = analytics.get_real_ip(request)
        user_agent  = request.headers.get('user-agent', 'unknown')
        referrer    = request.headers.get('referer', 'direct')
        path        = "/blog"   # or request.url.path if you want it dynamic

        # The same tracking calls you use elsewhere
        await analytics.start_session(client_ip, user_agent, path, redis)
        await analytics.track_page_view(client_ip, path, referrer, redis)
        await analytics.track_referrer(client_ip, referrer, redis)
        
        # If you also call this on the main page:
        geo_data = await geo.get_geo(client_ip, redis)
        await analytics.record_visitors(client_ip, user_agent, geo_data, redis)

        # Optional: add basic error handling / fallback
        try:
            return FileResponse("/root/static/blog.html", media_type="text/html")
        except Exception as e:
            print(f"[BLOG] Failed to serve file: {e}")
            return HTMLResponse(
                "<h1>Blog temporarily unavailable</h1><p>Error loading content.</p>",
                status_code=503
            )

    return Starlette(routes=[
        Route("/blog", raw_blog),
        Mount("/", app=web_app),
    ])
    

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
        if i not in self.diffs: self.diffs.append(i)

    def pull_diffs(self):
        diffs, self.diffs = self.diffs, []
        return diffs

    def set_geo(self, geo_obj, now=None): 
        self.geo = geo_obj 
        self.geo_ts = now or time.time()

    def has_recent_geo(self, now=None): 
        return (self.geo is not None) and ((now or time.time()) - self.geo_ts) <= config.CLIENT_GEO_TTL