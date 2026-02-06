#dreambooth end to end web app
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from modal import ( App, Image, Secret, Volume, asgi_app, enter, method)

# You can read more about how the values in `TrainConfig` are chosen and adjusted [in this blog post on Hugging Face](https://huggingface.co/blog/dreambooth).

app = App( name = "dreambooth-app")
# image = (Image.debian_slim(python_version="3.10").pip_install(   "accelerate==0.27.2", "datasets~=2.13.0", "ftfy~=6.1.0", "gradio~=3.50.2", "smart_open~=6.4.0",
#                                                                 "transformers~=4.35.2", "triton~=2.2.0", "peft==0.7.0", "wandb==0.16.3", #"huggingface_hub==0.17.3", "diffusers==0.27.2",  
#                                                                 "torch==2.2.2+cu121", "torchvision==0.17.2+cu121",  
#                                                                 extra_options="--extra-index-url https://download.pytorch.org/whl/cu121",)
# )
image = (
    Image.debian_slim(python_version="3.10")
    .run_commands("echo 'rebuild-v3'")  # Force rebuild
    .pip_install(
        "accelerate==0.27.2",
        "datasets~=2.13.0",
        "ftfy~=6.1.0",
        "gradio~=3.50.2",
        "smart_open~=6.4.0",
        "transformers~=4.38.1",
        "triton~=2.2.0",
        "peft==0.7.0",
        "wandb==0.16.3",
        "torch==2.2.2+cu121",
        "torchvision==0.17.2+cu121",
        "diffusers==0.30.0",  # Use newer version (0.30.0 is compatible)
        extra_options="--extra-index-url https://download.pytorch.org/whl/cu121",
    )
)

                                                                

# GIT_SHA = ( "abd922bd0c43a504e47eca2ed354c3634bd00834")  # specify the commit to fetch )

# image = (image.apt_install("git")
#          .run_commands(
#             "cd /root && git init .",
#             "cd /root && git remote add origin https://github.com/huggingface/diffusers",
#             f"cd /root && git fetch --depth=1 origin {GIT_SHA} && git checkout {GIT_SHA}",
#             "cd /root && pip install -e .",
#             "pip install 'huggingface_hub==0.17.3' --force-reinstall",
#          ))

@dataclass
class SharedConfig:
    """ Configuration info shared across the project"""
    # The instance name is the "proper noun" we're teaching the model
    isinstance_name: str = "Qwerty"
    class_name: str = "Golden Retriever"
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

volume = Volume.from_name( "dreambooth-finetunning-volume", create_if_missing=True)
MODEL_DIR = "/model"

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

