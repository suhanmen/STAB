from __future__ import annotations

import sys
import json
from pathlib import Path

_CODE_DIR = Path(__file__).resolve().parents[1]
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

from utils.tags_information import TAG_TO_SCENARIO
from utils.algorithm_adversary_catalog import load_catalog

SKIP_TAGS = {"", "*special"}

def cf_to_scens(tags):
    out = set()
    for t in tags:
        if t in SKIP_TAGS:
            continue
        for s in TAG_TO_SCENARIO.get(t, []):
            out.add(s)
    return out

def keyword_match(catalog, problem_desc: str):
    text = (problem_desc or "").lower()
    strong = []
    for scen_name, scen in catalog['scenarios'].items():
        det = scen.get('detection', {}) or {}
        sk = [k.lower() for k in det.get('strong_keywords', [])]
        if any(k in text for k in sk):
            strong.append(scen_name)
    return strong

def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache_dir", default="~/.cache/huggingface/datasets")
    ap.add_argument("--output", default=None)
    ap.add_argument("--splits", default="test,valid",
                    help="Comma-separated splits (default: test,valid). "
                         "Use 'train' for train-based anchor pool.")
    args = ap.parse_args()

    splits = [s.strip() for s in args.splits.split(",")]
    base_dir = Path(__file__).resolve().parents[2]
    if args.output:
        out_path = Path(args.output)
    elif splits == ["train"]:
        out_path = base_dir / "dataset" / "scenario_knn_cache" / "kw_anchor_meta_train.json"
    else:
        out_path = base_dir / "dataset" / "scenario_knn_cache" / "kw_anchor_meta.json"

    print(f"Loading dataset + catalog (splits={splits}) ...")
    from datasets import load_dataset
    ds = load_dataset("deepmind/code_contests", cache_dir=args.cache_dir)
    catalog = load_catalog()

    meta = {"version": "1.0", "splits": splits,
            "scenarios": list(catalog["scenarios"].keys()), "problems": {}}

    for split in splits:
        for idx, ex in enumerate(ds[split]):
            name = ex["name"]
            desc = ex.get("description", "") or ""
            kw = keyword_match(catalog, desc)
            cf = cf_to_scens(ex.get("cf_tags") or [])
            key = f"{split}/{idx}"
            meta["problems"][key] = {
                "name": name,
                "split": split,
                "idx": idx,
                "has_kw": bool(kw),
                "kw_scenarios": sorted(kw),
                "cf_tag_scenarios": sorted(cf),
            }
        print(f"  {split}: {len(ds[split])} problems processed")

    meta["index_by_name"] = {p["name"]: k for k, p in meta["problems"].items()}
    n_kw = sum(1 for p in meta["problems"].values() if p["has_kw"])
    n_cf = sum(1 for p in meta["problems"].values() if p["cf_tag_scenarios"])
    meta["stats"] = {
        "total": len(meta["problems"]),
        "with_kw": n_kw,
        "with_cf_tags_scenario": n_cf,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n→ {out_path}")
    print(f"  total problems: {meta['stats']['total']}")
    print(f"  with KW match:  {meta['stats']['with_kw']}  (anchor pool candidates)")
    print(f"  with cf_tags scenario: {meta['stats']['with_cf_tags_scenario']}")

if __name__ == "__main__":
    main()
