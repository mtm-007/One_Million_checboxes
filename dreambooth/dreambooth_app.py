#dreambooth end to end web app
from dataclasses import dataclass
from pathlib import Path

import modal
from fastapi import FastAPI
from fastapi.responses import FileResponse

import os
os.environ["WANDB_PROJECT"] = "dreambooth_sdxl_app"

# You can read more about how the values in `TrainConfig` are chosen and adjusted [in this blog post on Hugging Face](https://huggingface.co/blog/dreambooth).

app = modal.App( name = "dreambooth-app")
image = modal.Image.debian_slim(python_version="3.10").uv_pip_install(  
        #"python-fasthtml",
        "accelerate==0.27.2",
        "datasets~=2.13.0",
        "ftfy~=6.1.0",
        "gradio~=3.50.2",
        "smart_open~=6.4.0",
        "transformers~=4.38.1",
        "torch~=2.2.0",
        "torchvision~=0.16",
        "triton~=2.2.0",
        "peft==0.7.0",
        "wandb==0.16.3",
)
                                                                
GIT_SHA = ( "abd922bd0c43a504e47eca2ed354c3634bd00834")  # specify the commit to fetch )

image = (image.apt_install("git")
         .run_commands(
            "cd /root && git init .",
            "cd /root && git remote add origin https://github.com/huggingface/diffusers",
            f"cd /root && git fetch --depth=1 origin {GIT_SHA} && git checkout {GIT_SHA}",
            # Patch 1: Remove cached_download
            "sed -i 's/from huggingface_hub import cached_download,/from huggingface_hub import/' /root/src/diffusers/utils/dynamic_modules_utils.py",
            "cd /root && pip install -e . --no-deps",
         ))

@dataclass
class SharedConfig:
    """ Configuration info shared across the project"""
    # The instance name is the "proper noun" we're teaching the model
    instance_name: str = "Qwerty"
    class_name: str = "Golden Retriever"
    
    #model_name: str = "black-forest-labs/FLUX.1-dev"

    model_name: str = "stabilityai/stable-diffusion-xl-base-1.0"
    vae_name: str = "madebyollin/sdxl-vae-fp16-fix"  # required for numerical stability in fp16

def download_models():
    import torch
    from diffusers import AutoencoderKL, DiffusionPipeline
    from transformers.utils import move_cache

    config = SharedConfig()
    DiffusionPipeline.from_pretrained(
        config.model_name,
        vae=AutoencoderKL.from_pretrained(
            config.vae_name, torch_dtype=torch.float16 ),
        torch_dtype = torch.float16,  )
    move_cache()

image = image.run_function(download_models)

volume = modal.Volume.from_name( "dreambooth-finetunning-volume", create_if_missing=True)
MODEL_DIR = "/model"

#when using flux
# huggingface_secret = modal.Secret.from_name(
#     "huggingface-secret", required_keys=["HF_TOKEN"]
# )

# image = image.env( {"HF_XNET_HIGH_PERFORMANCE": "1"})

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
    resolution: int = 1024
    train_batch_size: int = 4
    gradient_accumulation_steps: int = 1
    learning_rate: float = 1e-4
    lr_scheduler:str = "constant"
    lr_warmup_steps: int = 0
    max_train_steps: int = 80
    checkpointing_steps: int = 1000
    seed: int = 117
    wandb_project: str = "dreambooth_sdxl_app"

@app.function(image=image, gpu="A100", volumes={MODEL_DIR:volume}, timeout=1800, secrets=[modal.Secret.from_name("my-wandb-secret")] if USE_WANDB else [],)

