#!/bin/bash

FEATURES_DIR="dataset/codecontests_description_separated"
OUTPUT_DIR="output/codecontests/our_method"
PARSED_STRUCTURES_DIR="dataset/parsed_structures"

TRAIN_SAMPLE_RATIO=0.1
TRAIN_SAMPLE_SEED=42

MAX_PROBLEMS=0

Z3_TIMEOUT=30

NUM_TESTCASES=5

TIERS="slow"

RESUME=true

COMPACT_M1=true

REFRESH_CACHE=true

GEN_TIMEOUT=60

LLM_TEMPERATURE="${LLM_TEMPERATURE:-}"
LLM_TOP_P="${LLM_TOP_P:-}"
LLM_TOP_K="${LLM_TOP_K:-}"
OUTPUT_TAG="${OUTPUT_TAG:-}"

SPLITS=(
    "valid"
    "test"
     )

MODES=(
    "All_USE"
)

REFINEMENT_PROMPTS=(
    "slow_testcase_refinement_prompt"
)

MODELS=(
     "local:google/gemma-4-31B-it"
     "local:Qwen/Qwen3.5-27B"
    "openai:gpt-5.4-nano"
    "gemini:gemini-3.1-flash-lite-preview"

    )
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="${BASE_DIR}/code:${PYTHONPATH}"

cd "$BASE_DIR" || exit 1

ENV_OPENAI="${ENV_OPENAI:-${SCRIPT_DIR}/api_key.env}"
ENV_GEMINI="${ENV_GEMINI:-${SCRIPT_DIR}/GEMINI_API_KEY.env}"

[ -f "$ENV_OPENAI" ] && { set -o allexport; . "$ENV_OPENAI";  set +o allexport; }
[ -f "$ENV_GEMINI" ] && { set -o allexport; . "$ENV_GEMINI";  set +o allexport; }

for MODEL_ENTRY in "${MODELS[@]}"; do
  if echo "$MODEL_ENTRY" | grep -q ':'; then
      PROVIDER="${MODEL_ENTRY%%:*}"
      MODEL_NAME="${MODEL_ENTRY#*:}"
      PROVIDER_ARG="--provider ${PROVIDER}"
      MODEL_ARG="--model ${MODEL_NAME}"
  else
      PROVIDER="${MODEL_ENTRY}"
      PROVIDER_ARG="--provider ${PROVIDER}"
      MODEL_ARG=""
  fi

  for SPLIT in "${SPLITS[@]}"; do
    for MODE in "${MODES[@]}"; do
      for REFINEMENT_PROMPT in "${REFINEMENT_PROMPTS[@]}"; do
        echo "=============================================="
        echo "Generating slow test cases for split: ${SPLIT} (model: ${MODEL_ENTRY}, mode: ${MODE}, tiers: ${TIERS}, refinement: ${REFINEMENT_PROMPT})"
        echo "=============================================="

        RESUME_ARG=""
        if [ "$RESUME" = "true" ]; then
            RESUME_ARG="--resume"
        fi

        COMPACT_M1_ARG=""
        if [ "$COMPACT_M1" = "true" ]; then
            COMPACT_M1_ARG="--compact_m1"
        fi

        REFRESH_CACHE_ARG=""
        if [ "$REFRESH_CACHE" = "true" ]; then
            REFRESH_CACHE_ARG="--refresh_cache"
        fi

        NO_WCM_ARG=""
        ACTUAL_MODE="$MODE"
        if echo "$ACTUAL_MODE" | grep -q '\-no-wcm$'; then
            ACTUAL_MODE="${ACTUAL_MODE%-no-wcm}"
            NO_WCM_ARG="--no_wcm"
        fi

        SAMPLING_ARGS=""
        [ -n "$LLM_TEMPERATURE" ] && SAMPLING_ARGS="$SAMPLING_ARGS --llm_temperature $LLM_TEMPERATURE"
        [ -n "$LLM_TOP_P" ]       && SAMPLING_ARGS="$SAMPLING_ARGS --llm_top_p $LLM_TOP_P"
        [ -n "$LLM_TOP_K" ]       && SAMPLING_ARGS="$SAMPLING_ARGS --llm_top_k $LLM_TOP_K"
        [ -n "$OUTPUT_TAG" ]      && SAMPLING_ARGS="$SAMPLING_ARGS --output_tag $OUTPUT_TAG"

        python code/utils/slow_testcase_generator.py \
            --split "${SPLIT}" \
            --tiers "${TIERS}" \
            --num_testcases "${NUM_TESTCASES}" \
            --mode "${ACTUAL_MODE}" \
            --z3_timeout "${Z3_TIMEOUT}" \
            --max_problems "${MAX_PROBLEMS}" \
            --features_dir "${FEATURES_DIR}" \
            --output_dir "${OUTPUT_DIR}" \
            --parsed_structures_dir "${PARSED_STRUCTURES_DIR}" \
            --train_sample_ratio "${TRAIN_SAMPLE_RATIO}" \
            --train_sample_seed "${TRAIN_SAMPLE_SEED}" \
            --refinement_prompt "${REFINEMENT_PROMPT}" \
            ${RESUME_ARG} \
            ${COMPACT_M1_ARG} \
            ${REFRESH_CACHE_ARG} \
            ${NO_WCM_ARG} \
            --gen_timeout "${GEN_TIMEOUT}" \
            ${PROVIDER_ARG} \
            ${MODEL_ARG} \
            ${SAMPLING_ARGS}

        echo ""
      done
    done
  done
done

echo "Done."
