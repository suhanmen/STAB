from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

TAG_TO_SCENARIO: Dict[str, List[str]] = {
    "sortings":                 ["sorting"],
    "dp":                       ["dp_recursion"],
    "divide and conquer":       ["dp_recursion"],
    "meet-in-the-middle":       ["dp_recursion"],
    "number theory":            ["number_theory"],
    "strings":                  ["string_matching"],
    "string suffix structures": ["string_matching"],
    "hashing":                  ["hashing"],
    "trees":                    ["tree_traversal"],
    "dfs and similar":          ["tree_traversal", "graph_shortest_path"],
    "shortest paths":           ["graph_shortest_path"],
    "graphs":                   ["graph_shortest_path"],
    "data structures":          ["bst_data_structure"],
    "dsu":                      ["bst_data_structure"],
    "binary search":            ["binary_search"],
    "two pointers":             ["two_pointers"],
    "bitmasks":                 ["bitmasks"],
    "combinatorics":            ["combinatorics"],
    "geometry":                 ["geometry"],
}

def load_dataset_lazy(cache_dir: str = "~/.cache/huggingface/datasets"):
    from datasets import load_dataset
    return load_dataset("deepmind/code_contests", cache_dir=cache_dir)

def collect_tag_stats(ds, splits: Sequence[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for s in splits:
        per_split = {
            "n_problems": 0,
            "n_with_tags": 0,
            "n_no_tags": 0,
            "tag_counts": Counter(),
            "problems": [],
        }
        for idx, ex in enumerate(ds[s]):
            per_split["n_problems"] += 1
            tags = ex.get("cf_tags") or []
            if tags:
                per_split["n_with_tags"] += 1
                for t in tags:
                    per_split["tag_counts"][t] += 1
            else:
                per_split["n_no_tags"] += 1
            per_split["problems"].append((idx, ex["name"], list(tags)))
        out[s] = per_split
    return out

def write_distribution(stats: Dict[str, dict], out_path: Path) -> None:
    splits = list(stats.keys())
    all_tags: set = set()
    for s in splits:
        all_tags.update(stats[s]["tag_counts"].keys())

    total = Counter()
    for s in splits:
        for t, c in stats[s]["tag_counts"].items():
            total[t] += c

    lines: List[str] = []
    lines.append("CF_TAGS DISTRIBUTION  (deepmind/code_contests)")
    lines.append("=" * 70)
    lines.append("")

    lines.append(f'{"Split":8} {"Total":>10} {"With tags":>10} {"No tags":>9} {"Unique tags":>12}')
    lines.append("-" * 55)
    for s in splits:
        st = stats[s]
        lines.append(
            f'{s:8} {st["n_problems"]:>10} {st["n_with_tags"]:>10} '
            f'{st["n_no_tags"]:>9} {len(st["tag_counts"]):>12}'
        )
    lines.append(
        f'{"all":8} {sum(stats[s]["n_problems"] for s in splits):>10} '
        f'{sum(stats[s]["n_with_tags"] for s in splits):>10} '
        f'{sum(stats[s]["n_no_tags"] for s in splits):>9} '
        f'{len(all_tags):>12}'
    )
    lines.append("")
    lines.append(f"Total unique tags across splits: {len(all_tags)}")
    lines.append("")
    lines.append("Per-tag counts (descending by total):")
    lines.append("-" * 70)
    header = f'{"tag":35s} '
    for s in splits:
        header += f"{s:>8} "
    header += f'{"total":>8}'
    lines.append(header)
    for tag, _ in total.most_common():
        row = f'{tag if tag else "(empty)":35s} '
        for s in splits:
            row += f'{stats[s]["tag_counts"].get(tag, 0):>8} '
        row += f"{total[tag]:>8}"
        lines.append(row)

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  → {out_path}")

def write_mapping(stats: Dict[str, dict], out_path: Path) -> None:
    splits = list(stats.keys())
    total = Counter()
    for s in splits:
        for t, c in stats[s]["tag_counts"].items():
            total[t] += c

    scen_to_tags: Dict[str, List[str]] = defaultdict(list)
    for tag, scens in TAG_TO_SCENARIO.items():
        for sc in scens:
            scen_to_tags[sc].append(tag)

    lines: List[str] = []
    lines.append("TAG → SCENARIO MAPPING")
    lines.append("=" * 70)
    lines.append("")
    lines.append("Forward (tag → scenarios):")
    lines.append("-" * 70)
    lines.append(f'{"tag":30s} {"total":>7} {"→ scenarios":40s}')
    for tag, _ in total.most_common():
        scens = TAG_TO_SCENARIO.get(tag, [])
        if scens:
            arrow = ", ".join(scens)
        else:
            arrow = "(unmapped)"
        lines.append(f'{tag if tag else "(empty)":30s} {total[tag]:>7} → {arrow}')
    lines.append("")

    lines.append("Reverse (scenario ← tags):")
    lines.append("-" * 70)
    for sc in sorted(scen_to_tags):
        tags = scen_to_tags[sc]
        tag_total = sum(total.get(t, 0) for t in tags)
        lines.append(f"  {sc:25s} ← {tags}    [total tag count = {tag_total}]")
    lines.append("")

    n_with_mapped = 0
    n_with_only_unmapped = 0
    n_no_tags = 0
    mapped_tags = set(TAG_TO_SCENARIO.keys())
    for s in splits:
        for _idx, _name, tags in stats[s]["problems"]:
            if not tags:
                n_no_tags += 1
            elif any(t in mapped_tags for t in tags):
                n_with_mapped += 1
            else:
                n_with_only_unmapped += 1
    n_total = n_with_mapped + n_with_only_unmapped + n_no_tags

    lines.append("Coverage (per-problem):")
    lines.append("-" * 70)
    lines.append(f"  ≥1 mapped tag (covered by catalog):     {n_with_mapped}/{n_total}  ({100*n_with_mapped/n_total:.1f}%)")
    lines.append(f"  Only unmapped tags (off-catalog):       {n_with_only_unmapped}/{n_total}  ({100*n_with_only_unmapped/n_total:.1f}%)")
    lines.append(f"  No tags at all:                         {n_no_tags}/{n_total}  ({100*n_no_tags/n_total:.1f}%)")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  → {out_path}")

def write_off_catalog(stats: Dict[str, dict], out_path: Path) -> None:
    splits = list(stats.keys())
    mapped_tags = set(TAG_TO_SCENARIO.keys())

    off_cat_problems: List[Tuple[str, str, List[str]]] = []
    off_cat_tag_count = Counter()
    for s in splits:
        for _idx, name, tags in stats[s]["problems"]:
            if tags and not any(t in mapped_tags for t in tags):
                off_cat_problems.append((s, name, tags))
                for t in tags:
                    off_cat_tag_count[t] += 1

    lines: List[str] = []
    lines.append("OFF-CATALOG TAGS / PROBLEMS")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"Problems whose every tag is unmapped: {len(off_cat_problems)}")
    lines.append("")
    lines.append("Tag frequency in those problems (top 30):")
    lines.append("-" * 70)
    for t, c in off_cat_tag_count.most_common(30):
        lines.append(f"  {t:30s} {c}")
    lines.append("")
    lines.append("Off-catalog problem list (split / name / tags):")
    lines.append("-" * 70)
    for s, name, tags in off_cat_problems:
        lines.append(f"  [{s}] {name}    {tags}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  → {out_path}")

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--splits", default="train,valid,test",
                    help="Comma-separated splits to scan (default: train,valid,test)")
    ap.add_argument("--cache_dir", default="~/.cache/huggingface/datasets")
    ap.add_argument("--output_dir", default=None,
                    help="Output directory (default: Base/dataset/tags_information/)")
    args = ap.parse_args()

    splits = [s.strip() for s in args.splits.split(",") if s.strip()]
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        base_dir = Path(__file__).resolve().parents[2]
        out_dir = base_dir / "dataset" / "tags_information"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading dataset (cache_dir={args.cache_dir}) ...")
    ds = load_dataset_lazy(args.cache_dir)
    print(f"Splits: {splits}")
    stats = collect_tag_stats(ds, splits)

    print("\nWriting reports:")
    write_distribution(stats, out_dir / "cf_tags_distribution.txt")
    write_mapping(stats, out_dir / "tag_to_scenario_mapping.txt")
    write_off_catalog(stats, out_dir / "off_catalog_tags.txt")
    print(f"\nDone. Output dir: {out_dir}")

if __name__ == "__main__":
    main()
