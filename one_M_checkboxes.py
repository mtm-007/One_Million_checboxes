import time,asyncio, json,subprocess, pytz, httpx, modal
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
from fasthtml.js import NotStr
import fasthtml.common as fh
from datetime import datetime, timezone
from redis.asyncio import Redis
import datetime as dt
import utils
import logging
from logging.handlers import RotatingFileHandler

N_CHECKBOXES, LOAD_MORE_SIZE = 1000000, 2000

checkboxes_bitmap_key, checkbox_cache, clients, clients_mutex= "checkboxes_bitmap", {}, {}, Lock()
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")
#SQLITE_DB_PATH = "/data/visitors.db"

css_path_local = Path(__file__).parent / "style_v2.css"
css_path_remote = "/assets/style_v2.css"

app = modal.App("one-million-checkboxes")
volume = modal.Volume.from_name("redis-data-vol", create_if_missing=True)
LOGS_DIR = "/logs"
logs_volume = modal.Volume.from_name("checkbox-app-logs", create_if_missing=True)
_logger = None


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
        
        if _logger and message.strip():
            _logger.info(message)
    builtins.print = logged_print
    return logger

app_image = (modal.Image.debian_slim(python_version="3.12")
    .pip_install("python-fasthtml==0.12.36", "httpx==0.27.0" ,"redis>=5.3.0", "pytz", "aiosqlite")
    .apt_install("redis-server").add_local_file(css_path_local,remote_path=css_path_remote)
    .add_local_python_source("utils") )# This is the key: it adds utils.py and makes it importable
    
