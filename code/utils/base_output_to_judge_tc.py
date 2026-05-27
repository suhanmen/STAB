#!/usr/bin/env python3
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

def _bracket_match_json(text: str, start: int) -> str | None:
    depth = 0
    in_str_double = in_str_single = escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and (in_str_double or in_str_single):
            escape = True
            continue
        if not in_str_double and not in_str_single:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            elif c == '"':
                in_str_double = True
            elif c == "'":
                in_str_single = True
        elif c == '"' and in_str_double:
            in_str_double = False
        elif c == "'" and in_str_single:
            in_str_single = False
    return None

def extract_json_from_text(text: str) -> str | None:
    if not text or not isinstance(text, str):
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    for key_pattern in ('"test_cases"', "'test_cases'",
                        '"tc_1"', "'tc_1'",
                        '"test_case"', "'test_case'",
                        '"stdin_format"', "'stdin_format'"):
        idx = text.rfind(key_pattern)
        if idx != -1:
            start = text.rfind("{", 0, idx)
            if start != -1:
                result = _bracket_match_json(text, start)
                if result:
                    return result
    for m_brace in re.finditer(r'\{', text):
        pos = m_brace.start()
        rest = text[pos + 1:].lstrip()
        if rest and rest[0] in ('"', "'"):
            result = _bracket_match_json(text, pos)
            if result:
                if ('"tc_' in result or "'tc_" in result
                        or '"test_cases"' in result
                        or '"test_case"' in result
                        or '"stdin_format"' in result):
                    return result
    return None

def _execute_generator_code(code: str, timeout: int = 30) -> Optional[str]:
    try:
        from utils.generator_executor import execute_generator
        ok, result = execute_generator(code, timeout=timeout)
        return result if ok else None
    except ImportError:
        pass
    import subprocess, tempfile
    wrapper = code + "\n\n" + (
        "if __name__ == '__main__':\n"
        "    import sys\n"
        "    result = generate()\n"
        "    if not isinstance(result, str):\n"
        "        sys.exit(1)\n"
        "    sys.stdout.write(result)\n"
    )
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, dir='/tmp') as f:
            f.write(wrapper)
            tmp_path = f.name
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True, text=True, timeout=timeout,
        )
        Path(tmp_path).unlink(missing_ok=True)
        if result.returncode == 0 and result.stdout:
            return result.stdout
        return None
    except Exception:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        return None

def _parse_v4_json_lenient(text: str) -> dict | None:
    if not text or not isinstance(text, str):
        return None
    result = {}
    for m in re.finditer(
        r'"(tc_\d+)"\s*:\s*"{3}(.*?)"{3}',
        text, re.DOTALL
    ):
        key, code = m.group(1), m.group(2)
        result[key] = code
    if result:
        return result
    for m in re.finditer(r'"(tc_\d+)"\s*:\s*"', text):
        key = m.group(1)
        start = m.end()
        i = start
        escape = False
        while i < len(text):
            c = text[i]
            if escape:
                escape = False
                i += 1
                continue
            if c == '\\':
                escape = True
                i += 1
                continue
            if c == '"':
                raw = text[start:i]
                try:
                    result[key] = json.loads(f'"{raw}"')
                except (json.JSONDecodeError, ValueError):
                    result[key] = raw
                break
            i += 1
    return result if result else None

def _is_v4_tc_dict(obj: dict) -> bool:
    return any(k.startswith("tc_") for k in obj) and all(
        isinstance(v, str) for k, v in obj.items() if k.startswith("tc_")
    )

def _execute_v4_tc_dict(tc_dict: dict, timeout: int = 30) -> list[dict]:
    results = []
    for key in sorted(tc_dict.keys()):
        if not key.startswith("tc_"):
            continue
        code = tc_dict[key]
        if not isinstance(code, str) or "def generate" not in code:
            continue
        stdin = _execute_generator_code(code, timeout=timeout)
        if stdin:
            results.append({"input": stdin})
    return results

def _resolve_generator_codes(tc: dict, timeout: int = 30) -> dict:
    resolved = {}
    for tier in ("fast", "medium", "slow"):
        items = tc.get(tier, [])
        if not isinstance(items, list):
            resolved[tier] = items
            continue
        resolved_items = []
        for item in items:
            if isinstance(item, dict) and "_generator_code" in item:
                code = item["_generator_code"]
                stdin = _execute_generator_code(code, timeout=timeout)
                if stdin:
                    resolved_items.append({"input": stdin})
            else:
                resolved_items.append(item)
        resolved[tier] = resolved_items
    return resolved

