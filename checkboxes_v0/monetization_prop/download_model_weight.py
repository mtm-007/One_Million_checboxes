from diffusers import StableDiffusionPipeline
import torch
model_path= "Johnowhitaker/rainbowdiffusion"
pipe = StableDiffusionPipeline.from_pretrained(model_path, torch_dtype=torch.float16)
                                               #use_safetensors=False, low_cpu_mem_usage=True, device_map=None)
pipe.to("cuda")
pipe.save_pretrained("my_diffusion_pipeline")
print("Model downloaded and saved successfully!")