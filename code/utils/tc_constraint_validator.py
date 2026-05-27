#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

_CHARSETS: dict[str, set[str]] = {
    "01": set("01"),
    "binary": set("01"),
    "lowercase": set("abcdefghijklmnopqrstuvwxyz"),
    "uppercase": set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    "letters": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    "digits": set("0123456789"),
    "alnum": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
}

class _Cursor:

    def __init__(self, text: str):
        self.lines: list[str] = text.splitlines()
        self._line_i: int = 0
        self._tokens: list[str] = []
        self._tok_i: int = 0
        self._refill()

    def _refill(self) -> None:
        while self._tok_i >= len(self._tokens) and self._line_i < len(self.lines):
            line = self.lines[self._line_i]
            self._line_i += 1
            self._tokens = line.split()
            self._tok_i = 0

    def has_token(self) -> bool:
        self._refill()
        return self._tok_i < len(self._tokens)

    def take_int(self) -> int:
        if not self.has_token():
            raise ValueError("token underflow (int expected)")
        tok = self._tokens[self._tok_i]
        self._tok_i += 1
        try:
            return int(tok)
        except ValueError:
            raise ValueError(f"cannot parse int: {tok!r}")

    def take_str_token(self) -> str:
        if not self.has_token():
            raise ValueError("token underflow (str expected)")
        tok = self._tokens[self._tok_i]
        self._tok_i += 1
        return tok

    def take_token(self):
        if not self.has_token():
            raise ValueError("token underflow")
        tok = self._tokens[self._tok_i]
        self._tok_i += 1
        try:
            return int(tok)
        except ValueError:
            return tok

    def skip_line(self) -> None:
        if self._tok_i < len(self._tokens):
            self._tok_i = len(self._tokens)
            return
        if self._line_i < len(self.lines):
            self._line_i += 1
            self._tokens = []
            self._tok_i = 0
            return
        raise ValueError("line underflow")

    def take_line(self) -> str:
        if self._tok_i < len(self._tokens):
            rest = " ".join(self._tokens[self._tok_i:])
            self._tok_i = len(self._tokens)
            return rest
        if self._line_i < len(self.lines):
            line = self.lines[self._line_i]
            self._line_i += 1
            self._tokens = []
            self._tok_i = 0
            return line
        raise ValueError("line underflow")

def _resolve_count(expr, header: dict, t_val, structure: dict):
    if expr is None or expr == "":
        return None
    if isinstance(expr, int):
        return expr
    if not isinstance(expr, str):
        return None
    if expr in header:
        v = header[expr]
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                return None
        return None
    if expr == structure.get("test_cases_var"):
        return t_val
    namespace = dict(header)
    if t_val is not None:
        namespace.setdefault(structure.get("test_cases_var") or "_", t_val)
    try:
        v = eval(expr, {"__builtins__": {}}, namespace)
        if isinstance(v, (int, float)):
            return int(v)
    except Exception:
        pass
    return None

