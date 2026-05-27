#!/bin/bash

set -e

CONTEST_ID="dj-2"
ADMIN_USER="admin"
ADMIN_PASSWORD="${DOMJUDGE_ADMIN_PASSWORD:-changeme}"
TEAM_USER="test_user"
TEAM_PASSWORD="${DOMJUDGE_TEAM_PASSWORD:-changeme}"
DOMJUDGE_URL="http://localhost:50043"

DOMJUDGE_URLS="http://localhost:50043,http://localhost:50044"

DB_HOST="localhost"
DB_PORT_BASE=50035

CACHE_DIR="${HF_CACHE_DIR:-/tmp/huggingface-cache}"
TIMELIMIT=2
POLL_INTERVAL=2
POLL_TIMEOUT=60000

SPLITS=(
    "test"
    "valid"
)
MAX_SOLUTIONS=1

SEL_MAX_SOLUTIONS=0

MAX_PROBLEMS=0

OUTPUT_DIR="Research_start_code/domjudge/results"

RESET=false

TC_SAMPLE_RATIO=0.1
TC_LARGEST_RATIO=0.5

MAX_CONCURRENT=8

MAX_PENDING=0

MAX_PHASE_RETRIES=5

TRAIN_SAMPLE_RATIO=0.1

TRAIN_SAMPLE_SEED=42

SKIP_PROBLEMS="791_A. Bear and Big Brother"

SOURCE="dataset"

CUSTOM_TESTCASES=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

check_prerequisites() {
    if ! command -v python3 &>/dev/null; then
        err "python3 not found. Please run: conda activate Research_start"
        exit 1
    fi

    python3 -c "import requests, datasets" 2>/dev/null || {
        err "Required packages (requests, datasets) not found. Please run: conda activate Research_start"
        exit 1
    }

    local check_url="${DOMJUDGE_URL%%,*}"
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
        "${check_url}/api/v4/contests/${CONTEST_ID}" 2>/dev/null || echo "000")
    if [ "$http_code" = "000" ]; then
        err "Cannot connect to DOMjudge domserver (${check_url})"
        err "Start it first: sh Research_start_code/domjudge/scripts/domjudge_server3_start.sh server"
        exit 1
    elif [ "$http_code" != "200" ]; then
        err "Contest '${CONTEST_ID}' access failed (HTTP ${http_code})"
        err "Please verify the contest exists."
        exit 1
    fi
    ok "DOMjudge domserver connection verified (contest: ${CONTEST_ID})"

    local all_urls="${DOMJUDGE_URLS:-$check_url}"
    local total_active=0
    local idx=0
    local old_ifs="$IFS"
    IFS=','
    for url in $all_urls; do
        IFS="$old_ifs"
        local judgehosts active_count
        judgehosts=$(curl -s -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
            "${url}/api/v4/judgehosts" 2>/dev/null)
        active_count=$(echo "$judgehosts" | python3 -c "
import json, sys
try:
    hosts = json.load(sys.stdin)
    print(sum(1 for h in hosts if h.get('enabled', False) and h.get('polltime') is not None))
except:
    print(0)
" 2>/dev/null)
        if [ "$active_count" = "0" ]; then
            warn "domserver-${idx} (${url}): No active judgehosts"
        else
            ok "domserver-${idx} (${url}): Active judgehosts: ${active_count}"
        fi
        total_active=$((total_active + active_count))
        idx=$((idx + 1))
        IFS=','
    done
    IFS="$old_ifs"
    if [ "$total_active" = "0" ]; then
        warn "No active judgehosts across all domservers. Start judgehost: sh Research_start_code/domjudge/scripts/domjudge_start.sh judge"
    else
        ok "Total active judgehosts: ${total_active} across ${idx} domserver(s)"
    fi
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

info "=== CodeContests -> DOMjudge Auto Run ==="
echo ""

check_prerequisites
echo ""

cd "$PROJECT_DIR"

for SPLIT in "${SPLITS[@]}"; do
    info "Starting: split=${SPLIT}, source=${SOURCE}, max_solutions=${MAX_SOLUTIONS}, max_problems=${MAX_PROBLEMS}"
    info "Output directory: ${OUTPUT_DIR}/${SOURCE}/"
    echo ""

    CUSTOM_TC_ARG=""
    if [ -n "$CUSTOM_TESTCASES" ]; then
        CUSTOM_TC_ARG="--custom_testcases $CUSTOM_TESTCASES"
    fi

    RESET_ARG=""
    if [ "$RESET" = true ]; then
        RESET_ARG="--reset"
    fi

    DOMJUDGE_URL_ARGS=""
    if [ -n "$DOMJUDGE_URLS" ]; then
        DOMJUDGE_URL_ARGS="--domjudge_urls $DOMJUDGE_URLS --db_host $DB_HOST --db_port_base $DB_PORT_BASE --db_port $DB_PORT_BASE"
    else
        DOMJUDGE_URL_ARGS="--domjudge_url $DOMJUDGE_URL"
    fi

    python3 -u Research_start_code/domjudge/codecontests_judge.py \
        --contest_id "$CONTEST_ID" \
        --admin_user "$ADMIN_USER" \
        --admin_password "$ADMIN_PASSWORD" \
        --team_user "$TEAM_USER" \
        --team_password "$TEAM_PASSWORD" \
        $DOMJUDGE_URL_ARGS \
        --cache_dir "$CACHE_DIR" \
        --split "$SPLIT" \
        --output_dir "$OUTPUT_DIR" \
        --source "$SOURCE" \
        $CUSTOM_TC_ARG \
        $RESET_ARG \
        --timelimit "$TIMELIMIT" \
        --max_solutions "$MAX_SOLUTIONS" \
        --sel_max_solutions "$SEL_MAX_SOLUTIONS" \
        --max_problems "$MAX_PROBLEMS" \
        --poll_interval "$POLL_INTERVAL" \
        --poll_timeout "$POLL_TIMEOUT" \
        --tc_sample_ratio "$TC_SAMPLE_RATIO" \
        --tc_largest_ratio "$TC_LARGEST_RATIO" \
        --max_concurrent "$MAX_CONCURRENT" \
        --max_pending "$MAX_PENDING" \
        --max_phase_retries "$MAX_PHASE_RETRIES" \
        --train_sample_ratio "$TRAIN_SAMPLE_RATIO" \
        --train_sample_seed "$TRAIN_SAMPLE_SEED" \
        --skip_problems "$SKIP_PROBLEMS" \
        "$@"

    echo ""
done

ok "Done! Result files:"
ls -lh "${OUTPUT_DIR}"/codecontests_timing_*.json 2>/dev/null || warn "No result files found."
