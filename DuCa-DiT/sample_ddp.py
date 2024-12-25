# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Samples a large number of images from a pre-trained DiT model using DDP.
Subsequently saves a .npz file that can be used to compute FID and other
evaluation metrics via the ADM repo: https://github.com/openai/guided-diffusion/tree/main/evaluations

For a simple single-GPU/CPU sampling script, see sample.py.
"""
import torch
import torch.distributed as dist
from models import DiT_models
from download import find_model
from diffusion import create_diffusion
from diffusers.models import AutoencoderKL
from tqdm import tqdm
import os
from PIL import Image
import numpy as np
import math
import argparse


def create_npz_from_sample_folder(sample_dir, num=50_000):
    """
    Builds a single .npz file from a folder of .png samples.
    """
    samples = []
    for i in tqdm(range(num), desc="Building .npz file from samples"):
        sample_pil = Image.open(f"{sample_dir}/{i:06d}.png")
        sample_np = np.asarray(sample_pil).astype(np.uint8)
        samples.append(sample_np)
    samples = np.stack(samples)
    assert samples.shape == (num, samples.shape[1], samples.shape[2], 3)
    npz_path = f"{sample_dir}.npz"
    np.savez(npz_path, arr_0=samples)
    print(f"Saved .npz file to {npz_path} [shape={samples.shape}].")
    return npz_path

def main(args):
    """
    Run sampling.
    """

    torch.backends.cuda.matmul.allow_tf32 = args.tf32  # True: fast but may lead to some small numerical differences
    assert torch.cuda.is_available(), "Sampling with DDP requires at least one GPU. sample.py supports CPU-only usage"
    torch.set_grad_enabled(False)

    # Setup DDP:
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    device = rank % torch.cuda.device_count()
    seed = args.global_seed * dist.get_world_size() + rank
    torch.manual_seed(seed)
    torch.cuda.set_device(device)
    print(f"Starting rank={rank}, seed={seed}, world_size={dist.get_world_size()}.")

    if args.ckpt is None:
        assert args.model == "DiT-XL/2", "Only DiT-XL/2 models are available for auto-download."
        assert args.image_size in [256, 512]
        assert args.num_classes == 1000

    # Load model:
    latent_size = args.image_size // 8
    model = DiT_models[args.model](
        input_size=latent_size,
        num_classes=args.num_classes
    ).to(device)
    # Auto-download a pre-trained model or load a custom DiT checkpoint from train.py:
    ckpt_path = args.ckpt or f"/root/autodl-tmp/pretrained_models/DiT-XL-2-{args.image_size}x{args.image_size}.pt"
    state_dict = find_model(ckpt_path)
    model.load_state_dict(state_dict)
    model.eval()  # important!
    diffusion = create_diffusion(str(args.num_sampling_steps))
    #vae = AutoencoderKL.from_pretrained(f"stabilityai/sd-vae-ft-{args.vae}").to(device)
    vae = AutoencoderKL.from_pretrained(f"/root/autodl-tmp/pretrained_models").to(device)
    assert args.cfg_scale >= 1.0, "In almost all cases, cfg_scale be >= 1.0"
    using_cfg = args.cfg_scale > 1.0
    #print("cfg scale = ", args.cfg_scale, flush=True)

    # Create folder to save samples:
    model_string_name = args.model.replace("/", "-")
    ckpt_string_name = os.path.basename(args.ckpt).replace(".pt", "") if args.ckpt else "pretrained"
    folder_name = f"Cache-Noise-{model_string_name}-{ckpt_string_name}-size-{args.image_size}-vae-{args.vae}-" \
                  f"cfg-{args.cfg_scale}-seed-{args.global_seed}-step-{args.num_sampling_steps}-num-{args.num_fid_samples}"\
                  f"-{args.cache_type}-{args.fresh_ratio}-{args.ratio_scheduler}-{args.force_fresh}-{args.fresh_threshold}"\
                  f"-softweight-{args.soft_fresh_weight}"
    sample_folder_dir = f"{args.sample_dir}/{folder_name}"
    if rank == 0:
        os.makedirs(sample_folder_dir, exist_ok=True)
        print(f"Saving .png samples at {sample_folder_dir}")
    dist.barrier()

    # Figure out how many samples we need to generate on each GPU and how many iterations we need to run:
    n = args.per_proc_batch_size
    global_batch_size = n * dist.get_world_size()
    # To make things evenly-divisible, we'll sample a bit more than we need and then discard the extra samples:
    total_samples = int(math.ceil(args.num_fid_samples / global_batch_size) * global_batch_size)
    if rank == 0:
        print(f"Total number of images that will be sampled: {total_samples}")
    assert total_samples % dist.get_world_size() == 0, "total_samples must be divisible by world_size"
    samples_needed_this_gpu = int(total_samples // dist.get_world_size())
    assert samples_needed_this_gpu % n == 0, "samples_needed_this_gpu must be divisible by the per-GPU batch size"
    iterations = int(samples_needed_this_gpu // n)
    pbar = range(iterations)
    pbar = tqdm(pbar) if rank == 0 else pbar
    total = 0

    for _ in pbar:
        # Sample inputs:
        z = torch.randn(n, model.in_channels, latent_size, latent_size, device=device)
        y = torch.randint(0, args.num_classes, (n,), device=device)

        # Setup classifier-free guidance:
        if using_cfg:
            z = torch.cat([z, z], 0)
            y_null = torch.tensor([1000] * n, device=device)
            y = torch.cat([y, y_null], 0)
            model_kwargs = dict(y=y, cfg_scale=args.cfg_scale)
            sample_fn = model.forward_with_cfg
        else:
            model_kwargs = dict(y=y)
            sample_fn = model.forward

        model_kwargs['cache_type']        = args.cache_type
        model_kwargs['fresh_ratio']       = args.fresh_ratio
        model_kwargs['force_fresh']       = args.force_fresh
        model_kwargs['fresh_threshold']   = args.fresh_threshold
        model_kwargs['ratio_scheduler']   = args.ratio_scheduler
        model_kwargs['soft_fresh_weight'] = args.soft_fresh_weight
        model_kwargs['test_FLOPs']        = args.test_FLOPs
        

        # Sample images:
        if args.ddim_sample:
            samples = diffusion.ddim_sample_loop(
                sample_fn, z.shape, z, clip_denoised=False, model_kwargs=model_kwargs, progress=False, device=device
            )
        else:
            samples = diffusion.p_sample_loop(
                sample_fn, z.shape, z, clip_denoised=False, model_kwargs=model_kwargs, progress=False, device=device,
            )
            
        if using_cfg:
            samples, _ = samples.chunk(2, dim=0)  # Remove null class samples

        samples = vae.decode(samples / 0.18215).sample
        samples = torch.clamp(127.5 * samples + 128.0, 0, 255).permute(0, 2, 3, 1).to("cpu", dtype=torch.uint8).numpy()

        # Save samples to disk as individual .png files
        for i, sample in enumerate(samples):
            index = i * dist.get_world_size() + rank + total
            Image.fromarray(sample).save(f"{sample_folder_dir}/{index:06d}.png")
        total += global_batch_size

    # Make sure all processes have finished saving their samples before attempting to convert to .npz
    dist.barrier()
    if rank == 0:
        create_npz_from_sample_folder(sample_folder_dir, args.num_fid_samples)
        print("Done.")
    dist.barrier()
    dist.destroy_process_group()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, choices=list(DiT_models.keys()), default="DiT-XL/2")
    parser.add_argument("--vae",  type=str, choices=["ema", "mse"], default="ema")
    parser.add_argument("--sample-dir", type=str, default="/root/autodl-tmp/samples") # Change this to your desired sample directory
    parser.add_argument("--per-proc-batch-size", type=int, default=32)
    parser.add_argument("--num-fid-samples", type=int, default=50_000)
    parser.add_argument("--image-size", type=int, choices=[256, 512], default=256)
    parser.add_argument("--num-classes", type=int, default=1000)
    parser.add_argument("--cfg-scale",  type=float, default=1.5)
    parser.add_argument("--num-sampling-steps", type=int, default=250)
    parser.add_argument("--global-seed", type=int, default=0)
    parser.add_argument("--tf32", action=argparse.BooleanOptionalAction, default=True,
                        help="By default, use TF32 matmuls. This massively accelerates sampling on Ampere GPUs.")
    parser.add_argument("--ckpt", type=str, default=None,
                        help="Optional path to a DiT checkpoint (default: auto-download a pre-trained DiT-XL/2 model).")
    parser.add_argument("--ddim-sample", action="store_true", default=False)
    parser.add_argument("--fresh-ratio", type=float, default=0.07)
    parser.add_argument("--cache-type", type=str, choices=['random', 'attention','similarity','norm', 'compress','kv-norm'], default='random') # only attention supported currently
    parser.add_argument("--ratio-scheduler", type=str, default='ToCa', choices=['linear', 'cosine', 'exp', 'constant','linear-mode','layerwise','ToCa']) #  'ToCa' is the proposed scheduler in Final version of the paper
    parser.add_argument("--force-fresh", type=str, choices=['global', 'local'], default='global', # only global is supported currently, local causes bad results
                        help="Force fresh strategy. global: fresh all tokens. local: fresh tokens acheiving fresh step threshold.")
    parser.add_argument("--fresh-threshold", type=int, default=4) # N in toca
    parser.add_argument("--soft-fresh-weight", type=float, default=0.25, # lambda_3 in toca
                        help="soft weight for updating the stale tokens by adding extra scores.")
    parser.add_argument("--test-FLOPs", action="store_true", default=False)
    #parser.add_argument("--merge-weight", type=float, default=0.0) # never used in toca, just for exploration

    args = parser.parse_args()
    main(args)

# 50 original
#Inception Score: 240.71978759765625
#FID: 8.931408839687833
#sFID: 34.64163271410109
#Precision: 0.8058
#Recall: 0.7394

# 50 cache noise-2
#FID: 9.69654743393869
#sFID: 34.94775009481066
#Precision: 0.7942
#Recall: 0.7392

# 50 cache noise-3
#Inception Score: 215.41249084472656
#FID: 11.025724426613124
#sFID: 35.28552175175628
#Precision: 0.7774
#Recall: 0.7268

# 25
#Inception Score: 229.67770385742188
#FID: 9.865083294427961
#sFID: 34.64141684527078
#Precision: 0.7928
#Recall: 0.7405

# 17
#Inception Score: 203.68312072753906
#FID: 11.40906817230325
#sFID: 35.29458246653974
#Precision: 0.7666
#Recall: 0.736

# 50 cache layer27 - 2 
#Inception Score: 229.546630859375
#FID: 9.590554703511089
#sFID: 34.75308927868082
#Precision: 0.8038
#Recall: 0.7372

#Inception Score: 230.4753875732422
#FID: 10.029959336465083
#sFID: 34.99507029423171
#Precision: 0.7874
#Recall: 0.7226

#10.73