from dataclasses import dataclass
from pathlib import Path

import modal
import os

import fasthtml.common as fh
#import dreambooth_app
from dreambooth_app import (app, image, Model, AppConfig, RESULTS_DIR, results_volume, )

os.environ["WANDB_PROJECT"] = "dreambooth_sdxl_app"


assets_path = Path(__file__).parent / "assets"
image = (image.add_local_dir(assets_path, remote_path="/assets")
              .add_local_file("dreambooth_app.py",remote_path="/root/dreambooth_app.py"))

@app.function(  image=image, max_containers=3, volumes={RESULTS_DIR: results_volume})

@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def fasthtml_app():
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
    def index():
        history = get_history()
        latest = history[0] if history else None

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
                ),
            )
        )

    @app_instance.post("/generate")
    def generate(prompt: str = ""):
        if not prompt: prompt = f"{instance_phrase}"

        Model().inference.remote(prompt, config)
        return fh.Redirect("/") 
    
    @app_instance.get("/image/{name}")
    def serve_image(name: str): return fh.FileResponse(Path(RESULTS_DIR) / name)

    @app_instance.get("/assets/{filename}")
    def serve_asset(filename: str):
        from starlette.responses import FileResponse

        response = fh.FileResponse(Path("/assets") / filename)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Exipires"] = "0"
        return response
    
    return app_instance
