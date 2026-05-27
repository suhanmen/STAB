#!/bin/bash

DATASET="codecontests"

NUM_TESTCASES=5

EVAL_TIERS="slow"

SPLITS=(
    test
    valid
    test_valid
    )

SOURCES=(

    slow_testcases

)

BASE_PROMPTS=(
    Testcase_generation_prompt_v4
)

SLOW_TESTCASES_REFINEMENT_PROMPTS=(
    "slow_testcase_refinement_prompt"
)

SLOW_TESTCASES_MODES=(
    "smt_only"
)

REACT_VERSION="v5"
BASE_MODELS=(
    Qwen3.5-27B

)

get_api_provider() {
    local model="$1"
    case "$model" in
        gemini-*|gemma-*) echo "gemini" ;;
        gpt-*|o1-*|o3-*|o4-*) echo "openai" ;;
        *) echo "gemini" ;;
    esac
}

resolve_base_output_path() {
    local dir="$1"
    local model="$2"
    local split="$3"
    local cand="${dir}/${model}_${split}_output.json"
    if [ -f "$cand" ]; then printf '%s' "$cand"; return; fi
    cand="${dir}/${model//./-}_${split}_output.json"
    if [ -f "$cand" ]; then printf '%s' "$cand"; return; fi
    printf '%s' "${dir}/${model}_${split}_output.json"
}
API_MODELS=(
    gpt-5.4-nano
    gemini-3.1-flash-lite-preview
)
API_OUTPUT_ROOT="../API/output"

PRIOR_WORK_LLM_MODEL=(
    "Qwen3.5-27B"
)

SLOW_TESTCASES_LLM_MODEL=(
    "Qwen_Qwen3.5-27B"

)

REACT_LLM_MODEL=(
)
ENABLE_MULTI_SOLUTION=true
SOLUTION_STRATEGIES=(
    "fast_solution"
    "slow_solution"
    "random1_solution"
    "random2_solution"
    "random3_solution"
    )

JUDGE_SKIP_PROBLEMS=""

PARSED_STRUCTURES_DIR="dataset/parsed_structures"

LLM_CONSTRAINTS_PROVIDER="openai"
LLM_CONSTRAINTS_MODEL="gpt-5-4-mini"

LLM_CONSTRAINTS_BOUNDARY_PROVIDER="openai"
LLM_CONSTRAINTS_BOUNDARY_MODEL="gpt-5-4-mini"

LLM_STDIN_SCHEMA_DIR="output/generate_stdin_schema"
LLM_STDIN_SCHEMA_PROVIDER="openai"
LLM_STDIN_SCHEMA_MODEL="gpt-5-4-mini"

DOMJUDGE_RESULTS_ROOT="../domjudge/results"

REFERENCE_TIMING_DIR="${DOMJUDGE_RESULTS_ROOT}/dataset"

DOMJUDGE_RESULTS_DIR="${DOMJUDGE_RESULTS_ROOT}/test_case_time_check/codecontests"
EVALUAND_TIMING_DIR="${DOMJUDGE_RESULTS_DIR}"

OUTPUT_ROOT="evaluation"

NO_PLOT="true"

SLOW_RATIO_THRESHOLD=${SLOW_RATIO_THRESHOLD:-0.9}

_SANITIZED=()
for _s in "${SOLUTION_STRATEGIES[@]}"; do
    _SANITIZED+=("${_s//,/}")
done
SOLUTION_STRATEGIES=("${_SANITIZED[@]}")
unset _SANITIZED _s

MULTI_SOLUTION_SEED=420

RUN_JUDGE="true"
RUN_JUDGE_FORCE="false"

JUDGE_SCRIPT="../domjudge/codecontests_judge.py"

EXPECTED_OUTPUT_CACHE_DIR="${EXPECTED_OUTPUT_CACHE_DIR:-}"
EXPECTED_OUTPUT_CACHE_DIR_FLAG=""
if [ -n "$EXPECTED_OUTPUT_CACHE_DIR" ]; then
    EXPECTED_OUTPUT_CACHE_DIR_FLAG="--expected_output_cache_dir $EXPECTED_OUTPUT_CACHE_DIR"
fi
JUDGE_CONTEST_ID="${JUDGE_CONTEST_ID:-dj-2}"
JUDGE_ADMIN_USER="${JUDGE_ADMIN_USER:-admin}"
JUDGE_ADMIN_PASSWORD="${JUDGE_ADMIN_PASSWORD:-changeme}"
JUDGE_TEAM_USER="${JUDGE_TEAM_USER:-test_user}"
JUDGE_TEAM_PASSWORD="${JUDGE_TEAM_PASSWORD:-changeme}"
JUDGE_DOMJUDGE_URL="${JUDGE_DOMJUDGE_URL:-http://localhost:50043}"
JUDGE_DB_HOST="${JUDGE_DB_HOST:-localhost}"
if [ -z "${JUDGE_DB_PORT:-}" ]; then
    case "$JUDGE_DOMJUDGE_URL" in
        *:50043*) JUDGE_DB_PORT=50035 ;;
        *:50044*) JUDGE_DB_PORT=50036 ;;
        *:50045*) JUDGE_DB_PORT=50037 ;;
        *:50046*) JUDGE_DB_PORT=50038 ;;
        *)        JUDGE_DB_PORT=50037 ;;
    esac
fi
JUDGE_CACHE_DIR="${JUDGE_CACHE_DIR:-${HF_CACHE_DIR:-/tmp/huggingface-cache}}"
JUDGE_MAX_SOLUTIONS="${JUDGE_MAX_SOLUTIONS:-1}"
JUDGE_MAX_CONCURRENT="${JUDGE_MAX_CONCURRENT:-1}"

JUDGE_SKIP_ARGS=()
JUDGE_EXCLUDED_PATH="${JUDGE_EXCLUDED_PATH:-../Base/dataset/excluded_problems.json}"
if [ -f "${JUDGE_EXCLUDED_PATH}" ]; then
    JUDGE_SKIP_ARGS+=(--excluded_path "${JUDGE_EXCLUDED_PATH}")
fi
if [ -n "${JUDGE_SKIP_PROBLEMS}" ]; then
    JUDGE_SKIP_ARGS=(--skip_problems "${JUDGE_SKIP_PROBLEMS}")
fi
LEF_ENABLED="true"
LEF_SELECTED_SOLUTIONS_DIR="${DOMJUDGE_RESULTS_ROOT}/selected_solutions"
LEF_HF_CACHE_DIR="${HF_CACHE_DIR:-/tmp/huggingface-cache}"
LEF_SEED=42
LEF_TIMEOUT=5.0
LEF_WORKERS=8

case "${1:-}" in
    43|44|45|46)
        JUDGE_DOMJUDGE_URL="http://localhost:500${1}"
        unset JUDGE_DB_PORT
        case "$JUDGE_DOMJUDGE_URL" in
            *:50043*) JUDGE_DB_PORT=50035 ;;
            *:50044*) JUDGE_DB_PORT=50036 ;;
            *:50045*) JUDGE_DB_PORT=50037 ;;
            *:50046*) JUDGE_DB_PORT=50038 ;;
        esac
        echo "[port-shorthand] using DOMjudge $JUDGE_DOMJUDGE_URL with DB $JUDGE_DB_PORT"
        shift
        ;;
esac

while [[ $# -gt 0 ]]; do
    case $1 in
        --split)
            SPLITS=("$2")
            shift 2
            ;;
        --splits)
            IFS=',' read -ra SPLITS <<< "$2"
            shift 2
            ;;
        --sources)
            IFS=',' read -ra SOURCES <<< "$2"
            shift 2
            ;;
        --base_models)
            IFS=',' read -ra BASE_MODELS <<< "$2"
            shift 2
            ;;
        --api_models)
            IFS=',' read -ra API_MODELS <<< "$2"
            shift 2
            ;;
        --api_provider)
            echo "WARNING: --api_provider is deprecated; provider is auto-detected from model name"
            shift 2
            ;;
        --slow_testcases_modes)
            IFS=',' read -ra SLOW_TESTCASES_MODES <<< "$2"
            shift 2
            ;;
        --slow_testcases_refinement_prompts)
            IFS=',' read -ra SLOW_TESTCASES_REFINEMENT_PROMPTS <<< "$2"
            shift 2
            ;;
        --base_prompts)
            IFS=',' read -ra BASE_PROMPTS <<< "$2"
            shift 2
            ;;
        --model)
            BASE_MODELS=("$2")
            SLOW_TESTCASES_LLM_MODEL=("$2")
            shift 2
            ;;
        --dataset)
            DATASET="$2"
            shift 2
            ;;
        --reference_timing_dir)
            REFERENCE_TIMING_DIR="$2"
            shift 2
            ;;
        --evaluand_timing_dir)
            EVALUAND_TIMING_DIR="$2"
            shift 2
            ;;
        --output_root)
            OUTPUT_ROOT="$2"
            shift 2
            ;;
        --no_plot)
            NO_PLOT="true"
            shift
            ;;
        --run_judge)
            RUN_JUDGE="true"
            shift
            ;;
        --run_judge_force)
            RUN_JUDGE="true"
            RUN_JUDGE_FORCE="true"
            shift
            ;;
        --help)
            echo "Usage: sh scripts/slow_testcase_evaluation.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --split SPLIT           Single split to evaluate"
            echo "  --splits SPLIT1,SPLIT2  Comma-separated splits (default: train,valid,test)"
            echo "  --sources SRC1,SRC2     Comma-separated evaluand sources: Base, slow_testcases"
            echo "  --base_models M1,M2      Comma-separated models for Base source"
            echo "  --api_models M1,M2       Comma-separated models for API source"
            echo "  --api_provider PROVIDER  API provider name (default: gemini)"
            echo "  --base_prompts P1,P2     Comma-separated prompt names for Base source (default: Testcase_generation_prompt)"
            echo "  --slow_testcases_modes M1,M2    Comma-separated modes for slow_testcases source (boundary/algorithmic/both/All_USE/smt_only/scenario_only)"
            echo "  --slow_testcases_refinement_prompts P1,P2  Comma-separated refinement prompt names"
            echo "  --model MODEL           Single model (sets both BASE_MODELS and SLOW_TESTCASES_MODELS)"
            echo "  --dataset DATASET       Dataset name (default: codecontests)"
            echo "  --reference_timing_dir DIR   Directory containing reference timing JSONs"
            echo "  --evaluand_timing_dir DIR    Directory for evaluand timing (default: DOMjudge results dir)"
            echo "  --output_root DIR       Output root directory"
            echo "  --no_plot               Skip plot generation"
            echo "  --run_judge             Run DOMjudge for missing evaluand timing JSONs before evaluation"
            echo "  --run_judge_force       Run DOMjudge for all evaluands (ignore existing files)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="${BASE_DIR}/code:${PYTHONPATH}"

