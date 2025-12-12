from cog import BasePredictor, Input, Path
import torch
from diffusers import DiffusionPipeline

class Predictor(BasePredictor):
    def setup(self)->None:
        """Load the model into memory to make running multiple predictions efficient"""
        self.pipe = DiffusionPipeline.from_pretrained("my_diffusion_pipeline", torch_dtype=torch.float32)#, local_files_only=True)
        if torch.cuda.is_available():
            self.pipe.to("cuda")
    
    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(
            description = "Input prompt",
            default="An owl wearing a hat",
        )) -> Path:
        """Run a single prediction on the model"""
        image = self.pipe(prompt).images[0]
        fn = f"{torch.rand(1).item()}_{prompt.replace('','_')}.png"
        image.save(fn)
        return Path(fn)