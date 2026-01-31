#dreambooth end to end web app
from dataclasses import dataclass
from pathlib import Path
import torch
from diffusers import AutoencoderKL, DiffusionPipeline
from transformers.utils import move_cache
import PIL.Image
from smart_open import open

from fastapi import FastAPI
from fastapi.responses import FileResponse

from modal import ( App, Image, Mount, Secret, Volume, asgi_app, enter, method)


app = App( name = "dreambooth-app")
image = Image.debian_slim(python_version="3.10").pip_install(   "accelerate==0.27.2", "datasets~=2.13.0", "ftfy~=6.1.0", "gradio~=3.50.2", "smart_open~=6.4.0",
                                                                "transformers~=4.38.1", "torch~=2.2.0", "torchvision~=0.16", "triton~=2.2.0", "peft==0.7.0", "wandb==0.16.3",)

GIT_SHA = ( "abd922bd0c43a504e47eca2ed354c3634bd00834")  # specify the commit to fetch )

image = (image.apt_install("git")
         .run_commands(
             "cd /root && git init .",
             "cd /root && git remote add origin https://github.com/huggingface/diffusers",
        f"cd /root && git fetch --depth=1 origin {GIT_SHA} && git checkout {GIT_SHA}",
        "cd /root && pip install -e .",
         ))

@dataclass
class SharedConfig:
    """ Configuration info shared across the project"""
    # The instance name is the "proper noun" we're teaching the model
    isinstance_name: str = "Qwerty"
    class_name: str = "Golden Retriever"
    model_name: str = "stabilityai/stable-diffusion-xl-base-1.0"
    vae_name: str = "madebyollin/sdxl-vae-fp16-fix"  # required for numerical stability in fp16

def download_models():
    config = SharedConfig()
    DiffusionPipeline.from_pretrained(
        config.model_name,
        vae=AutoencoderKL.from_pretrained(
            config.vae_name, torch_dtype=torch.float16 ),
        torch_dtype = torch.float16,  )
    move_cache()

image = image.run_function(download_models)

Volume = Volume.from_name( "dreambooth-finetunning-volume", create_if_missing=True)
MODEL_DIR = "/model"

#load dataset

def load_images(image_urls: list[str]) -> Path:
    img_path = Path("/img")

    img_path.mkdir(parents=True, exist_ok=True)
    for ii, url in enumerate(image_urls):
        with open(url, "rb") as f:
            image = PIL.Image.open(f)
            image.save(img_path / f"{ii}.png")
    print(f"{ii + 1} images loaded")

    return img_path

USE_WANDB = False


@dataclass
class TrainConfig(SharedConfig):
    """ Configuration for finetunining steps."""
    prefix: str = "a photo of"
    postfix: str = ""
    