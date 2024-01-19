from io import BytesIO
import requests
#import gradio as gr
import requests
import torch
from tqdm import tqdm
from PIL import Image, ImageOps
from diffusers import StableDiffusionInpaintPipeline
from torchvision.transforms import ToPILImage
import torchvision.transforms as T
from typing import Union, List, Optional, Callable
import numpy as np
from utils import preprocess, prepare_mask_and_masked_image, recover_image, resize_and_crop, prepare_image
import replicate
import os
import json
import base64
import io

REPLICATE_API_TOKEN="r8_KkNEhPUJvKV1FDQNoDNVLSg8pgHZLJE2fZCaK"
os.environ["REPLICATE_API_TOKEN"]=REPLICATE_API_TOKEN
topil = ToPILImage()
to_pil = T.ToPILImage()

pipe_inpaint = StableDiffusionInpaintPipeline.from_pretrained(
    "runwayml/stable-diffusion-inpainting" ,
    revision="fp16",
    torch_dtype=torch.float16
)
pipe_inpaint = pipe_inpaint.to("cuda")
#pipe_inpaint = pipe_inpaint.to("mps")
#pipe_inpaint = pipe_inpaint.to("cpu")
## Good params for editing that we used all over the paper --> decent quality and speed   
GUIDANCE_SCALE = 7.5
NUM_INFERENCE_STEPS = 100
DEFAULT_SEED = 1234

def pgd(X, targets, model, criterion, eps=0.1, step_size=0.015, iters=40, clamp_min=0, clamp_max=1, mask=None):
    X_adv = X.clone().detach() + (torch.rand(*X.shape)*2*eps-eps).cuda()
    pbar = tqdm(range(iters))
    for i in pbar:
        actual_step_size = step_size - (step_size - step_size / 100) / iters * i  
        X_adv.requires_grad_(True)

        loss = (model(X_adv).latent_dist.mean - targets).norm()
        pbar.set_description(f"Loss {loss.item():.5f} | step size: {actual_step_size:.4}")

        grad, = torch.autograd.grad(loss, [X_adv])
        
        X_adv = X_adv - grad.detach().sign() * actual_step_size
        X_adv = torch.minimum(torch.maximum(X_adv, X - eps), X + eps)
        X_adv.data = torch.clamp(X_adv, min=clamp_min, max=clamp_max)
        X_adv.grad = None    
        
        if mask is not None:
            X_adv.data *= mask
            
    return X_adv

def get_target(src):
    target_url = src
    response = requests.get(target_url)
    target_image = Image.open(BytesIO(response.content)).convert("RGB")
    target_image = target_image.resize((512, 512))
    return target_image

def immunize_fn(init_image, mask_image,target):
    with torch.autocast('cuda'):
        mask, X = prepare_mask_and_masked_image(init_image, mask_image)
        X = X.half().cuda()
        mask = mask.half().cuda()

        targets = pipe_inpaint.vae.encode(preprocess(target).half().cuda()).latent_dist.mean

        adv_X = pgd(X, 
                    targets = targets,
                    model=pipe_inpaint.vae.encode, 
                    criterion=torch.nn.MSELoss(), 
                    clamp_min=-1, 
                    clamp_max=1,
                    eps=0.12, 
                    step_size=0.01, 
                    iters=200,
                    mask=1-mask
                   )

        adv_X = (adv_X / 2 + 0.5).clamp(0, 1)
        
        adv_image = topil(adv_X[0]).convert("RGB")
        adv_image = recover_image(adv_image, init_image, mask_image, background=True)
        return adv_image        


def encoder_attack(image, mask,target):
    #init_image = Image.fromarray(image)
    init_image =image
    init_image = resize_and_crop(init_image, (512,512))
    mask_image = mask.convert('RGB')
    mask_image = resize_and_crop(mask_image, init_image.size)
    target_image = target.convert('RGB')
    target_image = resize_and_crop(target_image, init_image.size)
    immunized_image = immunize_fn(init_image, mask_image,target_image)

    return immunized_image

