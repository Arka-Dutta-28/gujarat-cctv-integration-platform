"""Train a plate-only super-resolution network (plan section 5b, step C).

A general super-resolution model (Real-ESRGAN, step B) was rejected on 14 Sep
2026: on the 41 hand-verified plates it took exact reads from 5 to 2 and nearly
doubled confidently wrong reads, from 14 to 26. This trains a small 4x network
on nothing but plates, from scripts/synth_plates.py pairs: the damaged crop
exactly as the recogniser sees it, and the same plate at the same tilt and
brightness, undamaged, at four times the size.

Rules fixed before the first run:

  - The checkpoint is chosen by PSNR on 500 seeded synthetic pairs. The real
    test plates are never used to choose.
  - After training, the chosen network enlarges each test crop once and writes
    it to <out>/test_sr/. Scoring is scripts/score_crops_ocr.py, with the
    production Tesseract reader, against the original crops on the same plates.
  - It is worth keeping only if stored exact reads rise and confidently wrong
    reads do not.

The network adds detail to a bicubic enlargement rather than drawing from
scratch (upscale plus residual), so with nothing learned it is bicubic.

    PYTHONPATH=. CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=1 \
      <python-with-torch> -m scripts.train_sr --out <run-dir> --steps 20000
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import DataLoader, IterableDataset

from scripts.synth_plates import pair

SCALE = 4
#: Training patch, in damaged pixels. Real crops are 10-52 px tall, so a patch
#: much taller than 16 would mostly be padding.
PATCH_H, PATCH_W = 16, 32


def _pad_to(img: np.ndarray, h: int, w: int) -> np.ndarray:
    ph, pw = max(0, h - img.shape[0]), max(0, w - img.shape[1])
    return cv2.copyMakeBorder(img, 0, ph, 0, pw, cv2.BORDER_REPLICATE) if ph or pw else img


class Pairs(IterableDataset):
    def __init__(self, seed: int) -> None:
        self.seed = seed

    def __iter__(self):
        info = torch.utils.data.get_worker_info()
        rng = random.Random(self.seed * 7919 + (info.id if info else 0))
        cv2.setNumThreads(1)
        while True:
            damaged, clean, _ = pair(rng, SCALE)
            damaged = _pad_to(damaged, PATCH_H, PATCH_W)
            clean = _pad_to(clean, damaged.shape[0] * SCALE, damaged.shape[1] * SCALE)
            y = rng.randint(0, damaged.shape[0] - PATCH_H)
            x = rng.randint(0, damaged.shape[1] - PATCH_W)
            lo = damaged[y:y + PATCH_H, x:x + PATCH_W]
            hi = clean[y * SCALE:(y + PATCH_H) * SCALE, x * SCALE:(x + PATCH_W) * SCALE]
            yield (torch.from_numpy(lo.astype(np.float32) / 255)[None],
                   torch.from_numpy(hi.astype(np.float32) / 255)[None])


class PlateSR(nn.Module):
    """Residual CNN, pixel-shuffle ×4, on top of bicubic. ~1.2 M parameters."""

    def __init__(self, channels: int = 64, blocks: int = 12) -> None:
        super().__init__()
        self.head = nn.Conv2d(1, channels, 3, padding=1)
        self.body = nn.ModuleList(
            nn.Sequential(nn.Conv2d(channels, channels, 3, padding=1), nn.ReLU(inplace=True),
                          nn.Conv2d(channels, channels, 3, padding=1))
            for _ in range(blocks))
        def up2():
            return [nn.Conv2d(channels, channels * 4, 3, padding=1), nn.PixelShuffle(2),
                    nn.ReLU(inplace=True)]

        self.up = nn.Sequential(*up2(), *up2(), nn.Conv2d(channels, 1, 3, padding=1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        base = F.interpolate(x, scale_factor=SCALE, mode="bicubic", align_corners=False)
        f = h = self.head(x)
        for block in self.body:
            h = h + 0.2 * block(h)
        return (base + self.up(h + f)).clamp(0, 1)


def psnr(a: torch.Tensor, b: torch.Tensor) -> float:
    mse = F.mse_loss(a, b).item()
    return 10 * math.log10(1 / max(mse, 1e-10))


@torch.no_grad()
def enlarge(model: nn.Module, grey: np.ndarray, device: str) -> np.ndarray:
    x = torch.from_numpy(grey.astype(np.float32) / 255)[None, None].to(device)
    return (model(x)[0, 0].cpu().numpy() * 255).round().astype(np.uint8)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=Path("data/corpus"))
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--val-every", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log = (args.out / "log.jsonl").open("a")

    def note(**kw):
        kw["t"] = round(time.time(), 1)
        log.write(json.dumps(kw) + "\n")
        log.flush()
        print(json.dumps(kw), flush=True)

    vrng = random.Random(424242)
    val = [pair(vrng, SCALE)[:2] for _ in range(500)]
    bicubic = sum(psnr(
        torch.from_numpy(cv2.resize(d, (c.shape[1], c.shape[0]), interpolation=cv2.INTER_CUBIC)
                         .astype(np.float32) / 255),
        torch.from_numpy(c.astype(np.float32) / 255)) for d, c in val) / len(val)
    note(event="start", device=device, val_bicubic_psnr=round(bicubic, 3),
         **{k: str(v) for k, v in vars(args).items()})

    model = PlateSR().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.steps)
    loader = DataLoader(Pairs(args.seed), batch_size=args.batch, num_workers=args.workers,
                        persistent_workers=True, prefetch_factor=4,
                        multiprocessing_context="spawn")  # see train_recogniser.py

    best, started = -1.0, time.time()
    for step, (lo, hi) in enumerate(loader, 1):
        lo, hi = lo.to(device, non_blocking=True), hi.to(device, non_blocking=True)
        loss = F.l1_loss(model(lo), hi)
        opt.zero_grad(set_to_none=True)
        loss.backward()
        opt.step()
        sched.step()
        if step % 200 == 0 and step % args.val_every:
            note(step=step, l1=round(loss.item(), 5),
                 patches_per_s=round(step * args.batch / (time.time() - started)))
        if step % args.val_every == 0 or step == args.steps:
            model.eval()
            score = sum(
                psnr(torch.from_numpy(enlarge(model, d, device).astype(np.float32) / 255),
                     torch.from_numpy(c.astype(np.float32) / 255))
                for d, c in val) / len(val)
            model.train()
            note(step=step, l1=round(loss.item(), 5), val_psnr=round(score, 3),
                 over_bicubic=round(score - bicubic, 3))
            if score > best:
                best = score
                torch.save({"model": model.state_dict(), "step": step, "val_psnr": score,
                            "val_bicubic_psnr": bicubic}, args.out / "best.pt")
        if step >= args.steps:
            break

    # The one pass over the real test crops. Written, not scored: scoring needs
    # Tesseract, which lives in the ANPR image (see scripts/score_crops_ocr.py).
    from scripts.review_server import verified_plates

    ckpt = torch.load(args.out / "best.pt", map_location=device)
    model.load_state_dict(ckpt["model"])
    model.eval()
    out = args.out / "test_sr"
    out.mkdir(exist_ok=True)
    plates = verified_plates(args.corpus)
    for r in plates:
        grey = cv2.imread(str(args.corpus / r["image"]), cv2.IMREAD_GRAYSCALE)
        cv2.imwrite(str(out / f"{r['sighting_id']}.png"), enlarge(model, grey, device))
    note(event="done", checkpoint_step=ckpt["step"], val_psnr=round(ckpt["val_psnr"], 3),
         val_bicubic_psnr=round(bicubic, 3), test_crops_written=len(plates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
