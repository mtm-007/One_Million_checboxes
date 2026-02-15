import time,asyncio, json,subprocess, pytz, httpx, modal
from asyncio import Lock
from pathlib import Path
from uuid import uuid4
from fasthtml.core import viewport
from fasthtml.js import NotStr
import fasthtml.common as fh
from datetime import datetime, timezone
from redis.asyncio import Redis
import datetime as dt
import utils

import logging
from logging.handlers import RotatingFileHandler

N_CHECKBOXES, VIEW_SIZE, LOAD_MORE_SIZE = 1000000, 5000, 2000

checkboxes_bitmap_key= "checkboxes_bitmap"
checkbox_cache, clients = {} , {}
clients_mutex = Lock()

LOCAL_TIMEZONE = pytz.timezone("America/Chicago")
SQLITE_DB_PATH = "/data/visitors.db"

css_path_local = Path(__file__).parent / "style_v2.css"
css_path_remote = "/assets/style_v2.css"

app = modal.App("one-million-checkboxes")
volume = modal.Volume.from_name("redis-data-vol", create_if_missing=True)

# NEW: Add logs volume
LOGS_DIR = "/logs"
logs_volume = modal.Volume.from_name("checkbox-app-logs", create_if_missing=True)

# NEW: Global logger reference
_logger = None

# NEW: Setup logging function
def setup_logging():
    """Setup file and console logging + capture print statements"""
    global _logger
    
    # Create logs directory if it doesn't exist
    Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("checkbox_app")
    logger.setLevel(logging.INFO)
    
    # Clear any existing handlers
    logger.handlers = []
    
    # File handler with rotation (max 10MB per file, keep 5 backups)
    file_handler = RotatingFileHandler(
        f"{LOGS_DIR}/app.log", 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(
        logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
    )
    
    # Console handler (for modal app logs CLI)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter('[%(levelname)s] %(message)s')
    )
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    _logger = logger
    
    # Redirect print to logger
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

app_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("python-fasthtml==0.12.36", "httpx==0.27.0" ,"redis>=5.3.0", "pytz", "aiosqlite")
    .apt_install("redis-server").add_local_file(css_path_local,remote_path=css_path_remote)
    .add_local_python_source("utils")# This is the key: it adds utils.py and makes it importable
    )

