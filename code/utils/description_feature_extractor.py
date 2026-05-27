
import re
import json
import argparse
from pathlib import Path
from collections import Counter
from tqdm import tqdm

LANGUAGE_MAP = {
    0: "Unknown",
    1: "Python2",
    2: "C++",
    3: "Python3",
    4: "Java",
}
SKIP_LANGUAGE_CODES = {0}

_TOP_LEVEL_HEADERS = [
    ("input",       re.compile(r"^Input$", re.MULTILINE)),
    ("output",      re.compile(r"^Output$", re.MULTILINE)),
    ("examples",    re.compile(r"^Examples?$", re.MULTILINE)),
    ("constraints", re.compile(r"^Constraints?$", re.MULTILINE)),
    ("note",        re.compile(r"^Note$", re.MULTILINE)),
]

_POST_EXAMPLE_RE = re.compile(r"^(Constraints?|Note)$", re.MULTILINE)

def _split_sections(description: str) -> dict:
    sections = {}
    text = description

    positions = {}
    for key, pat in _TOP_LEVEL_HEADERS:
        m = pat.search(text)
        if m:
            positions[key] = (m.start(), m.end())

    if not positions:
        return {"problem_description": text.strip()}

    ordered = sorted(positions.items(), key=lambda kv: kv[1][0])

    sections["problem_description"] = text[: ordered[0][1][0]].strip()

    for i, (key, (hdr_start, hdr_end)) in enumerate(ordered):
        if i + 1 < len(ordered):
            next_start = ordered[i + 1][1][0]
        else:
            next_start = len(text)

        body = text[hdr_end:next_start].strip()

        if key == "examples":
            post = _POST_EXAMPLE_RE.search(body)
            if post:
                body = body[: post.start()].strip()
        sections[key] = body

    return sections

def _parse_examples(examples_text: str) -> list:
    if not examples_text:
        return []

    results = []
    parts = re.split(r"^(Input|Output)$", examples_text, flags=re.MULTILINE)

    current_input = None
    i = 0
    while i < len(parts):
        token = parts[i].strip()
        if token == "Input" and i + 1 < len(parts):
            current_input = parts[i + 1].strip()
            i += 2
        elif token == "Output" and i + 1 < len(parts):
            output_text = parts[i + 1].strip()
            results.append({
                "input": current_input or "",
                "output": output_text,
            })
            current_input = None
            i += 2
        else:
            i += 1

    return results

def extract_features(description: str) -> dict:
    sections = _split_sections(description)

    examples_raw = sections.get("examples")
    if examples_raw:
        examples = _parse_examples(examples_raw)
    else:
        output_raw = sections.get("output", "")
        if re.search(r"^Input$", output_raw, re.MULTILINE):
            first_input = re.search(r"^Input$", output_raw, re.MULTILINE)
            sections["output"] = output_raw[: first_input.start()].strip()
            examples = _parse_examples(output_raw[first_input.start():])
        else:
            examples = []

    return {
        "problem_description": sections.get("problem_description", ""),
        "input_description": sections.get("input", ""),
        "output_description": sections.get("output", ""),
        "examples": examples,
        "constraints": sections.get("constraints"),
        "note": sections.get("note"),
    }

def save_json(data, path, indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)

def main():
    parser = argparse.ArgumentParser(description="Extract features from codecontests descriptions")
    parser.add_argument("--cache_dir", type=str, default="~/.cache/huggingface/hub",
                        help="HuggingFace cache directory")
    parser.add_argument("--output_dir", type=str, default=None,
                        help="Output directory (default: Base/dataset/codecontests_features/)")
    parser.add_argument("--split", type=str, default="all",
                        help="Which split to process: train, test, valid, or all (default: all)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    output_dir = Path(args.output_dir) if args.output_dir else project_root / "dataset" / "codecontests_features"

    from datasets import load_dataset
    cache_base = args.cache_dir.split("hub")[0] + "dataset" if "hub" in args.cache_dir else args.cache_dir
    dataset = load_dataset("deepmind/code_contests", cache_dir=cache_base)

    splits = list(dataset.keys()) if args.split == "all" else [args.split]

    all_split_stats = {}

    for split_name in splits:
        if split_name not in dataset:
            print(f"[WARNING] Split '{split_name}' not found, skipping.")
            continue

        split_data = dataset[split_name]
        results = []
        lang_counter = Counter()
        no_lang_count = 0

        for idx, example in enumerate(tqdm(split_data, desc=f"Extracting features [{split_name}]")):
            name = example.get("name", "")
            description = example.get("description", "")

            sols = example.get("solutions", {})
            raw_lang_codes = sols.get("language", []) if isinstance(sols, dict) else []
            unique_langs = sorted({
                LANGUAGE_MAP.get(lc, f"Language_{lc}")
                for lc in raw_lang_codes
                if lc not in SKIP_LANGUAGE_CODES
            })

            if not unique_langs:
                no_lang_count += 1
                continue
            for lang in unique_langs:
                lang_counter[lang] += 1

            features = extract_features(description)
            features = {
                "split": split_name,
                "index": idx,
                "name": name,
                "languages": unique_langs,
                **features,
            }
            results.append(features)

        out_path = output_dir / f"features_{split_name}.json"
        save_json(results, out_path)
        print(f"[{split_name}] {len(results)} problems (skipped {no_lang_count} UNKNOWN-only) -> {out_path}")

        all_split_stats[split_name] = {
            "total": len(results),
            "total_raw": len(split_data),
            "lang_counter": dict(lang_counter),
            "skipped_unknown_only": no_lang_count,
        }

    _write_summary(output_dir, all_split_stats)
    print("Done.")

def _write_summary(output_dir, all_split_stats):
    summary_path = Path(output_dir) / "summary.txt"
    lines = []
    lines.append("=" * 60)
    lines.append("DESCRIPTION FEATURE EXTRACTION SUMMARY")
    lines.append("=" * 60)

    grand_total = sum(s["total"] for s in all_split_stats.values())
    grand_raw = sum(s["total_raw"] for s in all_split_stats.values())
    grand_skipped = sum(s["skipped_unknown_only"] for s in all_split_stats.values())
    lines.append(f"Total problems (after filtering): {grand_total}")
    lines.append(f"Total problems (raw dataset):     {grand_raw}")
    lines.append(f"Skipped (UNKNOWN-only):           {grand_skipped}")
    lines.append(f"Splits: {list(all_split_stats.keys())}")
    lines.append("")

    agg_lang = Counter()
    for stats in all_split_stats.values():
        for lang, cnt in stats["lang_counter"].items():
            agg_lang[lang] += cnt

    lines.append("Language distribution (problems with solutions, UNKNOWN excluded):")
    for lang, cnt in sorted(agg_lang.items(), key=lambda x: -x[1]):
        lines.append(f"  {lang}: {cnt}")
    lines.append("")

    for split_name, stats in all_split_stats.items():
        lines.append("-" * 40)
        lines.append(f"[{split_name}]  {stats['total']} problems  (raw: {stats['total_raw']}, skipped: {stats['skipped_unknown_only']})")
        for lang, cnt in sorted(stats["lang_counter"].items(), key=lambda x: -x[1]):
            lines.append(f"  {lang}: {cnt}")

    lines.append("=" * 60)

    summary_text = "\n".join(lines) + "\n"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary_text)
    print(f"Summary saved to {summary_path}")

if __name__ == "__main__":
    main()
