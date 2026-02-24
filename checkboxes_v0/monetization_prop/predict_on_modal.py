import io
import time
import torch
import modal
from diffusers import DiffusionPipeline

app = modal.App("diffusion-service")

#use volume to store the weights permanently
model_cache = modal.Volume.from_name("modal-cache-vol", create_if_missing=True)
CACHE_DIR = "/cache"

image = (
    modal.Image.debian_slim()
    .pip_install( "torch", "diffusers", "transformers", "accelerate", "safetensors", "Xformers" )
    .env({"HF_HOME": CACHE_DIR})) #tells hf to use volume path

@app.cls( image=image, gpu="L4", timeout=600, volumes={CACHE_DIR: model_cache} )
class DiffusionModel:
    @modal.enter()
    def setup(self)->None:
        print("===SETUP START TIME===")
        start = time.time()
        print(f"[{time.time()- start:.2f}s] Loading pipeline (will use cache if available)...")
        self.pipe = DiffusionPipeline.from_pretrained(
            "Johnowhitaker/rainbowdiffusion", torch_dtype=torch.float16, cache_dir = CACHE_DIR)
        if torch.cuda.is_available():
            print(f"[{time.time()- start:.2f}s] Moving to cuda...")
            self.pipe.to("cuda")
        
        model_cache.commit() #for first download, ensure weights are saved   
        #Enable memory efficient attention
        self.pipe.enable_attention_slicing()
        try: 
            self.pipe.enable_xformers_memory_efficient_attention()
        except Exception as e:
            print(f"Xformers not available: {e}")

        if hasattr(torch, 'complile'):
            self.pipe.unet = torch.compile(self.pipe.unet, mode="reduce-overhead")
        print(f"[{time.time()- start:.2f}s] SETUP COMPLETE...")

    @modal.method()
    @torch.inference_mode()
    def generate_and_save( self, email: str, prompt: str, file_id: str):
        print(f"🎨 Generating: {prompt}")
        image = self.pipe(prompt).images[0]

        timestamp = int(time.time())
        safe_prompt = "".join([c if c.isalnum() else "_" for c in prompt[:20]])
        filename = f"{timestamp}_{safe_prompt}.png"

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return filename, buf.getvalue()
