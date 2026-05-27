#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

CACHE_DIR="${HOME}/.cache/huggingface/hub"
OUTPUT_DIR="${BASE_DIR}/dataset/codecontests_description_separated"
SPLIT="all"

echo "==============================="
echo "Cache Dir  : $CACHE_DIR"
echo "Output Dir : $OUTPUT_DIR"
echo "Split      : $SPLIT"
echo "==============================="

python "${BASE_DIR}/code/utils/description_feature_extractor.py" \
    --cache_dir  "$CACHE_DIR" \
    --output_dir "$OUTPUT_DIR" \
    --split      "$SPLIT"

echo "*** Done ***"