def _load_names_by_idx(split: str, cache_dir: str | None) -> dict[int, str]:
    try:
        from datasets import load_dataset
        cache = {"cache_dir": cache_dir} if cache_dir else {}
        ds = load_dataset("deepmind/code_contests", split=split, **cache)
        return {i: ds[i].get("name", f"problem_{i}") for i in range(len(ds))}
    except Exception as e:
        print(f"Warning: could not load dataset for names: {e}. Using ID as name.", file=sys.stderr)
        return {}

def _extract_tier_from_prompt(text: str) -> Optional[str]:
    m = re.search(r'Current Tier:\s*\*\*(\w+)\*\*', text)
    return m.group(1).lower() if m else None

def _extract_v4_response(text: str) -> Optional[str]:
    idx = text.find('assistant')
    if idx < 0:
        return None
    resp = text[idx + len('assistant'):]
    resp = re.sub(r'<think>.*?</think>', '', resp, flags=re.DOTALL).strip()
    return resp

def _convert_base(input_path: str, names_by_idx: dict[int, str], gen_timeout: int = 30) -> list[dict]:
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    output_list = data.get("output", [])
    if not output_list:
        print("No output entries in input file.", file=sys.stderr)
        return []

    is_v4 = False
    if output_list:
        raw0 = output_list[0].get("full_pred") or output_list[0].get("pred")
        text0 = raw0[0] if isinstance(raw0, list) else (raw0 or "")
        if _extract_tier_from_prompt(text0) is not None:
            resp0 = _extract_v4_response(text0)
            if not resp0:
                raw_pred0 = output_list[0].get("pred")
                resp0 = raw_pred0[0] if isinstance(raw_pred0, list) else (raw_pred0 or "")
            if resp0 and ("def generate" in resp0 or '"tc_' in resp0):
                is_v4 = True

    if is_v4:
        return _convert_base_v4(output_list, names_by_idx, gen_timeout)

    is_v3 = False
    if output_list:
        raw0 = output_list[0].get("full_pred") or output_list[0].get("pred")
        text0 = raw0[0] if isinstance(raw0, list) else (raw0 or "")
        if _extract_tier_from_prompt(text0) is not None:
            resp0 = _extract_v4_response(text0)
            if not resp0:
                raw_pred0 = output_list[0].get("pred")
                resp0 = raw_pred0[0] if isinstance(raw_pred0, list) else (raw_pred0 or "")
            if resp0 and ('"test_case"' in resp0 or '"stdin_format"' in resp0):
                is_v3 = True

    if is_v3:
        return _convert_base_v3(output_list, names_by_idx)

    judge_entries = []
    for i, example in enumerate(output_list):
        idx = example.get("ID", i)
        name = names_by_idx.get(idx, f"problem_{idx}")
        raw = example.get("full_pred") or example.get("pred")
        text = raw[0] if isinstance(raw, list) else (raw or "")
        json_str = extract_json_from_text(text)
        if not json_str:
            continue
        try:
            parsed = json.loads(json_str)
            tc = parsed.get("test_cases")
            if isinstance(tc, dict) and any(k in tc for k in ("fast", "medium", "slow")):
                judge_entries.append({"name": name, "test_cases": tc})
        except json.JSONDecodeError:
            continue
    return judge_entries