def _parse_one_case(cur: _Cursor, structure: dict, t_val: int) -> dict:
    case: dict = {"header": {}, "arrays": {}, "strings": {},
                  "edges": None, "pairs": {}, "matrix": None}

    for v in (structure.get("header_vars") or []):
        case["header"][v] = cur.take_token()

    for arr in (structure.get("arrays") or []):
        name = arr["name"]
        lvar = arr.get("length_var")
        if lvar is None:
            line = cur.take_line()
            case["arrays"][name] = [int(x) for x in line.split()]
            continue
        length = _resolve_count(lvar, case["header"], t_val, structure)
        if length is None or length < 0:
            raise ValueError(f"unknown length for array {name} (var={lvar})")
        if arr.get("per_line"):
            vals = []
            for _ in range(length):
                vals.append(cur.take_int())
            case["arrays"][name] = vals
        else:
            case["arrays"][name] = [cur.take_int() for _ in range(length)]

    for s in (structure.get("strings") or []):
        cv = s.get("count_var")
        if cv:
            n = _resolve_count(cv, case["header"], t_val, structure)
            if n is None or n < 0:
                raise ValueError(f"unknown string count (var={cv})")
            case["strings"][s["name"]] = [cur.take_line() for _ in range(n)]
        else:
            case["strings"][s["name"]] = cur.take_line()

    edges_spec = structure.get("edges")
    if edges_spec:
        cv = edges_spec.get("count_var") if isinstance(edges_spec, dict) else None
        m = _resolve_count(cv, case["header"], t_val, structure) if cv else None
        if m is None:
            raise ValueError(f"edges count_var unresolvable (var={cv})")
        weighted = bool(edges_spec.get("weighted")) if isinstance(edges_spec, dict) else False
        edges: list = []
        for _ in range(m):
            u = cur.take_int()
            v_ = cur.take_int()
            if weighted:
                w = cur.take_int()
                edges.append((u, v_, w))
            else:
                edges.append((u, v_))
        case["edges"] = edges

    for pair in (structure.get("pairs") or []):
        vars_ = pair.get("vars") or []
        cv = pair.get("count_var")
        if not vars_ or cv is None:
            continue
        n = _resolve_count(cv, case["header"], t_val, structure)
        if n is None or n < 0:
            raise ValueError(f"unknown pair count (var={cv})")
        key = "_".join(vars_)
        pair_list = []
        for _ in range(n):
            row = [cur.take_int() for _ in vars_]
            pair_list.append(tuple(row))
        case["pairs"][key] = pair_list

    matrix_spec = structure.get("matrix")
    if matrix_spec and isinstance(matrix_spec, dict):
        rows_var = matrix_spec.get("rows_var")
        cols_var = matrix_spec.get("cols_var")
        rows = case["header"].get(rows_var) if rows_var else None
        cols = case["header"].get(cols_var) if cols_var else None
        if rows is None or cols is None:
            raise ValueError("matrix rows/cols var missing")
        mat = []
        for _ in range(rows):
            line = cur.take_line()
            mat.append(line)
        case["matrix"] = mat

    extra = structure.get("_extra_lines_per_case")
    if extra is not None:
        n_skip = _resolve_count(extra, case["header"], t_val, structure)
        if n_skip is not None and n_skip > 0:
            for _ in range(n_skip):
                cur.skip_line()

    return case

def parse_stdin(stdin_text: str, structure: dict) -> Optional[dict]:
    if not isinstance(stdin_text, str) or not stdin_text.strip():
        return None
    if not isinstance(structure, dict):
        return None
    try:
        cur = _Cursor(stdin_text)
        tcv = structure.get("test_cases_var")
        if tcv:
            t_val = cur.take_int()
        else:
            t_val = 1
        if t_val < 0 or t_val > 10**7:
            return None
        cases = [_parse_one_case(cur, structure, t_val) for _ in range(t_val)]
        return {"t": t_val, "test_cases_var": tcv, "cases": cases}
    except (ValueError, IndexError, KeyError):
        pass

    try:
        cur = _Cursor(stdin_text)
        tcv = structure.get("test_cases_var")
        t_val = cur.take_int() if tcv else 1
        if t_val < 0 or t_val > 10**7:
            return None
        header: dict = {}
        for v in (structure.get("header_vars") or []):
            header[v] = cur.take_token()
        return {
            "t": t_val,
            "test_cases_var": tcv,
            "cases": [{"header": header, "arrays": {}, "strings": {},
                       "edges": None, "pairs": {}, "matrix": None}],
            "_partial": True,
        }
    except (ValueError, IndexError, KeyError):
        return None

def _strip_subscript(name: str) -> str:
    if not isinstance(name, str):
        return name
    if "_" in name:
        return name.split("_", 1)[0]
    if "[" in name:
        return name.split("[", 1)[0]
    return name

def _values_for_var(parsed: dict, var: str) -> list[int]:
    base = _strip_subscript(var)
    out: list[int] = []
    for case in parsed.get("cases", []):
        if base in case["header"]:
            out.append(case["header"][base])
        if base in case["arrays"]:
            out.extend(case["arrays"][base])
        for key, pairs in case["pairs"].items():
            parts = key.split("_")
            if base in parts:
                idx = parts.index(base)
                for tup in pairs:
                    if idx < len(tup):
                        out.append(tup[idx])
        if case.get("edges"):
            pass
    return out

def _string_for_var(parsed: dict, var: str) -> list[str]:
    base = _strip_subscript(var)
    out: list[str] = []
    for c in parsed.get("cases", []):
        v = c["strings"].get(base)
        if v is None:
            continue
        if isinstance(v, list):
            out.extend(v)
        else:
            out.append(v)
    return out

def _array_for_var(parsed: dict, var: str) -> list[list[int]]:
    base = _strip_subscript(var)
    return [c["arrays"][base] for c in parsed.get("cases", []) if base in c["arrays"]]

