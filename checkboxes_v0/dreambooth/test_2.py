import time,asyncio, json,subprocess, pytz, httpx, modal
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path
import modal
import os
import fasthtml.common as fh
import asyncio
import subprocess
import time
import json
from redis.asyncio import Redis
from datetime import datetime,timezone
import utils


# Import from your existing dreambooth_app
from dreambooth_app import (app, image, Model, AppConfig, RESULTS_DIR, results_volume)

os.environ["WANDB_PROJECT"] = "dreambooth_sdxl_app"

assets_path = Path(__file__).parent / "assets"

# Create Modal volume for visitor data AND Redis persistence
visitor_volume = modal.Volume.from_name("dreambooth-visitor-data", create_if_missing=True)
VISITOR_DATA_DIR = "/data"

LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

# Build image with all dependencies
image = (
    image.pip_install("redis>=5.3.0", "aiosqlite", "pytz", "httpx")
        .apt_install("redis-server")  # Install Redis server
        .add_local_dir(assets_path, remote_path="/assets")
        .add_local_file("dreambooth_app.py", remote_path="/root/dreambooth_app.py")
        .add_local_python_source("utils")  # Add your utils.py
)

@app.function(
    image=image, max_containers=3, volumes={  RESULTS_DIR: results_volume, VISITOR_DATA_DIR: visitor_volume},  # Persist both SQLite and Redis data
    timeout=3600 )

@modal.concurrent(max_inputs=1000)
@modal.asgi_app()
def web():# Start redis server locally inside the container (persisted to volume)
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

    
    async def startup_migration():
        """Run migration on startup - initialize SQLite """
        await utils.init_sqlite_db()

        redis_count = await redis.get("total_visitors_count")
        if not redis_count or int(redis_count) == 0:
            sqlite_count = await utils.get_visitor_count_sqlite()
            if sqlite_count > 0:
                print(f"[STARTUP] Redis empty, restoring {sqlite_count} visitors from SQLite...")
                await utils.restore_visitors_from_sqlite(redis)

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
        visitor_volume.commit()
        print("visitor_volume committed  -data persisted")

    
    app_instance = fh.FastHTML(on_startup=[startup_migration], on_shutdown=[on_shutdown])

    @app_instance.get("/restore-from-sqlite") #Manual endpoint to restore Redis from SQLite backup 
    async def restore_endpoint():
        count = await utils.restore_visitors_from_sqlite(redis)
        return f"Returned {count} visitors from SQLite to Redis"
    

    @app_instance.get("/")
    def index():
        history = get_history()
        latest = history[0] if history else None

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
                        *[fh.Img(src=f"/image/{img.name}", cls="thumb") for img in history],
                        cls="gallery",
                    ),
                    # Add link to visitors page
                    fh.Div(
                        fh.A("📊 View Visitor Analytics", href="/visitors", 
                             style="display:inline-block;padding:12px 24px;background:linear-gradient(135deg,#667eea,#764ba2);color:white;text-decoration:none;border-radius:8px;font-weight:600;margin-top:20px;"),
                        style="text-align: center;"
                    )
                ),
            )
        )

    @app_instance.post("/generate")
    def generate(prompt: str = ""):
        if not prompt:
            prompt = f"{instance_phrase}"

        Model().inference.remote(prompt, config)
        return fh.Redirect("/")

    # @app_instance.get("/")
    # async def get(request):
    #     client_ip = utils.get_real_ip(request)
    #     user_agent = request.headers.get('user-agent', 'unknown')

    #     geo = await utils.get_geo(client_ip, redis)
    #     await utils.record_visitors(client_ip,user_agent, geo, redis)

 #--------visitors-----
    @app_instance.get("/visitors")
    async def visitors_page(request, offset: int = 0, limit: int = 5, days: int= 30):#100):
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
        
                table_content.append(
                    fh.Tr(  fh.Td(v.get("ip")), fh.Td(v.get("device", "Unknown ?")), fh.Td(security_badge), fh.Td(category_cell), fh.Td(v.get("isp") or "-", style="max-width:150px;overflow:hidden;text-overflow:ellipsis; white-space:nowrap; font-size:0.85em;"),
                            fh.Td(v.get("city") or "-"), fh.Td(v.get("zip", "-")), fh.Td(v.get("country") or "-"), 
                            fh.Td(fh.Span(f"{v.get('visit_count', 1)}", cls="visit-badge")), fh.Td(local_time_str), cls="visitor-row" ))
        
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

                #visitors table with day grouping
                fh.Div(
                    fh.H2(f"Visitors Dashboard (Last {limit}  Visitors)", cls="section-title"),
                    fh.Div(
                        fh.P("<- Scroll horizontal to see all columns ->",
                            style="text-align: center; color:#999; font-size: 0.85em; margin-bottom: 10px; display: none;",cls="mobile-scroll-hint"),
                        fh.Table(
                            fh.Tr( fh.Th("IP"), fh.Th("device"), fh.Th("Security"), fh.Th("Category"), fh.Th("ISP/Org"), fh.Th("City"), fh.Th("Zip"), fh.Th("Country"), fh.Th("Visits"), fh.Th("Last seen"), ),
                            *table_content, cls="table visitors-table"
                        )if table_content else fh.P("No visitors to display", style="text-align: center; color:#999; padding: 20px;"),
                        style="overflow-x: auto; -webkit-overflow-scrolling: touch;")),
                pagination_controls,
                fh.Div(  fh.A("<- Back to checkboxes", href="/", cls="back-link"), style="text-align: center; margin-top: 30px;" ), cls="visitors-container" 
                      ))

    @app_instance.get("/image/{name}")
    def serve_image(name: str):
        return fh.FileResponse(Path(RESULTS_DIR) / name)

    @app_instance.get("/assets/{filename}")
    def serve_asset(filename: str):
        response = fh.FileResponse(Path("/assets") / filename)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    return app_instance

