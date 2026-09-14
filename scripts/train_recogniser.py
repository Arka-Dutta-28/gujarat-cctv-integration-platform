"""Train a plate recogniser on synthetic plates; score it once on real ones.

Stage 3 of the recogniser training plan, trained on Stage 1
(scripts/synth_plates.py). A small CRNN (convolutions, a bidirectional LSTM, CTC
loss), because the job is reading 7 to 11 characters from a 32 px strip, and a
model that small trains in minutes and runs on a CPU at the edge.

Evaluation discipline (plan section 6), enforced by construction:

  - The checkpoint is chosen on a fixed, seeded synthetic validation set only.
  - The 41 hand-verified government plates are the test set. They are read once,
    after training, by the chosen checkpoint, and never influence a choice.
  - The test is reported next to Tesseract's reads of the same crops, both raw
    and after normalise_plate, which is what search and alerts actually use.

Needs PyTorch, which the repository's .venv does not carry. Run it from an
environment that has it, with the repository on the path:

    PYTHONPATH=. CUDA_VISIBLE_DEVICES=1 <python-with-torch> -m scripts.train_recogniser \
        --out <work-dir>/runs/crnn-001 --steps 30000
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import time
from pathlib import Path

import cv2
import torch
from torch import nn
from torch.utils.data import DataLoader, IterableDataset

from scripts.synth_plates import CHARSET, INPUT_H, INPUT_W, sample, to_input
from services.common.plates import edit_distance, normalise_plate

BLANK = 0  # CTC blank; characters are 1..len(CHARSET)


class Synthetic(IterableDataset):
    """An endless stream of fresh fakes, seeded differently per worker."""

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def __iter__(self):
        info = torch.utils.data.get_worker_info()
        rng = random.Random(self.seed * 1000 + (info.id if info else 0))
        cv2.setNumThreads(1)
        while True:
            img, label = sample(rng)
            yield torch.from_numpy(to_input(img))[None], label


def collate(batch):
    images = torch.stack([b[0] for b in batch])
    labels = [b[1] for b in batch]
    targets = torch.tensor([CHARSET.index(c) + 1 for s in labels for c in s], dtype=torch.long)
    lengths = torch.tensor([len(s) for s in labels], dtype=torch.long)
    return images, targets, lengths, labels


class CRNN(nn.Module):
    """32x128 grey in, 32 time steps of character scores out."""

    def __init__(self, classes: int = len(CHARSET) + 1) -> None:
        super().__init__()

        def block(cin, cout, pool):
            return [nn.Conv2d(cin, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout),
                    nn.ReLU(inplace=True)] + ([nn.MaxPool2d(pool)] if pool else [])

        self.cnn = nn.Sequential(
            *block(1, 64, (2, 2)),       # 16x64
            *block(64, 128, (2, 2)),     # 8x32
            *block(128, 256, None),
            *block(256, 256, (2, 1)),    # 4x32
            *block(256, 384, None),
            *block(384, 384, (2, 1)),    # 2x32
            nn.Dropout2d(0.1),
        )
        self.rnn = nn.LSTM(384 * 2, 256, num_layers=2, bidirectional=True,
                           batch_first=True, dropout=0.2)
        self.head = nn.Linear(512, classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        f = self.cnn(x)                                  # B, C, 2, 32
        b, c, h, w = f.shape
        f = f.permute(0, 3, 1, 2).reshape(b, w, c * h)   # B, 32, C*2
        return self.head(self.rnn(f)[0])                 # B, 32, classes


def greedy_decode(logits: torch.Tensor) -> list[str]:
    """Collapse repeats, drop blanks: the standard CTC read-out."""
    out = []
    for seq in logits.argmax(-1).cpu().numpy():
        chars, prev = [], BLANK
        for k in seq:
            if k != prev and k != BLANK:
                chars.append(CHARSET[k - 1])
            prev = k
        out.append("".join(chars))
    return out


def fixed_validation(n: int, seed: int) -> tuple[torch.Tensor, list[str]]:
    rng = random.Random(seed)
    pairs = [sample(rng) for _ in range(n)]
    images = torch.stack([torch.from_numpy(to_input(i))[None] for i, _ in pairs])
    return images, [p for _, p in pairs]


def verified_test_set(corpus: Path) -> list[dict]:
    """The hand-verified plates, with pixels. Test only."""
    from scripts.review_server import verified_plates

    return [{**r, "pixels": cv2.imread(str(corpus / r["image"]), cv2.IMREAD_GRAYSCALE)}
            for r in verified_plates(corpus)]


def score(pairs: list[tuple[str, str]]) -> dict:
    d = [edit_distance(p, t) for p, t in pairs]
    n = len(d)
    return {"n": n, "exact": sum(x == 0 for x in d), "within_1": sum(x <= 1 for x in d),
            "within_2": sum(x <= 2 for x in d), "mean_edit": round(sum(d) / n, 2)}


@torch.no_grad()
def predict(model: nn.Module, images: torch.Tensor, device: str) -> list[str]:
    model.eval()
    reads = []
    for i in range(0, len(images), 512):
        reads += greedy_decode(model(images[i:i + 512].to(device)))
    model.train()
    return reads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=Path("data/corpus"))
    parser.add_argument("--steps", type=int, default=30000)
    parser.add_argument("--batch", type=int, default=256)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--workers", type=int, default=14)
    parser.add_argument("--val-every", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=1)
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

    val_x, val_y = fixed_validation(4000, seed=99991)
    note(event="start", device=device, **{k: str(v) for k, v in vars(args).items()})

    model = CRNN().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=args.steps,
                                                pct_start=0.05)
    ctc = nn.CTCLoss(blank=BLANK, zero_infinity=True)
    loader = DataLoader(Synthetic(args.seed), batch_size=args.batch, num_workers=args.workers,
                        collate_fn=collate, persistent_workers=True, prefetch_factor=4,
                        # spawn, not fork: the parent has already used OpenCV to
                        # build the validation set, and a forked child inherits
                        # its thread pool mid-state and deadlocks on the first
                        # resize. The smoke run hung exactly there.
                        multiprocessing_context="spawn")

    best, started = -1.0, time.time()
    for step, (x, targets, lengths, _) in enumerate(loader, 1):
        x = x.to(device, non_blocking=True)
        logits = model(x)                                        # B, T, C
        log_probs = logits.log_softmax(-1).permute(1, 0, 2)      # T, B, C
        in_lengths = torch.full((x.size(0),), log_probs.size(0), dtype=torch.long)
        loss = ctc(log_probs.float(), targets, in_lengths, lengths)
        opt.zero_grad(set_to_none=True)
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        opt.step()
        sched.step()

        if step % 100 == 0 and step % args.val_every:
            note(step=step, loss=round(loss.item(), 4),
                 img_per_s=round(step * args.batch / (time.time() - started)))
        if step % args.val_every == 0 or step == args.steps:
            reads = predict(model, val_x, device)
            acc = sum(r == t for r, t in zip(reads, val_y, strict=True)) / len(val_y)
            note(step=step, loss=round(loss.item(), 4), synth_val_exact=round(acc, 4))
            if acc > best:
                best = acc
                torch.save({"model": model.state_dict(), "step": step, "synth_val_exact": acc,
                            "charset": CHARSET, "input": [INPUT_H, INPUT_W],
                            "resize": "stretch"}, args.out / "best.pt")
        if step >= args.steps:
            break

    # --- the one look at real plates -------------------------------------
    ckpt = torch.load(args.out / "best.pt", map_location=device)
    model.load_state_dict(ckpt["model"])
    test = verified_test_set(args.corpus)
    x = torch.stack([torch.from_numpy(to_input(r["pixels"]))[None] for r in test])
    reads = predict(model, x, device)
    truth = [r["truth"] for r in test]
    tess_raw = [(r["read"] or "").replace(" ", "").upper() for r in test]

    results = {
        "checkpoint_step": ckpt["step"], "synth_val_exact": round(ckpt["synth_val_exact"], 4),
        "test": "hand-verified government plates, data/corpus",
        "crnn_raw": score(list(zip(reads, truth, strict=True))),
        "crnn_stored": score([(normalise_plate(p), t) for p, t in zip(reads, truth, strict=True)]),
        "tesseract_raw": score(list(zip(tess_raw, truth, strict=True))),
        "tesseract_stored": score(
            [(normalise_plate(p), t) for p, t in zip(tess_raw, truth, strict=True)]),
    }
    by_cond: dict[str, list] = {}
    for r, p in zip(test, reads, strict=True):
        by_cond.setdefault(r["condition"], []).append((normalise_plate(p), r["truth"]))
    results["crnn_stored_by_condition"] = {c: score(v) for c, v in by_cond.items()}
    (args.out / "results.json").write_text(json.dumps(results, indent=1))
    with (args.out / "test_predictions.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sighting_id", "condition", "plate_px", "truth", "crnn", "crnn_stored",
                    "tesseract", "tesseract_stored"])
        for r, p, tr in zip(test, reads, tess_raw, strict=True):
            w.writerow([r["sighting_id"], r["condition"], r["plate_px"], r["truth"], p,
                        normalise_plate(p), tr, normalise_plate(tr)])
    note(event="done", **results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