def _check_range(c: dict, parsed: dict) -> Optional[str]:
    var = c.get("var")
    lo = c.get("lo")
    hi = c.get("hi")
    if var is None:
        return None
    if not isinstance(lo, (int, float)):
        lo = None
    if not isinstance(hi, (int, float)):
        hi = None
    if lo is None and hi is None:
        return None
    vals = _values_for_var(parsed, var)
    if not vals:
        return None
    for v in vals:
        if not isinstance(v, (int, float)):
            continue
        if lo is not None and v < lo:
            return f"range {var}: observed {v} < lo {lo}"
        if hi is not None and v > hi:
            return f"range {var}: observed {v} > hi {hi}"
    return None

def _check_product(c: dict, parsed: dict) -> Optional[str]:
    vs = c.get("vars") or []
    hi = c.get("hi")
    if hi is None or not vs:
        return None
    for case in parsed.get("cases", []):
        prod = 1
        present = True
        for v in vs:
            base = _strip_subscript(v)
            val = case["header"].get(base)
            if val is None:
                if base == parsed.get("test_cases_var"):
                    val = parsed.get("t")
                if val is None:
                    present = False
                    break
            prod *= val
        if present and prod > hi:
            return f"product {'·'.join(vs)}: observed {prod} > hi {hi}"
    return None

def _check_sum(c: dict, parsed: dict) -> Optional[str]:
    vs = c.get("vars") or []
    hi = c.get("hi")
    if hi is None or not vs:
        return None
    for case in parsed.get("cases", []):
        total = 0
        present = True
        for v in vs:
            base = _strip_subscript(v)
            val = case["header"].get(base)
            if val is None:
                present = False
                break
            total += val
        if present and total > hi:
            return f"sum {'+'.join(vs)}: observed {total} > hi {hi}"
    return None

def _check_sum_over_tc(c: dict, parsed: dict) -> Optional[str]:
    var = c.get("var")
    hi = c.get("hi")
    if var is None or not isinstance(hi, (int, float)):
        return None
    base = _strip_subscript(var)
    total = 0
    found = False
    for case in parsed.get("cases", []):
        v = case["header"].get(base)
        if v is None:
            if base in case["arrays"]:
                v = len(case["arrays"][base])
        if isinstance(v, (int, float)):
            total += v
            found = True
    if found and total > hi:
        return f"sum_over_tc {var}: observed Σ={total} > hi {hi}"
    return None

def _check_length(c: dict, parsed: dict) -> Optional[str]:
    var = c.get("var")
    lvar = c.get("length_var")
    min_length = c.get("min_length")
    if var is not None and lvar is None and isinstance(min_length, (int, float)):
        base = _strip_subscript(var)
        for case in parsed.get("cases", []):
            if base in case.get("strings", {}):
                val = case["strings"][base]
                vals = val if isinstance(val, list) else [val]
                for i, s in enumerate(vals):
                    if isinstance(s, str) and len(s) < min_length:
                        return (f"length {var}: observed |{var}"
                                f"{'['+str(i)+']' if isinstance(val,list) else ''}|"
                                f"={len(s)} < min {min_length}")
            elif base in case.get("arrays", {}):
                arr = case["arrays"][base]
                if len(arr) < min_length:
                    return f"length {var}: observed |{var}|={len(arr)} < min {min_length}"
        return None
    if var is None or lvar is None:
        return None
    base = _strip_subscript(var)
    lbase = _strip_subscript(lvar)
    for case in parsed.get("cases", []):
        if base in case["strings"]:
            val = case["strings"][base]
            if isinstance(val, list):
                expected = case["header"].get(lbase)
                if expected is None and lbase == parsed.get("test_cases_var"):
                    expected = parsed.get("t")
                if expected is None:
                    continue
                for i, s in enumerate(val):
                    if len(s) != expected:
                        return (f"length {var}: observed |{var}[{i}]|={len(s)} "
                                f"!= {lvar}={expected}")
                continue
            length = len(val)
        elif base in case["arrays"]:
            length = len(case["arrays"][base])
        else:
            continue
        expected = case["header"].get(lbase)
        if expected is None and lbase == parsed.get("test_cases_var"):
            expected = parsed.get("t")
        if expected is not None and length != expected:
            return f"length {var}: observed |{var}|={length} != {lvar}={expected}"
    return None

def _check_charset(c: dict, parsed: dict) -> Optional[str]:
    var = c.get("var")
    cs_name = (c.get("value") or "").lower()
    if var is None:
        return None
    allowed_str = c.get("allowed_chars")
    if allowed_str:
        allowed = set(allowed_str)
    else:
        allowed = _CHARSETS.get(cs_name)
    if allowed is None:
        return None
    label = cs_name if not allowed_str else f"{{{','.join(sorted(allowed))}}}"
    for s in _string_for_var(parsed, var):
        for ch in s:
            if ch not in allowed:
                return f"charset {var}: char {ch!r} not in {label}"
    return None

