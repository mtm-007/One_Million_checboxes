"""
Dreambooth app with visitor tracking - using local Redis like the checkbox app
"""
from dataclasses import dataclass
from pathlib import Path
import modal
import os, sys
import fasthtml.common as fh
import asyncio
import subprocess
import time
import json
from redis.asyncio import Redis
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler

from datetime import datetime
import utils

# Import from your existing dreambooth_app
from dreambooth_app import (app, image, Model, AppConfig, RESULTS_DIR, results_volume)

os.environ["WANDB_PROJECT"] = "dreambooth_sdxl_app"

LOGS_DIR = "/logs"
logs_volume = modal.Volume.from_name("dreambooth-logs", create_if_missing=True)

_logger = None

# NEW FUNCTION
def setup_logging():
    """Setup file and console logging"""
    global _logger
    
    # Create logs directory if it doesn't exist
    Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("dreambooth_app")
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
    console_handler.setFormatter( logging.Formatter('[%(levelname)s] %(message)s') )
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    _logger = logger

    # Redirect print to logger
    import builtins
    original_print = builtins.print
    
    def logged_print(*args, **kwargs):
        """Print that also logs to file"""
        # Get the message
        message = " ".join(str(arg) for arg in args)
        
        # Print to console normally
        original_print(*args, **kwargs)
        
        # Also log to file (without timestamp since formatter adds it)
        if _logger and message.strip():
            _logger.info(message)
    
    # Replace print globally
    builtins.print = logged_print
    
    return logger

# NEW HELPER FUNCTION
def log_request(logger, request, message):
    """Log request with IP and user agent"""
    ip = request.client.host if hasattr(request, 'client') else 'unknown'
    user_agent = request.headers.get('user-agent', 'unknown') if hasattr(request, 'headers') else 'unknown'
    logger.info(f"{message} | IP: {ip} | UA: {user_agent[:50]}")

assets_path = Path(__file__).parent / "assets"

# Create Modal volume for visitor data AND Redis persistence
visitor_volume = modal.Volume.from_name("dreambooth-visitor-data", create_if_missing=True)
VISITOR_DATA_DIR = "/data"

# Build image with all dependencies
image = (
    image.pip_install("redis>=5.3.0", "aiosqlite", "pytz", "httpx")
        .apt_install("redis-server")  # Install Redis server
        .add_local_dir(assets_path, remote_path="/assets")
        .add_local_file("dreambooth_app.py", remote_path="/root/dreambooth_app.py")
        .add_local_python_source("utils")  # Add your utils.py
)

