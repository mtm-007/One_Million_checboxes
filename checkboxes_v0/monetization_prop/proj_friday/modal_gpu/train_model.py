import modal
from pathlib import Path
import base64
from io import BytesIO
from PIL import Image
from modal import gpu, secret
import torch
from diffursers import StableDiffusionXLPipeline, DPMSolverMultistepScheduler
from diffusers.loaders import LoraLoaderMixin
from peft import LoraConfig
import os

#create Modal stub
stub= modal.Stub("photo-ai-training")

image = (modal.Image.debian_slim().pip_install( "torch==2.1.0",
                                                "torchvision",
                                                "diffusers==0.25.0",
                                                "transformers",
                                                "accelerate",
                                                "peft",
                                                "bitsandbytes",
                                                "opencv-python-headless",
                                                "pillow",
                                                "huggingface_hub"))
        
volume = modal.Volume.from_name("modal-storage", create_if_missing=True)

@stub.function( gpu= "A100",
                timeout=3600, # 1 hr max
                image=image,
                volumes = {"/models": volume},
                secrets = [modal.Secret.from_name("huggingface-secret")]
)

def train_dreambooth_lora(
    model_id: str,
    instance_images: list[str],#base64 enconded images
    instance_prompt: str = "a photo of sks person",
    max_train_steps: int = 500,
):
    """ 
    Train a DreamBooth LoRA model for personalized image generation.
        
        Args:
            model_id: Unique identifier for this model
            instance_images: List of base64 encoded training images
            instance_prompt: Training prompt
            max_train_steps: Number of training steps
        
        Returns:
            dict with status and model_path
    """
    try: 
        training_dir = f"/tmp/training_{model_id}"
    except:
        pass