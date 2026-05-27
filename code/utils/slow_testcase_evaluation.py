#!/usr/bin/env python3

import argparse
import ast as _ast_module
import json
import logging
import os
import random
import re as _re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import Counter, defaultdict
from typing import Optional

try:
    from tqdm import tqdm as _tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

try:
    from utils.generator_executor import execute_generator
except ImportError:
    execute_generator = None

try:
    from utils.tc_constraint_validator import (
        parse_stdin as _tc_parse_stdin,
        validate_against_constraints as _tc_validate_constraints,
    )
except ImportError:
    try:
        from tc_constraint_validator import (
            parse_stdin as _tc_parse_stdin,
            validate_against_constraints as _tc_validate_constraints,
        )
    except ImportError:
        _tc_parse_stdin = None
        _tc_validate_constraints = None

COLORS = {"fast": "#2ecc71", "medium": "#f39c12", "slow": "#e74c3c"}
DIFFICULTY_ORDER = ["fast", "medium", "slow"]

_EMIT_LEGACY_CRITERIA = False

_EMIT_RATIO_CRITERION = False
_EMIT_TLE_CRITERION   = True

_EMIT_NON_BINARY_CRITERIA = True

_generator_method_map: dict = {}

_M1_METHODS = {"boundary_slow", "boundary_slow_compact"}
_M2_METHODS = {"algorithmic_slow"}

def _classify_method(method_str: str) -> str:
    if method_str in _M1_METHODS:
        return "M1"
    if method_str in _M2_METHODS:
        return "M2"
    return "unknown"

