from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

_CODE_DIR = Path(__file__).resolve().parents[1]
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from utils.tags_information import TAG_TO_SCENARIO

SKIP_TAGS = {"", "*special"}

def cf_to_scens(tags):
    out = set()
    for t in tags:
        if t in SKIP_TAGS:
            continue
        for s in TAG_TO_SCENARIO.get(t, []):
            out.add(s)
    return out

def build_anchors(ds_train, per_scen: int, seed: int = 42):
    rng = random.Random(seed)
    scen_pool: dict = defaultdict(list)
    seen_idx_to_meta = {}
    for idx, ex in enumerate(ds_train):
        tags = ex.get("cf_tags") or []
        scens = cf_to_scens(tags)
        if not scens:
            continue
        pd = ex.get("description") or ""
        meta = (idx, ex.get("name", f"idx_{idx}"), pd, scens)
        seen_idx_to_meta[idx] = meta
        for s in scens:
            scen_pool[s].append(idx)

    print(f"  Per-scenario candidate pool size:")
    for s in sorted(scen_pool):
        print(f"    {s:25s}  {len(scen_pool[s]):>5}")
    print()

    selected_idx = set()
    for s, pool in scen_pool.items():
        sample_size = min(per_scen, len(pool))
        sampled = rng.sample(pool, sample_size)
        selected_idx.update(sampled)
    anchors = [seen_idx_to_meta[i] for i in sorted(selected_idx)]
    return anchors

def encode_texts(texts, model_name: str, batch_size: int = 32):
    from sentence_transformers import SentenceTransformer
    print(f"  Loading model: {model_name}")
    model = SentenceTransformer(model_name)
    print(f"  Encoding {len(texts)} texts (batch={batch_size}) ...")
    embs = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    return embs

def build_kw_all_anchors(ds_train, meta_path: Path):
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    idx_to_ex = {}
    for idx, ex in enumerate(ds_train):
        idx_to_ex[idx] = ex

    anchors = []
    for key, p in meta["problems"].items():
        if not p["has_kw"]:
            continue
        idx = p["idx"]
        ex = idx_to_ex.get(idx)
        if ex is None:
            continue
        desc = ex.get("description") or ""
        anchors.append((idx, p["name"], desc, set(p["kw_scenarios"])))

    anchors.sort(key=lambda x: x[0])
    return anchors

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache_dir", default="~/.cache/huggingface/datasets")
    ap.add_argument("--per_scen", type=int, default=100,
                    help="Max anchors per scenario (default 100, legacy mode)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--model", default="all-mpnet-base-v2")
    ap.add_argument("--batch_size", type=int, default=32)
    ap.add_argument("--output_dir", default=None)
    ap.add_argument("--kw_all", action="store_true",
                    help="Embed all KW-matched train problems (uses SFR model)")
    args = ap.parse_args()

    base_dir = Path(__file__).resolve().parents[2]
    out_dir = Path(args.output_dir) if args.output_dir else (base_dir / "dataset" / "scenario_knn_cache")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.kw_all:
        args.model = "Salesforce/SFR-Embedding-2_R"

    model_slug = args.model.replace("/", "__")

    print(f"Loading dataset (cache_dir={args.cache_dir}) ...")
    from datasets import load_dataset
    ds = load_dataset("deepmind/code_contests", cache_dir=args.cache_dir)
    print(f"  train size: {len(ds['train'])}")
    print()

    if args.kw_all:
        meta_path = out_dir / "kw_anchor_meta_train.json"
        if not meta_path.exists():
            raise FileNotFoundError(
                f"{meta_path} not found. Run "
                "`python code/utils/build_kw_anchor_meta.py --splits train` first")
        print("KW-all mode: loading KW-matched train problems ...")
        anchors = build_kw_all_anchors(ds["train"], meta_path)
        out_path = out_dir / "anchors_train_kw_all_SFR.npz"
    else:
        print(f"per-scenario sampling (max {args.per_scen} each, seed={args.seed}) ...")
        anchors = build_anchors(ds["train"], per_scen=args.per_scen, seed=args.seed)
        out_path = out_dir / f"anchors_train_{model_slug}.npz"

    print(f"  Final anchor count: {len(anchors)}")

    label_counts = Counter()
    for _, _, _, scens in anchors:
        for s in scens:
            label_counts[s] += 1
    print(f"  Per-scenario anchor count:")
    for s in sorted(label_counts):
        print(f"    {s:25s}  {label_counts[s]:>5}")
    print()

    indices = [a[0] for a in anchors]
    names = [a[1] for a in anchors]
    descs = [a[2] for a in anchors]
    labels = [sorted(a[3]) for a in anchors]

    import numpy as np
    embeddings = encode_texts(descs, args.model, args.batch_size)
    embeddings = np.asarray(embeddings, dtype=np.float32)

    np.savez(
        out_path,
        indices=np.asarray(indices, dtype=np.int64),
        names=np.asarray(names, dtype=object),
        labels=np.asarray(labels, dtype=object),
        embeddings=embeddings,
    )
    print(f"\n→ Saved: {out_path}")
    print(f"   embeddings: {embeddings.shape}")

if __name__ == "__main__":
    main()