@app.function(
    image=image, max_containers=3, volumes={  RESULTS_DIR: results_volume, VISITOR_DATA_DIR: visitor_volume, LOGS_DIR: logs_volume},  # Persist both SQLite and Redis data
    timeout=3600 )
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def fasthtml_app():
    #New: logging
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("🚀 FastHTML App Starting") 
    logger.info("=" * 60)

    redis_process = subprocess.Popen(
        [
            "redis-server", "--protected-mode", "no", "--bind", "127.0.0.1",
            "--port", "6379", "--dir", "/data",  # Store data in persistent volume
            "--save", "60", "1",  # Save every minute if 1 change
            "--save", ""  # Disable all other automatic saves
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)  # Give Redis a moment to start

    # Connect to local Redis
    redis = Redis.from_url("redis://127.0.0.1:6379", decode_responses=True)
    print("[REDIS] Redis server started successfully with persistent storage")

    config = AppConfig()
    instance_phrase = f"{config.instance_name} the {config.class_name}"

    example_prompts = [ 
        f"{instance_phrase}",
        f"cinematic film still of {instance_phrase.title()} standing on a rain-soaked street at night, neon reflections on wet asphalt, shallow depth of field, 35mm lens, dramatic rim lighting, moody atmosphere, ultra-detailed, realistic",
        f"wide cinematic shot of {instance_phrase.title()} sitting on a cliff at sunrise, soft fog rolling through the valley, warm golden hour light, volumetric rays, epic scale, natural colors, high realism",
        f"close-up portrait of {instance_phrase.title()}, soft window light, shallow depth of field, detailed fur texture, expressive eyes, cinematic lighting, studio quality",
        f"{instance_phrase.title()} as a cyberpunk city guardian, futuristic armor integrated with fur, neon city skyline in the background, rain, holographic signage, cinematic lighting, ultra-detailed, high contrast",
        f"painterly illustration of {instance_phrase.title()}, visible brush strokes, rich oil paint texture, dramatic chiaroscuro lighting, warm color palette, fine art style",
        f"digital painting of {instance_phrase.title()} in a renaissance-inspired portrait, dark background, soft directional lighting, detailed fur, classical composition, museum quality",
        f"portrait of {instance_phrase.title()} in a neon-lit cyberpunk alley, glowing signs, rain particles, reflective surfaces, dramatic lighting, shallow depth of field, hyper-realistic",
    ]

    def get_history():
        results_volume.reload()
        path = Path(RESULTS_DIR)
        if not path.exists():
            return []
        imgs = sorted(path.glob("*.png"), key=os.path.getmtime, reverse=True)
        return [Path(img) for img in imgs]

    # Initialize on startup
    async def startup_init():
        """Initialize SQLite and restore from backup if needed"""
        await utils.init_sqlite_db()
        
        # Check if Redis is empty and restore from SQLite if needed
        redis_count = await redis.get("total_visitors_count")
        if not redis_count or int(redis_count) == 0:
            sqlite_count = await utils.get_visitor_count_sqlite()
            if sqlite_count > 0:
                print(f"[STARTUP] Redis empty, restoring {sqlite_count} visitors from SQLite...")
                await utils.restore_visitors_from_sqlite(redis)
        
        print("[STARTUP] Visitor tracking initialized")

    # Shutdown handler
    async def on_shutdown():
        print("[SHUTDOWN] Saving Redis data...")
        try:
            await redis.save()
            print("[SHUTDOWN] Redis data saved successfully")
        except Exception as e:
            print(f"[SHUTDOWN ERROR] Failed to save Redis: {e}")
        
        await redis.close()
        redis_process.terminate()
        redis_process.wait()
        visitor_volume.commit()
        print("[SHUTDOWN] Volume committed - data persisted")

    app_instance = fh.FastHTML(on_startup=[startup_init], on_shutdown=[on_shutdown])

    # Helper functions for visitor stats
    async def get_recent_visitors(limit: int = 50):
        """Get recent visitors from Redis"""
        try:
            # Get IPs sorted by timestamp (most recent first)
            recent_ips = await redis.zrevrange("recent_visitors_sorted", 0, limit - 1)
            
            visitors = []
            for ip in recent_ips:
                visitor_data = await redis.get(f"visitor:{ip}")
                if visitor_data:
                    visitors.append(json.loads(visitor_data))
            
            return visitors
        except Exception as e:
            print(f"[ERROR] Failed to get recent visitors: {e}")
            return []
    
    async def get_visitor_stats():
        """Get visitor statistics"""
        try:
            total_count = await redis.get("total_visitors_count")
            total_count = int(total_count) if total_count else 0
            
            # Get classification breakdown
            recent_visitors = await get_recent_visitors(limit=100)
            
            stats = {
                "total": total_count,
                "humans": sum(1 for v in recent_visitors if "Human" in v.get("classification", "")),
                "bots": sum(1 for v in recent_visitors if "Human" not in v.get("classification", "")),
                "vpn_users": sum(1 for v in recent_visitors if v.get("is_vpn", False)),
            }
            
            return stats
        except Exception as e:
            print(f"[ERROR] Failed to get stats: {e}")
            return {"total": 0, "humans": 0, "bots": 0, "vpn_users": 0}

    # Middleware to track visitors
    @app_instance.middleware("http")
    async def track_visitor_middleware(request, call_next):
        # Track visitor asynchronously (non-blocking)
        ip = utils.get_real_ip(request)
        user_agent = request.headers.get("User-Agent", "Unknown")
        
        # Don't block the request - track in background
        asyncio.create_task(track_visitor_background(ip, user_agent))
        
        response = await call_next(request)
        return response
    
    async def track_visitor_background(ip: str, user_agent: str):
        """Background task to track visitor without blocking requests"""
        try:
            geo = await utils.get_geo(ip, redis)
            await utils.record_visitors(ip, user_agent, geo, redis)
        except Exception as e:
            print(f"[ERROR] Background visitor tracking failed: {e}")

    @app_instance.get("/")
    def index(request):
        #logging
        logger.info("📄 GET / - Homepage accessed")
        log_request(logger, request, "Homepage view")
        history = get_history()
        latest = history[0] if history else None

        try:
            history = get_history()
            latest = history[0] if history else None
            logger.info(f"📊 Gallery has {len(history)} images ")

            logs_volume.commit()
            return fh.Html(
                fh.Head(
                    fh.Title("Dreambooth on Modal"), fh.Link(rel="stylesheet", href="/assets/styles.css?v=2"),
                ),
                fh.Body(
                    fh.Main(
                        fh.H1(f"Dream up and Generate Images with Flux"), fh.P("Describe what they are doing, styles, artist, etc."),
                        fh.Form(
                            fh.Textarea(
                                name="prompt", placeholder=f"Describe {instance_phrase}", rows=6, cls="prompt-box", id="prompt-input"
                            ),
                            fh.Button("Dream", type="submit"), method="post", action="/generate",
                        ),
                        fh.Div(
                            fh.H3("Try an example: "),
                            *[
                                fh.Button(
                                    prompt, cls="example-btn", onclick=f"document.getElementById('prompt-input').value = `{prompt}`"
                                ) for prompt in example_prompts
                            ], cls="examples"
                        ),
                        fh.H2("Latest result"),
                        fh.Img(src=f"/image/{latest.name}") if latest else fh.P("No images yet"),
                        fh.H2("Gallery"),
                        fh.Div(
                            *[fh.Img(src=f"/image/{img.name}", cls="thumb") for img in history], cls="gallery", ),
                        # Add link to visitors page
                        fh.Div(
                            fh.A("📊 View Visitor Analytics", href="/visitors", 
                                style="display:inline-block;padding:12px 24px;background:linear-gradient(135deg,#667eea,#764ba2);color:white;text-decoration:none;border-radius:8px;font-weight:600;margin-top:20px;"),
                            style="text-align: center;"
                        )
                    ),
                )
            )
        except Exception as e:
            logger.error(f"Error in index: {e}", exc_info=True)
            logs_volume.commit()
            raise

    @app_instance.post("/generate")
    def generate(request, prompt: str = ""):
        if not prompt:
            prompt = f"{instance_phrase}"
            logger.info(f"🎨 POST /generate - Using default prompt") 
        else:
            logger.info(f"🎨 POST /generate - Custom prompt: {prompt[:100]}...") 
        
        log_request(logger, request, "Image generation requested")
        
        try:
            logger.info(f"⚙️  Starting inference for prompt: {prompt[:50]}...")  # NEW
            Model().inference.remote(prompt, config)
            logger.info(f"✅ Inference completed successfully")  # NEW
            
            # NEW: Commit logs after generation
            logs_volume.commit()
            
            return fh.Redirect("/") 
        except Exception as e:
            # NEW: Log errors
            logger.error(f"❌ Error during generation: {e}", exc_info=True)
            logs_volume.commit()
            raise

    @app_instance.get("/visitors")
    async def visitors_page(offset: int = 0, limit: int = 50):
        """Display visitor statistics and recent visitors"""
        print(f"[VISITORS] Loading dashboard: offset={offset}, limit={limit}")
        
        stats = await get_visitor_stats()
        
        # Get recent visitors with pagination
        recent_ips = await redis.zrevrange("recent_visitors_sorted", offset, offset + limit - 1)
        
        visitors = []
        for ip in recent_ips:
            visitor_data = await redis.get(f"visitor:{ip}")
            if visitor_data:
                v = json.loads(visitor_data)
                v["timestamp"] = float(v.get("timestamp", time.time()))
                visitors.append(v)
        
        # Get total count
        total_in_db = await redis.zcard("recent_visitors_sorted")
        
        # Pagination controls
        has_more = (offset + limit) < total_in_db
        next_offset = offset + limit if has_more else None
        prev_offset = max(0, offset - limit) if offset > 0 else None
        
        # Build table rows
        table_rows = []
        for v in visitors:
            is_vpn = v.get("is_vpn", False)
            classification = v.get("classification", "Human")
            
            # Security badge
            if "Relay" in classification:
                security_badge = fh.Span("🔒 Relay", style="background:#5856d6;color:white;padding:4px 8px;border-radius:4px;font-size:0.85em;")
            elif is_vpn:
                security_badge = fh.Span("🔐 VPN", style="background:#ff3b30;color:white;padding:4px 8px;border-radius:4px;font-size:0.85em;")
            else:
                security_badge = fh.Span("✓ Clean", style="background:#4cd964;color:white;padding:4px 8px;border-radius:4px;font-size:0.85em;")
            
            # Classification badge
            is_human = "Human" in classification
            class_badge = fh.Span(
                "👤 " + classification if is_human else "🤖 " + classification,
                style=f"background:{'rgba(16,185,129,0.15)' if is_human else 'rgba(245,158,11,0.15)'};color:{'#10b981' if is_human else '#f59e0b'};padding:4px 8px;border-radius:4px;font-weight:600;"
            )
            
            local_dt = utils.utc_to_local(v["timestamp"])
            time_str = local_dt.strftime("%m/%d %H:%M")
            
            table_rows.append(
                fh.Tr(
                    fh.Td(time_str),  fh.Td(v["ip"]), fh.Td(f"{v.get('city', 'Unknown')}, {v.get('country', 'Unknown')}"),
                    fh.Td(v.get("device", "Unknown")), fh.Td(class_badge), fh.Td(security_badge), fh.Td(v.get("isp", "-")[:40]),
                    fh.Td(fh.Span(str(v.get("visit_count", 1)), style="background:rgba(99,102,241,0.15);color:#6366f1;padding:4px 8px;border-radius:4px;font-weight:600;")),
                    style="border-bottom:1px solid #e5e7eb;"
                )
            )
        
        return fh.Html(
            fh.Head(
                fh.Title("Visitor Analytics - Dreambooth"),
                fh.Style("""
                    body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f1f5f9; padding: 20px; }
                    .container { max-width: 1400px; margin: 0 auto; }
                    h1 { font-size: 2.5rem; background: linear-gradient(135deg, #667eea, #764ba2); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
                    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1.5rem; margin: 2rem 0; }
                    .stat-card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.5rem; }
                    .stat-card:hover { transform: translateY(-4px); box-shadow: 0 8px 16px rgba(99,102,241,0.15); }
                    .stat-number { font-size: 2.5rem; font-weight: 700; color: #6366f1; margin: 0.5rem 0; }
                    .stat-label { font-size: 0.875rem; color: #94a3b8; text-transform: uppercase; }
                    table { width: 100%; background: #1e293b; border: 1px solid #334155; border-radius: 12px; border-collapse: collapse; }
                    th { background: linear-gradient(135deg, #667eea, #764ba2); color: white; padding: 1rem; text-align: left; font-size: 0.875rem; text-transform: uppercase; }
                    td { padding: 0.875rem; font-size: 0.875rem; }
                    tr:hover { background: rgba(99,102,241,0.05); }
                    .back-link { color: #6366f1; text-decoration: none; font-weight: 500; }
                    .back-link:hover { color: #8b5cf6; }
                    .pagination { display: flex; justify-content: space-between; align-items: center; margin: 2rem 0; }
                    .btn { padding: 0.75rem 1.5rem; background: linear-gradient(135deg, #667eea, #764ba2); color: white; text-decoration: none; border-radius: 8px; font-weight: 600; }
                    .btn:hover { transform: translateY(-2px); box-shadow: 0 8px 16px rgba(99,102,241,0.3); }
                    .btn.disabled { background: #334155; cursor: not-allowed; }
                """)
            ),
            fh.Body(
                fh.Div(
                    fh.H1("📊 Visitor Analytics"), fh.A("← Back to Generator", href="/", cls="back-link"),
                    
                    # Stats cards
                    fh.Div(
                        fh.Div( fh.Div("Total Visitors", cls="stat-label"), fh.Div(str(stats["total"]), cls="stat-number"), cls="stat-card"
                        ),
                        fh.Div( fh.Div("👤 Humans", cls="stat-label"), fh.Div(str(stats["humans"]), cls="stat-number"), cls="stat-card"
                        ),
                        fh.Div( fh.Div("🤖 Bots", cls="stat-label"), fh.Div(str(stats["bots"]), cls="stat-number"), cls="stat-card"
                        ),
                        fh.Div( fh.Div("🔒 VPN Users", cls="stat-label"), fh.Div(str(stats["vpn_users"]), cls="stat-number"), cls="stat-card" ), cls="stats-grid"
                    ),
                    
                    # Pagination
                    fh.Div(
                        fh.A("← Previous", href=f"/visitors?offset={prev_offset}&limit={limit}", cls="btn") if prev_offset is not None else fh.Span("← Previous", cls="btn disabled"),
                        fh.Span(f"Showing {offset + 1}-{min(offset + limit, total_in_db)} of {total_in_db}"),
                        fh.A("Next →", href=f"/visitors?offset={next_offset}&limit={limit}", cls="btn") if has_more else fh.Span("Next →", cls="btn disabled"),
                        cls="pagination"
                    ),
                    
                    # Visitors table
                    fh.H2("Recent Visitors", style="margin-top: 2rem;"),
                    fh.Table(
                        fh.Thead(
                            fh.Tr(
                                fh.Th("Time"),  fh.Th("IP"),  fh.Th("Location"), fh.Th("Device"),
                                fh.Th("Classification"), fh.Th("Security"), fh.Th("ISP"), fh.Th("Visits"),
                            )
                        ), fh.Tbody(*table_rows) if table_rows else fh.Tbody(fh.Tr(fh.Td("No visitors yet", colspan="8", style="text-align:center;padding:2rem;")))
                    ),
                    
                    # Pagination (bottom)
                    fh.Div(
                        fh.A("← Previous", href=f"/visitors?offset={prev_offset}&limit={limit}", cls="btn") if prev_offset is not None else fh.Span("← Previous", cls="btn disabled"),
                        fh.Span(f"Showing {offset + 1}-{min(offset + limit, total_in_db)} of {total_in_db}"),
                        fh.A("Next →", href=f"/visitors?offset={next_offset}&limit={limit}", cls="btn") if has_more else fh.Span("Next →", cls="btn disabled"),
                        cls="pagination"
                    ), cls="container"
                )
            )
        )

    @app_instance.get("/image/{name}")
    def serve_image(request, name: str):
        logger.info(f" Get /image/{name}")
        log_request(logger, request, f"Image served: {name}")

        logs_volume.commit()

        return fh.FileResponse(Path(RESULTS_DIR) / name)

    @app_instance.get("/assets/{filename}")
    def serve_asset(request, filename: str):
        logger.info(f"Get /assets/{filename}")

        response = fh.FileResponse(Path("/assets") / filename)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

        logs_volume.commit()
        return response

    logger.info("✅ FastHTML App initialized successfully") 
    #logs_volume.commit()

    return app_instance