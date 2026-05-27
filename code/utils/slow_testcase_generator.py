
import gc
import json
import multiprocessing
import os
import re
import sys
import argparse
import random
import math
import tempfile
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any, Set
try:
    from utils.instruction.slow_testcase_refinement_prompt import (
        Slow_testcase_refinement_prompt,
        Slow_testcase_refinement_prompt_v1,
        Slow_testcase_refinement_prompt_v2,
        Slow_testcase_refinement_prompt_v3,
        Slow_testcase_refinement_prompt_v4,
        Slow_testcase_refinement_prompt_v5,
        TIER_BLOCKS,
    )
    HAS_REFINEMENT_PROMPT = True
except ImportError:
    HAS_REFINEMENT_PROMPT = False
    Slow_testcase_refinement_prompt = None
    Slow_testcase_refinement_prompt_v1 = None
    Slow_testcase_refinement_prompt_v2 = None
    Slow_testcase_refinement_prompt_v3 = None
    Slow_testcase_refinement_prompt_v4 = None
    Slow_testcase_refinement_prompt_v5 = None
    TIER_BLOCKS = None

try:
    from utils.instruction.slow_testcase_refinement_prompt_v6 import (
        Slow_testcase_refinement_prompt_v6,
        TIER_BLOCKS_V6,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v6 = None
    TIER_BLOCKS_V6 = None

try:
    from utils.instruction.slow_testcase_refinement_prompt_v7 import (
        Slow_testcase_refinement_prompt_v7,
        TIER_BLOCKS_V7,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v7 = None
    TIER_BLOCKS_V7 = None

try:
    from utils.instruction.slow_testcase_refinement_prompt_v8 import (
        Slow_testcase_refinement_prompt_v8,
        TIER_BLOCKS_V8,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v8 = None
    TIER_BLOCKS_V8 = None

try:
    from utils.instruction.slow_testcase_refinement_prompt import (
        Slow_testcase_refinement_prompt_v9,
        TIER_BLOCKS_V9,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v9 = None
    TIER_BLOCKS_V9 = None

try:
    from utils.instruction.slow_testcase_refinement_prompt_v9_smt_only import (
        Slow_testcase_refinement_prompt_v9_smt_only,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v9_smt_only = None

try:
    from utils.instruction.slow_testcase_refinement_prompt_v9_scenario_only import (
        Slow_testcase_refinement_prompt_v9_scenario_only,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v9_scenario_only = None

try:
    from utils.instruction.slow_testcase_refinement_prompt_v9_smt_only_minimal import (
        Slow_testcase_refinement_prompt_v9_smt_only_minimal,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v9_smt_only_minimal = None

try:
    from utils.instruction.slow_testcase_refinement_prompt_v9_scenario_only_minimal import (
        Slow_testcase_refinement_prompt_v9_scenario_only_minimal,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v9_scenario_only_minimal = None

try:
    from utils.instruction.slow_testcase_refinement_prompt_v10 import (
        Slow_testcase_refinement_prompt_v10,
        TIER_BLOCKS_V10,
    )
except ImportError:
    Slow_testcase_refinement_prompt_v10 = None
    TIER_BLOCKS_V10 = None

try:
    from utils.algorithm_adversary_catalog import (
        load_catalog as _load_adversary_catalog,
        detect_scenarios as _detect_adversary_scenarios,
        build_routing_section as _build_adversary_routing_section,
    )
    _ADVERSARY_CATALOG_CACHE: Optional[Dict[str, Any]] = None
except ImportError:
    _load_adversary_catalog = None
    _detect_adversary_scenarios = None
    _build_adversary_routing_section = None
    _ADVERSARY_CATALOG_CACHE = None

def _get_adversary_catalog():
    global _ADVERSARY_CATALOG_CACHE
    if _load_adversary_catalog is None:
        return None
    if _ADVERSARY_CATALOG_CACHE is None:
        _ADVERSARY_CATALOG_CACHE = _load_adversary_catalog()
    return _ADVERSARY_CATALOG_CACHE

try:
    from utils.generator_executor import extract_python_from_response, validate_generator
except ImportError:
    extract_python_from_response = None
    validate_generator = None

try:
    from ortools.sat.python import cp_model
    CPSAT_AVAILABLE = True
except ImportError:
    CPSAT_AVAILABLE = False

Z3_AVAILABLE = CPSAT_AVAILABLE

TIER_CONFIGS = {
    'fast': {
        'value_strategy': 'min',
        'array_fill': 'min',
        'graph_structure': 'star',
    },
    'medium': {
        'value_strategy': 'mid',
        'array_fill': 'mixed',
        'graph_structure': 'random',
    },
    'slow': {
        'value_strategy': 'max',
        'array_fill': 'reverse_sorted',
        'graph_structure': 'chain',
    },
}

SLOW_FILL_CYCLE = ['reverse_sorted', 'sawtooth', 'max', 'alternating_extremes', 'random_heavy']
SLOW_GRAPH_CYCLE = ['chain', 'caterpillar', 'random_deep', 'chain', 'caterpillar']
SLOW_STRING_STRATEGY = ['periodic_ab', 'single_char', 'anti_period', 'periodic_ab', 'single_char']

SLOW_FILL_CYCLE_LEGACY = ['max', 'max', 'max', 'max', 'max']
SLOW_GRAPH_CYCLE_LEGACY = ['chain', 'chain', 'chain', 'chain', 'chain']
SLOW_STRING_STRATEGY_LEGACY = ['default', 'default', 'default', 'default', 'default']

STRUCTURE_ANTIPATTERN_MAP = {
    'tree': {
        'patterns': ['bamboo', 'star', 'caterpillar'],
        'condition': "structure['edges'] exists and is_tree=True",
    },
    'graph': {
        'patterns': ['dense_clique_chain', 'adversarial_bfs'],
        'condition': "structure['edges'] exists and is_tree=False",
    },
    'array': {
        'patterns': ['reverse_sorted', 'all_equal', 'sorted_asc', 'alternating',
                     'sawtooth', 'coprime_sequence'],
        'condition': "structure['arrays'] is non-empty",
    },
    'string': {
        'patterns': ['periodic', 'anti_hash'],
        'condition': "structure['strings'] is non-empty",
    },
    'matrix': {
        'patterns': ['checkerboard', 'all_same'],
        'condition': "structure['matrix'] is not None",
    },
    'pairs': {
        'patterns': ['collinear', 'clustered'],
        'condition': "structure['pairs'] is non-empty",
    },
}

def parse_number(s: str) -> Optional[int]:
    s = s.strip()
    if not s:
        return None

    s = re.sub(r'(?<=\d)[\s,](?=\d{3}(?!\d))', '', s)

    try:
        return int(s)
    except ValueError:
        pass

    s = s.replace('⋅', '*').replace('×', '*').replace('·', '*')
    s = re.sub(r'\{(\d+)\}', r'\1', s)
    s = re.sub(r'\s*\*\s*', '*', s)
    s = re.sub(r'\s*\^\s*', '^', s)

    _MAX_VALUE = 10**18

    def _safe_pow(base: int, exp: int) -> int:
        if base in (0, 1) or exp == 0:
            return base ** exp
        if exp * (base.bit_length()) > 80:
            return _MAX_VALUE
        val = base ** exp
        return min(val, _MAX_VALUE)

    m = re.match(r'^(-?\d+\.\d+)\*(\d+)\^(\d+)$', s)
    if m:
        coef = float(m.group(1))
        powv = _safe_pow(int(m.group(2)), int(m.group(3)))
        return int(coef * powv)

    m = re.match(r'^(-?\d+)\*(\d+)\^(\d+)$', s)
    if m:
        return int(m.group(1)) * _safe_pow(int(m.group(2)), int(m.group(3)))

    m = re.match(r'^(\d+)\^(\d+)$', s)
    if m:
        return _safe_pow(int(m.group(1)), int(m.group(2)))

    m = re.match(r'^-(\d+)\^(\d+)$', s)
    if m:
        return -_safe_pow(int(m.group(1)), int(m.group(2)))

    m = re.match(r'^-(\d+)\*(\d+)\^(\d+)$', s)
    if m:
        return -(int(m.group(1)) * _safe_pow(int(m.group(2)), int(m.group(3))))

    try:
        return int(s)
    except ValueError:
        return None

NUM_PAT = (
    r'(-?\d+(?:\.\d+)?(?:[\s,]\d{3})*'
    r'(?:\s*[⋅×·\*]\s*\{?\s*\d+\s*\}?(?:[\s,]\d{3})*)?'
    r'(?:\s*\^?\s*\{?\s*\d*\s*\}?)?)'
    r'(?!\.\d|[A-Za-z_\^\*×⋅·])'
)
LE_PAT = r'[≤≦]|\\leq|\\le|<\s*='
LT_PAT = r'<(?!=)'
VAR_PAT = r'([a-zA-Z](?:_[a-zA-Z0-9{}]*)?)'

def parse_constraints(input_desc: str, constraints_text: Optional[str] = None,
                      problem_desc: Optional[str] = None) -> List[Dict[str, Any]]:
    text = input_desc or ""
    if constraints_text:
        text += "\n" + constraints_text
    if problem_desc:
        text += "\n" + problem_desc

    results = []

    text = text.replace('≤', '≤').replace('≦', '≤')
    text = re.sub(r'\\leq|\\le', '≤', text)
    text = text.replace('<=', '≤')
    text = text.replace('\u2019', "'").replace('\u2018', "'")

    CMP_PAT = r'(?:≤|<)'
    pattern_range = re.compile(
        NUM_PAT + r'\s*(' + CMP_PAT + r')\s*'
        r'([a-zA-Z_][a-zA-Z0-9_,\s]*?)'
        r'\s*(' + CMP_PAT + r')\s*' + NUM_PAT,
        re.UNICODE
    )
    for m in pattern_range.finditer(text):
        lo_str, lower_op, vars_str, upper_op, hi_str = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        lo = parse_number(lo_str)
        hi = parse_number(hi_str)
        if lo is None or hi is None:
            continue
        if lower_op == '<':
            lo = lo + 1
        if upper_op == '<':
            hi = hi - 1
        var_names = [v.strip() for v in re.split(r'[,\s]+', vars_str) if re.match(r'^[a-zA-Z]', v.strip())]
        for var in var_names:
            results.append({'type': 'range', 'var': var, 'lo': lo, 'hi': hi})

    pattern_range_2d = re.compile(
        NUM_PAT + r'\s*(' + CMP_PAT + r')\s*'
        r'([a-zA-Z])_\{[ij](?:,\s*[ij])?\}'
        r'\s*(' + CMP_PAT + r')\s*' + NUM_PAT,
        re.UNICODE,
    )
    for m in pattern_range_2d.finditer(text):
        lo_str, lower_op, var_base, upper_op, hi_str = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        lo = parse_number(lo_str)
        hi = parse_number(hi_str)
        if lo is None or hi is None:
            continue
        if lower_op == '<':
            lo = lo + 1
        if upper_op == '<':
            hi = hi - 1
        already = any(c.get('type') == 'range'
                       and c.get('var') == var_base
                       and c.get('lo') == lo and c.get('hi') == hi
                       for c in results)
        if not already:
            results.append({'type': 'range', 'var': var_base,
                            'lo': lo, 'hi': hi,
                            'is_array': True, 'array_name': var_base})

    pattern_chain_multi = re.compile(
        NUM_PAT + r'\s*(' + CMP_PAT + r')\s*'
        r'([a-zA-Z_][a-zA-Z0-9_,\s]*?)'
        r'\s*(' + CMP_PAT + r')\s*' + VAR_PAT
        + r'(?![a-zA-Z])'
        + r'(?!\s*[+\-]\s*\d)'
        + r'(?!\s*[·*×⋅]\s*[a-zA-Z])'
        + r'(?!\s*[a-zA-Z]\s*[/÷]\s*\d)'
        + r'(?!\s*[/÷]\s*\d)',
        re.UNICODE,
    )
    for m in pattern_chain_multi.finditer(text):
        lo_str, lower_op, vars_str, upper_op, var2 = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        lo = parse_number(lo_str)
        if lo is None:
            continue
        if lower_op == '<':
            lo = lo + 1
        if not re.match(r'^[a-zA-Z]', var2):
            continue
        var_names = [v.strip() for v in re.split(r'[,\s]+', vars_str)
                     if re.match(r'^[a-zA-Z][\w]*$', v.strip())]
        for var1 in var_names:
            if var1 == var2:
                continue
            already_has = any(c.get('var') == var1 and c.get('type') == 'range'
                              for c in results)
            if not already_has:
                results.append({'type': 'chain', 'var': var1, 'lo': lo,
                                 'hi_var': var2, 'strict_upper': upper_op == '<'})

    pattern_chain_triple = re.compile(
        NUM_PAT + r'\s*(' + CMP_PAT + r')\s*'
        + VAR_PAT + r'(?![a-zA-Z])'
        + r'\s*(' + CMP_PAT + r')\s*' + VAR_PAT + r'(?![a-zA-Z])'
        + r'\s*(' + CMP_PAT + r')\s*' + VAR_PAT + r'(?![a-zA-Z])',
        re.UNICODE,
    )
    for m in pattern_chain_triple.finditer(text):
        lo_str = m.group(1)
        op1 = m.group(2)
        var_a = m.group(3)
        op2 = m.group(4)
        var_b = m.group(5)
        op3 = m.group(6)
        var_c = m.group(7)
        lo = parse_number(lo_str)
        if lo is None:
            continue
        if op1 == '<':
            lo = lo + 1
        if var_b != var_c:
            already_has = any(c.get('var') == var_b
                               and c.get('type') in ('range', 'chain')
                               and c.get('hi_var', None) == var_c
                              for c in results)
            if not already_has:
                results.append({'type': 'chain', 'var': var_b, 'lo': lo,
                                 'hi_var': var_c,
                                 'strict_upper': op3 == '<'})

    pattern_min_upper = re.compile(
        NUM_PAT + r'\s*(' + CMP_PAT + r')\s*'
        + VAR_PAT + r'(?![a-zA-Z])'
        + r'\s*(' + CMP_PAT + r')\s*'
        + r'(?:min|MIN)\s*\([^)]*?,\s*' + NUM_PAT + r'\s*\)',
        re.UNICODE,
    )
    for m in pattern_min_upper.finditer(text):
        lo_str, op1, var, op2, hi_str = m.groups()
        lo = parse_number(lo_str); hi = parse_number(hi_str)
        if lo is None or hi is None:
            continue
        if op1 == '<': lo = lo + 1
        if op2 == '<': hi = hi - 1
        already = any(c.get('var') == var and c.get('type') == 'range'
                       and c.get('hi') == hi for c in results)
        if not already:
            results.append({'type': 'range', 'var': var, 'lo': lo, 'hi': hi})

    pattern_product_div = re.compile(
        NUM_PAT + r'\s*(?:≤|<)\s*' + VAR_PAT + r'\s*(?:≤|<)\s*'
        + VAR_PAT + r'\s*[⋅×·\*]?\s*' + VAR_PAT + r'\s*[/÷]\s*(\d+)',
        re.UNICODE,
    )
    for m in pattern_product_div.finditer(text):
        lo_str, var_main, var_a, var_b, div_str = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        lo = parse_number(lo_str)
        if lo is None: continue
        try:
            div = int(div_str)
        except ValueError:
            continue
        if div <= 0: continue
        already = any(c.get('var') == var_main and c.get('type') == 'product_div'
                       for c in results)
        if already:
            continue
        results.append({
            'type': 'product_div', 'var': var_main, 'lo': lo,
            'product_vars': [var_a, var_b], 'div': div,
        })

    pattern_product_only = re.compile(
        NUM_PAT + r'\s*(?:≤|<)\s*' + VAR_PAT + r'\s*(?:≤|<)\s*'
        + VAR_PAT + r'\s*[⋅×·\*]\s*' + VAR_PAT
        + r'(?!\s*[/÷]\s*\d)',
        re.UNICODE,
    )
    for m in pattern_product_only.finditer(text):
        lo_str, var_main, var_a, var_b = (
            m.group(1), m.group(2), m.group(3), m.group(4))
        lo = parse_number(lo_str)
        if lo is None: continue
        already = any(c.get('var') == var_main and c.get('type') in ('product_div',)
                       for c in results)
        if already:
            continue
        results.append({
            'type': 'product_div', 'var': var_main, 'lo': lo,
            'product_vars': [var_a, var_b], 'div': 1,
        })

    _MATH_VARS = set('nmkprqsabcdwhxyz')
    pattern_product_concat = re.compile(
        NUM_PAT + r'\s*(?:≤|<)\s*' + VAR_PAT + r'\s*(?:≤|<)\s*'
        + r'([a-zA-Z])([a-zA-Z])'
        + r'(?!\s*[/÷]\s*\d)'
        + r'(?!\s*[a-zA-Z_])'
        + r'(?!\s*[+\-*·×⋅])',
        re.UNICODE,
    )
    for m in pattern_product_concat.finditer(text):
        lo_str, var_main, va, vb = m.group(1), m.group(2), m.group(3), m.group(4)
        lo = parse_number(lo_str)
        if lo is None: continue
        if va not in _MATH_VARS or vb not in _MATH_VARS:
            continue
        already = any(c.get('var') == var_main and c.get('type') == 'product_div'
                       for c in results)
        if already:
            continue
        results.append({
            'type': 'product_div', 'var': var_main, 'lo': lo,
            'product_vars': [va, vb], 'div': 1,
        })

    pattern_chain_div = re.compile(
        NUM_PAT + r'\s*(?:≤|<)\s*' + VAR_PAT + r'\s*(?:≤|<)\s*'
        + VAR_PAT + r'\s*[/÷]\s*(\d+)'
        + r'(?!\s*[⋅×·\*]\s*[a-zA-Z])',
        re.UNICODE,
    )
    for m in pattern_chain_div.finditer(text):
        lo_str, var_main, var_hi, div_str = (
            m.group(1), m.group(2), m.group(3), m.group(4))
        lo = parse_number(lo_str)
        if lo is None: continue
        try:
            div = int(div_str)
        except ValueError:
            continue
        if div <= 0: continue
        already = any(c.get('var') == var_main and c.get('type') == 'chain_div'
                       for c in results)
        if already:
            continue
        results.append({
            'type': 'chain_div', 'var': var_main, 'lo': lo,
            'hi_var': var_hi, 'div': div,
        })

    pattern_product = re.compile(
        VAR_PAT + r'\s*[⋅×·\*]\s*' + VAR_PAT + r'\s*(' + CMP_PAT + r')\s*' + NUM_PAT, re.UNICODE
    )
    for m in pattern_product.finditer(text):
        var1, var2, op, hi_str = m.group(1), m.group(2), m.group(3), m.group(4)
        hi = parse_number(hi_str)
        if hi is None:
            continue
        if op == '<':
            hi = hi - 1
        results.append({'type': 'product', 'vars': [var1, var2], 'hi': hi})

    pattern_sum = re.compile(
        VAR_PAT + r'\s*\+\s*' + VAR_PAT + r'\s*(' + CMP_PAT + r')\s*' + NUM_PAT, re.UNICODE
    )
    for m in pattern_sum.finditer(text):
        var1, var2, op, hi_str = m.group(1), m.group(2), m.group(3), m.group(4)
        hi = parse_number(hi_str)
        if hi is None:
            continue
        if op == '<':
            hi = hi - 1
        results.append({'type': 'sum', 'vars': [var1, var2], 'hi': hi})

    for c in results:
        if c.get('type') == 'range' and '_' in c.get('var', ''):
            if re.match(r'^[a-zA-Z]_[a-zA-Z]$', c['var']):
                c['is_array'] = True
                c['array_name'] = c['var'].split('_')[0]

    pattern_three_way = re.compile(
        NUM_PAT + r'\s*(?:≤|<)\s*' + VAR_PAT + r'\s*(?:≤|<)\s*'
        + VAR_PAT + r'\s*(?:≤|<)\s*' + NUM_PAT,
        re.UNICODE,
    )
    for m in pattern_three_way.finditer(text):
        lo_str, var1, var2, hi_str = m.group(1), m.group(2), m.group(3), m.group(4)
        lo = parse_number(lo_str); hi = parse_number(hi_str)
        if lo is None or hi is None: continue
        for v in (var1, var2):
            if not re.match(r'^[a-zA-Z]', v): continue
            already = any(c.get('var') == v and c.get('type') == 'range'
                          and c.get('lo') is not None and c.get('hi') is not None
                          for c in results)
            if already: continue
            results = [c for c in results if not (
                c.get('var') == v and c.get('type') == 'range'
                and (c.get('lo') is None or c.get('hi') is None))]
            is_arr = bool(re.match(r'^[a-zA-Z]_[a-zA-Z]$', v))
            results.append({
                'type': 'range', 'var': v, 'lo': lo, 'hi': hi,
                'is_array': is_arr,
                'array_name': v.split('_')[0] if is_arr else None,
            })
        already_chain = any(c.get('var') == var1 and c.get('type') in ('chain', 'chain_arith')
                            for c in results)
        if not already_chain:
            results.append({'type': 'chain', 'var': var1, 'lo': lo,
                            'hi_var': var2, 'strict_upper': False})

    pattern_chain_arith = re.compile(
        NUM_PAT + r'\s*(?:≤|<)\s*' + VAR_PAT + r'\s*(?:≤|<)\s*'
        r'(?:(\d+)\s*\*?\s*)?'
        + VAR_PAT
        + r'(?:\s*([+\-])\s*(\d+))?',
        re.UNICODE,
    )
    for m in pattern_chain_arith.finditer(text):
        lo_str, var1, coef_str, var2, op, off_str = (
            m.group(1), m.group(2), m.group(3), m.group(4),
            m.group(5), m.group(6))
        lo = parse_number(lo_str)
        if lo is None: continue
        if not re.match(r'^[a-zA-Z]', var2): continue
        coef = int(coef_str) if coef_str else 1
        offset = (int(off_str) if off_str else 0) * (1 if op == '+' else -1 if op == '-' else 0)
        if coef == 1 and offset == 0:
            continue
        already = any(c.get('var') == var1 and c.get('type') in ('range', 'chain', 'chain_arith')
                       for c in results)
        if already: continue
        results.append({
            'type': 'chain_arith', 'var': var1, 'lo': lo,
            'hi_var': var2, 'hi_coef': coef, 'hi_offset': offset,
        })

    pattern_min_upper = re.compile(
        NUM_PAT + r'\s*(?:≤|<)\s*' + VAR_PAT
        + r'\s*(?:≤|<)\s*min\s*\(\s*' + NUM_PAT + r'\s*,\s*' + VAR_PAT
        + r'\s*\)',
        re.UNICODE,
    )
    for m in pattern_min_upper.finditer(text):
        lo_str, var, k_str, _v2 = m.group(1), m.group(2), m.group(3), m.group(4)
        lo = parse_number(lo_str)
        hi = parse_number(k_str)
        if lo is None or hi is None:
            continue
        already = any(c.get('var') == var and c.get('type') == 'range'
                      for c in results)
        if not already:
            is_arr = bool(re.match(r'^[a-zA-Z]_[a-zA-Z]$', var))
            results.append({
                'type': 'range', 'var': var, 'lo': lo, 'hi': hi,
                'is_array': is_arr,
                'array_name': var.split('_')[0] if is_arr else None,
            })

    pattern_op_eq = re.compile(
        r'\b(' + VAR_PAT[1:-1] + r')\s*=\s*(\d+)\s*(?:or|,|\|)\s*(?:\1\s*=\s*)?(\d+)'
        r'(?:\s*(?:or|,|\|)\s*(?:\1\s*=\s*)?(\d+))?',
        re.IGNORECASE | re.UNICODE,
    )
    for m in pattern_op_eq.finditer(text):
        var = m.group(1)
        if len(var) > 6: continue
        vals = [int(g) for g in m.groups()[1:] if g and g.isdigit()]
        if not vals: continue
        lo, hi = min(vals), max(vals)
        already = any(c.get('var') == var and c.get('type') == 'range'
                       for c in results)
        if not already:
            results.append({'type': 'range', 'var': var, 'lo': lo, 'hi': hi})

    pattern_neq = re.compile(
        VAR_PAT + r'\s*(?:≠|!=)\s*' + VAR_PAT, re.UNICODE
    )
    for m in pattern_neq.finditer(text):
        var1, var2 = m.group(1), m.group(2)
        v1 = re.sub(r'_[a-zA-Z0-9]+$', '', var1)
        v2 = re.sub(r'_[a-zA-Z0-9]+$', '', var2)
        canonical = f"{v1} ≠ {v2}"
        if not any(c.get('type') == 'other' and
                   c.get('text', '').lower() == canonical.lower()
                   for c in results):
            results.append({'type': 'other', 'text': canonical})

    text_low = text.lower()
    detected_charset = None
    if (re.search(r"\bbinary\b", text_low)
            or re.search(r"['\"]0['\"]\s*(?:and(?:/| or )?|or|/)\s*['\"]1['\"]", text_low)
            or re.search(r"\b0\s+(?:and|or)\s+1\b", text_low)):
        detected_charset = 'binary'
    elif re.search(r"lowercase\s+latin", text_low) or re.search(r"lowercase\s+english", text_low):
        detected_charset = 'lowercase'
    elif re.search(r"uppercase\s+latin", text_low) or re.search(r"uppercase\s+english", text_low):
        detected_charset = 'uppercase'
    elif re.search(r"\bdigits?\b", text_low) or re.search(r"\b0[\-\.]+9\b", text_low):
        detected_charset = 'digits'
    if detected_charset:
        for c in results:
            if c.get('type') == 'string_length' and c.get('var'):
                v = c['var']
                if not any(d.get('type') == 'charset' and d.get('var') == v
                           for d in results):
                    results.append({'type': 'charset', 'var': v,
                                     'value': detected_charset})

    pattern_abs_pair = re.compile(
        r'(?:'+ NUM_PAT + r'\s*≤\s*)?'
        r'[|｜]\s*' + VAR_PAT + r'\s*[|｜]\s*'
        r',\s*'
        r'[|｜]\s*' + VAR_PAT + r'\s*[|｜]\s*'
        r'(?:≤|<)\s*' + NUM_PAT,
        re.UNICODE,
    )
    for m in pattern_abs_pair.finditer(text):
        v1, v2, hi_str = m.group(2), m.group(3), m.group(4)
        hi = parse_number(hi_str)
        if hi is None: continue
        for var in (v1, v2):
            already = any(c.get('var') == var and c.get('type') == 'range'
                          for c in results)
            if already: continue
            is_arr = bool(re.match(r'^[a-zA-Z]_[a-zA-Z]$', var))
            results.append({
                'type': 'range', 'var': var, 'lo': -hi, 'hi': hi,
                'is_array': is_arr,
                'array_name': var.split('_')[0] if is_arr else None,
            })

    pattern_abs = re.compile(r'[|｜]\s*' + VAR_PAT + r'\s*[|｜]\s*≤\s*' + NUM_PAT, re.UNICODE)
    for m in pattern_abs.finditer(text):
        var, hi_str = m.group(1), m.group(2)
        hi = parse_number(hi_str)
        if hi is None:
            continue
        if len(var) == 1 and var.isalpha() and var.lower() in 'stpqrw':
            continue
        already = any(c.get('var') == var for c in results)
        if not already:
            is_arr = bool(re.match(r'^[a-zA-Z]_[a-zA-Z]$', var))
            results.append({
                'type': 'range', 'var': var, 'lo': -hi, 'hi': hi,
                'is_array': is_arr,
                'array_name': var.split('_')[0] if is_arr else None,
            })

    pattern_strlen = re.compile(
        r'(?:' + NUM_PAT + r'\s*≤\s*)?' +
        r'[|｜]\s*' + VAR_PAT + r'\s*[|｜]\s*≤\s*' + NUM_PAT, re.UNICODE
    )
    for m in pattern_strlen.finditer(text):
        lo_str, var, hi_str = m.group(1), m.group(2), m.group(3)
        hi = parse_number(hi_str)
        if hi is None:
            continue
        if len(var) == 1 and var.isalpha() and var.lower() in 'stpqrw':
            lo = parse_number(lo_str) if lo_str else 1
            already = any(c.get('var') == var and c.get('is_string') for c in results)
            if not already:
                results.append({
                    'type': 'string_length', 'var': var, 'lo': lo, 'hi': hi, 'is_string': True,
                })

    pattern_neg = re.compile(
        r'-\s*' + NUM_PAT + r'\s*≤\s*' + VAR_PAT + r'\s*≤\s*' + NUM_PAT, re.UNICODE
    )
    for m in pattern_neg.finditer(text):
        neg_hi_str, var, hi_str = m.group(1), m.group(2), m.group(3)
        neg_hi = parse_number(neg_hi_str)
        hi = parse_number(hi_str)
        if neg_hi is None or hi is None:
            continue
        already = any(c.get('var') == var for c in results)
        if not already:
            is_arr = bool(re.match(r'^[a-zA-Z]_[a-zA-Z]$', var))
            results.append({
                'type': 'range', 'var': var, 'lo': -neg_hi, 'hi': hi,
                'is_array': is_arr,
                'array_name': var.split('_')[0] if is_arr else None,
            })

    pattern_one_sided_hi = re.compile(
        r'\b' + VAR_PAT + r'\s*(' + CMP_PAT + r')\s*' + NUM_PAT,
        re.UNICODE,
    )
    for m in pattern_one_sided_hi.finditer(text):
        var, op, hi_str = m.group(1), m.group(2), m.group(3)
        if len(var) > 12: continue
        if var.lower() in ('and', 'or', 'not', 'is', 'be', 'an', 'the'):
            continue
        start = m.start()
        prefix = text[max(0, start-6):start]
        if re.search(r'[*·⋅×+\-]\s*$', prefix):
            continue
        hi = parse_number(hi_str)
        if hi is None: continue
        if op == '<': hi = hi - 1
        already = any(c.get('var') == var and c.get('type') in ('range', 'chain', 'chain_arith')
                       for c in results)
        if not already:
            is_arr = bool(re.match(r'^[a-zA-Z]_[a-zA-Z]$', var))
            results.append({
                'type': 'range', 'var': var, 'lo': None, 'hi': hi,
                'is_array': is_arr,
                'array_name': var.split('_')[0] if is_arr else None,
            })

    pattern_one_sided_lo = re.compile(
        r'\b' + VAR_PAT + r'\s*(?:≥|>=|>)\s*' + NUM_PAT,
        re.UNICODE,
    )
    for m in pattern_one_sided_lo.finditer(text):
        var, lo_str = m.group(1), m.group(2)
        if len(var) > 12: continue
        if var.lower() in ('and', 'or', 'not', 'is', 'be', 'an', 'the'):
            continue
        lo = parse_number(lo_str)
        if lo is None: continue
        already = any(c.get('var') == var and c.get('type') in ('range', 'chain', 'chain_arith')
                       for c in results)
        if not already:
            is_arr = bool(re.match(r'^[a-zA-Z]_[a-zA-Z]$', var))
            results.append({
                'type': 'range', 'var': var, 'lo': lo, 'hi': None,
                'is_array': is_arr,
                'array_name': var.split('_')[0] if is_arr else None,
            })

    _exceed_pat = (
        r"(?:does\s+not\s+exceed|doesn'?t\s+exceed|won'?t\s+exceed"
        r"|will\s+not\s+exceed|is\s+at\s+most|don'?t\s+exceed|do\s+not\s+exceed|≤|<=)"
    )
    pattern_sum_tc = re.compile(
        r'(?i)\bsum\s+of\s+'
        r'(?:all\s+|the\s+(?:values?\s+(?:of\s+)?)?)?'
        r'([a-zA-Z])(?:_[a-zA-Z0-9{}_]*)?'
        r'[^.\n]{0,80}?'
        r'\s*' + _exceed_pat + r'\s*' + NUM_PAT,
        re.UNICODE
    )
    for m in pattern_sum_tc.finditer(text):
        var, hi_str = m.group(1), m.group(2)
        hi = parse_number(hi_str)
        if hi is None:
            continue
        already = any(c.get('type') == 'sum_tc' and c.get('var') == var for c in results)
        if not already:
            results.append({'type': 'sum_tc', 'var': var, 'hi': hi})

    pattern_sigma_tc = re.compile(
        r'(?:Σ|∑)\s*\(?\s*'
        r'([a-zA-Z](?:[\s·*⋅×]\s*[a-zA-Z])*)'
        r'\s*\)?\s*' + _exceed_pat + r'\s*' + NUM_PAT
        + r'(?P<scope>[^.\n]{0,80})',
        re.UNICODE | re.IGNORECASE
    )
    for m in pattern_sigma_tc.finditer(text):
        var_expr = m.group(1).strip()
        hi_str = m.group(2)
        scope = m.group("scope") or ""
        if not re.search(r'(?i)test\s*cases?|testcases?|queries|inputs?',
                          scope):
            continue
        hi = parse_number(hi_str)
        if hi is None:
            continue
        vars_in_expr = re.findall(r'[a-zA-Z]', var_expr)
        if not vars_in_expr:
            continue
        canonical = "·".join(vars_in_expr) if len(vars_in_expr) > 1 else vars_in_expr[0]
        already = any(c.get('type') == 'sum_tc' and c.get('var') == canonical
                      for c in results)
        if not already:
            results.append({'type': 'sum_tc', 'var': canonical, 'hi': hi})

    pattern_sum_product_tc = re.compile(
        r'(?i)\bsum\s+of\s+'
        r'(?:all\s+|the\s+(?:values?\s+(?:of\s+)?)?)?'
        r'\(?\s*([a-zA-Z])\s*[·*⋅×]\s*([a-zA-Z])\s*\)?'
        r'[^.\n]{0,80}?'
        r'\s*' + _exceed_pat + r'\s*' + NUM_PAT,
        re.UNICODE,
    )
    for m in pattern_sum_product_tc.finditer(text):
        var1, var2, hi_str = m.group(1), m.group(2), m.group(3)
        hi = parse_number(hi_str)
        if hi is None:
            continue
        canonical = f"{var1}·{var2}"
        already = any(c.get('type') == 'sum_tc' and c.get('var') == canonical
                      for c in results)
        if not already:
            results.append({'type': 'sum_tc', 'var': canonical, 'hi': hi})

    pattern_sum_multi_tc = re.compile(
        r'(?i)\bsum\s+of\s+([a-zA-Z])(?:_[a-zA-Z0-9{}_]*)?'
        r'(?:\s*(?:and|,)\s*(?:the\s+)?sum\s+of\s+([a-zA-Z])(?:_[a-zA-Z0-9{}_]*)?)?'
        r'(?:\s*(?:and|,)\s*(?:the\s+)?sum\s+of\s+([a-zA-Z])(?:_[a-zA-Z0-9{}_]*)?)?'
        r'[^.\n]{0,80}?\bover\s+(?:all\s+)?(?:test\s*cases?|testcases?)'
        r'[^.\n]{0,40}?\s*' + _exceed_pat + r'\s*' + NUM_PAT,
        re.UNICODE,
    )
    for m in pattern_sum_multi_tc.finditer(text):
        hi_str = m.group(m.lastindex)
        hi = parse_number(hi_str)
        if hi is None:
            continue
        for grp_idx in (1, 2, 3):
            v = m.group(grp_idx)
            if not v:
                continue
            already = any(c.get('type') == 'sum_tc' and c.get('var') == v
                          for c in results)
            if not already:
                results.append({'type': 'sum_tc', 'var': v, 'hi': hi})

    _noun_to_var: Dict[str, str] = {}
    pattern_var_noun = re.compile(
        r'(?i)\binteger\s+([a-zA-Z])\b[^—–\n]*?[—–]\s*(?:the\s+)?number\s+of\s+'
        r'([a-zA-Z][a-zA-Z\s]{0,40}?)(?:[.,;\n(]|$)',
        re.UNICODE
    )
    for m in pattern_var_noun.finditer(text):
        var_letter = m.group(1)
        noun_phrase = m.group(2).strip().rstrip('.,;')
        for word in noun_phrase.lower().split():
            if len(word) > 2:
                _noun_to_var.setdefault(word, var_letter)

    _across_pat = r'(?:over\s+all\s+|in\s+all\s+|across\s+all\s+)'
    _scope_pat = r'(?:test\s+cases?|testcases?|queries|inputs?)'
    pattern_total_tc = re.compile(
        r'(?i)(?:the\s+)?total\s+(?:number\s+of\s+)?'
        r'([a-zA-Z][a-zA-Z\s]{0,50}?)'
        r'(?:\s+on\s+[^.]*?)?'
        r'\s+' + _across_pat + _scope_pat +
        r'\s*' + _exceed_pat + r'\s*' + NUM_PAT,
        re.UNICODE
    )
    for m in pattern_total_tc.finditer(text):
        noun_phrase = m.group(1).strip()
        hi_str = m.group(2)
        hi = parse_number(hi_str)
        if hi is None:
            continue
        if re.search(r'\btest\s*cases?\b|\btestcases?\b', noun_phrase, re.I):
            continue
        found_var = None
        for word in noun_phrase.lower().split():
            if len(word) > 2 and word in _noun_to_var:
                found_var = _noun_to_var[word]
                break
        if found_var:
            already = any(c.get('type') == 'sum_tc' and c.get('var') == found_var for c in results)
            if not already:
                results.append({'type': 'sum_tc', 'var': found_var, 'hi': hi})

    if not results and problem_desc:
        fallback_text = problem_desc
        if constraints_text:
            fallback_text += "\n" + constraints_text
        results = parse_constraints(fallback_text, None, None)

    pd_vars = {c['var'] for c in results
               if c.get('type') in ('product_div', 'chain_div')}
    if pd_vars:
        results = [
            c for c in results
            if not (c.get('type') == 'chain' and c.get('var') in pd_vars)
        ]

    return results

def parse_input_structure(input_desc: str) -> Dict[str, Any]:
    text = input_desc or ""
    lines_lower = text.lower()

    structure = {
        'test_cases_var': None,
        'header_vars': [],
        'arrays': [],
        'edges': None,
        'strings': [],
        'matrix': None,
        'grid': None,
        'pairs': [],
    }

    if re.search(r'(?:number of )?test\s*cases?', lines_lower):
        t_match = (
            re.search(r'\bintegers?\s+([a-zA-Z])\b[^.\n]*?(?:number of )?test\s*cases?', lines_lower)
            or re.search(r'\b([a-zA-Z])\s*\([^)]*\)\s*(?:—|--|-).*?(?:number of )?test\s*cases?', lines_lower)
        )
        structure['test_cases_var'] = t_match.group(1) if t_match else 't'

    _HEADER_STOPVARS = frozenset({'a', 'i', 'e', 'o'})
    _first_lines = '\n'.join(lines_lower.split('\n')[:2])

    header_patterns = [
        r'(?:contains?\s+)?(?:two|three|four)?\s*integers?\s+([a-zA-Z](?:\s*[,and\s]+\s*[a-zA-Z])*)',
        r'integers?\s+([a-zA-Z])\s+and\s+([a-zA-Z])',
        r'integers?\s+([a-zA-Z]),\s*([a-zA-Z])\s*(?:,\s*and\s+([a-zA-Z]))?',
    ]
    for pat in header_patterns:
        m = re.search(pat, _first_lines)
        if m:
            for g in m.groups():
                if g:
                    for v in re.findall(r'\b([a-zA-Z])\b', g):
                        if v not in structure['header_vars'] and v not in _HEADER_STOPVARS:
                            structure['header_vars'].append(v)
            if structure['header_vars']:
                break

    if not structure['header_vars']:
        first_line_m = re.search(
            r'first line.*?(?:integer|number)s?\s+([a-zA-Z](?:\s*(?:,|and)\s*[a-zA-Z])*)',
            _first_lines
        )
        if first_line_m:
            for v in re.findall(r'\b([a-zA-Z])\b', first_line_m.group(1)):
                if v not in structure['header_vars'] and v not in _HEADER_STOPVARS:
                    structure['header_vars'].append(v)

    array_patterns = [
        r'\b([a-zA-Z])\b\s+integers?\s+([a-zA-Z])(?:_|\{)',
        r'\b([a-zA-Z])\b\s+(?:[\w-]+\s+)+integers?\s+([a-zA-Z])(?:_|\{)',
        r'(?:next|second|third).*?line.*?\b([a-zA-Z])\b\s+integers?\s+([a-zA-Z])',
        r'\b([a-zA-Z])\b\s+(?:numbers?|values?|elements?)\s+([a-zA-Z])',
        r'(?:an? )?(?:array|sequence|permutation)\s+([a-zA-Z])\s+(?:of|consisting)',
        r'(?:array|sequence|permutation)\s+of\s+\b([a-zA-Z])\b\s+integers?\s*.*?([a-zA-Z])(?:_|\{)',
        r'elements?\s+of\s+(?:the\s+)?(?:array|sequence)\s+([a-zA-Z])(?:_|\{)',
        r'(?:a\s+)?(?:array|sequence|permutation)\s+of\s+(?:[\w-]+\s+)*integers?\s+([a-zA-Z])(?:_|\{)',
    ]
    _per_line = bool(re.search(
        r'(?:i.th|each)\s+of\s+(?:the\s+)?(?:next|following)\s+\w+\s+lines?\s+contains?',
        lines_lower
    ))
    for pat in array_patterns:
        for m in re.finditer(pat, lines_lower):
            groups = m.groups()
            if len(groups) >= 2:
                length_var, arr_name = groups[0], groups[1]
                if length_var in _HEADER_STOPVARS:
                    continue
                if length_var == structure.get('test_cases_var'):
                    continue
                if arr_name not in [a['name'] for a in structure['arrays']]:
                    structure['arrays'].append({
                        'name': arr_name, 'length_var': length_var,
                        'element_constraint': f'{arr_name}_i',
                        'per_line': _per_line,
                    })
            elif len(groups) == 1:
                arr_name = groups[0]
                if arr_name not in [a['name'] for a in structure['arrays']]:
                    structure['arrays'].append({
                        'name': arr_name,
                        'length_var': structure['header_vars'][0] if structure['header_vars'] else 'n',
                        'element_constraint': f'{arr_name}_i',
                        'per_line': _per_line,
                    })

    if re.search(r'(?:tree|n\s*-\s*1\s+(?:lines?|edges?))', lines_lower):
        edge_fmt = 'u v w' if re.search(r'[uvw]_i.*[uvw]_i.*(?:w|t|c)', lines_lower) else 'u v'
        structure['edges'] = {'count_expr': 'n-1', 'format': edge_fmt, 'is_tree': True}
    elif re.search(r'([a-zA-Z])\s+edges?', lines_lower):
        m = re.search(r'([a-zA-Z])\s+edges?', lines_lower)
        structure['edges'] = {'count_expr': m.group(1) if m else 'm', 'format': 'u v', 'is_tree': False}

    str_patterns = [r'(?:a )?string\s+([a-zA-Z])', r'\b([a-zA-Z])\b\s+consisting of.*?(?:letters|characters)']
    _is_binary = bool(re.search(
        r"binary\s+string"
        r"|consist(?:s|ing)\s+of\s+(?:the\s+)?(?:digits?\s+|characters?\s+)?(?:zeros?\s+and\s+ones?|0s?\s+and\s+1s?|['\"]?0['\"]?\s+and\s+['\"]?1['\"]?)",
        lines_lower
    ))
    _header_pool = [hv for hv in structure['header_vars'] if hv not in ('t', 'q')]
    _str_len_idx = 0
    for pat in str_patterns:
        for m in re.finditer(pat, lines_lower):
            s_name = m.group(1)
            if s_name in [s['name'] for s in structure['strings']]: continue
            if s_name in ('n', 'of'): continue
            _end = m.end()
            _stops = [lines_lower.find('\n', _end),
                      lines_lower.find('. ', _end)]
            _stops = [s for s in _stops if s != -1]
            _ctx_end = min(_stops) if _stops else _end + 200
            ctx = lines_lower[_end:_ctx_end + 1]
            length_var = None
            mlen = re.search(
                r'(?:of\s+|with\s+|having\s+)?\bl(?:en)?gth\s+([a-zA-Z])\b', ctx)
            if mlen:
                cand = mlen.group(1)
                if cand in _header_pool:
                    length_var = cand
            if length_var is None:
                mch = re.search(
                    r'\b([a-zA-Z])\s+(?:lower|upper|character|letter|digit|binary)',
                    ctx)
                if mch and mch.group(1) in _header_pool:
                    length_var = mch.group(1)
            if length_var is None:
                if _header_pool:
                    length_var = _header_pool[_str_len_idx % len(_header_pool)]
                    _str_len_idx += 1
            structure['strings'].append({
                'name': s_name,
                'length_var': length_var,
                'charset': '01' if _is_binary else None,
            })

    if re.search(
        r'(?:grid|matrix|table)'
        r'|(?:next|following)\s+\w+\s+lines?\b[\s\S]*?(?:\w+\s+characters?|consists?\s+of\s+(?:the\s+)?characters?)',
        lines_lower
    ):
        rows = structure['header_vars'][0] if len(structure['header_vars']) > 0 else 'n'
        cols = structure['header_vars'][1] if len(structure['header_vars']) > 1 else 'm'
        mat_name = None
        m_name = re.search(
            r'(?:dimensions?\s+of\s+(?:the\s+)?(?:grid|matrix|field|table)\s+([a-zA-Z])\b'
            r'|\b(?:grid|matrix|field|table)\s+([a-zA-Z])\s+(?:consist|of\s+size|of\s+dimensions))',
            lines_lower
        )
        if m_name:
            mat_name = next((g for g in m_name.groups() if g), None)
        structure['matrix'] = {'rows': rows, 'cols': cols, 'name': mat_name}

    if re.search(r'x_i.*y_i|coordinates', lines_lower):
        count_var = structure['header_vars'][0] if structure['header_vars'] else 'n'
        structure['pairs'].append({'vars': ['x', 'y'], 'count_var': count_var})

    tcv = structure.get('test_cases_var')
    if tcv and tcv in structure['header_vars']:
        structure['header_vars'].remove(tcv)

    if tcv and not structure['header_vars']:
        _cleaned = re.sub(r'\([^)]*\)', '', lines_lower)

        _var_extract_pat = r'([a-zA-Z](?:\s*(?:,\s*(?:and\s+)?|and\s+)[a-zA-Z])*)'
        _int_pat = r'(?:the\s+)?(?:integer|number)s?\s+'

        per_case_m = re.search(
            r'(?:each|every|per)\s+test\s+case.*?' + _int_pat + _var_extract_pat,
            _cleaned
        )

        if not per_case_m:
            per_case_m = re.search(
                r'(?:the\s+)?first\s+line\s+of\s+(?:the\s+|a\s+)?'
                r'(?:test\s+case|case)\s*(?:description\s+)?contains?\s+'
                r'(?:a\s+)?(?:single\s+)?(?:two\s+|three\s+|four\s+)?'
                + _int_pat + _var_extract_pat,
                _cleaned
            )

        if not per_case_m:
            _fl_iter = list(re.finditer(
                r'(?:the\s+)?first\s+line\s+(?:of\s+\w+\s+)?contains?\s+'
                r'(?:a\s+)?(?:single\s+)?(?:two\s+|three\s+|four\s+)?'
                + _int_pat + _var_extract_pat,
                _cleaned
            ))
            if len(_fl_iter) >= 2:
                per_case_m = _fl_iter[1]

        _int_pat_colon = r'(?:the\s+)?(?:integer|number)s?:?\s+'

        if not per_case_m:
            per_case_m = re.search(
                r'(?:the\s+)?first\s+line\s+of\s+(?:each|every)\s+'
                r'(?:test\s*cases?|case)\s*(?:description\s+)?contains?:?\s+'
                r'(?:a\s+)?(?:an\s+)?(?:single\s+)?(?:two\s+|three\s+|four\s+|one\s+|\d+\s+)?'
                + _int_pat_colon + _var_extract_pat,
                _cleaned
            )

        if not per_case_m:
            per_case_m = re.search(
                r'(?:only\s+)?(?:one|the)\s+line\s+of\s+(?:each|every)\s+'
                r'(?:test\s*cases?|case)\s*contains?\s+'
                r'(?:a\s+)?(?:single\s+)?(?:two\s+|three\s+|four\s+|one\s+|\d+\s+)?'
                + _int_pat_colon + _var_extract_pat,
                _cleaned
            )

        if not per_case_m:
            per_case_m = re.search(
                r'(?:each|every|per)\s+test\s*cases?[^.]*\.[^.]*?'
                r'(?:contains?|containing)\s+'
                r'(?:a\s+)?(?:an\s+)?(?:single\s+)?(?:two\s+|three\s+|four\s+|one\s+|\d+\s+)?'
                + _int_pat_colon + _var_extract_pat,
                _cleaned
            )

        if not per_case_m:
            per_case_m = re.search(
                r'(?:next\s+(?:comes?\s+)?(?:a\s+)?line|(?:next|then)\s+(?:is\s+|there\s+is\s+)?a\s+line)\s*'
                r'(?:that\s+)?contains?\s+'
                r'(?:a\s+)?(?:an\s+)?(?:single\s+)?(?:two\s+|three\s+|four\s+|one\s+|\d+\s+)?'
                + _int_pat_colon + _var_extract_pat,
                _cleaned
            )

        if per_case_m:
            _match_text = per_case_m.group(1)
            _is_var_list = ',' in _match_text or ' and ' in _match_text
            for v in re.findall(r'\b([a-zA-Z])\b', _match_text):
                if v == tcv:
                    continue
                if v in _HEADER_STOPVARS and not (v == 'a' and _is_var_list):
                    continue
                if v not in structure['header_vars']:
                    structure['header_vars'].append(v)

    if tcv and not structure['strings'] and not structure['matrix']:
        _char_line_m = re.search(
            r"(?:next|following)\s+(\w+)\s+lines?\b[\s\S]*?"
            r"(?:consist(?:s|ing)\s+of\s+(?:the\s+)?characters?\s*['\"]?[01]['\"]?"
            r"|characters?\s*['\"]?0['\"]?\s*and\s*['\"]?1['\"]?)",
            lines_lower
        )
        if _char_line_m:
            _num_word = _char_line_m.group(1)
            _word_to_int = {'two': 2, 'three': 3, 'four': 4, 'five': 5,
                            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
            _num_lines = _word_to_int.get(_num_word)
            if _num_lines is None and _num_word.isdigit():
                _num_lines = int(_num_word)
            if _num_lines and _num_lines <= 10:
                _len_var = structure['header_vars'][0] if structure['header_vars'] else 'n'
                for _si in range(_num_lines):
                    _sname = f'_row{_si+1}'
                    structure['strings'].append({
                        'name': _sname,
                        'length_var': _len_var,
                        'charset': '01',
                    })

    return structure

def analyze_dependencies(constraints: List[Dict[str, Any]]) -> Dict[str, Any]:
    independent = []
    dependent = []
    dep_graph: Dict[str, Set[str]] = {}
    routing_log = []

    smt_vars: Set[str] = set()
    for c in constraints:
        involved_vars = _extract_constraint_vars(c)
        if len(involved_vars) >= 2:
            smt_vars.update(involved_vars)
            var_list = list(involved_vars)
            for i in range(len(var_list)):
                for j in range(i + 1, len(var_list)):
                    dep_graph.setdefault(var_list[i], set()).add(var_list[j])
                    dep_graph.setdefault(var_list[j], set()).add(var_list[i])

    for c in constraints:
        involved_vars = _extract_constraint_vars(c)

        if len(involved_vars) >= 2:
            dependent.append(c)
            routing_log.append({
                'constraint': _constraint_to_str(c),
                'vars': list(involved_vars),
                'route': 'smt',
            })
        elif len(involved_vars) == 1:
            var_name = next(iter(involved_vars))
            if var_name in smt_vars:
                dependent.append(c)
                routing_log.append({
                    'constraint': _constraint_to_str(c),
                    'vars': list(involved_vars),
                    'route': 'smt (re-routed: var shares dependency)',
                })
            else:
                independent.append(c)
                routing_log.append({
                    'constraint': _constraint_to_str(c),
                    'vars': list(involved_vars),
                    'route': 'rule',
                })
        else:
            independent.append(c)
            routing_log.append({
                'constraint': _constraint_to_str(c),
                'vars': [],
                'route': 'rule',
            })

    dep_graph_serializable = {k: list(v) for k, v in dep_graph.items()}

    return {
        'independent': independent,
        'dependent': dependent,
        'dependency_graph': dep_graph_serializable,
        'routing_log': routing_log,
    }

def _extract_constraint_vars(c: Dict[str, Any]) -> Set[str]:
    involved = set()

    c_type = c.get('type', '')
    if c_type == 'range':
        if not c.get('is_array') and 'var' in c:
            involved.add(c['var'])
    elif c_type == 'chain':
        if 'var' in c:
            involved.add(c['var'])
        if 'hi_var' in c:
            involved.add(c['hi_var'])
    elif c_type in ('product', 'sum'):
        for v in c.get('vars', []):
            involved.add(v)
    elif c_type == 'string_length':
        if 'var' in c:
            involved.add(c['var'])
    elif c_type == 'sum_tc':
        if 'var' in c:
            involved.add(c['var'])
    elif c_type == 'product_div':
        if 'var' in c:
            involved.add(c['var'])
        for v in c.get('product_vars', []):
            involved.add(v)
    elif c_type == 'chain_div':
        if 'var' in c:
            involved.add(c['var'])
        if 'hi_var' in c:
            involved.add(c['hi_var'])

    return involved

def _fmt_num(v) -> str:
    if isinstance(v, int) and abs(v) > 10**18:
        import math
        exp = int(math.log10(abs(v))) if v != 0 else 0
        sign = '-' if v < 0 else ''
        return f"{sign}~10^{exp}"
    return str(v)

def _constraint_to_str(c: Dict[str, Any]) -> str:
    c_type = c.get('type', '')
    if c_type == 'range':
        return f"{_fmt_num(c.get('lo', '?'))} <= {c.get('var', '?')} <= {_fmt_num(c.get('hi', '?'))}"
    elif c_type == 'chain':
        return f"{_fmt_num(c.get('lo', '?'))} <= {c.get('var', '?')} <= {c.get('hi_var', '?')}"
    elif c_type == 'product':
        return f"{' * '.join(c.get('vars', []))} <= {_fmt_num(c.get('hi', '?'))}"
    elif c_type == 'sum':
        return f"{' + '.join(c.get('vars', []))} <= {_fmt_num(c.get('hi', '?'))}"
    elif c_type == 'string_length':
        return f"{_fmt_num(c.get('lo', '?'))} <= |{c.get('var', '?')}| <= {_fmt_num(c.get('hi', '?'))}"
    elif c_type == 'sum_tc':
        return f"sum_of_{c.get('var', '?')} <= {_fmt_num(c.get('hi', '?'))}"
    return str(c)

_M1_DIVERSITY_PCTS: Dict[str, List[float]] = {
    'min':  [0.00, 0.004, 0.008, 0.002, 0.006],
    'mid':  [0.50, 0.48, 0.52, 0.49, 0.51],
    'max':  [1.00, 0.98, 0.96, 0.99, 0.97],
}

def _pick_value(lo: int, hi: int, strategy: str, pct_override: Optional[float] = None) -> int:
    if pct_override is not None:
        return lo + int((hi - lo) * pct_override)
    if strategy == 'min':
        return lo
    elif strategy == 'p80':
        return lo + int((hi - lo) * 0.8)
    elif strategy == 'mid':
        return lo + (hi - lo) // 2
    else:
        return hi

def generate_rule_based(
    independent_constraints: List[Dict[str, Any]],
    value_strategy: str = 'max',
    pct_override: Optional[float] = None,
) -> Dict[str, int]:
    values = {}

    for c in independent_constraints:
        if c['type'] == 'range' and not c.get('is_array'):
            var = c['var']
            target = _pick_value(c['lo'], c['hi'], value_strategy, pct_override)
            if var not in values:
                values[var] = target
            else:
                if pct_override is not None:
                    existing_pct = (values[var] - c['lo']) / max(c['hi'] - c['lo'], 1)
                    if abs(existing_pct - pct_override) > abs(
                        (target - c['lo']) / max(c['hi'] - c['lo'], 1) - pct_override
                    ):
                        values[var] = target
                elif value_strategy in ('max', 'p80'):
                    values[var] = min(values[var], target)
                elif value_strategy == 'min':
                    values[var] = max(values[var], target)
                else:
                    values[var] = (values[var] + target) // 2

        elif c['type'] == 'string_length':
            var = c['var']
            if var not in values:
                values[var] = _pick_value(c['lo'], c['hi'], value_strategy, pct_override)

    return values

def generate_smt_based(
    dependent_constraints: List[Dict[str, Any]],
    known_values: Dict[str, int],
    z3_timeout: int = 30,
    value_strategy: str = 'max',
    pct_override: Optional[float] = None,
) -> Dict[str, int]:
    if not dependent_constraints:
        return {}

    try:
        from utils.cpsat_solver import solve as _cpsat_solve_top
    except ImportError:
        return _fallback_dependent_solver(dependent_constraints, known_values,
                                          value_strategy=value_strategy,
                                          pct_override=pct_override)

    return _cpsat_solve_top(
        dependent_constraints,
        known_values,
        tcv=None,
        value_strategy=value_strategy,
        pct_override=pct_override,
        time_limit_sec=float(z3_timeout),
    )

def _fallback_dependent_solver(
    dependent_constraints: List[Dict[str, Any]],
    known_values: Dict[str, int],
    value_strategy: str = 'max',
    pct_override: Optional[float] = None,
) -> Dict[str, int]:
    values = {}

    for c in dependent_constraints:
        if c['type'] == 'range' and not c.get('is_array'):
            var = c['var']
            target = _pick_value(c['lo'], c['hi'], value_strategy, pct_override)
            if var not in values:
                values[var] = target
            else:
                if pct_override is not None or value_strategy in ('max', 'p80'):
                    values[var] = min(values[var], target)
                elif value_strategy == 'min':
                    values[var] = max(values[var], target)
                else:
                    values[var] = (values[var] + target) // 2

    for c in dependent_constraints:
        if c['type'] == 'product' and len(c['vars']) == 2:
            v1, v2 = c['vars']
            val1 = values.get(v1, 1)
            val2 = values.get(v2, 1)
            if val1 * val2 > c['hi']:
                if value_strategy == 'min':
                    values[v1] = max(1, c.get('lo', 1) if isinstance(c.get('lo'), int) else 1)
                    values[v2] = max(1, c.get('lo', 1) if isinstance(c.get('lo'), int) else 1)
                elif value_strategy in ('mid', 'p80'):
                    pct = 0.8 if value_strategy == 'p80' else 0.5
                    balanced = int(math.isqrt(c['hi']) * pct)
                    balanced = max(balanced, 1)
                    values[v1] = balanced
                    values[v2] = balanced
                else:
                    balanced = int(math.isqrt(c['hi']))
                    values[v1] = balanced
                    values[v2] = c['hi'] // balanced if balanced > 0 else 1

    for c in dependent_constraints:
        if c['type'] == 'sum' and len(c['vars']) == 2:
            v1, v2 = c['vars']
            val1 = values.get(v1, 1)
            val2 = values.get(v2, 1)
            if val1 + val2 > c['hi']:
                if value_strategy == 'min':
                    values[v1] = max(1, c.get('lo', 1) if isinstance(c.get('lo'), int) else 1)
                    values[v2] = max(1, c.get('lo', 1) if isinstance(c.get('lo'), int) else 1)
                elif value_strategy == 'mid':
                    half = max(1, c['hi'] // 2)
                    values[v1] = int(half * 0.5)
                    values[v2] = c['hi'] - values[v1]
                else:
                    half = c['hi'] // 2
                    values[v1] = half
                    values[v2] = c['hi'] - half

    for c in dependent_constraints:
        if c['type'] == 'chain':
            hi_val = values.get(c['hi_var'])
            if hi_val is not None:
                if value_strategy == 'min':
                    values[c['var']] = c['lo']
                elif value_strategy in ('mid', 'p80'):
                    pct = 0.8 if value_strategy == 'p80' else 0.5
                    values[c['var']] = c['lo'] + int((hi_val - c['lo']) * pct)
                else:
                    values[c['var']] = hi_val
            else:
                values[c['var']] = c['lo']

    return values

def generate_boundary_slow(
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
    z3_timeout: int = 30,
    array_fill: str = 'max',
    graph_structure: str = 'star',
    value_strategy: str = 'max',
    compact_only: bool = False,
    tc_idx: int = 0,
    string_strategy: str = 'default',
) -> Optional[Dict[str, Any]]:
    if not constraints:
        return None

    tcv = structure.get('test_cases_var')
    if tcv:
        converted = []
        for c in constraints:
            if c.get('type') == 'sum_tc':
                if c.get('var') == tcv:
                    continue
                if c.get('var'):
                    converted.append({
                        'type': 'product',
                        'vars': [tcv, c['var']],
                        'hi': c['hi'],
                        'is_sum_tc': True,
                    })
                    continue
            converted.append(c)
        constraints = converted

    dep_analysis = analyze_dependencies(constraints)

    has_constraints = bool(dep_analysis['independent'] or dep_analysis['dependent'])
    pct_list = _M1_DIVERSITY_PCTS.get(value_strategy)
    pct_override: Optional[float] = None
    if has_constraints and pct_list is not None and tc_idx > 0:
        pct_override = pct_list[tc_idx % len(pct_list)]

    rule_values = generate_rule_based(dep_analysis['independent'],
                                      value_strategy=value_strategy,
                                      pct_override=pct_override)

    smt_values = generate_smt_based(
        dep_analysis['dependent'], rule_values,
        z3_timeout=z3_timeout, value_strategy=value_strategy,
        pct_override=pct_override,
    )

    values = {**rule_values, **smt_values}

    for hv in structure.get('header_vars', []):
        if hv not in values:
            values[hv] = 1

    if compact_only:
        return {
            'values': values,
            '_method': 'boundary_slow_compact',
            '_routing': dep_analysis.get('routing_log', []),
        }

    testcase = _fill_data_structures(
        values, constraints, structure,
        array_fill=array_fill, graph_structure=graph_structure,
        string_strategy=string_strategy,
    )
    testcase['_routing'] = dep_analysis['routing_log']
    testcase['_method'] = 'boundary_slow'

    return testcase

def _fill_data_structures(
    values: Dict[str, int],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
    array_fill: str = 'max',
    graph_structure: str = 'star',
    string_strategy: str = 'default',
) -> Dict[str, Any]:
    testcase = {'values': values}

    element_ranges = {}
    for c in constraints:
        if c.get('is_array') and c['type'] == 'range':
            arr_name = c.get('array_name', c['var'].split('_')[0])
            element_ranges[arr_name] = (c['lo'], c['hi'])

    for arr in structure.get('arrays', []):
        arr_name = arr['name']
        length = min(values.get(arr['length_var'], 1), 500000)
        lo, hi = element_ranges.get(arr_name, (1, 1000000000))

        if array_fill == 'max':
            testcase[f'array_{arr_name}'] = [hi] * length
        elif array_fill == 'min':
            testcase[f'array_{arr_name}'] = [lo] * length
        elif array_fill == 'random':
            testcase[f'array_{arr_name}'] = [random.randint(lo, hi) for _ in range(length)]
        elif array_fill == 'mixed':
            testcase[f'array_{arr_name}'] = [hi if i % 2 == 0 else lo for i in range(length)]
        elif array_fill == 'reverse_sorted':
            if length <= (hi - lo + 1):
                testcase[f'array_{arr_name}'] = [hi - i * max(1, (hi - lo) // max(length - 1, 1)) for i in range(length)]
                testcase[f'array_{arr_name}'] = [max(lo, min(hi, v)) for v in testcase[f'array_{arr_name}']]
            else:
                testcase[f'array_{arr_name}'] = [hi - (i % max(1, hi - lo + 1)) for i in range(length)]
                testcase[f'array_{arr_name}'] = [max(lo, min(hi, v)) for v in testcase[f'array_{arr_name}']]
        elif array_fill == 'sawtooth':
            period = max(2, int(length ** 0.5))
            testcase[f'array_{arr_name}'] = [lo + (i % period) * max(1, (hi - lo) // max(period - 1, 1))
                                              for i in range(length)]
            testcase[f'array_{arr_name}'] = [max(lo, min(hi, v)) for v in testcase[f'array_{arr_name}']]
        elif array_fill == 'alternating_extremes':
            testcase[f'array_{arr_name}'] = [hi if i % 2 == 0 else lo for i in range(length)]
        elif array_fill == 'random_heavy':
            testcase[f'array_{arr_name}'] = [random.randint(hi // 2, hi) if random.random() < 0.7
                                              else random.randint(lo, hi // 2)
                                              for _ in range(length)]
            testcase[f'array_{arr_name}'] = [max(lo, min(hi, v)) for v in testcase[f'array_{arr_name}']]
        else:
            testcase[f'array_{arr_name}'] = [hi] * length

    for s in structure.get('strings', []):
        length = min(values.get(s['length_var'], 1), 500000)
        charset = s.get('charset', 'lowercase')
        if charset == '01':
            if string_strategy == 'anti_period':
                testcase[f'string_{s["name"]}'] = ('0' * (length - 1) + '1')[:length]
            else:
                testcase[f'string_{s["name"]}'] = ('01' * (length // 2 + 1))[:length]
        else:
            if string_strategy == 'periodic_ab':
                testcase[f'string_{s["name"]}'] = ('ab' * (length // 2 + 1))[:length]
            elif string_strategy == 'single_char':
                testcase[f'string_{s["name"]}'] = 'a' * length
            elif string_strategy == 'anti_period':
                testcase[f'string_{s["name"]}'] = 'a' * (length - 1) + 'b' if length > 0 else ''
            else:
                testcase[f'string_{s["name"]}'] = 'a' * length

    if structure.get('edges'):
        edge_info = structure['edges']
        if edge_info.get('is_tree'):
            n_val = min(values.get('n', 2), 500000)
            if graph_structure == 'star':
                edges = [(1, i) for i in range(2, n_val + 1)]
            elif graph_structure == 'chain':
                edges = [(i, i + 1) for i in range(1, n_val)]
            elif graph_structure == 'caterpillar':
                spine_len = max(2, n_val // 2)
                edges = [(i, i + 1) for i in range(1, spine_len)]
                leaf_id = spine_len + 1
                for spine_node in range(1, spine_len + 1):
                    if leaf_id > n_val:
                        break
                    edges.append((spine_node, leaf_id))
                    leaf_id += 1
                while leaf_id <= n_val:
                    edges.append((leaf_id - 1, leaf_id))
                    leaf_id += 1
            elif graph_structure == 'random_deep':
                edges = []
                for i in range(2, n_val + 1):
                    window = max(1, int(i ** 0.5))
                    parent = random.randint(max(1, i - window), i - 1)
                    edges.append((parent, i))
            else:
                edges = []
                for i in range(2, n_val + 1):
                    edges.append((random.randint(1, i - 1), i))
            testcase['edges'] = edges
            if 'w' in edge_info['format'] or 't' in edge_info['format']:
                testcase['edge_weights'] = [random.randint(0, 1) for _ in edges]
        else:
            count_var = edge_info['count_expr']
            m_val = min(values.get(count_var, values.get('m', 1)), 500000)
            n_val = max(values.get('n', 2), 2)
            edges = [(random.randint(1, n_val), random.randint(1, n_val)) for _ in range(m_val)]
            testcase['edges'] = edges

    for pair in structure.get('pairs', []):
        count = min(values.get(pair['count_var'], 1), 500000)
        lo_x, hi_x, lo_y, hi_y = -100000, 100000, -100000, 100000
        for c in constraints:
            v = c.get('var', '')
            if v == f'{pair["vars"][0]}_i' or v == pair['vars'][0]:
                lo_x, hi_x = c.get('lo', lo_x), c.get('hi', hi_x)
            if v == f'{pair["vars"][1]}_i' or v == pair['vars'][1]:
                lo_y, hi_y = c.get('lo', lo_y), c.get('hi', hi_y)
        if array_fill == 'min':
            pairs_data = [(lo_x, lo_y)] * count
        elif array_fill == 'random':
            pairs_data = [(random.randint(lo_x, hi_x), random.randint(lo_y, hi_y))
                          for _ in range(count)]
        elif array_fill == 'mixed':
            pairs_data = [(hi_x, hi_y) if i % 2 == 0 else (lo_x, lo_y) for i in range(count)]
        else:
            pairs_data = [(hi_x, hi_y)] * count
        testcase[f'pairs_{pair["vars"][0]}_{pair["vars"][1]}'] = pairs_data

    if structure.get('matrix'):
        rows = min(values.get(structure['matrix']['rows'], 1), 1000)
        cols = min(values.get(structure['matrix']['cols'], 1), 1000)
        testcase['matrix'] = [['a'] * cols for _ in range(rows)]

    return testcase

ALGO_PATTERNS = {
    'sorting': [
        r'\bsort(?:ing|ed)?\b', r'\border(?:ing|ed)?\b', r'\bpermutation\b',
        r'\blexicograph', r'\binversion', r'\bascend', r'\bdescend',
    ],
    'tree': [
        r'\btree\b', r'\brooted\b.*\btree\b', r'\bsubtree\b',
        r'\blca\b', r'\bancestor\b', r'\bparent\b.*\bnode\b',
    ],
    'graph': [
        r'\bgraph\b', r'\bvertices\b.*\bedges\b', r'\bshortest path\b',
        r'\bconnected\b.*\bcomponent', r'\bbfs\b', r'\bdfs\b', r'\bcycle\b',
    ],
    'string': [
        r'\bstring\b', r'\bsubstring\b', r'\bpalindrome\b',
        r'\bprefix\b', r'\bsuffix\b', r'\bpattern\b.*\bmatch',
    ],
    'binary_search': [
        r'\bminimum\b.*\bmaximum\b', r'\bmaximize\b.*\bminimize\b',
        r'\bbinary search\b', r'\bmonotoni', r'\boptimal\b.*\bvalue\b',
    ],
    'dp': [
        r'\bsubsequence\b', r'\bsubarray\b', r'\bknapsack\b',
        r'\boptimal\b', r'\bmaximum\b.*\bsum\b', r'\bminimum\b.*\bcost\b',
        r'\bdynamic\b', r'\btransition\b',
    ],
    'greedy': [
        r'\bgreedy\b', r'\binterval\b', r'\bschedul', r'\bdeadline\b',
    ],
    'math': [
        r'\bprime\b', r'\bdivisor\b', r'\bgcd\b', r'\bmodulo\b',
        r'\bcombinat', r'\bfactorial\b',
    ],
    'two_pointer': [
        r'\btwo.?pointer\b', r'\bsliding.?window\b', r'\bwindow\s+size\b',
        r'\bsubarray\s+(?:sum|length)\b', r'\bmaximum\s+subarray\b',
        r'\bpointers?\s+(?:from|at)\s+(?:both|two)\s+ends?\b',
    ],
    'segment_tree': [
        r'\bsegment\s+tree\b', r'\bfenwick\b', r'\bbinary\s+indexed\b',
        r'\brange\s+(?:sum|min|max|query|update)\b', r'\bpoint\s+update\b',
        r'\bprefix\s+sum\b', r'\bsparse\s+table\b',
    ],
    'heap': [
        r'\bpriority\s+queue\b', r'\bheap\b', r'\bkth\s+(?:smallest|largest)\b',
        r'\bmedian\b', r'\bmin.?heap\b', r'\bmax.?heap\b',
    ],
    'number_theory': [
        r'\bgcd\b', r'\blcm\b', r'\beuclid', r'\bcoprime\b',
        r'\bprime\s+factor', r'\bsieve\b', r'\bdivisib',
    ],
}

def detect_problem_types(problem_desc: str, input_desc: str) -> List[str]:
    text = (problem_desc + " " + input_desc).lower()
    detected = []

    for algo_type, patterns in ALGO_PATTERNS.items():
        score = sum(1 for p in patterns if re.search(p, text))
        if score >= 1:
            detected.append((algo_type, score))

    detected.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in detected]

def select_worst_case_mutations(
    structure: Dict[str, Any],
    problem_desc: str,
    input_desc: str,
) -> Dict[str, Any]:
    candidates = {}
    detected_structures = []
    routing_log = []

    edges = structure.get('edges')
    arrays = structure.get('arrays', [])
    strings = structure.get('strings', [])
    matrix = structure.get('matrix')
    pairs = structure.get('pairs', [])

    if edges and edges.get('is_tree'):
        detected_structures.append('tree')
        for p in STRUCTURE_ANTIPATTERN_MAP['tree']['patterns']:
            candidates[p] = candidates.get(p, 0) + 1
        routing_log.append({
            'axis': 'structure', 'type': 'tree',
            'reason': 'edges with is_tree=True',
            'patterns': STRUCTURE_ANTIPATTERN_MAP['tree']['patterns'],
        })
    elif edges and not edges.get('is_tree'):
        detected_structures.append('graph')
        for p in STRUCTURE_ANTIPATTERN_MAP['graph']['patterns']:
            candidates[p] = candidates.get(p, 0) + 1
        routing_log.append({
            'axis': 'structure', 'type': 'graph',
            'reason': 'edges with is_tree=False',
            'patterns': STRUCTURE_ANTIPATTERN_MAP['graph']['patterns'],
        })

    if arrays:
        detected_structures.append('array')
        for p in STRUCTURE_ANTIPATTERN_MAP['array']['patterns']:
            candidates[p] = candidates.get(p, 0) + 1
        routing_log.append({
            'axis': 'structure', 'type': 'array',
            'reason': f'{len(arrays)} array(s) detected',
            'patterns': STRUCTURE_ANTIPATTERN_MAP['array']['patterns'],
        })

    if strings:
        detected_structures.append('string')
        for p in STRUCTURE_ANTIPATTERN_MAP['string']['patterns']:
            candidates[p] = candidates.get(p, 0) + 1
        routing_log.append({
            'axis': 'structure', 'type': 'string',
            'reason': f'{len(strings)} string(s) detected',
            'patterns': STRUCTURE_ANTIPATTERN_MAP['string']['patterns'],
        })

    if matrix:
        detected_structures.append('matrix')
        for p in STRUCTURE_ANTIPATTERN_MAP['matrix']['patterns']:
            candidates[p] = candidates.get(p, 0) + 1
        routing_log.append({
            'axis': 'structure', 'type': 'matrix',
            'reason': 'matrix structure detected',
            'patterns': STRUCTURE_ANTIPATTERN_MAP['matrix']['patterns'],
        })

    if pairs:
        detected_structures.append('pairs')
        for p in STRUCTURE_ANTIPATTERN_MAP['pairs']['patterns']:
            candidates[p] = candidates.get(p, 0) + 1
        routing_log.append({
            'axis': 'structure', 'type': 'pairs',
            'reason': f'{len(pairs)} pair group(s) detected',
            'patterns': STRUCTURE_ANTIPATTERN_MAP['pairs']['patterns'],
        })

    algo_types = detect_problem_types(problem_desc, input_desc)
    if algo_types:
        routing_log.append({
            'axis': 'algo_keyword', 'types': algo_types,
            'reason': 'keyword match from description (no priority boost applied)',
        })

    selected = [(p, 1) for p in sorted(candidates.keys())]

    return {
        'selected_patterns': selected,
        'detected_structures': detected_structures,
        'detected_algo_types': algo_types,
        'routing_log': routing_log,
    }

_LOCAL_MODEL_CACHE = {}
_GEMINI_CLIENT_CACHE = {}

logger = logging.getLogger(__name__)

def _get_local_model(model_name: str, cache_dir: str):
    if model_name not in _LOCAL_MODEL_CACHE:
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch
        tokenizer = AutoTokenizer.from_pretrained(
            model_name, cache_dir=cache_dir, trust_remote_code=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_name, cache_dir=cache_dir, trust_remote_code=True,
            torch_dtype=torch.bfloat16, device_map="auto",
        )
        _LOCAL_MODEL_CACHE[model_name] = (model, tokenizer)
    return _LOCAL_MODEL_CACHE[model_name]

def _call_llm(prompt: str, config: Dict[str, Any]) -> Optional[str]:
    provider = config.get('provider', 'gemini')
    max_retries = config.get('max_retries', 3)
    temperature = config.get('temperature', 0.7)

    for attempt in range(max_retries):
        try:
            if provider == 'gemini':
                import google.genai as genai
                from google.genai import types
                api_key = config.get('api_key') or os.environ.get('GEMINI_API_KEY')
                if not api_key:
                    logger.warning("No GEMINI_API_KEY found, skipping LLM refinement")
                    return None
                if api_key not in _GEMINI_CLIENT_CACHE:
                    _GEMINI_CLIENT_CACHE[api_key] = genai.Client(api_key=api_key)
                client = _GEMINI_CLIENT_CACHE[api_key]
                gen_config = types.GenerateContentConfig(
                    temperature=temperature,
                )
                import concurrent.futures as _cf
                _gemini_model = config.get('gemini_model')
                with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                    _fut = _ex.submit(
                        client.models.generate_content,
                        model=_gemini_model,
                        contents=prompt,
                        config=gen_config,
                    )
                    response = _fut.result(timeout=120)
                return response.text if hasattr(response, 'text') else str(response)

            elif provider == 'openai':
                import openai
                import re as _re
                api_key = config.get('api_key') or os.environ.get('OPENAI_API_KEY')
                if not api_key:
                    logger.warning("No OPENAI_API_KEY found, skipping LLM refinement")
                    return None
                model_name_oai = config.get('openai_model', 'o4-mini')
                is_reasoning_model = bool(_re.match(r'^o\d', model_name_oai))
                create_kwargs = dict(
                    model=model_name_oai,
                    messages=[{"role": "user", "content": prompt}],
                )
                if not is_reasoning_model:
                    create_kwargs['temperature'] = temperature
                if hasattr(openai, 'OpenAI'):
                    client = openai.OpenAI(api_key=api_key)
                    result = client.chat.completions.create(**create_kwargs)
                    return result.choices[0].message.content
                else:
                    openai.api_key = api_key
                    result = openai.ChatCompletion.create(**create_kwargs)
                    return result['choices'][0]['message']['content']

            elif provider == 'local':
                import torch
                model_name = config.get('local_model', 'Qwen/Qwen2.5-Coder-7B-Instruct')
                cache_dir = config.get('cache_dir', None)
                model, tokenizer = _get_local_model(model_name, cache_dir)

                is_qwen3 = "Qwen3" in model_name

                messages = [{"role": "user", "content": prompt}]
                if hasattr(tokenizer, 'apply_chat_template'):
                    if is_qwen3:
                        try:
                            input_text = tokenizer.apply_chat_template(
                                messages, tokenize=False, add_generation_prompt=True,
                                enable_thinking=False,
                            )
                        except TypeError:
                            input_text = tokenizer.apply_chat_template(
                                messages, tokenize=False, add_generation_prompt=True,
                            )
                    else:
                        input_text = tokenizer.apply_chat_template(
                            messages, tokenize=False, add_generation_prompt=True,
                        )
                else:
                    input_text = prompt
                inputs = tokenizer(input_text, return_tensors="pt").to(model.device)
                with torch.no_grad():
                    outputs = model.generate(
                        **inputs,
                        max_new_tokens=config.get('max_new_tokens', 4096),
                        temperature=temperature,
                        top_p=config.get('top_p', 0.95),
                        do_sample=True,
                    )
                response_ids = outputs[0][inputs['input_ids'].shape[1]:]
                return tokenizer.decode(response_ids, skip_special_tokens=True)

            else:
                logger.warning(f"Unknown LLM provider: {provider}")
                return None

        except Exception as e:
            if attempt == max_retries - 1:
                logger.warning(f"LLM call failed after {max_retries} retries: {e}")
                return None
            sleep_time = min(2 ** attempt + random.random(), 60)
            logger.info(f"LLM retry {attempt+1}/{max_retries}: {e}. Sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)

    return None

def _build_m1_boundary_block(
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
    m1_values: Dict[str, Any],
    mode: str = 'normal',
) -> str:
    if mode == 'scenario_only':
        return (
            "## M1 Boundary Values (scalar variables only)\n"
            "```json\n"
            '{\n  "(suppressed for ablation — scenario hints only; '
            'pick sizes from input constraints in the description above)"\n}\n'
            "```\n"
        )

    m1_values_json = json.dumps(m1_values, indent=2, ensure_ascii=False)
    single_block = (
        "## M1 Boundary Values (scalar variables only)\n"
        "```json\n"
        f"{m1_values_json}\n"
        "```\n"
        "Use these exact sizes for scalar variables (N, M, etc.) in every "
        "generator. Do NOT change them."
    )

    if not any(c.get('type') == 'sum_tc' for c in constraints):
        return single_block

    tcv = (structure or {}).get('test_cases_var')
    if not tcv:
        return single_block

    try:
        from utils.cpsat_solver import solve as _cpsat_solve_top
        opt_b = _cpsat_solve_top(
            constraints, {}, tcv=tcv,
            value_strategy='max', time_limit_sec=5.0, lex_priority='tcv',
        )
    except Exception:
        return single_block
    if not opt_b:
        return single_block

    opt_a_scalars = {k: v for k, v in m1_values.items() if isinstance(v, (int, float))}
    opt_b_scalars = {k: opt_b.get(k) for k in opt_a_scalars if k in opt_b}

    if not opt_b_scalars or opt_a_scalars == opt_b_scalars:
        return single_block

    a_json = json.dumps(opt_a_scalars, indent=2, ensure_ascii=False)
    b_json = json.dumps(opt_b_scalars, indent=2, ensure_ascii=False)
    sum_tc_strs = [
        f"Σ{c['var']} ≤ {c['hi']:,}"
        for c in constraints if c.get('type') == 'sum_tc' and isinstance(c.get('hi'), int)
    ]
    sum_tc_constraint = ', '.join(sum_tc_strs) if sum_tc_strs else 'sum_tc constraint'

    return (
        "## M1 Boundary Values — Choose ONE strategy per generator\n\n"
        "### Option A (data-max, current default)\n"
        "```json\n"
        f"{a_json}\n"
        "```\n\n"
        "### Option B (tc-max, alternative)\n"
        "```json\n"
        f"{b_json}\n"
        "```\n\n"
        f"Both satisfy: {sum_tc_constraint}. Choose based on algorithm:\n"
        "- Default to Option A (data-max) unless:\n"
        "  - Algorithm is clearly linear in t with light per-TC work\n"
        "  - Examples: simple I/O loop, fixed-size per-TC operations\n"
        "  Then pick Option B (tc-max)."
    )

def _build_refinement_prompt(
    problem_desc: str,
    input_desc: str,
    structure: Dict[str, Any],
    routing: Dict[str, Any],
    rule_based_testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    tc_index: int = 1,
    tc_total: int = 1,
    previous_strategies: Optional[List[str]] = None,
    timelimit: Optional[float] = None,
    refinement_prompt_name: Optional[str] = None,
    tier: str = 'slow',
    pure_llm: bool = False,
    mode: str = 'both',
    problem_name: Optional[str] = None,
) -> str:
    struct_parts = []
    if structure.get('arrays'):
        arr_names = [a['name'] for a in structure['arrays']]
        struct_parts.append(f"Arrays: {arr_names}")
    if structure.get('edges'):
        e = structure['edges']
        struct_parts.append(f"Graph/Tree edges: weighted={e.get('weighted', False)}, is_tree={e.get('is_tree', False)}")
    if structure.get('strings'):
        str_names = [s['name'] for s in structure['strings']]
        struct_parts.append(f"Strings: {str_names}")
    if structure.get('matrix'):
        struct_parts.append(f"Matrix: {structure['matrix']}")
    if structure.get('pairs'):
        struct_parts.append(f"Pairs: {structure['pairs']}")
    structure_summary = '\n'.join(struct_parts) if struct_parts else 'No special structures detected'

    tc_summary = {}
    for k, v in rule_based_testcase.items():
        if k.startswith('_'):
            continue
        if isinstance(v, list) and len(v) > 20:
            tc_summary[k] = f"[{v[0]}, {v[1]}, ..., {v[-1]}] (length={len(v)})"
        else:
            tc_summary[k] = v
    tc_json = json.dumps(tc_summary, indent=2, ensure_ascii=False, default=str)

    constraint_lines = []
    for c in constraints[:30]:
        if c['type'] == 'range':
            constraint_lines.append(f"  {c['var']}: {c['lo']} <= {c['var']} <= {c['hi']}")
        elif c['type'] == 'relation':
            constraint_lines.append(f"  {c.get('expr', str(c))}")
    constraints_summary = '\n'.join(constraint_lines) if constraint_lines else 'No constraints parsed'

    pattern_names = [p[0] for p in routing.get('selected_patterns', [])]

    if previous_strategies:
        prev_str = "Previously used strategies (DO NOT repeat these):\n" + \
                   "\n".join(f"  - TC {i+1}: {s}" for i, s in enumerate(previous_strategies))
    else:
        prev_str = "This is the first test case — choose the strongest adversarial strategy."

    if Slow_testcase_refinement_prompt is None:
        raise ImportError("slow_testcase_refinement_prompt module is not available")

    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v10' and (Slow_testcase_refinement_prompt_v10 is None or TIER_BLOCKS_V10 is None):
        logger.warning("v10 prompt requested but import failed; falling back to v9")
    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v10' and Slow_testcase_refinement_prompt_v10 is not None and TIER_BLOCKS_V10 is not None:
        m1_values = rule_based_testcase.get('values', {})
        m1_values_json = json.dumps(m1_values, indent=2, ensure_ascii=False)

        tl = timelimit or 2.0
        tb = TIER_BLOCKS_V10.get(tier, TIER_BLOCKS_V10['slow'])

        tier_time_guidance = tb['tier_time_guidance'].format(
            timelimit_fast_target=tl / 3,
            timelimit_medium_lo=tl / 3,
            timelimit_medium_hi=tl * 2 / 3,
            timelimit_slow_target=tl * 2 / 3,
        )
        tier_requirement = tb['tier_requirement']

        catalog = _get_adversary_catalog()
        _force_m2 = mode in ('All_USE', 'smt_only', 'scenario_only', 'smt_only_minimal', 'scenario_only_minimal')
        scenarios = []
        if catalog is None or _detect_adversary_scenarios is None:
            if not _force_m2:
                logger.warning("v10 catalog unavailable — skipping M2 LLM call for this problem")
                return None
            logger.info("v10 catalog unavailable — forced M2 with blank routing")
        else:
            _knn_args = globals().get("_KNN_ARGS", {}) or {}
            scenarios = _detect_adversary_scenarios(
                catalog, problem_desc, input_desc, structure=structure,
                use_knn_fallback=_knn_args.get("use_knn_fallback", False),
                knn_threshold=_knn_args.get("knn_threshold", 0.45),
                knn_top_k=_knn_args.get("knn_top_k", 2),
                knn_model_name=_knn_args.get("knn_model_name", "all-mpnet-base-v2"),
            )
        if pure_llm:
            routing_section = (
                "### Detected Algorithm Scenario(s)\n"
                "(suppressed in pure_llm mode)"
            )
        elif not scenarios:
            if not _force_m2:
                return None
            routing_section = (
                "### Detected Algorithm Scenario(s)\n"
                "(no catalog scenario matched for this problem)"
            )
        else:
            routing_section = _build_adversary_routing_section(
                catalog, scenarios, num_testcases=tc_total
            )

        if mode == 'smt_only':
            routing_section = (
                "### Detected Algorithm Scenario(s)\n"
                "(suppressed for ablation — SMT max values only)"
            )
        elif mode == 'scenario_only':
            m1_values_json = (
                '{\n  "(suppressed for ablation — scenario hints only; '
                'pick sizes from input constraints in the description above)"\n}'
            )

        return Slow_testcase_refinement_prompt_v10.format(
            tier=tier,
            num_testcases=tc_total,
            tier_instruction=tb['tier_instruction'],
            tier_time_guidance=tier_time_guidance,
            tier_requirement=tier_requirement,
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=tl,
            constraints_summary=constraints_summary,
            m1_values_json=m1_values_json,
            routing_section=routing_section,
        )

    if refinement_prompt_name == 'slow_testcase_refinement_prompt' and (Slow_testcase_refinement_prompt_v9 is None or TIER_BLOCKS_V9 is None):
        logger.warning("v9 prompt requested but import failed; falling back to v8")
    if refinement_prompt_name == 'slow_testcase_refinement_prompt' and Slow_testcase_refinement_prompt_v9 is not None and TIER_BLOCKS_V9 is not None:
        m1_values = rule_based_testcase.get('values', {})
        m1_boundary_block = _build_m1_boundary_block(constraints, structure, m1_values, mode='normal')

        tl = timelimit or 2.0
        tb = TIER_BLOCKS_V9.get(tier, TIER_BLOCKS_V9['slow'])

        tier_time_guidance = tb['tier_time_guidance'].format(
            timelimit_fast_target=tl / 3,
            timelimit_medium_lo=tl / 3,
            timelimit_medium_hi=tl * 2 / 3,
            timelimit_slow_target=tl * 2 / 3,
        )
        tier_requirement = tb['tier_requirement']

        catalog = _get_adversary_catalog()
        _force_m2 = mode in ('All_USE', 'smt_only', 'scenario_only', 'smt_only_minimal', 'scenario_only_minimal')

        scenarios = []
        _use_kw_anchor = globals().get("_KW_ANCHOR_KNN", True)
        if _use_kw_anchor:
            try:
                from utils.kw_anchor_knn import KWAnchorKNN
                _knn = KWAnchorKNN.instance()
                _name = problem_name or ""
                scenarios = _knn.detect_scenarios(
                    problem_name=_name, problem_desc=problem_desc,
                )
            except Exception as e:
                logger.warning(f"KWAnchorKNN failed ({e}); falling back to catalog detect")
                _use_kw_anchor = False

        if not _use_kw_anchor:
            if catalog is None or _detect_adversary_scenarios is None:
                if not _force_m2:
                    logger.warning("v9 catalog unavailable — skipping M2 LLM call for this problem")
                    return None
                logger.info("v9 catalog unavailable — forced M2 with blank routing")
            else:
                _knn_args = globals().get("_KNN_ARGS", {}) or {}
                scenarios = _detect_adversary_scenarios(
                    catalog, problem_desc, input_desc, structure=structure,
                    use_knn_fallback=_knn_args.get("use_knn_fallback", False),
                    knn_threshold=_knn_args.get("knn_threshold", 0.45),
                    knn_top_k=_knn_args.get("knn_top_k", 2),
                    knn_model_name=_knn_args.get("knn_model_name", "all-mpnet-base-v2"),
                )
        if pure_llm:
            routing_section = (
                "### Detected Algorithm Scenario(s)\n"
                "(suppressed in pure_llm mode)"
            )
        elif not scenarios:
            if not _force_m2:
                return None
            routing_section = (
                "### Detected Algorithm Scenario(s)\n"
                "(no catalog scenario matched for this problem)"
            )
        else:
            routing_section = _build_adversary_routing_section(
                catalog, scenarios, num_testcases=tc_total
            )

        if mode == 'smt_only' and Slow_testcase_refinement_prompt_v9_smt_only is not None:
            return Slow_testcase_refinement_prompt_v9_smt_only.format(
                tier=tier,
                num_testcases=tc_total,
                problem_desc=problem_desc,
                input_desc=input_desc,
                timelimit=tl,
                constraints_summary=constraints_summary,
                m1_boundary_block=m1_boundary_block,
            )
        if mode == 'scenario_only' and Slow_testcase_refinement_prompt_v9_scenario_only is not None:
            return Slow_testcase_refinement_prompt_v9_scenario_only.format(
                tier=tier,
                num_testcases=tc_total,
                tier_instruction=tb['tier_instruction'],
                tier_time_guidance=tier_time_guidance,
                tier_requirement=tier_requirement,
                problem_desc=problem_desc,
                input_desc=input_desc,
                timelimit=tl,
                routing_section=routing_section,
            )

        if mode == 'smt_only':
            routing_section = (
                "### Detected Algorithm Scenario(s)\n"
                "(suppressed for ablation — SMT max values only)"
            )
        elif mode == 'scenario_only':
            m1_boundary_block = _build_m1_boundary_block(constraints, structure, m1_values, mode='scenario_only')

        return Slow_testcase_refinement_prompt_v9.format(
            tier=tier,
            num_testcases=tc_total,
            tier_instruction=tb['tier_instruction'],
            tier_time_guidance=tier_time_guidance,
            tier_requirement=tier_requirement,
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=tl,
            constraints_summary=constraints_summary,
            m1_boundary_block=m1_boundary_block,
            routing_section=routing_section,
        )

    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v8' and (Slow_testcase_refinement_prompt_v8 is None or TIER_BLOCKS_V8 is None):
        logger.warning("v8 prompt requested but import failed; falling back to v6")
    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v8' and Slow_testcase_refinement_prompt_v8 is not None and TIER_BLOCKS_V8 is not None:
        if not previous_strategies:
            if tier == 'fast':
                prev_str = "This is the first test case — choose the simplest, most trivial pattern."
            elif tier == 'medium':
                prev_str = "This is the first test case — choose a moderately complex strategy."

        m1_values = rule_based_testcase.get('values', {})
        m1_values_json = json.dumps(m1_values, indent=2, ensure_ascii=False)

        algo_types = routing.get('detected_algo_types', [])
        complexity_map = {
            'sorting': 'N log N', 'binary_search': 'N log N',
            'tree': 'N log N', 'graph': 'N + M', 'dp': 'N^2 or N*M',
            'string': 'N*M', 'segment_tree': 'N log N',
            'two_pointer': 'N', 'greedy': 'N log N', 'math': 'N',
            'heap': 'N log N', 'number_theory': 'N sqrt(N)',
        }
        expected = 'unspecified' if pure_llm else 'N log N'
        for algo in algo_types:
            if algo in complexity_map:
                expected = complexity_map[algo]
                break

        tl = timelimit or 2.0
        tb = TIER_BLOCKS_V8.get(tier, TIER_BLOCKS_V8['slow'])

        tier_time_guidance = tb['tier_time_guidance'].format(
            timelimit_fast_target=tl / 3,
            timelimit_medium_lo=tl / 3,
            timelimit_medium_hi=tl * 2 / 3,
            timelimit_slow_target=tl * 2 / 3,
        )
        tier_requirement = tb['tier_requirement'].format(
            timelimit_slow_target=tl * 2 / 3,
        )

        if pure_llm:
            complexity_section = ""
        else:
            complexity_guidance = tb['complexity_guidance'].format(
                expected_complexity=expected,
            )
            complexity_section = (
                "\n## Expected Time Complexity\n"
                f"Typical correct solutions for this problem run in **O({expected})** time.\n"
                f"{complexity_guidance}\n"
            )

        routing_section = (
            f"- Detected structures: {routing.get('detected_structures', [])}\n"
            f"- Detected algorithm types: {algo_types}\n"
            f"- Suggested patterns: {pattern_names}"
        )

        return Slow_testcase_refinement_prompt_v8.format(
            tier=tier,
            num_testcases=tc_total,
            tier_instruction=tb['tier_instruction'],
            tier_time_guidance=tier_time_guidance,
            tier_requirement=tier_requirement,
            tier_examples_header=tb['tier_examples_header'],
            tier_examples=tb['tier_examples'],
            complexity_section=complexity_section,
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=tl,
            constraints_summary=constraints_summary,
            m1_values_json=m1_values_json,
            structure_summary=structure_summary,
            routing_section=routing_section,
        )

    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v7' and (Slow_testcase_refinement_prompt_v7 is None or TIER_BLOCKS_V7 is None):
        logger.warning("v7 prompt requested but import failed; falling back to v6")
    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v7' and Slow_testcase_refinement_prompt_v7 is not None and TIER_BLOCKS_V7 is not None:
        if not previous_strategies:
            if tier == 'fast':
                prev_str = "This is the first test case — just produce valid stdin from M1 boundary values."
            elif tier == 'medium':
                prev_str = "This is the first test case — choose a moderately complex strategy."

        m1_values = rule_based_testcase.get('values', {})
        m1_values_json = json.dumps(m1_values, indent=2, ensure_ascii=False)

        algo_types = routing.get('detected_algo_types', [])
        complexity_map = {
            'sorting': 'N log N', 'binary_search': 'N log N',
            'tree': 'N log N', 'graph': 'N + M', 'dp': 'N^2 or N*M',
            'string': 'N*M', 'segment_tree': 'N log N',
            'two_pointer': 'N', 'greedy': 'N log N', 'math': 'N',
            'heap': 'N log N', 'number_theory': 'N sqrt(N)',
        }
        expected = 'unspecified' if pure_llm else 'N log N'
        for algo in algo_types:
            if algo in complexity_map:
                expected = complexity_map[algo]
                break

        tl = timelimit or 2.0
        tb = TIER_BLOCKS_V7.get(tier, TIER_BLOCKS_V7['slow'])

        tier_time_guidance = tb['tier_time_guidance'].format(
            timelimit_fast_target=tl / 3,
            timelimit_medium_lo=tl / 3,
            timelimit_medium_hi=tl * 2 / 3,
            timelimit_slow_target=tl * 2 / 3,
        )
        tier_requirement = tb['tier_requirement'].format(
            timelimit_slow_target=tl * 2 / 3,
        )

        if tier == 'fast':
            routing_section = (
                f"- Detected structures: {routing.get('detected_structures', [])}"
            )
        else:
            routing_section = (
                f"- Detected structures: {routing.get('detected_structures', [])}\n"
                f"- Detected algorithm types: {algo_types}\n"
                f"- Suggested patterns: {pattern_names}"
            )

        tier_diversity_section = tb['tier_diversity_section']
        if tier_diversity_section:
            tier_diversity_section = tier_diversity_section.format(num_testcases=tc_total)

        return Slow_testcase_refinement_prompt_v7.format(
            tier=tier,
            num_testcases=tc_total,
            tier_instruction=tb['tier_instruction'],
            tier_task_framing=tb['tier_task_framing'],
            tier_time_guidance=tier_time_guidance,
            tier_requirement=tier_requirement,
            tier_examples_header=tb['tier_examples_header'],
            tier_examples=tb['tier_examples'],
            tier_diversity_section=tier_diversity_section,
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=tl,
            constraints_summary=constraints_summary,
            m1_values_json=m1_values_json,
            structure_summary=structure_summary,
            routing_section=routing_section,
        )

    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v6' and (Slow_testcase_refinement_prompt_v6 is None or TIER_BLOCKS_V6 is None):
        logger.warning("v6 prompt requested but import failed; falling back to v5/v4")
    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v6' and Slow_testcase_refinement_prompt_v6 is not None and TIER_BLOCKS_V6 is not None:
        if not previous_strategies:
            if tier == 'fast':
                prev_str = "This is the first test case — choose the simplest, most trivial pattern."
            elif tier == 'medium':
                prev_str = "This is the first test case — choose a moderately complex strategy."

        m1_values = rule_based_testcase.get('values', {})
        m1_values_json = json.dumps(m1_values, indent=2, ensure_ascii=False)

        algo_types = routing.get('detected_algo_types', [])
        complexity_map = {
            'sorting': 'N log N', 'binary_search': 'N log N',
            'tree': 'N log N', 'graph': 'N + M', 'dp': 'N^2 or N*M',
            'string': 'N*M', 'segment_tree': 'N log N',
            'two_pointer': 'N', 'greedy': 'N log N', 'math': 'N',
            'heap': 'N log N', 'number_theory': 'N sqrt(N)',
        }
        expected = 'unspecified' if pure_llm else 'N log N'
        for algo in algo_types:
            if algo in complexity_map:
                expected = complexity_map[algo]
                break

        tl = timelimit or 2.0
        tb = TIER_BLOCKS_V6.get(tier, TIER_BLOCKS_V6['slow'])

        tier_time_guidance = tb['tier_time_guidance'].format(
            timelimit_fast_target=tl / 3,
            timelimit_medium_lo=tl / 3,
            timelimit_medium_hi=tl * 2 / 3,
            timelimit_slow_target=tl * 2 / 3,
        )
        tier_requirement = tb['tier_requirement'].format(
            timelimit_slow_target=tl * 2 / 3,
        )

        routing_section = (
            f"- Detected structures: {routing.get('detected_structures', [])}\n"
            f"- Detected algorithm types: {algo_types}\n"
            f"- Suggested patterns: {pattern_names}"
        )

        return Slow_testcase_refinement_prompt_v6.format(
            tier=tier,
            num_testcases=tc_total,
            tier_instruction=tb['tier_instruction'],
            tier_time_guidance=tier_time_guidance,
            tier_requirement=tier_requirement,
            tier_examples_header=tb['tier_examples_header'],
            tier_examples=tb['tier_examples'],
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=tl,
            constraints_summary=constraints_summary,
            m1_values_json=m1_values_json,
            structure_summary=structure_summary,
            routing_section=routing_section,
        )

    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v5' and (Slow_testcase_refinement_prompt_v5 is None or TIER_BLOCKS is None):
        logger.warning("v5 prompt requested but import failed; falling back to v4/v3")
    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v5' and Slow_testcase_refinement_prompt_v5 is not None and TIER_BLOCKS is not None:
        if not previous_strategies:
            if tier == 'fast':
                prev_str = "This is the first test case — choose the simplest, most trivial pattern."
            elif tier == 'medium':
                prev_str = "This is the first test case — choose a moderately complex strategy."

        m1_values = rule_based_testcase.get('values', {})
        m1_values_json = json.dumps(m1_values, indent=2, ensure_ascii=False)

        algo_types = routing.get('detected_algo_types', [])
        complexity_map = {
            'sorting': 'N log N', 'binary_search': 'N log N',
            'tree': 'N log N', 'graph': 'N + M', 'dp': 'N^2 or N*M',
            'string': 'N*M', 'segment_tree': 'N log N',
            'two_pointer': 'N', 'greedy': 'N log N', 'math': 'N',
            'heap': 'N log N', 'number_theory': 'N sqrt(N)',
        }
        expected = 'unspecified' if pure_llm else 'N log N'
        for algo in algo_types:
            if algo in complexity_map:
                expected = complexity_map[algo]
                break

        tl = timelimit or 2.0
        tb = TIER_BLOCKS.get(tier, TIER_BLOCKS['slow'])

        tier_time_guidance = tb['tier_time_guidance'].format(
            timelimit_fast_target=tl / 3,
            timelimit_medium_lo=tl / 3,
            timelimit_medium_hi=tl * 2 / 3,
            timelimit_slow_target=tl * 2 / 3,
        )
        tier_requirement = tb['tier_requirement'].format(
            timelimit_slow_target=tl * 2 / 3,
        )

        return Slow_testcase_refinement_prompt_v5.format(
            tier=tier,
            tier_instruction=tb['tier_instruction'],
            tier_time_guidance=tier_time_guidance,
            tier_requirement=tier_requirement,
            tier_examples_header=tb['tier_examples_header'],
            tier_examples=tb['tier_examples'],
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=tl,
            expected_complexity=expected,
            constraints_summary=constraints_summary,
            m1_values_json=m1_values_json,
            structure_summary=structure_summary,
            detected_structures=routing.get('detected_structures', []),
            detected_algo_types=algo_types,
            pattern_names=pattern_names,
            tc_index=tc_index,
            tc_total=tc_total,
            previous_strategies=prev_str,
        )

    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v4' and Slow_testcase_refinement_prompt_v4 is not None:
        m1_values = rule_based_testcase.get('values', {})
        m1_values_json = json.dumps(m1_values, indent=2, ensure_ascii=False)

        algo_types = routing.get('detected_algo_types', [])
        complexity_map = {
            'sorting': 'N log N', 'binary_search': 'N log N',
            'tree': 'N log N', 'graph': 'N + M', 'dp': 'N^2 or N*M',
            'string': 'N*M', 'segment_tree': 'N log N',
            'two_pointer': 'N', 'greedy': 'N log N', 'math': 'N',
            'heap': 'N log N', 'number_theory': 'N sqrt(N)',
        }
        expected = 'unspecified' if pure_llm else 'N log N'
        for algo in algo_types:
            if algo in complexity_map:
                expected = complexity_map[algo]
                break

        tl = timelimit or 2.0
        return Slow_testcase_refinement_prompt_v4.format(
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=tl,
            timelimit_target=tl * 2 / 3,
            expected_complexity=expected,
            constraints_summary=constraints_summary,
            m1_values_json=m1_values_json,
            structure_summary=structure_summary,
            detected_structures=routing.get('detected_structures', []),
            detected_algo_types=algo_types,
            pattern_names=pattern_names,
            tc_index=tc_index,
            tc_total=tc_total,
            previous_strategies=prev_str,
        )

    if refinement_prompt_name == 'slow_testcase_refinement_prompt_v3' and Slow_testcase_refinement_prompt_v3 is not None:
        m1_values = rule_based_testcase.get('values', {})
        m1_values_json = json.dumps(m1_values, indent=2, ensure_ascii=False)

        algo_types = routing.get('detected_algo_types', [])
        complexity_map = {
            'sorting': 'N log N', 'binary_search': 'N log N',
            'tree': 'N log N', 'graph': 'N + M', 'dp': 'N^2 or N*M',
            'string': 'N*M', 'segment_tree': 'N log N',
            'two_pointer': 'N', 'greedy': 'N log N', 'math': 'N',
            'heap': 'N log N', 'number_theory': 'N sqrt(N)',
        }
        expected = 'unspecified' if pure_llm else 'N log N'
        for algo in algo_types:
            if algo in complexity_map:
                expected = complexity_map[algo]
                break

        tl = timelimit or 2.0
        return Slow_testcase_refinement_prompt_v3.format(
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=tl,
            timelimit_target=tl * 2 / 3,
            expected_complexity=expected,
            constraints_summary=constraints_summary,
            m1_values_json=m1_values_json,
            structure_summary=structure_summary,
            detected_structures=routing.get('detected_structures', []),
            detected_algo_types=algo_types,
            pattern_names=pattern_names,
            tc_index=tc_index,
            tc_total=tc_total,
            previous_strategies=prev_str,
        )

    use_v1_only = (refinement_prompt_name == 'slow_testcase_refinement_prompt_v1')

    if not use_v1_only and timelimit and Slow_testcase_refinement_prompt_v2 is not None:
        algo_types = routing.get('detected_algo_types', [])
        complexity_map = {
            'sorting': 'N log N', 'binary_search': 'N log N',
            'tree': 'N log N', 'graph': 'N + M', 'dp': 'N^2 or N*M',
            'string': 'N*M', 'segment_tree': 'N log N',
            'two_pointer': 'N', 'greedy': 'N log N', 'math': 'N',
            'heap': 'N log N', 'number_theory': 'N sqrt(N)',
        }
        expected = 'unspecified' if pure_llm else 'N log N'
        for algo in algo_types:
            if algo in complexity_map:
                expected = complexity_map[algo]
                break

        return Slow_testcase_refinement_prompt_v2.format(
            problem_desc=problem_desc,
            input_desc=input_desc,
            timelimit=timelimit,
            timelimit_target=timelimit * 2 / 3,
            timelimit_ratio=66.7,
            expected_complexity=expected,
            structure_summary=structure_summary,
            detected_structures=routing.get('detected_structures', []),
            detected_algo_types=algo_types,
            pattern_names=pattern_names,
            tc_json=tc_json,
            constraints_summary=constraints_summary,
            tc_index=tc_index,
            tc_total=tc_total,
            previous_strategies=prev_str,
        )

    return Slow_testcase_refinement_prompt_v1.format(
        problem_desc=problem_desc,
        input_desc=input_desc,
        structure_summary=structure_summary,
        detected_structures=routing.get('detected_structures', []),
        detected_algo_types=routing.get('detected_algo_types', []),
        pattern_names=pattern_names,
        tc_json=tc_json,
        constraints_summary=constraints_summary,
        tc_index=tc_index,
        tc_total=tc_total,
        previous_strategies=prev_str,
    )

def _extract_json_from_response(text: str) -> Optional[str]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "<think>" in text and "</think>" not in text:
        text = text.split("<think>", 1)[0]
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    brace = text.find("{")
    if brace == -1:
        return None
    try:
        _, end = json.JSONDecoder().raw_decode(text[brace:])
        return text[brace: brace + end].strip()
    except json.JSONDecodeError:
        return None

MAX_EXPAND_LENGTH = 500_000

def _expand_compact_array(expr) -> Optional[list]:
    if isinstance(expr, list):
        result = []
        for item in expr:
            if isinstance(item, dict):
                sub = _expand_compact_array(item)
                if sub is not None:
                    result.extend(sub)
                    if len(result) > MAX_EXPAND_LENGTH:
                        return result[:MAX_EXPAND_LENGTH]
            else:
                result.append(item)
        return result

    if not isinstance(expr, dict):
        return None

    if 'rep' in expr and 'count' in expr:
        count = min(int(expr['count']), MAX_EXPAND_LENGTH)
        return [expr['rep']] * count

    if 'range' in expr:
        bounds = expr['range']
        if isinstance(bounds, list) and len(bounds) == 2:
            start, end = int(bounds[0]), int(bounds[1])
            step = 1 if end >= start else -1
            length = abs(end - start) + 1
            if length > MAX_EXPAND_LENGTH:
                length = MAX_EXPAND_LENGTH
                end = start + (length - 1) * step
            return list(range(start, end + step, step))
        return None

    if 'range_desc' in expr:
        bounds = expr['range_desc']
        if isinstance(bounds, list) and len(bounds) == 2:
            start, end = int(bounds[0]), int(bounds[1])
            length = abs(start - end) + 1
            if length > MAX_EXPAND_LENGTH:
                length = MAX_EXPAND_LENGTH
            return list(range(start, start - length, -1))
        return None

    if 'cycle' in expr and 'count' in expr:
        pattern = expr['cycle']
        if isinstance(pattern, list) and pattern:
            count = min(int(expr['count']), MAX_EXPAND_LENGTH // len(pattern) + 1)
            result = (pattern * count)[:int(expr['count']) * len(pattern)]
            return result[:MAX_EXPAND_LENGTH]
        return None

    if 'concat' in expr:
        parts = expr['concat']
        if isinstance(parts, list):
            result = []
            for part in parts:
                expanded = _expand_compact_array(part)
                if expanded is not None:
                    result.extend(expanded)
                elif isinstance(part, (int, float)):
                    result.append(part)
                if len(result) > MAX_EXPAND_LENGTH:
                    return result[:MAX_EXPAND_LENGTH]
            return result
        return None

    return None

def _expand_compact_string(expr) -> Optional[str]:
    if isinstance(expr, str):
        return expr

    if not isinstance(expr, dict):
        return None

    if 'rep' in expr and 'count' in expr:
        s = str(expr['rep'])
        count = min(int(expr['count']), MAX_EXPAND_LENGTH)
        return s * count

    if 'cycle' in expr and 'count' in expr:
        pattern = expr['cycle']
        if isinstance(pattern, list) and pattern:
            count = int(expr['count'])
            joined = ''.join(str(e) for e in pattern)
            return (joined * count)[:MAX_EXPAND_LENGTH]
        return None

    if 'concat' in expr:
        parts = expr['concat']
        if isinstance(parts, list):
            result = []
            for part in parts:
                expanded = _expand_compact_string(part)
                if expanded is not None:
                    result.append(expanded)
                elif isinstance(part, str):
                    result.append(part)
            return ''.join(result)[:MAX_EXPAND_LENGTH]
        return None

    return None

def _expand_compact_edges(expr) -> Optional[list]:
    if isinstance(expr, list):
        return [tuple(e) for e in expr if isinstance(e, list) and len(e) >= 2]

    if not isinstance(expr, dict):
        return None

    n = 0
    if 'chain' in expr or 'bamboo' in expr:
        n = int(expr.get('chain', expr.get('bamboo', 0)))
        return [(i, i + 1) for i in range(1, min(n, MAX_EXPAND_LENGTH))]

    if 'star' in expr:
        n = int(expr['star'])
        return [(1, i) for i in range(2, min(n + 1, MAX_EXPAND_LENGTH))]

    return None

def _parse_llm_testcase(
    response: str,
    structure: Dict[str, Any],
    constraints: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    json_str = _extract_json_from_response(response)
    if not json_str:
        return None

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    testcase = {}

    if 'values' in data and isinstance(data['values'], dict):
        testcase['values'] = {}
        for k, v in data['values'].items():
            if isinstance(v, (int, float)):
                testcase['values'][k] = int(v)

    if 'arrays' in data and isinstance(data['arrays'], dict):
        for arr_name, arr_val in data['arrays'].items():
            expanded = _expand_compact_array(arr_val)
            if expanded is not None:
                testcase[f'array_{arr_name}'] = [int(x) if isinstance(x, (int, float)) else x for x in expanded]
            elif isinstance(arr_val, list):
                testcase[f'array_{arr_name}'] = [int(x) if isinstance(x, (int, float)) else x for x in arr_val]

    str_len_max = {}
    for c in constraints:
        if c.get('type') == 'string_length' and 'var' in c and isinstance(c.get('hi'), (int, float)):
            str_len_max[c['var']] = int(c['hi'])

    if 'strings' in data and isinstance(data['strings'], dict):
        for str_name, str_val in data['strings'].items():
            max_len = str_len_max.get(str_name, MAX_EXPAND_LENGTH)
            expanded = _expand_compact_string(str_val)
            if expanded is not None:
                testcase[f'string_{str_name}'] = expanded[:max_len]
            elif isinstance(str_val, str):
                testcase[f'string_{str_name}'] = str_val[:max_len]

    if 'edges' in data:
        expanded = _expand_compact_edges(data['edges'])
        if expanded is not None:
            testcase['edges'] = expanded

    if 'edge_weights' in data:
        expanded = _expand_compact_array(data['edge_weights'])
        if expanded is not None:
            testcase['edge_weights'] = [int(w) if isinstance(w, (int, float)) else w for w in expanded]
        elif isinstance(data['edge_weights'], list):
            testcase['edge_weights'] = [int(w) if isinstance(w, (int, float)) else w for w in data['edge_weights']]

    if 'matrix' in data and isinstance(data['matrix'], list):
        testcase['matrix'] = data['matrix']

    if 'pairs' in data and isinstance(data['pairs'], dict):
        for key, val in data['pairs'].items():
            if isinstance(val, list):
                testcase[f'pairs_{key}'] = [tuple(p) for p in val if isinstance(p, list) and len(p) >= 2]

    constraint_ranges = {}
    for c in constraints:
        if c['type'] == 'range' and not c.get('is_array'):
            constraint_ranges[c['var']] = (c['lo'], c['hi'])

    if 'values' in testcase:
        for var, val in testcase['values'].items():
            if var in constraint_ranges and isinstance(val, (int, float)):
                lo, hi = constraint_ranges[var]
                testcase['values'][var] = max(lo, min(hi, val))

    return testcase if testcase else None

def _merge_llm_result(base: Dict[str, Any], llm: Dict[str, Any]) -> None:
    if 'values' in llm:
        base.setdefault('values', {}).update(llm['values'])
    for key in list(llm.keys()):
        if key.startswith(('array_', 'string_', 'pairs_')) or key in ('edges', 'edge_weights', 'matrix'):
            base[key] = llm[key]

def generate_algorithmic_slow(
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
    problem_desc: str,
    input_desc: str,
    boundary_testcase: Optional[Dict[str, Any]] = None,
    z3_timeout: int = 30,
    graph_structure: str = 'chain',
    llm_config: Optional[Dict[str, Any]] = None,
    tc_index: int = 1,
    tc_total: int = 1,
    previous_strategies: Optional[List[str]] = None,
    timelimit: Optional[float] = None,
    refinement_prompt_name: Optional[str] = None,
    tier: str = 'slow',
    no_wcm: bool = False,
    gen_timeout: int = 60,
    pure_llm: bool = False,
) -> Optional[Dict[str, Any]]:
    if boundary_testcase is None:
        boundary_testcase = generate_boundary_slow(
            constraints, structure, z3_timeout=z3_timeout,
            array_fill='max', graph_structure=graph_structure,
        )
    if boundary_testcase is None:
        if not constraints:
            logger.info("Module 2: no constraints parsed; using empty base testcase for LLM refinement.")
            boundary_testcase = {"values": {}}
        else:
            return None

    routing = select_worst_case_mutations(structure, problem_desc, input_desc)
    if not routing['detected_structures'] or not routing['detected_algo_types']:
        logger.info(
            "Module 2: no strong routing signals "
            f"(structures={routing['detected_structures']}, algos={routing['detected_algo_types']}); "
            "falling back to LLM-only refinement."
        )

    testcase = json.loads(json.dumps(boundary_testcase))
    testcase['_method'] = 'algorithmic_slow'
    testcase['_routing'] = {
        'detected_structures': routing['detected_structures'],
        'detected_algo_types': routing['detected_algo_types'],
        'selected_patterns': routing['selected_patterns'],
        'routing_log': routing['routing_log'],
    }

    rule_applied: List[str] = []
    if no_wcm or pure_llm:
        _label = 'pure_llm' if pure_llm else 'no_wcm'
        logger.info(f"Module 2: {_label}=True — skipping Step A (rule-based pattern injection)")
        routing = {
            'selected_patterns': [],
            'detected_structures': [],
            'detected_algo_types': [],
            'routing_log': [],
        }
        testcase['_routing'] = routing
        testcase['_applied_patterns'] = [_label]
    else:
        selected = routing.get('selected_patterns', [])
        if selected:
            top_name, _ = selected[0]
            if top_name in PATTERN_APPLICATORS:
                rule_applied.extend(PATTERN_APPLICATORS[top_name](testcase, constraints, structure))
            if len(selected) > 1:
                second_name, _ = selected[1]
                if second_name in PATTERN_APPLICATORS and second_name != top_name:
                    rule_applied.extend(PATTERN_APPLICATORS[second_name](testcase, constraints, structure))
        if not rule_applied:
            rule_applied = _apply_generic_worst_case_mutation(testcase, constraints, structure)

        if rule_applied:
            testcase['_applied_patterns'] = rule_applied

    if not llm_config or not llm_config.get('provider'):
        return None

    if pure_llm:
        _prompt_structure = {}
        _prompt_constraints = []
    else:
        _prompt_structure = structure
        _prompt_constraints = constraints

    prompt = _build_refinement_prompt(
        problem_desc, input_desc, _prompt_structure,
        routing, testcase, _prompt_constraints,
        tc_index=tc_index,
        tc_total=tc_total,
        previous_strategies=previous_strategies,
        timelimit=timelimit,
        refinement_prompt_name=refinement_prompt_name,
        tier=tier,
        pure_llm=pure_llm,
    )

    if prompt is None:
        testcase['_applied_patterns'] = ['m1_only_no_catalog_match']
        testcase['_skipped_m2'] = True
        return testcase

    is_generator_mode = (refinement_prompt_name in ('slow_testcase_refinement_prompt_v4', 'slow_testcase_refinement_prompt_v5', 'slow_testcase_refinement_prompt_v6', 'slow_testcase_refinement_prompt_v7')
                         and extract_python_from_response is not None
                         and validate_generator is not None)

    parse_retries = llm_config.get('parse_retries', 2)
    last_response = None
    last_error = None

    for attempt in range(parse_retries):
        try:
            if is_generator_mode and attempt > 0 and last_error:
                retry_prompt = (
                    prompt + "\n\n## Previous Attempt Failed\n"
                    f"Your previous code produced an error:\n```\n{last_error}\n```\n"
                    "Please fix the code and try again.\n\n```python\n{your code here}\n```"
                )
                response = _call_llm(retry_prompt, llm_config)
            else:
                response = _call_llm(prompt, llm_config)
            last_response = response
            if not response:
                logger.warning(f"LLM returned no response (attempt {attempt+1}/{parse_retries})")
                continue

            if is_generator_mode:
                code = extract_python_from_response(response)
                if code:
                    ok, msg, _stdin = validate_generator(code, timeout=gen_timeout)
                    if ok:
                        testcase['_applied_patterns'] = ['llm_generation']
                        testcase['_generator_code'] = code
                        testcase['_llm_response'] = response[:2000] if len(response) > 2000 else response
                        testcase['_llm_prompt'] = prompt
                        last_response = None
                        last_error = None
                        break
                    else:
                        last_error = msg
                        logger.warning(
                            f"generator validation failed (attempt {attempt+1}/{parse_retries}): {msg[:200]}"
                        )
                        continue
                else:
                    last_error = "No generate() function found in LLM response"
                    logger.warning(
                        f"generator code extraction failed (attempt {attempt+1}/{parse_retries})"
                    )
                    continue

            llm_testcase = _parse_llm_testcase(response, structure, constraints)
            if llm_testcase:
                _merge_llm_result(testcase, llm_testcase)
                testcase['_applied_patterns'] = ['llm_generation']
                testcase['_llm_response'] = response[:2000] if len(response) > 2000 else response
                testcase['_llm_prompt'] = prompt
                last_response = None
                break

            preview = response[:500].replace('\n', '\\n')
            logger.warning(f"LLM parse failed (attempt {attempt+1}/{parse_retries}). Preview: {preview}")

        except Exception as e:
            last_error = str(e)
            logger.warning(f"LLM call failed (attempt {attempt+1}/{parse_retries}): {e}")

    if is_generator_mode and last_response is not None:
        testcase['_applied_patterns'] = ['v4_generator_failed']
        testcase['_llm_error_type'] = ['generator_validation_failed']
        testcase['_llm_error_msg'] = last_error or 'unknown'
        testcase['_llm_raw_response'] = last_response
    elif last_response is not None:
        truncated = not last_response.rstrip().endswith('}')
        has_python_syntax = any(op in last_response for op in ['" * ', "' * ", '" + ', "' + "])
        error_type = 'llm_parse_failed'
        error_detail = []
        if truncated:
            error_detail.append('truncated')
        if has_python_syntax:
            error_detail.append('python_syntax_in_json')
        if not error_detail:
            error_detail.append('invalid_json')

        testcase['_applied_patterns'] = [error_type]
        testcase['_llm_error_type'] = error_detail
        testcase['_llm_raw_response'] = last_response
        testcase['_llm_prompt'] = prompt[:3000] if len(prompt) > 3000 else prompt
    elif last_error:
        testcase['_applied_patterns'] = ['llm_failed']
        testcase['_llm_error_type'] = ['exception']
        testcase['_llm_error_msg'] = last_error
    elif last_response is None and testcase.get('_applied_patterns') != ['llm_generation']:
        testcase['_applied_patterns'] = ['llm_no_response']
        testcase['_llm_error_type'] = ['no_response']

    testcase['_detected_structures'] = routing['detected_structures']
    testcase['_detected_algo_types'] = routing['detected_algo_types']
    return testcase

def _get_element_ranges(
    constraints: List[Dict[str, Any]],
) -> Dict[str, Tuple[int, int]]:
    element_ranges = {}
    for c in constraints:
        if c.get('is_array') and c['type'] == 'range':
            arr_name = c.get('array_name', c['var'].split('_')[0])
            element_ranges[arr_name] = (c['lo'], c['hi'])
    return element_ranges

def _make_reverse_sorted(n: int, lo: int, hi: int) -> List[int]:
    if n <= 0:
        return []
    if n == 1:
        return [hi]
    step = (hi - lo) / max(n - 1, 1)
    return [min(hi, max(lo, int(hi - i * step))) for i in range(n)]

def _make_sorted_asc(n: int, lo: int, hi: int) -> List[int]:
    if n <= 0:
        return []
    if n == 1:
        return [lo]
    step = (hi - lo) / max(n - 1, 1)
    return [min(hi, max(lo, int(lo + i * step))) for i in range(n)]

def _apply_reverse_sorted(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    element_ranges = _get_element_ranges(constraints)
    for arr in structure.get('arrays', []):
        arr_name = arr['name']
        key = f'array_{arr_name}'
        if key not in testcase:
            continue
        length = len(testcase[key])
        lo, hi = element_ranges.get(arr_name, (1, 1000000000))
        testcase[key] = _make_reverse_sorted(length, lo, hi)
        patterns_applied.append(f'array_{arr_name}: reverse-sorted [{lo}..{hi}]')
    return patterns_applied

def _apply_all_equal(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    element_ranges = _get_element_ranges(constraints)
    for arr in structure.get('arrays', []):
        arr_name = arr['name']
        key = f'array_{arr_name}'
        if key not in testcase:
            continue
        length = len(testcase[key])
        lo, hi = element_ranges.get(arr_name, (1, 1000000000))
        testcase[key] = [hi] * length
        patterns_applied.append(f'array_{arr_name}: all-equal ({hi})')
    return patterns_applied

def _apply_sorted_asc(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    element_ranges = _get_element_ranges(constraints)
    for arr in structure.get('arrays', []):
        arr_name = arr['name']
        key = f'array_{arr_name}'
        if key not in testcase:
            continue
        length = len(testcase[key])
        lo, hi = element_ranges.get(arr_name, (1, 1000000000))
        testcase[key] = _make_sorted_asc(length, lo, hi)
        patterns_applied.append(f'array_{arr_name}: sorted-asc [{lo}..{hi}]')
    return patterns_applied

def _apply_alternating(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    element_ranges = _get_element_ranges(constraints)
    for arr in structure.get('arrays', []):
        arr_name = arr['name']
        key = f'array_{arr_name}'
        if key not in testcase:
            continue
        length = len(testcase[key])
        lo, hi = element_ranges.get(arr_name, (1, 1000000000))
        testcase[key] = [hi if i % 2 == 0 else lo for i in range(length)]
        patterns_applied.append(f'array_{arr_name}: alternating [{lo},{hi}]')
    return patterns_applied

def _apply_bamboo_tree(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    if 'edges' not in testcase:
        return []
    edge_info = structure.get('edges', {})
    if not edge_info.get('is_tree'):
        return []
    n_val = min(testcase.get('values', {}).get('n', 2), 500000)
    edges = [(i, i + 1) for i in range(1, n_val)]
    testcase['edges'] = edges
    if 'edge_weights' in testcase:
        testcase['edge_weights'] = [random.randint(0, 1) for _ in edges]
    return [f'tree: bamboo/chain (depth={n_val-1})']

def _apply_star_tree(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    if 'edges' not in testcase:
        return []
    edge_info = structure.get('edges', {})
    if not edge_info.get('is_tree'):
        return []
    n_val = min(testcase.get('values', {}).get('n', 2), 500000)
    edges = [(1, i) for i in range(2, n_val + 1)]
    testcase['edges'] = edges
    if 'edge_weights' in testcase:
        testcase['edge_weights'] = [random.randint(0, 1) for _ in edges]
    return [f'tree: star (root=1, leaves={n_val-1})']

def _apply_caterpillar_tree(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    if 'edges' not in testcase:
        return []
    edge_info = structure.get('edges', {})
    if not edge_info.get('is_tree'):
        return []
    n_val = min(testcase.get('values', {}).get('n', 2), 500000)
    if n_val < 3:
        return _apply_bamboo_tree(testcase, constraints, structure)
    spine_len = max(n_val // 2, 2)
    edges = [(i, i + 1) for i in range(1, spine_len)]
    leaf_node = spine_len + 1
    for i in range(leaf_node, n_val + 1):
        parent = ((i - spine_len - 1) % spine_len) + 1
        edges.append((parent, i))
    testcase['edges'] = edges
    if 'edge_weights' in testcase:
        testcase['edge_weights'] = [random.randint(0, 1) for _ in edges]
    return [f'tree: caterpillar (spine={spine_len}, leaves={n_val-spine_len})']

def _apply_dense_clique_chain(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    if 'edges' not in testcase:
        return []
    edge_info = structure.get('edges', {})
    if edge_info.get('is_tree'):
        return []
    n_val = max(testcase.get('values', {}).get('n', 2), 2)
    m_val = len(testcase.get('edges', []))
    clique_size = min(int(math.isqrt(m_val)), n_val)
    edges = []
    for i in range(1, clique_size + 1):
        for j in range(i + 1, clique_size + 1):
            if len(edges) < m_val:
                edges.append((i, j))
    node = clique_size + 1
    while len(edges) < m_val and node <= n_val:
        edges.append((node - 1, node))
        node += 1
    while len(edges) < m_val:
        edges.append((random.randint(1, n_val), random.randint(1, n_val)))
    testcase['edges'] = edges[:m_val]
    return [f'graph: dense-clique({clique_size})+chain']

def _apply_adversarial_bfs(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    if 'edges' not in testcase:
        return []
    edge_info = structure.get('edges', {})
    if edge_info.get('is_tree'):
        return []
    n_val = max(testcase.get('values', {}).get('n', 2), 2)
    m_val = len(testcase.get('edges', []))
    edges = []
    for i in range(1, min(n_val, m_val + 1)):
        edges.append((i, i + 1))
    remaining = m_val - len(edges)
    for k in range(remaining):
        src = min(n_val, n_val - k % max(n_val - 1, 1))
        dst = (k % max(n_val - 1, 1)) + 1
        edges.append((src, dst))
    testcase['edges'] = edges[:m_val]
    return [f'graph: adversarial-bfs (n={n_val}, m={m_val})']

def _apply_periodic_string(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    for s in structure.get('strings', []):
        key = f'string_{s["name"]}'
        if key not in testcase:
            continue
        length = len(testcase[key])
        if length > 1:
            testcase[key] = 'a' * (length - 1) + 'b'
            patterns_applied.append(f'string_{s["name"]}: periodic "a"*{length-1}+"b"')
        else:
            patterns_applied.append(f'string_{s["name"]}: single char')
    return patterns_applied

def _apply_anti_hash_string(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    for s in structure.get('strings', []):
        key = f'string_{s["name"]}'
        if key not in testcase:
            continue
        length = len(testcase[key])
        if length > 1:
            testcase[key] = ''.join('a' if i % 2 == 0 else 'b' for i in range(length))
            patterns_applied.append(f'string_{s["name"]}: anti-hash alternating ab*{length//2}')
        else:
            patterns_applied.append(f'string_{s["name"]}: single char')
    return patterns_applied

def _apply_checkerboard_matrix(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    if 'matrix' not in testcase:
        return []
    mat = testcase['matrix']
    for r in range(len(mat)):
        for c in range(len(mat[r])):
            mat[r][c] = 'a' if (r + c) % 2 == 0 else 'b'
    testcase['matrix'] = mat
    return [f'matrix: checkerboard ({len(mat)}x{len(mat[0]) if mat else 0})']

def _apply_all_same_matrix(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    if 'matrix' not in testcase:
        return []
    mat = testcase['matrix']
    for r in range(len(mat)):
        for c in range(len(mat[r])):
            mat[r][c] = 'a'
    testcase['matrix'] = mat
    return [f'matrix: all-same ({len(mat)}x{len(mat[0]) if mat else 0})']

def _apply_collinear_pairs(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    for pair in structure.get('pairs', []):
        pair_key = f'pairs_{pair["vars"][0]}_{pair["vars"][1]}'
        if pair_key not in testcase:
            continue
        count = len(testcase[pair_key])
        testcase[pair_key] = [(i, i) for i in range(count)]
        patterns_applied.append(f'{pair_key}: collinear (y=x, n={count})')
    return patterns_applied

def _apply_clustered_pairs(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    for pair in structure.get('pairs', []):
        pair_key = f'pairs_{pair["vars"][0]}_{pair["vars"][1]}'
        if pair_key not in testcase:
            continue
        count = len(testcase[pair_key])
        testcase[pair_key] = [(0, 0)] * count
        patterns_applied.append(f'{pair_key}: clustered at origin (n={count})')
    return patterns_applied

def _apply_sawtooth(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    element_ranges = _get_element_ranges(constraints)
    for arr in structure.get('arrays', []):
        arr_name = arr['name']
        key = f'array_{arr_name}'
        if key not in testcase:
            continue
        length = len(testcase[key])
        lo, hi = element_ranges.get(arr_name, (1, 1000000000))
        period = max(4, length // 4)
        half = period // 2
        testcase[key] = [hi if (i % period) < half else lo for i in range(length)]
        patterns_applied.append(f'array_{arr_name}: sawtooth period={period} hi={hi} lo={lo}')
    return patterns_applied

def _apply_coprime_sequence(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    element_ranges = _get_element_ranges(constraints)
    for arr in structure.get('arrays', []):
        arr_name = arr['name']
        key = f'array_{arr_name}'
        if key not in testcase:
            continue
        length = len(testcase[key])
        lo, hi = element_ranges.get(arr_name, (1, 1000000000))
        if length <= 1:
            testcase[key] = [hi]
        else:
            span = hi - lo
            if span >= length - 1:
                step = span // (length - 1)
                testcase[key] = [lo + i * step for i in range(length)]
            else:
                testcase[key] = [lo + (i % max(1, span + 1)) for i in range(length)]
        patterns_applied.append(f'array_{arr_name}: coprime-seq [{lo}..{hi}] n={length}')
    return patterns_applied

PATTERN_APPLICATORS = {
    'reverse_sorted': _apply_reverse_sorted,
    'all_equal': _apply_all_equal,
    'sorted_asc': _apply_sorted_asc,
    'alternating': _apply_alternating,
    'sawtooth': _apply_sawtooth,
    'coprime_sequence': _apply_coprime_sequence,
    'bamboo': _apply_bamboo_tree,
    'star': _apply_star_tree,
    'caterpillar': _apply_caterpillar_tree,
    'dense_clique_chain': _apply_dense_clique_chain,
    'adversarial_bfs': _apply_adversarial_bfs,
    'periodic': _apply_periodic_string,
    'anti_hash': _apply_anti_hash_string,
    'checkerboard': _apply_checkerboard_matrix,
    'all_same': _apply_all_same_matrix,
    'collinear': _apply_collinear_pairs,
    'clustered': _apply_clustered_pairs,
}

def _apply_generic_worst_case_mutation(
    testcase: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> List[str]:
    patterns_applied = []
    element_ranges = _get_element_ranges(constraints)

    for arr in structure.get('arrays', []):
        arr_name = arr['name']
        key = f'array_{arr_name}'
        if key not in testcase:
            continue

        length = len(testcase[key])
        lo, hi = element_ranges.get(arr_name, (1, 1000000000))

        testcase[key] = [hi] * length
        patterns_applied.append(f'array_{arr_name}: all-same ({hi})')

    if 'edges' in testcase and structure.get('edges', {}).get('is_tree'):
        n_val = min(testcase.get('values', {}).get('n', 2), 500000)
        testcase['edges'] = [(i, i + 1) for i in range(1, n_val)]
        if 'edge_weights' in testcase:
            testcase['edge_weights'] = [1] * (n_val - 1)
        patterns_applied.append(f'tree: bamboo/chain (generic)')

    return patterns_applied

def format_testcase_stdin(testcase: Dict[str, Any], structure: Dict[str, Any]) -> str:
    if testcase is None:
        return ""

    lines = []
    values = testcase.get('values', {})

    tcv = structure.get('test_cases_var')
    t_val = int(values.get(tcv, 1)) if tcv else 1

    if tcv:
        lines.append(str(t_val))

    case_lines = []

    if structure.get('header_vars'):
        case_lines.append(' '.join(str(values.get(v, 1)) for v in structure['header_vars']))

    for arr in structure.get('arrays', []):
        key = f'array_{arr["name"]}'
        if key in testcase:
            if arr.get('per_line'):
                for x in testcase[key]:
                    case_lines.append(str(x))
            else:
                case_lines.append(' '.join(str(x) for x in testcase[key]))

    for s in structure.get('strings', []):
        key = f'string_{s["name"]}'
        if key in testcase:
            case_lines.append(testcase[key])

    if 'edges' in testcase:
        edge_weights = testcase.get('edge_weights')
        for i, (u, v) in enumerate(testcase['edges']):
            if edge_weights and i < len(edge_weights):
                case_lines.append(f'{u} {v} {edge_weights[i]}')
            else:
                case_lines.append(f'{u} {v}')

    for pair in structure.get('pairs', []):
        pair_key = f'pairs_{pair["vars"][0]}_{pair["vars"][1]}'
        if pair_key in testcase:
            for x, y in testcase[pair_key]:
                case_lines.append(f'{x} {y}')

    if 'matrix' in testcase:
        for row in testcase['matrix']:
            case_lines.append(''.join(str(x) for x in row))

    for _ in range(t_val):
        lines.extend(case_lines)

    return '\n'.join(lines) + '\n'

def expand_compact_m1_tc(
    tc_entry: Dict[str, Any],
    constraints: List[Dict[str, Any]],
    structure: Dict[str, Any],
) -> Optional[str]:
    if tc_entry.get('method') != 'boundary_slow_compact':
        return tc_entry.get('stdin')

    tc_data = tc_entry.get('testcase', {})
    fill  = tc_data.get('_fill', 'max')
    graph = tc_data.get('_graph_structure', 'chain')
    str_strat = tc_data.get('_string_strategy', 'default')
    values = {k: v for k, v in tc_data.items()
              if not k.startswith('_') and isinstance(v, (int, float))}
    if not values and not structure:
        return None

    try:
        expanded = _fill_data_structures(values, constraints, structure, fill, graph,
                                         string_strategy=str_strat)
        return format_testcase_stdin(expanded, structure)
    except Exception:
        return None

def format_testcase_json(testcase: Dict[str, Any], structure: Dict[str, Any]) -> Dict[str, Any]:
    if testcase is None:
        return {}

    result = dict(testcase.get('values', {}))

    for key, val in testcase.items():
        if key.startswith('array_'):
            result[key.replace('array_', '')] = val
        elif key.startswith('string_'):
            result[key.replace('string_', '')] = val
        elif key in ('edges', 'edge_weights', 'matrix'):
            result[key] = val
        elif key.startswith('pairs_'):
            result[key] = val

    result.pop('values', None)

    for meta_key in ('_routing', '_method', '_detected_types', '_applied_patterns',
                     '_detected_structures', '_detected_algo_types',
                     '_tier', '_tier_index', '_llm_response', '_llm_prompt', '_generator_code',
                     '_fill', '_graph_structure', '_string_strategy', '_m1_values'):
        if meta_key in testcase:
            result[meta_key] = testcase[meta_key]

    return result

def _process_problem_worker(kwargs: dict, result_path: str) -> None:
    try:
        result = process_problem(**kwargs)
        with open(result_path, 'w') as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        import traceback
        err = {
            'index': kwargs['problem'].get('index', -1),
            'name': kwargs['problem'].get('name', ''),
            'error': str(e),
            'traceback': traceback.format_exc(),
            'constraints_parsed': [], 'structure': {}, 'dependency_analysis': {},
            'test_cases': {'fast': [], 'medium': [], 'slow': []},
            'testcases': [], 'stdin_texts': [], 'num_generated': 0,
        }
        with open(result_path, 'w') as f:
            json.dump(err, f, ensure_ascii=False)

def run_process_problem_isolated(
    problem: Dict[str, Any],
    z3_timeout: int = 30,
    tiers: Optional[List[str]] = None,
    num_testcases: int = 1,
    mode: str = 'both',
    llm_config: Optional[Dict[str, Any]] = None,
    timelimit: Optional[float] = None,
    refinement_prompt_name: Optional[str] = None,
    timeout: Optional[float] = None,
    split: str = '',
    compact_m1: bool = False,
    no_wcm: bool = False,
    only_module: Optional[str] = None,
    gen_timeout: int = 60,
) -> Dict[str, Any]:
    kwargs = dict(
        problem=problem,
        z3_timeout=z3_timeout,
        tiers=tiers,
        num_testcases=num_testcases,
        mode=mode,
        llm_config=llm_config,
        timelimit=timelimit,
        refinement_prompt_name=refinement_prompt_name,
        compact_m1=compact_m1,
        no_wcm=no_wcm,
        only_module=only_module,
        gen_timeout=gen_timeout,
    )
    idx = problem.get('index', -1)
    name = problem.get('name', f'problem_{idx}')

    if (llm_config or {}).get('provider') == 'local':
        try:
            return process_problem(**kwargs)
        except Exception as e:
            import traceback
            print(f"  [WARN] process_problem failed in-process for '{name}' (idx={idx}): {e}")
            traceback.print_exc()
            return {
                'index': idx, 'name': name, 'split': split,
                'error': f'inproc_{type(e).__name__}: {e}',
                'constraints_parsed': [], 'structure': {}, 'dependency_analysis': {},
                'test_cases': {'fast': [], 'medium': [], 'slow': []},
                'testcases': [], 'stdin_texts': [], 'num_generated': 0,
            }

    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
        result_path = tmp.name

    proc = multiprocessing.Process(
        target=_process_problem_worker,
        args=(kwargs, result_path),
        daemon=True,
    )
    proc.start()
    proc.join(timeout=timeout)

    if proc.is_alive():
        proc.kill()
        proc.join()
        os.unlink(result_path)
        print(f"  [WARN] process_problem timed out for '{name}' (idx={idx}) — skipping")
        return {
            'index': idx, 'name': name, 'split': split, 'error': 'subprocess_timeout',
            'constraints_parsed': [], 'structure': {}, 'dependency_analysis': {},
            'test_cases': {'fast': [], 'medium': [], 'slow': []},
            'testcases': [], 'stdin_texts': [], 'num_generated': 0,
        }

    exitcode = proc.exitcode
    if exitcode != 0:
        signal_name = f"signal {-exitcode}" if exitcode < 0 else f"exitcode {exitcode}"
        oom_hint = " (likely OOM)" if exitcode == -9 else ""
        print(f"  [WARN] process_problem subprocess failed for '{name}' (idx={idx}): "
              f"{signal_name}{oom_hint} — skipping")
        if os.path.exists(result_path):
            os.unlink(result_path)
        return {
            'index': idx, 'name': name, 'split': split,
            'error': f'subprocess_{signal_name.replace(" ", "_")}{oom_hint}',
            'constraints_parsed': [], 'structure': {}, 'dependency_analysis': {},
            'test_cases': {'fast': [], 'medium': [], 'slow': []},
            'testcases': [], 'stdin_texts': [], 'num_generated': 0,
        }

    _MAX_RESULT_BYTES = 15 * 1024 ** 3
    _result_size = os.path.getsize(result_path) if os.path.exists(result_path) else 0
    if _result_size > _MAX_RESULT_BYTES:
        size_gb = _result_size / 1024 ** 3
        print(f"  [WARN] Result for '{name}' (idx={idx}) is {size_gb:.1f} GB > 15 GB limit — skipping (too_large)")
        if os.path.exists(result_path):
            os.unlink(result_path)
        return {
            'index': idx, 'name': name, 'split': split, 'error': 'too_large',
            'constraints_parsed': [], 'structure': {}, 'dependency_analysis': {},
            'test_cases': {'fast': [], 'medium': [], 'slow': []},
            'testcases': [], 'stdin_texts': [], 'num_generated': 0,
        }

    try:
        with open(result_path, 'r') as f:
            result = json.load(f)
    except Exception as e:
        print(f"  [WARN] Failed to read subprocess result for '{name}' (idx={idx}): {e}")
        result = {
            'index': idx, 'name': name, 'split': split, 'error': f'result_read_failed: {e}',
            'constraints_parsed': [], 'structure': {}, 'dependency_analysis': {},
            'test_cases': {'fast': [], 'medium': [], 'slow': []},
            'testcases': [], 'stdin_texts': [], 'num_generated': 0,
        }
    finally:
        if os.path.exists(result_path):
            os.unlink(result_path)

    return result

def process_problem(
    problem: Dict[str, Any],
    z3_timeout: int = 30,
    tiers: Optional[List[str]] = None,
    num_testcases: int = 1,
    mode: str = 'both',
    llm_config: Optional[Dict[str, Any]] = None,
    timelimit: Optional[float] = None,
    refinement_prompt_name: Optional[str] = None,
    compact_m1: bool = False,
    no_wcm: bool = False,
    only_module: Optional[str] = None,
    gen_timeout: int = 60,
) -> Dict[str, Any]:
    tiers = tiers or ['slow']
    problem_start = time.time()

    input_desc = problem.get('input_description', '')
    problem_desc = problem.get('problem_description', '')
    constraints_text = problem.get('constraints')

    if '_parsed_constraints' in problem:
        constraints   = problem['_parsed_constraints']
        structure     = problem['_parsed_structure']
        dep_analysis  = problem['_parsed_dep_analysis']
        routing_result = problem['_parsed_routing']
    else:
        constraints  = parse_constraints(input_desc, constraints_text, problem_desc)
        structure    = parse_input_structure(input_desc)
        dep_analysis = analyze_dependencies(constraints)
        routing_result = select_worst_case_mutations(structure, problem_desc, input_desc)

    testcases = []
    stdin_texts = []
    compact_m1_infos = []
    timing = {'m1_times': [], 'm2_times': []}
    use_v3 = compact_m1 or (refinement_prompt_name == 'slow_testcase_refinement_prompt_v3')

    m2_will_run = bool(routing_result.get('detected_structures') and routing_result.get('detected_algo_types'))

    FILL_CYCLE = ['max', 'mixed', 'random', 'min']
    GRAPH_CYCLE = ['chain', 'random', 'star']
    STRING_STRATEGY_DEFAULT = 'default'

    _LEGACY_PROMPT_VERSIONS = {
        None,
        'slow_testcase_refinement_prompt_v1',
        'slow_testcase_refinement_prompt_v2',
        'slow_testcase_refinement_prompt_v3',
        'slow_testcase_refinement_prompt_v4',
        'slow_testcase_refinement_prompt_v5',
        'slow_testcase_refinement_prompt_v6',
        'slow_testcase_refinement_prompt_v8',
        'slow_testcase_refinement_prompt',
        'slow_testcase_refinement_prompt_v10',
    }
    _use_legacy_m1 = refinement_prompt_name in _LEGACY_PROMPT_VERSIONS
    _slow_fill = SLOW_FILL_CYCLE_LEGACY if _use_legacy_m1 else SLOW_FILL_CYCLE
    _slow_graph = SLOW_GRAPH_CYCLE_LEGACY if _use_legacy_m1 else SLOW_GRAPH_CYCLE
    _slow_string = SLOW_STRING_STRATEGY_LEGACY if _use_legacy_m1 else SLOW_STRING_STRATEGY

    _V6_BATCH_VERSIONS = {
        'slow_testcase_refinement_prompt_v6',
        'slow_testcase_refinement_prompt_v7',
        'slow_testcase_refinement_prompt_v8',
        'slow_testcase_refinement_prompt',
        'slow_testcase_refinement_prompt_v10',
    }
    is_v6_batch = (refinement_prompt_name in _V6_BATCH_VERSIONS
                   and (
                       (refinement_prompt_name == 'slow_testcase_refinement_prompt_v6' and Slow_testcase_refinement_prompt_v6 is not None and TIER_BLOCKS_V6 is not None)
                       or (refinement_prompt_name == 'slow_testcase_refinement_prompt_v7' and Slow_testcase_refinement_prompt_v7 is not None and TIER_BLOCKS_V7 is not None)
                       or (refinement_prompt_name == 'slow_testcase_refinement_prompt_v8' and Slow_testcase_refinement_prompt_v8 is not None and TIER_BLOCKS_V8 is not None)
                       or (refinement_prompt_name == 'slow_testcase_refinement_prompt' and Slow_testcase_refinement_prompt_v9 is not None and TIER_BLOCKS_V9 is not None)
                       or (refinement_prompt_name == 'slow_testcase_refinement_prompt_v10' and Slow_testcase_refinement_prompt_v10 is not None and TIER_BLOCKS_V10 is not None)
                   )
                   and extract_python_from_response is not None
                   and validate_generator is not None)

    for tier in tiers:
        config = TIER_CONFIGS[tier]

        algo_strategies_used = []

        if is_v6_batch:
            fill = config['array_fill']
            graph = config['graph_structure']

            base_tc = None
            if mode in ('boundary', 'both', 'All_USE', 'smt_only', 'scenario_only', 'smt_only_minimal', 'scenario_only_minimal'):
                m1_start = time.time()
                base_tc = generate_boundary_slow(
                    constraints, structure,
                    z3_timeout=z3_timeout,
                    array_fill=fill,
                    graph_structure=graph,
                    value_strategy=config['value_strategy'],
                    compact_only=use_v3,
                    tc_idx=0,
                )
                m1_elapsed = time.time() - m1_start
                if base_tc is not None:
                    timing['m1_times'].append(m1_elapsed)
            if base_tc is None and mode in ('algorithmic', 'All_USE', 'smt_only', 'scenario_only', 'smt_only_minimal', 'scenario_only_minimal'):
                base_tc = {"values": {}}

            skip_m2 = (only_module == 'm1')
            m1_failed = base_tc is None and mode in ('both', 'All_USE')
            run_m2 = (not skip_m2) and (
                (mode == 'algorithmic') or (mode == 'both' and m2_will_run) or (mode == 'All_USE')
                or (mode in ('smt_only', 'scenario_only', 'smt_only_minimal', 'scenario_only_minimal'))
                or m1_failed
            )

            m2_generator_codes = {}
            m2_prompt = None
            m2_llm_response = None
            if run_m2 and llm_config and llm_config.get('provider') and base_tc is not None:
                m2_start = time.time()
                if mode == 'algorithmic':
                    _prompt_routing = {
                        'detected_structures': [], 'detected_algo_types': [],
                        'selected_patterns': [], 'routing_log': [],
                    }
                    _prompt_structure = {}
                    _prompt_constraints = []
                else:
                    _prompt_routing = routing_result
                    _prompt_structure = structure
                    _prompt_constraints = constraints
                m2_prompt = _build_refinement_prompt(
                    problem_desc, input_desc, _prompt_structure,
                    _prompt_routing, base_tc, _prompt_constraints,
                    tc_index=1,
                    tc_total=num_testcases,
                    previous_strategies=None,
                    timelimit=timelimit,
                    refinement_prompt_name=refinement_prompt_name,
                    mode=mode,
                    tier=tier,
                    pure_llm=(mode == 'algorithmic'),
                    problem_name=problem.get('name'),
                )

                if m2_prompt is None:
                    logger.info(f"  [skip M2] no catalog scenario matched for {problem.get('name', '?')} tier={tier}")
                    base_tc['_applied_patterns'] = ['m1_only_no_catalog_match']
                    base_tc['_skipped_m2'] = True
                    run_m2 = False
                    m2_generator_codes = {}

                if (m2_prompt is not None
                        and llm_config.get('provider') == 'local'
                        and "Qwen3" in (llm_config.get('local_model') or "")):
                    m2_prompt = m2_prompt + (
                        "\n\nIMPORTANT (output budget): Keep EACH `def generate()` "
                        "concise — under ~30 lines, minimal explanatory comments, "
                        "no verbose docstrings. The full JSON response must fit "
                        "within ~4000 tokens total."
                    )

                parse_retries = llm_config.get('parse_retries', 10) if m2_prompt is not None else 0
                for attempt in range(parse_retries):
                    try:
                        response = _call_llm(m2_prompt, llm_config)
                        if not response:
                            continue

                        _resp_stripped = _extract_json_from_response(response)
                        if not _resp_stripped:
                            logger.warning(f"v6 batch: no JSON found in response (attempt {attempt+1}); "
                                           f"raw[:300]={response[:300]!r}")
                            continue

                        batch_json = json.loads(_resp_stripped)
                        if not isinstance(batch_json, dict):
                            logger.warning(f"v6 batch: expected dict, got {type(batch_json)} (attempt {attempt+1})")
                            continue

                        new_count = 0
                        for tc_key in sorted(batch_json.keys()):
                            try:
                                _tc_num = int(tc_key.split('_')[1]) - 1
                            except (IndexError, ValueError):
                                continue
                            if _tc_num < 0 or _tc_num >= num_testcases:
                                continue
                            if _tc_num in m2_generator_codes:
                                continue

                            code = batch_json[tc_key]
                            if not isinstance(code, str):
                                continue
                            if 'def generate' not in code:
                                if extract_python_from_response:
                                    code = extract_python_from_response(code) or code
                                if not isinstance(code, str) or 'def generate' not in code:
                                    continue

                            ok, msg, _stdin = validate_generator(code, timeout=gen_timeout)
                            if ok:
                                m2_generator_codes[_tc_num] = code
                                new_count += 1
                            else:
                                logger.warning(f"v6 batch: tc_{_tc_num+1} validation failed: {msg[:200]}")

                        total_filled = len(m2_generator_codes)
                        if response:
                            m2_llm_response = response
                        logger.info(f"v6 batch [{tier}]: attempt {attempt+1}: +{new_count} new, "
                                    f"{total_filled}/{num_testcases} total filled")
                        timing['m2_times'].append(time.time() - m2_start)
                        if total_filled >= num_testcases:
                            break
                    except json.JSONDecodeError as e:
                        _raw_head = response[:300] if 'response' in locals() and response else '<empty>'
                        logger.warning(f"v6 batch: JSON parse failed (attempt {attempt+1}): {e}; "
                                       f"raw[:300]={_raw_head!r}")
                        timing['m2_times'].append(time.time() - m2_start)
                    except Exception as e:
                        logger.warning(f"v6 batch: LLM call failed (attempt {attempt+1}): {e}")
                        timing['m2_times'].append(time.time() - m2_start)

            if run_m2 and m2_generator_codes:
                m1_values = base_tc.get('values', {}) if base_tc else {}
                for tc_idx in range(num_testcases):
                    if tc_idx in m2_generator_codes:
                        tc_algo = {
                            'values': dict(m1_values),
                            '_method': 'algorithmic_slow',
                            '_tier': tier,
                            '_tier_index': tc_idx,
                            '_applied_patterns': ['pure_llm'] if mode == 'algorithmic' else ['llm_generation'],
                            '_generator_code': m2_generator_codes[tc_idx],
                            '_detected_structures': [] if mode == 'algorithmic' else routing_result.get('detected_structures', []),
                            '_detected_algo_types': [] if mode == 'algorithmic' else routing_result.get('detected_algo_types', []),
                            '_routing': {} if mode == 'algorithmic' else routing_result,
                            '_fill': fill,
                            '_graph_structure': graph,
                            '_m1_values': dict(m1_values),
                        }
                        if m2_prompt:
                            tc_algo['_llm_prompt'] = m2_prompt
                        if m2_llm_response:
                            tc_algo['_llm_response'] = m2_llm_response[:5000] if len(m2_llm_response) > 5000 else m2_llm_response
                        testcases.append(format_testcase_json(tc_algo, structure))
                        stdin_texts.append(None)
                        compact_m1_infos.append(None)
            elif base_tc is not None and not run_m2:
                for tc_idx in range(num_testcases):
                    if num_testcases > 1:
                        if tier == 'slow':
                            _fill = _slow_fill[tc_idx % len(_slow_fill)]
                            _graph = _slow_graph[tc_idx % len(_slow_graph)]
                            _str_strat = _slow_string[tc_idx % len(_slow_string)]
                        else:
                            fill_options = [config['array_fill']] + [f for f in FILL_CYCLE if f != config['array_fill']]
                            graph_options = [config['graph_structure']] + [g for g in GRAPH_CYCLE if g != config['graph_structure']]
                            _fill = fill_options[tc_idx % len(fill_options)]
                            _graph = graph_options[tc_idx % len(graph_options)]
                            _str_strat = STRING_STRATEGY_DEFAULT
                    else:
                        _fill = fill
                        _graph = graph
                        _str_strat = _slow_string[0] if tier == 'slow' else STRING_STRATEGY_DEFAULT

                    tc_m1 = generate_boundary_slow(
                        constraints, structure,
                        z3_timeout=z3_timeout,
                        array_fill=_fill,
                        graph_structure=_graph,
                        value_strategy=config['value_strategy'],
                        compact_only=use_v3,
                        tc_idx=tc_idx,
                        string_strategy=_str_strat,
                    )
                    if tc_m1 is not None:
                        tc_m1['_tier'] = tier
                        tc_m1['_tier_index'] = tc_idx
                        tc_m1['_fill'] = _fill
                        tc_m1['_graph_structure'] = _graph
                        tc_m1['_string_strategy'] = _str_strat
                        timing['m1_times'].append(0)
                        testcases.append(format_testcase_json(tc_m1, structure))
                        method = tc_m1.get('_method', '')
                        if method == 'boundary_slow_compact':
                            stdin_texts.append(None)
                            compact_m1_infos.append({
                                'values': dict(tc_m1.get('values', {})),
                                'fill': _fill,
                                'graph_structure': _graph,
                                'string_strategy': _str_strat,
                            })
                        else:
                            stdin_texts.append(format_testcase_stdin(tc_m1, structure))
                            compact_m1_infos.append(None)

            continue

        for tc_idx in range(num_testcases):
            if num_testcases > 1:
                if tier == 'slow':
                    fill = _slow_fill[tc_idx % len(_slow_fill)]
                    graph = _slow_graph[tc_idx % len(_slow_graph)]
                    str_strat = _slow_string[tc_idx % len(_slow_string)]
                else:
                    fill_options = [config['array_fill']] + [f for f in FILL_CYCLE if f != config['array_fill']]
                    graph_options = [config['graph_structure']] + [g for g in GRAPH_CYCLE if g != config['graph_structure']]
                    fill = fill_options[tc_idx % len(fill_options)]
                    graph = graph_options[tc_idx % len(graph_options)]
                    str_strat = STRING_STRATEGY_DEFAULT
            else:
                fill = config['array_fill']
                graph = config['graph_structure']
                str_strat = _slow_string[0] if tier == 'slow' else STRING_STRATEGY_DEFAULT

            generated = []

            if mode in ('boundary', 'both', 'All_USE'):
                m1_start = time.time()
                tc_boundary = generate_boundary_slow(
                    constraints, structure,
                    z3_timeout=z3_timeout,
                    array_fill=fill,
                    graph_structure=graph,
                    value_strategy=config['value_strategy'],
                    compact_only=use_v3,
                    tc_idx=tc_idx,
                    string_strategy=str_strat,
                )
                m1_elapsed = time.time() - m1_start
                if tc_boundary is not None:
                    tc_boundary['_tier'] = tier
                    tc_boundary['_tier_index'] = tc_idx
                    tc_boundary['_time_sec'] = round(m1_elapsed, 3)
                    tc_boundary['_fill'] = fill
                    tc_boundary['_string_strategy'] = str_strat
                    tc_boundary['_graph_structure'] = graph
                    generated.append(('boundary', tc_boundary))
                    timing['m1_times'].append(m1_elapsed)

            skip_m2 = (only_module == 'm1')
            m1_failed = not generated and mode in ('both', 'All_USE')
            run_m2 = (not skip_m2) and (
                (mode == 'algorithmic') or (mode == 'both' and m2_will_run) or (mode == 'All_USE') or m1_failed
            )

            if run_m2:
                base_tc = None
                if generated:
                    base_tc = json.loads(json.dumps(generated[0][1]))
                elif mode in ('algorithmic', 'All_USE'):
                    base_tc = {"values": {}}

                if base_tc is None:
                    tc_algo = None
                    m2_elapsed = 0.0
                else:
                    m2_start = time.time()
                    tc_algo = generate_algorithmic_slow(
                        constraints, structure,
                        problem_desc=problem_desc,
                        input_desc=input_desc,
                        boundary_testcase=base_tc,
                        z3_timeout=z3_timeout,
                        graph_structure=graph,
                        llm_config=llm_config,
                        tc_index=tc_idx + 1,
                        tc_total=num_testcases,
                        previous_strategies=algo_strategies_used if algo_strategies_used else None,
                        timelimit=timelimit,
                        refinement_prompt_name=refinement_prompt_name,
                        tier=tier,
                        no_wcm=no_wcm,
                        gen_timeout=gen_timeout,
                        pure_llm=(mode == 'algorithmic'),
                    )
                    m2_elapsed = time.time() - m2_start
                if tc_algo is not None:
                    tc_algo['_tier'] = tier
                    tc_algo['_tier_index'] = tc_idx
                    tc_algo['_time_sec'] = round(m2_elapsed, 3)
                    if base_tc is not None:
                        tc_algo['_m1_values'] = dict(base_tc.get('values', {}))
                    strategy_desc = f"fill={fill}, graph={graph}, patterns={tc_algo.get('_applied_patterns', [])}"
                    algo_strategies_used.append(strategy_desc)
                    timing['m2_times'].append(m2_elapsed)
                    if mode in ('both', 'All_USE'):
                        generated = [('algorithmic', tc_algo)]
                    else:
                        generated.append(('algorithmic', tc_algo))
                else:
                    pass

            if only_module == 'm1':
                generated = [(lbl, tc) for lbl, tc in generated if 'boundary' in tc.get('_method', '')]
            elif only_module == 'm2':
                generated = [(lbl, tc) for lbl, tc in generated if 'algorithmic' in tc.get('_method', '')]

            for _, tc in generated:
                testcases.append(format_testcase_json(tc, structure))
                method = tc.get('_method', '')
                if method == 'boundary_slow_compact':
                    stdin_texts.append(None)
                    compact_m1_infos.append({
                        'values': dict(tc.get('values', {})),
                        'fill': tc.get('_fill', fill),
                        'graph_structure': tc.get('_graph_structure', graph),
                        'string_strategy': tc.get('_string_strategy', 'default'),
                    })
                elif '_generator_code' in tc:
                    stdin_texts.append(None)
                    compact_m1_infos.append(None)
                else:
                    stdin_texts.append(format_testcase_stdin(tc, structure))
                    compact_m1_infos.append(None)

    problem_elapsed = time.time() - problem_start
    timing['total_sec'] = round(problem_elapsed, 3)
    timing['m1_total_sec'] = round(sum(timing['m1_times']), 3)
    timing['m2_total_sec'] = round(sum(timing['m2_times']), 3)
    timing['m1_avg_sec'] = round(timing['m1_total_sec'] / max(len(timing['m1_times']), 1), 3)
    timing['m2_avg_sec'] = round(timing['m2_total_sec'] / max(len(timing['m2_times']), 1), 3)

    test_cases_by_tier = {'fast': [], 'medium': [], 'slow': []}
    for tc_meta, stdin_text, compact_info in zip(testcases, stdin_texts, compact_m1_infos):
        tier = tc_meta.get('_tier', 'slow')
        if tier not in test_cases_by_tier:
            continue
        if compact_info is not None:
            test_cases_by_tier[tier].append({'compact_m1': True, **compact_info})
        elif '_generator_code' in tc_meta:
            test_cases_by_tier[tier].append({
                'generator_code': True,
                'code': tc_meta['_generator_code'],
            })
        else:
            test_cases_by_tier[tier].append({'input': stdin_text or ''})

    return {
        'index': problem.get('index', -1),
        'name': problem.get('name', ''),
        'split': problem.get('split', ''),
        'constraints_parsed': constraints,
        'structure': structure,
        'dependency_analysis': dep_analysis,
        'test_cases': test_cases_by_tier,
        'testcases': testcases,
        'stdin_texts': stdin_texts,
        'num_generated': len(testcases),
        'routing': {
            'detected_structures': routing_result['detected_structures'],
            'detected_algo_types': routing_result['detected_algo_types'],
            'm2_activated': m2_will_run,
        },
        'timing': timing,
    }

def main():
    parser = argparse.ArgumentParser(
        description='Slow Test Case Generator (Boundary + Algorithmic)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  boundary        Module 1 only: maximize input SIZE (rule-based + Z3 SMT)
  algorithmic     Module 2 only: adversarial DATA PATTERNS (reverse-sorted arrays,
                  bamboo trees, anti-hash strings, etc.)
  both            Module 1 first; if routing detects structures/algo types that
                  benefit from adversarial patterns, also run Module 2 (default)
  All_USE         Module 1 always, then Module 2 always (routing ignored)
  both-only-m1    Update M1 (boundary) TCs for M1-only problems (skips problems that have M2)
  both-only-m2    Update M2 (algorithmic) TCs for M2-only problems (skips problems that have M1)

Tiers:
  fast         Lower-bound values, minimal arrays, star graphs
  medium       80th-percentile values, mixed arrays, random graphs
  slow         Upper-bound values, maximal arrays, chain graphs (default)
        """,
    )
    parser.add_argument('--split', type=str, default='test',
                        choices=['test', 'train', 'valid', 'all'])
    parser.add_argument('--features_dir', type=str,
                        default='dataset/codecontests_description_separated')
    parser.add_argument('--output_dir', type=str,
                        default='output/codecontests/our_method')
    parser.add_argument('--max_problems', type=int, default=0,
                        help='Max problems (0=all)')
    parser.add_argument('--z3_timeout', type=int, default=30,
                        help='Z3 timeout seconds')
    parser.add_argument('--num_testcases', type=int, default=1,
                        help='Test cases per tier per problem')
    parser.add_argument('--tiers', type=str, default='slow',
                        help='Comma-separated tiers: fast,medium,slow (default: slow)')
    parser.add_argument('--mode', type=str, default='both',
                        choices=['boundary', 'algorithmic', 'both', 'All_USE',
                                 'both-only-m1', 'both-only-m2',
                                 'smt_only', 'scenario_only'],
                        help='Generation mode. '
                             'both-only-m1: update M1 TCs for M1-only problems (skips problems that have M2). '
                             'both-only-m2: update M2 TCs for M2-only problems (skips problems that have M1). '
                             'smt_only: ablation — LLM gets SMT max values ONLY (scenario hints suppressed). '
                             'scenario_only: ablation — LLM gets scenarios ONLY (SMT max suppressed).')
    parser.add_argument('--llm_config', type=str,
                        default='code/config/test_case_generator_model.yaml',
                        help='Path to LLM config YAML for Module 2')
    parser.add_argument('--no_llm', action='store_true',
                        help='Disable LLM refinement (rule-based only)')
    parser.add_argument('--no_wcm', action='store_true',
                        help='Ablation: skip rule-based worst-case mutation (Step A) and '
                             'blank routing info in LLM prompt. LLM generates purely from problem description.')
    parser.add_argument('--train_sample_ratio', type=float, default=1.0,
                        help='Fraction of train split to use (0.0-1.0, default: 1.0 = all)')
    parser.add_argument('--train_sample_seed', type=int, default=42,
                        help='Random seed for reproducible train split sampling (default: 42)')
    parser.add_argument('--provider', type=str, default=None,
                        help='Override LLM provider (gemini/openai/local)')
    parser.add_argument('--model', type=str, default=None,
                        help='Override LLM model name (overrides YAML {provider}_model). '
                             'e.g., gemini-3-flash-preview, gpt-5.4-mini, google/gemma-3-12b-it')
    parser.add_argument('--api_key', type=str, default=None,
                        help='API key (overrides env var)')
    parser.add_argument('--llm_temperature', type=float, default=None,
                        help='Override LLM temperature (default 0.7 from config). '
                             'Non-default values auto-tag the output path.')
    parser.add_argument('--llm_top_p', type=float, default=None,
                        help='Override LLM top_p (default 0.95 from config).')
    parser.add_argument('--llm_top_k', type=int, default=None,
                        help='Override LLM top_k (Gemini/Local only — OpenAI Chat ignores).')
    parser.add_argument('--output_tag', type=str, default='',
                        help='Manual tag appended to model dir in output path (e.g., "expt1"). '
                             'Overrides the auto temperature-derived tag when set.')
    parser.add_argument('--log_dir', type=str,
                        default='scripts/log/slow_testcase_generator',
                        help='Log directory (logs saved per split/model)')
    parser.add_argument('--resume', action='store_true',
                        help='Skip problems already generated (based on existing slow_testcases_{split}.json)')
    parser.add_argument('--parsed_structures_dir', type=str,
                        default='dataset/parsed_structures',
                        help='Directory for pre-computed structure/constraint cache '
                             '(train.json, valid.json, test.json). '
                             'If the file exists it is loaded and reused; '
                             'new entries are appended after the pre-scan.')
    parser.add_argument('--refresh_cache', action='store_true',
                        help='Ignore existing parsed-structures cache and recompute all entries. '
                             'The old cache file is overwritten with fresh results.')
    parser.add_argument('--cache_dir', type=str, default=None,
                        help='HuggingFace dataset cache dir (for loading timelimit)')
    parser.add_argument('--refinement_prompt', type=str,
                        default='slow_testcase_refinement_prompt_v2',
                        help='Refinement prompt version name '
                             '(slow_testcase_refinement_prompt_v1/v2/v3). '
                             'Also used as a path component: output_dir/{split}/{mode}/{refinement_prompt}/{model}/')
    parser.add_argument('--compact_m1', action='store_true', default=False,
                        help='Store M1 boundary TCs as compact values (no full array expansion). '
                             'Full stdin is produced at evaluation time. '
                             'To also pass compact M1 context to the M2 LLM prompt, set '
                             '--refinement_prompt slow_testcase_refinement_prompt_v3.')
    parser.add_argument('--gen_timeout', type=int, default=60,
                        help='Timeout (seconds) for executing M2 generator code validation (default: 60)')
    parser.add_argument('--knn_fallback', action='store_true', default=False,
                        help='Enable KNN-based catalog scenario fallback when keyword/structure match fails. '
                             'Uses sentence-transformers to embed problem and find nearest scenarios.')
    parser.add_argument('--knn_threshold', type=float, default=0.45,
                        help='KNN cosine similarity threshold (default 0.45). '
                             'Lower = more permissive (catches more no-match cases). '
                             'Recommended: 0.40-0.50 for mpnet-base-v2.')
    parser.add_argument('--knn_top_k', type=int, default=2,
                        help='Max scenarios returned per problem by KNN (default 2)')
    parser.add_argument('--knn_model_name', type=str, default='all-mpnet-base-v2',
                        help='sentence-transformers model name (default: all-mpnet-base-v2). '
                             'Alt: all-MiniLM-L6-v2 (smaller/faster but lower quality).')
    args = parser.parse_args()

    global _KNN_ARGS
    _KNN_ARGS = {
        "use_knn_fallback": bool(args.knn_fallback),
        "knn_threshold": float(args.knn_threshold),
        "knn_top_k": int(args.knn_top_k),
        "knn_model_name": str(args.knn_model_name),
    }
    if _KNN_ARGS["use_knn_fallback"]:
        logger.info(f"KNN fallback enabled: threshold={_KNN_ARGS['knn_threshold']}, "
                    f"top_k={_KNN_ARGS['knn_top_k']}, model={_KNN_ARGS['knn_model_name']}")

    args.only_module = None
    args.base_mode = args.mode
    if args.mode.endswith('-only-m1'):
        args.only_module = 'm1'
        args.base_mode = args.mode[:-len('-only-m1')]
    elif args.mode.endswith('-only-m2'):
        args.only_module = 'm2'
        args.base_mode = args.mode[:-len('-only-m2')]

    args.mode_dir_name = args.base_mode + ('-no-wcm' if args.no_wcm else '')

    tiers = [t.strip() for t in args.tiers.split(',')]
    for t in tiers:
        if t not in TIER_CONFIGS:
            print(f"[ERROR] Unknown tier '{t}'. Valid tiers: {list(TIER_CONFIGS.keys())}")
            return

    llm_config = None
    if not args.no_llm:
        config_path = args.llm_config
        if os.path.exists(config_path):
            import yaml
            with open(config_path) as f:
                llm_config = yaml.safe_load(f)
            if args.provider:
                llm_config['provider'] = args.provider
            if args.model:
                provider = llm_config.get('provider', args.provider or '')
                if provider == 'local':
                    llm_config['local_model'] = args.model
                elif provider == 'gemini':
                    llm_config['gemini_model'] = args.model
                elif provider == 'openai':
                    llm_config['openai_model'] = args.model
            if args.api_key:
                llm_config['api_key'] = args.api_key
            if args.llm_temperature is not None:
                llm_config['temperature'] = args.llm_temperature
            if args.llm_top_p is not None:
                llm_config['top_p'] = args.llm_top_p
            if args.llm_top_k is not None:
                llm_config['top_k'] = args.llm_top_k
            _model_key = f"{llm_config.get('provider', 'none')}_model"
            print(f"[LLM] Provider: {llm_config.get('provider', 'none')}, Model: {llm_config.get(_model_key, 'default')}, "
                  f"temperature={llm_config.get('temperature', 0.7)}, top_p={llm_config.get('top_p', 0.95)}, top_k={llm_config.get('top_k', '-')}")
        else:
            print(f"[INFO] LLM config not found at {config_path}, using rule-based only")

    if not Z3_AVAILABLE and args.base_mode in ('boundary', 'both', 'All_USE') and args.only_module != 'm2':
        print("[WARN] z3-solver not installed. SMT constraints will use heuristic fallback.")

    if llm_config:
        provider = llm_config.get('provider', 'none')
        if provider == 'local':
            model_name_for_log = llm_config.get('local_model', 'unknown').replace('/', '_')
        elif provider == 'gemini':
            model_name_for_log = llm_config.get('gemini_model', 'unknown')
        elif provider == 'openai':
            model_name_for_log = llm_config.get('openai_model', 'unknown')
        else:
            model_name_for_log = provider
    else:
        model_name_for_log = 'no_llm'

    _suffix = ''
    if args.output_tag:
        _suffix = '_' + args.output_tag.lstrip('_')
    else:
        _tag_parts = []
        if args.llm_temperature is not None and abs(args.llm_temperature - 0.7) > 1e-6:
            _t_str = ('%g' % args.llm_temperature).replace('.', '').lstrip('0') or '0'
            _tag_parts.append(f't{_t_str}')
        if args.llm_top_p is not None and abs(args.llm_top_p - 0.95) > 1e-6:
            _p_str = ('%g' % args.llm_top_p).replace('.', '').lstrip('0') or '0'
            _tag_parts.append(f'p{_p_str}')
        if args.llm_top_k is not None:
            _tag_parts.append(f'k{args.llm_top_k}')
        if _tag_parts:
            _suffix = '_' + '_'.join(_tag_parts)
    if _suffix:
        model_name_for_log = f'{model_name_for_log}{_suffix}'
        print(f'[LLM] non-default sampling — output dir tagged: {model_name_for_log}')

    splits = ['test', 'train', 'valid'] if args.split == 'all' else [args.split]

    for split in splits:
        features_path = os.path.join(args.features_dir, f'features_{split}.json')
        if not os.path.exists(features_path):
            print(f"[WARN] Features file not found: {features_path}")
            continue

        log_split_dir = os.path.join(args.log_dir, split, model_name_for_log)
        os.makedirs(log_split_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file_path = os.path.join(log_split_dir, f'{timestamp}.log')

        _log_fh = open(log_file_path, 'w', encoding='utf-8')

        file_handler = logging.StreamHandler(_log_fh)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S',
        ))
        logging.getLogger().addHandler(file_handler)

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.WARNING)
        stderr_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S',
        ))
        logging.getLogger().addHandler(stderr_handler)

        class _TeeWriter:
            def __init__(self, original, log_file):
                self.original = original
                self.log_file = log_file
            def write(self, text):
                self.original.write(text)
                self.log_file.write(text)
                self.log_file.flush()
            def flush(self):
                self.original.flush()
                self.log_file.flush()
            def __getattr__(self, name):
                return getattr(self.original, name)

        _original_stdout = sys.stdout
        sys.stdout = _TeeWriter(_original_stdout, _log_fh)

        print(f"[LOG] Saving log to: {log_file_path}")

        print(f"\n{'='*60}")
        _mode_desc = args.mode_dir_name
        if args.only_module:
            _module_label = 'M1 (boundary)' if args.only_module == 'm1' else 'M2 (algorithmic)'
            _mode_desc += f" [only {_module_label}]"
        print(f"Processing split: {split} | mode: {_mode_desc} | tiers: {tiers}")
        print(f"{'='*60}")

        with open(features_path, 'r') as f:
            problems = json.load(f)

        timelimit_by_idx = {}
        try:
            from datasets import load_dataset as _load_ds
            _ds = _load_ds("deepmind/code_contests", split=split, cache_dir=args.cache_dir)
            for _i in range(len(_ds)):
                tl = _ds[_i].get('time_limit')
                if tl and isinstance(tl, dict):
                    timelimit_by_idx[_i] = tl.get('seconds', 2) + tl.get('nanos', 0) / 1e9
                elif tl:
                    timelimit_by_idx[_i] = float(tl)
            print(f"[INFO] Loaded timelimit for {len(timelimit_by_idx)} problems from HF dataset")
        except Exception as _e:
            print(f"[WARN] Could not load timelimit from HF dataset: {_e}. Prompt v2 will fall back to v1.")

        if split == 'train' and 0 < args.train_sample_ratio < 1.0:
            import random as _rng
            full_count = len(problems)
            sample_count = max(1, int(full_count * args.train_sample_ratio))
            sampler = _rng.Random(args.train_sample_seed)
            problems = sampler.sample(problems, sample_count)
            print(f"[SAMPLE] train split: {full_count:,} -> {sample_count:,} "
                  f"(ratio={args.train_sample_ratio}, seed={args.train_sample_seed})")

        if args.max_problems > 0:
            problems = problems[:args.max_problems]

        _struct_cache_path = os.path.join(args.parsed_structures_dir, f'{split}.json')
        _struct_cache: dict = {}
        if args.refresh_cache:
            print(f"[STRUCT CACHE] --refresh_cache: ignoring existing cache, will recompute all")
        elif os.path.exists(_struct_cache_path):
            try:
                with open(_struct_cache_path, 'r', encoding='utf-8') as _f:
                    _struct_cache = {int(k): v for k, v in json.load(_f).items()}
                print(f"[STRUCT CACHE] Loaded {len(_struct_cache)} entries from {_struct_cache_path}")
            except Exception as _e:
                print(f"[STRUCT CACHE] Failed to load cache: {_e} — will recompute")
        _cache_hits = 0
        for _p in problems:
            _pidx = _p.get('index')
            if _pidx is not None and _pidx in _struct_cache:
                _entry = _struct_cache[_pidx]
                _p['_parsed_constraints'] = _entry.get('constraints_parsed', [])
                _p['_parsed_structure']   = _entry.get('structure', {})
                _p['_parsed_dep_analysis'] = _entry.get('dep_analysis', {})
                _p['_parsed_routing']     = _entry.get('routing', {})
                _cache_hits += 1
        if _struct_cache:
            print(f"[STRUCT CACHE] Injected into {_cache_hits}/{len(problems)} problems")

        split_output_dir = os.path.join(args.output_dir, split, args.mode_dir_name, args.refinement_prompt, model_name_for_log)
        existing_json_path = os.path.join(split_output_dir, f'slow_testcases_{split}.json')
        done_indices = set()
        prev_results = []
        _existing_results_by_idx: Dict[int, Dict[str, Any]] = {}
        if args.only_module and os.path.exists(existing_json_path):
            try:
                with open(existing_json_path, 'r') as f:
                    _all_existing = json.load(f)
                for _r in _all_existing:
                    _existing_results_by_idx[_r.get('index', -1)] = _r
                prev_results = _all_existing
                print(f"[only-{args.only_module}] Loaded {len(prev_results)} existing results from JSON "
                      f"(will update {args.only_module.upper()} entries)")
            except Exception as e:
                print(f"[only-{args.only_module}] Failed to load existing JSON: {e} — starting fresh")
        elif args.resume and os.path.exists(existing_json_path):
            try:
                with open(existing_json_path, 'r') as f:
                    all_prev = json.load(f)
                prev_results = [r for r in all_prev if r.get('num_generated', 0) > 0]
                done_indices = {r['index'] for r in prev_results}
                failed_prev = len(all_prev) - len(prev_results)
                print(f"[RESUME] Loaded {len(all_prev)} previous results: "
                      f"{len(done_indices)} completed (skip), "
                      f"{failed_prev} failed (will retry).")
                del all_prev
            except Exception as e:
                print(f"[RESUME] Failed to load existing results: {e} — starting fresh.")
                prev_results = []
                done_indices = set()
        elif args.resume:
            _inputs_dir = os.path.join(split_output_dir, 'inputs')
            if os.path.exists(_inputs_dir):
                _idx_counts: Dict[int, int] = {}
                for _tier in os.listdir(_inputs_dir):
                    _tier_dir = os.path.join(_inputs_dir, _tier)
                    if not os.path.isdir(_tier_dir):
                        continue
                    for _fname in os.listdir(_tier_dir):
                        if not _fname.endswith('.json'):
                            continue
                        try:
                            _idx = int(_fname[:4])
                            _idx_counts[_idx] = _idx_counts.get(_idx, 0) + 1
                        except ValueError:
                            pass
                for _idx, _cnt in _idx_counts.items():
                    done_indices.add(_idx)
                    prev_results.append({
                        'index': _idx, 'num_generated': _cnt, 'split': split,
                        'constraints_parsed': [], 'structure': {}, 'routing': {},
                        'dependency_analysis': {},
                        'test_cases': {t: [] for t in tiers},
                        'testcases': [], 'stdin_texts': [],
                    })
                if done_indices:
                    print(f"[RESUME] No JSON found — reconstructed {len(done_indices)} done problems "
                          f"from inputs/ directory.")

        print(f"\n--- Pre-scan: routing analysis for {len(problems)} problems ---")
        m1_only_problems = []
        m2_problems = []
        no_constraint_problems = []
        structure_counter = {}
        algo_counter = {}
        _struct_cache_new = {}

        for pi, prob in enumerate(problems):
            p_input_desc = prob.get('input_description', '')
            p_problem_desc = prob.get('problem_description', '')
            p_constraints_text = prob.get('constraints')
            p_name = prob.get('name', f"problem_{prob.get('index', pi)}")
            p_idx = prob.get('index', pi)

            if '_parsed_constraints' in prob:
                p_constraints = prob['_parsed_constraints']
                p_routing = prob['_parsed_routing']
            else:
                p_constraints = parse_constraints(p_input_desc, p_constraints_text, p_problem_desc)
                p_structure = parse_input_structure(p_input_desc)
                p_dep_analysis = analyze_dependencies(p_constraints)
                p_routing = select_worst_case_mutations(p_structure, p_problem_desc, p_input_desc)
                _struct_cache_new[p_idx] = {
                    'index': p_idx,
                    'name': p_name,
                    'constraints_parsed': p_constraints,
                    'structure': p_structure,
                    'dep_analysis': p_dep_analysis,
                    'routing': {
                        'detected_structures': p_routing['detected_structures'],
                        'detected_algo_types': p_routing['detected_algo_types'],
                        'm2_activated': bool(p_routing['detected_structures'] and p_routing['detected_algo_types']) or args.base_mode == 'All_USE',
                    },
                }
                prob['_parsed_constraints']  = p_constraints
                prob['_parsed_structure']    = p_structure
                prob['_parsed_dep_analysis'] = p_dep_analysis
                prob['_parsed_routing']      = p_routing

            if not p_constraints:
                no_constraint_problems.append(p_name)
                continue

            has_struct = bool(p_routing.get('detected_structures'))
            has_algo = bool(p_routing.get('detected_algo_types'))

            if has_struct and has_algo:
                m2_problems.append((p_name, p_routing['detected_structures'], p_routing['detected_algo_types']))
                for s in p_routing['detected_structures']:
                    structure_counter[s] = structure_counter.get(s, 0) + 1
                for a in p_routing['detected_algo_types']:
                    algo_counter[a] = algo_counter.get(a, 0) + 1
            else:
                m1_only_problems.append(p_name)

        if _struct_cache_new:
            _merged_cache = {**_struct_cache, **_struct_cache_new}
            os.makedirs(args.parsed_structures_dir, exist_ok=True)
            _tmp_path = _struct_cache_path + '.tmp'
            with open(_tmp_path, 'w', encoding='utf-8') as _f:
                json.dump({str(k): v for k, v in sorted(_merged_cache.items())}, _f,
                          indent=2, ensure_ascii=False)
            os.replace(_tmp_path, _struct_cache_path)
            print(f"[STRUCT CACHE] Saved {len(_struct_cache_new)} new entries to {_struct_cache_path} "
                  f"(total: {len(_merged_cache)})")
            _struct_cache = _merged_cache

        total = len(problems)
        print(f"  Total problems: {total}")
        print(f"  M1-only (boundary):  {len(m1_only_problems)} ({len(m1_only_problems)*100//max(total,1)}%)")
        print(f"  M2 (algorithmic+LLM): {len(m2_problems)} ({len(m2_problems)*100//max(total,1)}%)")
        print(f"  No constraints (LLM-only): {len(no_constraint_problems)} ({len(no_constraint_problems)*100//max(total,1)}%)")
        if structure_counter:
            s_str = ', '.join(f'{k}:{v}' for k, v in sorted(structure_counter.items(), key=lambda x: x[1], reverse=True))
            print(f"  Structures in M2: {s_str}")
        if algo_counter:
            a_str = ', '.join(f'{k}:{v}' for k, v in sorted(algo_counter.items(), key=lambda x: x[1], reverse=True))
            print(f"  Algorithms in M2: {a_str}")
        if m1_only_problems:
            print(f"  M1-only problems: {m1_only_problems[:10]}{'...' if len(m1_only_problems) > 10 else ''}")
        if no_constraint_problems:
            print(f"  No-constraint problems: {no_constraint_problems[:10]}{'...' if len(no_constraint_problems) > 10 else ''}")

        llm_calls_est = len(m2_problems) * args.num_testcases * len(tiers) * 2
        print(f"  Estimated max LLM calls: ~{llm_calls_est} "
              f"({len(m2_problems)} problems × {args.num_testcases} tc × {len(tiers)} tiers × 2 retries)")
        print(f"--- End pre-scan ---\n")

        inputs_dir = os.path.join(split_output_dir, 'inputs')
        for t in tiers:
            os.makedirs(os.path.join(inputs_dir, t), exist_ok=True)
        os.makedirs(inputs_dir, exist_ok=True)

        results = list(prev_results)
        del prev_results
        split_start = time.time()
        stats = {'success': 0, 'no_constraint': 0, 'failed': 0, 'rule_only': 0, 'smt_used': 0,
                 'llm_attempted': 0, 'llm_applied': 0}
        tier_stats = {t: {'problems': 0, 'testcases': 0, 'boundary': 0, 'algorithmic': 0} for t in tiers}
        routing_stats = {}
        no_structure_count = 0
        skipped_count = 0

        for i, problem in enumerate(problems):
            idx = problem.get('index', i)
            name = problem.get('name', f'problem_{idx}')

            if idx in done_indices:
                skipped_count += 1
                if skipped_count <= 5 or skipped_count % 100 == 0:
                    print(f"  [{i+1}/{len(problems)}] {name} — SKIP (already generated, resume)")
                continue

            if args.only_module:
                _other_method = 'algorithmic' if args.only_module == 'm1' else 'boundary'
                _prefix = f'{idx:04d}_'
                _has_other = False
                for _tier in tiers:
                    _tier_dir = os.path.join(inputs_dir, _tier)
                    if not os.path.isdir(_tier_dir):
                        continue
                    for _fname in os.listdir(_tier_dir):
                        if _fname.startswith(_prefix) and _other_method in _fname and _fname.endswith('.json'):
                            _has_other = True
                            break
                    if _has_other:
                        break
                if _has_other:
                    skipped_count += 1
                    if skipped_count <= 5 or skipped_count % 100 == 0:
                        print(f"  [{i+1}/{len(problems)}] {name} — SKIP (has {_other_method} TCs, only-{args.only_module})")
                    continue

            try:
                result = run_process_problem_isolated(
                    problem,
                    z3_timeout=args.z3_timeout,
                    tiers=tiers,
                    num_testcases=args.num_testcases,
                    mode=args.base_mode,
                    llm_config=llm_config,
                    timelimit=timelimit_by_idx.get(idx),
                    refinement_prompt_name=args.refinement_prompt,
                    split=split,
                    compact_m1=args.compact_m1,
                    no_wcm=args.no_wcm,
                    only_module=args.only_module,
                    gen_timeout=args.gen_timeout,
                )

                if args.only_module and idx in _existing_results_by_idx:
                    existing_entry = _existing_results_by_idx[idx]
                    _target_method = 'boundary' if args.only_module == 'm1' else 'algorithmic'
                    _other_method = 'algorithmic' if args.only_module == 'm1' else 'boundary'

                    _existing_tcs = existing_entry.get('testcases', [])
                    _existing_stds = existing_entry.get('stdin_texts', [])
                    _other_tcs = [(tc, std) for tc, std in zip(_existing_tcs, _existing_stds)
                                  if _other_method in (tc.get('_method', '') or '')]
                    _new_tcs = list(zip(result.get('testcases', []), result.get('stdin_texts', [])))
                    _merged = _other_tcs + _new_tcs
                    merged_testcases = [t[0] for t in _merged]
                    merged_stdin_texts = [t[1] for t in _merged]

                    def _is_boundary_tc(tc_entry):
                        return tc_entry.get('compact_m1') or (not tc_entry.get('generator_code') and 'input' in tc_entry)

                    merged_test_cases = {}
                    for _tier in ('fast', 'medium', 'slow'):
                        _existing_tier = existing_entry.get('test_cases', {}).get(_tier, [])
                        if _target_method == 'boundary':
                            _other_tier = [tc for tc in _existing_tier if not _is_boundary_tc(tc)]
                        else:
                            _other_tier = [tc for tc in _existing_tier if _is_boundary_tc(tc)]
                        _new_tier = result.get('test_cases', {}).get(_tier, [])
                        merged_test_cases[_tier] = _other_tier + _new_tier

                    merged_entry = dict(existing_entry)
                    merged_entry['testcases'] = merged_testcases
                    merged_entry['stdin_texts'] = merged_stdin_texts
                    merged_entry['test_cases'] = merged_test_cases
                    merged_entry['num_generated'] = len(merged_testcases)
                    for _meta_key in ('constraints_parsed', 'structure', 'dependency_analysis', 'routing', 'timing'):
                        if _meta_key in result:
                            merged_entry[_meta_key] = result[_meta_key]

                    _existing_results_by_idx[idx] = merged_entry
                    for _ri, _r in enumerate(results):
                        if _r.get('index') == idx:
                            results[_ri] = merged_entry
                            break
                    else:
                        results.append(merged_entry)
                else:
                    results.append(result)

                dep = result.get('dependency_analysis', {})
                if dep.get('dependent'):
                    stats['smt_used'] += 1
                elif dep.get('independent'):
                    stats['rule_only'] += 1

                if result['num_generated'] > 0:
                    stats['success'] += 1

                    track_llm = llm_config and llm_config.get('provider')
                    for tc in result['testcases']:
                        structures = tc.get('_detected_structures', [])
                        patterns = tc.get('_applied_patterns', [])
                        if structures:
                            for s_type in structures:
                                if s_type not in routing_stats:
                                    routing_stats[s_type] = {}
                                for p in patterns:
                                    p_name = p.split(':')[0].strip() if ':' in p else p
                                    routing_stats[s_type][p_name] = routing_stats[s_type].get(p_name, 0) + 1
                        elif tc.get('_method') == 'algorithmic_slow':
                            no_structure_count += 1
                        if track_llm and tc.get('_method') == 'algorithmic_slow':
                            stats['llm_attempted'] += 1
                            if 'llm_generation' in patterns:
                                stats['llm_applied'] += 1

                    for tc in result['testcases']:
                        tc_tier = tc.get('_tier', 'slow')
                        if tc_tier in tier_stats:
                            tier_stats[tc_tier]['testcases'] += 1
                            method = tc.get('_method', '')
                            if 'boundary' in method:
                                tier_stats[tc_tier]['boundary'] += 1
                            elif 'algorithmic' in method:
                                tier_stats[tc_tier]['algorithmic'] += 1
                    for t in tiers:
                        if any(tc.get('_tier') == t for tc in result['testcases']):
                            tier_stats[t]['problems'] += 1

                    safe_name = re.sub(r'[^\w]', '_', name)
                    errors_dir = os.path.join(split_output_dir, 'errors')

                    if args.only_module:
                        _target_method = 'boundary' if args.only_module == 'm1' else 'algorithmic'
                        _prefix = f'{idx:04d}_'
                        for _tier in tiers:
                            _tier_dir = os.path.join(inputs_dir, _tier)
                            if not os.path.isdir(_tier_dir):
                                continue
                            for _old_f in os.listdir(_tier_dir):
                                if _old_f.startswith(_prefix) and _target_method in _old_f:
                                    os.remove(os.path.join(_tier_dir, _old_f))

                    total_written_bytes = 0
                    for tc_idx, stdin_text in enumerate(result['stdin_texts']):
                        tc_meta = result['testcases'][tc_idx] if tc_idx < len(result['testcases']) else {}
                        tc_tier = tc_meta.get('_tier', 'slow')
                        tc_tier_idx = tc_meta.get('_tier_index', 0)
                        tc_method = tc_meta.get('_method', '')
                        method_label = f"_{tc_method}" if tc_method else ''
                        fname = f'{idx:04d}_{safe_name}_{tc_tier}{method_label}_{tc_tier_idx}.json'
                        _has_generator = bool(tc_meta.get('_generator_code'))
                        tc_output = {
                            'index': idx,
                            'name': name,
                            'tier': tc_tier,
                            'tier_index': tc_tier_idx,
                            'method': tc_method,
                            'stdin': None if _has_generator else stdin_text,
                            'testcase': tc_meta,
                        }
                        tier_inputs_dir = os.path.join(inputs_dir, tc_tier)
                        os.makedirs(tier_inputs_dir, exist_ok=True)
                        fpath = os.path.join(tier_inputs_dir, fname)
                        with open(fpath, 'w') as f:
                            json.dump(tc_output, f, indent=2, ensure_ascii=False)
                        total_written_bytes += os.path.getsize(fpath)

                        llm_prompt_text = tc_meta.get('_llm_prompt', '')
                        if llm_prompt_text:
                            prompt_fname = fname.replace('.json', '_prompt.txt')
                            prompt_fpath = os.path.join(tier_inputs_dir, prompt_fname)
                            with open(prompt_fpath, 'w', encoding='utf-8') as pf:
                                pf.write(llm_prompt_text)

                        llm_patterns = tc_meta.get('_applied_patterns', [])
                        is_error = any(p in llm_patterns for p in
                                       ('llm_parse_failed', 'llm_no_response', 'llm_failed'))
                        if is_error:
                            os.makedirs(errors_dir, exist_ok=True)
                            error_types = tc_meta.get('_llm_error_type', ['unknown'])
                            err_tag = '_'.join(error_types)
                            err_fname = f'{idx:04d}_{safe_name}_{tc_tier}_{tc_tier_idx}_{err_tag}.json'
                            err_output = {
                                'index': idx,
                                'name': name,
                                'tier': tc_tier,
                                'tier_index': tc_tier_idx,
                                'error_type': error_types,
                                'applied_patterns': llm_patterns,
                                'llm_raw_response': tc_meta.get('_llm_raw_response', ''),
                                'llm_prompt': tc_meta.get('_llm_prompt', ''),
                                'llm_error_msg': tc_meta.get('_llm_error_msg', ''),
                                'routing': tc_meta.get('_routing', {}),
                            }
                            with open(os.path.join(errors_dir, err_fname), 'w') as f:
                                json.dump(err_output, f, indent=2, ensure_ascii=False)

                    routing_info = result.get('routing', {})
                    r_structures = routing_info.get('detected_structures', [])
                    r_algos = routing_info.get('detected_algo_types', [])
                    m2_activated = routing_info.get('m2_activated', False)

                    methods = set()
                    tiers_used = set()
                    for tc in result['testcases']:
                        methods.add(tc.get('_method', 'unknown'))
                        tiers_used.add(tc.get('_tier', '?'))
                    patterns_list = []
                    for tc in result['testcases']:
                        for p in tc.get('_applied_patterns', []):
                            if p not in patterns_list:
                                patterns_list.append(p)

                    module_tag = []
                    if 'boundary_slow' in methods:
                        module_tag.append('M1(boundary)')
                    if 'algorithmic_slow' in methods:
                        module_tag.append('M2(algorithmic)')
                    module_str = '+'.join(module_tag) if module_tag else 'M1(boundary) only'

                    _force_m2_modes = ('All_USE', 'smt_only', 'scenario_only')
                    if args.base_mode in _force_m2_modes:
                        if m2_activated:
                            m2_label = f"RUN (forced by {args.base_mode})"
                        else:
                            m2_label = f"RUN (routing=SKIP, forced by {args.base_mode})"
                    else:
                        m2_label = "RUN" if m2_activated else "SKIP"
                    print("\n")
                    print(f"  [{i+1}/{len(problems)}] {name}")
                    print(f"    Routing: Axis1={r_structures if r_structures else '[]'} | "
                          f"Axis2={r_algos if r_algos else '[]'} → M2: {m2_label}")
                    t_info = result.get('timing', {})
                    time_parts = [f"total={t_info.get('total_sec', 0)}s"]
                    if t_info.get('m1_total_sec'):
                        time_parts.append(f"M1={t_info['m1_total_sec']}s")
                    if t_info.get('m2_total_sec'):
                        time_parts.append(f"M2={t_info['m2_total_sec']}s(avg {t_info['m2_avg_sec']}s)")
                    if total_written_bytes >= 1024 ** 3:
                        size_str = f"{total_written_bytes / 1024**3:.2f} GB"
                    elif total_written_bytes >= 1024 ** 2:
                        size_str = f"{total_written_bytes / 1024**2:.1f} MB"
                    else:
                        size_str = f"{total_written_bytes / 1024:.1f} KB"
                    print(f"    Result: {module_str} | Tiers: {sorted(tiers_used)} | TC count: {result['num_generated']} | Size: {size_str}")
                    print(f"    Time: {', '.join(time_parts)}")
                    if m2_activated and patterns_list:
                        failed_patterns = [p for p in patterns_list if p in ('llm_parse_failed', 'llm_no_response', 'llm_failed')]
                        ok_patterns = [p for p in patterns_list if p not in ('llm_parse_failed', 'llm_no_response', 'llm_failed')]
                        if failed_patterns:
                            print(f"    LLM: FAILED ({failed_patterns})")
                        if ok_patterns:
                            print(f"    LLM: OK → {ok_patterns}")
                elif result.get('error'):
                    stats['failed'] += 1
                    print(f"  [{i+1}/{len(problems)}] {name} — ERROR ({result['error']})")
                elif not result['constraints_parsed']:
                    stats['no_constraint'] += 1
                    print(f"  [{i+1}/{len(problems)}] {name} — SKIP (no constraints parsed)")
                else:
                    stats['failed'] += 1
                    print(f"  [{i+1}/{len(problems)}] {name} — FAIL (0 testcases generated)")

                result['stdin_texts'] = []
                result['test_cases'] = {'fast': [], 'medium': [], 'slow': []}
                result['testcases'] = []
                gc.collect()

            except Exception as e:
                import traceback as _tb
                stats['failed'] += 1
                print(f"  [{i+1}/{len(problems)}] {name}: ERROR - {e}")
                _tb.print_exc()
                results.append({
                    'index': idx, 'name': name, 'split': split,
                    'error': str(e), 'constraints_parsed': [], 'structure': {},
                    'dependency_analysis': {},
                    'test_cases': {'fast': [], 'medium': [], 'slow': []},
                    'testcases': [], 'stdin_texts': [],
                    'num_generated': 0,
                })

            os.makedirs(split_output_dir, exist_ok=True)
            with open(existing_json_path, 'w') as _f:
                json.dump(results, _f, ensure_ascii=False)

        output_json_path = os.path.join(split_output_dir, f'slow_testcases_{split}.json')
        with open(output_json_path, 'w') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        parsed_path = os.path.join(split_output_dir, f'parsed_structures_{split}.json')
        parsed_summary = []
        for r in results:
            parsed_summary.append({
                'index': r.get('index'),
                'name': r.get('name'),
                'structure': r.get('structure'),
                'constraints_parsed': r.get('constraints_parsed'),
                'routing': r.get('routing'),
                'stdin_texts': r.get('stdin_texts', [])[:1],
            })
        with open(parsed_path, 'w') as f:
            json.dump(parsed_summary, f, indent=2, ensure_ascii=False)

        report_path = os.path.join(split_output_dir, f'parsed_structures_{split}_report.txt')
        with open(report_path, 'w') as f:
            f.write(f"Parsed Structure Quality Report\n{'='*60}\n")
            f.write(f"Split: {split}\n")
            f.write(f"Total problems: {len(parsed_summary)}\n\n")

            empty_structure = []
            no_header = []
            no_constraints = []
            for ps in parsed_summary:
                s = ps.get('structure') or {}
                has_any = any([
                    s.get('header_vars'), s.get('arrays'), s.get('strings'),
                    s.get('edges'), s.get('matrix'), s.get('grid'), s.get('pairs'),
                ])
                if not has_any:
                    empty_structure.append(ps)
                elif not s.get('header_vars'):
                    no_header.append(ps)
                if not ps.get('constraints_parsed'):
                    no_constraints.append(ps)

            from collections import Counter as _Counter
            ctype_counts = _Counter()
            for ps in parsed_summary:
                for c in (ps.get('constraints_parsed') or []):
                    ctype_counts[c.get('type', 'unknown')] += 1

            f.write(f"[Structure Parsing]\n")
            f.write(f"  Has header_vars: {len(parsed_summary) - len(empty_structure) - len(no_header)}/{len(parsed_summary)}\n")
            f.write(f"  No header_vars (but has other fields): {len(no_header)}/{len(parsed_summary)}\n")
            f.write(f"  Completely empty structure: {len(empty_structure)}/{len(parsed_summary)}\n\n")

            if empty_structure:
                f.write(f"[Empty Structure Problems] ({len(empty_structure)})\n")
                for ps in empty_structure:
                    f.write(f"  [{ps['index']}] {ps['name']}\n")
                f.write('\n')

            if no_header:
                f.write(f"[No header_vars Problems] ({len(no_header)})\n")
                for ps in no_header:
                    s = ps.get('structure') or {}
                    fields = [k for k in ['arrays','strings','edges','matrix','grid','pairs']
                              if s.get(k)]
                    f.write(f"  [{ps['index']}] {ps['name']}  (has: {', '.join(fields)})\n")
                f.write('\n')

            f.write(f"[Constraints Parsing]\n")
            f.write(f"  Has constraints: {len(parsed_summary) - len(no_constraints)}/{len(parsed_summary)}\n")
            f.write(f"  No constraints: {len(no_constraints)}/{len(parsed_summary)}\n")
            f.write(f"  Type distribution: {dict(ctype_counts.most_common())}\n\n")

            if no_constraints:
                f.write(f"[No Constraints Problems] ({len(no_constraints)})\n")
                for ps in no_constraints:
                    f.write(f"  [{ps['index']}] {ps['name']}\n")
                f.write('\n')

            m2_count = sum(1 for ps in parsed_summary if (ps.get('routing') or {}).get('m2_activated'))
            struct_counts = _Counter()
            algo_counts = _Counter()
            for ps in parsed_summary:
                r = ps.get('routing') or {}
                for s in r.get('detected_structures', []): struct_counts[s] += 1
                for a in r.get('detected_algo_types', []): algo_counts[a] += 1

            f.write(f"[Routing]\n")
            f.write(f"  Module2 (algorithmic) activated: {m2_count}/{len(parsed_summary)}\n")
            f.write(f"  Detected structures: {dict(struct_counts.most_common())}\n")
            f.write(f"  Detected algo types: {dict(algo_counts.most_common())}\n\n")

            stdin_lens = [len(ps.get('stdin_texts', [])) for ps in parsed_summary]
            f.write(f"[stdin_texts]\n")
            f.write(f"  Total: {sum(stdin_lens)} across {len(parsed_summary)} problems\n")
            f.write(f"  Min/Max per problem: {min(stdin_lens)}/{max(stdin_lens)}\n")
            empty_stdin = [ps for ps in parsed_summary if not ps.get('stdin_texts')]
            if empty_stdin:
                f.write(f"  Problems with 0 stdin ({len(empty_stdin)}):\n")
                for ps in empty_stdin:
                    f.write(f"    [{ps['index']}] {ps['name']}\n")
            f.write('\n')

        print(f"  Parsed structure report: {report_path}")

        split_elapsed = time.time() - split_start
        all_m1 = [t for r in results for t in r.get('timing', {}).get('m1_times', [])]
        all_m2 = [t for r in results for t in r.get('timing', {}).get('m2_times', [])]
        all_totals = [r.get('timing', {}).get('total_sec', 0) for r in results if r.get('timing')]

        summary_path = os.path.join(split_output_dir, 'summary.txt')
        with open(summary_path, 'w') as f:
            f.write(f"Slow Test Case Generation Summary\n{'='*40}\n")
            f.write(f"Split: {split}\n")
            f.write(f"Mode: {args.mode_dir_name}\n")
            f.write(f"Tiers: {', '.join(tiers)}\n")
            f.write(f"Num testcases per tier: {args.num_testcases}\n")
            f.write(f"Total problems: {len(problems)}\n")
            if skipped_count > 0:
                f.write(f"Skipped (resume): {skipped_count}\n")
            f.write(f"Success (generated): {stats['success']}\n")
            f.write(f"No constraints (LLM-only): {stats['no_constraint']}\n")
            f.write(f"Failed: {stats['failed']}\n")
            processed = len(problems) - skipped_count
            f.write(f"Success rate: {stats['success']/max(1,processed)*100:.1f}%\n")
            f.write(f"\nRouting Statistics:\n")
            f.write(f"  Rule-based only: {stats['rule_only']}\n")
            f.write(f"  SMT solver used: {stats['smt_used']}\n")
            f.write(f"\nTier Statistics:\n")
            for t in tiers:
                ts = tier_stats[t]
                f.write(f"  {t}: {ts['problems']} problems, {ts['testcases']} testcases")
                if ts['boundary'] or ts['algorithmic']:
                    f.write(f" (boundary: {ts['boundary']}, algorithmic: {ts['algorithmic']})")
                f.write('\n')
            f.write(f"\nStructure Routing Statistics:\n")
            for s_type, pattern_counts in sorted(routing_stats.items()):
                pattern_str = ', '.join(f'{p}({c})' for p, c in sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True))
                f.write(f"  {s_type} → {pattern_str}\n")
            f.write(f"  No structure detected (fallback): {no_structure_count}\n")
            if llm_config and llm_config.get('provider'):
                f.write(f"\nLLM Refinement Statistics:\n")
                f.write(f"  Provider: {llm_config.get('provider')}\n")
                model_key = f"{llm_config.get('provider')}_model"
                f.write(f"  Model: {llm_config.get(model_key, 'N/A')}\n")
                f.write(f"  Attempted: {stats['llm_attempted']}\n")
                f.write(f"  Applied: {stats['llm_applied']}\n")
                if stats['llm_attempted'] > 0:
                    f.write(f"  Success rate: {stats['llm_applied']/stats['llm_attempted']*100:.1f}%\n")
            f.write(f"\nTiming Statistics:\n")
            split_min = split_elapsed / 60.0
            f.write(f"  Total split time: {split_min:.2f} min ({split_elapsed:.1f}s)\n")
            if all_totals:
                f.write(f"  Per-problem: avg={sum(all_totals)/len(all_totals):.2f}s, "
                        f"min={min(all_totals):.2f}s, max={max(all_totals):.2f}s\n")
            if all_m1:
                f.write(f"  M1 (boundary): {len(all_m1)} calls, total={sum(all_m1):.2f}s, "
                        f"avg={sum(all_m1)/len(all_m1):.3f}s\n")
            if all_m2:
                f.write(f"  M2 (algorithmic/LLM): {len(all_m2)} calls, total={sum(all_m2):.2f}s, "
                        f"avg={sum(all_m2)/len(all_m2):.3f}s\n")
            f.write(f"\nSettings:\n")
            f.write(f"  z3_timeout: {args.z3_timeout}s\n")
            f.write(f"  num_testcases: {args.num_testcases}\n")
            f.write(f"  tiers: {', '.join(tiers)}\n")
            if llm_config and llm_config.get('provider'):
                f.write(f"  llm_provider: {llm_config.get('provider')}\n")
            else:
                f.write(f"  llm: disabled\n")

        print(f"\nResults for split '{split}':")
        print(f"  Total: {len(problems)}")
        if skipped_count > 0:
            print(f"  Skipped (resume): {skipped_count}")
        print(f"  Success: {stats['success']}")
        print(f"  No constraints (LLM-only): {stats['no_constraint']}")
        print(f"  Failed: {stats['failed']}")
        print(f"  Rule-only: {stats['rule_only']} | SMT-used: {stats['smt_used']}")
        if llm_config and llm_config.get('provider'):
            print(f"  LLM refinement: {stats['llm_applied']}/{stats['llm_attempted']} applied")
        print(f"  Tier Statistics:")
        for t in tiers:
            ts = tier_stats[t]
            print(f"    {t}: {ts['problems']} problems, {ts['testcases']} testcases "
                  f"(boundary: {ts['boundary']}, algorithmic: {ts['algorithmic']})")
        if routing_stats:
            print(f"  Structure Routing:")
            for s_type, pattern_counts in sorted(routing_stats.items()):
                pattern_str = ', '.join(f'{p}({c})' for p, c in sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True))
                print(f"    {s_type} → {pattern_str}")
            print(f"    No structure (fallback): {no_structure_count}")
        split_elapsed = time.time() - split_start
        split_min = split_elapsed / 60.0
        print(f"  Timing: total={split_min:.2f} min ({split_elapsed:.1f}s)", end="")
        if all_m1:
            print(f" | M1: {len(all_m1)} calls, avg={sum(all_m1)/len(all_m1):.3f}s", end="")
        if all_m2:
            print(f" | M2: {len(all_m2)} calls, avg={sum(all_m2)/len(all_m2):.3f}s", end="")
        print()
        print(f"  Output: {output_json_path}")
        print(f"  Log: {log_file_path}")

        sys.stdout = _original_stdout
        _log_fh.close()
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()
        logging.getLogger().removeHandler(stderr_handler)
        stderr_handler.close()

if __name__ == '__main__':
    main()
