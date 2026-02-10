#dreambooth end to end web app
from dataclasses import dataclass
from pathlib import Path

import modal
import os

import fasthtml.common as fh

os.environ["WANDB_PROJECT"] = "dreambooth_sdxl_app"

app = modal.App( name = "dreambooth-app")

image = modal.Image.debian_slim(python_version="3.10").uv_pip_install(  
        "python-fasthtml", "accelerate==0.31.0", "datasets~=2.13.0", "ftfy~=6.1.0", "huggingface-hub==0.36.0", "numpy<2", "peft==0.11.1",
        "pydantic==2.9.2", "sentencepiece>=0.1.91,!=0.1.92", "smart_open~=6.4.0", "starlette==0.41.2",
        "transformers~=4.41.2", "torch~=2.2.0", "torchvision~=0.16", "triton~=2.2.0", "bitsandbytes", "wandb==0.17.6",
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

    
@dataclass
class SharedConfig:
    """ Configuration info shared across the project"""
    # The instance name is the "proper noun" we're teaching the model
    instance_name: str = "Qwerty"
    class_name: str = "Golden Retriever"
    model_name: str = "black-forest-labs/FLUX.1-dev"

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
    train_batch_size: int = 1
    rank: int = 8
    gradient_accumulation_steps: int = 6
    learning_rate: float = 5e-4
    lr_scheduler:str = "constant"
    lr_warmup_steps: int = 0
    max_train_steps: int = 500
    checkpointing_steps: int = 100
    seed: int = 117
    resume_from_checkpointing: str = "latest"
    wandb_project: str = "dreambooth_sdxl_app"

@dataclass
class AppConfig(SharedConfig):
    """ Configuration information for inference."""
    num_inference_steps: int = 25
    guidance_scale: float = 7.5


volume = modal.Volume.from_name( "dreambooth-finetunning-volume-flux", create_if_missing=True)
results_volume = modal.Volume.from_name("dreambooth-results-volume", create_if_missing=True)
RESULTS_DIR = "/results"
MODEL_DIR = "/model"

USE_WANDB = True

#when using flux
huggingface_secret = modal.Secret.from_name( "huggingface", required_keys=["HF_TOKEN"])
image = image.env( {"HF_XNET_HIGH_PERFORMANCE": "1", "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",})

@app.function( volumes={MODEL_DIR: volume}, image=image, secrets=[huggingface_secret], timeout=600,) #10 min 

def download_models(config):
    import torch
    from diffusers import  DiffusionPipeline#,AutoencoderKL,
    from huggingface_hub import snapshot_download

    snapshot_download( config.model_name, local_dir= MODEL_DIR, ignore_patterns=["*.pt", "*.bin"],)
    DiffusionPipeline.from_pretrained(MODEL_DIR, torch_dtype = torch.bfloat16,  )

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

@app.function(image=image, gpu="L40S", volumes={MODEL_DIR:volume}, timeout=1800, 
              secrets=[huggingface_secret]+ ( [modal.Secret.from_name("my-wandb-secret")] if USE_WANDB else []), )

def train(instance_example_urls, config):
    import subprocess, torch
    from accelerate.utils import write_basic_config

    # Print GPU memory before training
    if torch.cuda.is_available():
        print(f"GPU Memory allocated: {torch.cuda.memory_allocated() / 1e9:.2f} GB")
        print(f"GPU Memory reserved: {torch.cuda.memory_reserved() / 1e9:.2f} GB")
    
    config = TrainConfig()
    img_path = load_images(instance_example_urls)
    
    write_basic_config(mixed_precision="bf16")

    # Check for existing checkpoints
    checkpoint_dir = Path(MODEL_DIR) / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

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
                        "--mixed_precision=bf16",
                        "--gradient_checkpointing",
                        "--use_8bit_adam",  # Add this flag
                        # Add these memory-saving flags:
                        "--set_grads_to_none",
                        #"--rank=8", 
                        "--enable_xformers_memory_efficient_attention",  # Use xformers
                        f"--resume_from_checkpoint={config.resume_from_checkpoint}",

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
                        # LoRA rank
                        f"--rank={config.rank}",
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

@app.cls(image=image, gpu="L40S", volumes={MODEL_DIR: volume, RESULTS_DIR: results_volume},
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
        return str(filepath)

@app.local_entrypoint()
def run( max_train_steps: int = 250,):
    print("loading model")
    download_models.remote(SharedConfig())
    print("setting up training")
    config = TrainConfig(max_train_steps=max_train_steps)
    instance_example_urls = ( Path(TrainConfig.instance_example_urls_file).read_text().splitlines() )
    train.remote(instance_example_urls, config)
    print("traning finished")