def _check_distinct(c: dict, parsed: dict) -> Optional[str]:
    var = c.get("var")
    if var is None:
        return None
    base = _strip_subscript(var)
    for arr in _array_for_var(parsed, base):
        if len(arr) != len(set(arr)):
            return f"distinct {var}: duplicates in array"
    for case in parsed.get("cases", []):
        s = case.get("strings", {}).get(base)
        if isinstance(s, list) and len(s) > 1:
            if len(s) != len(set(s)):
                return f"distinct {var}: duplicate strings (rows)"
    return None

def _check_power(c: dict, parsed: dict) -> Optional[str]:
    var = c.get("var")
    base = c.get("base")
    max_exp = c.get("max_exp")
    if var is None or base is None or max_exp is None:
        return None
    try:
        ceiling = int(base) ** int(max_exp) if int(max_exp) < 64 else None
    except (ValueError, OverflowError):
        return None
    if ceiling is None:
        return None
    for v in _values_for_var(parsed, var):
        if v > ceiling:
            return f"power {var}: observed {v} > {base}^{max_exp}"
    return None

_CHECKERS = {
    "range": _check_range,
    "product": _check_product,
    "sum": _check_sum,
    "sum_over_tc": _check_sum_over_tc,
    "length": _check_length,
    "charset": _check_charset,
    "distinct": _check_distinct,
    "power": _check_power,
}

def validate_against_constraints(parsed: dict, constraints: list) -> dict:
    if not isinstance(constraints, list) or not constraints:
        return {"compliant": False, "violations": ["no constraints to check"],
                "skipped": []}
    if not isinstance(parsed, dict):
        return {"compliant": False, "violations": ["parse failed"], "skipped": []}

    is_partial = bool(parsed.get("_partial"))
    header_vars = set()
    if is_partial and parsed.get("cases"):
        header_vars = set(parsed["cases"][0].get("header", {}).keys())

    BODY_CONSTRAINT_TYPES = {"length", "charset", "distinct", "permutation", "other"}

    violations: list[str] = []
    skipped: list[str] = []

    for c in constraints:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type")
        checker = _CHECKERS.get(ctype)
        if checker is None:
            continue

        if is_partial:
            if ctype in BODY_CONSTRAINT_TYPES:
                skipped.append(f"{ctype} on {c.get('var') or '?'} (body, partial)")
                continue
            if ctype in ("range", "power"):
                base = _strip_subscript(c.get("var") or "")
                if base and base not in header_vars:
                    skipped.append(f"{ctype} on {base} (body, partial)")
                    continue
            if ctype in ("product", "sum"):
                vars_ = c.get("vars") or []
                if not vars_ or any(_strip_subscript(v) not in header_vars for v in vars_):
                    skipped.append(f"{ctype} on {vars_} (body, partial)")
                    continue

        try:
            err = checker(c, parsed)
        except Exception as e:
            err = f"{ctype} check raised {type(e).__name__}: {e}"
        if err:
            violations.append(err)

    return {
        "compliant": len(violations) == 0,
        "violations": violations,
        "skipped": skipped,
    }

def is_tc_compliant(stdin_text: str, structure: dict, constraints: list) -> bool:
    parsed = parse_stdin(stdin_text, structure)
    if parsed is None:
        return False
    return validate_against_constraints(parsed, constraints).get("compliant", False)