cd "$BASE_DIR" || exit 1

echo "=============================================="
echo "Slow Test Case Evaluation"
echo "=============================================="
echo "EVAL_TIERS: $EVAL_TIERS"
echo "ENABLE_MULTI_SOLUTION: $ENABLE_MULTI_SOLUTION"
echo "SOLUTION_STRATEGIES: ${SOLUTION_STRATEGIES[*]}"
echo "MULTI_SOLUTION_SEED: $MULTI_SOLUTION_SEED"
echo "=============================================="
echo "  Dataset:    ${DATASET}"
echo "  Splits:     ${SPLITS[*]}"
echo "  Sources:    ${SOURCES[*]}"
if [[ " ${SOURCES[*]} " =~ " Base " ]]; then
    echo "  Base models: ${BASE_MODELS[*]}"
fi
if [[ " ${SOURCES[*]} " =~ " API " ]]; then
    echo "  API models: ${API_MODELS[*]} (provider: auto-detected per model)"
fi
if [[ " ${SOURCES[*]} " =~ " slow_testcases " ]]; then
    echo "  Slow modes: ${SLOW_TESTCASES_MODES[*]}"
    echo "  Slow prompts: ${SLOW_TESTCASES_REFINEMENT_PROMPTS[*]}"
    echo "  Slow LLMs: ${SLOW_TESTCASES_LLM_MODEL[*]}"
fi
if [[ " ${SOURCES[*]} " =~ " dataset " ]]; then
    echo "  Dataset analysis: reference self-comparison → evaluation/codecontests/dataset_analysis/{split}/"
fi
echo "  Run judge:  ${RUN_JUDGE} (force=${RUN_JUDGE_FORCE})"
echo "=============================================="
echo ""

PLOT_FLAG=""
if [ "$NO_PLOT" = "true" ]; then
    PLOT_FLAG="--no_plot"
fi