@app.function(image=image, gpu="A100", volumes={MODEL_DIR:volume}, timeout=1800, 
            # This looks for a Secret in your Modal dashboard named 'my-wandb-secret'
            # which should contain the environment variable WANDB_API_KEY
            secrets=[Secret.from_name("my-wandb-secret")] if USE_WANDB else [],)

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
    cmd =  [    "accelerate", "launch", "examples/dreambooth/train_dreambooth_lora_sdxl.py",
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
    if USE_WANDB:
        cmd += [ "--report_to=wandb", 
                f"--wandb_project={config.wandb_project}",
                f"--validation_prompt={prompt} in space",
                f"--validation_epochs={max(1, config.max_train_steps // 5)}", 
                "--seed=42"] 
    
    print("launching dreambooth training script")
    _exec_subprocess(cmd)                  
    volume.commit()

# In order to initialize the model just once on container startup,
# we use Modal's [container lifecycle](https://modal.com/docs/guide/lifecycle-functions) features, which require the function to be part
# of a class. Note that the `modal.Volume` we saved the model to is mounted here as well,
# so that the fine-tuned model created  by `train` is available to us.


@app.cls(image=image, gpu="A10G", volumes={MODEL_DIR: volume})
class Model:
    #enter()
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
    
    @method()
    def inference(self, text, config):
        return self.pipe(  text, num_inference_steps=config.num_inference_steps, guidance_scale=config.guidance_scale,).images[0]
        
web_app = FastAPI()
assets_path = Path(__file__).parent / "assets" 

@dataclass
class AppConfig(SharedConfig):
    """ Configuration information for inference."""
    num_inference_steps: int = 25
    guidance_scale: float = 7.5

assets_path = Path(__file__).parent / "assets"
image = image.add_local_dir(assets_path, remote_path="/assets")

#@app.function(  image=image, concurrency_limit=3, mounts =[Volume.from_local_dir(assets_path, remote_path="/assets")],)
@app.function(  image=image, max_containers=3, )

@asgi_app()
def fastapi_app():
    import gradio as gr
    from gradio.routes import mount_gradio_app

    # Call out to the inference in a separate Modal environment with a GPU
    def go(text=""):
        if not text:
            text = example_prompts[0]
        return Model().inference.remote(text, config)

        #set up AppConfig
    config = AppConfig()

    instance_phrase = f"{config.instance_name} the {config.class_name}"
    example_prompts = [ f"{instance_phrase}",
                        f"a painting of {instance_phrase.title()} With A Pearl Earring, by Vermeer",
                        f"oil painting of {instance_phrase} flying through space as an astronaut",
                        f"a painting of {instance_phrase} in cyberpunk city. character desing by cory loftis.volumetric light, detailed, rendered in octance",
                        f"drawing of {instance_phrase} high quality, cartoon, path traced, by studio ghibli and don bluth",]
    
    modal_docs_url = "https://modal.com/docs/guide"
    modal_example_url = f"{modal_docs_url}/examples/dreambooth_app"
    description = f"""Describe what they are doing or how a particular artist or style would depict them. Be fantastical! Try the examples below for inspiration.

    ### Learn how to make a "Dreambooth" for your own pet [here]({modal_example_url}) """
    
    #custom styles: an icon, a background, and theme
    @web_app.get("/favicon.ico", include_in_schema=False)
    async def favicon(): 
        return FileResponse("/assets/favicon.svg")

    with open("/assets/index.css") as f: css = f.read()

    theme = gr.themes.Default(  primary_huw="green", secondary_hue="emerald", neutral_hue="neutral" )

    #add gradio UI around inference
    with gr.Blocks( theme = theme, css=css, title ="Pet Dreambooth on Modal") as interface:
        gr.Markdown( f"# Dream up images of {instance_phrase}.\n\n{description}",)
        with gr.Row():
            inp = gr.Textbox ( label="", placeholder=f"Describe the version of {instance_phrase} you'd like to see", lines=10,)
            out = gr.Image( height=512, width=512, label="", min_width=512, elem_id="output")
        with gr.Row():
            btn = gr.Button("Dream", variant="primary", scale=2)
            btn.click(  fn=go, inputs=inp, outputs=out) # connect inputs and outputs with inference function
            gr.Button(  "⚡️ Powered by Modal", variant ="secondary", link="https://modal.com",)
        with gr.Column(variant="compact"):
            for ii, prompt in enumerate(example_prompts):
                btn = gr.Button(prompt, variant="secondary")
                btn.click(fn=lambda idx=ii: example_prompts[idx], outputs=inp)
    return mount_gradio_app(app=web_app, blocks=interface, path="/")


# ## Running your own Dreambooth from the command line
#
# You can use the `modal` command-line interface to set up, customize, and deploy this app:
#
# - `modal run dreambooth_app.py` will train the model. Change the `instance_example_urls_file` to point to your own pet's images.
# - `modal serve dreambooth_app.py` will [serve](https://modal.com/docs/guide/webhooks#developing-with-modal-serve) the Gradio interface at a temporary location. Great for iterating on code!
# - `modal shell dreambooth_app.py` is a convenient helper to open a bash [shell](https://modal.com/docs/guide/developing-debugging#interactive-shell) in our image. Great for debugging environment issues.
#
# Remember, once you've trained your own fine-tuned model, you can deploy it using `modal deploy dreambooth_app.py`.
#
# If you just want to try the app out, you can find it at https://modal-labs-example-dreambooth-app-fastapi-app.modal.run


@app.local_entrypoint()
def run():
    with open(TrainConfig().instance_example_urls_file) as f:
        instance_example_urls = [line.strip() for line in f.readlines()]
    train.remote(instance_example_urls)