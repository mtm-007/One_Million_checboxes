#dreambooth end to end web app
from dataclasses import dataclass
from pathlib import Path

import modal
import os
# from fastapi import FastAPI
# from fastapi.responses import FileResponse

import fasthtml.common as fh

os.environ["WANDB_PROJECT"] = "dreambooth_sdxl_app"

# You can read more about how the values in `TrainConfig` are chosen and adjusted [in this blog post on Hugging Face](https://huggingface.co/blog/dreambooth).

app = modal.App( name = "dreambooth-app")
image = modal.Image.debian_slim(python_version="3.10").uv_pip_install(  
        "python-fasthtml",
        "accelerate==0.31.0",
        "datasets~=2.13.0",
        "ftfy~=6.1.0",
        "huggingface-hub==0.36.0",
        "numpy<2",
        "peft==0.11.1",
        "pydantic==2.9.2",
        "sentencepiece>=0.1.91,!=0.1.92",
        "smart_open~=6.4.0",
        "starlette==0.41.2",
        "transformers~=4.41.2",
        "torch~=2.2.0",
        "torchvision~=0.16",
        "triton~=2.2.0",
        "wandb==0.17.6",
        "gradio~=3.50.2",
)
                                                                
GIT_SHA = "e649678bf55aeaa4b60bd1f68b1ee726278c0304"  # specify the commit to fetch )

image = (image.apt_install("git")
         .run_commands(
            "cd /root && git init .",
            "cd /root && git remote add origin https://github.com/huggingface/diffusers",
            f"cd /root && git fetch --depth=1 origin {GIT_SHA} && git checkout {GIT_SHA}",
            # Patch 1: Remove cached_download
            #"sed -i 's/from huggingface_hub import cached_download,/from huggingface_hub import/' /root/src/diffusers/utils/dynamic_modules_utils.py",
            "cd /root && pip install -e . --no-deps",
         ))


# @app.function(secrets=[modal.Secret.from_name("huggingface")])                                                                                  
# def some_function():                                                                                                                            
#     os.getenv("HF_TOKEN")
    
@dataclass
class SharedConfig:
    """ Configuration info shared across the project"""
    # The instance name is the "proper noun" we're teaching the model
    instance_name: str = "Qwerty"
    class_name: str = "Golden Retriever"
    
    model_name: str = "black-forest-labs/FLUX.1-dev"

    # model_name: str = "stabilityai/stable-diffusion-xl-base-1.0"
    # vae_name: str = "madebyollin/sdxl-vae-fp16-fix"  # required for numerical stability in fp16


volume = modal.Volume.from_name( "dreambooth-finetunning-volume-flux", create_if_missing=True)
MODEL_DIR = "/model"

#when using flux
huggingface_secret = modal.Secret.from_name(
    "huggingface", required_keys=["HF_TOKEN"]
)

image = image.env( {"HF_XNET_HIGH_PERFORMANCE": "1"})

@app.function( volumes={MODEL_DIR: volume}, image=image, secrets=[huggingface_secret], timeout=600,) #10 min 


def download_models(config):
    import torch
    from diffusers import  DiffusionPipeline#,AutoencoderKL,
    from huggingface_hub import snapshot_download

    snapshot_download( config.model_name, local_dir= MODEL_DIR, ignore_patterns=["*.pt", "*.bin"],)

    DiffusionPipeline.from_pretrained(MODEL_DIR, torch_dtype = torch.bfloat16,  )


#load dataset
def load_images(image_urls: list[str]) -> Path:
    import PIL.Image
    from smart_open import open

    img_path = Path("/img")

    img_path.mkdir(parents=True, exist_ok=True)
    for ii, url in enumerate(image_urls):
        with open(url, "rb") as f:
            image = PIL.Image.open(f)
            image.save(img_path / f"{ii}.png")
    print(f"{ii + 1} images loaded")

    return img_path

USE_WANDB = True

    
#image = image.run_function(download_models)