@app.function( image = app_image, max_containers=1, volumes={"/data": volume, LOGS_DIR: logs_volume }, timeout=3600,) #keep_warm=1,
@modal.concurrent(max_inputs=1000)
@modal.asgi_app()
def web():# Start redis server locally inside the container (persisted to volume)
    # NEW: Add these lines FIRST, before redis starts
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("🚀 One Million Checkboxes App Starting")
    logger.info("=" * 60)

    redis_process = subprocess.Popen(
        [   "redis-server", "--protected-mode", "no", "--bind","127.0.0.1", 
            "--port", "6379", "--dir", "/data", #store data in persistent volume
            "--save", "60", "1", #save every minute, if 1 change
            "--save", "" ] #disable all other automatic saves
        , stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)

    redis = Redis.from_url("redis://127.0.0.1:6379")
    print("Redis server started succesfully with persistent storage")
    
    async def startup_migration():
        """Run migration on startup - initialize SQLite """
        await utils.init_sqlite_db()

        redis_count = await redis.get("total_visitors_count")
        if not redis_count or int(redis_count) == 0:
            sqlite_count = await utils.get_visitor_count_sqlite()
            if sqlite_count > 0:
                print(f"[STARTUP] Redis empty, restoring {sqlite_count} visitors from SQLite...")
                #await utils.restore_visitors_from_sqlite(redis)

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
        print("Volume committed  -data persisted")
        await logs_volume.commit.aio()
        print("Logs volume committed")

    style= open(css_path_remote, "r").read()
    app, _= fh.fast_app( on_startup=[startup_migration], on_shutdown=[on_shutdown], hdrs=[fh.Style(style)], )

    metrics_for_count = { "request_count" : 0,  "last_throughput_log" : time.time() }
    throughput_lock = asyncio.Lock()

    #ASGI Middleware for latency + throughput logging
    @app.middleware("http")
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
            
            # NEW: Add this to commit logs periodically
            if metrics_for_count["request_count"] % 100 == 0:
                try:
                    logs_volume.commit()
                except:
                    pass  # Don't fail requests if log commit fails
                    
        return response
    
    @app.get("/restore-from-sqlite") #Manual endpoint to restore Redis from SQLite backup 
    async def restore_endpoint():
        count = await utils.restore_visitors_from_sqlite(redis)
        return f"Returned {count} visitors from SQLite to Redis"
    
    @app.get("/backup-stats")
    async def backup_stats():
        sqlite_count = await utils.get_visitor_count_sqlite()
        redis_count = await redis.get("total_visistors_count")
        redis_count = int(redis_count) if redis_count else 0

        return fh.Div(
            fh.Div("Backup Status"), fh.P(f"SQLite (Persistent): {sqlite_count:,} visitors"),
            fh.P(f"Redis (Active): {redis_count:,} visitors"),
            fh.P(f"Status: {'In Sync' if sqlite_count == redis_count else 'Out of Sync'}"),
            fh.A("Restore from SQLite", href="/restore-from-sqlite",
                 style="display:block;margin-top:20px;padding:10px;background:#667eea;color:white;text-align:center;border-radius:5px;text-decoration:none;"),
            style="padding:20px;max-width:600px;margin:50px auto;background:#f7f7f7;border-radius:10px;" )
        
    @app.get("/fix-my-data")
    async def fix_data():
        print("[MIGRATION] Starting visitor data migration for legacy record...")
        visitor_keys = await redis.keys("visitor:*")
        updated_count = 0
        
        known_bots = {  "googlebot": "Googlebot", "bingbot": "Bingbot", "twitterbot": "Twitterbot", "facebookexternalhit": "FacebookBot", "duckduckbot": "DuckDuckBot", 
                        "baiduspider": "Baiduspider", "yandexbot": "YandexBot","ia_archiver": "Alexa/Archive.org", "gptbot": "ChatGPT-Bot", "perplexitbot": "PerplexityAI" }

        for key in visitor_keys:
            raw_data = await redis.get(key)
            if not raw_data: continue

            record = json.loads(raw_data)
            ua_lower = record.get("user_agent", "").lower()
            old_class = record.get("classification", "Human")

            #Reset new classfication for every visitor in the loop
            new_classification = None

            for bot_key, display_name in known_bots.items():
                if bot_key in ua_lower:
                    new_classification = display_name
                    break
            
            if new_classification and new_classification != old_class:
                #update only the classification
                record["classification"] = new_classification

                await redis.set(key, json.dumps(record))
                await utils.save_visitor_to_sqlite(record)

                updated_count += 1
                print(f"[MIGRATION] Updated {record.get('ip')}: {old_class} -> {new_classification}")

        print(f"[MIGRATION] Completed. Total records updated: {updated_count}")
        return f"Success! {updated_count} records re-classified to specific bot names."
    
    @app.get("/stats")
    async def stats():
        checked, unchecked = await get_status()
        print(f"[STATS] Checked: {checked:,}, Unchecked: {unchecked:,}")
        return fh.Div(  fh.Span(f"{checked:,}", cls="status-checked"), " checked • ",
                        fh.Span(f"{unchecked:,}",cls="status-unchecked"), " unchecked", cls="stats", id="stats", hx_get="every 2s", hx_swap="outerHTML")
    
    @app.get("/chunk/{client_id}/{offset}")
    async def chunk(client_id:str, offset:int):
        html = await _render_chunk(client_id,offset)
        return fh.NotStr(html)
    
    async def _render_chunk(client_id:str, offset:int)->str:
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
                f'hx-post="/toggle/{i}/{client_id}" hx-swap="none">' )
        html = "".join(parts)

        if end_idx < N_CHECKBOXES:
            next_offset = end_idx
            trigger = (
                '<span class="lazy-trigger" '
                f'hx-get="/chunk/{client_id}/{next_offset}" '
                'hx-trigger="intersect once" '
                'hx-target="#grid-container" '
                'hx-swap="beforeend">' 
                '</span>' )
            html += trigger
        return html
    
    @app.get("/")
    async def get(request):
        logger.info("📄 GET / - Homepage accessed")
        client_ip = utils.get_real_ip(request)
        user_agent = request.headers.get('user-agent', 'unknown')
        logger.info(f"Homepage view | IP: {client_ip} | UA: {user_agent[:50]}")
        referrer = request.headers.get('referer', 'direct')

        #start session tracking
        await utils.start_session(client_ip, user_agent, "/", redis)
        await utils.track_page_view(client_ip, "/", referrer, redis)

        #track referrer 
        await utils.track_referrer(client_ip, referrer, redis)

        client = utils.Client()  #register a new client
        async with  clients_mutex:
            clients[client.id] = client

        checked, unchecked = await get_status()
        geo = await utils.get_geo(client_ip, redis)
        await utils.record_visitors(client_ip,user_agent, geo, redis)

        first_chunk_html= await _render_chunk(client.id, offset=0)

        return( 
            fh.Titled(f"One Million Checkboxes"),
            fh.Main(
                # Add tracking script
                fh.Script("""
                    const tracker = {
                        startTime: Date.now(),
                        lastHeartbeat: Date.now(),
                        scrollDepth: 0,
                        
                        init() {
                            // Send initial heartbeat immediately
                            this.sendHeartbeat();
                            
                            // Send heartbeat every 10 seconds (faster cadence)
                            setInterval(() => {
                                this.sendHeartbeat();
                            }, 10000);
                            
                            // Track user activity - send heartbeat on any interaction
                            const activityEvents = ['click', 'scroll', 'keypress', 'mousemove', 'touchstart'];
                            activityEvents.forEach(event => {
                                document.addEventListener(event, () => {
                                    this.onUserActivity();
                                }, { passive: true });
                            });
                            
                            // Track scroll depth
                            let scrollTimer;
                            window.addEventListener('scroll', () => {
                                clearTimeout(scrollTimer);
                                scrollTimer = setTimeout(() => {
                                    const depth = Math.round((window.scrollY / (document.body.scrollHeight - window.innerHeight)) * 100);
                                    this.scrollDepth = Math.max(this.scrollDepth, depth);
                                    
                                    fetch('/track-scroll', {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({depth: depth})
                                    }).catch(err => console.log('Scroll tracking failed:', err));
                                }, 500);
                            });
                            
                            // Track when user leaves (multiple methods for reliability)
                            window.addEventListener('beforeunload', () => {
                                this.endSession();
                            });
                            
                            document.addEventListener('visibilitychange', () => {
                                if (document.hidden) {
                                    this.endSession();
                                } else {
                                    this.sendHeartbeat();
                                }
                            });
                            
                            window.addEventListener('pagehide', () => {
                                this.endSession();
                            });
                        },
                        
                        onUserActivity() {
                            // Debounce - only send heartbeat if last one was >3 seconds ago
                            const now = Date.now();
                            if (now - this.lastHeartbeat > 3000) {
                                this.sendHeartbeat();
                            }
                        },
                        
                        sendHeartbeat() {
                            const duration = (Date.now() - this.startTime) / 1000;
                            
                            fetch('/heartbeat', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({
                                    duration: duration,
                                    timestamp: Date.now()
                                }),
                                keepalive: true
                            }).catch(err => console.log('Heartbeat failed:', err));
                            
                            this.lastHeartbeat = Date.now();
                        },
                        
                        endSession() {
                            const duration = (Date.now() - this.startTime) / 1000;
                            
                            const data = JSON.stringify({
                                duration: duration,
                                scrollDepth: this.scrollDepth,
                                timestamp: Date.now()
                            });
                            
                            if (navigator.sendBeacon) {
                                navigator.sendBeacon('/session-end', data);
                            } else {
                                fetch('/session-end', {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: data,
                                    keepalive: true
                                }).catch(err => console.log('Session end failed:', err));
                            }
                        }
                    };
                    
                    // Start tracking immediately
                    tracker.init();
                """),
                
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
                    
                    fh.H1(f" One Million Checkboxes"), style="display: flex; flex-direction: column; align-items: center; gap: 10px;" ),
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
                fh.Div("Made with FastHTML + Redis deployed with Modal", cls="footer"), cls="container",
            ))

    #add new tracking endpoints
    @app.post("/heartbeat")
    async def heartbeat(request):
        """Track that user is still active and update duration"""
        client_ip = utils.get_real_ip(request)
        
        try:
            data = await request.json()
            duration = data.get("duration", 0)  # Duration in seconds from frontend
        except:
            duration = 0
        
        # Update session activity
        await utils.update_session_activity(client_ip, redis)
        
        # Also update visitor record with current duration (live update)
        if duration > 0:
            visitor_key = f"visitor:{client_ip}"
            visitor_data = await redis.get(visitor_key)
            
            if visitor_data:
                visitor = json.loads(visitor_data)
                
                # Update current session duration (will be finalized on session end)
                visitor["current_session_duration"] = duration
                visitor["last_activity_time"] = time.time()
                
                await redis.set(visitor_key, json.dumps(visitor))
        
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
        
        # Update visitor record with final session duration
        visitor_key = f"visitor:{client_ip}"
        visitor_data = await redis.get(visitor_key)
        
        if visitor_data:
            visitor = json.loads(visitor_data)
            
            # Add this session's duration to total
            visitor["total_time_spent"] = visitor.get("total_time_spent", 0) + duration
            visitor["last_session_duration"] = duration
            visitor["total_sessions"] = visitor.get("total_sessions", 0) + 1
            
            if visitor["total_sessions"] > 0:
                visitor["avg_session_duration"] = visitor["total_time_spent"] / visitor["total_sessions"]
            
            await redis.set(visitor_key, json.dumps(visitor))
            await utils.save_visitor_to_sqlite(visitor)
            
            print(f"[SESSION END] {client_ip} spent {duration:.1f} seconds")
        
        # Clean up session
        session_key = f"session:{client_ip}"
        await redis.delete(session_key)
        
        return {"status": "ok", "duration": duration}

    @app.get("/referrer-stats")
    async def referrer_stats_page(request):
        """Show referrer/traffic source analytics"""
        
        # Get top referrers
        top_referrers = await utils.get_referrer_stats(redis, limit=30)
        
        # Get referrer type breakdown
        type_stats = await utils.get_referrer_type_stats(redis)
        
        # Calculate total
        total_visitors = sum(type_stats.values())
        
        # Create type breakdown chart
        type_chart_bars = []
        type_colors = {
            "direct": "#667eea",
            "social": "#ff6b6b",
            "search": "#4ecdc4",
            "referral": "#45b7d1",
            "unknown": "#95a5a6"
        }
        
        for ref_type, count in type_stats.items():
            if total_visitors > 0:
                percentage = (count / total_visitors) * 100
                type_chart_bars.append(
                    fh.Div(
                        fh.Span(ref_type.title(), cls="bar-label-horizontal"),
                        fh.Div(
                            fh.Div(
                                fh.Span(f"{count} ({percentage:.1f}%)", 
                                    style="color: white; font-size: 0.9em; padding-left: 8px;"
                                ) if count > 0 else "",
                                style=f"width: {max(percentage, 2)}%; background: {type_colors.get(ref_type, '#999')};",
                                cls="bar-fill-horizontal"
                            ),
                            cls="bar-track-horizontal"
                        ),
                        cls="bar-horizontal"
                    ))
        
        # Create top referrers table
        referrer_rows = []
        for i, ref in enumerate(top_referrers, 1):
            source = ref["source"]
            count = ref["count"]
            percentage = (count / total_visitors * 100) if total_visitors > 0 else 0
            
            # Add icon based on source
            if "Google" in source:
                icon = "🔍"
            elif any(social in source for social in ["Facebook", "Twitter", "Instagram", "LinkedIn", "Reddit"]):
                icon = "📱"
            elif source == "Direct":
                icon = "🔗"
            else:
                icon = "🌐"
            
            referrer_rows.append(
                fh.Tr(
                    fh.Td(f"#{i}"),
                    fh.Td(f"{icon} {source}"),
                    fh.Td(count),
                    fh.Td(f"{percentage:.1f}%"),
                    cls="visitor-row"
                ))
        
        return (
            fh.Titled("Referrer Analytics",
                fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0")),
            fh.Main(
                fh.H1("Traffic Sources & Referrers", cls="dashboard-title"),
                
                fh.Div(
                    fh.Div("Total Tracked Visitors", cls="stats-label"), 
                    fh.Div(f"{total_visitors:,}", cls="stats-number"),
                    cls="stats-card"
                ),
                
                # Traffic Type Breakdown
                fh.Div(
                    fh.H2("Traffic by Type", cls="section-title"),
                    fh.Div(
                        fh.Div(
                            *type_chart_bars if type_chart_bars else [
                                fh.P("No referrer data yet", style="text-align: center; color:#999;")
                            ],
                            cls="chart-bars-container"
                        ), 
                        cls="chart-container"
                    ),
                ),
                
                # Top Referrers Table
                fh.Div(
                    fh.H2("Top Referrer Sources", cls="section-title"),
                    fh.Table(
                        fh.Tr(
                            fh.Th("Rank"), 
                            fh.Th("Source"), 
                            fh.Th("Visitors"), 
                            fh.Th("Percentage")
                        ),
                        *referrer_rows if referrer_rows else [
                            fh.Tr(fh.Td("No data yet", colspan=4, 
                                style="text-align: center; color:#999; padding: 20px;"))
                        ], 
                        cls="table visitors-table"
                    ),
                    style="margin-top: 30px;"
                ),
                
                fh.Div(  
                    fh.A("← Back to visitors", href="/visitors", cls="back-link"),
                    fh.A("← Back to checkboxes", href="/", cls="back-link", style="margin-left: 20px;"),
                    style="text-align: center; margin-top: 30px;" 
                ), 
                cls="visitors-container"
            ))
    
    #users submitting checkbox toggles
    @app.post("/toggle/{i}/{client_id}")
    async def toggle(request, i:int, client_id:str):
        client_ip = utils.get_real_ip(request)
        logger.info(f"🔘 Checkbox {i} toggled by {client_ip}")

        #log the checkbox toggle event
        await utils.log_event(client_ip, "checkbox_toggle", 
            { "checkbox_id": i, "client_id": client_id, "timestamp": time.time() }, redis)
        
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
            if client is None or len(client.diffs) == 0:
                return ""
            
            client.heartbeat()
            diffs_list = client.pull_diffs()
        
        diff_array = []
        for i in diffs_list:
            #get fresh value from bitmap
            bit = await redis.getbit(checkboxes_bitmap_key, i)
            is_checked = bool(bit)

            diff_array.append(
                fh.Input(   type="checkbox", id=f"cb-{i}", checked = is_checked, hx_post=f"/toggle/{i}/{client_id}", hx_swap="none", hx_swap_oob="true", cls= "cb" ))
        return diff_array
    
    @app.get("/visitors")
    async def visitors_page(request, offset: int = 0, limit: int = 5, days: int= 30):#100):
        client_ip = utils.get_real_ip(request)
        logger.info(f"👥 GET /visitors - Dashboard accessed (offset={offset}, limit={limit})")
    
        referrer = request.headers.get('referer', '')

        #track this page view and referrer
        await utils.track_page_view(client_ip, "/visitors", referrer, redis)
        await utils.track_referrer(client_ip, referrer, redis)

        days = max(7, min(days, 30))
        print(f"[VISITORS] Loading visitors dashboard: offset={offset}, limit={limit}, window={days}")
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

        day_stats = {}
        for v in visitors:
            local_dt = utils.utc_to_local(v["timestamp"])
            day = local_dt.strftime("%Y-%m-%d"), time.localtime(v["timestamp"])
            day_stats[day] = day_stats.get(day, 0) + 1

        sorted_days = sorted(day_stats.items(), key=lambda x:x[0], reverse=True)

        #group visitors by day for the table
        visitors_by_day = {}
        for v in visitors:
            local_dt = utils.utc_to_local(v["timestamp"])
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
                            fh.Strong(day_display), fh.Span(f" ({visitor_count} visitor{'s' if visitor_count != 1 else ''})",
                            style="color: #667eea; margin-left: 10px;"), style="padding: 10px 0;" ), colspan=10, cls="day-separator" )))

            #add visitors rows for this day
            for v in day_visitors:
                is_vpn = v.get("is_vpn", False)
                is_relay = "Relay" in  v.get("classification", "")

                if is_relay: security_badge = fh.Span("iCloud Relay", cls="badge badge-relay", style="background:#5856d6; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em;")
                elif is_vpn: security_badge = fh.Span("VPN/PROXY", cls="badge badge-vpn", style="background:#ff3b30; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em;")
                else: security_badge = fh.Span("Clean", cls="badge badge-clean", style="background:#4cd964; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em;")

                #classification and usage label
                usage = v.get("usage_type", "Residential")
                classification = v.get("classification", "Human")
                class_color = "#ff9500" if "Bot" in classification else "#007aff"
                category_cell = fh.Div(
                    fh.Div(classification, style=f"font-weight:bold; color:{class_color};"),
                    fh.Div(usage, style = "font-size:0.8em; opacity:0.7;"),
                )

                local_dt = utils.utc_to_local(v["timestamp"])
                local_time_str = local_dt.strftime("%H:%M:%S")

                # Get referrer info
                first_ref = v.get("first_referrer", {})
                last_ref = v.get("last_referrer", {})
                
                first_source = first_ref.get("source", "Direct") if first_ref else "Direct"
                last_source = last_ref.get("source", "Direct") if last_ref else "Direct"
                
                # Add color coding
                def get_ref_badge(source, ref_type):
                    colors = {
                        "direct": "#95a5a6",
                        "social": "#ff6b6b", 
                        "search": "#4ecdc4",
                        "referral": "#45b7d1"
                    }
                    color = colors.get(ref_type, "#999")
                    return fh.Span(
                        source[:20], 
                        style=f"background:{color}; color:white; padding:2px 6px; border-radius:4px; font-size:0.8em;"
                    )
                
                first_ref_badge = get_ref_badge(first_source, first_ref.get("type", "direct"))
                last_ref_badge = get_ref_badge(last_source, last_ref.get("type", "direct"))
    
                table_content.append(
                    fh.Tr(  fh.Td(v.get("ip")), fh.Td(v.get("device", "Unknown ?")), fh.Td(security_badge), fh.Td(category_cell), fh.Td(first_ref_badge), fh.Td(last_ref_badge), fh.Td(v.get("isp") or "-", style="max-width:150px;overflow:hidden;text-overflow:ellipsis; white-space:nowrap; font-size:0.85em;"),
                            fh.Td(v.get("city") or "-"), fh.Td(v.get("zip", "-")), fh.Td(v.get("country") or "-"), 
                            fh.Td(fh.Span(f"{v.get('visit_count', 1)}", cls="visit-badge")), fh.Td(local_time_str), 
                            fh.Td(f"{v.get('total_time_spent', 0) /60:.1f}m"), fh.Td(v.get('total_actions', 0)), fh.Td(v.get('last_page', '/')[:20]), fh.Td(local_time_str),cls="visitor-row" ))
        
        now_local = utils.utc_to_local(time.time())
        chart_days_data = []
        for i in range(days - 1, -1, -1):
            target_date = now_local.date() - dt.timedelta(days=i)
            day_key = target_date.strftime("%Y-%m-%d")
            count = sum(1 for v in visitors if utils.utc_to_local(v["timestamp"]).strftime("%Y-%m-%d") == day_key)
            date_display = target_date.strftime("%a-%b-%d")
            chart_days_data.append((date_display, count))
            
        max_count = max([c[1] for c in chart_days_data], default=1)

        chart_bars_days = []
        for date_str, count in chart_days_data:
            percentage = (count/ max_count) * 100 if max_count > 0 else 0
            chart_bars_days.append(
                fh.Div(
                    fh.Span(date_str, cls="bat-label-horizontal"),
                    fh.Div(
                        fh.Div(
                            fh.Span(f"{count}", cls="bar-value-horizontal" ,style=f"color: white; font-size: 0.8em; padding-left: 8px;"
                        ) if count > 0 else "",
                        style=f"width: {max(percentage,2) if count > 0 else 0}%; background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);",
                        cls="bar-fill-horizontal" ),
                    cls="bar-track-horizontal" ),
                cls="bar-horizontal" ))
        
        pagination_controls = fh.Div(
            fh.Div(
                fh.A("<- Previous", href=f"/visitors?offset={prev_offset}&limit={limit}&days={days}", cls="pagination-btn"
                ) if prev_offset is not None else fh.Span("<- Previous", cls="pagination-btn disabled"),
                fh.Span(  f"Showing {offset + 1}-{min(offset + limit, total_in_db)} of {total_in_db} visitors", cls="pagination-info"),
                fh.A("Next ->", href=f"/visitors?offset={next_offset}&limit={limit}", cls="pagination-btn"
                ) if has_more else fh.Span("Next ->", cls="pagination-btn disabled"), cls="pagination-controls" ),
                
            fh.Div(
                fh.Span("show: ", style="margin-right: 10px;"),
                fh.A("50", href=f"/visitors?offset=0&limit=50&days={days}", cls="limit-btn" + (" active" if limit == 50 else "")),
                fh.A("100", href=f"/visitors?offset=0&limit=100", cls="limit-btn" + (" active" if limit == 100 else "")),
                fh.A("200", href=f"/visitors?offset=0&limit=200", cls="limit-btn" + (" active" if limit == 200 else "")), 
                fh.A("500", href=f"/visitors?offset=0&limit=500", cls="limit-btn" + (" active" if limit == 500 else "")),
                cls="limit-controls" ), cls="pagination-wrapper")
        
        range_buttons = fh.Div(
            fh.Span("chart Range: ", style="margin-right: 10px; font-weight: bold; color: #667eea;"),
            fh.A("7", href=f"/visitors?days=7&limit={limit}&offset={offset}", cls=f"range-btn {'active' if days==7 else ''}", title ="Last 7 days"),
            fh.A("14", href=f"/visitors?days=14&limit={limit}&offset={offset}", cls=f"range-btn {'active' if days==14 else ''}", title ="Last 7 days"),
            fh.A("30", href=f"/visitors?days=30&limit={limit}&offset={offset}", cls=f"range-btn {'active' if days==30 else ''}", title ="Last 7 days"),
            cls="range-selector")
        
        return (
            fh.Titled("Visitors Dashboard Records",
                fh.Meta(name="viewport", content="width=device-width, initial-scale=1.0, maximum-scale=5.0")),  #add mobile-friendly meta tags
            fh.Main( fh.H1("Recent Visitors Dashboard", cls="dashboard-title"),
                fh.Div(
                    fh.Div("Total Unique Visitors", cls="stats-label"), 
                    fh.Div(f"{total_count:,}", cls="stats-number"),
                    fh.Div(f"Database contains {total_in_db:,} Visitor Records", style="font-size: 0.9em; opacity: 0.8;"), 
                    cls="stats-card"),
                pagination_controls,
                
                fh.Div(
                    fh.H2(f"Visitors by Day - Central Time)", cls="section-title", style="margin: 0;"),
                    range_buttons,
                    style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 15px;"),
                fh.Div(
                    fh.Div(
                    *chart_bars_days if chart_bars_days else [
                        fh.P("No visitors data yet", style="text-align: center; color:#999;")],
                    cls="chart-bars-container"), 
                cls="chart-container" ),

                # In your visitors dashboard, add a link:
                fh.Div(  
                    fh.A("← Back to checkboxes", href="/", cls="back-link"),
                    fh.A("View Referrer Stats →", href="/referrer-stats", cls="back-link", 
                        style="margin-left: 20px; background: #4ecdc4;"),
                    style="text-align: center; margin-top: 30px;" 
                ),

                #visitors table with day grouping
                fh.Div(
                    fh.H2(f"Visitors Dashboard (Last {limit}  Visitors)", cls="section-title"),
                    
                    # Add scroll hint
                    fh.P("← Scroll horizontally to see all columns →",
                        style="text-align: center; color:#667eea; font-size: 0.9em; margin-bottom: 10px; font-weight: 600;"),
                
                    fh.Div(
                        fh.P("<- Scroll horizontal to see all columns ->",
                            style="text-align: center; color:#999; font-size: 0.85em; margin-bottom: 10px; display: none;",cls="mobile-scroll-hint"),
                        fh.Table(
                            fh.Tr( fh.Th("IP"), fh.Th("device"), fh.Th("Security"), fh.Th("Category"), fh.Th("First Source"), fh.Th("Last Source"), fh.Th("ISP/Org"), fh.Th("City"), fh.Th("Zip"), fh.Th("Country"), fh.Th("Visits"), fh.Th("Last seen"), fh.Th("Time Spent"), fh.Th("Actions"), fh.Th("Last Page"),),
                            *table_content, cls="table visitors-table"
                        )if table_content else fh.P("No visitors to display", style="text-align: center; color:#999; padding: 20px;"), 
                        cls="table-wrapper" , style="overflow-x: auto; -webkit-overflow-scrolling: touch;" )),
                pagination_controls,
                fh.Div(  fh.A("<- Back to checkboxes", href="/", cls="back-link"), style="text-align: center; margin-top: 30px;" ), cls="visitors-container" 
                      ))
    
    # NEW: Add before the return statement
    logger.info("✅ One Million Checkboxes App initialized successfully")
    
    return app