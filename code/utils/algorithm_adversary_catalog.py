
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

_DEFAULT_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "dataset",
    "algorithm_adversary_scenarios.json",
)

def load_catalog(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    p = path or _DEFAULT_CATALOG_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return None

def detect_scenarios(
    catalog: Dict[str, Any],
    problem_desc: str,
    input_desc: str = "",
    structure: Optional[Dict[str, Any]] = None,
    use_knn_fallback: bool = False,
    knn_threshold: float = 0.45,
    knn_top_k: int = 2,
    knn_model_name: str = "all-mpnet-base-v2",
) -> List[str]:
    if not catalog or "scenarios" not in catalog:
        return []

    text = ((problem_desc or "") + " " + (input_desc or "")).lower()
    matched: List[str] = []
    for sname, sdata in catalog["scenarios"].items():
        det = sdata.get("detection", {})
        strong_kws = det.get("strong_keywords") or det.get("keywords") or []
        if any(kw.lower() in text for kw in strong_kws):
            matched.append(sname)

    if not matched and use_knn_fallback:
        try:
            from utils.scenario_knn import (
                knn_classify_problem_text_anchored,
                DEFAULT_TOP_K, DEFAULT_MIN_VOTES, DEFAULT_MAX_OUT,
            )
            knn_matches, _neighbors = knn_classify_problem_text_anchored(
                problem_description=problem_desc or "",
                input_description=input_desc or "",
                catalog=catalog,
                model_name=knn_model_name,
                threshold=knn_threshold,
                top_k=DEFAULT_TOP_K,
                min_votes=DEFAULT_MIN_VOTES,
                max_out=knn_top_k,
            )
            if knn_matches:
                return knn_matches
        except Exception:
            pass

    return matched

def _strip_for_llm(impl: Dict[str, Any]) -> Dict[str, str]:
    si = impl.get("slow_input", {}) or {}
    return {
        "name": str(impl.get("name", "")),
        "vulnerability_class": str(impl.get("vulnerability_class", "")),
        "construction": str(si.get("construction", "")),
        "worst_complexity": str(si.get("worst_complexity", "")),
        "best_complexity": str(si.get("best_complexity", "")),
    }

def build_routing_section(
    catalog: Optional[Dict[str, Any]],
    scenarios: List[str],
    num_testcases: int = 5,
) -> str:
    if not catalog or not scenarios:
        return (
            "### Detected Algorithm Scenario(s)\n"
            "No catalog scenario matched this problem; rely on the problem description "
            "above and standard adversarial-input techniques (worst-case data patterns, "
            "degenerate structures, hash collisions, etc.) appropriate for the algorithm "
            "the contestant is likely to use."
        )

    lines: List[str] = ["### Detected Algorithm Scenario(s)"]
    for sname in scenarios:
        sdata = catalog["scenarios"].get(sname, {})
        summary = sdata.get("scenario_summary", "")
        lines.append(f"- **{sname}** — {summary}")
    lines.append("")
    lines.append("### Possible Solver Implementations and Their Adversarial Inputs")
    lines.append("")
    lines.append(
        f"A correct solution may use any of the implementations below. Each entry describes "
        f"the input pattern that maximizes that implementation's runtime, the vulnerability "
        f"class (asymptotic / constant_factor / size_only / randomized / variant_dependent), "
        f"and the resulting worst- and best-case complexity. To produce maximally adversarial "
        f"test cases, target a different implementation across your generated tc_1, ..., tc_{num_testcases}."
    )
    lines.append("")

    counter = 1
    for sname in scenarios:
        sdata = catalog["scenarios"].get(sname, {})
        for impl in sdata.get("implementations", []):
            f = _strip_for_llm(impl)
            lines.append(f"{counter}. **{f['name']}**  [class: {f['vulnerability_class']}]")
            lines.append(f"   - Construction: {f['construction']}")
            lines.append(
                f"   - Worst: {f['worst_complexity']}  |  Best: {f['best_complexity']}"
            )
            lines.append("")
            counter += 1

    return "\n".join(lines).rstrip()
