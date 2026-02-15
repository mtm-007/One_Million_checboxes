from dataclasses import dataclass
from pathlib import Path

from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler

import modal
import os

import fasthtml.common as fh
#import dreambooth_app
from dreambooth_app import (app, image, Model, AppConfig, RESULTS_DIR, results_volume, )

os.environ["WANDB_PROJECT"] = "dreambooth_sdxl_app"

LOGS_DIR = "/logs"
logs_volume = modal.Volume.from_name("dreambooth-logs", create_if_missing=True)

# NEW FUNCTION
def setup_logging():
    """Setup file and console logging"""
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
    
    return logger

# NEW HELPER FUNCTION
def log_request(logger, request, message):
    """Log request with IP and user agent"""
    ip = request.client.host if hasattr(request, 'client') else 'unknown'
    user_agent = request.headers.get('user-agent', 'unknown') if hasattr(request, 'headers') else 'unknown'
    logger.info(f"{message} | IP: {ip} | UA: {user_agent[:50]}")

assets_path = Path(__file__).parent / "assets"
image = (image.add_local_dir(assets_path, remote_path="/assets")
              .add_local_file("dreambooth_app.py",remote_path="/root/dreambooth_app.py"))

@app.function(  image=image, max_containers=3, volumes={RESULTS_DIR: results_volume, LOGS_DIR: logs_volume })

@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def fasthtml_app():
    #New: logging
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("🚀 FastHTML App Starting") 
    logger.info("=" * 60)

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
        f"surreal concept art of {instance_phrase.title()} floating through space inside an astronaut suit, distant galaxies, soft rim lighting, cinematic composition, high detail, dreamlike atmosphere",
        f"{instance_phrase.title()} emerging from swirling clouds made of light, ethereal glow, soft pastel colors, fantasy concept art, volumetric lighting, ultra detailed",
        f"stylized character design of {instance_phrase.title()}, expressive face, simplified shapes, soft lighting, clean color palette, high-quality animation style render",
        f"3D character render of {instance_phrase.title()}, friendly proportions, soft studio lighting, detailed fur groom, high-end animation quality",
        f"professional wildlife photograph of {instance_phrase.title()}, natural outdoor lighting, sharp focus, detailed fur texture, realistic colors, DSLR photo, high resolution",
        f"studio portrait photography of {instance_phrase.title()}, neutral background, softbox lighting, high detail, realistic fur, shallow depth of field",
        f"cinematic film still of {instance_phrase.title()} in the rain at night, neon reflections, dramatic lighting, shallow depth of field, ultra-realistic, moody atmosphere",
        f"epic fantasy concept art of {instance_phrase.title()} standing on a mountain peak, flowing clouds, dramatic sky, volumetric lighting, heroic composition, ultra detailed",
        f"studio portrait of {instance_phrase.title()}, soft directional lighting, expressive eyes, detailed fur, cinematic realism",
        f"a painting of {instance_phrase.title()} With A Pearl Earring, by Vermeer",]

    def get_history():
        results_volume.reload()
        path = Path(RESULTS_DIR)
        if not path.exists(): return []
        imgs = sorted( path.glob("*.png"), key=os.path.getmtime, reverse=True)
        return [Path(img) for img in imgs]

    app_instance = fh.FastHTML()

    @app_instance.get("/")
    def index(request):
        #logging
        logger.info("📄 GET / - Homepage accessed")
        log_request(logger, request, "Homepage view")

        try:
            history = get_history()
            latest = history[0] if history else None
            logger.info(f"📊 Gallery has {len(history)} images ")

            logs_volume.commit()

            return fh.Html(
                fh.Head(
                    fh.Title("Dreambooth on Modal"),
                    fh.Link(rel="stylesheet", href="/assets/styles.css?v=2"),
                ),
                fh.Body(
                    fh.Main(
                        fh.H1(f"Dream up and Generate Images with Flux "),
                        fh.P("Describe what they are doing, styles, artist, etc."),
                        fh.Form(
                            fh.Textarea(name="prompt", palceholder=f"Describe {instance_phrase}", rows=6, cls="prompt-box", id="prompt-input"),
                            fh.Button("Dream", type="submit"), method="post", action="/generate",),
                        fh.Div(fh.H3("Try an example: "),
                            *[
                                fh.Button( prompt, cls="example-btn", onclick=f"document.getElementById('prompt-input').value = `{prompt}`")
                                for prompt in example_prompts
                            ], cls="examples" 
                        ),

                        fh.H2("Lastet result"), fh.Img(src=f"/image/{latest.name}") if latest else fh.P("No images yet"),
                        fh.H2("Gallery"), fh.Div( *[ fh.Img(src=f"/image/{img.name}", cls="thumb") for img in history[:]], cls="gallery",
                            ),
                    ),))
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
    
    @app_instance.get("/image/{name}")
    def serve_image(request, name: str): 
        logger.info(f" Get /image/{name}")
        log_request(logger, request, f"Image served: {name}")

        logs_volume.commit()

        return fh.FileResponse(Path(RESULTS_DIR) / name)

    @app_instance.get("/assets/{filename}")
    def serve_asset(request, filename: str):
        from starlette.responses import FileResponse

        logger.info(f"Get /assets/{filename}")

        response = fh.FileResponse(Path("/assets") / filename)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Exipires"] = "0"

        logs_volume.commit()

        return response
    
    logger.info("✅ FastHTML App initialized successfully") 
    logs_volume.commit()
    
    return app_instance