@app.function( image = app_image, max_containers=1, volumes={"/data": volume, LOGS_DIR: logs_volume }, timeout=3600,) #keep_warm=1,
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
        await utils.init_sqlite_db()
        redis_count = await redis.get("total_visitors_count")
        if not redis_count or int(redis_count) == 0:
            sqlite_count = await utils.get_visitor_count_sqlite()
            if sqlite_count > 0:
                print(f"[STARTUP] Redis empty, restoring {sqlite_count} visitors from SQLite...")
        
        await redis.setbit(checkboxes_bitmap_key, N_CHECKBOXES - 1, 0)
        print("[STARTUP] Bitmap initialized/verified,... Migration check complete")
       
    async def get_checkbox_range_cached(start_idx: int, end_idx:int):
        """ Load a specific range of chekcboxes, with caching"""
        cached_values ,missing_indices = [], []
        for i in range(start_idx, end_idx):
            if i in checkbox_cache: cached_values.append((i, checkbox_cache[i]))
            else: missing_indices.append(i)
        
        if missing_indices:
            pipe = redis.pipeline() #use pipeline for batch loading
            for idx in missing_indices: pipe.getbit(checkboxes_bitmap_key, idx)
            results = await pipe.execute()
            for idx, result in zip(missing_indices, results):
                value = bool(result) #json.loads(result) if result is not None else False
                checkbox_cache[idx] = value
                cached_values.append((idx, value))

        cached_values.sort(key=lambda x:x[0])  #sort by index to maintain order
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
        except Exception as e: print(f"Error saving Redis data: {e}")
        await redis.close() #not necessarily needed here just best practice
        redis_process.terminate()
        redis_process.wait()
        await volume.commit.aio()
        await logs_volume.commit.aio()
        print("Logs and Volume committed -data persisted")

    style= open(css_path_remote, "r").read()
    app, _= fh.fast_app( on_startup=[startup_migration], on_shutdown=[on_shutdown], hdrs=[fh.Style(style)], )

    metrics_for_count = { "request_count" : 0,  "last_throughput_log" : time.time() }
    throughput_lock = asyncio.Lock()

    @app.middleware("http")#ASGI Middleware for latency + throughput logging
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
    
    @app.get("/stats")
    async def stats():
        checked, unchecked = await get_status()
        print(f"[STATS] Checked: {checked:,}, Unchecked: {unchecked:,}")
        return fh.Div(  fh.Span(f"{checked:,}", cls="status-checked"), " checked • ",
                        fh.Span(f"{unchecked:,}",cls="status-unchecked"), " unchecked", cls="stats", id="stats", hx_get="every 2s", hx_swap="outerHTML")
    
    @app.get("/chunk/{client_id}/{offset}")
    async def chunk(client_id:str, offset:int):
        return fh.NotStr(await _render_chunk(client_id,offset) )
         
    async def _render_chunk(client_id:str, offset:int)->str:
        start_idx = offset
        end_idx = min(offset + LOAD_MORE_SIZE, N_CHECKBOXES)
        print(f"[CHUNK] Loading {start_idx:,}-{end_idx:,} for {client_id[:8]}")
        checked_values = await get_checkbox_range_cached(start_idx, end_idx)

        parts =[f'<input type="checkbox" id="cb-{i}" class="cb" {"checked" if is_checked else ''} '
                f'hx-post="/toggle/{i}/{client_id}" hx-swap="none">' 
                for i, is_checked in enumerate(checked_values, start=start_idx)]
        
        if end_idx < N_CHECKBOXES:
            parts.append( f'<span class="lazy-trigger" hx-get="/chunk/{client_id}/{end_idx}" '
                          f'hx-trigger="intersect once" hx-target="#grid-container" hx-swap="beforeend"></span>' )
        return "".join(parts)
    
    @app.get("/")
    async def get(request):
        logger.info("📄 GET / - Homepage accessed")
        client_ip = utils.get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')
        logger.info(f"Homepage view | IP: {client_ip} | UA: {user_agent[:50]}")
        referrer = request.headers.get('referer', 'direct')

        await utils.start_session(client_ip, user_agent, "/", redis)
        await utils.track_page_view(client_ip, "/", referrer, redis)
        await utils.track_referrer(client_ip, referrer, redis)

        client = utils.Client()  #register a new client
        async with  clients_mutex: clients[client.id] = client

        checked, unchecked = await get_status()
        await utils.record_visitors(client_ip,user_agent, await utils.get_geo(client_ip, redis), redis)
        first_chunk_html= await _render_chunk(client.id, offset=0)

        return( 
            fh.Titled(f"One Million Checkboxes"),
            fh.Main(
                # Add tracking script
                fh.Script("""
                    const tracker = { startTime: Date.now(), lastHeartbeat: Date.now(), scrollDepth: 0,
                        init() { this.sendHeartbeat(); setInterval(() => { this.sendHeartbeat(); }, 10000);
                
                            const activityEvents = ['click', 'scroll', 'keypress', 'mousemove', 'touchstart'];
                            activityEvents.forEach(event => { document.addEventListener(event, ()=>{ this.onUserActivity(); }, { passive: true }); });
                            
                            let scrollTimer; window.addEventListener('scroll', ()=>{clearTimeout(scrollTimer); scrollTimer=setTimeout(()=>{
                                const depth = Math.round((window.scrollY / (document.body.scrollHeight - window.innerHeight))*100);
                                this.scrollDepth = Math.max(this.scrollDepth, depth);fetch('/track-scroll', { method: 'POST',  headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({depth: depth})}).catch(err => console.log('Scroll tracking failed:', err));},500);});
                            window.addEventListener('beforeunload', ()=>{this.endSession());
                            document.addEventListener('visibilitychange', ()=>document.hidden?this.endSession():this.sendHeartbeat());
                            window.addEventListener('pagehide', ()=>{this.endSession());},
                        onUserActivity(){const now=Date.now(); if(now - this.lastHeartbeat > 3000)this.sendHeartbeat();},
                        sendHeartbeat() {const duration = (Date.now() - this.startTime) / 1000;
                            fetch('/heartbeat', { method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({ duration: duration, timestamp: Date.now()}),keepalive: true})
                            .catch(err => console.log('Heartbeat failed:', err));this.lastHeartbeat = Date.now();},
                        endSession() {const duration = (Date.now() - this.startTime) / 1000;
                            const data = JSON.stringify({ duration: duration, scrollDepth: this.scrollDepth, timestamp: Date.now() });
                            navigator.sendBeacon?navigator.sendBeacon('/session-end', data):
                                fetch('/session-end', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: data, keepalive: true}).catch(err=>console.log('Session end failed:', err));}};
                    tracker.init();
                """),
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

    @app.post("/heartbeat")
    async def heartbeat(request):
        """Track that user is still active and update duration"""
        client_ip = utils.get_real_ip(request)
        try:
            data = await request.json()
            duration = data.get("duration", 0)  # Duration in seconds from frontend
        except:
            duration = 0
        
        await utils.update_session_activity(client_ip, redis)
        if duration > 0:            
            if (visitor_data:=await redis.get(f"visitor:{client_ip}")):
                visitor = json.loads(visitor_data)
                visitor["current_session_duration"] = duration
                visitor["last_activity_time"] = time.time()
                await redis.set(f"visitor:{client_ip}", json.dumps(visitor) )
        return {"status": "ok", "duration": duration}

    
    @app.post("/track-scroll")
    async def track_scroll(request):
        """ Track scroll depth"""
        client_ip = utils.get_real_ip(request)
        data = await request.json()
        depth = data.get("depth", 0)
        await utils.update_scroll_depth(client_ip, depth, redis)
        return {"status": "ok"}
    
    @app.post("/session-end")
    async def session_end(request):
        """End session and save final time spent"""
        client_ip = utils.get_real_ip(request)
        try:
            data = await request.json()
            duration = data.get("duration", 0)  # Duration in seconds from frontend
        except:
            duration = 0
    
        if visitor_data := await redis.get(f"visitor:{client_ip}"):
            visitor = json.loads(visitor_data)
            visitor["total_time_spent"] = visitor.get("total_time_spent", 0) + duration
            visitor["last_session_duration"] = duration
            visitor["total_sessions"] = visitor.get("total_sessions", 0) + 1
            
            if visitor["total_sessions"] > 0: visitor["avg_session_duration"] = visitor["total_time_spent"] / visitor["total_sessions"]
            await redis.set(f"visitor:{client_ip}", json.dumps(visitor))
            await utils.save_visitor_to_sqlite(visitor)
            print(f"[SESSION END] {client_ip} spent {duration:.1f} seconds")
        await redis.delete(f"session:{client_ip}")
        return {"status": "ok", "duration": duration}

    @app.get("/referrer-stats")
    async def referrer_stats_page(request):
        """Show referrer/traffic source analytics"""
        top_referrers, type_stats = await utils.get_referrer_stats(redis, limit=30),await utils.get_referrer_type_stats(redis)
        total_visitors = sum(type_stats.values())
        
        type_chart_bars = []
        type_colors = { "direct": "#667eea", "social": "#ff6b6b", "search": "#4ecdc4", "referral": "#45b7d1", "unknown": "#95a5a6" }
        
        for ref_type, count in type_stats.items():
            if total_visitors > 0:
                percentage = (count / total_visitors) * 100
                type_chart_bars.append(
                    fh.Div( fh.Span(ref_type.title(), cls="bar-label-horizontal"),
                        fh.Div( fh.Div(fh.Span(f"{count} ({percentage:.1f}%)", style="color: white; font-size: 0.9em; padding-left: 8px;"
                                ) if count > 0 else "", style=f"width: {max(percentage, 2)}%; background: {type_colors.get(ref_type, '#999')};",
                                cls="bar-fill-horizontal"), cls="bar-track-horizontal" ), cls="bar-horizontal" ))
    
        referrer_rows = []
        for i, ref in enumerate(top_referrers, 1):
            source ,count = ref["source"], ref["count"]
            percentage = (count / total_visitors * 100) if total_visitors > 0 else 0
            
            # Add icon based on source
            if "Google" in source: icon = "🔍"
            elif any(social in source for social in ["Facebook", "Twitter", "Instagram", "LinkedIn", "Reddit"]): icon = "📱"
            elif source == "Direct": icon = "🔗"
            else: icon = "🌐"
            
            referrer_rows.append( fh.Tr( fh.Td(f"#{i}"), fh.Td(f"{icon} {source}"), fh.Td(count), fh.Td(f"{percentage:.1f}%"), cls="visitor-row" ))
        
        return (
            fh.Titled("Referrer Analytics", fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0")),
            fh.Main( fh.H1("Traffic Sources & Referrers", cls="dashboard-title"),
                fh.Div( fh.Div("Total Tracked Visitors", cls="stats-label"), fh.Div(f"{total_visitors:,}", cls="stats-number"), cls="stats-card" ),
                fh.Div( fh.H2("Traffic by Type", cls="section-title"),
                        fh.Div( fh.Div( *type_chart_bars if type_chart_bars else [ fh.P("No referrer data yet", style="text-align: center; color:#999;")]
                            ,cls="chart-bars-container" ), cls="chart-container" ), ),
                fh.Div( fh.H2("Top Referrer Sources", cls="section-title"),
                    fh.Table( fh.Tr(fh.Th("Rank"), fh.Th("Source"), fh.Th("Visitors"), fh.Th("Percentage") ),
                        *referrer_rows if referrer_rows else [ fh.Tr(fh.Td("No data yet", colspan=4, style="text-align: center; color:#999; padding: 20px;")) ], 
                        cls="table visitors-table" ), style="margin-top: 30px;" ),
                fh.Div( fh.A("← Back to visitors", href="/visitors", cls="back-link"),
                    fh.A("← Back to checkboxes", href="/", cls="back-link", style="margin-left: 20px;"),
                    style="text-align: center; margin-top: 30px;" ), cls="visitors-container" ))
    
    @app.get("/time-spent-stats")
    async def time_spent_stats_page(request):
        logger.info("⏱️  GET /time-spent-stats")
        stats, buckets = await utils.get_time_stats(redis, 100), await utils.get_time_buckets(redis, 500)
        bkt_colors = {"0-10s": "#e74c3c", "10-30s": "#e67e22", "30s-1m": "#f39c12", "1-2m": "#f1c40f",
                      "2-5m": "#2ecc71", "5-10m": "#27ae60", "10-30m": "#3498db", "30m+": "#9b59b6"}
        
        return (fh.Titled("Time Spent Analytics", fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0")),
                fh.Main(fh.H1("⏱️ Time Spent Analytics", cls="dashboard-title"),
                    fh.Div(utils.stat_card("Total Time", f"{stats['total_time']/3600:.1f}h", f"{stats['total_time']:.0f}s"),
                           utils.stat_card("Avg per Visitor", f"{stats['avg_per_visitor']/60:.1f}m", f"{len(stats['visitors'])} visitors"),
                           utils.stat_card("Avg per Session", f"{stats['avg_per_session']/60:.1f}m", f"{stats['total_sessions']} sessions"),
                           utils.stat_card("Visitors Tracked", f"{len(stats['visitors'])}", "with time data"), cls="stats-grid"),
                    fh.Div(fh.H2("Time Spent Distribution", cls="section-title"),
                           fh.P("How long visitors stay", style="text-align:center;color:#94a3b8;margin-bottom:20px;"),
                           fh.Div(utils.h_chart(buckets, bkt_colors), cls="chart-container")),
                    fh.Div(fh.H2("Most Engaged Visitors (Top 30)", cls="section-title"),
                        fh.Table(fh.Tr(fh.Th("Rank"), fh.Th("IP"), fh.Th("Total Time"), fh.Th("Sessions"), 
                                       fh.Th("Avg Session"), fh.Th("Last Session"), fh.Th("Device"), fh.Th("Type"), fh.Th("Location")),
                            *[fh.Tr(fh.Td(f"#{i}"), fh.Td(v.get("ip")), 
                                    fh.Td(fh.Span(utils.fmt_time(t := v.get("total_time_spent", 0)), 
                                        style=f"background:{'#9b59b6' if t>300 else '#3498db' if t>120 else '#2ecc71' if t>60 else '#95a5a6'};color:white;padding:4px 8px;border-radius:4px;font-weight:600;")),
                                    fh.Td(v.get("total_sessions", 0)), fh.Td(utils.fmt_time(v.get("avg_session_duration", 0))),
                                    fh.Td(utils.fmt_time(v.get("last_session_duration", 0))), fh.Td(v.get("device", "Unknown")),
                                    fh.Td(utils.class_badge(v.get("classification", "Human"))),
                                    fh.Td(f"{v.get('city','Unknown')}, {v.get('country','Unknown')}", style="font-size:0.85em;"), cls="visitor-row")
                              for i, v in enumerate(stats["visitors"][:30], 1)] if stats["visitors"] else 
                            [fh.Tr(fh.Td("No data", colspan=9, style="text-align:center;color:#999;padding:20px;"))],
                            cls="table visitors-table"), style="margin-top:30px;"),
                    utils.nav_links(("← Back to visitors", "/visitors"), 
                                    ("View Referrer Stats", "/referrer-stats", "background:#4ecdc4;"),
                                    ("← Back to checkboxes", "/")), cls="visitors-container"))
    
    @app.post("/toggle/{i}/{client_id}")
    async def toggle(request, i:int, client_id:str):
        client_ip = utils.get_real_ip(request)
        logger.info(f"🔘 Checkbox {i} toggled by {client_ip}")
        await utils.log_event(client_ip, "checkbox_toggle",{ "checkbox_id": i, "client_id": client_id, "timestamp": time.time() }, redis)
        
        async with clients_mutex:
            client = clients.get(client_id)  
            current = checkbox_cache[i] if i in checkbox_cache else bool (await redis.getbit(checkboxes_bitmap_key, i))
            new_value = not current
            checkbox_cache[i] = new_value #Update cache
            print(f"[TOGGLE] index{i}: {current} -> {new_value}")

            try:
                await redis.setbit(checkboxes_bitmap_key, i, 1 if new_value else 0)
                bit_value = await redis.getbit(checkboxes_bitmap_key, i)
                print(f"[TOGGLE] Verified bitmap[{i}] = {bit_value}")
            except Exception as e: print(f"[TOGGLE ERROR] Failed to update Redis: {e}")

            expired = []
            for client in clients.values():
                if client.id == client_id: continue
                if not client.is_active(): expired.append(client.id) #clean up old clients
                client.add_diff(i)#add diff to client fpr when they next poll
            for client_id in expired: del clients[client_id]

        checked, unchecked = await get_status()
        return fh.Div(  fh.Span(f"{checked:,}", cls="status-checked"), " checked • ",  fh.Span(f"{unchecked:,}", cls="status-unchecked"),
                        " unchecked", cls="stats", id="stats", hx_get="/stats", hx_trigger="every 1s",hx_swap="outerHTML", hx_swap_oob="true" )
    
    @app.get("/diffs/{client_id}") #clients polling for outstanding diffs
    async def diffs(request, client_id:str):
        async with clients_mutex:
            client = clients.get(client_id, None)
            if client is None or len(client.diffs) == 0: return ""
            client.heartbeat()
            diffs_list = client.pull_diffs()
        
        return [fh.Input(type="checkbox", id=f"cb-{i}", checked = bool(await redis.getbit(checkboxes_bitmap_key, i)), 
                         hx_post=f"/toggle/{i}/{client_id}", hx_swap="none", hx_swap_oob="true", cls= "cb" )
                for i in diffs_list ]
      
    @app.get("/visitors")
    async def visitors_page(request, offset: int = 0, limit: int = 5, days: int= 30):#100):
        client_ip = utils.get_real_ip(request)
        logger.info(f"👥 GET /visitors - Dashboard accessed (offset={offset}, limit={limit})")
        referrer = request.headers.get('referer', '')
        await utils.track_page_view(client_ip, "/visitors", referrer, redis)
        await utils.track_referrer(client_ip, referrer, redis)

        days = max(7, min(days, 30))
        print(f"[VISITORS] Loading visitors dashboard: offset={offset}, limit={limit}, window={days}")
        recent_ips = await redis.zrange("recent_visitors_sorted", offset, offset + limit - 1, desc=True)
        print(f"[VISITORS] Found {len(recent_ips)} IPs in sorted set")

        visitors = []
        for ip in recent_ips:
            ip_str = ip.decode('utf-8') if isinstance(ip, bytes) else str(ip)
            if (visitors_raw := await redis.get(f"visitor:{ip_str}")):
                v = json.loads(visitors_raw)
                v["timestamp"] = float(v.get("timestamp", time.time()))
                visitors.append(v)
        print(f"[VISITORS] Loaded {len(visitors)} visitor records")

        total_in_db = await redis.zcard("recent_visitors_sorted")
        total_count = int(tv) if( tv := await redis.get("total_visitors_count")) else 0
        print(f"[VISITORS] Total unique visitors: {total_count}, in DB: {total_in_db}")

        #group visitors by day for the table
        visitors_by_day = {}
        for v in visitors:
            day = utils.utc_to_local(v["timestamp"]).strftime("%Y-%m-%d")
            if day not in visitors_by_day:
                visitors_by_day[day] = []
            visitors_by_day[day].append(v)
    
        #Create table rows grouped by day
        table_content = []
        for day_key in sorted(visitors_by_day.keys(), reverse=True):
            day_visitors = visitors_by_day[day_key]
            day_display = datetime.strptime(day_key, "%Y-%m-%d").strftime("%A, %B %d, %Y")
            visitor_count = len(day_visitors)

            table_content.append(fh.Tr( fh.Td( fh.Div(fh.Strong(day_display),
                            fh.Span(f" ({visitor_count} visitor{'s' if visitor_count != 1 else ''})",
                            style="color: #667eea; margin-left: 10px;"), style="padding: 10px 0;" ), colspan=10, cls="day-separator" )))

            #add visitors rows for this day
            for v in day_visitors:
                is_vpn , is_relay= v.get("is_vpn", False), "Relay" in  v.get("classification", "")
                first_ref, last_ref = v.get("first_referrer", {}), v.get("last_referrer", {})
        
                table_content.append(fh.Tr(
                    fh.Td(v.get("ip")), fh.Td(v.get("device", "?")), fh.Td(utils.sec_badge(is_vpn, is_relay)),
                    fh.Td(fh.Div(fh.Div(c := v.get("classification", "Human"), 
                                       style=f"font-weight:bold;color:{'#ff9500' if 'Bot' in c else '#007aff'};"),
                                fh.Div(v.get("usage_type", "Residential"), style="font-size:0.8em;opacity:0.7;"))),
                    fh.Td(utils.ref_badge(first_ref.get("source", "Direct") if first_ref else "Direct", 
                                         first_ref.get("type", "direct") if first_ref else "direct")),
                    fh.Td(utils.ref_badge(last_ref.get("source", "Direct") if last_ref else "Direct", 
                                         last_ref.get("type", "direct") if last_ref else "direct")),
                    fh.Td((v.get("isp") or "-")[:40], style="font-size:0.85em;"), 
                    fh.Td(v.get("city") or "-"), fh.Td(v.get("zip", "-")), fh.Td(v.get("country") or "-"),
                    fh.Td(fh.Span(f"{v.get('visit_count',1)}", cls="visit-badge")),
                    fh.Td(utils.utc_to_local(v["timestamp"]).strftime("%H:%M:%S")),
                    fh.Td(f"{v.get('total_time_spent',0)/60:.1f}m"), fh.Td(v.get('total_actions', 0)),
                    fh.Td(v.get('last_page', '/')[:20]), cls="visitor-row"))
                
         # Chart data
        now_local = utils.utc_to_local(time.time())
        chart_days_data = [(date_display := (now_local.date() - dt.timedelta(days=i)).strftime("%a-%b-%d"),
                           sum(1 for v in visitors if utils.utc_to_local(v["timestamp"]).strftime("%Y-%m-%d") == 
                               (now_local.date() - dt.timedelta(days=i)).strftime("%Y-%m-%d")))
                          for i in range(days - 1, -1, -1)]
        
        return (fh.Titled("Visitors Dashboard", fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0, maximum-scale=5.0")),
                fh.Main(fh.H1("Recent Visitors Dashboard", cls="dashboard-title"),
                    utils.stat_card("Total Unique Visitors", f"{total_count:,}", f"Database: {total_in_db:,} records"),
                    utils.pagination(offset, limit, total_in_db, "/visitors", {"days": days}),
                    fh.Div(fh.H2(f"Visitors by Day - Central Time", cls="section-title", style="margin:0;"),
                           utils.range_sel(days, limit, offset, "/visitors"),
                           style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:15px;"),
                    fh.Div(fh.Div(utils.gradient_chart(chart_days_data), cls="chart-bars-container"), cls="chart-container"),
                    utils.nav_links(("← Back to checkboxes", "/"), 
                                    ("View Referrer Stats →", "/referrer-stats", "background:#4ecdc4;"),
                                    ("View Time Stats →", "/time-spent-stats", "background:#9b59b6;")),
                    fh.Div(fh.H2(f"Visitors Dashboard (Last {limit} Visitors)", cls="section-title"),
                           fh.P("← Scroll horizontally to see all columns →", 
                                style="text-align:center;color:#667eea;font-size:0.9em;margin-bottom:10px;font-weight:600;"),
                           fh.Div(fh.Table(fh.Tr(fh.Th("IP"), fh.Th("Device"), fh.Th("Security"), fh.Th("Category"), 
                                                 fh.Th("First Source"), fh.Th("Last Source"), fh.Th("ISP/Org"), 
                                                 fh.Th("City"), fh.Th("Zip"), fh.Th("Country"), fh.Th("Visits"), 
                                                 fh.Th("Last seen"), fh.Th("Time Spent"), fh.Th("Actions"), fh.Th("Last Page")),
                                          *table_content, cls="table visitors-table") if table_content else 
                                  fh.P("No visitors", style="text-align:center;color:#999;padding:20px;"),
                                  cls="table-wrapper", style="overflow-x:auto;-webkit-overflow-scrolling:touch;")),
                    utils.pagination(offset, limit, total_in_db, "/visitors", {"days": days}),
                    utils.nav_links(("← Back to checkboxes", "/")), cls="visitors-container"))
    
    logger.info("✅ One Million Checkboxes App initialized successfully")
    return app