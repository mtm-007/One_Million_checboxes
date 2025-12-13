from cog import BasePredictor, Input, Path
import torch
import time
from diffusers import DiffusionPipeline

class Predictor(BasePredictor):
    def setup(self)->None:
        """Load the model into memory to make running multiple predictions efficient"""
        print("===SETUP START TIME===")
        start = time.time()

        print(f"[{time.time()- start:.2f}s] Loading pipeline...")
        #self.pipe = DiffusionPipeline.from_pretrained("my_diffusion_pipeline", torch_dtype=torch.float16)#, variant="fp16")#, local_files_only=True)
        #use directly from hf instead of donwloading first
        self.pipe = DiffusionPipeline.from_pretrained("Johnowhitaker/rainbowdiffusion", torch_dtype=torch.float16)
        if torch.cuda.is_available():
            print(f"[{time.time()- start:.2f}s] Moving to cuda...")
            self.pipe.to("cuda")
            
            #Enable memory efficient attention
            self.pipe.enable_attention_slicing()

            #enable xformers
            try: 
                self.pipe.enable_xformers_memory_efficient_attention()
            except:
                pass

            if hasattr(torch, 'complile'):
                self.pipe.unet = torch.compile(self.pipe.unet, mode="reduce-overhead")
            print(f"[{time.time()- start:.2f}s] SETUP COMPLETE...")
    
    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(
            description = "Input prompt",
            default="An owl wearing a hat",
        )) -> Path:
        """Run a single prediction on the model"""
        start = time.time()
        print("===SETUP START TIME===")

    
        image = self.pipe(prompt).images[0]
        print(f"[{time.time()- start:.2f}s] Inference Complete...")
        fn = f"{torch.rand(1).item()}_{prompt.replace('','_')}.png"
        print(f"[{time.time()- start:.2f}s] Saving Image...")
        image.save(fn)
        print(f"====PREDICT END: {time.time()-start:.2f}s ===") 
        return Path(fn)