def train(instance_example_urls):
    import subprocess
    from accelerate.utils import write_basic_config

    config = TrainConfig()
    #load data locally
    img_path = load_images(instance_example_urls)

    write_basic_config(mixed_precision="fp16")

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
                        "examples/dreambooth/train_dreambooth_lora_sdxl.py",
                        "--mixed_precision=fp16",
                        f"--pretrained_model_name_or_path={config.model_name}",
                        f"--pretrained_vae_model_name_or_path={config.vae_name}",
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
                        ["--report_to=wandb", f"--validation_prompt={prompt} in space",
                        f"--validation_epochs={config.max_train_steps // 5}",] if USE_WANDB else []
                      ),
                    )
    volume.commit()

RESULTS_DIR = "/results"
results_volume = modal.Volume.from_name("dreambooth-results-volume", create_if_missing=True)

@app.cls(image=image, gpu="A10G", volumes={MODEL_DIR: volume, RESULTS_DIR: results_volume},
         secrets=[modal.Secret.from_name("my-wandb-secret")] if USE_WANDB else [])
class Model:
    @modal.enter()
    def load_model(self):
        import torch
        from diffusers import AutoencoderKL, DiffusionPipeline
        
        config = TrainConfig()
        volume.reload()

        pipe = DiffusionPipeline.from_pretrained(
            config.model_name,  vae=AutoencoderKL.from_pretrained(config.vae_name, torch_dtype=torch.float16),
                                torch_dtype=torch.float16,).to("cuda")
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
        return image

assets_path = Path(__file__).parent / "assets"   
web_image = (
    modal.Image.debian_slim(python_version="3.10")
    .pip_install("python-fasthtml")
    .add_local_dir(assets_path, remote_path="/assets")
)


@dataclass
class AppConfig(SharedConfig):
    """ Configuration information for inference."""
    num_inference_steps: int = 25
    guidance_scale: float = 7.5

assets_path = Path(__file__).parent / "assets"
image = image.add_local_dir(assets_path, remote_path="/assets")

@app.function(  image=web_image, max_containers=3, )

@modal.asgi_app()
def fasthtml_app():
    
    import fasthtml.common as fh
    from starlette.responses import FileResponse

    fh_app, rt = fh.fast_app(
        hdrs = ( fh.Link(rel="icon", href="/favicon.ico", type="image/svg+xml"),
                 fh.Link(rel="stylesheet", href="/assets/styles.css", type="text/css"),))
    
    config = AppConfig()
    instance_phrase = f"{config.instance_name} the {config.class_name}"

    example_prompts = [ f"{instance_phrase}",
                        f"a painting of {instance_phrase.title()} With A Pearl Earring, by Vermeer",
                        f"oil painting of {instance_phrase} flying through space as an astronaut",
                        f"a painting of {instance_phrase} in cyberpunk city. character desing by cory loftis.volumetric light, detailed, rendered in octance",
                        f"drawing of {instance_phrase} high quality, cartoon, path traced, by studio ghibli and don bluth",]
    
    modal_docs_url = "https://modal.com/docs/guide"
    modal_example_url = f"{modal_docs_url}/examples/dreambooth_app"

    @fh_app.get("/favicon.ico")
    async def favicon(): return FileResponse("/assets/favicon.svg")

    @fh_app.get("/assets/background.svg")
    async def background(): return FileResponse("/assets/background.svg")

    @fh_app.get("/assets/style.css")
    async def styles(): return FileResponse("/assets/styles.css")

    @rt("/")
    def get():
        return fh.Titled (
            f"Dreambooth on Modal - {instance_phrase}",
            fh.Div( fh.H1(f"Dream up images of {instance_phrase}"),
                fh.P( 
                    f"Describe what they are doing or how a particular artist or style would depict them. Be fantastical! Try the examples below for inspiration.",
                    fh.Br(), fh.Br(),
                    fh.A(f"Learn how to make a 'Dreambooth' for your own pet here.", 
                         href= modal_example_url, target = "_blank", style="color: var(--primary);"), cls="description" ),
                fh.Div( #input section
                    fh.Div(
                        fh.Textarea( id="prompt-input", name="prompt", placeholder=f"Describe the version of {instance_phrase} you'd like to see", value=example_prompts[0]),
                    fh.Div( fh.Button("Dream", hx_post="/generate", hx_target="#output-image", hx_include="#prompt-input", hx_indicator="#output-image", cls="btn"),
                           fh.A( fh.Button(" Powered by Modal", cls="btn btn-secondary"), href="https://modal.com", target="_blank"), style="margin-top: 1rem;"), cls="input-section"),

                    fh.Div(
                        fh.Div(
                            fh.P( "Your generated image will appear here", style="text-align: center; opacity:0.5; padding: 3rem;"), id="output-image" ), cls="output-section"), cls="main-content" ),
            fh.Div( 
                fh.H3("Examples Prompts"),
                *[ 
                    fh.Button( prompt, 
                            hx_get=f"/set-prompt?prompt={quote(prompt)}", 
                            hx_target="#prompt-input", 
                            hx_swap="outerHTML", 
                            cls="btn btn-example"
                    )
                    for prompt in example_prompts
                ], 
                cls="examples"
            ), 
            cls="container" 
        ),
        #add HTMX
        fh.Script(src="https://unpkg.com/htmx.org@1.9.10")
    )
    # Set prompt endpoint
    @rt("/set-prompt")
    def get(prompt: str):
        return fh.Textarea(
            id="prompt-input",
            name="prompt",
            placeholder=f"Describe the version of {instance_phrase} you'd like to see",
            value=prompt
        )
    
    # Generate endpoint
    @rt("/generate")
    async def post(prompt: str):
        if not prompt:
            prompt = example_prompts[0]
        
        # Show loading state immediately
        yield fh.Div(
            fh.Div(cls="spinner"),
            fh.P("Generating your image...", style="text-align: center;"),
            cls="loading",
            id="output-image"
        )
        
        # Call inference
        img_base64 = Model().inference.remote(prompt, config)
        
        # Return generated image
        yield fh.Div(
            fh.Img(src=f"data:image/png;base64,{img_base64}", alt=prompt),
            id="output-image"
        )
    
    return fh_app