@dataclass
class TrainConfig(SharedConfig):
    """ Configuration for finetunining steps."""
    # training prompt looks like `{PREFIX} {INSTANCE_NAME} the {CLASS_NAME} {POSTFIX}`
    prefix: str = "a photo of"
    postfix: str = ""
    instance_name: str = "sks"  # or whatever default you want

    # locator for plaintext file with urls for images of target instance
    instance_example_urls_file: str = ( Path(__file__).parent / "instance_example_urls.txt")

    #hyperparameters
    resolution: int = 512
    train_batch_size: int = 3
    rank: int = 16
    gradient_accumulation_steps: int = 1
    learning_rate: float = 4e-4
    lr_scheduler:str = "constant"
    lr_warmup_steps: int = 0
    max_train_steps: int = 500
    checkpointing_steps: int = 1000
    seed: int = 117
    wandb_project: str = "dreambooth_sdxl_app"

@app.function(image=image, gpu="A100-80GB", volumes={MODEL_DIR:volume}, timeout=1800, 
              secrets=[huggingface_secret]+ ( [modal.Secret.from_name("my-wandb-secret")] if USE_WANDB else []), )

def train(instance_example_urls, config):
    import subprocess
    from accelerate.utils import write_basic_config

    config = TrainConfig()
    #load data locally
    img_path = load_images(instance_example_urls)

    write_basic_config(mixed_precision="bf16")

    #define training prompt
    instance_phrase = f"{config.instance_name} the {config.class_name}"
    prompt = f"{config.prefix} {instance_phrase} { config.postfix}".strip()

    # the model training is packaged as a script, so we have to execute it as a subprocess, which adds some boilerplate
    def _exec_subprocess(cmd: list[str]):
        """Executes subprocess and prints log to terminal while subprocess is running."""
        process = subprocess.Popen( cmd, stdout= subprocess.PIPE, stderr=subprocess.STDOUT,)
        with process.stdout as pipe:
            for line in iter(pipe.readline, b""):
                line_str = line.decode()
                print(f"{line_str}", end="")
        
        if exitcode := process.wait() != 0:
            raise subprocess.CalledProcessError(exitcode, "\n".join(cmd))
    
    print("launching dreambooth training script")
    _exec_subprocess( [ "accelerate", "launch", 
                        "examples/dreambooth/train_dreambooth_lora_flux.py",
                        "--mixed_precision=fp16",
                        f"--pretrained_model_name_or_path={config.model_name}",
                        f"--instance_data_dir={img_path}",
                        f"--output_dir={MODEL_DIR}",
                        f"--instance_prompt={prompt}",
                        f"--resolution={config.resolution}",
                        f"--train_batch_size={config.train_batch_size}",
                        f"--gradient_accumulation_steps={config.gradient_accumulation_steps}",
                        f"--learning_rate={config.learning_rate}",
                        f"--lr_scheduler={config.lr_scheduler}",
                        f"--lr_warmup_steps={config.lr_warmup_steps}",
                        f"--max_train_steps={config.max_train_steps}",
                        f"--checkpointing_steps={config.checkpointing_steps}",
                        f"--seed={config.seed}",]
                    + (
                        [
                            "--report_to=wandb",
                            #f"--validation_prompt={prompt} in space",
                            #f"--validation_epochs={config.max_train_steps // 5}",
                        ] if USE_WANDB else []
                      ),
                    )
    volume.commit()

RESULTS_DIR = "/results"
results_volume = modal.Volume.from_name("dreambooth-results-volume", create_if_missing=True)

@app.cls(image=image, gpu="A100", volumes={MODEL_DIR: volume, RESULTS_DIR: results_volume},
         secrets=[modal.Secret.from_name("my-wandb-secret")] if USE_WANDB else [])