def load_timing_results(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        problems = []
        metadata = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("type") == "metadata":
                    metadata = entry
                elif "solutions" in entry:
                    problems.append(entry)
        log.info("Loaded %s (JSONL): %d problems", path.name, len(problems))
        return {"metadata": metadata, "problems": problems}
    else:
        with open(path) as f:
            data = json.load(f)
        problems = data.get("problems", [])
        metadata = data.get("metadata", {})
        log.info("Loaded %s: %d problems", path.name, len(problems))
        return {"metadata": metadata, "problems": problems}

def load_generator_tier_map(custom_tc_path: Path) -> dict:
    global _generator_method_map
    with open(custom_tc_path) as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        log.warning("Generator output is not a list, skipping tier map")
        return {}

    is_format_c = False
    for entry in entries:
        if not entry.get("name"):
            continue
        tier_at_record = entry.get("tier")
        has_nested_b = isinstance(entry.get("testcases"), list) and entry["testcases"]
        has_nested_a = isinstance(entry.get("test_cases"), dict) and entry["test_cases"]
        if tier_at_record in ("fast", "medium", "slow") and not has_nested_a and not has_nested_b:
            is_format_c = True
        break

    if is_format_c:
        tier_map = {}
        method_map = {}
        per_name_tiers = defaultdict(list)
        per_name_methods = defaultdict(list)
        for entry in entries:
            name = entry.get("name", "")
            tier = entry.get("tier")
            if not name or tier not in ("fast", "medium", "slow"):
                continue
            per_name_tiers[name].append(tier)
            per_name_methods[name].append(entry.get("method", ""))
        for name, tiers in per_name_tiers.items():
            tier_map[name] = {i + 1: t for i, t in enumerate(tiers)}
            method_map[name] = {i + 1: m for i, m in enumerate(per_name_methods[name])}
        _generator_method_map = method_map
        log.info("Loaded generator tier map (Format C, flat-list): %d problems, %d TCs total from %s",
                 len(tier_map), sum(len(m) for m in tier_map.values()), custom_tc_path.name)
        return tier_map

    tier_map = {}
    method_map = {}
    for entry in entries:
        name = entry.get("name", "")
        if not name:
            continue
        mapping = {}
        m_mapping = {}

        testcases = entry.get("testcases", [])
        if isinstance(testcases, list) and testcases:
            has_tier = any((tc.get("_tier") or tc.get("tier")) in ("fast", "medium", "slow") for tc in testcases)
            if has_tier:
                for tc_idx, tc in enumerate(testcases):
                    tier = tc.get("_tier") or tc.get("tier")
                    if tier in ("fast", "medium", "slow"):
                        mapping[tc_idx + 1] = tier
                    m_mapping[tc_idx + 1] = tc.get("_method", "")
            else:
                n = len(testcases)
                per_tier = n // 3 if n >= 3 else 1
                tiers_ordered = ["fast"] * per_tier + ["medium"] * per_tier + ["slow"] * (n - 2 * per_tier)
                for tc_idx in range(min(n, len(tiers_ordered))):
                    mapping[tc_idx + 1] = tiers_ordered[tc_idx]
                    m_mapping[tc_idx + 1] = testcases[tc_idx].get("_method", "") if tc_idx < n else ""
            if mapping:
                tier_map[name] = mapping
                method_map[name] = m_mapping
            continue

        tc_data = entry.get("test_cases", {})
        if not isinstance(tc_data, dict):
            continue
        tc_num = 1
        for tier in ("fast", "medium", "slow"):
            for _ in tc_data.get(tier, []):
                mapping[tc_num] = tier
                tc_num += 1
        if mapping:
            tier_map[name] = mapping

    _generator_method_map = method_map
    log.info("Loaded generator tier map: %d problems from %s", len(tier_map), custom_tc_path.name)
    return tier_map

def load_generator_tier_map_from_inputs_dir(inputs_dir: Path) -> dict:
    global _generator_method_map
    from collections import defaultdict

    name_tier_files: dict = defaultdict(lambda: {"fast": [], "medium": [], "slow": []})
    name_tier_methods: dict = defaultdict(lambda: {"fast": [], "medium": [], "slow": []})

    for tier in ("fast", "medium", "slow"):
        tier_dir = inputs_dir / tier
        if not tier_dir.exists():
            continue
        for fpath in sorted(tier_dir.iterdir()):
            if fpath.suffix != ".json":
                continue
            try:
                with open(fpath) as _f:
                    tc = json.load(_f)
            except Exception:
                continue
            name = tc.get("name", "")
            if not name:
                continue
            name_tier_files[name][tier].append(fpath.name)
            name_tier_methods[name][tier].append(tc.get("method", ""))

    tier_map: dict = {}
    method_map: dict = {}
    for name, tier_files in name_tier_files.items():
        mapping: dict = {}
        m_mapping: dict = {}
        ordinal = 1
        for tier in ("fast", "medium", "slow"):
            methods_list = name_tier_methods[name][tier]
            sorted_files = sorted(tier_files[tier])
            for i, _fname in enumerate(sorted_files):
                mapping[ordinal] = tier
                m_mapping[ordinal] = methods_list[i] if i < len(methods_list) else ""
                ordinal += 1
        if mapping:
            tier_map[name] = mapping
            method_map[name] = m_mapping

    _generator_method_map = method_map
    log.info("Loaded generator tier map from inputs/: %d problems, %d TCs from %s",
             len(tier_map), sum(len(m) for m in tier_map.values()), inputs_dir)
    return tier_map

def _parse_threshold_range(s: str) -> Optional[tuple[float, float]]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip().rstrip("s").strip()
    parts = s.split("~")
    if len(parts) != 2:
        return None
    try:
        low = float(parts[0].strip())
        high = float(parts[1].strip())
        return (low, high)
    except (ValueError, TypeError):
        return None

def get_per_language_tier_bounds(problem: dict) -> dict:
    timelimit = problem.get("timelimit")
    if timelimit is not None and timelimit <= 0:
        timelimit = None
    t_max_default = float(timelimit) if timelimit is not None else None

    out = {}
    for sol in problem.get("solutions", []):
        st = sol.get("speed_tiers")
        if not st:
            continue
        lang = sol.get("language_id") or sol.get("language", "")
        if not lang:
            continue
        r = st.get("range", {})
        t_min = r.get("min")
        t_max = t_max_default if t_max_default is not None else r.get("max")

        thresholds = st.get("thresholds")
        fast_upper = None
        medium_upper = None
        if thresholds and isinstance(thresholds, dict):
            fast_str = thresholds.get("fast")
            if fast_str:
                parsed = _parse_threshold_range(fast_str)
                if parsed:
                    if t_min is None:
                        t_min = parsed[0]
                    fast_upper = parsed[1]
            medium_str = thresholds.get("medium")
            if medium_str:
                parsed = _parse_threshold_range(medium_str)
                if parsed:
                    medium_upper = parsed[1]

        if fast_upper is None or medium_upper is None:
            if t_min is None or t_max is None:
                continue
            span = t_max - t_min
            fast_upper = t_min + span / 3.0
            medium_upper = t_min + span * 2.0 / 3.0
        if t_min is None:
            t_min = 0.0
        if t_max is None:
            t_max = float(timelimit) if timelimit is not None else (medium_upper if medium_upper is not None else 1.0)

        slow_upper = t_max
        out[lang] = {"fast_upper": fast_upper, "medium_upper": medium_upper, "slow_upper": slow_upper, "t_min": t_min, "t_max": t_max}
    return out

def get_per_language_max_ref_time(problem: dict) -> dict:
    out = {}
    for sol in problem.get("solutions", []):
        st = sol.get("speed_tiers")
        if not st:
            continue
        lang = sol.get("language_id") or sol.get("language", "")
        if not lang:
            continue
        r = st.get("range", {})
        t_max = r.get("max")
        if t_max is not None:
            t_max = float(t_max)
            if lang not in out or t_max > out[lang]:
                out[lang] = t_max
    return out

def classify_run_by_bounds(run_time: float, fast_upper: float, medium_upper: float) -> str:
    if run_time <= fast_upper:
        return "fast"
    if run_time <= medium_upper:
        return "medium"
    return "slow"

def classify_evaluand_by_reference_thresholds(
    reference_problem: dict, evaluand_problem: dict
) -> Optional[dict]:
    ref_bounds = get_per_language_tier_bounds(reference_problem)
    if not ref_bounds:
        return None

    lang_counts = defaultdict(lambda: {"fast": 0, "medium": 0, "slow": 0})
    for sol in evaluand_problem.get("solutions", []):
        lang = sol.get("language_id") or sol.get("language", "")
        bounds = ref_bounds.get(lang)
        if not bounds:
            continue
        fu = bounds["fast_upper"]
        mu = bounds["medium_upper"]
        for run in sol.get("runs", []):
            t = run.get("run_time")
            if t is None:
                continue
            tier = classify_run_by_bounds(t, fu, mu)
            lang_counts[lang][tier] += 1

    return dict(lang_counts)

TIER_RANK = {"fast": 0, "medium": 1, "slow": 2}

def classify_ref_bounds(testcases: list, ref_bounds: dict) -> dict:
    counts = defaultdict(int)
    classified = []

    for tc in testcases:
        if tc.get("tc_verdict") in ("wrong-answer", "timelimit"):
            counts["invalid"] += 1
            classified.append({
                "testcase": tc.get("testcase"),
                "language": None,
                "ratio": None,
                "difficulty": "invalid",
                "tc_verdict": "wrong-answer",
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
            })
            continue
        pl = tc.get("per_language", {})
        for lang, data in pl.items():
            run_time = data.get("run_time")
            if run_time is None:
                continue
            bounds = ref_bounds.get(lang)
            if not bounds:
                diff = "fast"
            else:
                diff = classify_run_by_bounds(
                    run_time, bounds["fast_upper"], bounds["medium_upper"]
                )
            counts[diff] += 1
            classified.append({
                "testcase": tc.get("testcase"),
                "language": lang,
                "run_time": run_time,
                "ratio": data.get("ratio", 0.0),
                "difficulty": diff,
                "tc_verdict": tc.get("tc_verdict"),
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
            })

    thresholds = {
        "method": "reference_per_language_bounds",
        "languages": {
            lang: f"fast<={b['fast_upper']:.4f}s, medium<={b['medium_upper']:.4f}s, slow>{b['medium_upper']:.4f}s"
            for lang, b in ref_bounds.items()
        },
    }
    return {"thresholds": thresholds, "counts": dict(counts), "testcases": classified}

def classify_ratio_based(testcases: list, max_ref_times: dict,
                         threshold: float = 0.5) -> dict:
    counts = defaultdict(int)
    classified = []

    for tc in testcases:
        if tc.get("tc_verdict") in ("wrong-answer", "timelimit"):
            counts["invalid"] += 1
            classified.append({
                "testcase": tc.get("testcase"),
                "language": None,
                "ratio": None,
                "difficulty": "invalid",
                "tc_verdict": "wrong-answer",
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
            })
            continue
        pl = tc.get("per_language", {})
        for lang, data in pl.items():
            run_time = data.get("run_time")
            if run_time is None:
                continue
            max_rt = max_ref_times.get(lang)
            if not max_rt or max_rt <= 0:
                diff = "fast"
            else:
                slow_bound = max_rt * threshold
                fast_bound = max_rt * threshold / 2.0
                if run_time >= slow_bound:
                    diff = "slow"
                elif run_time >= fast_bound:
                    diff = "medium"
                else:
                    diff = "fast"
            counts[diff] += 1
            classified.append({
                "testcase": tc.get("testcase"),
                "language": lang,
                "run_time": run_time,
                "ratio": data.get("ratio", 0.0),
                "difficulty": diff,
                "tc_verdict": tc.get("tc_verdict"),
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
            })

    thresholds = {
        "method": f"ratio_based (threshold={threshold})",
        "languages": {
            lang: f"fast<{max_rt * threshold / 2.0:.4f}s, "
                  f"medium<{max_rt * threshold:.4f}s, "
                  f"slow>={max_rt * threshold:.4f}s (max_ref={max_rt:.4f}s)"
            for lang, max_rt in max_ref_times.items() if max_rt and max_rt > 0
        },
    }
    return {"thresholds": thresholds, "counts": dict(counts), "testcases": classified}

def classify_binary_exceeded(testcases: list, max_ref_times: dict,
                              eval_tiers: Optional[set] = None,
                              structure: Optional[dict] = None,
                              constraints: Optional[list] = None,
                              stdin_lookup=None,
                              constraints_boundary: Optional[list] = None) -> dict:
    counts = {"exceeded": 0, "not_exceeded": 0, "invalid": 0}
    classified = []

    _have_validator = (
        _tc_parse_stdin is not None and _tc_validate_constraints is not None
        and structure and stdin_lookup is not None
    )
    _can_check_all = bool(_have_validator and constraints)
    _can_check_boundary = bool(_have_validator and (constraints_boundary or constraints))
    _compliance_cache: dict = {}

    def _get_compliance(tc_id):
        if (not _can_check_all and not _can_check_boundary) or tc_id is None:
            return (None, []), (None, [])
        if tc_id in _compliance_cache:
            return _compliance_cache[tc_id]
        try:
            stdin_text = stdin_lookup(tc_id)
        except Exception:
            stdin_text = None
        if not stdin_text:
            _compliance_cache[tc_id] = ((None, []), (None, []))
            return (None, []), (None, [])
        try:
            parsed = _tc_parse_stdin(stdin_text, structure)
        except Exception:
            parsed = None
        if parsed is None:
            _compliance_cache[tc_id] = ((None, []), (None, []))
            return (None, []), (None, [])
        if _can_check_all:
            try:
                r = _tc_validate_constraints(parsed, constraints)
                ok_a = bool(r.get("compliant"))
                v_a = list(r.get("violations") or [])
            except Exception:
                ok_a, v_a = None, []
        else:
            ok_a, v_a = None, []
        if _can_check_boundary:
            if constraints_boundary:
                try:
                    r = _tc_validate_constraints(parsed, constraints_boundary)
                    ok_b = bool(r.get("compliant"))
                    v_b = list(r.get("violations") or [])
                except Exception:
                    ok_b, v_b = None, []
            else:
                ok_b, v_b = True, []
        else:
            ok_b, v_b = None, []
        _compliance_cache[tc_id] = ((ok_a, v_a), (ok_b, v_b))
        return (ok_a, v_a), (ok_b, v_b)

    for tc in testcases:
        if eval_tiers is not None:
            tt = tc.get("target_tier")
            if tt not in eval_tiers:
                continue
        _tc_id = tc.get("testcase")
        (_compliant, _violations), (_compliant_b, _violations_b) = _get_compliance(_tc_id)
        if tc.get("tc_verdict") in ("wrong-answer", "timelimit"):
            counts["invalid"] += 1
            classified.append({
                "testcase": _tc_id,
                "language": None,
                "run_time": None,
                "exceeded": None,
                "tc_verdict": "wrong-answer",
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
                "constraint_compliant": _compliant,
                "constraint_violations": _violations,
                "constraint_compliant_boundary": _compliant_b,
                "constraint_violations_boundary": _violations_b,
            })
            continue
        pl = tc.get("per_language", {})
        if not pl:
            counts["invalid"] += 1
            classified.append({
                "testcase": _tc_id,
                "language": None,
                "run_time": None,
                "max_ref_time": None,
                "exceeded": None,
                "tc_verdict": tc.get("tc_verdict"),
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
                "constraint_compliant": _compliant,
                "constraint_violations": _violations,
                "constraint_compliant_boundary": _compliant_b,
                "constraint_violations_boundary": _violations_b,
            })
            continue
        for lang, data in pl.items():
            run_time = data.get("run_time")
            max_rt = max_ref_times.get(lang)
            if run_time is None:
                counts["invalid"] += 1
                classified.append({
                    "testcase": _tc_id,
                    "language": lang,
                    "run_time": None,
                    "max_ref_time": max_rt,
                    "exceeded": None,
                    "tc_verdict": tc.get("tc_verdict"),
                    "target_tier": tc.get("target_tier"),
                    "module": tc.get("module"),
                    "constraint_compliant": _compliant,
                    "constraint_violations": _violations,
                    "constraint_compliant_boundary": _compliant_b,
                    "constraint_violations_boundary": _violations_b,
                })
                continue
            if max_rt is None or max_rt <= 0:
                exceeded = False
            else:
                exceeded = run_time > max_rt
            key = "exceeded" if exceeded else "not_exceeded"
            counts[key] += 1
            classified.append({
                "testcase": _tc_id,
                "language": lang,
                "run_time": run_time,
                "max_ref_time": max_rt,
                "exceeded": exceeded,
                "tc_verdict": tc.get("tc_verdict"),
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
                "constraint_compliant": _compliant,
                "constraint_violations": _violations,
                "constraint_compliant_boundary": _compliant_b,
                "constraint_violations_boundary": _violations_b,
            })

    thresholds = {
        "method": "binary_reference_exceeded",
        "languages": {
            lang: f"exceeded if run_time > {max_rt:.4f}s"
            for lang, max_rt in max_ref_times.items() if max_rt and max_rt > 0
        },
    }
    return {"thresholds": thresholds, "counts": counts, "testcases": classified}

def classify_tle_compliant(testcases: list, structure: Optional[dict],
                            constraints: Optional[list],
                            stdin_lookup,
                            eval_tiers: Optional[set] = None) -> dict:
    counts = {"tle_compliant": 0, "tle_violating": 0}
    classified: list = []
    if _tc_parse_stdin is None:
        return {"counts": counts, "testcases": classified}
    for tc in testcases:
        if eval_tiers is not None:
            if tc.get("target_tier") not in eval_tiers:
                continue
        per_lang = tc.get("per_language", {}) or {}
        is_tle = (tc.get("tc_verdict") == "timelimit"
                  or any((d or {}).get("verdict") == "timelimit"
                         for d in per_lang.values()))
        if not is_tle:
            continue
        tc_id = tc.get("testcase")
        stdin_text = None
        try:
            stdin_text = stdin_lookup(tc_id) if stdin_lookup else None
        except Exception:
            stdin_text = None
        if not stdin_text or not structure or not constraints:
            counts["tle_violating"] += 1
            classified.append({
                "testcase": tc_id,
                "compliant": False,
                "violations": ["missing stdin/structure/constraints"],
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
                "verdict": "timelimit",
            })
            continue
        parsed = _tc_parse_stdin(stdin_text, structure)
        if parsed is None:
            counts["tle_violating"] += 1
            classified.append({
                "testcase": tc_id,
                "compliant": False,
                "violations": ["stdin parse failure"],
                "target_tier": tc.get("target_tier"),
                "module": tc.get("module"),
                "verdict": "timelimit",
            })
            continue
        result = _tc_validate_constraints(parsed, constraints)
        if result.get("compliant"):
            counts["tle_compliant"] += 1
        else:
            counts["tle_violating"] += 1
        classified.append({
            "testcase": tc_id,
            "compliant": bool(result.get("compliant")),
            "violations": result.get("violations", []),
            "target_tier": tc.get("target_tier"),
            "module": tc.get("module"),
            "verdict": "timelimit",
        })
    return {"counts": counts, "testcases": classified}

def _load_llm_constraints_jsonl(path: str) -> dict:
    if not path:
        return {}
    try:
        out: dict = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                idx = rec.get("index")
                if idx is None:
                    continue
                out[int(idx)] = rec.get("constraints") or []
        log.info("Loaded LLM constraints: %d problems from %s", len(out), path)
        return out
    except FileNotFoundError:
        log.warning("LLM constraints file not found: %s", path)
        return {}
    except Exception as e:
        log.warning("Failed to load LLM constraints (%s): %s", path, e)
        return {}

def _load_structures_by_idx(parsed_structures_dir: str, split: str) -> dict:
    if not parsed_structures_dir:
        return {}
    out: dict = {}
    sub_splits = (split,) if split != "all" else ("train", "test", "valid")
    for sub in sub_splits:
        p = Path(parsed_structures_dir) / f"{sub}.json"
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                try:
                    idx = int(k)
                except (ValueError, TypeError):
                    continue
                out[idx] = (v or {}).get("structure") or {}
        except Exception as e:
            log.warning("Failed to load parsed_structures (%s): %s", p, e)
    return out

def _build_stdin_ordinal_map(inputs_dir: Path,
                              compact_m1_features: dict) -> dict:
    from collections import defaultdict
    name_files: dict = defaultdict(lambda: {"fast": [], "medium": [], "slow": []})
    for tier in ("fast", "medium", "slow"):
        tier_dir = inputs_dir / tier
        if not tier_dir.exists():
            continue
        for fpath in sorted(tier_dir.iterdir()):
            if fpath.suffix != ".json":
                continue
            try:
                with open(fpath, encoding="utf-8") as f:
                    tc = json.load(f)
            except Exception:
                continue
            name = tc.get("name", "")
            if not name:
                continue
            name_files[name][tier].append((fpath, tc))
    out: dict = {}
    for name, tier_buckets in name_files.items():
        ord_map: dict = {}
        ordinal = 1
        for tier in ("fast", "medium", "slow"):
            entries = sorted(tier_buckets[tier], key=lambda p: p[0].name)
            for fpath, tc in entries:
                stdin_val = _expand_compact_m1_stdin(tc, compact_m1_features,
                                                     tc.get("index"))
                ord_map[ordinal] = stdin_val or ""
                ordinal += 1
        if ord_map:
            out[name] = ord_map
    return out

def extract_problem_timing(prob: dict) -> Optional[dict]:
    timelimit = prob.get("timelimit", 0)
    if timelimit <= 0:
        return None

    tc_lang_times = defaultdict(lambda: defaultdict(list))
    tc_lang_tle: dict = defaultdict(lambda: defaultdict(list))
    tc_invalid = set()
    tc_all_ids = set()
    total_solutions = 0

    _TIMING_VALID_VERDICTS = {"correct", "AC"}
    _INVALID_VERDICTS = {"wrong-answer", "WA", "run-error", "timelimit",
                         "output-limit", "memory-limit"}

    for sol in prob.get("solutions", []):
        total_solutions += 1
        lang = sol.get("language_id") or sol.get("language", "") or "unknown"
        for run in sol.get("runs", []):
            tc_id = run.get("testcase")
            if tc_id is None:
                continue
            tc_all_ids.add(tc_id)
            v = run.get("verdict", "")
            if v in _TIMING_VALID_VERDICTS and run.get("run_time") is not None:
                tc_lang_times[tc_id][lang].append(run["run_time"])
            elif v == "timelimit":
                rt = run.get("run_time")
                if rt is not None:
                    tc_lang_tle[tc_id][lang].append(rt)
                tc_invalid.add(tc_id)
            elif v in _INVALID_VERDICTS or not v:
                tc_invalid.add(tc_id)

    all_tc_ids = sorted(tc_all_ids)
    if not all_tc_ids:
        return None

    testcases = []
    valid_count = 0
    invalid_count = 0

    for tc_id in all_tc_ids:
        per_lang = {}
        if tc_lang_times[tc_id]:
            for lang, times in tc_lang_times[tc_id].items():
                if times:
                    rt = min(times)
                    per_lang[lang] = {"run_time": round(rt, 6), "ratio": round(rt / timelimit, 6),
                                      "verdict": "correct"}
            valid_count += 1
            testcases.append({
                "testcase": tc_id,
                "per_language": per_lang,
                "tc_verdict": "correct",
            })
        elif tc_lang_tle[tc_id]:
            for lang, times in tc_lang_tle[tc_id].items():
                if times:
                    rt = min(times)
                    per_lang[lang] = {"run_time": round(rt, 6),
                                      "ratio": round(rt / timelimit, 6),
                                      "verdict": "timelimit"}
            testcases.append({
                "testcase": tc_id,
                "per_language": per_lang,
                "tc_verdict": "timelimit",
            })
            invalid_count += 1
        else:
            testcases.append({
                "testcase": tc_id,
                "per_language": {},
                "tc_verdict": "wrong-answer",
            })
            invalid_count += 1

    valid_tcs = [tc for tc in testcases if tc["tc_verdict"] == "correct"]
    ratios = []
    for tc in valid_tcs:
        for lang, data in tc.get("per_language", {}).items():
            ratios.append(data["ratio"])
    ratio_stats = {}
    if ratios:
        if HAS_NUMPY:
            ratio_stats = {
                "min": round(float(np.min(ratios)), 6),
                "max": round(float(np.max(ratios)), 6),
                "mean": round(float(np.mean(ratios)), 6),
                "median": round(float(np.median(ratios)), 6),
                "std": round(float(np.std(ratios)), 6),
            }
        else:
            s = sorted(ratios)
            ratio_stats = {
                "min": round(s[0], 6),
                "max": round(s[-1], 6),
                "mean": round(sum(s) / len(s), 6),
                "median": round(s[len(s) // 2], 6),
            }
    ac_count = sum(1 for sol in prob.get("solutions", []) if sol.get("verdict") == "AC")

    return {
        "index": prob.get("index"),
        "name": prob.get("name"),
        "timelimit": timelimit,
        "ac_solutions": ac_count,
        "total_solutions": total_solutions,
        "num_testcases": len(testcases),
        "num_valid_testcases": valid_count,
        "num_wa_testcases": invalid_count,
        "tc_accuracy": round(valid_count / len(testcases), 4) if testcases else 0.0,
        "ratio_stats": ratio_stats,
        "testcases": testcases,
    }

def classify_timelimit(testcases: list, timelimit: float) -> dict:
    t1, t2 = 1 / 3, 2 / 3
    counts = defaultdict(int)
    classified = []

    for tc in testcases:
        if tc.get("tc_verdict") in ("wrong-answer", "timelimit"):
            counts["invalid"] += 1
            classified.append({
                "testcase": tc.get("testcase"),
                "language": None,
                "ratio": None,
                "difficulty": "invalid",
                "tc_verdict": "wrong-answer",
                "target_tier": tc.get("target_tier"),
            })
            continue
        pl = tc.get("per_language", {})
        for lang, data in pl.items():
            r = data.get("ratio", 0.0)
            if r <= t1:
                diff = "fast"
            elif r <= t2:
                diff = "medium"
            else:
                diff = "slow"
            counts[diff] += 1
            classified.append({
                "testcase": tc.get("testcase"),
                "language": lang,
                "ratio": r,
                "difficulty": diff,
                "tc_verdict": tc.get("tc_verdict"),
                "target_tier": tc.get("target_tier"),
                "run_time": data.get("run_time"),
            })

    return {
        "thresholds": {
            "fast": f"0 ~ {timelimit / 3:.4f}s (ratio 0 ~ 0.3333)",
            "medium": f"{timelimit / 3:.4f} ~ {timelimit * 2 / 3:.4f}s (ratio 0.3333 ~ 0.6667)",
            "slow": f"{timelimit * 2 / 3:.4f} ~ {timelimit:.4f}s (ratio 0.6667 ~ 1.0, timelimit={timelimit}s)",
            "invalid": "non-correct TCs (WA / run-error / TLE / OLE / MLE / empty verdict)",
        },
        "counts": dict(counts),
        "testcases": classified,
    }

def classify_tercile(testcases: list, timelimit: float) -> dict:
    ratios = []
    for tc in testcases:
        if tc.get("tc_verdict") in ("wrong-answer", "timelimit"):
            continue
        for data in tc.get("per_language", {}).values():
            ratios.append(data.get("ratio", 0.0))
    if not ratios:
        counts = defaultdict(int)
        classified = []
        for tc in testcases:
            if tc.get("tc_verdict") in ("wrong-answer", "timelimit"):
                counts["invalid"] += 1
                classified.append({
                    "testcase": tc.get("testcase"),
                    "language": None,
                    "ratio": None,
                    "difficulty": "invalid",
                    "tc_verdict": "wrong-answer",
                    "target_tier": tc.get("target_tier"),
                })
        return {"thresholds": {}, "percentiles": {}, "counts": dict(counts), "testcases": classified}

    if HAS_NUMPY:
        t1 = float(np.percentile(ratios, 33.33))
        t2 = float(np.percentile(ratios, 66.67))
    else:
        s = sorted(ratios)
        t1 = s[int(len(s) * 0.3333)]
        t2 = s[int(len(s) * 0.6667)]

    counts = defaultdict(int)
    classified = []
    for tc in testcases:
        if tc.get("tc_verdict") in ("wrong-answer", "timelimit"):
            counts["invalid"] += 1
            classified.append({
                "testcase": tc.get("testcase"),
                "language": None,
                "ratio": None,
                "difficulty": "invalid",
                "tc_verdict": "wrong-answer",
                "target_tier": tc.get("target_tier"),
            })
            continue
        pl = tc.get("per_language", {})
        for lang, data in pl.items():
            r = data.get("ratio", 0.0)
            if r <= t1:
                diff = "fast"
            elif r <= t2:
                diff = "medium"
            else:
                diff = "slow"
            counts[diff] += 1
            classified.append({
                "testcase": tc.get("testcase"),
                "language": lang,
                "ratio": r,
                "difficulty": diff,
                "tc_verdict": tc.get("tc_verdict"),
                "target_tier": tc.get("target_tier"),
                "run_time": data.get("run_time"),
            })

    thresholds = {
        "fast": f"ratio <= {t1:.6f} (time <= {t1 * timelimit:.6f}s)",
        "slow": f"ratio > {t2:.6f} (time > {t2 * timelimit:.6f}s, up to timelimit={timelimit}s)",
        "invalid": "truly invalid TCs (run-error / timelimit / output-limit / memory-limit / empty verdict)",
    }
    if abs(t1 - t2) < 1e-9:
        thresholds["medium"] = f"(empty: p33 == p67 == {t1:.6f})"
    else:
        thresholds["medium"] = f"{t1:.6f} < ratio <= {t2:.6f} ({t1 * timelimit:.6f} ~ {t2 * timelimit:.6f}s)"

    return {
        "thresholds": thresholds,
        "percentiles": {"p33_ratio": round(t1, 6), "p67_ratio": round(t2, 6)},
        "counts": dict(counts),
        "testcases": classified,
    }

def calculate_tpr(testcases: list, target_tier: str = "slow") -> float:
    target_tcs = [tc for tc in testcases if tc.get("target_tier") == target_tier]
    if not target_tcs:
        return 0.0

    success_count = sum(
        1 for tc in target_tcs
        if tc.get("difficulty") == target_tier
    )
    return round(success_count / len(target_tcs), 4)

def _tpr_normalized_for_problem(
    ev_tcs: list,
    tcs_per_tier: int,
    target_tier: str = "slow",
    available_langs: set = None,
) -> float:
    lang_hits: dict = {}
    for tc in ev_tcs:
        if tc.get("target_tier") != target_tier:
            continue
        lang = tc.get("language")
        if not lang:
            continue
        if tc.get("difficulty") == target_tier:
            lang_hits[lang] = lang_hits.get(lang, 0) + 1

    if available_langs is not None:
        applicable = available_langs
    else:
        applicable = set(lang_hits.keys())

    if not applicable:
        return 0.0
    return sum(lang_hits.get(l, 0) / tcs_per_tier for l in applicable) / len(applicable)

def _tpr_norm_counts(
    ev_tcs: list,
    tcs_per_tier: int,
    target_tier: str = "slow",
    available_langs: set = None,
) -> tuple:
    lang_hits: dict = {}
    for tc in ev_tcs:
        if tc.get("target_tier") != target_tier:
            continue
        lang = tc.get("language")
        if not lang:
            continue
        if tc.get("difficulty") == target_tier:
            lang_hits[lang] = lang_hits.get(lang, 0) + 1

    if available_langs is not None:
        applicable = available_langs
    else:
        applicable = set(lang_hits.keys())

    if not applicable:
        return 0, 0
    hit_sum = sum(lang_hits.get(l, 0) for l in applicable)
    denom_sum = tcs_per_tier * len(applicable)
    return hit_sum, denom_sum

def calculate_taa(testcases: list) -> float:
    if not testcases:
        return 0.0

    correct_hits = sum(
        1 for tc in testcases
        if tc.get("target_tier") == tc.get("difficulty")
    )
    return round(correct_hits / len(testcases), 4)

def calculate_ats(opt_testcases: list, sub_testcases: list) -> float:
    slow_keys = []
    for tc in opt_testcases:
        if tc.get("difficulty") != "slow":
            continue
        idx = tc.get("testcase")
        lang = tc.get("language")
        if lang is not None:
            slow_keys.append((idx, lang))
        else:
            slow_keys.append((idx, None))
    if not slow_keys:
        return 0.0

    sub_map = {}
    for tc in sub_testcases:
        k = (tc.get("testcase"), tc.get("language"))
        sub_map[k] = tc

    total_score = 0.0
    for key in slow_keys:
        sub_tc = sub_map.get(key)

        if sub_tc is None or sub_tc.get("verdict") == "TLE":
            total_score += 1.0
        elif sub_tc.get("difficulty") == "invalid":
            total_score += 0.0
        elif sub_tc.get("difficulty") == "slow":
            total_score += 0.7
        else:
            total_score += 0.1

    return round(total_score / len(slow_keys), 4)

def _counts_per_unique_tc(testcases: list) -> dict:
    tc_worst: dict = {}
    rank = {"invalid": 3, "slow": 2, "medium": 1, "fast": 0}

    for row in testcases:
        tc_id = row.get("testcase")
        if tc_id is None:
            continue
        d = row.get("difficulty") or "fast"
        r = rank.get(d, 0)
        if tc_id not in tc_worst or r > rank.get(tc_worst[tc_id], 0):
            tc_worst[tc_id] = d

    counts = defaultdict(int)
    for d in tc_worst.values():
        counts[d] += 1
    return dict(counts)

def compare_problem(
    reference_timing: Optional[dict],
    evaluand_timing: Optional[dict],
    prob_info: dict,
) -> dict:
    result = {
        "index": prob_info.get("index"),
        "name": prob_info.get("name"),
        "timelimit": prob_info.get("timelimit", 0),
    }

    for source_key, timing in [("reference", reference_timing), ("evaluand", evaluand_timing)]:
        if timing is None:
            result[source_key] = {
                "status": "no_data",
                "ac_solutions": 0,
                "num_testcases": 0,
            }
            continue

        tl = timing.get("timelimit", 0)
        tcs = timing.get("testcases", [])

        timelimit_cls = classify_timelimit(tcs, tl)
        tercile_cls = classify_tercile(tcs, tl)

        counts_per_tc = _counts_per_unique_tc(timelimit_cls["testcases"])
        result[source_key] = {
            "status": "ok",
            "ac_solutions": timing["ac_solutions"],
            "total_solutions": timing.get("total_solutions", timing["ac_solutions"]),
            "num_testcases": timing["num_testcases"],
            "num_valid_testcases": timing.get("num_valid_testcases", timing["num_testcases"]),
            "num_wa_testcases": timing.get("num_wa_testcases", 0),
            "tc_accuracy": timing.get("tc_accuracy", 1.0),
            "ratio_stats": timing["ratio_stats"],
            "language_solutions": timing.get("language_solutions", {}),
            "timelimit_based": {
                "thresholds": timelimit_cls["thresholds"],
                "counts": timelimit_cls["counts"],
                "counts_per_tc": counts_per_tc,
            },
            "tercile_based": {
                "thresholds": tercile_cls["thresholds"],
                "counts": tercile_cls["counts"],
                "percentiles": tercile_cls.get("percentiles", {}),
                "testcases": tercile_cls["testcases"],
            },
            "testcases": timelimit_cls["testcases"],
        }

    if result.get("reference", {}).get("status") == "ok" and result.get("evaluand", {}).get("status") == "ok":
        result["shift_analysis"] = analyze_difficulty_shift(
            result["reference"]["timelimit_based"]["counts_per_tc"],
            result["evaluand"]["timelimit_based"]["counts_per_tc"],
        )

    return result

def analyze_difficulty_shift(reference_counts: dict, evaluand_counts: dict) -> dict:
    reference_total = sum(reference_counts.get(d, 0) for d in DIFFICULTY_ORDER)
    evaluand_total = sum(evaluand_counts.get(d, 0) for d in DIFFICULTY_ORDER)

    if reference_total == 0 or evaluand_total == 0:
        return {"status": "insufficient_data"}

    reference_pct = {d: reference_counts.get(d, 0) / reference_total * 100 for d in DIFFICULTY_ORDER}
    evaluand_pct = {d: evaluand_counts.get(d, 0) / evaluand_total * 100 for d in DIFFICULTY_ORDER}

    shifts = {}
    for d in DIFFICULTY_ORDER:
        shifts[d] = {
            "reference_count": reference_counts.get(d, 0),
            "reference_pct": round(reference_pct[d], 2),
            "evaluand_count": evaluand_counts.get(d, 0),
            "evaluand_pct": round(evaluand_pct[d], 2),
            "delta_pct": round(evaluand_pct[d] - reference_pct[d], 2),
        }

    slower = evaluand_pct["slow"] > reference_pct["slow"]
    faster = evaluand_pct["fast"] > reference_pct["fast"]

    if slower and not faster:
        verdict = "SLOWER_HEAVIER"
    elif faster and not slower:
        verdict = "FASTER_HEAVIER"
    elif slower and faster:
        verdict = "MORE_POLARIZED"
    else:
        verdict = "SIMILAR"

    return {
        "status": "ok",
        "shifts": shifts,
        "verdict": verdict,
        "slow_increase_pct": round(evaluand_pct["slow"] - reference_pct["slow"], 2),
    }

def _build_target_breakdown_per_measured(testcases: list) -> dict:
    rank = {"invalid": 3, "slow": 2, "medium": 1, "fast": 0}
    tc_worst: dict = {}
    for row in testcases:
        tc_id = row.get("testcase")
        if tc_id is None:
            continue
        d = row.get("difficulty") or "fast"
        tgt = row.get("target_tier") or ""
        r = rank.get(d, 0)
        if tc_id not in tc_worst or r > rank.get(tc_worst[tc_id][0], 0):
            tc_worst[tc_id] = (d, tgt)

    breakdown: dict = {}
    for meas, tgt in tc_worst.values():
        if meas not in breakdown:
            breakdown[meas] = {}
        key = tgt if tgt in ("fast", "medium", "slow") else "_"
        breakdown[meas][key] = breakdown[meas].get(key, 0) + 1
    return breakdown

def plot_comparison_bar(
    problems: list,
    method: str,
    output_path: Path,
    title_suffix: str = "",
):
    if not HAS_MPL:
        log.warning("matplotlib not available, skipping plot")
        return

    valid = [p for p in problems if p.get("reference", {}).get("status") == "ok"
             and p.get("evaluand", {}).get("status") == "ok"]
    if not valid:
        return

    method_key = "timelimit_based" if method == "timelimit" else "tercile_based"
    n = len(valid)

    def _get_counts(p: dict, src: str):
        blk = p[src][method_key]
        if method == "timelimit" and "counts_per_tc" in blk:
            return blk["counts_per_tc"]
        return blk["counts"]

    ev_target_breakdown = [
        _build_target_breakdown_per_measured(p.get("evaluand", {}).get("testcases", []))
        for p in valid
    ]

    if n < 30:
        ann_fs, ann_fmt = 6, "multi"
    elif n < 80:
        ann_fs, ann_fmt = 5, "multi"
    else:
        ann_fs, ann_fmt = 4, "single"

    fig_w = max(14, n * 1.2)
    fig, axes = plt.subplots(1, 2, figsize=(fig_w, 6), sharey=True)
    fig.suptitle(f"Difficulty Distribution Comparison ({method}){title_suffix}", fontsize=14, fontweight="bold")

    for ax_idx, (source, label) in enumerate([("reference", "Reference TC"), ("evaluand", "Evaluand TC")]):
        ax = axes[ax_idx]
        names = []
        bottoms = [0.0] * n

        for d_idx, diff in enumerate(DIFFICULTY_ORDER):
            counts = []
            for p in valid:
                cts = _get_counts(p, source)
                total = sum(cts.get(dd, 0) for dd in DIFFICULTY_ORDER)
                c = cts.get(diff, 0)
                pct = (c / total * 100) if total > 0 else 0.0
                counts.append(pct)
                if d_idx == 0:
                    names.append((p.get("name") or "")[:25])

            ax.bar(range(n), counts, bottom=bottoms,
                   color=COLORS[diff], label=diff if ax_idx == 0 else "",
                   edgecolor="white", linewidth=0.5)

            if source == "evaluand":
                min_pct_for_ann = 8.0
                for p_idx, pct in enumerate(counts):
                    if pct < min_pct_for_ann:
                        continue
                    seg_mid = bottoms[p_idx] + pct / 2
                    bd = ev_target_breakdown[p_idx].get(diff, {})
                    f_c = bd.get("fast", 0)
                    m_c = bd.get("medium", 0)
                    s_c = bd.get("slow", 0)
                    total_ann = f_c + m_c + s_c
                    if total_ann == 0:
                        continue
                    if ann_fmt == "multi":
                        lbl = f"f:{f_c}\nm:{m_c}\ns:{s_c}"
                    else:
                        lbl = f"f:{f_c}/m:{m_c}/s:{s_c}"
                    ax.text(p_idx, seg_mid, lbl,
                            ha="center", va="center",
                            fontsize=ann_fs, color="white", fontweight="bold",
                            clip_on=True)

            bottoms = [b + c for b, c in zip(bottoms, counts)]

        ax.set_title(label, fontsize=12)
        ax.set_xticks(range(n))
        ax.set_xticklabels(names, rotation=45, ha="right", fontsize=7)
        ax.set_ylabel("%" if ax_idx == 0 else "")
        ax.set_ylim(0, 105)

    axes[0].legend(loc="upper left")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    log.info("Saved comparison plot: %s", output_path)

def plot_tc_tier_overview(
    problems: list,
    output_path: Path,
    title_suffix: str = "",
    tcs_per_tier: int = 5,
):
    if not HAS_MPL:
        return

    from matplotlib.patches import Patch

    if not problems:
        return

    ZONE_GAP  = 1
    ZH        = tcs_per_tier
    TIER_ORDER = ["fast", "medium", "slow"]
    COLORS     = {"fast": "#2ecc71", "medium": "#f39c12", "slow": "#e74c3c"}
    LIGHTS     = {"fast": "#d5f5e3",  "medium": "#fdebd0", "slow": "#fadbd8"}
    ZONE_BOT   = {
        "fast":   0,
        "medium": ZH + ZONE_GAP,
        "slow":   2 * (ZH + ZONE_GAP),
    }
    TOTAL_H = 3 * ZH + 2 * ZONE_GAP

    n = len(problems)
    names = []
    prob_valid:   dict = {"fast": [], "medium": [], "slow": []}
    prob_invalid: dict = {"fast": [], "medium": [], "slow": []}

    rank = {"invalid": 3, "slow": 2, "medium": 1, "fast": 0}
    for p in problems:
        names.append((p.get("name") or "")[:20])
        ev_tcs = p.get("evaluand", {}).get("testcases", [])

        tc_worst: dict = {}
        for row in ev_tcs:
            tc_id = row.get("testcase")
            if tc_id is None:
                continue
            d   = row.get("difficulty") or "fast"
            tgt = row.get("target_tier") or ""
            r   = rank.get(d, 0)
            if tc_id not in tc_worst or r > rank.get(tc_worst[tc_id][0], 0):
                tc_worst[tc_id] = (d, tgt)

        vc = {"fast": 0, "medium": 0, "slow": 0}
        ic = {"fast": 0, "medium": 0, "slow": 0}
        for meas, tgt in tc_worst.values():
            if tgt not in TIER_ORDER:
                continue
            if meas == "invalid":
                ic[tgt] += 1
            else:
                vc[tgt] += 1

        for t in TIER_ORDER:
            prob_valid[t].append(vc[t])
            prob_invalid[t].append(ic[t])

    fig_w  = max(18, n * 0.15)
    ann_fs = max(3, min(6, int(90 / n)))

    fig, ax = plt.subplots(figsize=(fig_w, 7))

    for tier in TIER_ORDER:
        bot   = ZONE_BOT[tier]
        col   = COLORS[tier]
        light = LIGHTS[tier]

        for p_idx in range(n):
            valid   = min(prob_valid[tier][p_idx],   ZH)
            invalid = min(prob_invalid[tier][p_idx], ZH - valid)

            if valid > 0:
                ax.bar(p_idx, valid, bottom=bot, color=col,
                       edgecolor="white", linewidth=0.3, zorder=2)
                ax.text(p_idx, bot + valid / 2, str(valid),
                        ha="center", va="center",
                        fontsize=ann_fs, color="white", fontweight="bold",
                        clip_on=True, zorder=3)

            if invalid > 0:
                ax.bar(p_idx, invalid, bottom=bot + valid, color=col,
                       edgecolor="white", linewidth=0.3,
                       alpha=0.45, hatch="//", zorder=2)

    for tier in ["medium", "slow"]:
        ax.axhline(ZONE_BOT[tier] - ZONE_GAP / 2,
                   color="#7f8c8d", linewidth=0.7, linestyle="--", zorder=0)

    ax.set_yticks([ZONE_BOT[t] + ZH / 2 for t in TIER_ORDER])
    ax.set_yticklabels([
        f"Fast\n(0–{ZH})",
        f"Medium\n({ZH + ZONE_GAP}–{2*ZH + ZONE_GAP})",
        f"Slow\n({2*(ZH + ZONE_GAP)}–{3*ZH + 2*ZONE_GAP})",
    ], fontsize=9)
    ax.set_ylim(-0.5, TOTAL_H + 0.5)

    ax.set_xlim(-0.5, n - 0.5)
    ax.set_xticks(range(n))
    tick_fs = max(3, min(6, int(120 / n)))
    ax.set_xticklabels(names, rotation=90, ha="right", fontsize=tick_fs)
    ax.set_xlabel("Problem")
    ax.set_title(f"Per-Problem Valid TC Count by Target Tier{title_suffix} ({n})",
                 fontsize=13, fontweight="bold")

    legend_elements = [
        Patch(facecolor=COLORS["fast"],   label=f"Valid fast   (0–{ZH})"),
        Patch(facecolor=COLORS["medium"], label=f"Valid medium ({ZH+ZONE_GAP}–{2*ZH+ZONE_GAP})"),
        Patch(facecolor=COLORS["slow"],   label=f"Valid slow   ({2*(ZH+ZONE_GAP)}–{3*ZH+2*ZONE_GAP})"),
        Patch(facecolor="#aab7b8", hatch="//", label="Invalid (WA/RE/TLE/OLE/MLE)"),
    ]
    ax.legend(handles=legend_elements, loc="upper right", fontsize=8)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    log.info("Saved TC tier overview plot: %s", output_path)

def plot_problem_comparison(
    prob: dict,
    method: str,
    output_path: Path,
):
    if not HAS_MPL:
        return

    method_key = "timelimit_based" if method == "timelimit" else "tercile_based"
    ref_ok = prob.get("reference", {}).get("status") == "ok"
    ev_ok = prob.get("evaluand", {}).get("status") == "ok"
    if not ref_ok and not ev_ok:
        return

    def _cts(p: dict, src: str):
        blk = p[src][method_key]
        if method == "timelimit" and "counts_per_tc" in blk:
            return blk["counts_per_tc"]
        return blk["counts"]

    fig, ax = plt.subplots(figsize=(6, 4))

    x = range(len(DIFFICULTY_ORDER))
    width = 0.35

    if ref_ok:
        ref_counts = [_cts(prob, "reference").get(d, 0) for d in DIFFICULTY_ORDER]
        ax.bar([i - width / 2 for i in x], ref_counts, width, label="Reference TC",
               color=["#3498db"] * 3, alpha=0.8)

    if ev_ok:
        ev_counts = [_cts(prob, "evaluand").get(d, 0) for d in DIFFICULTY_ORDER]
        ax.bar([i + width / 2 for i in x], ev_counts, width, label="Evaluand TC",
               color=["#e74c3c"] * 3, alpha=0.8)

    ax.set_xticks(x)
    ax.set_xticklabels(DIFFICULTY_ORDER)
    ax.set_ylabel("Test Case Count")
    ax.set_title(f"{prob['name'][:40]} ({method})", fontsize=10)
    ax.legend()

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()

def _tpr_counts(tcs: list, target_tier: str) -> tuple:
    target = [tc for tc in tcs if tc.get("target_tier") == target_tier]
    total = len(target)
    hit = sum(1 for tc in target if tc.get("difficulty") == target_tier)
    return hit, total

def _taa_counts(tcs: list) -> tuple:
    with_tier = [tc for tc in tcs if tc.get("target_tier") and tc.get("difficulty")]
    total = len(with_tier)
    hit = sum(1 for tc in with_tier if tc.get("target_tier") == tc.get("difficulty"))
    return hit, total

def _tpr_counts_valid(tcs: list, target_tier: str) -> tuple:
    target = [tc for tc in tcs
              if tc.get("target_tier") == target_tier and tc.get("difficulty") != "invalid"]
    total = len(target)
    hit = sum(1 for tc in target if tc.get("difficulty") == target_tier)
    return hit, total

def _taa_counts_valid(tcs: list) -> tuple:
    with_tier = [tc for tc in tcs
                 if tc.get("target_tier") and tc.get("difficulty")
                 and tc.get("difficulty") != "invalid"]
    total = len(with_tier)
    hit = sum(1 for tc in with_tier if tc.get("target_tier") == tc.get("difficulty"))
    return hit, total

def _fmt_metric(hit: int, total: int) -> str:
    ratio = hit / total if total > 0 else 0.0
    return f"{ratio:.4f}  ({hit}/{total})"

def _write_tpr_taa_block(f, tcs: list, label: str, num_ref_problems: int):
    for tier in ("fast", "medium", "slow"):
        hit, total = _tpr_counts(tcs, tier)
        pad = " " * (6 - len(tier))
        f.write(f"  TPR({tier}){pad} = {_fmt_metric(hit, total)}  "
                f"(intended {tier} -> actually {tier}, scope=both_ok)\n")

def _write_module_split_block(f, tcs: list, method_label: str):
    tcs_m1 = [tc for tc in tcs if tc.get("module") == "M1"]
    tcs_m2 = [tc for tc in tcs if tc.get("module") == "M2"]
    if not tcs_m1 and not tcs_m2:
        return

    f.write(f"\n  --- Per-Module Breakdown ({method_label}) ---\n")
    f.write(f"  {'':30} {'All':>20} {'M1(boundary)':>20} {'M2(algorithmic)':>20}\n")
    f.write(f"  {'-' * 90}\n")

    all_sets = [("All", tcs), ("M1(boundary)", tcs_m1), ("M2(algorithmic)", tcs_m2)]

    for tier in ("fast", "medium", "slow"):
        cells = []
        for _label, subset in all_sets:
            hit, total = _tpr_counts(subset, tier)
            ratio = hit / total if total > 0 else 0.0
            cells.append(f"{ratio:.4f} ({hit}/{total})")
        pad = " " * (6 - len(tier))
        f.write(f"  TPR({tier}){pad}                   {cells[0]:>20} {cells[1]:>20} {cells[2]:>20}\n")

    f.write(f"  {'-' * 90}\n")
    f.write(f"  TC count (with target_tier)       {len(tcs):>20} {len(tcs_m1):>20} {len(tcs_m2):>20}\n")

def _write_module_split_block_valid(f, tcs: list, method_label: str):
    tcs_m1 = [tc for tc in tcs if tc.get("module") == "M1"]
    tcs_m2 = [tc for tc in tcs if tc.get("module") == "M2"]
    if not tcs_m1 and not tcs_m2:
        return

    def _valid(subset):
        return [tc for tc in subset if tc.get("difficulty") != "invalid"]

    valid_all, valid_m1, valid_m2 = _valid(tcs), _valid(tcs_m1), _valid(tcs_m2)
    invalid_all = len(tcs) - len(valid_all)
    invalid_m1 = len(tcs_m1) - len(valid_m1)
    invalid_m2 = len(tcs_m2) - len(valid_m2)

    f.write(f"\n  --- Per-Module Breakdown — Valid TCs Only ({method_label}) ---\n")
    f.write(f"  * Excludes invalid TCs (WA/RE/TLE/OLE/MLE). Shows tier accuracy\n")
    f.write(f"    among successfully generated TCs: \"of valid TCs, how many hit the right tier?\"\n")
    f.write(f"  {'':30} {'All':>20} {'M1(boundary)':>20} {'M2(algorithmic)':>20}\n")
    f.write(f"  {'-' * 90}\n")

    all_sets = [("All", tcs), ("M1(boundary)", tcs_m1), ("M2(algorithmic)", tcs_m2)]

    for tier in ("fast", "medium", "slow"):
        cells = []
        for _label, subset in all_sets:
            hit, total = _tpr_counts_valid(subset, tier)
            ratio = hit / total if total > 0 else 0.0
            cells.append(f"{ratio:.4f} ({hit}/{total})")
        pad = " " * (6 - len(tier))
        f.write(f"  TPR({tier}){pad}                   {cells[0]:>20} {cells[1]:>20} {cells[2]:>20}\n")

    f.write(f"  {'-' * 90}\n")
    f.write(f"  TC total                         {len(tcs):>20} {len(tcs_m1):>20} {len(tcs_m2):>20}\n")
    f.write(f"  TC valid                         {len(valid_all):>20} {len(valid_m1):>20} {len(valid_m2):>20}\n")
    f.write(f"  TC invalid                       {invalid_all:>20} {invalid_m1:>20} {invalid_m2:>20}\n")

def _write_tier_confusion_matrix(f, tcs: list, method_label: str):
    tiers = ("fast", "medium", "slow")
    cols = ("fast", "medium", "slow", "invalid")

    matrix = {t: {c: 0 for c in cols} for t in tiers}
    for tc in tcs:
        intended = tc.get("target_tier")
        actual = tc.get("difficulty", "invalid")
        if intended not in tiers:
            continue
        if actual not in cols:
            actual = "invalid"
        matrix[intended][actual] += 1

    f.write(f"\n  Tier Confusion Matrix ({method_label}):\n")
    header = "intended \\ actual"
    f.write(f"  {header:<20}")
    for c in cols:
        f.write(f" {c:>10}")
    f.write(f" {'Total':>10}\n")
    f.write(f"  {'-' * 72}\n")

    col_totals = {c: 0 for c in cols}
    grand_total = 0
    for t in tiers:
        row_total = sum(matrix[t].values())
        f.write(f"  {t:<20}")
        for c in cols:
            f.write(f" {matrix[t][c]:>10}")
            col_totals[c] += matrix[t][c]
        f.write(f" {row_total:>10}\n")
        grand_total += row_total

    f.write(f"  {'-' * 72}\n")
    f.write(f"  {'Total':<20}")
    for c in cols:
        f.write(f" {col_totals[c]:>10}")
    f.write(f" {grand_total:>10}\n")

def _write_tier_confusion_matrix_per_module(f, tcs: list, method_label: str):
    tiers = ("fast", "medium", "slow")
    cols = ("fast", "medium", "slow", "invalid")

    m1_tcs = [tc for tc in tcs if tc.get("module") == "M1"]
    m2_tcs = [tc for tc in tcs if tc.get("module") == "M2"]
    if not m1_tcs and not m2_tcs:
        return

    def _build_matrix(tc_list):
        matrix = {t: {c: 0 for c in cols} for t in tiers}
        for tc in tc_list:
            intended = tc.get("target_tier")
            actual = tc.get("difficulty", "invalid")
            if intended not in tiers:
                continue
            if actual not in cols:
                actual = "invalid"
            matrix[intended][actual] += 1
        return matrix

    matrices = {
        "All": _build_matrix(tcs),
        "M1(boundary)": _build_matrix(m1_tcs),
        "M2(algorithmic)": _build_matrix(m2_tcs),
    }

    for label, matrix in matrices.items():
        f.write(f"\n  Tier Confusion Matrix ({method_label}) — {label}:\n")
        header = "intended \\ actual"
        f.write(f"  {header:<20}")
        for c in cols:
            f.write(f" {c:>10}")
        f.write(f" {'Total':>10}\n")
        f.write(f"  {'-' * 72}\n")

        col_totals = {c: 0 for c in cols}
        grand_total = 0
        for t in tiers:
            row_total = sum(matrix[t].values())
            f.write(f"  {t:<20}")
            for c in cols:
                f.write(f" {matrix[t][c]:>10}")
                col_totals[c] += matrix[t][c]
            f.write(f" {row_total:>10}\n")
            grand_total += row_total

        f.write(f"  {'-' * 72}\n")
        f.write(f"  {'Total':<20}")
        for c in cols:
            f.write(f" {col_totals[c]:>10}")
        f.write(f" {grand_total:>10}\n")

def write_summary(
    comparisons: list,
    output_dir: Path,
    split: str,
    reference_path: str,
    evaluand_path: str,
    generator_tier_map: dict | None = None,
    lang_label: str = "total",
    tcs_per_tier: int | None = None,
    slow_ratio_threshold: float = 0.9,
    ref_max_ref_times: dict | None = None,
):
    summary_path = output_dir / "summary.txt"

    total_problems = len(comparisons)
    both_ok = [c for c in comparisons if c.get("reference", {}).get("status") == "ok"
               and c.get("evaluand", {}).get("status") == "ok"]
    reference_only = [c for c in comparisons if c.get("reference", {}).get("status") == "ok"
                      and c.get("evaluand", {}).get("status") != "ok"]
    evaluand_only = [c for c in comparisons if c.get("evaluand", {}).get("status") == "ok"
                     and c.get("reference", {}).get("status") != "ok"]

    ref_problems = [c for c in comparisons if c.get("reference", {}).get("status") == "ok"]
    num_ref_problems = len(ref_problems)

    if generator_tier_map:
        tc_counts = [len(m) for m in generator_tier_map.values() if m]
        if tc_counts:
            tcs_per_problem = max(set(tc_counts), key=tc_counts.count)
            if tcs_per_tier is None:
                sample_mapping = next(iter(generator_tier_map.values()))
                tier_counts = defaultdict(int)
                for tier in sample_mapping.values():
                    tier_counts[tier] += 1
                tcs_per_tier = max(tier_counts.values()) if tier_counts else tcs_per_problem // 3
        else:
            tcs_per_problem = 15
            if tcs_per_tier is None:
                tcs_per_tier = 5
    else:
        ev_tc_counts = []
        for c in both_ok:
            ev_info = c.get("evaluand", {})
            if ev_info.get("status") == "ok":
                ev_tc_counts.append(len(ev_info.get("testcases", [])))
        if ev_tc_counts:
            tcs_per_problem = max(set(ev_tc_counts), key=ev_tc_counts.count)
            if tcs_per_tier is None:
                tcs_per_tier = tcs_per_problem // 3
        else:
            tcs_per_problem = 15
            if tcs_per_tier is None:
                tcs_per_tier = 5

    agg_ref = defaultdict(int)
    agg_ev = defaultdict(int)
    agg_ev_invalid = 0
    agg_ref_tc = defaultdict(int)
    agg_ev_tc = defaultdict(int)
    agg_ev_invalid_tc = 0
    agg_ev_rb = defaultdict(int)
    agg_ev_invalid_rb = 0
    agg_ev_ratio = defaultdict(int)
    agg_ev_invalid_ratio = 0
    agg_ev_exceeded = 0
    agg_ev_not_exceeded = 0
    agg_ev_invalid_binary = 0
    agg_ev_tle_compliant = 0
    agg_ev_tle_violating = 0
    verdicts = defaultdict(int)
    tl_p33_vals: list[float] = []
    tl_p67_vals: list[float] = []
    tc_p33_vals: list[float] = []
    tc_p67_vals: list[float] = []

    all_ev_tcs: list[dict] = []
    all_ev_tcs_tc: list[dict] = []
    all_ev_tcs_rb: list[dict] = []
    all_ev_tcs_ratio: list[dict] = []
    all_ev_tcs_binary: list[dict] = []
    all_ev_tcs_tle: list[dict] = []
    max_ref_time_pairs: list[tuple[str, float]] = []
    if ref_max_ref_times:
        for _name, _per_lang in ref_max_ref_times.items():
            for _lang, _mrt in _per_lang.items():
                if _lang and _mrt is not None and _mrt > 0:
                    max_ref_time_pairs.append((_lang, float(_mrt)))

    generated_ev_tc = 0
    valid_ev_tc = 0
    wa_ev_tc = 0
    tier_accuracy: dict[str, dict[str, int]] = {
        t: {"generated": 0, "valid": 0, "invalid": 0} for t in ("fast", "medium", "slow")
    }
    _ALL_LANG_ORDER = ["cpp", "python3", "java"]
    _LANG_LABEL_MAP = {"cpp": ["cpp"], "python": ["python3"], "java": ["java"]}
    LANG_ORDER = _LANG_LABEL_MAP.get(lang_label, _ALL_LANG_ORDER)
    TIER_ORDER_ACC = ["fast", "medium", "slow"]
    lang_tier_valid: dict = {
        lang: {t: 0 for t in TIER_ORDER_ACC} for lang in LANG_ORDER
    }
    all_lang_valid_tier: dict = {t: 0 for t in TIER_ORDER_ACC}
    problems_with_any_valid = 0
    problems_with_valid_tier: dict[str, int] = {"fast": 0, "medium": 0, "slow": 0}

    _rank = {"invalid": 3, "slow": 2, "medium": 1, "fast": 0}

    def _count_ev_tcs(ev_info: dict):
        nonlocal generated_ev_tc, valid_ev_tc, wa_ev_tc
        rows = ev_info.get("testcases", [])

        tc_valid_langs: dict = {}
        tc_valid_tier: dict = {}
        for tc in rows:
            lang = tc.get("language")
            tier = tc.get("target_tier")
            tc_id = tc.get("testcase")
            if lang in lang_tier_valid and tier in lang_tier_valid[lang]:
                if tc.get("difficulty") != "invalid":
                    lang_tier_valid[lang][tier] += 1
                    if tc_id is not None:
                        if tc_id not in tc_valid_langs:
                            tc_valid_langs[tc_id] = set()
                            tc_valid_tier[tc_id] = tier
                        tc_valid_langs[tc_id].add(lang)
        for tc_id, langs in tc_valid_langs.items():
            if set(LANG_ORDER).issubset(langs):
                t = tc_valid_tier.get(tc_id)
                if t in all_lang_valid_tier:
                    all_lang_valid_tier[t] += 1

        tc_worst: dict = {}
        tc_target: dict = {}
        for tc in rows:
            tc_id = tc.get("testcase")
            if tc_id is None:
                continue
            d = tc.get("difficulty") or "fast"
            r = _rank.get(d, 0)
            if tc_id not in tc_worst or r > _rank.get(tc_worst[tc_id], 0):
                tc_worst[tc_id] = d
                tc_target[tc_id] = tc.get("target_tier")

        for tc_id, d in tc_worst.items():
            generated_ev_tc += 1
            is_invalid = (d == "invalid")
            if is_invalid:
                wa_ev_tc += 1
            else:
                valid_ev_tc += 1
            tt = tc_target.get(tc_id)
            if tt in tier_accuracy:
                tier_accuracy[tt]["generated"] += 1
                if is_invalid:
                    tier_accuracy[tt]["invalid"] += 1
                else:
                    tier_accuracy[tt]["valid"] += 1

    for c in ref_problems:
        ref_per_tc = c["reference"]["timelimit_based"].get("counts_per_tc") or {}
        for d in DIFFICULTY_ORDER:
            agg_ref[d] += ref_per_tc.get(d, 0)
        ref_tc_counts = c["reference"].get("tercile_based", {}).get("counts", {})
        for d in DIFFICULTY_ORDER:
            agg_ref_tc[d] += ref_tc_counts.get(d, 0)
        tl = c.get("timelimit", 0)
        if tl > 0:
            tl_p33_vals.append(tl / 3.0)
            tl_p67_vals.append(2.0 * tl / 3.0)
        ref_perc = c["reference"].get("tercile_based", {}).get("percentiles", {})
        if ref_perc.get("p33_ratio") is not None:
            tc_p33_vals.append(ref_perc["p33_ratio"])
        if ref_perc.get("p67_ratio") is not None:
            tc_p67_vals.append(ref_perc["p67_ratio"])

    _pp_tpr_tl: dict = {"slow": [], "medium": [], "fast": []}
    _pp_tpr_tc: dict = {"slow": [], "medium": [], "fast": []}
    _agg_norm_tl: dict = {t: {"hit": 0, "denom": 0} for t in ("slow", "medium", "fast")}
    _agg_norm_tc: dict = {t: {"hit": 0, "denom": 0} for t in ("slow", "medium", "fast")}
    _pp_tpr_rb: dict = {"slow": [], "medium": [], "fast": []}
    _agg_norm_rb: dict = {t: {"hit": 0, "denom": 0} for t in ("slow", "medium", "fast")}
    _pp_tpr_ratio: dict = {"slow": [], "medium": [], "fast": []}
    _agg_norm_ratio: dict = {t: {"hit": 0, "denom": 0} for t in ("slow", "medium", "fast")}

    for c in both_ok:
        ev_info = c["evaluand"]
        ev_per_tc = ev_info["timelimit_based"].get("counts_per_tc") or {}
        for d in DIFFICULTY_ORDER:
            agg_ev[d] += ev_per_tc.get(d, 0)
        agg_ev_invalid += ev_per_tc.get("invalid", 0)
        ev_tc_counts = ev_info.get("tercile_based", {}).get("counts", {})
        for d in DIFFICULTY_ORDER:
            agg_ev_tc[d] += ev_tc_counts.get(d, 0)
        agg_ev_invalid_tc += ev_tc_counts.get("invalid", 0)
        ev_rb_counts = ev_info.get("ref_bounds_based", {}).get("counts_per_tc") or {}
        for d in DIFFICULTY_ORDER:
            agg_ev_rb[d] += ev_rb_counts.get(d, 0)
        agg_ev_invalid_rb += ev_rb_counts.get("invalid", 0)
        ev_ratio_counts = ev_info.get("ratio_based", {}).get("counts_per_tc") or {}
        for d in DIFFICULTY_ORDER:
            agg_ev_ratio[d] += ev_ratio_counts.get(d, 0)
        agg_ev_invalid_ratio += ev_ratio_counts.get("invalid", 0)
        ev_binary = ev_info.get("binary_exceeded", {}).get("counts", {})
        agg_ev_exceeded += ev_binary.get("exceeded", 0)
        agg_ev_not_exceeded += ev_binary.get("not_exceeded", 0)
        agg_ev_invalid_binary += ev_binary.get("invalid", 0)
        ev_tle = ev_info.get("tle_compliant", {}).get("counts", {})
        agg_ev_tle_compliant += ev_tle.get("tle_compliant", 0)
        agg_ev_tle_violating += ev_tle.get("tle_violating", 0)

        _count_ev_tcs(ev_info)
        all_ev_tcs.extend(ev_info.get("testcases", []))

        tcs_this_prob = ev_info.get("testcases", [])
        tc_worst_prob: dict = {}
        tc_target_prob: dict = {}
        for tc in tcs_this_prob:
            tc_id = tc.get("testcase")
            if tc_id is None:
                continue
            d = tc.get("difficulty") or "fast"
            r = _rank.get(d, 0)
            if tc_id not in tc_worst_prob or r > _rank.get(tc_worst_prob[tc_id], 0):
                tc_worst_prob[tc_id] = d
                tc_target_prob[tc_id] = tc.get("target_tier")
        valid_unique = {tc_id for tc_id, d in tc_worst_prob.items() if d != "invalid"}
        if valid_unique:
            problems_with_any_valid += 1
        for tier in ("fast", "medium", "slow"):
            if any(tc_target_prob.get(tc_id) == tier for tc_id in valid_unique):
                problems_with_valid_tier[tier] += 1

        tc_target_map = {
            tc.get("testcase"): tc.get("target_tier")
            for tc in ev_info.get("testcases", [])
            if tc.get("testcase") is not None and tc.get("target_tier")
        }
        tc_module_map = {
            tc.get("testcase"): tc.get("module")
            for tc in ev_info.get("testcases", [])
            if tc.get("testcase") is not None and tc.get("module")
        }
        for tc in ev_info.get("tercile_based", {}).get("testcases", []):
            tc_copy = dict(tc)
            tc_id = tc_copy.get("testcase")
            if tc_id in tc_target_map:
                tc_copy["target_tier"] = tc_target_map[tc_id]
            if tc_id in tc_module_map:
                tc_copy["module"] = tc_module_map[tc_id]
            all_ev_tcs_tc.append(tc_copy)

        for tc in ev_info.get("ref_bounds_based", {}).get("testcases", []):
            tc_copy = dict(tc)
            tc_id = tc_copy.get("testcase")
            if tc_id in tc_target_map:
                tc_copy["target_tier"] = tc_target_map[tc_id]
            if tc_id in tc_module_map:
                tc_copy["module"] = tc_module_map[tc_id]
            all_ev_tcs_rb.append(tc_copy)

        for tc in ev_info.get("ratio_based", {}).get("testcases", []):
            tc_copy = dict(tc)
            tc_id = tc_copy.get("testcase")
            if tc_id in tc_target_map:
                tc_copy["target_tier"] = tc_target_map[tc_id]
            if tc_id in tc_module_map:
                tc_copy["module"] = tc_module_map[tc_id]
            all_ev_tcs_ratio.append(tc_copy)

        for tc in ev_info.get("binary_exceeded", {}).get("testcases", []):
            tc_copy = dict(tc)
            tc_id = tc_copy.get("testcase")
            if tc_id in tc_target_map:
                tc_copy["target_tier"] = tc_target_map[tc_id]
            if tc_id in tc_module_map:
                tc_copy["module"] = tc_module_map[tc_id]
            all_ev_tcs_binary.append(tc_copy)

        for tc in ev_info.get("tle_compliant", {}).get("testcases", []):
            tc_copy = dict(tc)
            tc_id = tc_copy.get("testcase")
            if tc_id in tc_target_map:
                tc_copy["target_tier"] = tc_target_map[tc_id]
            if tc_id in tc_module_map:
                tc_copy["module"] = tc_module_map[tc_id]
            all_ev_tcs_tle.append(tc_copy)

        _tl_tcs = ev_info.get("testcases", [])
        _avail_langs = set(ev_info.get("language_solutions", {}).keys()) or None

        _rb_tcs_with_target = []
        for _tc in ev_info.get("ref_bounds_based", {}).get("testcases", []):
            _tc_copy = dict(_tc)
            _tc_id = _tc_copy.get("testcase")
            if _tc_id in tc_target_map:
                _tc_copy["target_tier"] = tc_target_map[_tc_id]
            if _tc_id in tc_module_map:
                _tc_copy["module"] = tc_module_map[_tc_id]
            _rb_tcs_with_target.append(_tc_copy)
        for _tier in ("slow", "medium", "fast"):
            _pp_tpr_rb[_tier].append(
                _tpr_normalized_for_problem(_rb_tcs_with_target, tcs_per_tier, _tier, _avail_langs))
            _h, _d = _tpr_norm_counts(_rb_tcs_with_target, tcs_per_tier, _tier, _avail_langs)
            _agg_norm_rb[_tier]["hit"] += _h
            _agg_norm_rb[_tier]["denom"] += _d

        _ratio_tcs_with_target = []
        for _tc in ev_info.get("ratio_based", {}).get("testcases", []):
            _tc_copy = dict(_tc)
            _tc_id = _tc_copy.get("testcase")
            if _tc_id in tc_target_map:
                _tc_copy["target_tier"] = tc_target_map[_tc_id]
            if _tc_id in tc_module_map:
                _tc_copy["module"] = tc_module_map[_tc_id]
            _ratio_tcs_with_target.append(_tc_copy)
        for _tier in ("slow", "medium", "fast"):
            _pp_tpr_ratio[_tier].append(
                _tpr_normalized_for_problem(_ratio_tcs_with_target, tcs_per_tier, _tier, _avail_langs))
            _h, _d = _tpr_norm_counts(_ratio_tcs_with_target, tcs_per_tier, _tier, _avail_langs)
            _agg_norm_ratio[_tier]["hit"] += _h
            _agg_norm_ratio[_tier]["denom"] += _d

        for _tier in ("slow", "medium", "fast"):
            _pp_tpr_tl[_tier].append(
                _tpr_normalized_for_problem(_tl_tcs, tcs_per_tier, _tier, _avail_langs))
            _h, _d = _tpr_norm_counts(_tl_tcs, tcs_per_tier, _tier, _avail_langs)
            _agg_norm_tl[_tier]["hit"] += _h
            _agg_norm_tl[_tier]["denom"] += _d

        _tc_tcs_with_target = []
        for _tc in ev_info.get("tercile_based", {}).get("testcases", []):
            _tc_copy = dict(_tc)
            _tc_id = _tc_copy.get("testcase")
            if _tc_id in tc_target_map:
                _tc_copy["target_tier"] = tc_target_map[_tc_id]
            if _tc_id in tc_module_map:
                _tc_copy["module"] = tc_module_map[_tc_id]
            _tc_tcs_with_target.append(_tc_copy)
        for _tier in ("slow", "medium", "fast"):
            _pp_tpr_tc[_tier].append(
                _tpr_normalized_for_problem(_tc_tcs_with_target, tcs_per_tier, _tier, _avail_langs))
            _h, _d = _tpr_norm_counts(_tc_tcs_with_target, tcs_per_tier, _tier, _avail_langs)
            _agg_norm_tc[_tier]["hit"] += _h
            _agg_norm_tc[_tier]["denom"] += _d

        if "shift_analysis" in c and c["shift_analysis"].get("status") == "ok":
            verdicts[c["shift_analysis"]["verdict"]] += 1

    expected_total_tc = num_ref_problems * tcs_per_problem
    missing_tc = expected_total_tc - generated_ev_tc
    tc_accuracy = round(valid_ev_tc / expected_total_tc, 4) if expected_total_tc > 0 else 0.0

    def _norm_tpr(per_prob_list: list, n_ref_problems: int, n_ref_only: int) -> float:
        all_vals = per_prob_list + [0.0] * n_ref_only
        return round(sum(all_vals) / n_ref_problems, 4) if n_ref_problems > 0 else 0.0

    _n_ref_only = len(reference_only)

    tpr_norm_tl_slow   = _norm_tpr(_pp_tpr_tl["slow"],   num_ref_problems, _n_ref_only)
    tpr_norm_tl_medium = _norm_tpr(_pp_tpr_tl["medium"], num_ref_problems, _n_ref_only)
    tpr_norm_tl_fast   = _norm_tpr(_pp_tpr_tl["fast"],   num_ref_problems, _n_ref_only)
    tpr_norm_tc_slow   = _norm_tpr(_pp_tpr_tc["slow"],   num_ref_problems, _n_ref_only)
    tpr_norm_tc_medium = _norm_tpr(_pp_tpr_tc["medium"], num_ref_problems, _n_ref_only)
    tpr_norm_tc_fast   = _norm_tpr(_pp_tpr_tc["fast"],   num_ref_problems, _n_ref_only)
    tpr_norm_rb_slow   = _norm_tpr(_pp_tpr_rb["slow"],   num_ref_problems, _n_ref_only)
    tpr_norm_rb_medium = _norm_tpr(_pp_tpr_rb["medium"], num_ref_problems, _n_ref_only)
    tpr_norm_rb_fast   = _norm_tpr(_pp_tpr_rb["fast"],   num_ref_problems, _n_ref_only)
    tpr_norm_ratio_slow   = _norm_tpr(_pp_tpr_ratio["slow"],   num_ref_problems, _n_ref_only)
    tpr_norm_ratio_medium = _norm_tpr(_pp_tpr_ratio["medium"], num_ref_problems, _n_ref_only)
    tpr_norm_ratio_fast   = _norm_tpr(_pp_tpr_ratio["fast"],   num_ref_problems, _n_ref_only)

    def _has_target_tier(tcs: list[dict]) -> bool:
        return any("target_tier" in tc for tc in tcs)

    tpr_ev_tl = taa_ev_tl = 0.0
    tpr_ev_tl_fast = tpr_ev_tl_medium = 0.0

    if _has_target_tier(all_ev_tcs):
        tpr_ev_tl = calculate_tpr(all_ev_tcs, target_tier="slow")
        tpr_ev_tl_fast = calculate_tpr(all_ev_tcs, target_tier="fast")
        tpr_ev_tl_medium = calculate_tpr(all_ev_tcs, target_tier="medium")
        taa_ev_tl = calculate_taa(all_ev_tcs)

    ats_tl_scores = []
    for c in both_ok:
        ref_tcs = c["reference"].get("testcases", [])
        ev_tcs = c["evaluand"].get("testcases", [])
        ref_map = {(tc.get("testcase"), tc.get("language")): tc for tc in ref_tcs if "difficulty" in tc}
        ev_map = {(tc.get("testcase"), tc.get("language")): tc for tc in ev_tcs if "difficulty" in tc}
        common_keys = sorted(set(ref_map.keys()) & set(ev_map.keys()))
        if common_keys:
            score = calculate_ats(
                [ref_map[k] for k in common_keys],
                [ev_map[k] for k in common_keys],
            )
            ats_tl_scores.append(score)
    ats_tl = round(sum(ats_tl_scores) / len(ats_tl_scores), 4) if ats_tl_scores else 0.0

    tpr_ev_tc = taa_ev_tc = 0.0
    tpr_ev_tc_fast = tpr_ev_tc_medium = 0.0

    if _has_target_tier(all_ev_tcs_tc):
        tpr_ev_tc = calculate_tpr(all_ev_tcs_tc, target_tier="slow")
        tpr_ev_tc_fast = calculate_tpr(all_ev_tcs_tc, target_tier="fast")
        tpr_ev_tc_medium = calculate_tpr(all_ev_tcs_tc, target_tier="medium")
        taa_ev_tc = calculate_taa(all_ev_tcs_tc)

    ats_tc_scores = []
    for c in both_ok:
        ref_tcs = c["reference"].get("tercile_based", {}).get("testcases", [])
        ev_info_c = c["evaluand"]
        tc_target_map_c = {
            tc.get("testcase"): tc.get("target_tier")
            for tc in ev_info_c.get("testcases", [])
            if tc.get("testcase") is not None and tc.get("target_tier")
        }
        ev_tcs_tc = []
        for tc in ev_info_c.get("tercile_based", {}).get("testcases", []):
            tc_copy = dict(tc)
            if tc_copy.get("testcase") in tc_target_map_c:
                tc_copy["target_tier"] = tc_target_map_c[tc_copy["testcase"]]
            ev_tcs_tc.append(tc_copy)
        ref_map = {(tc.get("testcase"), tc.get("language")): tc for tc in ref_tcs if "difficulty" in tc}
        ev_map = {(tc.get("testcase"), tc.get("language")): tc for tc in ev_tcs_tc if "difficulty" in tc}
        common_keys = sorted(set(ref_map.keys()) & set(ev_map.keys()))
        if common_keys:
            score = calculate_ats(
                [ref_map[k] for k in common_keys],
                [ev_map[k] for k in common_keys],
            )
            ats_tc_scores.append(score)
    ats_tc = round(sum(ats_tc_scores) / len(ats_tc_scores), 4) if ats_tc_scores else 0.0

    tpr_ev_rb = taa_ev_rb = 0.0
    tpr_ev_rb_fast = tpr_ev_rb_medium = 0.0

    if _has_target_tier(all_ev_tcs_rb):
        tpr_ev_rb = calculate_tpr(all_ev_tcs_rb, target_tier="slow")
        tpr_ev_rb_fast = calculate_tpr(all_ev_tcs_rb, target_tier="fast")
        tpr_ev_rb_medium = calculate_tpr(all_ev_tcs_rb, target_tier="medium")
        taa_ev_rb = calculate_taa(all_ev_tcs_rb)

    tpr_ev_ratio = 0.0
    tpr_ev_ratio_fast = tpr_ev_ratio_medium = 0.0
    if _has_target_tier(all_ev_tcs_ratio):
        tpr_ev_ratio = calculate_tpr(all_ev_tcs_ratio, target_tier="slow")
        tpr_ev_ratio_fast = calculate_tpr(all_ev_tcs_ratio, target_tier="fast")
        tpr_ev_ratio_medium = calculate_tpr(all_ev_tcs_ratio, target_tier="medium")

    tpr_ev, tpr_ev_fast, tpr_ev_medium, taa_ev, ats_score = (
        tpr_ev_tc, tpr_ev_tc_fast, tpr_ev_tc_medium, taa_ev_tc, ats_tc
    )

    def _median(vals: list) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        return (s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2)

    tl_med_p33 = _median(tl_p33_vals)
    tl_med_p67 = _median(tl_p67_vals)
    tc_med_p33 = _median(tc_p33_vals)
    tc_med_p67 = _median(tc_p67_vals)

    def _fmt_problem_list(comps: list, max_show: int = 20) -> str:
        names = [f"[{c['index']}] {c['name']}" for c in comps]
        if len(names) <= max_show:
            return "\n".join(f"      {n}" for n in names)
        shown = "\n".join(f"      {n}" for n in names[:max_show])
        return shown + f"\n      ... and {len(names) - max_show} more"

    _tier_total = {t: sum(lang_tier_valid[l].get(t, 0) for l in LANG_ORDER) for t in TIER_ORDER_ACC}
    _nonzero_tiers = [t for t, c in _tier_total.items() if c > 0]
    _single_tier = _nonzero_tiers[0] if len(_nonzero_tiers) == 1 else None

    with open(summary_path, "w") as f:
        f.write(f"Slow Test Case Evaluation Summary\n{'=' * 60}\n")
        f.write(f"Split: {split}\n")
        f.write(f"Reference (criterion) timing: {reference_path}\n")
        f.write(f"Evaluand (slow) timing: {evaluand_path}\n")
        if _single_tier:
            f.write(f"\n⚠ Tier Coverage: SINGLE TIER ONLY ('{_single_tier}', count={_tier_total[_single_tier]})\n")
            f.write(f"   fast/medium/slow tier breakdown not applicable for this source.\n")
            f.write(f"   Typical for worst-case generators (e.g., WEDGE — paper §4.1).\n")
        else:
            f.write(f"\nTier Coverage (valid TCs across languages): "
                    f"fast={_tier_total['fast']}, medium={_tier_total['medium']}, slow={_tier_total['slow']}\n")
        f.write(f"{'=' * 60}\n\n")

        f.write(f"Problems:\n")
        f.write(f"  Total (reference): {num_ref_problems}\n")
        f.write(f"  Both sources available: {len(both_ok)}\n")
        if both_ok:
            f.write(_fmt_problem_list(both_ok) + "\n")
        f.write(f"  Reference only: {len(reference_only)}\n")
        if reference_only:
            f.write(_fmt_problem_list(reference_only) + "\n")
        f.write(f"  Evaluand only: {len(evaluand_only)}\n")
        if evaluand_only:
            f.write(_fmt_problem_list(evaluand_only) + "\n")

        _m1_problems = set()
        _m2_problems = set()
        for _pname, _tc_methods in _generator_method_map.items():
            for _tc_num, _meth in _tc_methods.items():
                _mod = _classify_method(_meth)
                if _mod == "M1":
                    _m1_problems.add(_pname)
                elif _mod == "M2":
                    _m2_problems.add(_pname)
        if _m1_problems or _m2_problems:
            _m1_only = _m1_problems - _m2_problems
            _m2_only = _m2_problems - _m1_problems
            _both_mod = _m1_problems & _m2_problems
            f.write(f"\n  Module Usage (from generator inputs):\n")
            f.write(f"    M1 only (boundary):    {len(_m1_only)} problems\n")
            f.write(f"    M2 only (algorithmic): {len(_m2_only)} problems\n")
            f.write(f"    Both M1+M2:            {len(_both_mod)} problems\n")
        f.write("\n")

        f.write("============================================================\n")
        f.write(f"**TC Accuracy**\n")
        f.write(f"  (Note: 'valid' = TC got 'correct'/'AC' verdict from all gold solutions.\n"
                f"   'invalid' = any non-correct verdict (WA, run-error, TLE, OLE, MLE, empty).\n"
                f"   Per-language .ans generation ensures WA reliably indicates a bad TC.)\n")
        f.write(f"Test Case Accuracy (evaluand, scope = {num_ref_problems} reference problems):\n")
        f.write(f"  Expected TCs: {expected_total_tc} ({num_ref_problems} problems x {tcs_per_problem} TCs)\n")
        f.write(f"  Generated (judged, unique TC): {generated_ev_tc} ({generated_ev_tc * 100 / max(expected_total_tc, 1):.1f}%)\n")
        f.write(f"  Missing (no evaluand): {missing_tc} ({missing_tc * 100 / max(expected_total_tc, 1):.1f}%)"
                f"  — {len(reference_only)} problems have no evaluand data\n")
        f.write(f"  Valid (correct, unique TC): {valid_ev_tc} ({valid_ev_tc * 100 / max(expected_total_tc, 1):.1f}%)\n")
        f.write(f"  Invalid (WA/RE/TLE/OLE/MLE/empty, unique TC): {wa_ev_tc} ({wa_ev_tc * 100 / max(expected_total_tc, 1):.1f}%)\n")
        f.write(f"  TC Accuracy (valid / expected): {tc_accuracy:.4f}\n\n")

        f.write(f"  Per-Language Breakdown (tier = target_tier, valid = timelimit-based):\n")
        f.write(f"    valid runs / unique valid TCs per target tier:\n")
        f.write(f"    {'Language':<10}")
        for t in TIER_ORDER_ACC:
            uv = tier_accuracy[t]["valid"]
            f.write(f"  {t}(valid={uv})")
        f.write("\n")
        f.write(f"    {'-' * 62}\n")
        for lang in LANG_ORDER:
            f.write(f"    {lang:<10}")
            for t in TIER_ORDER_ACC:
                uv = tier_accuracy[t]["valid"]
                lv = lang_tier_valid[lang][t]
                pct = lv / uv * 100 if uv > 0 else 0.0
                f.write(f"  {lv:>4} / {uv:<4} ({pct:4.1f}%)")
            f.write("\n")
        f.write(f"    {'-' * 62}\n")
        f.write(f"    {'all langs':<10}")
        for t in TIER_ORDER_ACC:
            uv = tier_accuracy[t]["valid"]
            av = all_lang_valid_tier[t]
            pct = av / uv * 100 if uv > 0 else 0.0
            f.write(f"  {av:>4} / {uv:<4} ({pct:4.1f}%)")
        f.write("\n")
        f.write("\n")

        expected_per_tier = num_ref_problems * tcs_per_tier
        f.write(f"  Per-Tier TC Accuracy (intended tier, {tcs_per_tier} TCs/tier/problem):\n")
        f.write(f"    {'Tier':<8} {'Expected':>8} {'Generated':>10} {'Valid':>6} {'Invalid':>8} {'Acc(v/e)':>10}\n")
        f.write(f"    {'-' * 56}\n")
        has_any_tier = False
        for tier in ("fast", "medium", "slow"):
            ta = tier_accuracy[tier]
            if ta["generated"] > 0:
                has_any_tier = True
            acc = ta["valid"] / expected_per_tier if expected_per_tier > 0 else 0.0
            f.write(f"    {tier:<8} {expected_per_tier:>8} {ta['generated']:>10} "
                    f"{ta['valid']:>6} {ta['invalid']:>8} {acc:>9.4f}\n")
        no_tier_count = generated_ev_tc - sum(ta["generated"] for ta in tier_accuracy.values())
        if no_tier_count > 0:
            f.write(f"    {'(no tier)':<8} {'':>8} {no_tier_count:>10}    (target_tier not mapped)\n")
        if not has_any_tier:
            f.write(f"    (No target_tier data — generator output not provided?)\n")
        f.write("\n")

        scope = len(both_ok)
        f.write(f"  Problems with ≥1 valid TC surviving (scope = {scope} both_ok problems):\n")
        f.write(f"    {'Tier':<8} {'Problems':>8} {'/ scope':>8}   {'%':>6}\n")
        f.write(f"    {'-' * 36}\n")
        for tier in ("fast", "medium", "slow"):
            cnt = problems_with_valid_tier[tier]
            pct = cnt / scope * 100 if scope > 0 else 0.0
            f.write(f"    {tier:<8} {cnt:>8} {'/ ' + str(scope):>8}   {pct:>5.1f}%\n")
        total_pct = problems_with_any_valid / scope * 100 if scope > 0 else 0.0
        f.write(f"    {'-' * 36}\n")
        f.write(f"    {'any':<8} {problems_with_any_valid:>8} {'/ ' + str(scope):>8}   {total_pct:>5.1f}%\n")
        f.write("\n")

        if _EMIT_LEGACY_CRITERIA:
            f.write("============================================================\n")
            f.write(f"**Criterion: Tercile (Data-Driven Percentile Split)**\n")
            f.write(f"  Splits reference TC execution times into 3 equal-count groups per problem.\n")
            f.write(f"  Boundaries = 33rd & 67th percentile of actual reference run_time/timelimit ratios.\n")
            f.write(f"  Unlike fixed-ratio methods, adapts to each problem's actual time distribution.\n\n")
            f.write(f"Aggregate Difficulty Distribution (tercile-based, per TC x language):\n")
            f.write(f"  [Reference scope: all {num_ref_problems} reference problems | Evaluand scope: {len(both_ok)} both_ok problems]\n")
            ref_tc_total = sum(agg_ref_tc.values())
            ev_tc_total = sum(agg_ev_tc.values()) + agg_ev_invalid_tc
            f.write(f"  {'Difficulty':<10} {'Reference':>12} {'%':>8}  {'Evaluand':>12} {'%':>8}  {'Delta':>8}\n")
            f.write(f"  {'-' * 62}\n")
            for d in DIFFICULTY_ORDER:
                r_cnt = agg_ref_tc[d]
                e_cnt = agg_ev_tc[d]
                r_pct = (r_cnt / ref_tc_total * 100) if ref_tc_total > 0 else 0
                e_pct = (e_cnt / ev_tc_total * 100) if ev_tc_total > 0 else 0
                delta = e_pct - r_pct
                f.write(f"  {d:<10} {r_cnt:>12} {r_pct:>7.1f}%  {e_cnt:>12} {e_pct:>7.1f}%  {delta:>+7.1f}%\n")
            if agg_ev_invalid_tc > 0:
                inv_pct = (agg_ev_invalid_tc / ev_tc_total * 100) if ev_tc_total > 0 else 0
                f.write(f"  {'invalid':<10} {'—':>12} {'—':>8}  {agg_ev_invalid_tc:>12} {inv_pct:>7.1f}%  {'—':>8}\n")
            f.write(f"  {'-' * 62}\n")
            f.write(f"  {'Total':<10} {ref_tc_total:>12}          {ev_tc_total:>12}\n\n")
            f.write("Evaluand Metrics (over TCs with target_tier, tercile difficulty):\n")
            _write_tpr_taa_block(f, all_ev_tcs_tc, "tercile", num_ref_problems)
            if lang_label != "total":
                _expected = num_ref_problems * tcs_per_tier
                for _tier, _val in [("fast", tpr_norm_tc_fast), ("medium", tpr_norm_tc_medium), ("slow", tpr_norm_tc_slow)]:
                    _h = _agg_norm_tc[_tier]["hit"]
                    _pad = " " * (6 - len(_tier))
                    f.write(f"  TPR_norm({_tier}){_pad}= {_val:.4f}  ({_h}/{_expected})  "
                            f"(scope={num_ref_problems} ref problems x {tcs_per_tier} TCs)\n")
            f.write(f"  ATS        = {ats_tc:.4f}  (reference vs evaluand tier sensitivity)\n")
            f.write(f"  Thresholds (median across {len(tc_p33_vals)} reference problems, ratio = run_time / timelimit):\n")
            f.write(f"    fast:   ratio <= {tc_med_p33:.4f}  (below 33rd percentile of reference TC execution times)\n")
            f.write(f"    medium: {tc_med_p33:.4f} < ratio <= {tc_med_p67:.4f}  (33rd ~ 67th percentile)\n")
            f.write(f"    slow:   ratio >  {tc_med_p67:.4f}  (above 67th percentile of reference TC execution times)\n")
            _write_module_split_block(f, all_ev_tcs_tc, "tercile")
            _write_module_split_block_valid(f, all_ev_tcs_tc, "tercile")
            _write_tier_confusion_matrix_per_module(f, all_ev_tcs_tc, "tercile")
            f.write("\n")

            f.write("============================================================\n")
            f.write(f"**Criterion: Timelimit (Fixed 1/3, 2/3 Ratio)**\n")
            f.write(f"  Divides each problem's timelimit into 3 equal intervals.\n")
            f.write(f"  fast: run_time <= timelimit/3, medium: <= 2/3, slow: > 2/3.\n")
            f.write(f"  Same threshold for all languages within a problem.\n\n")
            f.write(f"Aggregate Difficulty Distribution (timelimit-based, per unique test case):\n")
            f.write(f"  [Reference scope: all {num_ref_problems} reference problems | Evaluand scope: {len(both_ok)} both_ok problems]\n")
            ref_total = sum(agg_ref.values())
            ev_total = sum(agg_ev.values()) + agg_ev_invalid

            f.write(f"  {'Difficulty':<10} {'Reference':>12} {'%':>8}  {'Evaluand':>12} {'%':>8}  {'Delta':>8}\n")
            f.write(f"  {'-' * 62}\n")
            for d in DIFFICULTY_ORDER:
                r_cnt = agg_ref[d]
                e_cnt = agg_ev[d]
                r_pct = (r_cnt / ref_total * 100) if ref_total > 0 else 0
                e_pct = (e_cnt / ev_total * 100) if ev_total > 0 else 0
                delta = e_pct - r_pct
                f.write(f"  {d:<10} {r_cnt:>12} {r_pct:>7.1f}%  {e_cnt:>12} {e_pct:>7.1f}%  {delta:>+7.1f}%\n")
            if agg_ev_invalid > 0:
                inv_pct = (agg_ev_invalid / ev_total * 100) if ev_total > 0 else 0
                f.write(f"  {'invalid':<10} {'—':>12} {'—':>8}  {agg_ev_invalid:>12} {inv_pct:>7.1f}%  {'—':>8}\n")
            f.write(f"  {'-' * 62}\n")
            f.write(f"  {'Total':<10} {ref_total:>12}          {ev_total:>12}\n\n")

            f.write("Evaluand Metrics (over TCs with target_tier, timelimit difficulty):\n")
            _write_tpr_taa_block(f, all_ev_tcs, "timelimit", num_ref_problems)
            if lang_label != "total":
                _expected = num_ref_problems * tcs_per_tier
                for _tier, _val in [("fast", tpr_norm_tl_fast), ("medium", tpr_norm_tl_medium), ("slow", tpr_norm_tl_slow)]:
                    _h = _agg_norm_tl[_tier]["hit"]
                    _pad = " " * (6 - len(_tier))
                    f.write(f"  TPR_norm({_tier}){_pad}= {_val:.4f}  ({_h}/{_expected})  "
                            f"(scope={num_ref_problems} ref problems x {tcs_per_tier} TCs)\n")
            f.write(f"  ATS        = {ats_tl:.4f}  (reference vs evaluand tier sensitivity)\n")
            f.write(f"  Thresholds (fixed rule: ratio = run_time / timelimit; "
                    f"median boundary times across {len(tl_p33_vals)} problems):\n")
            f.write(f"    fast:   ratio <= 1/3  (run_time <= timelimit/3,  median={tl_med_p33:.3f}s)\n")
            f.write(f"    medium: 1/3 < ratio <= 2/3  (median boundary {tl_med_p33:.3f}s ~ {tl_med_p67:.3f}s)\n")
            f.write(f"    slow:   ratio >  2/3  (run_time > timelimit*2/3, median={tl_med_p67:.3f}s)\n")
            _write_module_split_block(f, all_ev_tcs, "timelimit")
            _write_module_split_block_valid(f, all_ev_tcs, "timelimit")
            _write_tier_confusion_matrix_per_module(f, all_ev_tcs, "timelimit")
            f.write("\n")

            if all_ev_tcs_rb:
                f.write("============================================================\n")
                f.write(f"**Criterion: Reference Bounds (Per-Language Actual Time Range)**\n")
                f.write(f"  Uses the actual [min, max] execution time range of reference TCs, per language.\n")
                f.write(f"  Divides that range into 3 equal intervals (not timelimit-based).\n")
                f.write(f"  Different languages get different thresholds based on their actual speed.\n\n")
                f.write(f"Aggregate Difficulty Distribution (ref-bounds, per unique test case):\n")
                f.write(f"  [Evaluand scope: {len(both_ok)} both_ok problems]\n")
                ev_rb_total = sum(agg_ev_rb.values()) + agg_ev_invalid_rb
                f.write(f"  {'Difficulty':<10} {'Evaluand':>12} {'%':>8}\n")
                f.write(f"  {'-' * 32}\n")
                for d in DIFFICULTY_ORDER:
                    e_cnt = agg_ev_rb[d]
                    e_pct = (e_cnt / ev_rb_total * 100) if ev_rb_total > 0 else 0
                    f.write(f"  {d:<10} {e_cnt:>12} {e_pct:>7.1f}%\n")
                if agg_ev_invalid_rb > 0:
                    inv_pct = (agg_ev_invalid_rb / ev_rb_total * 100) if ev_rb_total > 0 else 0
                    f.write(f"  {'invalid':<10} {agg_ev_invalid_rb:>12} {inv_pct:>7.1f}%\n")
                f.write(f"  {'-' * 32}\n")
                f.write(f"  {'Total':<10} {ev_rb_total:>12}\n\n")

                f.write("Evaluand Metrics (over TCs with target_tier, ref-bounds difficulty):\n")
                _write_tpr_taa_block(f, all_ev_tcs_rb, "ref_bounds", num_ref_problems)
                if lang_label != "total":
                    _expected = num_ref_problems * tcs_per_tier
                    for _tier, _val in [("fast", tpr_norm_rb_fast), ("medium", tpr_norm_rb_medium), ("slow", tpr_norm_rb_slow)]:
                        _h = _agg_norm_rb[_tier]["hit"]
                        _pad = " " * (6 - len(_tier))
                        f.write(f"  TPR_norm({_tier}){_pad}= {_val:.4f}  ({_h}/{_expected})  "
                                f"(scope={num_ref_problems} ref problems x {tcs_per_tier} TCs)\n")
                _write_module_split_block(f, all_ev_tcs_rb, "ref_bounds")
                _write_module_split_block_valid(f, all_ev_tcs_rb, "ref_bounds")
                _write_tier_confusion_matrix_per_module(f, all_ev_tcs_rb, "ref_bounds")
                f.write("\n")

        def _write_max_ref_time_block(fh):
            if not max_ref_time_pairs:
                return
            from statistics import mean as _mean, median as _median
            all_vals = [v for _, v in max_ref_time_pairs]
            by_lang: dict[str, list[float]] = {}
            for _l, _v in max_ref_time_pairs:
                by_lang.setdefault(_l, []).append(_v)
            def _p90(xs):
                if not xs:
                    return 0.0
                xs2 = sorted(xs)
                k = max(0, int(round(0.9 * (len(xs2) - 1))))
                return xs2[k]
            fh.write(f"  Reference Max Time (max_ref_time per problem-language pair; "
                     f"reference-only, NOT filtered by both_ok):\n")
            fh.write(f"    {'Scope':<12} {'N':>6} {'avg':>10} {'median':>10} {'min':>10} {'max':>10} {'p90':>10}\n")
            fh.write(f"    {'-' * 70}\n")
            fh.write(f"    {'overall':<12} {len(all_vals):>6} "
                     f"{_mean(all_vals):>9.4f}s {_median(all_vals):>9.4f}s "
                     f"{min(all_vals):>9.4f}s {max(all_vals):>9.4f}s {_p90(all_vals):>9.4f}s\n")
            for _l in sorted(by_lang):
                vs = by_lang[_l]
                fh.write(f"    {_l:<12} {len(vs):>6} "
                         f"{_mean(vs):>9.4f}s {_median(vs):>9.4f}s "
                         f"{min(vs):>9.4f}s {max(vs):>9.4f}s {_p90(vs):>9.4f}s\n")
            fh.write(f"    {'-' * 70}\n\n")

        if all_ev_tcs_ratio and _EMIT_RATIO_CRITERION:
            f.write("============================================================\n")
            f.write(f"**Criterion: Ratio to Max Reference Time (threshold={slow_ratio_threshold})**\n")
            f.write(f"  Measures how close a TC's run_time is to the max observed reference time per language.\n")
            f.write(f"  slow: run_time >= max_ref x {slow_ratio_threshold} "
                    f"({slow_ratio_threshold*100:.0f}% of max reference time)\n")
            f.write(f"  medium: >= max_ref x {slow_ratio_threshold/2}, "
                    f"fast: < max_ref x {slow_ratio_threshold/2}\n\n")
            _write_max_ref_time_block(f)
            f.write(f"Aggregate Difficulty Distribution (ratio-based, per unique test case):\n")
            f.write(f"  [Evaluand scope: {len(both_ok)} both_ok problems]\n")
            ev_ratio_total = sum(agg_ev_ratio.values()) + agg_ev_invalid_ratio
            f.write(f"  {'Difficulty':<10} {'Evaluand':>12} {'%':>8}\n")
            f.write(f"  {'-' * 32}\n")
            for d in DIFFICULTY_ORDER:
                e_cnt = agg_ev_ratio[d]
                e_pct = (e_cnt / ev_ratio_total * 100) if ev_ratio_total > 0 else 0
                f.write(f"  {d:<10} {e_cnt:>12} {e_pct:>7.1f}%\n")
            if agg_ev_invalid_ratio > 0:
                inv_pct = (agg_ev_invalid_ratio / ev_ratio_total * 100) if ev_ratio_total > 0 else 0
                f.write(f"  {'invalid':<10} {agg_ev_invalid_ratio:>12} {inv_pct:>7.1f}%\n")
            f.write(f"  {'-' * 32}\n")
            f.write(f"  {'Total':<10} {ev_ratio_total:>12}\n\n")

            f.write("Evaluand Metrics (over TCs with target_tier, ratio-based difficulty):\n")
            _write_tpr_taa_block(f, all_ev_tcs_ratio, "ratio_based", num_ref_problems)
            if lang_label != "total":
                _expected = num_ref_problems * tcs_per_tier
                for _tier, _val in [("fast", tpr_norm_ratio_fast), ("medium", tpr_norm_ratio_medium), ("slow", tpr_norm_ratio_slow)]:
                    _h = _agg_norm_ratio[_tier]["hit"]
                    _pad = " " * (6 - len(_tier))
                    f.write(f"  TPR_norm({_tier}){_pad}= {_val:.4f}  ({_h}/{_expected})  "
                            f"(scope={num_ref_problems} ref problems x {tcs_per_tier} TCs)\n")
            _write_module_split_block(f, all_ev_tcs_ratio, "ratio_based")
            _write_module_split_block_valid(f, all_ev_tcs_ratio, "ratio_based")
            _write_tier_confusion_matrix_per_module(f, all_ev_tcs_ratio, "ratio_based")
            f.write("\n")

        if all_ev_tcs_binary:
            f.write("============================================================\n")
            f.write(f"**Criterion: Binary Reference Exceeded**\n")
            f.write(f"  Did the generated TC make the solution run slower than ANY reference TC?\n")
            f.write(f"  exceeded: evaluand_run_time > max(reference_run_times) for that language.\n\n")
            _write_max_ref_time_block(f)
            total_binary = agg_ev_exceeded + agg_ev_not_exceeded + agg_ev_invalid_binary
            valid_binary = agg_ev_exceeded + agg_ev_not_exceeded
            f.write(f"  [Evaluand scope: {len(both_ok)} both_ok problems]\n")
            f.write(f"  {'Category':<15} {'Count':>8} {'%':>8}  {'% (valid only)':>15}\n")
            f.write(f"  {'-' * 50}\n")
            exc_pct = (agg_ev_exceeded / total_binary * 100) if total_binary > 0 else 0
            exc_vpct = (agg_ev_exceeded / valid_binary * 100) if valid_binary > 0 else 0
            f.write(f"  {'exceeded':<15} {agg_ev_exceeded:>8} {exc_pct:>7.1f}%  {exc_vpct:>14.1f}%\n")
            ne_pct = (agg_ev_not_exceeded / total_binary * 100) if total_binary > 0 else 0
            ne_vpct = (agg_ev_not_exceeded / valid_binary * 100) if valid_binary > 0 else 0
            f.write(f"  {'not_exceeded':<15} {agg_ev_not_exceeded:>8} {ne_pct:>7.1f}%  {ne_vpct:>14.1f}%\n")
            if agg_ev_invalid_binary > 0:
                inv_pct = (agg_ev_invalid_binary / total_binary * 100) if total_binary > 0 else 0
                f.write(f"  {'invalid':<15} {agg_ev_invalid_binary:>8} {inv_pct:>7.1f}%\n")
            f.write(f"  {'-' * 50}\n")
            f.write(f"  {'Total':15} {total_binary:>8}\n\n")

            tcs_m1 = [tc for tc in all_ev_tcs_binary if tc.get("module") == "M1"]
            tcs_m2 = [tc for tc in all_ev_tcs_binary if tc.get("module") == "M2"]
            if tcs_m1 or tcs_m2:
                f.write(f"  --- Per-Module Breakdown (binary_exceeded) ---\n")
                f.write(f"  {'':20} {'All':>16} {'M1(boundary)':>16} {'M2(algorithmic)':>16}\n")
                f.write(f"  {'-' * 70}\n")
                cells = []
                for _label, subset in [("All", all_ev_tcs_binary), ("M1(boundary)", tcs_m1), ("M2(algorithmic)", tcs_m2)]:
                    exc = sum(1 for tc in subset if tc.get("exceeded") is True)
                    tot = sum(1 for tc in subset if tc.get("exceeded") is not None)
                    pct = exc / tot * 100 if tot > 0 else 0
                    cells.append(f"{exc}/{tot} ({pct:.1f}%)")
                f.write(f"  {'exceeded':20} {cells[0]:>16} {cells[1]:>16} {cells[2]:>16}\n")
                f.write(f"  {'-' * 70}\n\n")

            tcs_by_tier = {
                "fast":   [tc for tc in all_ev_tcs_binary if tc.get("target_tier") == "fast"],
                "medium": [tc for tc in all_ev_tcs_binary if tc.get("target_tier") == "medium"],
                "slow":   [tc for tc in all_ev_tcs_binary if tc.get("target_tier") == "slow"],
            }
            if any(tcs_by_tier.values()):
                f.write(f"  --- Per-Tier Breakdown (binary_exceeded, by intended target_tier) ---\n")
                f.write(f"  {'':20} {'fast':>18} {'medium':>18} {'slow':>18} {'All':>18}\n")
                f.write(f"  {'-' * 96}\n")

                def _row(label: str, value_fn):
                    cells = []
                    for subset in (tcs_by_tier["fast"], tcs_by_tier["medium"],
                                   tcs_by_tier["slow"], all_ev_tcs_binary):
                        cells.append(value_fn(subset))
                    f.write(f"  {label:20} {cells[0]:>18} {cells[1]:>18} {cells[2]:>18} {cells[3]:>18}\n")

                def _v_exceeded(subset):
                    exc = sum(1 for tc in subset if tc.get("exceeded") is True)
                    tot = sum(1 for tc in subset if tc.get("exceeded") is not None)
                    pct = exc / tot * 100 if tot > 0 else 0
                    return f"{exc}/{tot} ({pct:.1f}%)"
                _row("exceeded", _v_exceeded)

                def _v_not_exceeded(subset):
                    nex = sum(1 for tc in subset if tc.get("exceeded") is False)
                    tot = sum(1 for tc in subset if tc.get("exceeded") is not None)
                    pct = nex / tot * 100 if tot > 0 else 0
                    return f"{nex}/{tot} ({pct:.1f}%)"
                _row("not_exceeded", _v_not_exceeded)

                def _v_invalid(subset):
                    inv = sum(1 for tc in subset if tc.get("exceeded") is None)
                    tot = len(subset)
                    pct = inv / tot * 100 if tot > 0 else 0
                    return f"{inv}/{tot} ({pct:.1f}%)"
                _row("invalid", _v_invalid)

                f.write(f"  {'-' * 96}\n")
                def _v_total(subset):
                    return f"{len(subset)}"
                _row("Total", _v_total)
                f.write(f"  {'-' * 96}\n\n")

                _tcs_per_tier_safe = tcs_per_tier if tcs_per_tier else max(1, tcs_per_problem // 3)
                _norm_denom_per_tier = num_ref_problems * _tcs_per_tier_safe
                _norm_denom_total = _norm_denom_per_tier * 3
                f.write(f"  --- Per-Tier Breakdown (TPR_norm style; "
                        f"denom = num_ref_problems × tcs_per_tier = "
                        f"{num_ref_problems} × {_tcs_per_tier_safe} = {_norm_denom_per_tier}) ---\n")
                f.write(f"  {'':36} {'fast':>18} {'medium':>18} {'slow':>18} {'All':>18}\n")
                f.write(f"  {'-' * 112}\n")

                def _row_norm(label: str, value_fn, denom_per_tier: int, denom_total: int):
                    cells = []
                    for subset, denom in [
                        (tcs_by_tier["fast"], denom_per_tier),
                        (tcs_by_tier["medium"], denom_per_tier),
                        (tcs_by_tier["slow"], denom_per_tier),
                        (all_ev_tcs_binary, denom_total),
                    ]:
                        cells.append(value_fn(subset, denom))
                    f.write(f"  {label:36} {cells[0]:>18} {cells[1]:>18} {cells[2]:>18} {cells[3]:>18}\n")

                def _vn_exceeded(subset, denom):
                    exc = sum(1 for tc in subset if tc.get("exceeded") is True)
                    pct = exc / denom * 100 if denom > 0 else 0
                    return f"{exc}/{denom} ({pct:.1f}%)"
                _row_norm("exceeded_norm", _vn_exceeded, _norm_denom_per_tier, _norm_denom_total)

                _tle_by_tier_local = {
                    "fast":   [tc for tc in all_ev_tcs_tle if tc.get("target_tier") == "fast"],
                    "medium": [tc for tc in all_ev_tcs_tle if tc.get("target_tier") == "medium"],
                    "slow":   [tc for tc in all_ev_tcs_tle if tc.get("target_tier") == "slow"],
                }

                def _vn_exceeded_with_tle(subset, denom):
                    if subset is all_ev_tcs_binary:
                        tle_subset = all_ev_tcs_tle
                    else:
                        tier_set = {tc.get("target_tier") for tc in subset}
                        tier_set.discard(None)
                        tle_subset = [tc for tc in all_ev_tcs_tle
                                      if tc.get("target_tier") in tier_set]
                    exc = sum(1 for tc in subset if tc.get("exceeded") is True)
                    cmp_ = sum(1 for tc in tle_subset if tc.get("compliant") is True)
                    hits = exc + cmp_
                    pct = hits / denom * 100 if denom > 0 else 0
                    return f"{hits}/{denom} ({pct:.1f}%)"
                _row_norm("exceeded_norm_with_TLE_compliant",
                          _vn_exceeded_with_tle,
                          _norm_denom_per_tier, _norm_denom_total)

                def _vn_not_exceeded(subset, denom):
                    nex = sum(1 for tc in subset if tc.get("exceeded") is False)
                    pct = nex / denom * 100 if denom > 0 else 0
                    return f"{nex}/{denom} ({pct:.1f}%)"
                _row_norm("not_exceeded_norm", _vn_not_exceeded, _norm_denom_per_tier, _norm_denom_total)

                f.write(f"  {'-' * 112}\n")

                def _emit_constraint_block(scope_label: str, scope_blurb: str,
                                            cc_key: str, viol_key: str):
                    f.write(f"\n  --- Per-Tier Breakdown with Constraints check ({scope_label}; "
                            f"TPR_norm style; "
                            f"denom = num_ref_problems × tcs_per_tier = "
                            f"{num_ref_problems} × {_tcs_per_tier_safe} = {_norm_denom_per_tier}) ---\n")
                    f.write(f"  Constraint source: LLM-extracted via generate_constraints.py.\n")
                    f.write(f"  {scope_blurb}\n")
                    f.write(f"  {'':52} {'fast':>16} {'medium':>16} {'slow':>16} {'All':>16}\n")
                    f.write(f"  {'-' * 120}\n")

                    def _row_norm_cc(label: str, value_fn, dpt: int, dt: int):
                        cells = []
                        for subset, denom in [
                            (tcs_by_tier["fast"], dpt),
                            (tcs_by_tier["medium"], dpt),
                            (tcs_by_tier["slow"], dpt),
                            (all_ev_tcs_binary, dt),
                        ]:
                            cells.append(value_fn(subset, denom))
                        f.write(f"  {label:52} {cells[0]:>16} {cells[1]:>16} {cells[2]:>16} {cells[3]:>16}\n")

                    def _vn_exceeded_cc(subset, denom):
                        cnt = sum(1 for tc in subset
                                  if tc.get("exceeded") is True
                                  and tc.get(cc_key) is True)
                        pct = cnt / denom * 100 if denom > 0 else 0
                        return f"{cnt}/{denom} ({pct:.1f}%)"
                    _row_norm_cc("exceeded_norm_constraint_valid",
                                 _vn_exceeded_cc,
                                 _norm_denom_per_tier, _norm_denom_total)

                    def _vn_exceeded_cc_with_tle(subset, denom):
                        if subset is all_ev_tcs_binary:
                            tle_subset = all_ev_tcs_tle
                        else:
                            tier_set = {tc.get("target_tier") for tc in subset}
                            tier_set.discard(None)
                            tle_subset = [tc for tc in all_ev_tcs_tle
                                          if tc.get("target_tier") in tier_set]
                        exc = sum(1 for tc in subset
                                  if tc.get("exceeded") is True
                                  and tc.get(cc_key) is True)
                        cmp_ = sum(1 for tc in tle_subset
                                   if tc.get("compliant") is True)
                        hits = exc + cmp_
                        pct = hits / denom * 100 if denom > 0 else 0
                        return f"{hits}/{denom} ({pct:.1f}%)"
                    _row_norm_cc("exceeded_norm_constraint_valid_with_TLE_compliant",
                                 _vn_exceeded_cc_with_tle,
                                 _norm_denom_per_tier, _norm_denom_total)

                    def _vn_not_exceeded_cc(subset, denom):
                        cnt = sum(1 for tc in subset
                                  if tc.get("exceeded") is False
                                  and tc.get(cc_key) is True)
                        pct = cnt / denom * 100 if denom > 0 else 0
                        return f"{cnt}/{denom} ({pct:.1f}%)"
                    _row_norm_cc("not_exceeded_norm_constraint_valid",
                                 _vn_not_exceeded_cc,
                                 _norm_denom_per_tier, _norm_denom_total)

                    def _vn_violated(subset, denom):
                        cnt = sum(1 for tc in subset
                                  if tc.get(cc_key) is False)
                        pct = cnt / denom * 100 if denom > 0 else 0
                        return f"{cnt}/{denom} ({pct:.1f}%)"
                    _row_norm_cc("constraint_violated",
                                 _vn_violated,
                                 _norm_denom_per_tier, _norm_denom_total)

                    def _vn_unknown(subset, denom):
                        cnt = sum(1 for tc in subset
                                  if tc.get(cc_key) is None)
                        pct = cnt / denom * 100 if denom > 0 else 0
                        return f"{cnt}/{denom} ({pct:.1f}%)"
                    _row_norm_cc("compliance_unknown",
                                 _vn_unknown,
                                 _norm_denom_per_tier, _norm_denom_total)

                    f.write(f"  {'-' * 120}\n")

                _has_compliance_signal = any(
                    tc.get("constraint_compliant") is not None for tc in all_ev_tcs_binary)
                if _has_compliance_signal:
                    _emit_constraint_block(
                        scope_label="All types",
                        scope_blurb=("A TC is counted only when its stdin parses against "
                                     "the parsed_structures schema AND respects every "
                                     "LLM-extracted bound (range/length/product/sum/"
                                     "sum_over_tc/distinct/charset/power/other)."),
                        cc_key="constraint_compliant",
                        viol_key="constraint_violations",
                    )

                _has_boundary_signal = any(
                    tc.get("constraint_compliant_boundary") is not None for tc in all_ev_tcs_binary)
                if _has_boundary_signal:
                    _emit_constraint_block(
                        scope_label="Boundary only",
                        scope_blurb=("Same TC pool, but compliance is checked ONLY against "
                                     "boundary-style constraints (range/length/product/sum). "
                                     "Non-boundary constraints (distinct/charset/sum_over_tc/"
                                     "power/other) are ignored — useful when boundary "
                                     "violations are the dominant source of false adversarial "
                                     "credit."),
                        cc_key="constraint_compliant_boundary",
                        viol_key="constraint_violations_boundary",
                    )
            f.write("\n")

        if all_ev_tcs_tle and _EMIT_TLE_CRITERION:
            f.write("============================================================\n")
            f.write(f"**Criterion: TLE on Constraint-Compliant TC**\n")
            f.write(f"  A TC is counted only if (a) at least one language verdict was "
                    f"'timelimit' AND (b) its input boundary respects every "
                    f"LLM-extracted constraint for the problem.\n")
            f.write(f"  Constraint source: LLM-extracted via generate_constraints.py "
                    f"(set --llm_constraints_path).\n\n")

            total_tle = agg_ev_tle_compliant + agg_ev_tle_violating
            f.write(f"  [Evaluand scope: {len(both_ok)} both_ok problems]\n")
            f.write(f"  {'Category':<20} {'Count':>8} {'%':>8}\n")
            f.write(f"  {'-' * 40}\n")
            cmp_pct = (agg_ev_tle_compliant / total_tle * 100) if total_tle > 0 else 0
            vio_pct = (agg_ev_tle_violating / total_tle * 100) if total_tle > 0 else 0
            f.write(f"  {'tle_compliant':<20} {agg_ev_tle_compliant:>8} {cmp_pct:>7.1f}%\n")
            f.write(f"  {'tle_violating':<20} {agg_ev_tle_violating:>8} {vio_pct:>7.1f}%\n")
            f.write(f"  {'-' * 40}\n")
            f.write(f"  {'Total TLE TCs':<20} {total_tle:>8}\n\n")

            tle_by_tier = {
                "fast":   [tc for tc in all_ev_tcs_tle if tc.get("target_tier") == "fast"],
                "medium": [tc for tc in all_ev_tcs_tle if tc.get("target_tier") == "medium"],
                "slow":   [tc for tc in all_ev_tcs_tle if tc.get("target_tier") == "slow"],
            }
            if any(tle_by_tier.values()):
                _denom_per_tier = num_ref_problems * (tcs_per_tier
                                                      if tcs_per_tier
                                                      else max(1, tcs_per_problem // 3))
                _denom_total = _denom_per_tier * 3
                f.write(f"  --- Per-Tier Breakdown (TPR_norm style; "
                        f"denom = num_ref_problems × tcs_per_tier = "
                        f"{num_ref_problems} × {_denom_per_tier // num_ref_problems if num_ref_problems else 0} "
                        f"= {_denom_per_tier}) ---\n")
                f.write(f"  {'':22} {'fast':>16} {'medium':>16} {'slow':>16} {'All':>16}\n")
                f.write(f"  {'-' * 90}\n")

                def _row_tle(label: str, value_fn, dpt: int, dt: int):
                    cells = []
                    for subset, denom in [
                        (tle_by_tier["fast"], dpt),
                        (tle_by_tier["medium"], dpt),
                        (tle_by_tier["slow"], dpt),
                        (all_ev_tcs_tle, dt),
                    ]:
                        cells.append(value_fn(subset, denom))
                    f.write(f"  {label:22} {cells[0]:>16} {cells[1]:>16} {cells[2]:>16} {cells[3]:>16}\n")

                def _vn_tle_compliant(subset, denom):
                    cnt = sum(1 for tc in subset if tc.get("compliant") is True)
                    pct = cnt / denom * 100 if denom > 0 else 0
                    return f"{cnt}/{denom} ({pct:.1f}%)"
                _row_tle("TLE_compliant_norm", _vn_tle_compliant, _denom_per_tier, _denom_total)

                def _vn_tle_violating(subset, denom):
                    cnt = sum(1 for tc in subset if tc.get("compliant") is False)
                    pct = cnt / denom * 100 if denom > 0 else 0
                    return f"{cnt}/{denom} ({pct:.1f}%)"
                _row_tle("TLE_violating_norm", _vn_tle_violating, _denom_per_tier, _denom_total)
                f.write(f"  {'-' * 90}\n")
            f.write("\n")

        verdict_total = sum(verdicts.get(v, 0) for v in ["SLOWER_HEAVIER", "FASTER_HEAVIER", "MORE_POLARIZED", "SIMILAR"])
        no_verdict_count = len(both_ok) - verdict_total
        f.write("============================================================\n")
        f.write(f"**Verdict Distribution**\n")
        f.write(f"Verdict Distribution (timelimit-based, per-problem shift; scope = both_ok = {len(both_ok)}):\n")
        for v in ["SLOWER_HEAVIER", "FASTER_HEAVIER", "MORE_POLARIZED", "SIMILAR"]:
            f.write(f"  {v}: {verdicts.get(v, 0)} problems\n")
        if no_verdict_count > 0:
            f.write(f"  No verdict (insufficient evaluand valid TCs — all WA or no runs): {no_verdict_count} problems\n")
        f.write(f"  Sum: {verdict_total + no_verdict_count} = both_ok\n")
        f.write(f"\n")

    log.info("Summary saved: %s", summary_path)

    detail_path = summary_path.parent / "per_problem_detail.txt"
    with open(detail_path, "w") as f:
        f.write(f"Per-Problem Details\n")
        f.write(f"{'=' * 80}\n")
        f.write(f"Source:     {summary_path.parent.name}\n")
        f.write(f"{'=' * 80}\n\n")
        for c in comparisons:
            f.write(f"  [{c['index']}] {c['name']}\n")
            f.write(f"    Timelimit: {c.get('timelimit', 'N/A')}s\n")

            for src in ["reference", "evaluand"]:
                info = c.get(src, {})
                if info.get("status") != "ok":
                    f.write(f"    {src.upper()}: no data\n")
                    continue
                counts = info["timelimit_based"].get("counts_per_tc") or info["timelimit_based"]["counts"]
                total = sum(counts.get(d, 0) for d in DIFFICULTY_ORDER)
                inv = counts.get("invalid", 0)
                dist = ", ".join(f"{d}={counts.get(d, 0)}" for d in DIFFICULTY_ORDER)
                if inv > 0:
                    dist += f", invalid={inv}"
                    total += inv
                f.write(f"    {src.upper()}: {total} TCs — {dist}\n")
                rs = info.get("ratio_stats", {})
                f.write(f"      ratio: min={rs.get('min', 'N/A')}, max={rs.get('max', 'N/A')}, "
                        f"mean={rs.get('mean', 'N/A')}, median={rs.get('median', 'N/A')}\n")

            ev_info = c.get("evaluand", {})
            if ev_info.get("status") == "ok":
                ev_tcs = ev_info.get("testcases", [])
                tcs_with_tier = [tc for tc in ev_tcs if "target_tier" in tc]
                if tcs_with_tier:
                    correct = sum(1 for tc in tcs_with_tier if tc.get("target_tier") == tc.get("difficulty"))
                    f.write(f"    Target tier alignment: {correct}/{len(tcs_with_tier)} "
                            f"({correct * 100 / len(tcs_with_tier):.1f}%)\n")
                    for tier in ("fast", "medium", "slow"):
                        tier_tcs = [tc for tc in tcs_with_tier if tc.get("target_tier") == tier]
                        if tier_tcs:
                            hit = sum(1 for tc in tier_tcs if tc.get("difficulty") == tier)
                            valid = sum(1 for tc in tier_tcs
                                        if tc.get("tc_verdict") not in ("wrong-answer", "timelimit")
                                        and tc.get("difficulty") != "invalid")
                            f.write(f"      {tier}: {len(tier_tcs)} intended, "
                                    f"{valid} valid, {hit} on-tier\n")

            shift = c.get("shift_analysis", {})
            if shift.get("status") == "ok":
                f.write(f"    VERDICT: {shift['verdict']} "
                        f"(slow {shift['slow_increase_pct']:+.1f}%)\n")
            per_lang = c.get("evaluand_per_language_tiers")
            if per_lang:
                f.write(f"    Evaluand per-language tiers (by reference thresholds):\n")
                for lang, counts in sorted(per_lang.items()):
                    f.write(f"      {lang}: fast={counts['fast']}, medium={counts['medium']}, slow={counts['slow']}\n")
            f.write(f"\n")

    log.info("Per-problem details saved: %s", detail_path)

LANG_VIEWS = [
    ("total",  None),
    ("cpp",    frozenset({"cpp"})),
    ("python", frozenset({"python3"})),
    ("java",   frozenset({"java"})),
]

def filter_prob_by_lang(prob: dict, lang_ids: frozenset) -> dict:
    lower_ids = {l.lower() for l in lang_ids}
    filtered_sols = [
        sol for sol in prob.get("solutions", [])
        if (sol.get("language_id") or sol.get("language", "")).lower() in lower_ids
    ]
    result = dict(prob)
    result["solutions"] = filtered_sols
    return result

_LEF_TRACER_HEADER_PARTS = [
    "import sys as _lef_sys",
    "import json as _lef_json",
    "",
    "_lef_counts = {}",
    "_lef_outfile = None  # filled by _build_lef_script",
    "_lef_offset = 0     # filled by _build_lef_script",
    "",
    "def _lef_tracer(frame, event, arg):",
    "    if event == 'line' and frame.f_code.co_filename == __file__:",
    "        n = frame.f_lineno - _lef_offset",
    "        if n > 0:",
    "            _lef_counts[n] = _lef_counts.get(n, 0) + 1",
    "    return _lef_tracer",
    "",
    "_lef_sys.settrace(_lef_tracer)",
]
_LEF_HEADER_LINE_COUNT = len(_LEF_TRACER_HEADER_PARTS)

_LEF_TRACER_FOOTER_PARTS = [
    "",
    "_lef_sys.settrace(None)",
    "with open(_lef_outfile, 'w') as _lef_f:",
    "    _lef_json.dump(_lef_counts, _lef_f)",
]

def _build_lef_script(source_code: str, out_file: str) -> str:
    parts = list(_LEF_TRACER_HEADER_PARTS)
    parts[4] = f"_lef_outfile = {out_file!r}"
    parts[5] = f"_lef_offset = {_LEF_HEADER_LINE_COUNT}"
    header = "\n".join(parts) + "\n"
    footer = "\n".join(_LEF_TRACER_FOOTER_PARTS) + "\n"
    return header + source_code + footer

def count_executable_lines(source_code: str) -> int:
    try:
        tree = _ast_module.parse(source_code)
        lines = {
            node.lineno
            for node in _ast_module.walk(tree)
            if isinstance(node, _ast_module.stmt) and hasattr(node, "lineno")
        }
        return max(len(lines), 1)
    except SyntaxError:
        non_blank = [
            ln for ln in source_code.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        return max(len(non_blank), 1)

def profile_python3_single_tc(source_code: str, stdin_text: str,
                               timeout: float = 5.0) -> Optional[dict]:
    tmpdir = tempfile.mkdtemp(prefix="lef_")
    try:
        out_file = os.path.join(tmpdir, "lef_counts.json")
        script = _build_lef_script(source_code, out_file)
        script_file = os.path.join(tmpdir, "solution.py")
        with open(script_file, "w", encoding="utf-8") as f:
            f.write(script)
        subprocess.run(
            [sys.executable, script_file],
            input=stdin_text,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        if os.path.exists(out_file):
            with open(out_file) as f:
                raw = json.load(f)
            return {int(k): v for k, v in raw.items()}
    except (subprocess.TimeoutExpired, Exception):
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return None

def load_selected_solutions_for_lef(path: Path) -> dict:
    result = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "solutions" not in obj or obj.get("type") == "metadata":
                continue
            name = obj.get("name", "")
            if not name:
                continue
            result[name] = {}
            for lang, sols in obj["solutions"].items():
                if not isinstance(sols, list):
                    continue
                result[name][lang] = [
                    {"sol_idx": s["sol_idx"], "max_run_time": s.get("max_run_time", 0.0)}
                    for s in sols
                    if s.get("verdict") == "AC"
                ]
    return result

def select_lef_solutions(lang_solutions: list, seed: int = 42,
                          n_random: int = 3) -> list:
    if not lang_solutions:
        return []
    sorted_sols = sorted(lang_solutions, key=lambda s: s["max_run_time"])
    selected = []
    used = set()

    best = sorted_sols[0]
    selected.append({"role": "best", "sol_idx": best["sol_idx"],
                     "max_run_time": best["max_run_time"]})
    used.add(best["sol_idx"])

    slowest = sorted_sols[-1]
    if slowest["sol_idx"] not in used:
        selected.append({"role": "slowest", "sol_idx": slowest["sol_idx"],
                         "max_run_time": slowest["max_run_time"]})
        used.add(slowest["sol_idx"])

    remaining = [s for s in lang_solutions if s["sol_idx"] not in used]
    rng = random.Random(seed)
    sampled = rng.sample(remaining, min(n_random, len(remaining)))
    for i, s in enumerate(sampled):
        selected.append({"role": f"random_{i}", "sol_idx": s["sol_idx"],
                         "max_run_time": s["max_run_time"]})

    return selected

def _load_compact_m1_features(parsed_structures_dir: str, split: str) -> dict:
    _sub_splits = split.split('_') if '_' in split and split not in ('train', 'valid', 'test') else [split]
    if len(_sub_splits) == 1:
        try:
            path = Path(parsed_structures_dir) / f"{_sub_splits[0]}.json"
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {int(k): (v.get("constraints_parsed", []), v.get("structure", {}))
                    for k, v in data.items()}
        except Exception as e:
            log.warning("[compact M1] Failed to load parsed_structures (%s/%s.json): %s",
                        parsed_structures_dir, _sub_splits[0], e)
            return {}
    merged = {}
    for _sub in _sub_splits:
        try:
            path = Path(parsed_structures_dir) / f"{_sub}.json"
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            merged.update({int(k): (v.get("constraints_parsed", []), v.get("structure", {}))
                           for k, v in data.items()})
        except Exception as e:
            log.warning("[compact M1] Failed to load parsed_structures (%s/%s.json): %s",
                        parsed_structures_dir, _sub, e)
    return merged

def _expand_compact_m1_stdin(tc_entry: dict, features: dict, prob_idx: Optional[int]) -> Optional[str]:
    stdin_val = tc_entry.get("stdin") if tc_entry.get("stdin") is not None else tc_entry.get("input")
    if stdin_val is not None:
        return stdin_val

    generator_code = tc_entry.get("_generator_code")
    if generator_code is None:
        testcase = tc_entry.get("testcase", {})
        generator_code = testcase.get("_generator_code")
    if generator_code is not None:
        if execute_generator is None:
            log.warning("[v4 generator] generator_executor not available; skipping TC (idx=%s)", prob_idx)
            return None
        try:
            ok, result = execute_generator(generator_code, timeout=30)
            if ok:
                return result
            else:
                log.warning("[v4 generator] execution failed (idx=%s): %s", prob_idx, result[:200])
                return None
        except Exception as e:
            log.warning("[v4 generator] execution error (idx=%s): %s", prob_idx, e)
            return None

    if tc_entry.get("method") != "boundary_slow_compact":
        return None
    if prob_idx is None or prob_idx not in features:
        return None
    try:
        from utils.slow_testcase_generator import expand_compact_m1_tc
        constraints, structure = features[prob_idx]
        return expand_compact_m1_tc(tc_entry, constraints, structure)
    except Exception as e:
        log.warning("[compact M1] expand failed (idx=%s): %s", prob_idx, e)
        return None

def load_tc_inputs_from_inputs_dir(inputs_dir: Path,
                                    compact_m1_features: dict) -> dict:
    result: dict = {}
    for tier in ("fast", "medium", "slow"):
        tier_dir = inputs_dir / tier
        if not tier_dir.exists():
            continue
        for fpath in sorted(tier_dir.iterdir()):
            if fpath.suffix != ".json":
                continue
            try:
                with open(fpath, encoding="utf-8") as f:
                    tc = json.load(f)
            except Exception:
                continue
            name = tc.get("name", "")
            if not name:
                continue
            prob_idx = tc.get("index")
            stdin_val = _expand_compact_m1_stdin(tc, compact_m1_features, prob_idx)
            if not stdin_val:
                continue
            if name not in result:
                result[name] = {"fast": [], "medium": [], "slow": []}
            result[name][tier].append(stdin_val)
    for name in result:
        result[name] = {t: v for t, v in result[name].items() if v}
    return result

def load_tc_inputs_from_generator(gen_path: Path, problem_name: str) -> list:
    try:
        with open(gen_path, encoding="utf-8") as f:
            data = json.load(f)
        for prob in data:
            if not isinstance(prob, dict):
                continue
            if prob.get("name") != problem_name:
                continue
            if "stdin_texts" in prob and prob["stdin_texts"]:
                return [t for t in prob["stdin_texts"] if t]
            if "test_cases" in prob:
                tcs = prob["test_cases"]
                if isinstance(tcs, dict):
                    inputs = []
                    for tier in ("fast", "medium", "slow"):
                        for tc in tcs.get(tier, []):
                            inp = tc.get("input", "")
                            if not inp and tc.get("generator_code") and execute_generator:
                                code = tc.get("code", "")
                                if code:
                                    ok, result = execute_generator(code, timeout=30)
                                    if ok:
                                        inp = result
                            if inp:
                                inputs.append(inp)
                    return inputs
                if isinstance(tcs, list):
                    return [tc.get("input", "") for tc in tcs if tc.get("input")]
    except Exception as e:
        log.warning("[LEF] Failed to load TC inputs from %s for '%s': %s",
                    gen_path, problem_name, e)
    return []

def load_hf_source_codes(hf_cache_dir: str, split: str,
                          problem_sol_map: dict) -> dict:
    try:
        from datasets import load_dataset  # type: ignore
        _sub_splits = split.split('_') if '_' in split and split not in ('train', 'valid', 'test') else [split]
        from datasets import concatenate_datasets  # type: ignore
        _datasets = []
        for _sub in _sub_splits:
            _datasets.append(load_dataset("deepmind/code_contests", split=_sub,
                                          cache_dir=hf_cache_dir))
        ds = concatenate_datasets(_datasets) if len(_datasets) > 1 else _datasets[0]
    except Exception as e:
        log.error("[LEF] Failed to load HF dataset: %s", e)
        return {}

    result = {}
    found = 0
    for prob in ds:
        name = prob.get("name", "")
        if name not in problem_sol_map:
            continue
        sol_data = prob.get("solutions", {})
        sol_texts = sol_data.get("solution", [])
        codes = {}
        for sol_idx in problem_sol_map[name]:
            if sol_idx < len(sol_texts):
                codes[sol_idx] = sol_texts[sol_idx]
        if codes:
            result[name] = codes
        found += 1
        if found >= len(problem_sol_map):
            break

    log.info("[LEF] Loaded source codes for %d / %d problems",
             len(result), len(problem_sol_map))
    return result

def compute_lef_for_problem(problem_name: str, lef_sols: list,
                             source_codes: dict,
                             tc_inputs,
                             timeout: float = 5.0,
                             max_workers: int = 8) -> dict:
    _TIER_ORDER = ["fast", "medium", "slow"]
    if isinstance(tc_inputs, dict):
        flat_inputs = []
        tier_slices: dict = {}
        offset = 0
        for tier in _TIER_ORDER:
            tcs = tc_inputs.get(tier, [])
            tier_slices[tier] = (offset, offset + len(tcs))
            flat_inputs.extend(tcs)
            offset += len(tcs)
    else:
        flat_inputs = list(tc_inputs)
        tier_slices = None

    result = {}
    if not flat_inputs:
        return result

    runnable = []
    for sol_info in lef_sols:
        sol_idx = sol_info["sol_idx"]
        role = sol_info["role"]
        source = source_codes.get(sol_idx)
        if not source:
            result[role] = {
                "sol_idx": sol_idx,
                "max_run_time": sol_info["max_run_time"],
                "average_lef_score": None,
                "error": "source_code_not_found",
            }
        else:
            runnable.append((role, sol_info, source))

    if not runnable:
        return result

    n_exec_by_role = {}
    future_to_key = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for role, sol_info, source in runnable:
            n_exec_by_role[role] = count_executable_lines(source)
            for tc_idx, stdin_text in enumerate(flat_inputs):
                fut = ex.submit(profile_python3_single_tc, source, stdin_text, timeout)
                future_to_key[fut] = (role, tc_idx)

        counts_by_role: dict = {role: [None] * len(flat_inputs) for role, _, _ in runnable}
        for fut in as_completed(future_to_key):
            role, tc_idx = future_to_key[fut]
            counts = fut.result()
            if counts is not None:
                counts_by_role[role][tc_idx] = sum(counts.values())

    for role, sol_info, _ in runnable:
        raw = counts_by_role[role]

        if tier_slices:
            lef_per_tc = {}
            all_valid = []
            for tier in _TIER_ORDER:
                start, end = tier_slices[tier]
                tier_vals = [raw[i] for i in range(start, end)]
                lef_per_tc[tier] = tier_vals
                all_valid.extend(v for v in tier_vals if v is not None)
        else:
            lef_per_tc = raw
            all_valid = [v for v in raw if v is not None]

        avg_score = sum(all_valid) / len(all_valid) if all_valid else None
        result[role] = {
            "sol_idx": sol_info["sol_idx"],
            "max_run_time": sol_info["max_run_time"],
            "n_exec_lines": n_exec_by_role[role],
            "lef_per_tc": lef_per_tc,
            "average_lef_score": round(avg_score, 4) if avg_score is not None else None,
            "n_tc_success": len(all_valid),
            "n_tc_total": len(flat_inputs),
        }

    return result

def write_lef_summary(lef_by_problem: dict, output_dir: Path,
                       counts: dict = None, top_n: int = 5) -> None:
    def _get_score(res: dict):
        v = res.get("average_lef_score")
        if v is None:
            v = res.get("lef_score")
        return v

    role_scores: dict = defaultdict(list)
    for prob_result in lef_by_problem.values():
        for role, res in prob_result.items():
            s = _get_score(res)
            if s is not None:
                role_scores[role].append(s)

    n_lef = len(lef_by_problem)
    lines = [
        "=" * 60,
        "Line Execution Frequency (LEF) Summary",
        "=" * 60,
        "",
    ]

    if counts:
        n_ref   = counts.get("n_ref_total", "?")
        n_py    = counts.get("n_with_python", "?")
        n_tc    = counts.get("n_with_tc", "?")
        n_comp  = counts.get("n_lef_computed", n_lef)
        lines += [
            "Coverage breakdown:",
            f"  Total problems in reference (selected_solutions): {n_ref}",
            f"  Problems with Python3 AC solutions:               {n_py}"
            + (f"  (of {n_ref})" if isinstance(n_ref, int) and isinstance(n_py, int) else ""),
            f"  Of those, with valid generated TC inputs:         {n_tc}"
            + (f"  (of {n_py})" if isinstance(n_py, int) and isinstance(n_tc, int) else ""),
            f"  Problems with LEF data (computed):                {n_comp}",
            "",
        ]

    ROLE_DESC = {
        "best":     "fastest solution on reference TCs (lowest max_run_time)",
        "slowest":  "slowest solution on reference TCs (highest max_run_time)",
    }

    lines += [
        "Metric: LEF = total line execution count per TC run (Python3 solutions only)",
        "        best/slowest/random selected by max_run_time on reference TCs",
        "",
        "Valid TC results per role  (problems where average_lef_score is non-null / total):",
        "  A problem is counted as valid if at least 1 out of 15 generated TCs",
        "  (5 fast + 5 medium + 5 slow) ran successfully on the selected solution.",
        "  null = all TCs failed for that solution (crash or wrong input format).",
        "  average_lef_score is computed over non-null TCs only.",
        "",
    ]
    role_order = ["best", "slowest"] + [f"random_{i}" for i in range(10)]
    for role in role_order:
        if role not in role_scores and not any(
            role in prob_result for prob_result in lef_by_problem.values()
        ):
            continue
        n_valid = len(role_scores.get(role, []))
        desc = ROLE_DESC.get(role, "randomly sampled solution")
        lines.append(
            f"  {role:<12s}: {n_valid:>3} / {n_lef:<3}  — {desc}"
        )

    lines += ["", f"Total problems with LEF data: {n_lef}"]

    gap_list = []
    for prob_name, prob_result in lef_by_problem.items():
        best_score    = _get_score(prob_result.get("best",    {}))
        slowest_score = _get_score(prob_result.get("slowest", {}))
        if best_score is not None and slowest_score is not None:
            gap_list.append((prob_name, best_score, slowest_score,
                             slowest_score - best_score))
    gap_list.sort(key=lambda x: x[3], reverse=True)

    if gap_list:
        lines += [
            "",
            f"Top {min(top_n, len(gap_list))} problems by (slowest - best) LEF gap:",
        ]
        for rank, (name, b, s, gap) in enumerate(gap_list[:top_n], 1):
            lines.append(
                f"  {rank}. {name}"
                f"\n       best={b:>10.2f}  slowest={s:>10.2f}  gap={gap:>10.2f}"
            )

    path = output_dir / "lef_summary.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("[LEF] Summary saved: %s", path)

def _parse_summary_metrics(summary_path: Path) -> dict:
    metrics = {}
    text = summary_path.read_text(encoding="utf-8")
    current_section = "header"

    for line in text.splitlines():
        stripped = line.strip()
        header_m = _re.match(r'\*\*(.+?)\*\*', stripped)
        if header_m:
            current_section = header_m.group(1).strip()
            if current_section.startswith("Criterion:"):
                current_section = current_section.split("(")[0].replace("Criterion:", "").strip()
            continue

        m = _re.match(r'(TPR(?:_norm)?\([^)]+\))\s*=\s*([\d.]+)', stripped)
        if m:
            metrics[f"{current_section}::{m.group(1)}"] = float(m.group(2))
            continue
        m = _re.match(r'(ATS)\s*=\s*([\d.]+)', stripped)
        if m:
            metrics[f"{current_section}::{m.group(1)}"] = float(m.group(2))
            continue
        m = _re.match(r'TC Accuracy[^:]*:\s*([\d.]+)', stripped)
        if m:
            metrics[f"{current_section}::tc_accuracy"] = float(m.group(1))
            continue
        m = _re.match(r'(fast|medium|slow|invalid|exceeded|not_exceeded)\s+(\d+)\s+([\d.]+)%', stripped)
        if m:
            tier, count, pct = m.group(1), int(m.group(2)), float(m.group(3))
            metrics[f"{current_section}::{tier}_count"] = count
            metrics[f"{current_section}::{tier}_pct"] = pct
            continue

    return metrics

def _write_constraint_violations_report(comparisons: list, view_output_dir: Path,
                                          split: str, lang_label: str) -> None:

    def _build_report(cc_key: str, viol_key: str, file_name: str, header_label: str):
        out_path = view_output_dir / file_name

        n_total = 0
        n_exceeded = 0
        n_exc_violated = 0
        n_exc_unknown = 0
        n_exc_valid = 0

        per_problem: list = []
        any_signal = False

        for comp in comparisons:
            prob_name = comp.get("name") or f"problem_{comp.get('index')}"
            prob_idx = comp.get("index")
            ev = comp.get("evaluand") or {}
            binary = ev.get("binary_exceeded") or {}
            tcs = binary.get("testcases") or []
            problem_rows: list = []
            for tc in tcs:
                n_total += 1
                if tc.get(cc_key) is not None:
                    any_signal = True
                if tc.get("exceeded") is not True:
                    continue
                n_exceeded += 1
                cc = tc.get(cc_key)
                if cc is True:
                    n_exc_valid += 1
                elif cc is False:
                    n_exc_violated += 1
                    problem_rows.append({
                        "tc": tc.get("testcase"),
                        "lang": tc.get("language"),
                        "run_time": tc.get("run_time"),
                        "max_ref_time": tc.get("max_ref_time"),
                        "tier": tc.get("target_tier"),
                        "violations": tc.get(viol_key) or [],
                    })
                else:
                    n_exc_unknown += 1
            if problem_rows:
                per_problem.append((prob_name, prob_idx, problem_rows))

        if not any_signal:
            return

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"Constraint Violations Report ({header_label})\n")
            f.write(f"=" * 64 + "\n")
            f.write(f"Split:           {split}\n")
            f.write(f"Language view:   {lang_label}\n")
            f.write(f"Constraint set:  {header_label}\n")
            f.write(f"\nAggregate (across all (TC, language) entries in this view):\n")
            f.write(f"  total entries:                                {n_total}\n")
            f.write(f"  exceeded_norm (raw):                          {n_exceeded}\n")
            f.write(f"  exceeded AND constraint_valid:                {n_exc_valid}\n")
            f.write(f"  exceeded AND constraint_violated (LISTED below): {n_exc_violated}\n")
            f.write(f"  exceeded AND compliance_unknown (skipped):    {n_exc_unknown}\n")
            if n_exceeded:
                pct_violated = 100 * n_exc_violated / n_exceeded
                pct_unknown = 100 * n_exc_unknown / n_exceeded
                f.write(f"\n  → of the raw {n_exceeded} exceeded TCs:\n")
                f.write(f"      {n_exc_violated} ({pct_violated:.1f}%) actually violated constraints\n")
                f.write(f"      {n_exc_unknown} ({pct_unknown:.1f}%) had compliance unknown (stdin parse failed/missing)\n")
            f.write("\n")
            f.write("=" * 64 + "\n")
            f.write(f"Per-problem listing of exceeded-but-constraint-violated TCs\n")
            f.write("=" * 64 + "\n")

            if not per_problem:
                f.write("\n(no exceeded-but-violated TCs in this view)\n")
                return

            per_problem.sort(key=lambda x: (x[1] if x[1] is not None else 0, x[0]))
            for prob_name, prob_idx, rows in per_problem:
                f.write(f"\n[{prob_idx}] {prob_name}  ({len(rows)} TCs)\n")
                for r in rows:
                    rt = r["run_time"]
                    mxrt = r["max_ref_time"]
                    rt_s = f"{rt:.3f}s" if isinstance(rt, (int, float)) else "—"
                    mxrt_s = f"{mxrt:.3f}s" if isinstance(mxrt, (int, float)) else "—"
                    f.write(f"  TC#{r['tc']:<3} lang={r['lang'] or '-':<8} "
                            f"run_time={rt_s} (max_ref={mxrt_s})\n")
                    for v in r["violations"]:
                        f.write(f"      ✗ {v}\n")

    _build_report("constraint_compliant", "constraint_violations",
                  "constraint_violations.txt", "All types")
    _build_report("constraint_compliant_boundary", "constraint_violations_boundary",
                  "constraint_violations_boundary.txt", "Boundary only")

def _parse_summary_full(summary_path: Path) -> dict:
    text = summary_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    out = {
        "header": {"split": None, "ref_path": None, "ev_path": None,
                   "single_tier": None, "tier_coverage": {}},
        "problems": {"total_ref": None, "both_ok": None,
                     "ref_only": None, "ev_only": None},
        "tc_accuracy": {
            "expected_total": None, "tcs_per_problem": None,
            "generated": None, "generated_pct": None,
            "missing": None, "missing_pct": None,
            "valid": None, "valid_pct": None,
            "invalid": None, "invalid_pct": None,
            "accuracy": None,
            "per_tier": {},
            "tcs_per_tier": None,
            "problems_with_valid_tier": {},
            "problems_with_any_valid": None,
            "problems_with_valid_scope": None,
        },
        "criteria": {},
        "binary": {
            "category": {},
            "total": None,
            "per_tier": {},
            "per_tier_norm": {},
            "per_tier_norm_denom_per_tier": None,
            "per_tier_norm_cc_all": {},
            "per_tier_norm_cc_boundary": {},
        },
        "tle": {
            "category": {},
            "total": None,
            "per_tier_norm": {},
            "per_tier_norm_denom_per_tier": None,
        },
        "verdict": {},
        "no_verdict": None,
        "max_ref_time": {},
    }

    current_section = "header"
    in_distribution = False
    distribution_target = None
    in_binary_per_tier = False
    in_binary_per_tier_norm = False
    in_binary_per_tier_norm_cc_scope = None
    in_tle_per_tier_norm = False
    in_per_tier_acc = False
    in_problems_valid = False
    in_max_ref_time = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        m = _re.match(r'^Split:\s*(\S+)', stripped)
        if m:
            out["header"]["split"] = m.group(1); continue
        m = _re.match(r'^Reference \(criterion\) timing:\s*(.+)$', stripped)
        if m:
            out["header"]["ref_path"] = m.group(1); continue
        m = _re.match(r'^Evaluand \(slow\) timing:\s*(.+)$', stripped)
        if m:
            out["header"]["ev_path"] = m.group(1); continue
        m = _re.match(r'.*SINGLE TIER ONLY \(\'([^\']+)\',\s*count=(\d+)\)', stripped)
        if m:
            out["header"]["single_tier"] = m.group(1)
            out["header"]["tier_coverage"][m.group(1)] = int(m.group(2)); continue
        m = _re.match(r'^Tier Coverage \(valid TCs across languages\):\s*fast=(\d+),\s*medium=(\d+),\s*slow=(\d+)', stripped)
        if m:
            out["header"]["tier_coverage"] = {
                "fast": int(m.group(1)), "medium": int(m.group(2)), "slow": int(m.group(3))}
            continue

        m = _re.match(r'^Total \(reference\):\s*(\d+)', stripped)
        if m: out["problems"]["total_ref"] = int(m.group(1)); continue
        m = _re.match(r'^Both sources available:\s*(\d+)', stripped)
        if m: out["problems"]["both_ok"] = int(m.group(1)); continue
        m = _re.match(r'^Reference only:\s*(\d+)', stripped)
        if m: out["problems"]["ref_only"] = int(m.group(1)); continue
        m = _re.match(r'^Evaluand only:\s*(\d+)', stripped)
        if m: out["problems"]["ev_only"] = int(m.group(1)); continue

        hm = _re.match(r'\*\*(.+?)\*\*', stripped)
        if hm:
            sec = hm.group(1).strip()
            if sec.startswith("Criterion:"):
                sec_name = sec.replace("Criterion:", "").strip()
                sec_short = sec_name.split("(")[0].strip()
                current_section = sec_short
                out["criteria"].setdefault(current_section, {
                    "title": sec_name,
                    "distribution": {},
                    "ev_total_dist": None,
                    "ref_total_dist": None,
                    "tpr": {},
                    "tpr_norm": {},
                    "ats": None,
                })
            elif sec == "TC Accuracy":
                current_section = "TC Accuracy"
            elif sec == "Verdict Distribution":
                current_section = "Verdict Distribution"
            else:
                current_section = sec
            in_distribution = False
            in_binary_per_tier = False
            in_binary_per_tier_norm = False
            in_binary_per_tier_norm_cc_scope = None
            in_tle_per_tier_norm = False
            in_max_ref_time = False
            continue

        if _re.match(r'^Reference Max Time\b', stripped):
            in_max_ref_time = True
            continue
        if in_max_ref_time:
            if not stripped or stripped.startswith("Aggregate ") or stripped.startswith("[Evaluand"):
                in_max_ref_time = False
            else:
                m = _re.match(
                    r'^(\S+)\s+(\d+)\s+([\d.]+)s\s+([\d.]+)s\s+([\d.]+)s\s+([\d.]+)s\s+([\d.]+)s\s*$',
                    stripped,
                )
                if m:
                    scope = m.group(1)
                    if scope.lower() not in ("scope",):
                        out["max_ref_time"][scope] = {
                            "N":      int(m.group(2)),
                            "avg":    float(m.group(3)),
                            "median": float(m.group(4)),
                            "min":    float(m.group(5)),
                            "max":    float(m.group(6)),
                            "p90":    float(m.group(7)),
                        }
                    continue

        if current_section == "TC Accuracy":
            m = _re.match(r'^Expected TCs:\s*(\d+)\s*\((\d+)\s+problems x\s+(\d+)\s+TCs\)', stripped)
            if m:
                out["tc_accuracy"]["expected_total"] = int(m.group(1))
                out["tc_accuracy"]["tcs_per_problem"] = int(m.group(3))
                continue
            m = _re.match(r'^Generated \(judged, unique TC\):\s*(\d+)\s*\(([\d.]+)%\)', stripped)
            if m:
                out["tc_accuracy"]["generated"] = int(m.group(1))
                out["tc_accuracy"]["generated_pct"] = float(m.group(2)); continue
            m = _re.match(r'^Missing \(no evaluand\):\s*(\d+)\s*\(([\d.]+)%\)', stripped)
            if m:
                out["tc_accuracy"]["missing"] = int(m.group(1))
                out["tc_accuracy"]["missing_pct"] = float(m.group(2)); continue
            m = _re.match(r'^Valid \(correct, unique TC\):\s*(\d+)\s*\(([\d.]+)%\)', stripped)
            if m:
                out["tc_accuracy"]["valid"] = int(m.group(1))
                out["tc_accuracy"]["valid_pct"] = float(m.group(2)); continue
            m = _re.match(r'^Invalid \([^)]+\):\s*(\d+)\s*\(([\d.]+)%\)', stripped)
            if m:
                out["tc_accuracy"]["invalid"] = int(m.group(1))
                out["tc_accuracy"]["invalid_pct"] = float(m.group(2)); continue
            m = _re.match(r'^TC Accuracy \(valid / expected\):\s*([\d.]+)', stripped)
            if m:
                out["tc_accuracy"]["accuracy"] = float(m.group(1)); continue

            if "Per-Tier TC Accuracy" in stripped:
                m = _re.match(r'.*\((\d+)\s+TCs/tier/problem\)', stripped)
                if m: out["tc_accuracy"]["tcs_per_tier"] = int(m.group(1))
                in_per_tier_acc = True; continue
            if in_per_tier_acc:
                m = _re.match(r'^(fast|medium|slow)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)', stripped)
                if m:
                    out["tc_accuracy"]["per_tier"][m.group(1)] = {
                        "expected": int(m.group(2)),
                        "generated": int(m.group(3)),
                        "valid": int(m.group(4)),
                        "invalid": int(m.group(5)),
                        "acc": float(m.group(6)),
                    }
                    continue
                if "Problems with" in stripped:
                    in_per_tier_acc = False
                    in_problems_valid = True
                    m2 = _re.match(r'.*scope\s*=\s*(\d+)', stripped)
                    if m2:
                        out["tc_accuracy"]["problems_with_valid_scope"] = int(m2.group(1))
                    continue
            if in_problems_valid:
                m = _re.match(r'^(fast|medium|slow|any)\s+(\d+)\s+/\s+(\d+)\s+([\d.]+)%', stripped)
                if m:
                    tier = m.group(1)
                    if tier == "any":
                        out["tc_accuracy"]["problems_with_any_valid"] = int(m.group(2))
                    else:
                        out["tc_accuracy"]["problems_with_valid_tier"][tier] = int(m.group(2))
                    continue

        if current_section in out["criteria"] and current_section != "Binary Reference Exceeded":
            crit = out["criteria"][current_section]
            m = _re.match(r'^(fast|medium|slow)\s+(\d+)\s+([\d.]+)%\s+(\d+)\s+([\d.]+)%\s+([+-]?[\d.]+)%', stripped)
            if m:
                crit["distribution"][m.group(1)] = {
                    "ref_n": int(m.group(2)), "ref_pct": float(m.group(3)),
                    "ev_n": int(m.group(4)), "ev_pct": float(m.group(5)),
                    "delta": float(m.group(6)),
                }
                continue
            m = _re.match(r'^(fast|medium|slow)\s+(\d+)\s+([\d.]+)%\s*$', stripped)
            if m:
                crit["distribution"][m.group(1)] = {
                    "ev_n": int(m.group(2)), "ev_pct": float(m.group(3)),
                }
                continue
            m = _re.match(r'^invalid\s+—\s+—\s+(\d+)\s+([\d.]+)%', stripped)
            if m:
                crit["distribution"]["invalid"] = {
                    "ev_n": int(m.group(1)), "ev_pct": float(m.group(2)),
                }
                continue
            m = _re.match(r'^invalid\s+(\d+)\s+([\d.]+)%\s*$', stripped)
            if m:
                crit["distribution"]["invalid"] = {
                    "ev_n": int(m.group(1)), "ev_pct": float(m.group(2)),
                }
                continue
            m = _re.match(r'^Total\s+(\d+)\s+(\d+)\s*$', stripped)
            if m:
                crit["ref_total_dist"] = int(m.group(1))
                crit["ev_total_dist"] = int(m.group(2)); continue
            m = _re.match(r'^Total\s+(\d+)\s*$', stripped)
            if m:
                crit["ev_total_dist"] = int(m.group(1)); continue

            m = _re.match(r'TPR\((fast|medium|slow)\)\s*=\s*([\d.]+)\s*\((\d+)/(\d+)\)', stripped)
            if m:
                crit["tpr"][m.group(1)] = (float(m.group(2)), int(m.group(3)), int(m.group(4)))
                continue
            m = _re.match(r'TPR_norm\((fast|medium|slow)\)\s*=\s*([\d.]+)\s*\((\d+)/(\d+)\)', stripped)
            if m:
                crit["tpr_norm"][m.group(1)] = (float(m.group(2)), int(m.group(3)), int(m.group(4)))
                continue
            m = _re.match(r'^ATS\s*=\s*([\d.]+)', stripped)
            if m:
                crit["ats"] = float(m.group(1)); continue

        if current_section == "Binary Reference Exceeded":
            m = _re.match(r'^(exceeded|not_exceeded)\s+(\d+)\s+([\d.]+)%\s+([\d.]+)%', stripped)
            if m:
                out["binary"]["category"][m.group(1)] = {
                    "count": int(m.group(2)), "pct": float(m.group(3)),
                    "vpct": float(m.group(4)),
                }
                continue
            m = _re.match(r'^invalid\s+(\d+)\s+([\d.]+)%\s*$', stripped)
            if m:
                out["binary"]["category"]["invalid"] = {
                    "count": int(m.group(1)), "pct": float(m.group(2)), "vpct": None,
                }
                continue
            m = _re.match(r'^Total\s+(\d+)\s*$', stripped)
            if m:
                out["binary"]["total"] = int(m.group(1)); continue

            if "Per-Tier Breakdown (binary_exceeded, by intended target_tier)" in stripped:
                in_binary_per_tier = True; in_binary_per_tier_norm = False
                in_binary_per_tier_norm_cc_scope = None
                continue
            if "Per-Tier Breakdown with Constraints check" in stripped:
                in_binary_per_tier = False; in_binary_per_tier_norm = False
                if "All types" in stripped:
                    in_binary_per_tier_norm_cc_scope = "all"
                elif "Boundary only" in stripped:
                    in_binary_per_tier_norm_cc_scope = "boundary"
                else:
                    in_binary_per_tier_norm_cc_scope = None
                continue
            if "Per-Tier Breakdown (TPR_norm style" in stripped:
                in_binary_per_tier = False; in_binary_per_tier_norm = True
                in_binary_per_tier_norm_cc_scope = None
                m = _re.match(r'.*=\s*(\d+)\s*[×x]\s*(\d+)\s*=\s*(\d+)\)', stripped)
                if m:
                    out["binary"]["per_tier_norm_denom_per_tier"] = int(m.group(3))
                continue
            if (in_binary_per_tier or in_binary_per_tier_norm
                    or in_binary_per_tier_norm_cc_scope):
                m = _re.match(
                    r'^(exceeded(?:_norm)?(?:_constraint_valid(?:_with_TLE_compliant)?)?'
                    r'|not_exceeded(?:_norm)?(?:_constraint_valid)?'
                    r'|constraint_violated|compliance_unknown|invalid|Total)\s+'
                    r'(\d+)(?:/(\d+))?(?:\s*\(([\d.]+)%\))?\s+'
                    r'(\d+)(?:/(\d+))?(?:\s*\(([\d.]+)%\))?\s+'
                    r'(\d+)(?:/(\d+))?(?:\s*\(([\d.]+)%\))?\s+'
                    r'(\d+)(?:/(\d+))?(?:\s*\(([\d.]+)%\))?',
                    stripped)
                if m:
                    label = m.group(1)
                    cells = []
                    for i in range(4):
                        base = 2 + i * 3
                        hit = int(m.group(base))
                        denom = int(m.group(base + 1)) if m.group(base + 1) else None
                        pct = float(m.group(base + 2)) if m.group(base + 2) else None
                        cells.append((hit, denom, pct))
                    if in_binary_per_tier_norm_cc_scope == "all":
                        target = out["binary"]["per_tier_norm_cc_all"]
                    elif in_binary_per_tier_norm_cc_scope == "boundary":
                        target = out["binary"]["per_tier_norm_cc_boundary"]
                    else:
                        target = (out["binary"]["per_tier_norm"]
                                  if in_binary_per_tier_norm
                                  else out["binary"]["per_tier"])
                    target[label] = {"fast": cells[0], "medium": cells[1],
                                     "slow": cells[2], "all": cells[3]}
                    continue

        if current_section == "TLE on Constraint-Compliant TC":
            m = _re.match(r'^(tle_compliant|tle_violating)\s+(\d+)\s+([\d.]+)%', stripped)
            if m:
                out["tle"]["category"][m.group(1)] = {
                    "count": int(m.group(2)), "pct": float(m.group(3)),
                }
                continue
            m = _re.match(r'^Total TLE TCs\s+(\d+)', stripped)
            if m:
                out["tle"]["total"] = int(m.group(1)); continue
            if "Per-Tier Breakdown (TPR_norm style" in stripped:
                in_tle_per_tier_norm = True
                m = _re.match(r'.*=\s*(\d+)\s*[×x]\s*(\d+)\s*=\s*(\d+)\)', stripped)
                if m:
                    out["tle"]["per_tier_norm_denom_per_tier"] = int(m.group(3))
                continue
            if in_tle_per_tier_norm:
                m = _re.match(
                    r'^(TLE_compliant_norm|TLE_violating_norm)\s+'
                    r'(\d+)(?:/(\d+))?(?:\s*\(([\d.]+)%\))?\s+'
                    r'(\d+)(?:/(\d+))?(?:\s*\(([\d.]+)%\))?\s+'
                    r'(\d+)(?:/(\d+))?(?:\s*\(([\d.]+)%\))?\s+'
                    r'(\d+)(?:/(\d+))?(?:\s*\(([\d.]+)%\))?',
                    stripped)
                if m:
                    label = m.group(1)
                    cells = []
                    for i in range(4):
                        base = 2 + i * 3
                        hit = int(m.group(base))
                        denom = int(m.group(base + 1)) if m.group(base + 1) else None
                        pct = float(m.group(base + 2)) if m.group(base + 2) else None
                        cells.append((hit, denom, pct))
                    out["tle"]["per_tier_norm"][label] = {
                        "fast": cells[0], "medium": cells[1],
                        "slow": cells[2], "all": cells[3],
                    }
                    continue

        if current_section == "Verdict Distribution":
            m = _re.match(r'^(SLOWER_HEAVIER|FASTER_HEAVIER|MORE_POLARIZED|SIMILAR):\s*(\d+)\s+problems', stripped)
            if m:
                out["verdict"][m.group(1)] = int(m.group(2)); continue
            m = _re.match(r'^No verdict[^:]*:\s*(\d+)\s+problems', stripped)
            if m:
                out["no_verdict"] = int(m.group(1)); continue

    return out

def _avg_int(values):
    vs = [v for v in values if isinstance(v, (int, float))]
    return sum(vs) / len(vs) if vs else 0.0

def _avg_or_none(values):
    vs = [v for v in values if isinstance(v, (int, float))]
    return sum(vs) / len(vs) if vs else None

def _binary_per_tier_from_difficulty_json(dc_path: Path,
                                          eval_tiers: Optional[set] = None) -> dict:
    import json as _json
    with open(dc_path, "r", encoding="utf-8") as f:
        data = _json.load(f)
    out = {t: {"exceeded": 0, "not_exceeded": 0, "invalid": 0, "total": 0}
           for t in ("fast", "medium", "slow", "no_tier")}
    per_lang: dict = {}
    n_problems = 0
    for prob in data.get("problems", []):
        ev = prob.get("evaluand") or {}
        binary = ev.get("binary_exceeded") or {}
        tcs = binary.get("testcases") or []
        if not tcs:
            continue
        n_problems += 1
        for tc in tcs:
            tier = tc.get("target_tier") or "no_tier"
            if eval_tiers is not None and tier not in eval_tiers:
                continue
            if tier not in out:
                tier = "no_tier"
            exc = tc.get("exceeded")
            out[tier]["total"] += 1
            if exc is True:
                out[tier]["exceeded"] += 1
            elif exc is False:
                out[tier]["not_exceeded"] += 1
            else:
                out[tier]["invalid"] += 1
            lang = tc.get("language") or "no_lang"
            if lang not in per_lang:
                per_lang[lang] = {t: {"exceeded": 0, "not_exceeded": 0,
                                       "invalid": 0, "total": 0}
                                  for t in ("fast", "medium", "slow", "no_tier")}
            if tier not in per_lang[lang]:
                per_lang[lang][tier] = {"exceeded": 0, "not_exceeded": 0,
                                         "invalid": 0, "total": 0}
            per_lang[lang][tier]["total"] += 1
            if exc is True:
                per_lang[lang][tier]["exceeded"] += 1
            elif exc is False:
                per_lang[lang][tier]["not_exceeded"] += 1
            else:
                per_lang[lang][tier]["invalid"] += 1
    out["_per_language"] = per_lang
    out["_problem_count"] = n_problems
    return out

def _tle_compliant_per_tier_from_difficulty_json(dc_path: Path,
                                                  num_ref_problems: Optional[int] = None,
                                                  tcs_per_tier: Optional[int] = None,
                                                  eval_tiers: Optional[set] = None) -> dict:
    import json as _json
    try:
        with open(dc_path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except Exception:
        return {}

    counts = {"compliant": {"fast": 0, "medium": 0, "slow": 0},
              "violating": {"fast": 0, "medium": 0, "slow": 0}}
    for prob in data.get("problems", []):
        ev = prob.get("evaluand") or {}
        tle = ev.get("tle_compliant") or {}
        for tc in tle.get("testcases", []) or []:
            tier = tc.get("target_tier")
            if tier not in counts["compliant"]:
                continue
            if eval_tiers is not None and tier not in eval_tiers:
                continue
            if tc.get("compliant") is True:
                counts["compliant"][tier] += 1
            else:
                counts["violating"][tier] += 1

    denom_per_tier = (num_ref_problems * tcs_per_tier
                      if num_ref_problems and tcs_per_tier else None)

    def _build(label_map: dict) -> dict:
        out: dict = {}
        for tier in ("fast", "medium", "slow"):
            hits = label_map[tier]
            out[tier] = (hits, denom_per_tier)
        all_hits = sum(label_map.values())
        all_denom = denom_per_tier * 3 if denom_per_tier else None
        out["all"] = (all_hits, all_denom)
        return out

    return {
        "TLE_compliant_norm": _build(counts["compliant"]),
        "TLE_violating_norm": _build(counts["violating"]),
    }

def _render_avg_summary(out_path: Path, parsed_per_strategy: list,
                       strategy_names: list, lang_label: str,
                       slow_ratio_threshold: float = 0.9,
                       binary_per_tier_pooled: list = None):
    n = len(parsed_per_strategy)
    if n == 0:
        return
    p0 = parsed_per_strategy[0]

    def _write_per_strategy_max_ref_time(fh):
        use_intersected = all(p.get("max_ref_time_intersected") for p in parsed_per_strategy)
        key = "max_ref_time_intersected" if use_intersected else "max_ref_time"
        any_present = any(p.get(key) for p in parsed_per_strategy)
        if not any_present:
            return
        scopes_seen: list[str] = []
        seen_set: set = set()
        for p in parsed_per_strategy:
            for s in (p.get(key) or {}).keys():
                if s not in seen_set:
                    seen_set.add(s)
                    scopes_seen.append(s)
        ordered_scopes = (["overall"] if "overall" in seen_set else []) + \
                         [s for s in sorted(scopes_seen) if s != "overall"]
        if use_intersected:
            common_n = next((p[key].get("overall", {}).get("N")
                             for p in parsed_per_strategy
                             if p.get(key, {}).get("overall")), None)
            scope_note = (f" [intersection scope: {common_n} problems "
                          f"covered by ALL {len(parsed_per_strategy)} strategies]"
                          if common_n is not None else " [intersection scope]")
        else:
            scope_note = " [per-strategy own scope; intersection unavailable]"
        fh.write("Per-Strategy Reference Max Time "
                 "(max_ref_time aggregated across problem-language pairs)"
                 f"{scope_note}:\n")
        for scope in ordered_scopes:
            fh.write(f"  Scope: {scope}\n")
            fh.write(f"    {'Strategy':<20} {'N':>6} {'avg':>10} {'median':>10} "
                     f"{'min':>10} {'max':>10} {'p90':>10}\n")
            fh.write(f"    {'-' * 80}\n")
            for i, sname in enumerate(strategy_names):
                row = (parsed_per_strategy[i].get(key) or {}).get(scope)
                if not row:
                    fh.write(f"    {sname:<20} {'—':>6} {'—':>10} {'—':>10} "
                             f"{'—':>10} {'—':>10} {'—':>10}\n")
                    continue
                fh.write(f"    {sname:<20} {row['N']:>6} "
                         f"{row['avg']:>9.4f}s {row['median']:>9.4f}s "
                         f"{row['min']:>9.4f}s {row['max']:>9.4f}s {row['p90']:>9.4f}s\n")
            fh.write(f"    {'-' * 80}\n")
        fh.write("\n")

    def avg_field(*path):
        vals = []
        for p in parsed_per_strategy:
            cur = p
            ok = True
            for k in path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False; break
            if ok and isinstance(cur, (int, float)):
                vals.append(cur)
        return _avg_or_none(vals)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("Slow Test Case Evaluation Summary (averaged across solution strategies)\n")
        f.write("=" * 70 + "\n")
        f.write(f"Strategies averaged: {n} ({', '.join(strategy_names)})\n")
        f.write(f"Language view: {lang_label}\n")
        if p0["header"].get("split"):
            f.write(f"Split: {p0['header']['split']}\n")
        if p0["header"].get("ref_path"):
            f.write(f"Reference (criterion) timing: {p0['header']['ref_path']}\n")

        single = p0["header"].get("single_tier")
        if single:
            avg_cov = _avg_or_none([p["header"]["tier_coverage"].get(single, 0)
                                    for p in parsed_per_strategy])
            f.write(f"\n⚠ Tier Coverage: SINGLE TIER ONLY ('{single}', avg count={avg_cov:.1f})\n")
        else:
            tcov = {t: _avg_or_none([p["header"]["tier_coverage"].get(t, 0)
                                     for p in parsed_per_strategy])
                    for t in ("fast", "medium", "slow")}
            f.write(f"\nTier Coverage (avg valid TCs across languages): "
                    f"fast={tcov['fast']:.1f}, medium={tcov['medium']:.1f}, slow={tcov['slow']:.1f}\n")
        f.write("=" * 70 + "\n\n")

        f.write("Problems (averaged across strategies):\n")
        for label, key in [("Total (reference)", "total_ref"),
                           ("Both sources available", "both_ok"),
                           ("Reference only", "ref_only"),
                           ("Evaluand only", "ev_only")]:
            v = avg_field("problems", key)
            if v is not None:
                f.write(f"  {label}: {v:.1f}\n")
        f.write("\n")

        tca0 = p0["tc_accuracy"]
        expected_total = avg_field("tc_accuracy", "expected_total") or 0
        tcs_per_problem = tca0.get("tcs_per_problem")
        n_ref = avg_field("problems", "total_ref") or 0
        f.write("============================================================\n")
        f.write("**TC Accuracy** (averaged across strategies)\n")
        f.write(f"Test Case Accuracy (evaluand, scope = {n_ref:.1f} reference problems):\n")
        f.write(f"  Expected TCs: {expected_total:.1f}"
                f"  ({n_ref:.1f} problems x {tcs_per_problem or '?'} TCs)\n")
        for label, key, pct_key in [
            ("Generated (judged, unique TC)", "generated", "generated_pct"),
            ("Missing (no evaluand)", "missing", "missing_pct"),
            ("Valid (correct, unique TC)", "valid", "valid_pct"),
            ("Invalid (WA/RE/TLE/OLE/MLE/empty, unique TC)", "invalid", "invalid_pct"),
        ]:
            v = avg_field("tc_accuracy", key)
            pv = avg_field("tc_accuracy", pct_key)
            if v is not None:
                f.write(f"  {label}: {v:.1f}  ({pv if pv is not None else 0.0:.1f}%)\n")
        acc = avg_field("tc_accuracy", "accuracy")
        if acc is not None:
            f.write(f"  TC Accuracy (valid / expected): {acc:.4f}\n")
        f.write("\n")

        tier_data = {}
        any_tier = False
        for tier in ("fast", "medium", "slow"):
            present = [p["tc_accuracy"]["per_tier"].get(tier) for p in parsed_per_strategy]
            present = [pt for pt in present if pt]
            if present:
                any_tier = True
                tier_data[tier] = {
                    "expected": _avg_or_none([pt["expected"] for pt in present]),
                    "generated": _avg_or_none([pt["generated"] for pt in present]),
                    "valid": _avg_or_none([pt["valid"] for pt in present]),
                    "invalid": _avg_or_none([pt["invalid"] for pt in present]),
                    "acc": _avg_or_none([pt["acc"] for pt in present]),
                }
        if any_tier:
            tcs_per_tier = avg_field("tc_accuracy", "tcs_per_tier")
            f.write(f"  Per-Tier TC Accuracy (intended tier, ~{tcs_per_tier or '?'} TCs/tier/problem; averaged):\n")
            f.write(f"    {'Tier':<8} {'Expected':>10} {'Generated':>10} {'Valid':>8} {'Invalid':>8} {'Acc(v/e)':>10}\n")
            f.write(f"    {'-' * 60}\n")
            for tier in ("fast", "medium", "slow"):
                d = tier_data.get(tier)
                if d:
                    f.write(f"    {tier:<8} {d['expected']:>10.1f} {d['generated']:>10.1f} "
                            f"{d['valid']:>8.1f} {d['invalid']:>8.1f} {d['acc']:>10.4f}\n")
            f.write("\n")

        scope = avg_field("tc_accuracy", "problems_with_valid_scope")
        if scope is not None:
            f.write(f"  Problems with ≥1 valid TC surviving (avg scope = {scope:.1f} both_ok problems):\n")
            f.write(f"    {'Tier':<8} {'Problems':>10} {'/ scope':>10}   {'%':>6}\n")
            f.write(f"    {'-' * 40}\n")
            for tier in ("fast", "medium", "slow"):
                v = _avg_or_none([
                    p["tc_accuracy"]["problems_with_valid_tier"].get(tier, 0)
                    for p in parsed_per_strategy])
                if v is None: v = 0.0
                pct = v / scope * 100 if scope else 0.0
                f.write(f"    {tier:<8} {v:>10.1f} {'/ ' + f'{scope:.1f}':>10}   {pct:>5.1f}%\n")
            anyv = avg_field("tc_accuracy", "problems_with_any_valid")
            if anyv is not None:
                pct = anyv / scope * 100 if scope else 0.0
                f.write(f"    {'-' * 40}\n")
                f.write(f"    {'any':<8} {anyv:>10.1f} {'/ ' + f'{scope:.1f}':>10}   {pct:>5.1f}%\n")
            f.write("\n")

        crit_order = ["Tercile", "Timelimit", "Reference Bounds",
                      "Ratio to Max Reference Time", "Binary Reference Exceeded"]
        if not _EMIT_LEGACY_CRITERIA:
            crit_order = [c for c in crit_order
                          if c not in {"Tercile", "Timelimit", "Reference Bounds"}]
        if not _EMIT_RATIO_CRITERION:
            crit_order = [c for c in crit_order if c != "Ratio to Max Reference Time"]
        present_crits = [c for c in crit_order if c in p0["criteria"]]

        for crit_name in [c for c in present_crits if c != "Binary Reference Exceeded"]:
            f.write("============================================================\n")
            title = p0["criteria"][crit_name].get("title", crit_name)
            f.write(f"**Criterion: {title}** (averaged across strategies)\n\n")

            crit_first = p0["criteria"][crit_name]
            has_ref_col = any("ref_n" in d for d in crit_first["distribution"].values())
            difficulties = ("fast", "medium", "slow", "invalid")
            f.write("Aggregate Difficulty Distribution (averaged counts; per language view):\n")
            if has_ref_col:
                f.write(f"  {'Difficulty':<10} {'Reference':>12} {'%':>8}  {'Evaluand':>12} {'%':>8}  {'Delta':>8}\n")
                f.write(f"  {'-' * 62}\n")
                for d in difficulties:
                    rows = [p["criteria"].get(crit_name, {}).get("distribution", {}).get(d)
                            for p in parsed_per_strategy]
                    rows = [r for r in rows if r]
                    if not rows:
                        continue
                    if d == "invalid":
                        e_n = _avg_or_none([r["ev_n"] for r in rows]) or 0.0
                        e_p = _avg_or_none([r["ev_pct"] for r in rows]) or 0.0
                        f.write(f"  {d:<10} {'—':>12} {'—':>8}  {e_n:>12.1f} {e_p:>7.1f}%  {'—':>8}\n")
                    else:
                        r_n = _avg_or_none([r.get("ref_n", 0) for r in rows]) or 0.0
                        r_p = _avg_or_none([r.get("ref_pct", 0) for r in rows]) or 0.0
                        e_n = _avg_or_none([r.get("ev_n", 0) for r in rows]) or 0.0
                        e_p = _avg_or_none([r.get("ev_pct", 0) for r in rows]) or 0.0
                        delta = _avg_or_none([r.get("delta", 0) for r in rows]) or 0.0
                        f.write(f"  {d:<10} {r_n:>12.1f} {r_p:>7.1f}%  {e_n:>12.1f} {e_p:>7.1f}%  {delta:>+7.1f}%\n")
                f.write(f"  {'-' * 62}\n")
                ref_tot = _avg_or_none([p["criteria"][crit_name].get("ref_total_dist")
                                        for p in parsed_per_strategy
                                        if crit_name in p["criteria"]])
                ev_tot = _avg_or_none([p["criteria"][crit_name].get("ev_total_dist")
                                       for p in parsed_per_strategy
                                       if crit_name in p["criteria"]])
                f.write(f"  {'Total':<10} {ref_tot or 0:>12.1f}          {ev_tot or 0:>12.1f}\n")
            else:
                f.write(f"  {'Difficulty':<10} {'Evaluand':>12} {'%':>8}\n")
                f.write(f"  {'-' * 32}\n")
                for d in difficulties:
                    rows = [p["criteria"].get(crit_name, {}).get("distribution", {}).get(d)
                            for p in parsed_per_strategy]
                    rows = [r for r in rows if r]
                    if not rows:
                        continue
                    e_n = _avg_or_none([r.get("ev_n", 0) for r in rows]) or 0.0
                    e_p = _avg_or_none([r.get("ev_pct", 0) for r in rows]) or 0.0
                    f.write(f"  {d:<10} {e_n:>12.1f} {e_p:>7.1f}%\n")
                f.write(f"  {'-' * 32}\n")
                ev_tot = _avg_or_none([p["criteria"][crit_name].get("ev_total_dist")
                                       for p in parsed_per_strategy
                                       if crit_name in p["criteria"]])
                f.write(f"  {'Total':<10} {ev_tot or 0:>12.1f}\n")
            f.write("\n")

            if crit_name != "Binary Reference Exceeded":
                f.write(f"Evaluand Metrics (pooled across {n} strategies — sum hits / sum denom):\n")
                f.write(f"  Note: for each tier, sum_hits and sum_denom are TOTALS across all {n} strategies,\n"
                        f"        not averages. The reported value is sum_hits / sum_denom.\n")
                for tier in ("fast", "medium", "slow"):
                    triples = [p["criteria"].get(crit_name, {}).get("tpr", {}).get(tier)
                               for p in parsed_per_strategy]
                    triples = [t for t in triples if t]
                    if triples:
                        sum_h = sum(int(t[1]) for t in triples)
                        sum_d = sum(int(t[2]) for t in triples)
                        pooled = (sum_h / sum_d) if sum_d else 0.0
                        per_strat_avg = _avg_or_none([t[0] for t in triples])
                        f.write(f"  TPR({tier}){' ' * (7 - len(tier))}"
                                f"= {pooled:.4f}  "
                                f"(pooled {sum_h}/{sum_d}; per-strategy avg ratio={per_strat_avg:.4f})  "
                                f"[denom = generated TCs in '{tier}' tier]\n")
                for tier in ("fast", "medium", "slow"):
                    triples = [p["criteria"].get(crit_name, {}).get("tpr_norm", {}).get(tier)
                               for p in parsed_per_strategy]
                    triples = [t for t in triples if t]
                    if triples:
                        sum_h = sum(int(t[1]) for t in triples)
                        per_strat_denom = int(triples[0][2]) if triples[0][2] else 0
                        total_denom = per_strat_denom * n
                        pooled = (sum_h / total_denom) if total_denom else 0.0
                        f.write(f"  TPR_norm({tier}){' ' * (7 - len(tier))}"
                                f"= {pooled:.4f}  "
                                f"(pooled {sum_h}/{total_denom}; "
                                f"= sum_hits / (N × {per_strat_denom}) = sum_hits / ({n} × {per_strat_denom}))\n")
                ats = _avg_or_none([p["criteria"].get(crit_name, {}).get("ats")
                                    for p in parsed_per_strategy])
                if ats is not None:
                    f.write(f"  ATS        = {ats:.4f}  (mean of per-strategy ATS; reference vs evaluand tier sensitivity)\n")
                f.write("\n")

                f.write("Per-Strategy Evaluand Distribution (count and %):\n")
                difficulties = ("fast", "medium", "slow", "invalid")
                header = f"  {'Strategy':<20}"
                for d in difficulties:
                    header += f" {d:>16}"
                header += f" {'Total':>10}"
                f.write(header + "\n")
                f.write(f"  {'-' * (20 + 16 * len(difficulties) + 10 + len(difficulties) + 1)}\n")
                for i, sname in enumerate(strategy_names):
                    crit_p = parsed_per_strategy[i]["criteria"].get(crit_name, {})
                    dist_p = crit_p.get("distribution", {})
                    tot_p = crit_p.get("ev_total_dist") or 0
                    line = f"  {sname:<20}"
                    for d in difficulties:
                        row = dist_p.get(d, {})
                        nv = row.get("ev_n")
                        pct = row.get("ev_pct")
                        if nv is None:
                            cell = "—"
                        elif pct is None:
                            cell = f"{int(nv)}"
                        else:
                            cell = f"{int(nv)} ({pct:.1f}%)"
                        line += f" {cell:>16}"
                    line += f" {int(tot_p):>10}"
                    f.write(line + "\n")
                f.write(f"  {'-' * (20 + 16 * len(difficulties) + 10 + len(difficulties) + 1)}\n\n")

                if crit_name == "Ratio to Max Reference Time":
                    _write_per_strategy_max_ref_time(f)

        if "Binary Reference Exceeded" in p0["criteria"]:
            f.write("============================================================\n")
            f.write("**Criterion: Binary Reference Exceeded — Category & Per-Tier (averaged)**\n")
            f.write("  Did the generated TC make the solution run slower than ANY reference TC?\n")
            tot = _avg_or_none([p["binary"].get("total") for p in parsed_per_strategy])
            tot_per = tot or 0.0
            grand_total = int(round(tot_per * n))
            f.write(f"  Per-strategy total TCs ≈ {tot_per:.1f}  →  "
                    f"Across {n} strategies, grand total = {grand_total}\n\n")
            _write_per_strategy_max_ref_time(f)

            f.write(f"  {'Category':<15} {'Avg/strat':>10} {'%':>7}  {'%(valid)':>9}  "
                    f"{'Sum (N=' + str(n) + ')':>14}  {'Sum / Total':>17}  {'%':>6}\n")
            f.write(f"  {'-' * 96}\n")
            for cat in ("exceeded", "not_exceeded", "invalid"):
                rows = [p["binary"]["category"].get(cat) for p in parsed_per_strategy]
                rows = [r for r in rows if r]
                if not rows:
                    continue
                cnt = _avg_or_none([r["count"] for r in rows]) or 0.0
                pct = _avg_or_none([r["pct"] for r in rows]) or 0.0
                vpct_vals = [r["vpct"] for r in rows if r.get("vpct") is not None]
                vpct = (sum(vpct_vals) / len(vpct_vals)) if vpct_vals else None
                sum_total = sum(r["count"] for r in rows)
                sum_pct = (sum_total / grand_total * 100) if grand_total else 0.0
                ratio_str = f"{sum_total} / {grand_total}"
                if vpct is not None:
                    f.write(f"  {cat:<15} {cnt:>10.1f} {pct:>6.1f}%  {vpct:>8.1f}%  "
                            f"{sum_total:>14d}  {ratio_str:>17}  {sum_pct:>5.1f}%\n")
                else:
                    f.write(f"  {cat:<15} {cnt:>10.1f} {pct:>6.1f}%  {'—':>9}  "
                            f"{sum_total:>14d}  {ratio_str:>17}  {sum_pct:>5.1f}%\n")
            f.write(f"  {'-' * 96}\n")
            f.write(f"  {'Total':<15} {tot or 0:>10.1f} {'':>8}  {'':>9}  "
                    f"{grand_total:>14d}  {f'{grand_total} / {grand_total}':>17}  {'100.0%':>6}\n\n")

            f.write("Per-Strategy Category Distribution (count and %):\n")
            cats = ("exceeded", "not_exceeded", "invalid")
            header = f"  {'Strategy':<20}"
            for cat in cats:
                header += f" {cat:>18}"
            header += f" {'Total':>10}"
            f.write(header + "\n")
            f.write(f"  {'-' * (20 + 18 * len(cats) + 10 + len(cats) + 1)}\n")
            for i, sname in enumerate(strategy_names):
                cat_p = parsed_per_strategy[i]["binary"].get("category", {})
                tot_p = parsed_per_strategy[i]["binary"].get("total") or 0
                line = f"  {sname:<20}"
                for cat in cats:
                    row = cat_p.get(cat, {})
                    nv = row.get("count")
                    pct = row.get("pct")
                    if nv is None:
                        cell = "—"
                    elif pct is None:
                        cell = f"{int(nv)}"
                    else:
                        cell = f"{int(nv)} ({pct:.1f}%)"
                    line += f" {cell:>18}"
                line += f" {int(tot_p):>10}"
                f.write(line + "\n")
            f.write(f"  {'-' * (20 + 18 * len(cats) + 10 + len(cats) + 1)}\n\n")

            if binary_per_tier_pooled and any(d for d in binary_per_tier_pooled):
                f.write("Per-Strategy x Tier Category Distribution (count and %):\n")
                tiers_pst = ("fast", "medium", "slow")
                header = f"  {'Strategy':<20} {'Tier':<8}"
                for cat in cats:
                    header += f" {cat:>18}"
                header += f" {'Total':>10}"
                f.write(header + "\n")
                sep_len = 20 + 8 + 18 * len(cats) + 10 + len(cats) + 2
                f.write(f"  {'-' * sep_len}\n")
                for i, sname in enumerate(strategy_names):
                    d = binary_per_tier_pooled[i] if i < len(binary_per_tier_pooled) else {}
                    if not d:
                        line = f"  {sname:<20} {'—':<8}"
                        for _ in cats:
                            line += f" {'—':>18}"
                        line += f" {'—':>10}"
                        f.write(line + "\n")
                        continue
                    for tier in tiers_pst:
                        td = d.get(tier, {})
                        tot_t = td.get("total", 0) or 0
                        line = f"  {sname:<20} {tier:<8}"
                        for cat in cats:
                            v = td.get(cat, 0) or 0
                            pct = (v / tot_t * 100) if tot_t else 0.0
                            cell = f"{int(v)} ({pct:.1f}%)"
                            line += f" {cell:>18}"
                        line += f" {int(tot_t):>10}"
                        f.write(line + "\n")
                    if i < len(strategy_names) - 1:
                        f.write(f"  {'-' * sep_len}\n")
                f.write(f"  {'-' * sep_len}\n\n")

            _have_raw_per_tier = (binary_per_tier_pooled
                                  and any(d for d in binary_per_tier_pooled))
            _used_raw_per_tier = False
            if _have_raw_per_tier:
                _used_raw_per_tier = True
                tiers = ("fast", "medium", "slow")
                pooled = {t: {"exceeded": 0, "not_exceeded": 0,
                              "invalid": 0, "total": 0} for t in tiers}
                pooled_lang: dict = {}
                for d in binary_per_tier_pooled:
                    if not d:
                        continue
                    for t in tiers:
                        td = d.get(t, {})
                        for k in ("exceeded", "not_exceeded", "invalid", "total"):
                            pooled[t][k] += td.get(k, 0)
                    pl_d = d.get("_per_language", {}) or {}
                    for lang, tier_dict in pl_d.items():
                        if lang not in pooled_lang:
                            pooled_lang[lang] = {t: {"exceeded": 0,
                                                     "not_exceeded": 0,
                                                     "invalid": 0,
                                                     "total": 0} for t in tiers}
                        for t in tiers:
                            td = tier_dict.get(t, {})
                            for k in ("exceeded", "not_exceeded", "invalid", "total"):
                                pooled_lang[lang][t][k] += td.get(k, 0)

                f.write("  --- Per-Tier Breakdown (binary_exceeded by intended target_tier; "
                        f"pooled across {n} strategies, all languages) ---\n")
                f.write(f"  Source: difficulty_comparison.json (re-aggregated raw TC data)\n")
                f.write(f"  {'':20} {'fast':>20} {'medium':>20} {'slow':>20}\n")
                f.write(f"  {'-' * 86}\n")
                for label in ("exceeded", "not_exceeded", "invalid", "Total"):
                    cells = []
                    for t in tiers:
                        d = pooled[t]
                        if label == "Total":
                            cells.append(f"{d['total']}")
                        else:
                            key = label
                            tot = d["total"]
                            v = d.get(key, 0)
                            pct = (v / tot * 100) if tot else 0.0
                            cells.append(f"{v}/{tot} ({pct:.1f}%)")
                    f.write(f"  {label:20} {cells[0]:>20} {cells[1]:>20} {cells[2]:>20}\n")
                    if label == "invalid":
                        f.write(f"  {'-' * 86}\n")
                f.write(f"  {'-' * 86}\n\n")

                if pooled_lang:
                    for lang in sorted(pooled_lang.keys()):
                        lang_pool = pooled_lang[lang]
                        f.write("  --- Per-Tier Breakdown (binary_exceeded; "
                                f"language={lang}; pooled across {n} strategies) ---\n")
                        f.write(f"  {'':20} {'fast':>20} {'medium':>20} {'slow':>20}\n")
                        f.write(f"  {'-' * 86}\n")
                        for label in ("exceeded", "not_exceeded", "invalid", "Total"):
                            cells = []
                            for t in tiers:
                                d = lang_pool[t]
                                if label == "Total":
                                    cells.append(f"{d['total']}")
                                else:
                                    key = label
                                    tot = d["total"]
                                    v = d.get(key, 0)
                                    pct = (v / tot * 100) if tot else 0.0
                                    cells.append(f"{v}/{tot} ({pct:.1f}%)")
                            f.write(f"  {label:20} {cells[0]:>20} {cells[1]:>20} {cells[2]:>20}\n")
                            if label == "invalid":
                                f.write(f"  {'-' * 86}\n")
                        f.write(f"  {'-' * 86}\n\n")

            per_tier_rows_present = any(
                p["binary"].get("per_tier") for p in parsed_per_strategy
            )
            if per_tier_rows_present and not _used_raw_per_tier:
                f.write("  --- Per-Tier Breakdown (binary_exceeded, averaged hit/denom; "
                        "denom = generated TCs per tier) ---\n")
                f.write(f"  {'':20} {'fast':>18} {'medium':>18} {'slow':>18} {'All':>18}\n")
                f.write(f"  {'-' * 96}\n")
                for label in ("exceeded", "not_exceeded", "invalid", "Total"):
                    cells = []
                    any_present = False
                    for tier in ("fast", "medium", "slow", "all"):
                        triples = []
                        for p in parsed_per_strategy:
                            row = p["binary"].get("per_tier", {}).get(label)
                            if row and row.get(tier):
                                triples.append(row[tier])
                        if triples:
                            any_present = True
                            hits = _avg_or_none([t[0] for t in triples]) or 0.0
                            denoms = [t[1] for t in triples if t[1] is not None]
                            denom_avg = (sum(denoms) / len(denoms)) if denoms else None
                            pcts = [t[2] for t in triples if t[2] is not None]
                            pct_avg = (sum(pcts) / len(pcts)) if pcts else None
                            if denom_avg is not None and pct_avg is not None:
                                cells.append(f"{hits:.1f}/{denom_avg:.1f} ({pct_avg:.1f}%)")
                            elif denom_avg is None:
                                cells.append(f"{hits:.1f}")
                            else:
                                cells.append(f"{hits:.1f}/{denom_avg:.1f}")
                        else:
                            cells.append("—")
                    if any_present:
                        f.write(f"  {label:20} {cells[0]:>18} {cells[1]:>18} {cells[2]:>18} {cells[3]:>18}\n")
                    if label == "invalid":
                        f.write(f"  {'-' * 96}\n")
                f.write(f"  {'-' * 96}\n\n")

            per_tier_norm_present = any(
                p["binary"].get("per_tier_norm") for p in parsed_per_strategy
            )
            if per_tier_norm_present:
                denom_pt = next((p["binary"].get("per_tier_norm_denom_per_tier")
                                 for p in parsed_per_strategy
                                 if p["binary"].get("per_tier_norm_denom_per_tier") is not None),
                                None)
                pooled_pt = denom_pt * n if denom_pt else None
                if pooled_pt is not None:
                    f.write(f"  --- Per-Tier Breakdown (TPR_norm style; "
                            f"pooled across {n} strategies; "
                            f"denom_per_tier={pooled_pt} = {denom_pt}×{n}) ---\n")
                else:
                    f.write("  --- Per-Tier Breakdown (TPR_norm style; pooled) ---\n")
                f.write(f"  {'':32} {'fast':>22} {'medium':>22} {'slow':>22} {'All':>24}\n")
                f.write(f"  {'-' * 126}\n")
                def _collect(label_name: str, source_key: str) -> dict:
                    out = {}
                    for tier in ("fast", "medium", "slow", "all"):
                        triples = []
                        for p in parsed_per_strategy:
                            row = p[source_key].get("per_tier_norm", {}).get(label_name)
                            if row and row.get(tier):
                                triples.append(row[tier])
                        if not triples:
                            out[tier] = None
                            continue
                        hits = sum(t[0] for t in triples if t[0] is not None)
                        denoms = [t[1] for t in triples if t[1] is not None]
                        denom = denoms[0] if denoms else None
                        pooled = denom * len(triples) if denom else None
                        out[tier] = (hits, pooled)
                    return out

                def _fmt_cells(per_tier: dict) -> list:
                    cells = []
                    for tier in ("fast", "medium", "slow", "all"):
                        v = per_tier.get(tier)
                        if v is None:
                            cells.append("—"); continue
                        hits, pooled = v
                        if pooled:
                            pct = hits / pooled * 100
                            cells.append(f"{int(hits)}/{pooled} ({pct:.1f}%)")
                        else:
                            cells.append(f"{int(hits)}")
                    return cells

                _b_exceeded     = _collect("exceeded_norm",     "binary")
                _b_not_exceeded = _collect("not_exceeded_norm", "binary")
                _t_compliant    = _collect("TLE_compliant_norm", "tle")

                if any(_b_exceeded.get(t) for t in ("fast","medium","slow","all")):
                    cells = _fmt_cells(_b_exceeded)
                    f.write(f"  {'exceeded_norm':32} {cells[0]:>22} {cells[1]:>22} {cells[2]:>22} {cells[3]:>24}\n")

                if any(_b_exceeded.get(t) or _t_compliant.get(t) for t in ("fast","medium","slow","all")):
                    combined = {}
                    for tier in ("fast","medium","slow","all"):
                        be = _b_exceeded.get(tier); tc = _t_compliant.get(tier)
                        if be is None and tc is None:
                            combined[tier] = None
                        else:
                            hits = (be[0] if be else 0) + (tc[0] if tc else 0)
                            pooled = (be[1] if be else (tc[1] if tc else None))
                            combined[tier] = (hits, pooled)
                    cells = _fmt_cells(combined)
                    f.write(f"  {'exceeded_norm_with_TLE_compliant':32} {cells[0]:>22} {cells[1]:>22} {cells[2]:>22} {cells[3]:>24}\n")

                if any(_b_not_exceeded.get(t) for t in ("fast","medium","slow","all")):
                    cells = _fmt_cells(_b_not_exceeded)
                    f.write(f"  {'not_exceeded_norm':32} {cells[0]:>22} {cells[1]:>22} {cells[2]:>22} {cells[3]:>24}\n")
                f.write(f"  {'-' * 126}\n\n")

                def _collect_cc(label_name: str, scope_key: str) -> dict:
                    out = {}
                    for tier in ("fast", "medium", "slow", "all"):
                        triples = []
                        for p in parsed_per_strategy:
                            row = (p.get("binary", {})
                                    .get(scope_key, {})
                                    .get(label_name))
                            if row and row.get(tier):
                                triples.append(row[tier])
                        if not triples:
                            out[tier] = None
                            continue
                        hits = sum(t[0] for t in triples if t[0] is not None)
                        denoms = [t[1] for t in triples if t[1] is not None]
                        denom = denoms[0] if denoms else None
                        pooled = denom * len(triples) if denom else None
                        out[tier] = (hits, pooled)
                    return out

                _CC_SCOPES = (
                    ("All types", "per_tier_norm_cc_all",
                     "A TC is counted only when its stdin parses against the "
                     "parsed_structures schema AND respects every LLM-extracted "
                     "bound (range/length/product/sum/sum_over_tc/distinct/"
                     "charset/power/other)."),
                    ("Boundary only", "per_tier_norm_cc_boundary",
                     "Same TC pool, but compliance is checked ONLY against "
                     "boundary-style constraints (range/length/product/sum). "
                     "Non-boundary constraints (distinct/charset/sum_over_tc/"
                     "power/other) are ignored."),
                )
                _CC_ROW_LABELS = (
                    "exceeded_norm_constraint_valid",
                    "exceeded_norm_constraint_valid_with_TLE_compliant",
                    "not_exceeded_norm_constraint_valid",
                    "constraint_violated",
                    "compliance_unknown",
                )
                for _scope_label, _scope_key, _scope_blurb in _CC_SCOPES:
                    has_any = any(
                        any(p.get("binary", {}).get(_scope_key, {}).get(lbl)
                            for lbl in _CC_ROW_LABELS)
                        for p in parsed_per_strategy
                    )
                    if not has_any:
                        continue
                    if pooled_pt is not None:
                        f.write(f"  --- Per-Tier Breakdown with Constraints check "
                                f"({_scope_label}; TPR_norm style; pooled across "
                                f"{n} strategies; denom_per_tier={pooled_pt} = "
                                f"{denom_pt}×{n}) ---\n")
                    else:
                        f.write(f"  --- Per-Tier Breakdown with Constraints check "
                                f"({_scope_label}; TPR_norm style; pooled) ---\n")
                    f.write(f"  Constraint source: LLM-extracted via generate_constraints.py.\n")
                    f.write(f"  {_scope_blurb}\n")
                    f.write(f"  {'':52} {'fast':>22} {'medium':>22} {'slow':>22} {'All':>24}\n")
                    f.write(f"  {'-' * 146}\n")
                    for _row_label in _CC_ROW_LABELS:
                        agg = _collect_cc(_row_label, _scope_key)
                        if not any(agg.get(t) for t in ("fast","medium","slow","all")):
                            continue
                        cells = _fmt_cells(agg)
                        f.write(f"  {_row_label:52} {cells[0]:>22} {cells[1]:>22} "
                                f"{cells[2]:>22} {cells[3]:>24}\n")
                    f.write(f"  {'-' * 146}\n\n")

        tle_present = any(p.get("tle", {}).get("category")
                          or p.get("tle", {}).get("per_tier_norm")
                          for p in parsed_per_strategy)
        if tle_present and _EMIT_TLE_CRITERION:
            f.write("============================================================\n")
            f.write("**Criterion: TLE on Constraint-Compliant TC — Category & Per-Tier (averaged)**\n")
            f.write("  TC counts as TLE_compliant only when verdict=timelimit AND its boundary "
                    "respects every LLM-extracted constraint.\n\n")

            f.write(f"  {'Category':<20} {'Avg/strat':>10} {'%':>7}  "
                    f"{'Sum (N=' + str(n) + ')':>14}  {'Sum / Total':>17}  {'%':>6}\n")
            f.write(f"  {'-' * 86}\n")
            grand_total = 0
            for cat in ("tle_compliant", "tle_violating"):
                rows = [p["tle"]["category"].get(cat) for p in parsed_per_strategy]
                rows = [r for r in rows if r]
                if not rows:
                    continue
                avg_per = _avg_or_none([r["count"] for r in rows]) or 0.0
                avg_pct = _avg_or_none([r.get("pct", 0) for r in rows]) or 0.0
                sum_n = int(round(avg_per * n))
                grand_total += sum_n
                f.write(f"  {cat:<20} {avg_per:>10.1f} {avg_pct:>6.1f}%  "
                        f"{sum_n:>14d}  {'-':>17}  {'-':>6}\n")
            f.write(f"  {'-' * 86}\n")
            f.write(f"  {'Grand total':<20} {'-':>10} {'-':>7}  {grand_total:>14d}\n\n")

            denom_pt = next((p["tle"].get("per_tier_norm_denom_per_tier")
                             for p in parsed_per_strategy
                             if p["tle"].get("per_tier_norm_denom_per_tier") is not None),
                            None)
            pooled_pt = denom_pt * n if denom_pt else None
            if pooled_pt is not None:
                f.write(f"  --- Per-Tier Breakdown (TPR_norm style; pooled across {n} strategies; "
                        f"denom_per_tier={pooled_pt} = {denom_pt}×{n}) ---\n")
            else:
                f.write("  --- Per-Tier Breakdown (TPR_norm style; pooled) ---\n")
            f.write(f"  {'':22} {'fast':>22} {'medium':>22} {'slow':>22} {'All':>24}\n")
            f.write(f"  {'-' * 116}\n")
            for label in ("TLE_compliant_norm", "TLE_violating_norm"):
                cells = []
                any_present = False
                for tier in ("fast", "medium", "slow", "all"):
                    triples = []
                    for p in parsed_per_strategy:
                        row = p["tle"].get("per_tier_norm", {}).get(label)
                        if row and row.get(tier):
                            triples.append(row[tier])
                    if triples:
                        any_present = True
                        hits_sum = sum(t[0] for t in triples if t[0] is not None)
                        denoms = [t[1] for t in triples if t[1] is not None]
                        denom = (denoms[0] if denoms else None)
                        pooled_denom = denom * len(triples) if denom else None
                        pct = (hits_sum / pooled_denom * 100) if pooled_denom else None
                        if pooled_denom is not None and pct is not None:
                            cells.append(f"{int(hits_sum)}/{pooled_denom} ({pct:.1f}%)")
                        else:
                            cells.append(f"{int(hits_sum)}")
                    else:
                        cells.append("—")
                if any_present:
                    f.write(f"  {label:22} {cells[0]:>22} {cells[1]:>22} {cells[2]:>22} {cells[3]:>24}\n")
            f.write(f"  {'-' * 116}\n\n")

        if any(p.get("verdict") for p in parsed_per_strategy):
            f.write("============================================================\n")
            f.write("**Verdict Distribution** (averaged)\n")
            for v in ("SLOWER_HEAVIER", "FASTER_HEAVIER", "MORE_POLARIZED", "SIMILAR"):
                avg_v = _avg_or_none([p["verdict"].get(v, 0) for p in parsed_per_strategy])
                if avg_v is not None:
                    f.write(f"  {v}: {avg_v:.1f} problems (avg)\n")
            nv = _avg_or_none([p["no_verdict"] for p in parsed_per_strategy
                               if p["no_verdict"] is not None])
            if nv is not None:
                f.write(f"  No verdict: {nv:.1f} problems (avg)\n")
            f.write("\n")

        f.write("=" * 70 + "\n")
        f.write("Per-Strategy Raw Values (for inspection)\n")
        f.write("=" * 70 + "\n")
        for i, sname in enumerate(strategy_names):
            p = parsed_per_strategy[i]
            f.write(f"\n[{sname}]\n")
            tca = p["tc_accuracy"]
            if tca.get("accuracy") is not None:
                f.write(f"  TC Accuracy: {tca['accuracy']:.4f}  "
                        f"(valid={tca.get('valid')}/expected={tca.get('expected_total')})\n")
            for cn in present_crits:
                if cn == "Binary Reference Exceeded":
                    cat = p["binary"].get("category", {})
                    exc = cat.get("exceeded", {})
                    if exc:
                        f.write(f"  Binary exceeded: {exc.get('count')} "
                                f"({exc.get('pct'):.1f}%, valid_only={exc.get('vpct')}%)\n")
                    pt = p["binary"].get("per_tier_norm", {}).get("exceeded_norm", {})
                    if pt:
                        parts = []
                        for tier in ("fast", "medium", "slow"):
                            tcell = pt.get(tier)
                            if tcell:
                                parts.append(f"{tier}={tcell[0]}/{tcell[1]}")
                        if parts:
                            f.write(f"  Per-tier exceeded_norm: {', '.join(parts)}\n")
                else:
                    crit = p["criteria"].get(cn, {})
                    parts = []
                    for tier in ("fast", "medium", "slow"):
                        t = crit.get("tpr_norm", {}).get(tier)
                        if t:
                            parts.append(f"{tier}={t[0]:.4f}({t[1]}/{t[2]})")
                    if parts:
                        f.write(f"  {cn} TPR_norm: {', '.join(parts)}\n")

def _load_ref_timing_problems(ref_path: Path) -> list[dict]:
    if not ref_path.exists():
        return []
    try:
        text = ref_path.read_text(encoding="utf-8")
    except Exception:
        return []
    if str(ref_path).endswith(".jsonl"):
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "problem":
                out.append(d)
        return out
    try:
        return json.loads(text).get("problems") or []
    except json.JSONDecodeError:
        return []

def _intersected_per_strategy_max_ref_time(parsed_list: list, lang_label: str) -> Optional[list]:
    from statistics import mean as _mean, median as _median
    if not parsed_list:
        return None
    target_langs: Optional[list[str]]
    if lang_label == "cpp":
        target_langs = ["cpp"]
    elif lang_label == "python":
        target_langs = ["python3"]
    elif lang_label == "java":
        target_langs = ["java"]
    else:
        target_langs = None

    per_strategy_pp: list[dict] = []
    for parsed in parsed_list:
        ref_path_str = parsed.get("header", {}).get("ref_path")
        if not ref_path_str:
            return None
        rp = Path(ref_path_str)
        problems = _load_ref_timing_problems(rp)
        if not problems:
            return None
        pp: dict[str, dict[str, float]] = {}
        for prob in problems:
            name = prob.get("name") or prob.get("problem_id")
            if not name:
                continue
            mrt = get_per_language_max_ref_time(prob)
            if not mrt:
                continue
            if target_langs is not None:
                mrt = {l: v for l, v in mrt.items() if l in target_langs}
                if not mrt:
                    continue
            pp[name] = mrt
        per_strategy_pp.append(pp)

    common = None
    for pp in per_strategy_pp:
        s = set(pp.keys())
        common = s if common is None else (common & s)
    if not common:
        return None

    def _p90(xs):
        if not xs:
            return 0.0
        xs2 = sorted(xs)
        return xs2[max(0, int(round(0.9 * (len(xs2) - 1))))]

    out_list: list[dict] = []
    for pp in per_strategy_pp:
        all_vals: list[float] = []
        by_lang: dict[str, list[float]] = {}
        for name in common:
            for l, v in pp.get(name, {}).items():
                if v is None or v <= 0:
                    continue
                all_vals.append(float(v))
                by_lang.setdefault(l, []).append(float(v))
        scope_stats: dict[str, dict] = {}
        if all_vals:
            scope_stats["overall"] = {
                "N": len(all_vals), "avg": _mean(all_vals), "median": _median(all_vals),
                "min": min(all_vals), "max": max(all_vals), "p90": _p90(all_vals),
            }
        for l in sorted(by_lang):
            vs = by_lang[l]
            scope_stats[l] = {
                "N": len(vs), "avg": _mean(vs), "median": _median(vs),
                "min": min(vs), "max": max(vs), "p90": _p90(vs),
            }
        out_list.append(scope_stats)
    return out_list

def compute_total_avg(args):
    model_dir = args.model.replace("/", "_") if args.model else "unknown"

    _src = getattr(args, "source", "Base")
    _src_root = _src.split("/", 1)[0] if isinstance(_src, str) else _src
    if _src == "Base" and getattr(args, "prompt", None):
        parent_dir = (Path(args.output_root) / "codecontests" / "baseline"
                      / args.prompt / args.split / model_dir)
    elif _src_root in ("wedge", "evalperf_sas"):
        parent_dir = Path(args.output_root) / "codecontests"
        for part in _src.split("/"):
            parent_dir = parent_dir / part
        parent_dir = parent_dir / args.split
    else:
        parent_dir = Path(args.output_root) / "codecontests" / "our_method" / args.split / model_dir
        if getattr(args, "refinement_prompt", None):
            parent_dir = parent_dir / args.refinement_prompt
        if getattr(args, "llm_model", None):
            parent_dir = parent_dir / args.llm_model

    lang_views = ["total", "cpp", "python", "java"]
    slow_ratio_threshold = getattr(args, "slow_ratio_threshold", 0.9) or 0.9

    _STD_STRATS = ("fast_solution", "slow_solution",
                   "random1_solution", "random2_solution", "random3_solution")
    _disk_strats: list[str] = []
    if parent_dir.exists():
        for _s in _STD_STRATS:
            if (parent_dir / _s).is_dir():
                _disk_strats.append(_s)
        for _p in sorted(parent_dir.iterdir()):
            if _p.is_dir() and _p.name.endswith("_solution") and _p.name not in _disk_strats:
                _disk_strats.append(_p.name)
    if _disk_strats:
        effective_strategies = _disk_strats
        log.info("[total_avg] Auto-discovered strategies on disk: %s",
                 ", ".join(effective_strategies))
    else:
        effective_strategies = list(args.strategies)
        log.info("[total_avg] No strategy dirs on disk under %s; using args.strategies=%s",
                 parent_dir, effective_strategies)

    for lang_label in lang_views:
        parsed_list: list[dict] = []
        strategy_names: list[str] = []
        binary_per_tier_pooled: list[dict] = []

        for strategy in effective_strategies:
            summary_path = parent_dir / strategy / lang_label / "summary.txt"
            if summary_path.exists():
                try:
                    parsed = _parse_summary_full(summary_path)
                except Exception as e:
                    log.warning("[total_avg] Failed to parse %s: %s", summary_path, e)
                    continue
                parsed_list.append(parsed)
                strategy_names.append(strategy)
                log.info("[total_avg] Loaded %s/%s", strategy, lang_label)

                dc_path = summary_path.parent / "difficulty_comparison.json"
                if dc_path.exists():
                    try:
                        binary_per_tier_pooled.append(
                            _binary_per_tier_from_difficulty_json(
                                dc_path,
                                eval_tiers=getattr(args, "eval_tiers_set", None)))
                    except Exception as e:
                        log.warning("[total_avg] Failed to load %s: %s", dc_path, e)
                        binary_per_tier_pooled.append({})

                    try:
                        tle_dict = parsed.setdefault("tle", {})
                        if not tle_dict.get("per_tier_norm"):
                            n_ref = tle_dict.get("per_tier_norm_denom_per_tier")
                            tcs_pt = None
                            if not n_ref:
                                bin_dict = parsed.get("binary", {}) or {}
                                bin_pt = bin_dict.get("per_tier_norm_denom_per_tier")
                                if bin_pt:
                                    n_ref = bin_pt
                                    tcs_pt = 1
                            tle_pt = _tle_compliant_per_tier_from_difficulty_json(
                                dc_path,
                                num_ref_problems=n_ref if (n_ref and tcs_pt is None) else None,
                                tcs_per_tier=tcs_pt,
                                eval_tiers=getattr(args, "eval_tiers_set", None))
                            if tle_pt:
                                tle_dict["per_tier_norm"] = tle_pt
                                if n_ref and tle_dict.get("per_tier_norm_denom_per_tier") is None:
                                    tle_dict["per_tier_norm_denom_per_tier"] = n_ref
                    except Exception as e:
                        log.warning("[total_avg] Failed to backfill TLE per-tier from %s: %s",
                                    dc_path, e)
                else:
                    binary_per_tier_pooled.append({})
            else:
                log.warning("[total_avg] Summary not found: %s", summary_path)

        if not parsed_list:
            log.warning("[total_avg] No strategy summaries found for %s — skipping", lang_label)
            continue

        intersected = _intersected_per_strategy_max_ref_time(parsed_list, lang_label)
        if intersected:
            for parsed, inter in zip(parsed_list, intersected):
                parsed["max_ref_time_intersected"] = inter
            log.info("[total_avg] Reference-max intersection for %s: scope=%d problems",
                     lang_label, intersected[0].get("overall", {}).get("N", 0))
        else:
            log.info("[total_avg] Could not intersect max_ref_time for %s — "
                     "using per-strategy own scope", lang_label)

        avg_dir = parent_dir / "total_avg" / lang_label
        avg_dir.mkdir(parents=True, exist_ok=True)
        out_path = avg_dir / "summary.txt"
        _render_avg_summary(out_path, parsed_list, strategy_names, lang_label,
                            slow_ratio_threshold=slow_ratio_threshold,
                            binary_per_tier_pooled=binary_per_tier_pooled)
        log.info("[total_avg] Wrote %s (averaged across %d strategies)",
                 out_path, len(parsed_list))

    log.info("[total_avg] Done.")

def export_final_selected_solutions(args):
    import json as _json

    sel_dir = Path(args.selected_solutions_dir)
    sel_path = sel_dir / f"selected_solutions_{args.split}.jsonl"
    if not sel_path.exists():
        log.error("selected_solutions not found: %s", sel_path)
        return

    problems = []
    with open(sel_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = _json.loads(line)
            if rec.get("type") == "problem":
                problems.append(rec)

    log.info("Loaded %d problems from %s", len(problems), sel_path)

    try:
        from datasets import load_dataset as _hf_load
        ds = _hf_load("deepmind/code_contests", split=args.split,
                       cache_dir=args.hf_cache_dir)
        ds_by_idx = {i: ex for i, ex in enumerate(ds)}
    except Exception as e:
        log.error("Failed to load HF dataset: %s", e)
        return

    seed = getattr(args, "multi_solution_seed", 42)
    strategies = ["fast_solution", "slow_solution",
                  "random1_solution", "random2_solution", "random3_solution"]

    out_dir = Path(args.final_selected_output_dir) / args.split
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "final_selected_solutions.jsonl"

    def _avg_rt(s):
        runs = s.get("runs", [])
        if not runs:
            return float("inf")
        return sum(
            r["run_time"] if r.get("run_time") is not None else float("inf")
            for r in runs
        ) / len(runs)

    def _pick_by_strategy(sorted_sols, strategy, problem_idx=0):
        if not sorted_sols:
            return None
        if strategy == "fast_solution":
            return sorted_sols[0]
        elif strategy == "slow_solution":
            return sorted_sols[-1]
        elif strategy.startswith("random"):
            n = int(strategy.replace("random", "").replace("_solution", ""))
            rng = random.Random(seed + n + problem_idx * 1000)
            if len(sorted_sols) <= 2:
                return rng.choice(sorted_sols)
            middle = sorted_sols[1:-1]
            return rng.choice(middle)
        return sorted_sols[0]

    written = 0
    with open(out_path, "w", encoding="utf-8") as f:
        meta = {
            "type": "metadata",
            "split": args.split,
            "seed": seed,
            "strategies": strategies,
        }
        f.write(_json.dumps(meta, ensure_ascii=False) + "\n")

        for prob in problems:
            idx = prob["index"]
            name = prob.get("name", f"problem_{idx}")

            example = ds_by_idx.get(idx)
            if not example:
                continue
            sol_texts = example.get("solutions", {}).get("solution", [])

            strategies_map = {}
            for strategy in strategies:
                strategies_map[strategy] = {}
                for lang_id, sol_list in prob.get("solutions", {}).items():
                    ac_solutions = [s for s in sol_list if s.get("verdict") == "AC"]
                    if not ac_solutions:
                        continue
                    sorted_sols = sorted(ac_solutions, key=_avg_rt)
                    picked = _pick_by_strategy(sorted_sols, strategy, problem_idx=idx)
                    if picked and picked["sol_idx"] < len(sol_texts):
                        strategies_map[strategy][lang_id] = {
                            "sol_idx": picked["sol_idx"],
                            "avg_runtime": _avg_rt(picked),
                            "max_run_time": picked.get("max_run_time"),
                            "code": sol_texts[picked["sol_idx"]],
                        }

            record = {
                "type": "problem",
                "index": idx,
                "name": name,
                "strategies": strategies_map,
            }
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    log.info("Exported %d problems to %s", written, out_path)

def main():
    parser = argparse.ArgumentParser(
        description="Compare difficulty distributions using reference (criterion) and evaluand timing"
    )
    parser.add_argument("--reference_timing", type=str, default=None,
                        help="Path to reference (criterion) codecontests_timing_*.json "
                             "(required unless --compute_total_avg or --export_final_selected)")
    parser.add_argument("--evaluand_timing", type=str, default=None,
                        help="Path to evaluand (e.g. slow TC) codecontests_timing_*.json "
                             "(required unless --compute_total_avg or --export_final_selected)")
    parser.add_argument("--split", type=str, default="test",
                        help="Dataset split name. Supports compound splits like "
                             "'test_valid' or 'train_valid_test' (underscore-joined).")
    parser.add_argument("--dataset", type=str, default="codecontests",
                        help="Dataset name")
    parser.add_argument("--source", type=str, default="",
                        help="Evaluand source label, e.g. Base or slow_testcases")
    parser.add_argument("--model", type=str, default="",
                        help="Model name (evaluand timing from {source}/{model}/)")
    parser.add_argument("--prompt", type=str, default="",
                        help="Instruction/prompt name (e.g. Testcase_generation_prompt). "
                             "Used in output path for Base source: "
                             "{output_root}/codecontests/baseline/{prompt}/{split}/{model}/")
    parser.add_argument("--llm_model", type=str, default="",
                        help="LLM model name for our_method source (e.g. gemini-3-flash-preview). "
                             "Used in output path: "
                             "{output_root}/codecontests/our_method/{split}/{mode}/{refinement_prompt}/{llm_model}/")
    parser.add_argument("--refinement_prompt", type=str, default="",
                        help="Refinement prompt name for our_method source "
                             "(e.g. slow_testcase_refinement_prompt_v2). "
                             "Used in output path between mode and llm_model: "
                             "{output_root}/codecontests/our_method/{split}/{mode}/{refinement_prompt}/{llm_model}/")
    parser.add_argument("--output_root", type=str,
                        default="evaluation",
                        help="Root output directory")
    parser.add_argument("--no_plot", action="store_true",
                        help="Skip visualization generation")
    parser.add_argument("--generator_output", type=str, default=None,
                        help="Path to custom testcases JSON (generator output) for target_tier mapping. "
                             "Enables TPR/TAA metrics by assigning intended tier per TC.")
    parser.add_argument("--generator_inputs_dir", type=str, default=None,
                        help="Path to inputs/ directory (fast/, medium/, slow/ subdirs) produced by "
                             "slow_testcase_generator.py. Used as fallback when --generator_output "
                             "JSON is unavailable. Builds tier map from directory structure.")

    parser.add_argument("--lef", action="store_true",
                        help="Compute Line Execution Frequency metric for Python3 solutions.")
    parser.add_argument("--selected_solutions", type=str, default=None,
                        help="Path to selected_solutions_*.jsonl (required with --lef). "
                             "Used to select best/slowest/random solutions per problem.")
    parser.add_argument("--tc_input_path", type=str, default=None,
                        help="Path to generator output JSON containing TC stdin inputs "
                             "(slow_testcases or Base judge_tc format). Required with --lef.")
    parser.add_argument("--parsed_structures_dir", type=str, default=None,
                        help="Path to parsed_structures/ directory (e.g. dataset/parsed_structures). "
                             "Required to expand boundary_slow_compact TCs (stdin=null) in LEF "
                             "profiling. If omitted, compact M1 TCs are silently skipped.")
    parser.add_argument("--llm_stdin_schema_path", type=str, default=None,
                        help="Path to LLM-extracted stdin schema JSONL "
                             "(output of generate_stdin_schema.py). When set, "
                             "this overrides parsed_structures for constraint-compliance "
                             "checking — the LLM schema (extracted from AC solution code) "
                             "tends to be more accurate than the rule-based parser. "
                             "Format: one JSON per line with {index, name, structure: {...}}.")
    parser.add_argument("--hf_cache_dir", type=str,
                        default=None,
                        help="HuggingFace dataset cache directory for loading source codes.")
    parser.add_argument("--lef_seed", type=int, default=42,
                        help="Random seed for selecting 3 random solutions (default: 42).")
    parser.add_argument("--lef_num_random", type=int, default=3,
                        help="Number of random solutions to sample (default: 3).")
    parser.add_argument("--lef_timeout", type=float, default=5.0,
                        help="Timeout in seconds per TC profiling run (default: 5.0).")
    parser.add_argument("--lef_workers", type=int, default=8,
                        help="Number of parallel workers for subprocess profiling (default: 8).")
    parser.add_argument("--tcs_per_tier", type=int, default=0,
                        help="Expected number of test cases per tier per problem (e.g. 5). "
                             "Used as the denominator for TPR_norm. "
                             "0 (default) = infer from generator tier map.")
    parser.add_argument("--slow_ratio_threshold", type=float, default=0.9,
                        help="Threshold for ratio-based slow classification. "
                             "A TC is 'slow' if run_time >= max_ref_time * threshold. "
                             "Default: 0.9 (90%% of max reference time).")
    parser.add_argument("--solution_strategy", type=str, default=None,
                        help="Solution strategy name (fast_solution/slow_solution/random{1,2,3}_solution). "
                             "When set, output path includes strategy subdirectory.")
    parser.add_argument("--compute_total_avg", action="store_true",
                        help="Compute total_avg by averaging summary metrics across all strategies.")
    parser.add_argument("--strategies", type=str, nargs="+",
                        default=["fast_solution", "slow_solution",
                                 "random1_solution", "random2_solution", "random3_solution"],
                        help="Strategies to average over (used with --compute_total_avg).")
    parser.add_argument("--export_final_selected", action="store_true",
                        help="Export final_selected_solutions JSONL with strategy->lang->sol_idx mapping.")
    parser.add_argument("--selected_solutions_dir", type=str, default=None,
                        help="Directory containing selected_solutions_*.jsonl.")
    parser.add_argument("--final_selected_output_dir", type=str, default=None,
                        help="Output directory for final_selected_solutions.")
    parser.add_argument("--multi_solution_seed", type=int, default=42,
                        help="Random seed for multi-solution selection (default: 42).")
    parser.add_argument("--eval_tiers", type=str, default="fast,medium,slow",
                        help="Comma-separated tiers to evaluate: fast,medium,slow. "
                             "TCs with target_tier outside this set are skipped from "
                             "all per-tier classifications and breakdowns. "
                             "Default: 'fast,medium,slow' (all tiers).")
    parser.add_argument("--llm_constraints_path", type=str, default=None,
                        help="Path to LLM-extracted constraints JSONL produced by "
                             "generate_constraints.py. Required for the new "
                             "'TLE on Constraint-Compliant TC' metric. Format: one JSON "
                             "record per problem with 'index' and 'constraints' fields.")
    parser.add_argument("--llm_constraints_path_boundary", type=str, default=None,
                        help="Optional path to a SECOND LLM-extracted constraints JSONL "
                             "containing only boundary-style constraints (range/length/"
                             "product/sum). When set, summary.txt gets an additional "
                             "'Per-Tier Breakdown with Constraints check (Boundary only)' "
                             "block alongside the all-types block. Same JSONL schema as "
                             "--llm_constraints_path.")

    args = parser.parse_args()
    _et_raw = (args.eval_tiers or "").strip()
    if _et_raw and _et_raw.lower() != "all":
        args.eval_tiers_set = {t.strip() for t in _et_raw.split(",") if t.strip()}
    else:
        args.eval_tiers_set = None

    if args.export_final_selected:
        export_final_selected_solutions(args)
        return

    if args.compute_total_avg:
        compute_total_avg(args)
        return

    if not args.reference_timing or not args.evaluand_timing:
        log.error("--reference_timing and --evaluand_timing are required for evaluation mode")
        return

    reference_path = Path(args.reference_timing)
    evaluand_path = Path(args.evaluand_timing)

    if not reference_path.exists():
        log.error("Reference timing file not found: %s", reference_path)
        return
    if not evaluand_path.exists():
        log.error("Evaluand timing file not found: %s", evaluand_path)
        return

    generator_tier_map = {}
    if args.generator_output:
        gen_path = Path(args.generator_output)
        if gen_path.exists():
            generator_tier_map = load_generator_tier_map(gen_path)
        else:
            log.warning("Generator output not found: %s", gen_path)
    if not generator_tier_map and args.generator_inputs_dir:
        inputs_dir_path = Path(args.generator_inputs_dir)
        if inputs_dir_path.exists():
            generator_tier_map = load_generator_tier_map_from_inputs_dir(inputs_dir_path)
        else:
            log.warning("generator_inputs_dir not found: %s", inputs_dir_path)

    llm_constraints_by_idx = _load_llm_constraints_jsonl(
        getattr(args, "llm_constraints_path", None) or "")
    llm_constraints_boundary_by_idx = _load_llm_constraints_jsonl(
        getattr(args, "llm_constraints_path_boundary", None) or "")
    if llm_constraints_boundary_by_idx:
        log.info("Loaded boundary-only LLM constraints: %d problems from %s",
                 len(llm_constraints_boundary_by_idx),
                 args.llm_constraints_path_boundary)
    structures_by_idx: dict = {}
    stdin_ordinal_map: dict = {}
    if llm_constraints_by_idx or llm_constraints_boundary_by_idx:
        _llm_schema_path = getattr(args, "llm_stdin_schema_path", None)
        if _llm_schema_path and Path(_llm_schema_path).exists():
            try:
                with open(_llm_schema_path, encoding="utf-8") as _f:
                    _n_loaded = 0
                    for _line in _f:
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            _rec = json.loads(_line)
                        except json.JSONDecodeError:
                            continue
                        _idx = _rec.get("index")
                        _struct = _rec.get("structure")
                        if _idx is None or not _struct:
                            continue
                        structures_by_idx[int(_idx)] = _struct
                        _n_loaded += 1
                log.info("Loaded LLM stdin schemas: %d problems from %s",
                         _n_loaded, _llm_schema_path)
            except Exception as _e:
                log.warning("Failed to load LLM stdin schema (%s): %s — "
                            "falling back to parsed_structures", _llm_schema_path, _e)
                structures_by_idx = {}
        if not structures_by_idx and getattr(args, "parsed_structures_dir", None):
            structures_by_idx = _load_structures_by_idx(
                args.parsed_structures_dir, args.split)
            log.info("Loaded structures from parsed_structures: %d problems",
                     len(structures_by_idx))
        if args.generator_inputs_dir and Path(args.generator_inputs_dir).exists():
            try:
                _compact_features = _load_compact_m1_features(
                    args.parsed_structures_dir or "", args.split)
            except Exception:
                _compact_features = {}
            stdin_ordinal_map = _build_stdin_ordinal_map(
                Path(args.generator_inputs_dir), _compact_features)
            log.info("Built stdin ordinal map: %d problems", len(stdin_ordinal_map))
        elif args.generator_output and Path(args.generator_output).exists():
            try:
                with open(args.generator_output, encoding="utf-8") as _gf:
                    _gen_data = json.load(_gf)
                if isinstance(_gen_data, list):
                    for _entry in _gen_data:
                        if not isinstance(_entry, dict):
                            continue
                        _pname = _entry.get("name")
                        if not _pname:
                            continue
                        _tcs_by_tier = _entry.get("test_cases") or {}
                        _ord_map: dict = {}
                        _ord = 1
                        for _tier in ("fast", "medium", "slow"):
                            for _tc in _tcs_by_tier.get(_tier, []) or []:
                                if not isinstance(_tc, dict):
                                    continue
                                _stdin = _tc.get("input") or _tc.get("stdin") or ""
                                _ord_map[_ord] = _stdin
                                _ord += 1
                        if _ord_map:
                            stdin_ordinal_map[_pname] = _ord_map
                log.info("Built stdin ordinal map from --generator_output: %d problems",
                         len(stdin_ordinal_map))
            except Exception as _e:
                log.warning("Failed to build stdin map from --generator_output (%s): %s",
                            args.generator_output, _e)

    reference_data = load_timing_results(reference_path)
    evaluand_data = load_timing_results(evaluand_path)

    reference_probs = {(p.get("name") or str(p["index"])): p for p in reference_data["problems"]}
    evaluand_probs  = {(p.get("name") or str(p["index"])): p for p in evaluand_data["problems"]}

    all_prob_names = sorted(set(reference_probs.keys()) | set(evaluand_probs.keys()))
    log.info("Problems: %d reference, %d evaluand, %d combined",
             len(reference_probs), len(evaluand_probs), len(all_prob_names))

    model_dir = args.model.replace("/", "_") if args.model else "unknown"
    if args.source == "Base" and args.prompt:
        base_output_dir = (Path(args.output_root) / "codecontests" / "baseline"
                           / args.prompt / args.split / model_dir)
    elif args.source == "dataset_analysis":
        base_output_dir = Path(args.output_root) / "codecontests" / "dataset_analysis" / args.split
    elif args.source.split("/", 1)[0] in ("wedge", "evalperf_sas", "wedge_selected_solutions", "evalperf_sas_selected_solutions"):
        base_output_dir = Path(args.output_root) / "codecontests"
        for part in args.source.split("/"):
            base_output_dir = base_output_dir / part
        base_output_dir = base_output_dir / args.split
    else:
        our_method_path = Path(args.output_root) / "codecontests" / "our_method" / args.split / model_dir
        if args.refinement_prompt:
            our_method_path = our_method_path / args.refinement_prompt
        if args.llm_model:
            our_method_path = our_method_path / args.llm_model
        base_output_dir = our_method_path

    if args.solution_strategy:
        base_output_dir = base_output_dir / args.solution_strategy

    _csv_rows: list[dict] = []

    for lang_label, lang_ids in LANG_VIEWS:
        log.info("=== Language view: %s ===", lang_label)

        if lang_ids is not None:
            filt_ref_probs = {n: filter_prob_by_lang(p, lang_ids) for n, p in reference_probs.items()}
            filt_ev_probs  = {n: filter_prob_by_lang(p, lang_ids) for n, p in evaluand_probs.items()}
        else:
            filt_ref_probs = reference_probs
            filt_ev_probs  = evaluand_probs

        filt_ref_by_name: dict = {}
        ref_max_ref_times: dict = {}
        for n, p in filt_ref_probs.items():
            t = extract_problem_timing(p)
            if t:
                filt_ref_by_name[n] = t
            mrt = get_per_language_max_ref_time(p)
            if mrt:
                ref_max_ref_times[n] = mrt

        filt_ev_by_name: dict = {}
        for n, p in filt_ev_probs.items():
            t = extract_problem_timing(p)
            if t:
                filt_ev_by_name[n] = t

        filt_all_names = sorted(set(filt_ref_by_name.keys()) | set(filt_ev_by_name.keys()))
        if not filt_all_names:
            log.warning("[%s] No problems with timing data — skipping this view", lang_label)
            continue
        log.info("[%s] Problems: %d reference, %d evaluand, %d combined",
                 lang_label, len(filt_ref_by_name), len(filt_ev_by_name), len(filt_all_names))

        comparisons = []
        for name in filt_all_names:
            ref_timing = filt_ref_by_name.get(name)
            ev_timing  = filt_ev_by_name.get(name)
            ref = ref_timing or ev_timing
            if ref is None:
                continue
            prob_info = {"index": ref.get("index", 0), "name": ref.get("name", ""),
                         "timelimit": ref.get("timelimit", 0)}
            comp = compare_problem(ref_timing, ev_timing, prob_info)

            ref_prob = filt_ref_probs.get(name)
            ev_prob  = filt_ev_probs.get(name)
            if ref_prob and ev_prob:
                per_lang = classify_evaluand_by_reference_thresholds(ref_prob, ev_prob)
                if per_lang:
                    comp["evaluand_per_language_tiers"] = per_lang

            if generator_tier_map and name in generator_tier_map:
                tier_mapping = generator_tier_map[name]
                method_mapping = _generator_method_map.get(name, {})
                ev_info = comp.get("evaluand", {})
                if ev_info.get("status") == "ok":
                    for tc in ev_info.get("testcases", []):
                        tc_num = tc.get("testcase")
                        if tc_num in tier_mapping:
                            tc["target_tier"] = tier_mapping[tc_num]
                        if tc_num in method_mapping:
                            tc["method"] = method_mapping[tc_num]
                            tc["module"] = _classify_method(method_mapping[tc_num])

            if ref_prob and comp.get("evaluand", {}).get("status") == "ok":
                rb = get_per_language_tier_bounds(ref_prob)
                if rb and ev_timing:
                    raw_ev_tcs = ev_timing.get("testcases", [])
                    tl_tcs = comp["evaluand"].get("testcases", [])
                    _tl_target = {tc.get("testcase"): tc.get("target_tier") for tc in tl_tcs
                                  if tc.get("testcase") is not None and tc.get("target_tier")}
                    _tl_module = {tc.get("testcase"): tc.get("module") for tc in tl_tcs
                                  if tc.get("testcase") is not None and tc.get("module")}
                    for tc in raw_ev_tcs:
                        tc_id = tc.get("testcase")
                        if tc_id in _tl_target:
                            tc["target_tier"] = _tl_target[tc_id]
                        if tc_id in _tl_module:
                            tc["module"] = _tl_module[tc_id]
                    rb_cls = classify_ref_bounds(raw_ev_tcs, rb)
                    rb_counts_per_tc = _counts_per_unique_tc(rb_cls["testcases"])
                    comp["evaluand"]["ref_bounds_based"] = {
                        "thresholds": rb_cls["thresholds"],
                        "counts": rb_cls["counts"],
                        "counts_per_tc": rb_counts_per_tc,
                        "testcases": rb_cls["testcases"],
                    }

                    max_ref_times = get_per_language_max_ref_time(ref_prob)
                    if max_ref_times:
                        ratio_cls = classify_ratio_based(
                            raw_ev_tcs, max_ref_times,
                            threshold=args.slow_ratio_threshold)
                        ratio_counts_per_tc = _counts_per_unique_tc(ratio_cls["testcases"])
                        comp["evaluand"]["ratio_based"] = {
                            "thresholds": ratio_cls["thresholds"],
                            "counts": ratio_cls["counts"],
                            "counts_per_tc": ratio_counts_per_tc,
                            "testcases": ratio_cls["testcases"],
                        }

                        prob_idx = comp.get("index")
                        prob_name = comp.get("name")
                        _structure = structures_by_idx.get(prob_idx) if prob_idx is not None else None
                        _constraints = llm_constraints_by_idx.get(prob_idx) if prob_idx is not None else None
                        _constraints_boundary = (
                            llm_constraints_boundary_by_idx.get(prob_idx)
                            if prob_idx is not None else None)
                        _ord_map = stdin_ordinal_map.get(prob_name) if prob_name else None
                        _stdin_lookup = (lambda tc_id, m=_ord_map: m.get(tc_id) if m else None)

                        binary_cls = classify_binary_exceeded(
                            raw_ev_tcs, max_ref_times,
                            eval_tiers=getattr(args, "eval_tiers_set", None),
                            structure=_structure,
                            constraints=_constraints,
                            stdin_lookup=_stdin_lookup,
                            constraints_boundary=_constraints_boundary)
                        comp["evaluand"]["binary_exceeded"] = {
                            "thresholds": binary_cls["thresholds"],
                            "counts": binary_cls["counts"],
                            "testcases": binary_cls["testcases"],
                        }

                        tle_cls = classify_tle_compliant(
                            raw_ev_tcs, _structure, _constraints, _stdin_lookup,
                            eval_tiers=getattr(args, "eval_tiers_set", None))
                        comp["evaluand"]["tle_compliant"] = {
                            "counts": tle_cls["counts"],
                            "testcases": tle_cls["testcases"],
                        }

            comparisons.append(comp)

        view_output_dir = base_output_dir / lang_label
        view_output_dir.mkdir(parents=True, exist_ok=True)

        lef_by_problem: dict = {}
        lef_counts: dict = {}
        if lang_label == "python" and args.lef:
            view_per_problem_dir = view_output_dir / "per_problem"
            comp_dir_by_name = {
                comp["name"]: (comp["index"], _re.sub(r'[^\w]', '_', comp.get("name") or "unknown"))
                for comp in comparisons
                if comp.get("name") and comp.get("index") is not None
            }

            _has_tc_source = bool(args.tc_input_path) or bool(
                args.generator_inputs_dir and Path(args.generator_inputs_dir).exists())
            if not args.selected_solutions or not _has_tc_source:
                log.warning("[LEF] --lef requires --selected_solutions and "
                            "(--tc_input_path or --generator_inputs_dir); skipping LEF.")
            else:
                sel_path = Path(args.selected_solutions)
                tc_path = Path(args.tc_input_path) if args.tc_input_path else None
                if not sel_path.exists():
                    log.warning("[LEF] selected_solutions not found: %s", sel_path)
                elif tc_path is not None and not tc_path.exists():
                    log.warning("[LEF] tc_input_path not found: %s", tc_path)
                else:
                    log.info("[LEF] Loading selected solutions from %s", sel_path)
                    sel_sols_data = load_selected_solutions_for_lef(sel_path)
                    n_ref_total = len(sel_sols_data)

                    problem_sol_map: dict = {}
                    problem_lef_sols: dict = {}
                    for prob_name, lang_sols in sel_sols_data.items():
                        py3_sols = lang_sols.get("python3", [])
                        if not py3_sols:
                            continue
                        selected = select_lef_solutions(py3_sols, seed=args.lef_seed,
                                                        n_random=args.lef_num_random)
                        if selected:
                            problem_sol_map[prob_name] = {s["sol_idx"] for s in selected}
                            problem_lef_sols[prob_name] = selected
                    n_with_python = len(problem_lef_sols)
                    lef_counts["n_ref_total"] = n_ref_total
                    lef_counts["n_with_python"] = n_with_python

                    problems_to_compute: dict = {}
                    n_cached = 0
                    for prob_name, lef_sols in problem_lef_sols.items():
                        if prob_name in comp_dir_by_name:
                            idx, safe = comp_dir_by_name[prob_name]
                            cache_file = view_per_problem_dir / f"{idx:04d}_{safe}" / "lef.json"
                            if cache_file.exists():
                                try:
                                    with open(cache_file, encoding="utf-8") as f:
                                        lef_by_problem[prob_name] = json.load(f)
                                    n_cached += 1
                                    continue
                                except Exception:
                                    pass
                        problems_to_compute[prob_name] = lef_sols

                    if n_cached:
                        log.info("[LEF] Loaded %d cached, %d to compute",
                                 n_cached, len(problems_to_compute))
                        if not problems_to_compute:
                            lef_counts["n_with_tc"] = n_cached

                    if problems_to_compute:
                        log.info("[LEF] Loading Python3 source codes for %d problems "
                                 "from HF dataset (%s)...", len(problems_to_compute), args.split)
                        source_by_problem = load_hf_source_codes(
                            args.hf_cache_dir, args.split,
                            {n: {s["sol_idx"] for s in sols}
                             for n, sols in problems_to_compute.items()}
                        )

                        _compact_features: dict = {}
                        if getattr(args, 'parsed_structures_dir', None):
                            _compact_features = _load_compact_m1_features(
                                args.parsed_structures_dir, args.split)
                            log.info("[LEF] compact M1 features: %d problems", len(_compact_features))

                        tc_inputs_by_prob: dict = {}
                        if tc_path is not None:
                            log.info("[LEF] Preloading TC inputs from %s", tc_path)
                            try:
                                with open(tc_path, encoding="utf-8") as _f:
                                    _tc_data = json.load(_f)
                                for _prob in _tc_data:
                                    if not isinstance(_prob, dict):
                                        continue
                                    _name = _prob.get("name", "")
                                    if not _name or _name not in problems_to_compute:
                                        continue
                                    _prob_idx = _prob.get("index")
                                    if "stdin_texts" in _prob and _prob["stdin_texts"]:
                                        tc_inputs_by_prob[_name] = [t for t in _prob["stdin_texts"] if t]
                                    elif "test_cases" in _prob:
                                        _tcs = _prob["test_cases"]
                                        if isinstance(_tcs, dict):
                                            _tier_dict = {}
                                            for _tier in ("fast", "medium", "slow"):
                                                _vals = []
                                                for _tc in _tcs.get(_tier, []):
                                                    _inp = _expand_compact_m1_stdin(
                                                        _tc, _compact_features, _prob_idx)
                                                    if _inp:
                                                        _vals.append(_inp)
                                                if _vals:
                                                    _tier_dict[_tier] = _vals
                                            if _tier_dict:
                                                tc_inputs_by_prob[_name] = _tier_dict
                                        elif isinstance(_tcs, list):
                                            _inputs = []
                                            for _tc in _tcs:
                                                _inp = _expand_compact_m1_stdin(
                                                    _tc, _compact_features, _prob_idx)
                                                if _inp:
                                                    _inputs.append(_inp)
                                            if _inputs:
                                                tc_inputs_by_prob[_name] = _inputs
                            except Exception as _e:
                                log.warning("[LEF] Failed to preload TC inputs: %s", _e)
                        elif args.generator_inputs_dir:
                            _inputs_path = Path(args.generator_inputs_dir)
                            log.info("[LEF] Preloading TC inputs from inputs/ dir: %s", _inputs_path)
                            _all = load_tc_inputs_from_inputs_dir(_inputs_path, _compact_features)
                            tc_inputs_by_prob = {k: v for k, v in _all.items()
                                                  if k in problems_to_compute}
                        n_with_tc = n_cached + len(tc_inputs_by_prob)
                        lef_counts["n_with_tc"] = n_with_tc

                        log.info("[LEF] Profiling %d problems (workers=%d, timeout=%.1fs)...",
                                 len(problems_to_compute), args.lef_workers, args.lef_timeout)

                        if HAS_TQDM:
                            _iter = _tqdm(problems_to_compute.items(),
                                          total=len(problems_to_compute),
                                          desc="[LEF]", unit="prob")
                        else:
                            _iter = problems_to_compute.items()

                        n_computed = 0
                        for prob_name, lef_sols in _iter:
                            tc_inputs = tc_inputs_by_prob.get(prob_name, [])
                            if not tc_inputs:
                                if hasattr(_iter, "set_postfix"):
                                    _iter.set_postfix(cached=n_cached, computed=n_computed,
                                                      skip="no_tc")
                                continue
                            source_codes = source_by_problem.get(prob_name, {})
                            lef_result = compute_lef_for_problem(
                                prob_name, lef_sols, source_codes,
                                tc_inputs, args.lef_timeout, args.lef_workers,
                            )
                            if lef_result:
                                lef_by_problem[prob_name] = lef_result
                                n_computed += 1
                                if prob_name in comp_dir_by_name:
                                    idx, safe = comp_dir_by_name[prob_name]
                                    prob_dir = view_per_problem_dir / f"{idx:04d}_{safe}"
                                    prob_dir.mkdir(parents=True, exist_ok=True)
                                    with open(prob_dir / "lef.json", "w", encoding="utf-8") as f:
                                        json.dump(lef_result, f, indent=2, ensure_ascii=False)
                                    _role_order = {"best": 0, "slowest": 1, "random_0": 2, "random_1": 3, "random_2": 4}
                                    for sol_info in lef_sols:
                                        role = sol_info["role"]
                                        sol_idx = sol_info["sol_idx"]
                                        src = source_codes.get(sol_idx)
                                        if src:
                                            prefix = _role_order.get(role, len(_role_order))
                                            (prob_dir / f"{prefix}.{role}_sol{sol_idx}.txt").write_text(
                                                src, encoding="utf-8"
                                            )
                            if hasattr(_iter, "set_postfix"):
                                _iter.set_postfix(cached=n_cached, computed=n_computed)

                    log.info("[LEF] Total: %d cached + %d newly computed = %d problems",
                             n_cached, len(lef_by_problem) - n_cached, len(lef_by_problem))

            for comp in comparisons:
                if comp.get("name", "") in lef_by_problem:
                    comp["lef"] = lef_by_problem[comp["name"]]

        json_path = view_output_dir / "difficulty_comparison.json"
        with open(json_path, "w") as f:
            json.dump({
                "metadata": {
                    "split": args.split,
                    "dataset": args.dataset,
                    "source": args.source,
                    "model": args.model,
                    "language_view": lang_label,
                    "reference_timing": str(reference_path),
                    "evaluand_timing": str(evaluand_path),
                    "num_problems": len(comparisons),
                },
                "problems": comparisons,
            }, f, indent=2, ensure_ascii=False)
        log.info("[%s] JSON saved: %s", lang_label, json_path)

        write_summary(comparisons, view_output_dir, args.split,
                      str(reference_path), str(evaluand_path),
                      generator_tier_map=generator_tier_map or None,
                      lang_label=lang_label,
                      tcs_per_tier=args.tcs_per_tier or None,
                      slow_ratio_threshold=args.slow_ratio_threshold,
                      ref_max_ref_times=ref_max_ref_times)

        try:
            _write_constraint_violations_report(
                comparisons, view_output_dir, args.split, lang_label)
        except Exception as _e:
            log.warning("[%s] failed to write constraint_violations.txt: %s",
                        lang_label, _e)

        if lef_by_problem:
            lef_counts["n_lef_computed"] = len(lef_by_problem)
            write_lef_summary(lef_by_problem, view_output_dir,
                              counts=lef_counts or None)

        if not args.no_plot and HAS_MPL:
            for method in ["timelimit", "tercile"]:
                plot_comparison_bar(
                    comparisons, method,
                    view_output_dir / f"comparison_{method}.png",
                    title_suffix=f" — {args.split} ({lang_label})",
                )

            _tcs_per_tier = 5
            if generator_tier_map:
                _sample = next(iter(generator_tier_map.values()), {})
                _tier_cnt = Counter(_sample.values())
                if _tier_cnt:
                    _tcs_per_tier = max(_tier_cnt.values())
            ref_problems_plot = [c for c in comparisons
                                 if c.get("reference", {}).get("status") == "ok"]
            plot_tc_tier_overview(
                ref_problems_plot,
                view_output_dir / "tc_tier_overview.png",
                title_suffix=f" — {args.split} ({lang_label})",
                tcs_per_tier=_tcs_per_tier,
            )

            per_problem_dir_plot = view_output_dir / "per_problem"
            for comp in comparisons:
                safe_name = _re.sub(r'[^\w]', '_', comp.get('name') or 'unknown')
                prob_dir = per_problem_dir_plot / f"{comp['index']:04d}_{safe_name}"
                prob_dir.mkdir(parents=True, exist_ok=True)
                for method in ["timelimit", "tercile"]:
                    plot_problem_comparison(comp, method, prob_dir / f"{method}_comparison.png")

        print(f"\n[{lang_label}] Evaluation complete. Output: {view_output_dir}")
        print(f"  difficulty_comparison.json, summary.txt")
        if lef_by_problem:
            print(f"  lef_summary.txt  ({len(lef_by_problem)} problems with LEF)")
        if not args.no_plot and HAS_MPL:
            print(f"  comparison_timelimit.png, comparison_tercile.png, tc_tier_overview.png")
            print(f"  per_problem/  ({len(comparisons)} problems)")

def _collect_csv_row(lang_label: str, comparisons: list, generator_tier_map: dict) -> dict:
    both_ok = [c for c in comparisons
               if c.get("reference", {}).get("status") == "ok"
               and c.get("evaluand", {}).get("status") == "ok"]
    ref_problems = [c for c in comparisons if c.get("reference", {}).get("status") == "ok"]

    all_ev_tcs: list[dict] = []
    _rank = {"invalid": 3, "slow": 2, "medium": 1, "fast": 0}
    generated = 0
    valid = 0
    for c in both_ok:
        ev_info = c.get("evaluand", {})
        if ev_info.get("status") != "ok":
            continue
        tcs = ev_info.get("testcases", [])
        all_ev_tcs.extend(tcs)
        tc_worst: dict = {}
        for tc in tcs:
            tc_id = tc.get("testcase")
            if tc_id is None:
                continue
            d = tc.get("difficulty") or "fast"
            r = _rank.get(d, 0)
            if tc_id not in tc_worst or r > _rank.get(tc_worst[tc_id], 0):
                tc_worst[tc_id] = d
        for d in tc_worst.values():
            generated += 1
            if d != "invalid":
                valid += 1

    row = {"lang": lang_label, "total_tc": generated, "valid_tc": valid}

    for tier in ("fast", "medium", "slow"):
        hit, total = _tpr_counts(all_ev_tcs, tier)
        ratio = hit / total if total > 0 else 0.0
        row[f"TPR_{tier}"] = round(ratio, 4)
        row[f"TPR_{tier}_detail"] = f"{hit}/{total}"

    for module_label in ("M1", "M2"):
        m_tcs = [tc for tc in all_ev_tcs if tc.get("module") == module_label]
        for tier in ("fast", "medium", "slow"):
            hit, total = _tpr_counts(m_tcs, tier)
            ratio = hit / total if total > 0 else 0.0
            row[f"TPR_{tier}_{module_label}"] = round(ratio, 4)
            row[f"TPR_{tier}_{module_label}_detail"] = f"{hit}/{total}"

    return row

def _write_cross_lang_csv(csv_rows: list, output_dir: Path):
    if not csv_rows:
        return
    csv_path = output_dir / "metrics_summary.csv"

    columns = [
        "lang", "total_tc", "valid_tc",
        "TPR_fast", "TPR_fast_detail",
        "TPR_medium", "TPR_medium_detail",
        "TPR_slow", "TPR_slow_detail",
        "TAA", "TAA_detail",
        "TPR_fast_M1", "TPR_fast_M1_detail",
        "TPR_medium_M1", "TPR_medium_M1_detail",
        "TPR_slow_M1", "TPR_slow_M1_detail",
        "TAA_M1", "TAA_M1_detail",
        "TPR_fast_M2", "TPR_fast_M2_detail",
        "TPR_medium_M2", "TPR_medium_M2_detail",
        "TPR_slow_M2", "TPR_slow_M2_detail",
        "TAA_M2", "TAA_M2_detail",
    ]

    import csv
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore",
                                quoting=csv.QUOTE_NONNUMERIC)
        writer.writeheader()
        for row in csv_rows:
            writer.writerow(row)

    log.info("Cross-language CSV saved: %s", csv_path)
    print(f"\nCross-language summary: {csv_path}")

if __name__ == "__main__":
    main()