def _self_test() -> int:
    failures = 0

    def check(label, cond):
        nonlocal failures
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failures += 1

    s1 = "5\n"
    st1 = {"test_cases_var": None, "header_vars": ["n"], "arrays": [],
           "strings": [], "edges": None, "matrix": None, "grid": None, "pairs": []}
    p = parse_stdin(s1, st1)
    check("parse simple header", p and p["cases"][0]["header"]["n"] == 5)
    r = validate_against_constraints(p, [{"type": "range", "var": "n", "lo": 1, "hi": 10}])
    check("range OK", r["compliant"])
    r = validate_against_constraints(p, [{"type": "range", "var": "n", "lo": 1, "hi": 4}])
    check("range violating (n>hi)", not r["compliant"])

    s2 = "3\n10 20 30\n"
    st2 = {"test_cases_var": None, "header_vars": ["n"],
           "arrays": [{"name": "a", "length_var": "n", "per_line": False}],
           "strings": [], "edges": None, "matrix": None, "grid": None, "pairs": []}
    p = parse_stdin(s2, st2)
    check("parse header+array", p and p["cases"][0]["arrays"]["a"] == [10, 20, 30])
    r = validate_against_constraints(p, [
        {"type": "range", "var": "n", "lo": 1, "hi": 100},
        {"type": "range", "var": "a", "lo": 0, "hi": 100, "is_array": True}])
    check("array element range OK", r["compliant"])
    r = validate_against_constraints(p, [
        {"type": "range", "var": "a", "lo": 0, "hi": 25}])
    check("array element range violating", not r["compliant"])

    s3 = "3\n2\n1 2\n3\n5 6 7\n4\n1 1 1 1\n"
    st3 = {"test_cases_var": "t", "header_vars": ["n"],
           "arrays": [{"name": "a", "length_var": "n", "per_line": False}],
           "strings": [], "edges": None, "matrix": None, "grid": None, "pairs": []}
    p = parse_stdin(s3, st3)
    check("parse multi-tc", p and p["t"] == 3 and len(p["cases"]) == 3)
    check("multi-tc cases", p["cases"][1]["arrays"]["a"] == [5, 6, 7])
    r = validate_against_constraints(p, [{"type": "sum_over_tc", "var": "n", "hi": 9}])
    check("sum_over_tc OK (Σn=9)", r["compliant"])
    r = validate_against_constraints(p, [{"type": "sum_over_tc", "var": "n", "hi": 8}])
    check("sum_over_tc violating", not r["compliant"])

    s4 = "3 4\n"
    st4 = {"test_cases_var": None, "header_vars": ["n", "m"], "arrays": [],
           "strings": [], "edges": None, "matrix": None, "grid": None, "pairs": []}
    p = parse_stdin(s4, st4)
    r = validate_against_constraints(p, [{"type": "product", "vars": ["n", "m"], "hi": 12}])
    check("product OK (3*4=12)", r["compliant"])
    r = validate_against_constraints(p, [{"type": "product", "vars": ["n", "m"], "hi": 11}])
    check("product violating", not r["compliant"])

    s5 = "5 abcde\n"
    st5 = {"test_cases_var": None, "header_vars": ["n"], "arrays": [],
           "strings": [{"name": "s", "length_var": "n", "charset": None}],
           "edges": None, "matrix": None, "grid": None, "pairs": []}
    p = parse_stdin(s5, st5)
    check("parse string", p and p["cases"][0]["strings"]["s"] == "abcde")
    r = validate_against_constraints(p, [
        {"type": "length", "var": "s", "length_var": "n"},
        {"type": "charset", "var": "s", "value": "lowercase"}])
    check("length+charset OK", r["compliant"])
    r = validate_against_constraints(p, [{"type": "charset", "var": "s", "value": "01"}])
    check("charset violating", not r["compliant"])

    r = validate_against_constraints(parse_stdin("5", st1), [])
    check("empty constraints → violating", not r["compliant"])

    s7 = "3\n1 2\n3 4\n5 6\n"
    st7 = {"test_cases_var": None, "header_vars": ["n"], "arrays": [],
           "strings": [], "edges": None, "matrix": None, "grid": None,
           "pairs": [{"vars": ["x", "y"], "count_var": "n"}]}
    p = parse_stdin(s7, st7)
    check("parse pairs", p and p["cases"][0]["pairs"]["x_y"] == [(1, 2), (3, 4), (5, 6)])
    r = validate_against_constraints(p, [{"type": "range", "var": "x", "lo": 0, "hi": 10}])
    check("pair x range OK", r["compliant"])
    r = validate_against_constraints(p, [{"type": "range", "var": "y", "lo": 0, "hi": 5}])
    check("pair y violating", not r["compliant"])

    p_fail = parse_stdin("not a number", st1)
    check("parse failure returns None", p_fail is None)

    ok = is_tc_compliant("5\n1 2 3 4 5\n", st2,
                          [{"type": "range", "var": "n", "lo": 1, "hi": 10},
                           {"type": "range", "var": "a", "lo": 1, "hi": 5}])
    check("is_tc_compliant OK", ok)
    bad = is_tc_compliant("5\n1 2 3 4 99\n", st2,
                          [{"type": "range", "var": "a", "lo": 1, "hi": 5}])
    check("is_tc_compliant violating", not bad)

    bad2 = is_tc_compliant("garbage", st1, [{"type": "range", "var": "n", "lo": 1, "hi": 10}])
    check("is_tc_compliant on garbage → False", not bad2)

    print(f"\n{'='*40}\n{'PASS' if failures == 0 else 'FAIL'}: failures={failures}\n{'='*40}")
    return failures

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    print("nothing to do; pass --self-test to run unit checks", file=sys.stderr)
    return 1

if __name__ == "__main__":
    sys.exit(main())