def _convert_base_v3(output_list: list, names_by_idx: dict[int, str]) -> list[dict]:
    problem_tcs: dict[int, dict] = {}
    problem_names: dict[int, str] = {}
    problem_stdin_format: dict[int, list] = {}
    parse_ok = parse_fail = 0

    for i, example in enumerate(output_list):
        idx = example.get("ID", i)
        name = names_by_idx.get(idx, f"problem_{idx}")
        problem_names[idx] = name

        raw_full = example.get("full_pred") or example.get("pred")
        text = raw_full[0] if isinstance(raw_full, list) else (raw_full or "")

        tier = _extract_tier_from_prompt(text)
        if not tier:
            continue

        resp = _extract_v4_response(text)
        if not resp:
            raw_pred = example.get("pred")
            resp = raw_pred[0] if isinstance(raw_pred, list) else (raw_pred or "")
        if not resp:
            parse_fail += 1
            continue

        parsed = None
        json_str = extract_json_from_text(resp)
        if json_str:
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError:
                parsed = None
        if parsed is None:
            try:
                parsed = json.loads(resp)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        if not isinstance(parsed, dict):
            parse_fail += 1
            continue

        tc_inner = parsed.get("test_case")
        if tc_inner is None:
            tcs_field = parsed.get("test_cases")
            if isinstance(tcs_field, dict) and tier in tcs_field:
                v = tcs_field[tier]
                if isinstance(v, list) and v:
                    tc_inner = v[0]
                elif isinstance(v, dict):
                    tc_inner = v
        if not isinstance(tc_inner, dict):
            parse_fail += 1
            continue

        if idx not in problem_tcs:
            problem_tcs[idx] = {"fast": [], "medium": [], "slow": []}
        problem_tcs[idx][tier].append(tc_inner)

        sf = parsed.get("stdin_format")
        if isinstance(sf, list) and sf and idx not in problem_stdin_format:
            problem_stdin_format[idx] = sf

        parse_ok += 1

    judge_entries = []
    for idx in sorted(problem_tcs.keys()):
        tc = problem_tcs[idx]
        total = sum(len(tc[t]) for t in ("fast", "medium", "slow"))
        if total == 0:
            continue
        entry = {"name": problem_names.get(idx, f"problem_{idx}"),
                 "test_cases": tc}
        if idx in problem_stdin_format:
            entry["stdin_format"] = problem_stdin_format[idx]
        judge_entries.append(entry)

    print(f"[v3] Parsed {parse_ok} entries → {len(judge_entries)} problems "
          f"({parse_fail} parse failures)", file=sys.stderr)
    return judge_entries

def _convert_base_v4(output_list: list, names_by_idx: dict[int, str],
                     gen_timeout: int = 30) -> list[dict]:
    problem_tcs: dict[int, dict] = {}
    problem_names: dict[int, str] = {}

    for i, example in enumerate(output_list):
        idx = example.get("ID", i)
        name = names_by_idx.get(idx, f"problem_{idx}")
        problem_names[idx] = name

        raw_full = example.get("full_pred") or example.get("pred")
        text = raw_full[0] if isinstance(raw_full, list) else (raw_full or "")

        tier = _extract_tier_from_prompt(text)
        if not tier:
            continue

        resp = _extract_v4_response(text)
        if not resp:
            raw_pred = example.get("pred")
            resp = raw_pred[0] if isinstance(raw_pred, list) else (raw_pred or "")
            if not resp:
                continue

        parsed = None
        json_str = extract_json_from_text(resp)
        if json_str:
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError:
                pass
        if parsed is None:
            try:
                parsed = json.loads(resp)
            except (json.JSONDecodeError, ValueError):
                pass
        if parsed is None:
            parsed = _parse_v4_json_lenient(resp)
        if not isinstance(parsed, dict):
            continue

        if idx not in problem_tcs:
            problem_tcs[idx] = {"fast": [], "medium": [], "slow": []}

        if _is_v4_tc_dict(parsed):
            tc_items = _execute_v4_tc_dict(parsed, timeout=gen_timeout)
            problem_tcs[idx][tier].extend(tc_items)
        elif "test_cases" in parsed:
            tc = parsed["test_cases"]
            if isinstance(tc, dict) and tier in tc:
                problem_tcs[idx][tier].extend(
                    tc[tier] if isinstance(tc[tier], list) else [tc[tier]]
                )

    judge_entries = []
    exec_ok = exec_fail = 0
    for idx in sorted(problem_tcs.keys()):
        tc = problem_tcs[idx]
        total = sum(len(tc[t]) for t in ("fast", "medium", "slow"))
        if total > 0:
            judge_entries.append({
                "name": problem_names.get(idx, f"problem_{idx}"),
                "test_cases": tc,
            })
            exec_ok += total
        else:
            exec_fail += 1

    print(f"[v4] Executed generators: {exec_ok} TCs from {len(judge_entries)} problems "
          f"({exec_fail} problems with 0 valid TCs)", file=sys.stderr)
    return judge_entries

