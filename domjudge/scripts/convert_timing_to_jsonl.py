#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

def convert(src: Path, dst: Path) -> int:
    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)
    metadata = dict(data.get("metadata", {}))
    metadata.setdefault("type", "metadata")
    if "type" in metadata and metadata["type"] != "metadata":
        metadata["type"] = "metadata"
    problems = data.get("problems", []) or []
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        for p in problems:
            entry = dict(p)
            entry["type"] = "problem"
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return len(problems)

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", type=Path, help="Legacy single-JSON timing file")
    ap.add_argument("dst", type=Path, help="Destination JSONL path")
    args = ap.parse_args()
    if not args.src.is_file():
        print(f"[convert_timing_to_jsonl] source not found: {args.src}",
              file=sys.stderr)
        sys.exit(1)
    n = convert(args.src, args.dst)
    print(f"[convert_timing_to_jsonl] {n} problems: {args.src} -> {args.dst}")

if __name__ == "__main__":
    main()