class Model:
    @modal.enter()
    def load_model(self):
        import torch
        from diffusers import AutoencoderKL, DiffusionPipeline
        
        volume.reload()

        pipe = DiffusionPipeline.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16,).to("cuda")
        pipe.load_lora_weights(MODEL_DIR)
        self.pipe = pipe
    
    @modal.method()
    def inference(self, text, config):
        import datetime, time, wandb, os

        t1 = time.perf_counter()
        image =  self.pipe(  text, num_inference_steps=config.num_inference_steps, guidance_scale=config.guidance_scale,).images[0]

        t2 = time.perf_counter()
        #filename = f"generated_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.png"
        filename = f"generated_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        filepath = Path(RESULTS_DIR) / filename
        image.save(filepath)

        results_volume.commit()
        t3 = time.perf_counter()
        total_duration = t3 - t1
        inference_time = t2 - t1
        print(f"Image saved to {filepath}")
        print(f" Inference took {inference_time:.3f} sec and total with saving file it took {total_duration:.3f} sec")

        if USE_WANDB :
            wandb.init(
                project = os.environ.get("WANDB_PROJECT", "dreambooth_sdxl_app"),
                job_type = "production-inference",
                config={ "modal_dir": MODEL_DIR,
                        "num_inference_steps": config.num_inference_steps,
                        "guidance_scale": config.guidance_scale},
                reinit=True # allows creating multiple logs in the same session
            )
            wandb.log({ "inference/image": wandb.Image(str(filepath), caption=text),
                        "inference/prompt": text, "inference/inference_time": inference_time})
            wandb.finish()
        
        #return image
        return str(filepath)


#web_app = FastAPI()

@dataclass
class AppConfig(SharedConfig):
    """ Configuration information for inference."""
    num_inference_steps: int = 25
    guidance_scale: float = 7.5

assets_path = Path(__file__).parent / "assets"
image = image.add_local_dir(assets_path, remote_path="/assets")

@app.function(  image=image, max_containers=3, volumes={RESULTS_DIR: results_volume})

@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def fasthtml_app():
    config = AppConfig()

    instance_phrase = f"{config.instance_name} the {config.class_name}"

    def get_history():
        results_volume.reload()
        path = Path(RESULTS_DIR)
        if not path.exists(): return []
        imgs = sorted( path.glob("*.png"), key=os.path.getmtime, reverse=True)
        return [Path(img) for img in imgs]

    app = fh.FastHTML()

    @app.get("/")
    def index():
        history = get_history()
        latest = history[0] if history else None

        return fh.Html(
            fh.Head(
                fh.Title("Dreambooth on Modal"),
                fh.Link(rel="stylesheet", href="/assets/styles.css"),
            ),
            fh.Body(
                fh.Main(
                    fh.H1(f"Dream up images of {instance_phrase}"),
                    fh.P("Describe what they are doing, styles, artist, etc."),
                    fh.Form(
                        fh.Textarea(name="prompt", palceholder=f"Describe {instance_phrase}", rows=6, cls="prompt-box",),
                        fh.Button("Dream", type="submit"), method="post", action="/generate",),

                    fh.H2("Lastet result"), fh.Img(src=f"/image/{latest.name}") if latest else fh.P("No images yet"),
                    fh.H2("Gallery"), fh.Div( *[ fh.Img(src=f"/image/{img.name}", cls="thumb") for img in history[:]], cls="gallery",
                        ),
                ),
            )
        )

    @app.post("/generate")
    def generate(prompt: str = ""):
        if not prompt: prompt = f"{instance_phrase}"

        Model().inference.remote(prompt, config)
        return fh.Redirect("/") 
    
    @app.get("/image/{name}")
    def serve_image(name: str):
        return fh.FileResponse(Path(RESULTS_DIR) / name)

    @app.get("/assets/{filename}")
    def serve_asset(filename: str):
        return fh.FileResponse(Path("/assets") / filename)
    
    return app


# def fastapi_app():
#     import gradio as gr
#     import os
#     from gradio.routes import mount_gradio_app