# Helper for URL encoding
def quote(s):
    from urllib.parse import quote as url_quote
    return url_quote(s)
               
    
# @modal.asgi_app()   
# def fastapi_app():
#     import gradio as gr
#     from gradio.routes import mount_gradio_app

#     # Call out to the inference in a separate Modal environment with a GPU
#     def go(text=""):
#         if not text:
#             text = example_prompts[0]
#         return Model().inference.remote(text, config) #inference.remote here enables the GPU to scale independently than the cpu

#         #set up AppConfig
#     config = AppConfig()

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
#     with gr.Blocks( theme = theme, css=css, title ="Pet Dreambooth on Modal") as interface:
#         gr.Markdown( f"# Dream up images of {instance_phrase}.\n\n{description}",)
#         with gr.Row():
#             inp = gr.Textbox ( label="", placeholder=f"Describe the version of {instance_phrase} you'd like to see", lines=10,)
#             out = gr.Image( height=512, width=512, label="", min_width=512, elem_id="output")
#         with gr.Row():
#             btn = gr.Button("Dream", variant="primary", scale=2)
#             btn.click(  fn=go, inputs=inp, outputs=out) # connect inputs and outputs with inference function
#             gr.Button(  "⚡️ Powered by Modal", variant ="secondary", link="https://modal.com",)
#         with gr.Column(variant="compact"):
#             for ii, prompt in enumerate(example_prompts):
#                 btn = gr.Button(prompt, variant="secondary")
#                 btn.click(fn=lambda idx=ii: example_prompts[idx], outputs=inp)

#     return mount_gradio_app(app=web_app, blocks=interface, path="/")


@app.local_entrypoint()
def run():
    with open(TrainConfig().instance_example_urls_file) as f:
        instance_example_urls = [line.strip() for line in f.readlines()]
    train.remote(instance_example_urls)