def _convert_api_jsonl(input_path: str, names_by_idx: dict[int, str],
                       gen_timeout: int = 30) -> list[dict]:
    judge_entries = []
    line_num = 0
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                line_num += 1
                continue
            idx = entry.get("ID", line_num)
            line_num += 1
            name = names_by_idx.get(idx, f"problem_{idx}")

            tc = None
            stdin_format = None
            for field in ("parsed_output", "response"):
                raw = entry.get(field)
                if raw is None:
                    continue
                if isinstance(raw, dict):
                    tc = raw.get("test_cases")
                    stdin_format = raw.get("stdin_format")
                elif isinstance(raw, str):
                    json_str = extract_json_from_text(raw)
                    if json_str:
                        try:
                            parsed = json.loads(json_str)
                            tc = parsed.get("test_cases")
                            stdin_format = parsed.get("stdin_format")
                        except json.JSONDecodeError:
                            pass
                if tc and isinstance(tc, dict) and any(k in tc for k in ("fast", "medium", "slow")):
                    break
                tc = None
                stdin_format = None

            if not tc:
                raw = entry.get("response")
                if isinstance(raw, dict) and any(k in raw for k in ("fast", "medium", "slow")):
                    tc_collected: dict[str, list[dict]] = {"fast": [], "medium": [], "slow": []}
                    for tier in ("fast", "medium", "slow"):
                        tier_items = raw.get(tier, [])
                        if not isinstance(tier_items, list):
                            continue
                        for blob in tier_items:
                            parsed_blob = None
                            if isinstance(blob, dict):
                                parsed_blob = blob
                            elif isinstance(blob, str):
                                json_str = extract_json_from_text(blob) or blob
                                try:
                                    parsed_blob = json.loads(json_str)
                                except json.JSONDecodeError:
                                    parsed_blob = _parse_v4_json_lenient(json_str)
                            if not isinstance(parsed_blob, dict) or not _is_v4_tc_dict(parsed_blob):
                                continue
                            for k in sorted(parsed_blob.keys()):
                                if not k.startswith("tc_"):
                                    continue
                                code = parsed_blob[k]
                                if isinstance(code, str) and "def generate" in code:
                                    tc_collected[tier].append({"_generator_code": code})
                    if any(tc_collected[t] for t in tc_collected):
                        tc = tc_collected

            if tc:
                has_generators = any(
                    isinstance(item, dict) and "_generator_code" in item
                    for tier_items in tc.values()
                    if isinstance(tier_items, list)
                    for item in tier_items
                )
                if has_generators:
                    tc = _resolve_generator_codes(tc, timeout=gen_timeout)

                judge_entry = {"name": name, "test_cases": tc}
                if stdin_format:
                    judge_entry["stdin_format"] = stdin_format
                judge_entries.append(judge_entry)
    return judge_entries

def main():
    parser = argparse.ArgumentParser(
        description="Convert model output to judge custom_testcases JSON")
    parser.add_argument("--input", required=True,
                        help="Path to Base *_output.json or API *_raw.jsonl")
    parser.add_argument("--output", required=True, help="Path to write judge-format JSON")
    parser.add_argument("--split", default="test", help="Split name (to load dataset for problem names)")
    parser.add_argument("--cache_dir", default=None, help="HuggingFace dataset cache dir")
    parser.add_argument("--format", default="auto", choices=["auto", "base", "api_jsonl"],
                        help="Input format: base (JSON), api_jsonl (JSONL), auto (detect by extension)")
    parser.add_argument("--gen_timeout", type=int, default=30,
                        help="Timeout (seconds) for executing v4 generator codes (default: 30)")
    args = parser.parse_args()

    fmt = args.format
    if fmt == "auto":
        fmt = "api_jsonl" if args.input.endswith(".jsonl") else "base"

    names_by_idx = _load_names_by_idx(args.split, args.cache_dir)

    if fmt == "api_jsonl":
        judge_entries = _convert_api_jsonl(args.input, names_by_idx, gen_timeout=args.gen_timeout)
    else:
        judge_entries = _convert_base(args.input, names_by_idx, gen_timeout=args.gen_timeout)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(judge_entries, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(judge_entries)} problem entries to {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()