#     def get_history():
#         results_volume.reload()#sync volume to see new files
#         path = Path(RESULTS_DIR)
#         if not path.exists(): return []
#         #get all pngs, sort by creation time(newest first)
#         imgs = sorted(path.glob("*.png"), key=os.path.getmtime, reverse=True)
#         return [str(img) for img in imgs]

#     # Call out to the inference in a separate Modal environment with a GPU
#     def go(text):
#         if not text: text = example_prompts[0]
#         Model().inference.remote(text, config) #inference.remote here enables the GPU to scale independently than the cpu
#         history = get_history()
#         return history[0], history

#     #set up AppConfig
#     config = AppConfig()
#     web_app = FastAPI()

#     instance_phrase = f"{config.instance_name} the {config.class_name}"
#     example_prompts = [ f"{instance_phrase}",
#                         f"a painting of {instance_phrase.title()} With A Pearl Earring, by Vermeer",
#                         f"oil painting of {instance_phrase} flying through space as an astronaut",
#                         f"a painting of {instance_phrase} in cyberpunk city. character desing by cory loftis.volumetric light, detailed, rendered in octance",
#                         f"drawing of {instance_phrase} high quality, cartoon, path traced, by studio ghibli and don bluth",]
    
#     modal_docs_url = "https://modal.com/docs/guide"
#     modal_example_url = f"{modal_docs_url}/examples/dreambooth_app"
#     description = f"""Describe what they are doing or how a particular artist or style would depict them. Be fantastical! Try the examples below for inspiration.

# ### Learn how to make a "Dreambooth" for your own pet [here]({modal_example_url}).
#     """
    
#     # custom styles: an icon, a background, and a theme
#     @web_app.get("/favicon.ico", include_in_schema=False)
#     async def favicon():
#         return FileResponse("/assets/favicon.svg")

#     @web_app.get("/assets/background.svg", include_in_schema=False)
#     async def background():
#         return FileResponse("/assets/background.svg")

#     with open("/assets/index.css") as f:
#         css = f.read()

#     theme = gr.themes.Default(
#         primary_hue="green", secondary_hue="emerald", neutral_hue="neutral"
#     )

#     #add gradio UI around inference
#     with gr.Blocks( theme = theme, css=css, title ="Dreambooth on Modal") as interface:
#         gr.Markdown( f"# Dream up images of {instance_phrase}.\n\n{description}",)
#         with gr.Row():
#             inp = gr.Textbox ( label="", placeholder=f"Describe the version of {instance_phrase} you'd like to see", lines=10,)
            
#             intial_history = get_history()
#             intial_img = intial_history[0] if intial_history else None
#             out = gr.Image( value = intial_img, height=512, width=512, label="", min_width=512, elem_id="output")
#         with gr.Row():
#             btn = gr.Button("Dream", variant="primary", scale=2)
#             #btn.click(  fn=go, inputs=inp, outputs=out) # connect inputs and outputs with inference function
#             gr.Button(  "⚡️ Powered by Modal", variant ="secondary", link="https://modal.com",)
        
#         #exmaple prompts
#         with gr.Column(variant="compact"):
#             gr.Markdown('### Try an example prompt:')
#             for prompt in example_prompts:
#                 ex_btn = gr.Button(prompt, variant="secondary")
#                 ex_btn.click(fn=lambda p=prompt: p, outputs=inp)

#         gr.Markdown('### Gallery ')
#         gallery = gr.Gallery(
#             value=intial_history, columns= 4, rows=2, height="auto", allow_preview=True)
#         btn.click(fn=go, inputs=inp, outputs=[out, gallery])

#     return mount_gradio_app(app=web_app, blocks=interface, path="/")


@app.local_entrypoint()
def run( max_train_steps: int = 250,):
    print("loading model")
    download_models.remote(SharedConfig())
    print("setting up training")
    config = TrainConfig(max_train_steps=max_train_steps)
    instance_example_urls = ( Path(TrainConfig.instance_example_urls_file).read_text().splitlines() )
    train.remote(instance_example_urls, config)
    print("traning finished")