for SPLIT in "${SPLITS[@]}"; do
    if [[ "$SPLIT" == *_* ]]; then
        IFS='_' read -ra _COMPOUND_SUBS <<< "$SPLIT"
        echo "=============================================="
        echo "  [COMPOUND SPLIT] ${SPLIT} = ${_COMPOUND_SUBS[*]}"
        echo "  Skipping judge/generation. Aggregating per-split summaries."
        echo "=============================================="
        _COMPOUND_OK=true
        for _CSUB in "${_COMPOUND_SUBS[@]}"; do
            _FOUND=$(find "${OUTPUT_ROOT}/${DATASET}" -mindepth 3 -maxdepth 6 -type d -name "$_CSUB" 2>/dev/null | head -1)
            if [ -z "$_FOUND" ]; then
                echo "  [ERROR] Per-split eval missing for sub-split '${_CSUB}': no eval directory found under ${OUTPUT_ROOT}/${DATASET}/"
                echo "          Run SPLITS=(${_CSUB}) sh ${0##*/} first."
                _COMPOUND_OK=false
            fi
        done
        if [ "$_COMPOUND_OK" = false ]; then
            echo "  [ABORT] Compound split '${SPLIT}' requires all sub-splits to be pre-evaluated."
            continue
        fi

        _IN_SPLITS_CSV=$(IFS=,; echo "${_COMPOUND_SUBS[*]}")
        _STRATS_CSV=$(IFS=,; echo "${SOLUTION_STRATEGIES[*]}")
        _LANGS_CSV="total,cpp,python,java"
        _run_total_avg() {
            local _CT_SPLIT="$1"; local _CT_SOURCE="$2"; local _CT_MODEL="$3"
            local _CT_PROMPT="$4"; local _CT_REFINE="$5"
            local _CT_LLM="${6:-}"
            local _CT_PROMPT_FLAG="" _CT_REFINE_FLAG="" _CT_LLM_FLAG=""
            [ -n "$_CT_PROMPT" ] && _CT_PROMPT_FLAG="--prompt $_CT_PROMPT"
            [ -n "$_CT_REFINE" ] && _CT_REFINE_FLAG="--refinement_prompt $_CT_REFINE"
            [ -n "$_CT_LLM" ] && _CT_LLM_FLAG="--llm_model $_CT_LLM"
            python code/utils/slow_testcase_evaluation.py \
                --compute_total_avg \
                --split "$_CT_SPLIT" \
                --dataset "$DATASET" \
                --source "$_CT_SOURCE" \
                --model "$_CT_MODEL" \
                --output_root "$OUTPUT_ROOT" \
                --eval_tiers "$EVAL_TIERS" \
                $_CT_PROMPT_FLAG \
                $_CT_REFINE_FLAG \
                $_CT_LLM_FLAG \
                --strategies ${SOLUTION_STRATEGIES[*]} 2>&1 | tail -5
        }

        for SOURCE in "${SOURCES[@]}"; do
            _IS_PRIOR_WORK=false
            if [ "$SOURCE" = "Base" ]; then
                _MODELS_LIST=("${BASE_MODELS[@]}")
                _MODELS_CSV=$(IFS=,; echo "${BASE_MODELS[*]}")
                _PROMPTS_LIST=("${BASE_PROMPTS[@]}")
                _SOURCE_PREFIX="baseline"
                _EVAL_SOURCE="Base"
            elif [ "$SOURCE" = "API" ]; then
                _MODELS_LIST=("${API_MODELS[@]}")
                _MODELS_CSV=$(IFS=,; echo "${API_MODELS[*]}")
                _PROMPTS_LIST=("${BASE_PROMPTS[@]}")
                _SOURCE_PREFIX="baseline"
                _EVAL_SOURCE="Base"
            elif [ "$SOURCE" = "wedge" ] || [ "$SOURCE" = "evalperf_sas" ] || \
                 [ "$SOURCE" = "wedge_selected_solutions" ] || [ "$SOURCE" = "evalperf_sas_selected_solutions" ]; then
                _IS_PRIOR_WORK=true
                case "$SOURCE" in
                    wedge_selected_solutions)         _PRIOR_BASE="wedge_selected_solutions" ;;
                    evalperf_sas_selected_solutions)  _PRIOR_BASE="evalperf_sas_selected_solutions" ;;
                    *)                                _PRIOR_BASE="$SOURCE" ;;
                esac
                _MODELS_LIST=("")
                _MODELS_CSV=""
                if [ ${#PRIOR_WORK_LLM_MODEL[@]} -gt 0 ]; then
                    _PROMPTS_LIST=("${PRIOR_WORK_LLM_MODEL[@]}")
                else
                    _PROMPTS_LIST=("")
                fi
                _EVAL_SOURCE="$_PRIOR_BASE"
                _SOURCE_PREFIX="$_PRIOR_BASE"
            elif [ "$SOURCE" = "react" ]; then
                _IS_PRIOR_WORK=true
                _PRIOR_BASE="react"
                _MODELS_LIST=("")
                _MODELS_CSV=""
                if [ ${#REACT_LLM_MODEL[@]} -gt 0 ]; then
                    _PROMPTS_LIST=("${REACT_LLM_MODEL[@]}")
                else
                    _PROMPTS_LIST=("")
                fi
                _SOURCE_PREFIX="our_method/react/${REACT_VERSION}"
                _EVAL_SOURCE="$_SOURCE_PREFIX"
            else
                _MODELS_LIST=("${SLOW_TESTCASES_LLM_MODEL[@]}")
                _MODELS_CSV=$(IFS=,; echo "${SLOW_TESTCASES_LLM_MODEL[*]}")
                _PROMPTS_LIST=("${SLOW_TESTCASES_REFINEMENT_PROMPTS[@]}")
                _SOURCE_PREFIX="our_method"
                _EVAL_SOURCE="$SOURCE"
            fi
            for _PROMPT in "${_PROMPTS_LIST[@]}"; do
                if [ "$_IS_PRIOR_WORK" = "true" ]; then
                    if [ "$_PRIOR_BASE" = "react" ]; then
                        if [ -n "$_PROMPT" ]; then
                            _SOURCE_PREFIX="our_method/react/${REACT_VERSION}/${_PROMPT}"
                        else
                            _SOURCE_PREFIX="our_method/react/${REACT_VERSION}"
                        fi
                        _EVAL_SOURCE="$_SOURCE_PREFIX"
                    else
                        if [ -n "$_PROMPT" ]; then
                            _SOURCE_PREFIX="${_PRIOR_BASE}/${_PROMPT}"
                            _EVAL_SOURCE="${_PRIOR_BASE}/${_PROMPT}"
                        else
                            _SOURCE_PREFIX="$_PRIOR_BASE"
                            _EVAL_SOURCE="$_PRIOR_BASE"
                        fi
                    fi
                fi
                if [ "$_IS_PRIOR_WORK" = "true" ] && [ "$_PRIOR_BASE" = "react" ]; then
                    _RE_MODEL="$_PROMPT"
                    echo "  → aggregating (react): source_path=our_method  subpath=react/${REACT_VERSION}  model=${_RE_MODEL}  strategies=${_STRATS_CSV}"
                    python3 code/utils/merge_eval_summaries.py \
                        --eval_dir "${OUTPUT_ROOT}/${DATASET}" \
                        --source_path "our_method" \
                        --subpath_after_split "react/${REACT_VERSION}" \
                        --models "$_RE_MODEL" \
                        --strategies "$_STRATS_CSV" \
                        --languages "$_LANGS_CSV" \
                        --in_splits "$_IN_SPLITS_CSV" \
                        --out_split "$SPLIT" \
                        --tcs_per_tier "$NUM_TESTCASES"
                    echo "    [TOTAL_AVG] ${_RE_MODEL} (react/${REACT_VERSION})"
                    _run_total_avg "$SPLIT" "our_method" "react" "$REACT_VERSION" "$REACT_VERSION" "$_RE_MODEL"
                elif [ "$_IS_PRIOR_WORK" = "true" ]; then
                    _SP="$_SOURCE_PREFIX"
                    case "$_PRIOR_BASE" in
                        wedge_selected_solutions)        _PRIOR_STRATS_CSV="wedge_solution" ;;
                        evalperf_sas_selected_solutions) _PRIOR_STRATS_CSV="evalperf_sas_solution" ;;
                        *)                                _PRIOR_STRATS_CSV="$_STRATS_CSV" ;;
                    esac
                    echo "  → aggregating (prior-work): source_path=${_SP}  strategies=${_PRIOR_STRATS_CSV}"
                    python3 code/utils/merge_eval_summaries.py \
                        --eval_dir "${OUTPUT_ROOT}/${DATASET}" \
                        --source_path "$_SP" \
                        --models "$_MODELS_CSV" \
                        --strategies "$_PRIOR_STRATS_CSV" \
                        --languages "$_LANGS_CSV" \
                        --in_splits "$_IN_SPLITS_CSV" \
                        --out_split "$SPLIT" \
                        --tcs_per_tier "$NUM_TESTCASES"
                    echo "    [TOTAL_AVG] (${_SP})"
                    _run_total_avg "$SPLIT" "$_EVAL_SOURCE" "" "" ""
                elif [ "$_SOURCE_PREFIX" = "our_method" ]; then
                    for _MODE in "${SLOW_TESTCASES_MODES[@]:-both}"; do
                        _SP_LOG="our_method/${_MODE}/${_PROMPT}"
                        echo "  → aggregating: layout=our_method/${_MODE}/${_PROMPT} (subpath after split)"
                        python3 code/utils/merge_eval_summaries.py \
                            --eval_dir "${OUTPUT_ROOT}/${DATASET}" \
                            --source_path "our_method" \
                            --subpath_after_split "${_MODE}/${_PROMPT}" \
                            --models "$_MODELS_CSV" \
                            --strategies "$_STRATS_CSV" \
                            --languages "$_LANGS_CSV" \
                            --in_splits "$_IN_SPLITS_CSV" \
                            --out_split "$SPLIT" \
                            --tcs_per_tier "$NUM_TESTCASES"
                        for _M in "${_MODELS_LIST[@]}"; do
                            echo "    [TOTAL_AVG] ${_M} (${_SP_LOG})"
                            _run_total_avg "$SPLIT" "$_EVAL_SOURCE" "$_MODE" "$_PROMPT" "$_PROMPT" "$_M"
                        done
                    done
                else
                    _SP="${_SOURCE_PREFIX}/${_PROMPT}"
                    echo "  → aggregating: source_path=${_SP}"
                    python3 code/utils/merge_eval_summaries.py \
                        --eval_dir "${OUTPUT_ROOT}/${DATASET}" \
                        --source_path "$_SP" \
                        --models "$_MODELS_CSV" \
                        --strategies "$_STRATS_CSV" \
                        --languages "$_LANGS_CSV" \
                        --in_splits "$_IN_SPLITS_CSV" \
                        --out_split "$SPLIT" \
                        --tcs_per_tier "$NUM_TESTCASES"
                    for _M in "${_MODELS_LIST[@]}"; do
                        echo "    [TOTAL_AVG] ${_M} (${_SP})"
                        _run_total_avg "$SPLIT" "$_EVAL_SOURCE" "$_M" "$_PROMPT" ""
                    done
                fi
            done
        done
        echo "  [DONE] Compound split '${SPLIT}' aggregation complete."
        continue
    fi

    if [[ "$SPLIT" == *_* ]]; then
        IFS='_' read -ra _SUB_SPLITS <<< "$SPLIT"
        _MERGE_FILES=()
        _MERGE_OK=true
        for _SUB in "${_SUB_SPLITS[@]}"; do
            if [ -f "${REFERENCE_TIMING_DIR}/codecontests_timing_${_SUB}.jsonl" ]; then
                _MERGE_FILES+=("${REFERENCE_TIMING_DIR}/codecontests_timing_${_SUB}.jsonl")
            elif [ -f "${REFERENCE_TIMING_DIR}/codecontests_timing_${_SUB}.json" ]; then
                _MERGE_FILES+=("${REFERENCE_TIMING_DIR}/codecontests_timing_${_SUB}.json")
            else
                echo "[WARN] Reference not found for sub-split '${_SUB}' — skipping compound split ${SPLIT}"
                _MERGE_OK=false
                break
            fi
        done
        if [ "$_MERGE_OK" = false ]; then continue; fi
        REFERENCE_TIMING="${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.jsonl"
        python3 -c "
import json, sys
out_path = sys.argv[1]
metadata_emitted = False
with open(out_path, 'w') as fo:
    for fp in sys.argv[2:]:
        try:
            with open(fp) as fi:
                d = json.load(fi)
            if isinstance(d, dict) and 'problems' in d:
                if not metadata_emitted and d.get('metadata'):
                    md = dict(d['metadata']); md['type'] = 'metadata'
                    fo.write(json.dumps(md, ensure_ascii=False) + '\n')
                    metadata_emitted = True
                for p in d['problems']:
                    if 'type' not in p: p = {**p, 'type': 'problem'}
                    fo.write(json.dumps(p, ensure_ascii=False) + '\n')
            elif isinstance(d, list):
                for p in d:
                    if isinstance(p, dict) and 'type' not in p: p = {**p, 'type': 'problem'}
                    fo.write(json.dumps(p, ensure_ascii=False) + '\n')
        except json.JSONDecodeError:
            with open(fp) as fi:
                for line in fi:
                    s = line.strip()
                    if not s: continue
                    fo.write(s + '\n')
" "$REFERENCE_TIMING" "${_MERGE_FILES[@]}"
        echo "[INFO] Merged ${#_MERGE_FILES[@]} reference timing files -> ${REFERENCE_TIMING}"
    else
        if [ -f "${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.jsonl" ]; then
            REFERENCE_TIMING="${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.jsonl"
        else
            REFERENCE_TIMING="${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.json"
        fi
    fi
    if [ ! -f "$REFERENCE_TIMING" ]; then
        echo "[WARN] Reference not found: ${REFERENCE_TIMING} — skipping split ${SPLIT}"
        continue
    fi

    if [ "$ENABLE_MULTI_SOLUTION" = "true" ] && [ "$RUN_JUDGE" = "true" ]; then
        for _REF_STRATEGY in "${SOLUTION_STRATEGIES[@]}"; do
            REF_STRAT_DIR="${REFERENCE_TIMING_DIR}/${_REF_STRATEGY}"
            REF_STRAT_FILE="${REF_STRAT_DIR}/codecontests_timing_${SPLIT}.json"
            REF_STRAT_FILE_JSONL="${REF_STRAT_DIR}/codecontests_timing_${SPLIT}.jsonl"
            if { [ -f "$REF_STRAT_FILE" ] || [ -f "$REF_STRAT_FILE_JSONL" ]; } \
               && [ "$RUN_JUDGE_FORCE" != "true" ]; then
                echo "[REF_SETUP] Per-strategy reference exists for ${_REF_STRATEGY}: ${REF_STRAT_DIR}"
                continue
            fi
            if [[ "$SPLIT" == *_* ]] && [ "$RUN_JUDGE_FORCE" != "true" ]; then
                IFS='_' read -ra _STRAT_SUBS <<< "$SPLIT"
                _STRAT_MERGE_FILES=()
                for _SSUB in "${_STRAT_SUBS[@]}"; do
                    _SUB_STRAT_JSONL="${REF_STRAT_DIR}/codecontests_timing_${_SSUB}.jsonl"
                    _SUB_STRAT_JSON="${REF_STRAT_DIR}/codecontests_timing_${_SSUB}.json"
                    if [ -f "$_SUB_STRAT_JSONL" ]; then
                        _STRAT_MERGE_FILES+=("$_SUB_STRAT_JSONL")
                    elif [ -f "$_SUB_STRAT_JSON" ]; then
                        _STRAT_MERGE_FILES+=("$_SUB_STRAT_JSON")
                    fi
                done
                if [ ${#_STRAT_MERGE_FILES[@]} -gt 0 ]; then
                    mkdir -p "$REF_STRAT_DIR"
                    python3 -c "
import json, sys
out_path = sys.argv[1]
metadata_emitted = False
with open(out_path, 'w') as fo:
    for fp in sys.argv[2:]:
        try:
            with open(fp) as fi:
                d = json.load(fi)
            if isinstance(d, dict) and 'problems' in d:
                if not metadata_emitted and d.get('metadata'):
                    md = dict(d['metadata']); md['type'] = 'metadata'
                    fo.write(json.dumps(md, ensure_ascii=False) + '\n')
                    metadata_emitted = True
                for p in d['problems']:
                    fo.write(json.dumps(p, ensure_ascii=False) + '\n')
            elif isinstance(d, list):
                for p in d:
                    fo.write(json.dumps(p, ensure_ascii=False) + '\n')
        except json.JSONDecodeError:
            with open(fp) as fi:
                for line in fi:
                    s = line.strip()
                    if not s: continue
                    fo.write(s + '\n')
" "$REF_STRAT_FILE_JSONL" "${_STRAT_MERGE_FILES[@]}"
                    echo "[REF_SETUP] Merged ${#_STRAT_MERGE_FILES[@]} sub-split per-strategy files for ${_REF_STRATEGY} → ${REF_STRAT_FILE_JSONL}"
                    continue
                fi
                echo "[REF_SETUP] No sub-split per-strategy files for compound split ${SPLIT} (strategy=${_REF_STRATEGY}) — skipping judge (run on sub-splits first)"
                continue
            fi
            if [ "$_REF_STRATEGY" = "fast_solution" ] && [ "$RUN_JUDGE_FORCE" != "true" ]; then
                LEGACY_FAST_JSON="${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.json"
                LEGACY_FAST_JSONL="${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.jsonl"
                if [ -f "$LEGACY_FAST_JSONL" ]; then
                    mkdir -p "$REF_STRAT_DIR"
                    cp "$LEGACY_FAST_JSONL" "$REF_STRAT_FILE_JSONL"
                    echo "[REF_SETUP] Migrated fast_solution from legacy JSONL: ${LEGACY_FAST_JSONL} → ${REF_STRAT_FILE_JSONL}"
                    continue
                elif [ -f "$LEGACY_FAST_JSON" ]; then
                    mkdir -p "$REF_STRAT_DIR"
                    python3 ../domjudge/scripts/convert_timing_to_jsonl.py \
                        "$LEGACY_FAST_JSON" "$REF_STRAT_FILE_JSONL" \
                        || { echo "[WARN] JSON→JSONL conversion failed; falling back to plain cp"; \
                             cp "$LEGACY_FAST_JSON" "$REF_STRAT_FILE"; }
                    echo "[REF_SETUP] Migrated fast_solution from legacy JSON (converted to JSONL): ${LEGACY_FAST_JSON} → ${REF_STRAT_FILE_JSONL}"
                    continue
                fi
            fi
            echo "=============================================="
            echo "  [REF_SETUP] Building per-strategy reference timing"
            echo "  Strategy:  ${_REF_STRATEGY}"
            echo "  Split:     ${SPLIT}"
            echo "  Output:    ${REF_STRAT_FILE}"
            echo "=============================================="
            mkdir -p "$REF_STRAT_DIR"
            RESET_REF_ARG=""
            if [ "$RUN_JUDGE_FORCE" = "true" ]; then
                RESET_REF_ARG="--reset"
            fi
            python3 "$JUDGE_SCRIPT" \
                --contest_id "$JUDGE_CONTEST_ID" \
                --admin_user "$JUDGE_ADMIN_USER" \
                --admin_password "$JUDGE_ADMIN_PASSWORD" \
                --team_user "$JUDGE_TEAM_USER" \
                --team_password "$JUDGE_TEAM_PASSWORD" \
                --domjudge_url "$JUDGE_DOMJUDGE_URL" \
                --db_host "$JUDGE_DB_HOST" \
                --db_port "$JUDGE_DB_PORT" \
                --cache_dir "$JUDGE_CACHE_DIR" \
                --split "$SPLIT" \
                --output_dir "$DOMJUDGE_RESULTS_ROOT" \
                $EXPECTED_OUTPUT_CACHE_DIR_FLAG \
                --source dataset \
                --solution_strategy "$_REF_STRATEGY" \
                --timelimit 2 \
                --max_solutions "$JUDGE_MAX_SOLUTIONS" \
                --max_concurrent "$JUDGE_MAX_CONCURRENT" \
                "${JUDGE_SKIP_ARGS[@]}" \
                $RESET_REF_ARG || echo "[WARN] reference judging failed for ${_REF_STRATEGY}/${SPLIT}"
        done
    fi

    for SOURCE in "${SOURCES[@]}"; do
    if [ "$SOURCE" = "dataset" ]; then
        echo "=============================================="
        echo "  split=${SPLIT}  source=dataset (TC distribution analysis)"
        echo "  Data file:    ${REFERENCE_TIMING}"
        echo "  Output:       ${OUTPUT_ROOT}/codecontests/dataset_analysis/${SPLIT}/"
        echo "=============================================="

        if [ ! -f "$REFERENCE_TIMING" ]; then
            echo "  [SKIP] Reference not found: ${REFERENCE_TIMING}"
            continue
        fi

        python code/utils/dataset_tc_analysis.py \
            --timing "$REFERENCE_TIMING" \
            --split "$SPLIT" \
            --output_root "$OUTPUT_ROOT" \
            --dataset "$DATASET" \
            ${PLOT_FLAG}
        continue
    fi

        case "$SOURCE" in
            Base) MODELS_TO_USE=("${BASE_MODELS[@]}") ;;
            API)  MODELS_TO_USE=("${API_MODELS[@]}") ;;
            slow_testcases) MODELS_TO_USE=("${SLOW_TESTCASES_MODES[@]}") ;;
            wedge) MODELS_TO_USE=("wedge") ;;
            evalperf_sas) MODELS_TO_USE=("evalperf_sas") ;;
            wedge_selected_solutions) MODELS_TO_USE=("wedge") ;;
            evalperf_sas_selected_solutions) MODELS_TO_USE=("evalperf_sas") ;;
            react) MODELS_TO_USE=("react") ;;
            *) MODELS_TO_USE=() ;;
        esac

        IS_METHOD_PICKED=false
        FORCED_STRATEGY=""
        case "$SOURCE" in
            wedge_selected_solutions)
                IS_METHOD_PICKED=true
                FORCED_STRATEGY="wedge_solution"
                ;;
            evalperf_sas_selected_solutions)
                IS_METHOD_PICKED=true
                FORCED_STRATEGY="evalperf_sas_solution"
                ;;
        esac
        for MODEL in "${MODELS_TO_USE[@]}"; do
            ACTUAL_MODEL="$MODEL"
            METHOD_FILTER_ARG=""
            case "$MODEL" in
                *-only-m1) ACTUAL_MODEL="${MODEL%-only-m1}"; METHOD_FILTER_ARG="--method_filter boundary" ;;
                *-only-m2) ACTUAL_MODEL="${MODEL%-only-m2}"; METHOD_FILTER_ARG="--method_filter algorithmic" ;;
            esac
            MODEL_SLUG="${MODEL//\//_}"
            MODEL_SLUG="${MODEL_SLUG//./-}"

            case "$SOURCE" in
                slow_testcases) REF_PROMPT_ITER=("${SLOW_TESTCASES_REFINEMENT_PROMPTS[@]}") ;;
                wedge|evalperf_sas|wedge_selected_solutions|evalperf_sas_selected_solutions) REF_PROMPT_ITER=("") ;;
                react)           REF_PROMPT_ITER=("${REACT_VERSION}") ;;
                *)               REF_PROMPT_ITER=("") ;;
            esac

            for REF_PROMPT in "${REF_PROMPT_ITER[@]}"; do

            _REF_PROMPT_SEG="${REF_PROMPT}/"

            case "$SOURCE" in
                Base|API) PROMPT_ITER=("${BASE_PROMPTS[@]}") ;;
                wedge|evalperf_sas|wedge_selected_solutions|evalperf_sas_selected_solutions)
                    PROMPT_ITER=("${PRIOR_WORK_LLM_MODEL[@]}") ;;
                react)    PROMPT_ITER=("${REACT_LLM_MODEL[@]}") ;;
                *)        PROMPT_ITER=("${SLOW_TESTCASES_LLM_MODEL[@]}") ;;
            esac

            for PROMPT in "${PROMPT_ITER[@]}"; do

            if [ "$SOURCE" = "Base" ] || [ "$SOURCE" = "API" ]; then
                ACTIVE_OUTPUT_DIR="output/codecontests/baseline/${PROMPT}/${SPLIT}"
                ACTIVE_JUDGE_TC_DIR="output/codecontests/baseline/${PROMPT}/${SPLIT}"
            fi

            if [ "$IS_METHOD_PICKED" = "true" ]; then
                _STRATEGY_LIST=("$FORCED_STRATEGY")
            elif [ "$ENABLE_MULTI_SOLUTION" = "true" ]; then
                _STRATEGY_LIST=("${SOLUTION_STRATEGIES[@]}")
            else
                _STRATEGY_LIST=("fast_solution")
            fi

            for _STRATEGY in "${_STRATEGY_LIST[@]}"; do

            if [ "$IS_METHOD_PICKED" = "true" ] || [ "$ENABLE_MULTI_SOLUTION" = "true" ]; then
                _STRAT_SUFFIX="/${_STRATEGY}"
                _STRAT_FLAG="--solution_strategy ${_STRATEGY}"
            else
                _STRAT_SUFFIX=""
                _STRAT_FLAG=""
            fi

            if [ "$IS_METHOD_PICKED" = "true" ]; then
                _METHOD_SEL_FILE="${DOMJUDGE_RESULTS_ROOT}/selected_solutions/selected_solutions_${FORCED_STRATEGY}_${SPLIT}.jsonl"
                if [ ! -f "$_METHOD_SEL_FILE" ]; then
                    _WEDGE_PICKS_JSON="../Prior_work/wedge/code-contest-exp/results/alphacode_${SPLIT}/wedge_picks_${SPLIT}.json"
                    if [ -f "$_WEDGE_PICKS_JSON" ]; then
                        echo "  [BUILD] Building ${_METHOD_SEL_FILE} from ${_WEDGE_PICKS_JSON}"
                        python code/utils/build_method_selected_solutions.py \
                            --picks_json "$_WEDGE_PICKS_JSON" \
                            --split "$SPLIT" \
                            --output_dir "${DOMJUDGE_RESULTS_ROOT}/selected_solutions" \
                            --strategies "$FORCED_STRATEGY" \
                            --cache_dir "$JUDGE_CACHE_DIR" || echo "  [WARN] build_method_selected_solutions.py failed"
                    else
                        echo "  [WARN] WEDGE picks JSON not found: ${_WEDGE_PICKS_JSON}"
                        echo "  [WARN] Method-picked evaluation will fall back to standard selected_solutions."
                    fi
                fi
            fi

            REFERENCE_TIMING_FOR_STRATEGY=""
            if [ "$ENABLE_MULTI_SOLUTION" = "true" ]; then
                _CAND_JSONL="${REFERENCE_TIMING_DIR}/${_STRATEGY}/codecontests_timing_${SPLIT}.jsonl"
                _CAND_JSON="${REFERENCE_TIMING_DIR}/${_STRATEGY}/codecontests_timing_${SPLIT}.json"
                if [ -f "$_CAND_JSONL" ]; then
                    REFERENCE_TIMING_FOR_STRATEGY="$_CAND_JSONL"
                elif [ -f "$_CAND_JSON" ]; then
                    REFERENCE_TIMING_FOR_STRATEGY="$_CAND_JSON"
                fi
            fi
            if [ -z "$REFERENCE_TIMING_FOR_STRATEGY" ]; then
                REFERENCE_TIMING_FOR_STRATEGY="$REFERENCE_TIMING"
                if [ "$ENABLE_MULTI_SOLUTION" = "true" ] && [ "$_STRATEGY" != "fast_solution" ]; then
                    echo "  [WARN] Per-strategy reference missing for ${_STRATEGY}; falling back to legacy fast reference (${REFERENCE_TIMING}) — comparison may be mismatched."
                fi
            fi

            _PRIOR_LLM_SUBDIR=""
            case "$SOURCE" in
                wedge|evalperf_sas|wedge_selected_solutions|evalperf_sas_selected_solutions)
                    _PRIOR_LLM="${PROMPT:-${PRIOR_WORK_LLM_MODEL[0]:-}}"
                    _PRIOR_LLM_SUBDIR="${_PRIOR_LLM:+/${_PRIOR_LLM}}"
                    ;;
            esac

            if [ "$SOURCE" == "Base" ] || [ "$SOURCE" == "API" ]; then
                _EVAL_PREFIX="${EVALUAND_TIMING_DIR}/baseline/${MODEL}/${PROMPT}${_STRAT_SUFFIX}/codecontests_timing"
            elif [ "$SOURCE" == "wedge" ] || [ "$SOURCE" == "evalperf_sas" ] || \
                 [ "$SOURCE" == "wedge_selected_solutions" ] || [ "$SOURCE" == "evalperf_sas_selected_solutions" ]; then
                _EVAL_PREFIX="${EVALUAND_TIMING_DIR}/${MODEL}${_PRIOR_LLM_SUBDIR}${_STRAT_SUFFIX}/codecontests_timing"
            else
                _EVAL_PREFIX="${EVALUAND_TIMING_DIR}/our_method/${MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}${_STRAT_SUFFIX}/codecontests_timing"
            fi

            if [[ "$SPLIT" == *_* ]]; then
                IFS='_' read -ra _EVAL_SUBS <<< "$SPLIT"
                _EVAL_MERGE_FILES=()
                for _ESUB in "${_EVAL_SUBS[@]}"; do
                    if [ -f "${_EVAL_PREFIX}_${_ESUB}.jsonl" ]; then
                        _EVAL_MERGE_FILES+=("${_EVAL_PREFIX}_${_ESUB}.jsonl")
                    elif [ -f "${_EVAL_PREFIX}_${_ESUB}.json" ]; then
                        _EVAL_MERGE_FILES+=("${_EVAL_PREFIX}_${_ESUB}.json")
                    fi
                done
                if [ ${#_EVAL_MERGE_FILES[@]} -gt 0 ]; then
                    EVALUAND_TIMING="${_EVAL_PREFIX}_${SPLIT}.jsonl"
                    python3 -c "
import json, sys
out_path = sys.argv[1]
metadata_emitted = False
with open(out_path, 'w') as fo:
    for fp in sys.argv[2:]:
        try:
            with open(fp) as fi:
                d = json.load(fi)
            if isinstance(d, dict) and 'problems' in d:
                if not metadata_emitted and d.get('metadata'):
                    md = dict(d['metadata']); md['type'] = 'metadata'
                    fo.write(json.dumps(md, ensure_ascii=False) + '\n')
                    metadata_emitted = True
                for p in d['problems']:
                    if 'type' not in p: p = {**p, 'type': 'problem'}
                    fo.write(json.dumps(p, ensure_ascii=False) + '\n')
            elif isinstance(d, list):
                for p in d:
                    if isinstance(p, dict) and 'type' not in p: p = {**p, 'type': 'problem'}
                    fo.write(json.dumps(p, ensure_ascii=False) + '\n')
        except json.JSONDecodeError:
            with open(fp) as fi:
                for line in fi:
                    s = line.strip()
                    if not s: continue
                    fo.write(s + '\n')
" "$EVALUAND_TIMING" "${_EVAL_MERGE_FILES[@]}"
                    echo "[INFO] Merged ${#_EVAL_MERGE_FILES[@]} evaluand timing files -> ${EVALUAND_TIMING}"
                else
                    echo "[WARN] No sub-split evaluand files found for compound split '${SPLIT}'"
                    EVALUAND_TIMING="${_EVAL_PREFIX}_${SPLIT}.jsonl"
                fi
            else
                EVALUAND_BASE="${_EVAL_PREFIX}_${SPLIT}"
                if [ -f "${EVALUAND_BASE}.jsonl" ]; then
                    EVALUAND_TIMING="${EVALUAND_BASE}.jsonl"
                else
                    EVALUAND_TIMING="${EVALUAND_BASE}.json"
                fi
            fi

            echo "=============================================="
            if [ "$SOURCE" = "Base" ] || [ "$SOURCE" = "API" ]; then
                echo "  split=${SPLIT}  source=${SOURCE}  prompt=${PROMPT}  model=${MODEL}"
            elif [ "$IS_METHOD_PICKED" = "true" ]; then
                echo "  split=${SPLIT}  source=${SOURCE} (alias of ${MODEL})  strategy=${_STRATEGY}"
            else
                echo "  split=${SPLIT}  source=${SOURCE}  mode=${MODEL}  ref_prompt=${REF_PROMPT}  llm=${PROMPT}"
            fi
            echo "  Reference:    ${REFERENCE_TIMING}"
            echo "  Result root:  ${EVALUAND_TIMING_DIR}"
            echo "  Data file:    ${EVALUAND_TIMING}"
            echo "=============================================="

            if [ "$RUN_JUDGE" = "true" ] && [[ "$SPLIT" == *_* ]]; then
                echo "  [SKIP] RUN_JUDGE not supported for compound split '${SPLIT}'."
                echo "         Run judge separately for each sub-split first, then evaluate with compound split."
                RUN_JUDGE_SKIP=true
            fi
            if [ "$RUN_JUDGE" = "true" ] && [ "${RUN_JUDGE_SKIP:-false}" != "true" ]; then
                NEED_JUDGE=false
                if [ ! -f "$EVALUAND_TIMING" ]; then
                    NEED_JUDGE=true
                elif [ -f "$EVALUAND_TIMING" ]; then
                    if ! grep -qv '"type": *"metadata"' "$EVALUAND_TIMING" 2>/dev/null; then
                        echo "  [RUN_JUDGE] Timing file exists but has 0 problem results — re-running judge"
                        NEED_JUDGE=true
                    else
                        _eval_cnt=$(python3 -c "
import json, sys
try:
    cnt = sum(1 for l in open('${EVALUAND_TIMING}') if l.strip() and json.loads(l).get('type') != 'metadata')
    print(cnt)
except: print(0)" 2>/dev/null || echo "0")
                        _ref_cnt=$(python3 -c "
import json, sys
try:
    data = json.load(open('${REFERENCE_TIMING}'))
    if isinstance(data, list):
        print(len(data))
    elif isinstance(data, dict) and 'problems' in data:
        print(len(data['problems']))
    else:
        print(0)
except: print(0)" 2>/dev/null || echo "0")
                        if [ "$_eval_cnt" -gt 0 ] && [ "$_ref_cnt" -gt 0 ] && [ "$_eval_cnt" -lt "$_ref_cnt" ]; then
                            echo "  [RUN_JUDGE] Timing file incomplete ($_eval_cnt/$_ref_cnt problems) — re-running judge to resume"
                            NEED_JUDGE=true
                        fi
                    fi
                fi
                if [ "$RUN_JUDGE_FORCE" = "true" ]; then
                    NEED_JUDGE=true
                fi
                RESET_ARG=""
                if [ "$RUN_JUDGE_FORCE" = "true" ]; then
                    RESET_ARG="--reset"
                fi
                JUDGE_FAILED=false
                if [ "$NEED_JUDGE" = "true" ]; then
                    if [ "$SOURCE" = "Base" ]; then
                        BASE_OUTPUT_JSON="$(resolve_base_output_path "$ACTIVE_OUTPUT_DIR" "$MODEL" "$SPLIT")"
                        JUDGE_TC_JSON="${ACTIVE_JUDGE_TC_DIR}/${MODEL}_${SPLIT}_output_parsing.json"
                        if [ -f "$BASE_OUTPUT_JSON" ]; then
                            if [ ! -f "$JUDGE_TC_JSON" ]; then
                                echo "  [RUN_JUDGE] Converting Base output to judge TC format: ${JUDGE_TC_JSON}"
                                python code/utils/base_output_to_judge_tc.py \
                                    --input "$BASE_OUTPUT_JSON" \
                                    --output "$JUDGE_TC_JSON" \
                                    --format base \
                                    --split "$SPLIT" \
                                    --cache_dir "$JUDGE_CACHE_DIR" || true
                            fi
                            if [ -f "$JUDGE_TC_JSON" ]; then
                                echo "  [RUN_JUDGE] Expanding compact TCs (v3) if any..."
                                python code/utils/expand_compact_testcases.py \
                                    --input "$JUDGE_TC_JSON" \
                                    --output "$JUDGE_TC_JSON" \
                                    --format judge || true
                            fi
                            if [ -f "$JUDGE_TC_JSON" ]; then
                                echo "  [RUN_JUDGE] Running DOMjudge (source=baseline/${MODEL}/${PROMPT}${_STRAT_SUFFIX}, strategy=${_STRATEGY}) ..."
                                python3 "$JUDGE_SCRIPT" \
                                    --contest_id "$JUDGE_CONTEST_ID" \
                                    --admin_user "$JUDGE_ADMIN_USER" \
                                    --admin_password "$JUDGE_ADMIN_PASSWORD" \
                                    --team_user "$JUDGE_TEAM_USER" \
                                    --team_password "$JUDGE_TEAM_PASSWORD" \
                                    --domjudge_url "$JUDGE_DOMJUDGE_URL" \
                                    --db_host "$JUDGE_DB_HOST" \
                                    --db_port "$JUDGE_DB_PORT" \
                                    --cache_dir "$JUDGE_CACHE_DIR" \
                                    --split "$SPLIT" \
                                    --output_dir "$DOMJUDGE_RESULTS_DIR" \
                                $EXPECTED_OUTPUT_CACHE_DIR_FLAG \
                                    --source "baseline/${MODEL}/${PROMPT}${_STRAT_SUFFIX}" \
                                    --custom_testcases "$JUDGE_TC_JSON" \
                                    --timelimit 2 \
                                    --max_solutions "$JUDGE_MAX_SOLUTIONS" \
                                    --max_concurrent "$JUDGE_MAX_CONCURRENT" \
                                    $_STRAT_FLAG \
                                    "${JUDGE_SKIP_ARGS[@]}" \
                                    $RESET_ARG || JUDGE_FAILED=true
                            fi
                        else
                            echo "  [RUN_JUDGE] Base output not found: ${BASE_OUTPUT_JSON}"
                        fi
                    elif [ "$SOURCE" = "API" ]; then
                        API_RAW_JSONL="${API_OUTPUT_ROOT}/$(get_api_provider "$MODEL")/${PROMPT}/${MODEL}/${SPLIT}/${SPLIT}_${MODEL_SLUG}_raw.jsonl"
                        JUDGE_TC_JSON="${ACTIVE_JUDGE_TC_DIR}/${MODEL}_${SPLIT}_output_parsing.json"
                        if [ -f "$API_RAW_JSONL" ]; then
                            if [ ! -f "$JUDGE_TC_JSON" ]; then
                                echo "  [RUN_JUDGE] Converting API JSONL to judge TC format: ${JUDGE_TC_JSON}"
                                python code/utils/base_output_to_judge_tc.py \
                                    --input "$API_RAW_JSONL" \
                                    --output "$JUDGE_TC_JSON" \
                                    --format api_jsonl \
                                    --split "$SPLIT" \
                                    --cache_dir "$JUDGE_CACHE_DIR" || true
                            fi
                            if [ -f "$JUDGE_TC_JSON" ]; then
                                echo "  [RUN_JUDGE] Expanding compact TCs (v3) if any..."
                                python code/utils/expand_compact_testcases.py \
                                    --input "$JUDGE_TC_JSON" \
                                    --output "$JUDGE_TC_JSON" \
                                    --format judge || true
                            fi
                            if [ -f "$JUDGE_TC_JSON" ]; then
                                echo "  [RUN_JUDGE] Running DOMjudge (source=baseline/${MODEL}/${PROMPT}${_STRAT_SUFFIX}, strategy=${_STRATEGY}) ..."
                                python3 "$JUDGE_SCRIPT" \
                                    --contest_id "$JUDGE_CONTEST_ID" \
                                    --admin_user "$JUDGE_ADMIN_USER" \
                                    --admin_password "$JUDGE_ADMIN_PASSWORD" \
                                    --team_user "$JUDGE_TEAM_USER" \
                                    --team_password "$JUDGE_TEAM_PASSWORD" \
                                    --domjudge_url "$JUDGE_DOMJUDGE_URL" \
                                    --db_host "$JUDGE_DB_HOST" \
                                    --db_port "$JUDGE_DB_PORT" \
                                    --cache_dir "$JUDGE_CACHE_DIR" \
                                    --split "$SPLIT" \
                                    --output_dir "$DOMJUDGE_RESULTS_DIR" \
                                $EXPECTED_OUTPUT_CACHE_DIR_FLAG \
                                    --source "baseline/${MODEL}/${PROMPT}${_STRAT_SUFFIX}" \
                                    --custom_testcases "$JUDGE_TC_JSON" \
                                    --timelimit 2 \
                                    --max_solutions "$JUDGE_MAX_SOLUTIONS" \
                                    --max_concurrent "$JUDGE_MAX_CONCURRENT" \
                                    $_STRAT_FLAG \
                                    "${JUDGE_SKIP_ARGS[@]}" \
                                    $RESET_ARG || JUDGE_FAILED=true
                            fi
                        else
                            echo "  [RUN_JUDGE] API output not found: ${API_RAW_JSONL}"
                        fi
                    elif [ "$SOURCE" = "wedge" ] || [ "$SOURCE" = "evalperf_sas" ] || \
                         [ "$SOURCE" = "wedge_selected_solutions" ] || [ "$SOURCE" = "evalperf_sas_selected_solutions" ]; then
                        SLOW_TC_JSON="output/codecontests/${MODEL}/${SPLIT}${_PRIOR_LLM_SUBDIR}/${MODEL}_testcases_${SPLIT}.json"
                        SLOW_TC_INPUTS_DIR="output/codecontests/${MODEL}/${SPLIT}${_PRIOR_LLM_SUBDIR}/inputs"
                        if [ -d "$SLOW_TC_INPUTS_DIR" ]; then
                            echo "  [RUN_JUDGE] Running DOMjudge via inputs/ dir (source=${MODEL}${_STRAT_SUFFIX}, strategy=${_STRATEGY}) ..."
                            CUSTOM_TC_ARG=""
                            if [ -f "$SLOW_TC_JSON" ]; then
                                CUSTOM_TC_ARG="--custom_testcases $SLOW_TC_JSON"
                            fi
                            python3 "$JUDGE_SCRIPT" \
                                --contest_id "$JUDGE_CONTEST_ID" \
                                --admin_user "$JUDGE_ADMIN_USER" \
                                --admin_password "$JUDGE_ADMIN_PASSWORD" \
                                --team_user "$JUDGE_TEAM_USER" \
                                --team_password "$JUDGE_TEAM_PASSWORD" \
                                --domjudge_url "$JUDGE_DOMJUDGE_URL" \
                                --db_host "$JUDGE_DB_HOST" \
                                --db_port "$JUDGE_DB_PORT" \
                                --cache_dir "$JUDGE_CACHE_DIR" \
                                --split "$SPLIT" \
                                --output_dir "$DOMJUDGE_RESULTS_DIR" \
                                $EXPECTED_OUTPUT_CACHE_DIR_FLAG \
                                --source "${MODEL}${_PRIOR_LLM_SUBDIR}${_STRAT_SUFFIX}" \
                                --inputs_dir "$SLOW_TC_INPUTS_DIR" \
                                --parsed_structures_dir "$PARSED_STRUCTURES_DIR" \
                                $CUSTOM_TC_ARG \
                                --timelimit 2 \
                                --max_solutions "$JUDGE_MAX_SOLUTIONS" \
                                --max_concurrent "$JUDGE_MAX_CONCURRENT" \
                                $_STRAT_FLAG \
                                "${JUDGE_SKIP_ARGS[@]}" \
                                $RESET_ARG || JUDGE_FAILED=true
                        elif [ -f "$SLOW_TC_JSON" ]; then
                            echo "  [RUN_JUDGE] Running DOMjudge via JSON (source=${MODEL}${_STRAT_SUFFIX}, strategy=${_STRATEGY}) ..."
                            python3 "$JUDGE_SCRIPT" \
                                --contest_id "$JUDGE_CONTEST_ID" \
                                --admin_user "$JUDGE_ADMIN_USER" \
                                --admin_password "$JUDGE_ADMIN_PASSWORD" \
                                --team_user "$JUDGE_TEAM_USER" \
                                --team_password "$JUDGE_TEAM_PASSWORD" \
                                --domjudge_url "$JUDGE_DOMJUDGE_URL" \
                                --db_host "$JUDGE_DB_HOST" \
                                --db_port "$JUDGE_DB_PORT" \
                                --cache_dir "$JUDGE_CACHE_DIR" \
                                --split "$SPLIT" \
                                --output_dir "$DOMJUDGE_RESULTS_DIR" \
                                $EXPECTED_OUTPUT_CACHE_DIR_FLAG \
                                --source "${MODEL}${_PRIOR_LLM_SUBDIR}${_STRAT_SUFFIX}" \
                                --custom_testcases "$SLOW_TC_JSON" \
                                --timelimit 2 \
                                --max_solutions "$JUDGE_MAX_SOLUTIONS" \
                                --max_concurrent "$JUDGE_MAX_CONCURRENT" \
                                $_STRAT_FLAG \
                                "${JUDGE_SKIP_ARGS[@]}" \
                                $RESET_ARG || JUDGE_FAILED=true
                        else
                            echo "  [RUN_JUDGE] No test cases found for ${SOURCE} (model=${MODEL}): ${SLOW_TC_INPUTS_DIR} (and ${SLOW_TC_JSON})"
                        fi
                    else
                        SLOW_TC_JSON="output/codecontests/our_method/${SPLIT}/${ACTUAL_MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}/slow_testcases_${SPLIT}.json"
                        SLOW_TC_INPUTS_DIR="output/codecontests/our_method/${SPLIT}/${ACTUAL_MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}/inputs"
                        if [ -d "$SLOW_TC_INPUTS_DIR" ]; then
                            echo "  [RUN_JUDGE] Running DOMjudge via inputs/ dir (source=our_method/${MODEL}/${REF_PROMPT}/${PROMPT}${_STRAT_SUFFIX}, strategy=${_STRATEGY}) ..."
                            CUSTOM_TC_ARG=""
                            if [ -f "$SLOW_TC_JSON" ]; then
                                CUSTOM_TC_ARG="--custom_testcases $SLOW_TC_JSON"
                            fi
                            python3 "$JUDGE_SCRIPT" \
                                --contest_id "$JUDGE_CONTEST_ID" \
                                --admin_user "$JUDGE_ADMIN_USER" \
                                --admin_password "$JUDGE_ADMIN_PASSWORD" \
                                --team_user "$JUDGE_TEAM_USER" \
                                --team_password "$JUDGE_TEAM_PASSWORD" \
                                --domjudge_url "$JUDGE_DOMJUDGE_URL" \
                                --db_host "$JUDGE_DB_HOST" \
                                --db_port "$JUDGE_DB_PORT" \
                                --cache_dir "$JUDGE_CACHE_DIR" \
                                --split "$SPLIT" \
                                --output_dir "$DOMJUDGE_RESULTS_DIR" \
                                $EXPECTED_OUTPUT_CACHE_DIR_FLAG \
                                --source "our_method/${MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}${_STRAT_SUFFIX}" \
                                --inputs_dir "$SLOW_TC_INPUTS_DIR" \
                                --parsed_structures_dir "$PARSED_STRUCTURES_DIR" \
                                $CUSTOM_TC_ARG \
                                $METHOD_FILTER_ARG \
                                --timelimit 2 \
                                --max_solutions "$JUDGE_MAX_SOLUTIONS" \
                                --max_concurrent "$JUDGE_MAX_CONCURRENT" \
                                $_STRAT_FLAG \
                                "${JUDGE_SKIP_ARGS[@]}" \
                                $RESET_ARG || JUDGE_FAILED=true
                        elif [ -f "$SLOW_TC_JSON" ]; then
                            echo "  [RUN_JUDGE] Running DOMjudge via JSON (legacy, source=our_method/${MODEL}/${REF_PROMPT}/${PROMPT}${_STRAT_SUFFIX}, strategy=${_STRATEGY}) ..."
                            python3 "$JUDGE_SCRIPT" \
                                --contest_id "$JUDGE_CONTEST_ID" \
                                --admin_user "$JUDGE_ADMIN_USER" \
                                --admin_password "$JUDGE_ADMIN_PASSWORD" \
                                --team_user "$JUDGE_TEAM_USER" \
                                --team_password "$JUDGE_TEAM_PASSWORD" \
                                --domjudge_url "$JUDGE_DOMJUDGE_URL" \
                                --db_host "$JUDGE_DB_HOST" \
                                --db_port "$JUDGE_DB_PORT" \
                                --cache_dir "$JUDGE_CACHE_DIR" \
                                --split "$SPLIT" \
                                --output_dir "$DOMJUDGE_RESULTS_DIR" \
                                $EXPECTED_OUTPUT_CACHE_DIR_FLAG \
                                --source "our_method/${MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}${_STRAT_SUFFIX}" \
                                --custom_testcases "$SLOW_TC_JSON" \
                                $METHOD_FILTER_ARG \
                                --timelimit 2 \
                                --max_solutions "$JUDGE_MAX_SOLUTIONS" \
                                --max_concurrent "$JUDGE_MAX_CONCURRENT" \
                                $_STRAT_FLAG \
                                "${JUDGE_SKIP_ARGS[@]}" \
                                $RESET_ARG || JUDGE_FAILED=true
                        else
                            echo "  [RUN_JUDGE] No test cases found (inputs_dir or JSON): ${SLOW_TC_INPUTS_DIR}"
                        fi
                    fi
                fi
            fi

            RUN_JUDGE_SKIP=false

            if [ "$JUDGE_FAILED" = "true" ]; then
                echo "  [SKIP] Judge failed (OOM or crash) — skipping evaluation; re-run to resume from checkpoint"
                continue
            fi

            if [ ! -f "$EVALUAND_TIMING" ]; then
                echo "  [SKIP] Evaluand not found — remove from MODELS/SOURCES or add file"
                continue
            fi

            if [ "$SOURCE" = "Base" ] || [ "$SOURCE" = "API" ]; then
                _PARSING="${ACTIVE_JUDGE_TC_DIR}/${MODEL}_${SPLIT}_output_parsing.json"
                if [ ! -f "$_PARSING" ]; then
                    if [ "$SOURCE" = "Base" ]; then
                        _RAW_INPUT="$(resolve_base_output_path "$ACTIVE_OUTPUT_DIR" "$MODEL" "$SPLIT")"
                        _FMT="base"
                    else
                        _RAW_INPUT="${API_OUTPUT_ROOT}/$(get_api_provider "$MODEL")/${PROMPT}/${MODEL}/${SPLIT}/${SPLIT}_${MODEL_SLUG}_raw.jsonl"
                        _FMT="api_jsonl"
                    fi
                    if [ -f "$_RAW_INPUT" ]; then
                        echo "  [CONVERT] parsing.json missing — converting from raw output: ${_PARSING}"
                        python code/utils/base_output_to_judge_tc.py \
                            --input "$_RAW_INPUT" \
                            --output "$_PARSING" \
                            --format "$_FMT" \
                            --split "$SPLIT" \
                            --cache_dir "$JUDGE_CACHE_DIR" || true
                    fi
                fi
            fi

            GENERATOR_OUTPUT_FLAG=""
            if [ "$SOURCE" = "Base" ] || [ "$SOURCE" = "API" ]; then
                GEN_TC="${ACTIVE_JUDGE_TC_DIR}/${MODEL}_${SPLIT}_output_parsing.json"
            elif [ "$SOURCE" = "wedge" ] || [ "$SOURCE" = "evalperf_sas" ] || \
                 [ "$SOURCE" = "wedge_selected_solutions" ] || [ "$SOURCE" = "evalperf_sas_selected_solutions" ]; then
                GEN_TC="output/codecontests/${MODEL}/${SPLIT}${_PRIOR_LLM_SUBDIR}/${MODEL}_testcases_${SPLIT}.json"
            else
                GEN_TC="output/codecontests/our_method/${SPLIT}/${ACTUAL_MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}/slow_testcases_${SPLIT}.json"
                if [ ! -f "$GEN_TC" ]; then
                    GEN_TC="output/codecontests/our_method/${SPLIT}/${ACTUAL_MODEL}/${PROMPT}/slow_testcases_${SPLIT}.json"
                fi
                if [ ! -f "$GEN_TC" ]; then
                    GEN_TC="output/slow_testcases/${SPLIT}/${ACTUAL_MODEL}/${PROMPT}/slow_testcases_${SPLIT}.json"
                fi
            fi

            if [[ "$SPLIT" == *_* ]] && [ ! -f "$GEN_TC" ]; then
                IFS='_' read -ra _GEN_SUBS <<< "$SPLIT"
                _GEN_MERGE_FILES=()
                if [ "$SOURCE" = "Base" ] || [ "$SOURCE" = "API" ]; then
                    _GEN_TC_PATTERN="${BASE_OUTPUT_DIR}/{{SUB}}/${MODEL}_{{SUB}}_output.json"
                elif [ "$SOURCE" = "wedge" ] || [ "$SOURCE" = "evalperf_sas" ] || \
                     [ "$SOURCE" = "wedge_selected_solutions" ] || [ "$SOURCE" = "evalperf_sas_selected_solutions" ]; then
                    _GEN_TC_PATTERN="output/codecontests/${MODEL}/{{SUB}}/${MODEL}_testcases_{{SUB}}.json"
                else
                    _GEN_TC_PATTERN="output/codecontests/our_method/{{SUB}}/${ACTUAL_MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}/slow_testcases_{{SUB}}.json"
                fi
                for _GSUB in "${_GEN_SUBS[@]}"; do
                    _SUB_GEN="${_GEN_TC_PATTERN//\{\{SUB\}\}/${_GSUB}}"
                    if [ -f "$_SUB_GEN" ]; then
                        _GEN_MERGE_FILES+=("$_SUB_GEN")
                    fi
                done
                if [ ${#_GEN_MERGE_FILES[@]} -gt 0 ]; then
                    mkdir -p "$(dirname "$GEN_TC")"
                    python3 -c "
import json, sys
out = []
for fp in sys.argv[1:]:
    data = json.load(open(fp))
    if isinstance(data, list):
        out.extend(data)
    elif isinstance(data, dict):
        out.append(data)
with open('${GEN_TC}', 'w') as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f'merged {len(out)} entries from {len(sys.argv)-1} files')
" "${_GEN_MERGE_FILES[@]}"
                    echo "[INFO] Merged ${#_GEN_MERGE_FILES[@]} sub-split generator TC files → ${GEN_TC}"
                fi
            fi
            if [ "$SOURCE" = "Base" ] || [ "$SOURCE" = "API" ]; then
                if [ -f "$GEN_TC" ]; then
                    GENERATOR_OUTPUT_FLAG="--generator_output $GEN_TC"
                    echo "  Generator TC: ${GEN_TC}"
                else
                    echo "  [WARN] No generator output for target_tier: ${GEN_TC}"
                fi
            elif [ "$SOURCE" = "wedge" ] || [ "$SOURCE" = "evalperf_sas" ] || \
                 [ "$SOURCE" = "wedge_selected_solutions" ] || [ "$SOURCE" = "evalperf_sas_selected_solutions" ]; then
                GEN_INPUTS_DIR="output/codecontests/${MODEL}/${SPLIT}${_PRIOR_LLM_SUBDIR}/inputs"
                if [ -d "$GEN_INPUTS_DIR" ]; then
                    GENERATOR_OUTPUT_FLAG="--generator_inputs_dir $GEN_INPUTS_DIR"
                    echo "  Generator TC (inputs/): ${GEN_INPUTS_DIR}"
                elif [ -f "$GEN_TC" ]; then
                    GENERATOR_OUTPUT_FLAG="--generator_output $GEN_TC"
                    echo "  Generator TC (JSON): ${GEN_TC}"
                else
                    echo "  [WARN] No generator output for target_tier: ${GEN_INPUTS_DIR}"
                fi
            else
                GEN_INPUTS_DIR="output/codecontests/our_method/${SPLIT}/${ACTUAL_MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}/inputs"
                if [[ "$SPLIT" == *_* ]] && [ ! -d "$GEN_INPUTS_DIR" ]; then
                    IFS='_' read -ra _IN_SUBS <<< "$SPLIT"
                    _IN_COPIED=0
                    for _ISUB in "${_IN_SUBS[@]}"; do
                        _SUB_IN_DIR="output/codecontests/our_method/${_ISUB}/${ACTUAL_MODEL}/${_REACT_VERSION_SEG}${_REF_PROMPT_SEG}${PROMPT}/inputs"
                        if [ -d "$_SUB_IN_DIR" ]; then
                            mkdir -p "$GEN_INPUTS_DIR"
                            cp -rn "$_SUB_IN_DIR"/* "$GEN_INPUTS_DIR/" 2>/dev/null || true
                            _IN_COPIED=$((_IN_COPIED + 1))
                        fi
                    done
                    if [ $_IN_COPIED -gt 0 ]; then
                        echo "[INFO] Merged ${_IN_COPIED} sub-split inputs/ dirs → ${GEN_INPUTS_DIR}"
                    fi
                fi
                if [ -d "$GEN_INPUTS_DIR" ]; then
                    GENERATOR_OUTPUT_FLAG="--generator_inputs_dir $GEN_INPUTS_DIR"
                    echo "  Generator TC (inputs/): ${GEN_INPUTS_DIR}"
                elif [ -f "$GEN_TC" ]; then
                    GENERATOR_OUTPUT_FLAG="--generator_output $GEN_TC"
                    echo "  Generator TC (JSON legacy): ${GEN_TC}"
                else
                    echo "  [WARN] No generator output for target_tier: ${GEN_INPUTS_DIR}"
                fi
            fi

            LEF_FLAG=""
            if [ "$LEF_ENABLED" = "true" ]; then
                SEL_SOL_FILE="${LEF_SELECTED_SOLUTIONS_DIR}/selected_solutions_${SPLIT}.jsonl"
                if [[ "$SPLIT" == *_* ]] && [ ! -f "$SEL_SOL_FILE" ]; then
                    IFS='_' read -ra _SEL_SUBS <<< "$SPLIT"
                    _SEL_MERGE_FILES=()
                    for _SSUB in "${_SEL_SUBS[@]}"; do
                        _SUB_SEL="${LEF_SELECTED_SOLUTIONS_DIR}/selected_solutions_${_SSUB}.jsonl"
                        if [ -f "$_SUB_SEL" ]; then
                            _SEL_MERGE_FILES+=("$_SUB_SEL")
                        fi
                    done
                    if [ ${#_SEL_MERGE_FILES[@]} -gt 0 ]; then
                        cat "${_SEL_MERGE_FILES[@]}" > "$SEL_SOL_FILE"
                        echo "[INFO] Merged ${#_SEL_MERGE_FILES[@]} sub-split selected_solutions → ${SEL_SOL_FILE}"
                    fi
                fi
                if [ -f "$SEL_SOL_FILE" ]; then
                    if [ "$SOURCE" = "Base" ] || [ "$SOURCE" = "API" ]; then
                        if [ -n "$GEN_TC" ] && [ -f "$GEN_TC" ]; then
                            LEF_FLAG="--lef --selected_solutions $SEL_SOL_FILE --tc_input_path $GEN_TC --hf_cache_dir $LEF_HF_CACHE_DIR --lef_seed $LEF_SEED --lef_timeout $LEF_TIMEOUT --lef_workers $LEF_WORKERS"
                        fi
                    else
                        if [ -n "$GEN_INPUTS_DIR" ] && [ -d "$GEN_INPUTS_DIR" ]; then
                            LEF_FLAG="--lef --selected_solutions $SEL_SOL_FILE --generator_inputs_dir $GEN_INPUTS_DIR --hf_cache_dir $LEF_HF_CACHE_DIR --lef_seed $LEF_SEED --lef_timeout $LEF_TIMEOUT --lef_workers $LEF_WORKERS"
                        elif [ -n "$GEN_TC" ] && [ -f "$GEN_TC" ]; then
                            LEF_FLAG="--lef --selected_solutions $SEL_SOL_FILE --tc_input_path $GEN_TC --hf_cache_dir $LEF_HF_CACHE_DIR --lef_seed $LEF_SEED --lef_timeout $LEF_TIMEOUT --lef_workers $LEF_WORKERS"
                        fi
                    fi
                fi
                if [ -z "$LEF_FLAG" ]; then
                    echo "  [LEF] Skipping: selected_solutions or TC input not found"
                    echo "    selected_solutions: ${SEL_SOL_FILE}"
                    echo "    tc_input_path:      ${GEN_TC}"
                    if [ "$SOURCE" != "Base" ] && [ "$SOURCE" != "API" ]; then
                        echo "    generator_inputs_dir: ${GEN_INPUTS_DIR}"
                    fi
                fi
            fi

            PROMPT_FLAG=""
            LLM_MODEL_FLAG=""
            REFINEMENT_PROMPT_FLAG=""
            if [ "$SOURCE" = "Base" ] || [ "$SOURCE" = "API" ]; then
                PROMPT_FLAG="--prompt ${PROMPT}"
            elif [ "$SOURCE" = "wedge" ] || [ "$SOURCE" = "evalperf_sas" ] || \
                 [ "$SOURCE" = "wedge_selected_solutions" ] || [ "$SOURCE" = "evalperf_sas_selected_solutions" ]; then
                :
            else
                LLM_MODEL_FLAG="--llm_model ${PROMPT}"
                if [ -n "$REF_PROMPT" ]; then
                    REFINEMENT_PROMPT_FLAG="--refinement_prompt ${REF_PROMPT}"
                fi
            fi

            EVAL_SOURCE="$SOURCE"
            if [ "$SOURCE" = "API" ]; then
                EVAL_SOURCE="Base"
            fi
            case "$SOURCE" in
                wedge|evalperf_sas|wedge_selected_solutions|evalperf_sas_selected_solutions)
                    EVAL_SOURCE="${EVAL_SOURCE}${_PRIOR_LLM_SUBDIR}"
                    ;;
            esac

            STRATEGY_FLAG=""
            if [ "$IS_METHOD_PICKED" = "true" ] || [ "$ENABLE_MULTI_SOLUTION" = "true" ]; then
                STRATEGY_FLAG="--solution_strategy ${_STRATEGY}"
            fi

            LLM_CONSTRAINTS_FLAG=""
            if [ -n "$LLM_CONSTRAINTS_PROVIDER" ] && [ -n "$LLM_CONSTRAINTS_MODEL" ]; then
                _LLM_CONST_PATH="output/generate_constraints/${SPLIT}_${LLM_CONSTRAINTS_PROVIDER}_${LLM_CONSTRAINTS_MODEL}_constraints.jsonl"
                if [[ "$SPLIT" == *_* ]] && [ ! -f "$_LLM_CONST_PATH" ]; then
                    IFS='_' read -ra _LC_SUBS <<< "$SPLIT"
                    _LC_MERGE_FILES=()
                    for _LCSUB in "${_LC_SUBS[@]}"; do
                        _SUB_LC="output/generate_constraints/${_LCSUB}_${LLM_CONSTRAINTS_PROVIDER}_${LLM_CONSTRAINTS_MODEL}_constraints.jsonl"
                        if [ -f "$_SUB_LC" ]; then
                            _LC_MERGE_FILES+=("$_SUB_LC")
                        fi
                    done
                    if [ ${#_LC_MERGE_FILES[@]} -gt 0 ]; then
                        cat "${_LC_MERGE_FILES[@]}" > "$_LLM_CONST_PATH"
                        echo "  [INFO] Merged ${#_LC_MERGE_FILES[@]} sub-split LLM constraints → $_LLM_CONST_PATH"
                    fi
                fi
                if [ -f "$_LLM_CONST_PATH" ]; then
                    LLM_CONSTRAINTS_FLAG="--llm_constraints_path $_LLM_CONST_PATH"
                else
                    echo "  [WARN] LLM constraints file not found: $_LLM_CONST_PATH (TLE-compliant metric will be skipped)"
                fi
            fi

            LLM_CONSTRAINTS_BOUNDARY_FLAG=""
            if [ -n "$LLM_CONSTRAINTS_BOUNDARY_PROVIDER" ] && [ -n "$LLM_CONSTRAINTS_BOUNDARY_MODEL" ]; then
                _LLM_CONST_BD_PATH="output/generate_constraints/${SPLIT}_${LLM_CONSTRAINTS_BOUNDARY_PROVIDER}_${LLM_CONSTRAINTS_BOUNDARY_MODEL}_constraints_boundary.jsonl"
                if [[ "$SPLIT" == *_* ]] && [ ! -f "$_LLM_CONST_BD_PATH" ]; then
                    IFS='_' read -ra _LCB_SUBS <<< "$SPLIT"
                    _LCB_MERGE=()
                    for _LCBSUB in "${_LCB_SUBS[@]}"; do
                        _SUB_LCB="output/generate_constraints/${_LCBSUB}_${LLM_CONSTRAINTS_BOUNDARY_PROVIDER}_${LLM_CONSTRAINTS_BOUNDARY_MODEL}_constraints_boundary.jsonl"
                        [ -f "$_SUB_LCB" ] && _LCB_MERGE+=("$_SUB_LCB")
                    done
                    if [ ${#_LCB_MERGE[@]} -gt 0 ]; then
                        cat "${_LCB_MERGE[@]}" > "$_LLM_CONST_BD_PATH"
                        echo "  [INFO] Merged ${#_LCB_MERGE[@]} sub-split boundary constraints → $_LLM_CONST_BD_PATH"
                    fi
                fi
                if [ -f "$_LLM_CONST_BD_PATH" ]; then
                    LLM_CONSTRAINTS_BOUNDARY_FLAG="--llm_constraints_path_boundary $_LLM_CONST_BD_PATH"
                else
                    echo "  [INFO] Boundary-only LLM constraints not found ($_LLM_CONST_BD_PATH); skipping boundary block"
                fi
            fi

            LLM_STDIN_SCHEMA_FLAG=""
            if [ -n "$LLM_STDIN_SCHEMA_PROVIDER" ] && [ -n "$LLM_STDIN_SCHEMA_MODEL" ]; then
                _LLM_SCHEMA_PATH="${LLM_STDIN_SCHEMA_DIR}/${SPLIT}_${LLM_STDIN_SCHEMA_PROVIDER}_${LLM_STDIN_SCHEMA_MODEL}_schema.jsonl"
                if [[ "$SPLIT" == *_* ]] && [ ! -f "$_LLM_SCHEMA_PATH" ]; then
                    IFS='_' read -ra _LS_SUBS <<< "$SPLIT"
                    _LS_MERGE=()
                    for _LSSUB in "${_LS_SUBS[@]}"; do
                        _SUB_LS="${LLM_STDIN_SCHEMA_DIR}/${_LSSUB}_${LLM_STDIN_SCHEMA_PROVIDER}_${LLM_STDIN_SCHEMA_MODEL}_schema.jsonl"
                        [ -f "$_SUB_LS" ] && _LS_MERGE+=("$_SUB_LS")
                    done
                    if [ ${#_LS_MERGE[@]} -gt 0 ]; then
                        cat "${_LS_MERGE[@]}" > "$_LLM_SCHEMA_PATH"
                        echo "  [INFO] Merged ${#_LS_MERGE[@]} sub-split stdin schemas → $_LLM_SCHEMA_PATH"
                    fi
                fi
                if [ -f "$_LLM_SCHEMA_PATH" ]; then
                    LLM_STDIN_SCHEMA_FLAG="--llm_stdin_schema_path $_LLM_SCHEMA_PATH"
                else
                    echo "  [INFO] LLM stdin schema not found ($_LLM_SCHEMA_PATH); falling back to parsed_structures"
                fi
            fi

            python code/utils/slow_testcase_evaluation.py \
                --reference_timing "$REFERENCE_TIMING_FOR_STRATEGY" \
                --evaluand_timing "$EVALUAND_TIMING" \
                --split "$SPLIT" \
                --dataset "$DATASET" \
                --source "$EVAL_SOURCE" \
                --model "$MODEL" \
                --output_root "$OUTPUT_ROOT" \
                --tcs_per_tier "$NUM_TESTCASES" \
                --eval_tiers "$EVAL_TIERS" \
                --slow_ratio_threshold "$SLOW_RATIO_THRESHOLD" \
                --parsed_structures_dir "$PARSED_STRUCTURES_DIR" \
                $LLM_CONSTRAINTS_FLAG \
                $LLM_CONSTRAINTS_BOUNDARY_FLAG \
                $LLM_STDIN_SCHEMA_FLAG \
                $PROMPT_FLAG \
                $LLM_MODEL_FLAG \
                $REFINEMENT_PROMPT_FLAG \
                $GENERATOR_OUTPUT_FLAG \
                $LEF_FLAG \
                $PLOT_FLAG \
                $STRATEGY_FLAG

            done

            if [ "$ENABLE_MULTI_SOLUTION" = "true" ] && [ "$IS_METHOD_PICKED" != "true" ]; then
                echo "  [TOTAL_AVG] Computing averaged metrics across ${#SOLUTION_STRATEGIES[@]} strategies..."
                python code/utils/slow_testcase_evaluation.py \
                    --compute_total_avg \
                    --split "$SPLIT" \
                    --dataset "$DATASET" \
                    --source "$EVAL_SOURCE" \
                    --model "$MODEL" \
                    --output_root "$OUTPUT_ROOT" \
                    --eval_tiers "$EVAL_TIERS" \
                    $PROMPT_FLAG \
                    $LLM_MODEL_FLAG \
                    $REFINEMENT_PROMPT_FLAG \
                    --strategies ${SOLUTION_STRATEGIES[*]}
            fi

            done
            done
        done
    done
done

if [ "$ENABLE_MULTI_SOLUTION" = "true" ]; then
    SEL_SOL_DIR="${DOMJUDGE_RESULTS_ROOT}/selected_solutions"
    FINAL_SEL_DIR="${SEL_SOL_DIR}/final_selected_solutions"
    for SPLIT in "${SPLITS[@]}"; do
        if [[ "$SPLIT" == *_* ]]; then
            echo "  [SKIP] Skipping final_selected_solutions export for compound split '${SPLIT}' (not in HF dataset)."
            continue
        fi
        SEL_SOL_FILE="${SEL_SOL_DIR}/selected_solutions_${SPLIT}.jsonl"
        if [ -f "$SEL_SOL_FILE" ]; then
            echo "=============================================="
            echo "  Exporting final_selected_solutions (split=${SPLIT})"
            echo "  Output: ${FINAL_SEL_DIR}/${SPLIT}/"
            echo "=============================================="
            python code/utils/slow_testcase_evaluation.py \
                --export_final_selected \
                --split "$SPLIT" \
                --selected_solutions_dir "$SEL_SOL_DIR" \
                --hf_cache_dir "$JUDGE_CACHE_DIR" \
                --final_selected_output_dir "$FINAL_SEL_DIR" \
                --multi_solution_seed "$MULTI_SOLUTION_SEED"
        else
            echo "  [SKIP] selected_solutions not found for split=${SPLIT}: ${SEL_SOL_FILE}"
        fi
    done
fi

if [[ " ${SOURCES[*]} " =~ " dataset " ]] && [ ${#SPLITS[@]} -gt 1 ]; then
    TIMING_ARGS=()
    SPLIT_ARGS=()
    for SPLIT in "${SPLITS[@]}"; do
        if [ -f "${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.jsonl" ]; then
            TIMING_ARGS+=("${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.jsonl")
        elif [ -f "${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.json" ]; then
            TIMING_ARGS+=("${REFERENCE_TIMING_DIR}/codecontests_timing_${SPLIT}.json")
        else
            continue
        fi
        SPLIT_ARGS+=("$SPLIT")
    done
    if [ ${#TIMING_ARGS[@]} -gt 1 ]; then
        MERGED_NAME=$(IFS=_; echo "${SPLIT_ARGS[*]}")
        echo "=============================================="
        echo "  Merged dataset analysis: ${MERGED_NAME}"
        echo "  Output: ${OUTPUT_ROOT}/codecontests/dataset_analysis/${MERGED_NAME}/"
        echo "=============================================="
        python code/utils/dataset_tc_analysis.py \
            --timing "${TIMING_ARGS[@]}" \
            --split "${SPLIT_ARGS[@]}" \
            --output_root "$OUTPUT_ROOT" \
            --dataset "$DATASET" \
            ${PLOT_FLAG}
    fi
fi

echo "=============================================="
echo "Done."
echo "=============================================="