def diffusion(image, mask,prompt):
    #init_image = Image.fromarray(image)
    init_image =image
    init_image = resize_and_crop(init_image, (512,512))
    #mask_image = ImageOps.invert(Image.fromarray(mask).convert('RGB'))
    mask_image = mask.convert('RGB')
    mask_image = resize_and_crop(mask_image, init_image.size)

    image_edited = pipe_inpaint(prompt=prompt, 
                         image=init_image, 
                         mask_image=mask_image, 
                         height = init_image.size[0],
                         width = init_image.size[1],
                         eta=1,
                         guidance_scale=GUIDANCE_SCALE,
                         num_inference_steps=NUM_INFERENCE_STEPS,
                        ).images[0]
        
    image_edited = recover_image(image_edited, init_image, mask_image)

    return image_edited
# A differentiable version of the forward function of the inpainting stable diffusion model! See https://github.com/huggingface/diffusers
def attack_forward(
        self,
        prompt: Union[str, List[str]],
        masked_image: Union[torch.FloatTensor, Image.Image],
        mask: Union[torch.FloatTensor, Image.Image],
        height: int = 512,
        width: int = 512,
        num_inference_steps: int = 50,
        guidance_scale: float = 7.5,
        eta: float = 0.0,
    ):

        text_inputs = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            return_tensors="pt",
        )
        text_input_ids = text_inputs.input_ids
        text_embeddings = self.text_encoder(text_input_ids.to(self.device))[0]

        uncond_tokens = [""]
        max_length = text_input_ids.shape[-1]
        uncond_input = self.tokenizer(
            uncond_tokens,
            padding="max_length",
            max_length=max_length,
            truncation=True,
            return_tensors="pt",
        )
        uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]
        seq_len = uncond_embeddings.shape[1]
        text_embeddings = torch.cat([uncond_embeddings, text_embeddings])
        
        text_embeddings = text_embeddings.detach()

        num_channels_latents = self.vae.config.latent_channels
        
        latents_shape = (1 , num_channels_latents, height // 8, width // 8)
        latents = torch.randn(latents_shape, device=self.device, dtype=text_embeddings.dtype)

        mask = torch.nn.functional.interpolate(mask, size=(height // 8, width // 8))
        mask = torch.cat([mask] * 2)

        masked_image_latents = self.vae.encode(masked_image).latent_dist.sample()
        masked_image_latents = 0.18215 * masked_image_latents
        masked_image_latents = torch.cat([masked_image_latents] * 2)

        latents = latents * self.scheduler.init_noise_sigma
        
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps_tensor = self.scheduler.timesteps.to(self.device)

        for i, t in enumerate(timesteps_tensor):
            latent_model_input = torch.cat([latents] * 2)
            latent_model_input = torch.cat([latent_model_input, mask, masked_image_latents], dim=1)
            noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample
            noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
            noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
            latents = self.scheduler.step(noise_pred, t, latents, eta=eta).prev_sample

        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents).sample
        return image

    
def compute_grad(cur_mask, cur_masked_image, prompt, target_image, **kwargs):
    torch.set_grad_enabled(True)
    cur_mask = cur_mask.clone()
    cur_masked_image = cur_masked_image.clone()
    cur_mask.requires_grad = False
    cur_masked_image.requires_grad_()
    image_nat = attack_forward(pipe_inpaint,mask=cur_mask,
                               masked_image=cur_masked_image,
                               prompt=prompt,
                               **kwargs)
    
    loss = (image_nat - target_image).norm(p=2)
    grad = torch.autograd.grad(loss, [cur_masked_image])[0] * (1 - cur_mask)
        
    return grad, loss.item(), image_nat.data.cpu()

def super_l2(cur_mask, X, prompt, step_size, iters, eps, clamp_min, clamp_max, grad_reps = 5, target_image = 0, **kwargs):
    X_adv = X.clone()
    iterator = tqdm(range(iters))
    for i in iterator:

        all_grads = []
        losses = []
        for i in range(grad_reps):
            c_grad, loss, last_image = compute_grad(cur_mask, X_adv, prompt, target_image=target_image, **kwargs)
            all_grads.append(c_grad)
            losses.append(loss)
        grad = torch.stack(all_grads).mean(0)
        
        iterator.set_description_str(f'AVG Loss: {np.mean(losses):.3f}')

        l = len(X.shape) - 1
        grad_norm = torch.norm(grad.detach().reshape(grad.shape[0], -1), dim=1).view(-1, *([1] * l))
        grad_normalized = grad.detach() / (grad_norm + 1e-10)

        # actual_step_size = step_size - (step_size - step_size / 100) / iters * i
        actual_step_size = step_size
        X_adv = X_adv - grad_normalized * actual_step_size

        d_x = X_adv - X.detach()
        d_x_norm = torch.renorm(d_x, p=2, dim=0, maxnorm=eps)
        X_adv.data = torch.clamp(X + d_x_norm, clamp_min, clamp_max)        
    
    torch.cuda.empty_cache()

    return X_adv, last_image

def super_linf(cur_mask, X, prompt, step_size, iters, eps, clamp_min, clamp_max, grad_reps = 5, target_image = 0, **kwargs):
    X_adv = X.clone()
    iterator = tqdm(range(iters))
    for i in iterator:

        all_grads = []
        losses = []
        for i in range(grad_reps):
            c_grad, loss, last_image = compute_grad(cur_mask, X_adv, prompt, target_image=target_image, **kwargs)
            all_grads.append(c_grad)
            losses.append(loss)
        grad = torch.stack(all_grads).mean(0)
        
        iterator.set_description_str(f'AVG Loss: {np.mean(losses):.3f}')
        
        # actual_step_size = step_size - (step_size - step_size / 100) / iters * i
        actual_step_size = step_size
        X_adv = X_adv - grad.detach().sign() * actual_step_size

        X_adv = torch.minimum(torch.maximum(X_adv, X - eps), X + eps)
        X_adv.data = torch.clamp(X_adv, min=clamp_min, max=clamp_max)
        
    torch.cuda.empty_cache()

    return X_adv, last_image

def diffusion_attack(image, mask,target):
    init_image =image
    init_image = resize_and_crop(init_image, (512,512))
    mask_image = mask.convert('RGB')
    mask_image = resize_and_crop(mask_image, init_image.size)
    target_image = target.convert('RGB')
    target_image = resize_and_crop(target_image, init_image.size)
    strength = 0.5
    guidance_scale = 7.5
    num_inference_steps = 3
    torch.cuda.empty_cache() #free gpu space because the following logic is going to be heavy
    cur_mask, cur_masked_image = prepare_mask_and_masked_image(init_image, mask_image)

    cur_mask = cur_mask.half().cuda()
    cur_masked_image = cur_masked_image.half().cuda()
    target_image_tensor = prepare_image(target_image)
    target_image_tensor = 0*target_image_tensor.cuda() # we can either attack towards a target image or simply the zero tensor

    result, last_image= super_l2(cur_mask, cur_masked_image,
                  prompt="",
                  target_image=target_image_tensor,
                  eps=20,
                  step_size=1,
                  iters=20,
                  clamp_min = -1,
                  clamp_max = 1,
                  eta=1,
                  num_inference_steps=num_inference_steps,
                  guidance_scale=guidance_scale,
                  grad_reps=4
                 )
    adv_X = (result / 2 + 0.5).clamp(0, 1)
    adv_image = to_pil(adv_X[0]).convert("RGB")
    adv_image = recover_image(adv_image, init_image, mask_image, background=True)
    return adv_image


def styleGan(img,neutral_input,promt):
    data = io.BytesIO()
    img.save(data, "PNG")
    encoded_image=base64.b64encode(data.getvalue())
    image_base64 = encoded_image.decode('utf-8')
    # Create a data URI
    data_uri = f"data:image/jpeg;base64,{image_base64}"
    output = replicate.run(
    "orpatashnik/styleclip:7af9a66f36f97fee2fece7dcc927551a951f0022cbdd23747b9212f23fc17021",
    input={"input": data_uri,"neutral": neutral_input,"target":promt,"manipulation_strength": 5})
    result=get_target(output)
    return result
