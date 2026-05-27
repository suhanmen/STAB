from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CPP_LANG_CODE = 2

def _short_id(full_name: str) -> str:
    return full_name.split(".", 1)[0].strip()

def _build_index(dataset) -> dict:
    index = {}
    for i, row in enumerate(dataset):
        sid = _short_id(row["name"])
        if sid in index:
            print(f"[WARN] duplicate short_id '{sid}' in dataset; first occurrence kept",
                  file=sys.stderr)
            continue
        tl = row.get("time_limit") or {}
        secs = float(tl.get("seconds", 0)) + float(tl.get("nanos", 0)) / 1e9
        index[sid] = (
            i,
            row["name"],
            list(row["solutions"]["language"]),
            int(round(secs)) if secs > 0 else 1,
        )
    return index

def _build_records(picks: dict, ds_index: dict) -> tuple[list[dict], list[str]]:
    records = []
    warnings = []
    for sid, pick_list in picks.items():
        if not pick_list:
            warnings.append(f"{sid}: empty picks list")
            continue
        first = pick_list[0]
        sol_idx = int(first["sol_idx"])
        ds_entry = ds_index.get(sid)
        if ds_entry is None:
            warnings.append(f"{sid}: not present in dataset split (skipped)")
            continue
        row_index, full_name, lang_codes, timelimit = ds_entry
        if sol_idx >= len(lang_codes):
            warnings.append(f"{sid}: sol_idx {sol_idx} out of range "
                            f"(have {len(lang_codes)} solutions; skipped)")
            continue
        if lang_codes[sol_idx] != CPP_LANG_CODE:
            warnings.append(f"{sid}: sol_idx {sol_idx} is language code "
                            f"{lang_codes[sol_idx]}, expected cpp ({CPP_LANG_CODE}); skipped")
            continue
        records.append({
            "type": "problem",
            "index": row_index,
            "name": full_name,
            "timelimit": timelimit,
            "sampled_test_cases": 0,
            "solutions": {
                "cpp": [{
                    "sol_idx": sol_idx,
                    "verdict": "AC",
                    "max_run_time": 0.0,
                    "runs": [],
                }],
            },
        })
    records.sort(key=lambda r: r["index"])
    return records, warnings

def _write_jsonl(out_path: Path, split: str, records: list[dict], strategy: str):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "type": "metadata",
        "split": split,
        "strategy": strategy,
        "source": "wedge_picks",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tc_sample_ratio": 0.0,
        "tc_largest_ratio": 0.0,
        "num_problems": len(records),
        "total_ac": len(records),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--picks_json", required=True,
                   help="Path to wedge_picks_{split}.json (used by both wedge and evalperf_sas)")
    p.add_argument("--split", required=True, choices=["train", "valid", "test"])
    p.add_argument("--output_dir", required=True,
                   help="Directory to write selected_solutions_{strategy}_{split}.jsonl files")
    p.add_argument("--strategies", nargs="+",
                   default=["wedge_solution", "evalperf_sas_solution"],
                   help="Strategy names (one output file per name; same content)")
    p.add_argument("--cache_dir", default="~/.cache/huggingface/datasets",
                   help="HuggingFace dataset cache directory")
    p.add_argument("--overwrite", action="store_true",
                   help="Overwrite existing output files (default: skip)")
    args = p.parse_args()

    picks_path = Path(args.picks_json)
    if not picks_path.is_file():
        print(f"[ERROR] picks file not found: {picks_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_paths = {
        s: out_dir / f"selected_solutions_{s}_{args.split}.jsonl"
        for s in args.strategies
    }
    if not args.overwrite:
        existing = [p for p in out_paths.values() if p.exists()]
        if len(existing) == len(out_paths):
            print(f"[OK] all outputs already exist (use --overwrite to rebuild):")
            for p in existing:
                print(f"  {p}")
            return

    print(f"[INFO] loading picks from {picks_path}")
    with open(picks_path, encoding="utf-8") as f:
        picks_doc = json.load(f)
    picks = picks_doc.get("picks", picks_doc)
    print(f"[INFO] loaded {len(picks)} pick entries")

    print(f"[INFO] loading code_contests split={args.split} (cache_dir={args.cache_dir})")
    from datasets import load_dataset
    dataset = load_dataset("deepmind/code_contests", split=args.split, cache_dir=args.cache_dir)
    print(f"[INFO] dataset loaded: {len(dataset)} rows")

    print(f"[INFO] indexing dataset by short_id ...")
    ds_index = _build_index(dataset)
    print(f"[INFO] indexed {len(ds_index)} problems")

    print(f"[INFO] building selected_solutions records ...")
    records, warnings = _build_records(picks, ds_index)
    if warnings:
        print(f"[WARN] {len(warnings)} pick(s) skipped:")
        for w in warnings[:10]:
            print(f"  - {w}")
        if len(warnings) > 10:
            print(f"  ... ({len(warnings) - 10} more)")
    print(f"[INFO] {len(records)} valid records (cpp picks)")

    if not records:
        print("[ERROR] no valid records produced; aborting", file=sys.stderr)
        sys.exit(2)

    for strategy, out_path in out_paths.items():
        if out_path.exists() and not args.overwrite:
            print(f"[SKIP] exists: {out_path}")
            continue
        _write_jsonl(out_path, args.split, records, strategy)
        print(f"[OK] wrote {len(records)} records to {out_path}")

if __name__ == "__main__":
    main()
