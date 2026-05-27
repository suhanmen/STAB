from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from ortools.sat.python import cp_model
    CPSAT_AVAILABLE = True
except ImportError:
    CPSAT_AVAILABLE = False

_SAFE_INT_LIMIT = 10**18

_WEIGHT_SAFE = 2**62

def _value_strategy_target(lo: int, hi: int, value_strategy: str,
                           pct_override: Optional[float]) -> int:
    if pct_override is not None:
        return lo + int((hi - lo) * pct_override)
    if value_strategy == 'min':
        return lo
    if value_strategy == 'p80':
        return lo + int((hi - lo) * 0.8)
    if value_strategy == 'mid':
        return lo + (hi - lo) // 2
    return hi

def _collect_bounds(constraints: List[Dict[str, Any]],
                    tcv: Optional[str]) -> Dict[str, Tuple[int, int]]:
    bounds: Dict[str, Tuple[int, int]] = {}

    def update(var: str, lo: int, hi: int) -> None:
        if var in bounds:
            o_lo, o_hi = bounds[var]
            bounds[var] = (max(o_lo, lo), min(o_hi, hi))
        else:
            bounds[var] = (lo, hi)

    for c in constraints:
        ctype = c.get('type')
        if ctype == 'range':
            lo = c.get('lo') if c.get('lo') is not None else 1
            hi = c.get('hi') if c.get('hi') is not None else 10**9
            update(c['var'], lo, hi)
        elif ctype == 'sum_tc':
            update(tcv or 't', 1, c['hi'])
            update(c['var'], 1, c['hi'])
        elif ctype in ('product', 'sum'):
            for v in c.get('vars', []):
                update(v, 1, c['hi'])
        elif ctype == 'chain':
            lo = c.get('lo') if c.get('lo') is not None else 1
            update(c['var'], lo, 10**18)
            if c.get('hi_var'):
                update(c['hi_var'], lo, 10**18)
        elif ctype == 'product_div':
            lo = c.get('lo', 0)
            update(c['var'], lo, 10**18)
            for v in c.get('product_vars', []):
                update(v, 1, 10**18)
        elif ctype == 'chain_div':
            lo = c.get('lo', 0)
            update(c['var'], lo, 10**18)
            if c.get('hi_var'):
                update(c['hi_var'], 1, 10**18)
    return bounds

def _sum_tc_data_vars(constraints: List[Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for c in constraints:
        if c.get('type') == 'sum_tc':
            out.add(c['var'])
        elif c.get('type') == 'product' and c.get('is_sum_tc') and len(c.get('vars', [])) == 2:
            out.add(c['vars'][1])
    return out

def _infer_tcv(constraints: List[Dict[str, Any]],
               explicit_tcv: Optional[str]) -> Optional[str]:
    if explicit_tcv:
        return explicit_tcv
    for c in constraints:
        if c.get('type') == 'product' and c.get('is_sum_tc') and len(c.get('vars', [])) == 2:
            return c['vars'][0]
    return None

def cpsat_solve(dependent_constraints: List[Dict[str, Any]],
                tcv: Optional[str],
                value_strategy: str = 'max',
                pct_override: Optional[float] = None,
                time_limit_sec: float = 5.0,
                lex_priority: str = 'data') -> Optional[Dict[str, int]]:
    if not CPSAT_AVAILABLE or not dependent_constraints:
        return None

    tcv = _infer_tcv(dependent_constraints, tcv)
    model = cp_model.CpModel()
    cv: Dict[str, cp_model.IntVar] = {}

    bounds = _collect_bounds(dependent_constraints, tcv)

    for v, (lo, hi) in bounds.items():
        lo = max(lo, -_SAFE_INT_LIMIT)
        hi = min(hi, _SAFE_INT_LIMIT)
        if lo > hi:
            lo = hi
        cv[v] = model.NewIntVar(lo, hi, v)

    for c in dependent_constraints:
        ctype = c.get('type')
        if ctype == 'sum_tc':
            v = c['var']
            t = tcv or 't'
            if t in cv and v in cv:
                cap = min(c['hi'], _SAFE_INT_LIMIT)
                prod = model.NewIntVar(0, cap, f'p_sumtc_{t}_{v}')
                model.AddMultiplicationEquality(prod, [cv[t], cv[v]])
        elif ctype == 'product':
            vs = c.get('vars', [])
            if len(vs) == 2 and all(v in cv for v in vs):
                cap = min(c['hi'], _SAFE_INT_LIMIT)
                prod = model.NewIntVar(0, cap, f'p_prod_{vs[0]}_{vs[1]}')
                model.AddMultiplicationEquality(prod, [cv[vs[0]], cv[vs[1]]])
        elif ctype == 'sum':
            vs = c.get('vars', [])
            if len(vs) == 2 and all(v in cv for v in vs):
                model.Add(cv[vs[0]] + cv[vs[1]] <= c['hi'])
        elif ctype == 'chain':
            v = c['var']
            hv = c.get('hi_var')
            if v in cv and hv in cv:
                if c.get('strict_upper'):
                    model.Add(cv[v] < cv[hv])
                else:
                    model.Add(cv[v] <= cv[hv])
        elif ctype == 'product_div':
            main = c['var']
            pvars = c.get('product_vars', [])
            div = c.get('div', 1)
            if main in cv and len(pvars) == 2 and all(v in cv for v in pvars):
                hi_a = bounds.get(pvars[0], (1, _SAFE_INT_LIMIT))[1]
                hi_b = bounds.get(pvars[1], (1, _SAFE_INT_LIMIT))[1]
                prod_hi = min(hi_a * hi_b, _SAFE_INT_LIMIT)
                prod = model.NewIntVar(0, prod_hi, f'pdv_{pvars[0]}_{pvars[1]}')
                model.AddMultiplicationEquality(prod, [cv[pvars[0]], cv[pvars[1]]])
                model.Add(cv[main] * div <= prod)
        elif ctype == 'chain_div':
            main = c['var']
            hv = c.get('hi_var')
            div = c.get('div', 1)
            if main in cv and hv in cv:
                model.Add(cv[main] * div <= cv[hv])

    if not cv:
        return {}

    max_bound = max(abs(b) for _, (lo, hi) in bounds.items() for b in (lo, hi))
    n_vars = max(1, len(cv))
    max_safe_w = max(1, _WEIGHT_SAFE // max(1, max_bound * n_vars * 2))
    sum_tc_data = _sum_tc_data_vars(dependent_constraints)

    if pct_override is not None or value_strategy in ('mid', 'p80'):
        chain_vars = {c['var'] for c in dependent_constraints if c.get('type') == 'chain'}
        for v, (lo, hi) in bounds.items():
            if v in chain_vars:
                continue
            tgt = _value_strategy_target(lo, hi, value_strategy, pct_override)
            if tgt > lo:
                model.Add(cv[v] >= tgt)
        terms = []
        for v in cv:
            if v in chain_vars:
                terms.append(min(10**6, max_safe_w) * cv[v])
            else:
                terms.append(-min(10**6, max_safe_w) * cv[v])
        if terms:
            model.Maximize(sum(terms))
    elif value_strategy == 'min':
        terms = [-cv[v] for v in cv]
        model.Maximize(sum(terms))
    else:
        terms = []
        for v in cv:
            if lex_priority == 'tcv':
                if v == tcv:
                    w = min(10**9, max_safe_w)
                elif v in sum_tc_data:
                    w = 1
                else:
                    w = min(10**6, max_safe_w)
            else:
                if v in sum_tc_data:
                    w = min(10**9, max_safe_w)
                elif v == tcv:
                    w = 1
                else:
                    w = min(10**6, max_safe_w)
            terms.append(w * cv[v])
        model.Maximize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    solver.parameters.log_search_progress = False
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return {v: solver.Value(cv[v]) for v in cv}

def rule_solve(dependent_constraints: List[Dict[str, Any]],
               tcv: Optional[str],
               value_strategy: str = 'max',
               pct_override: Optional[float] = None) -> Dict[str, int]:
    tcv = _infer_tcv(dependent_constraints, tcv)
    bounds: Dict[str, Tuple[int, int]] = {}
    chains: List[Dict[str, Any]] = []
    sum_tcs: List[Dict[str, Any]] = []
    products: List[Dict[str, Any]] = []
    product_divs: List[Dict[str, Any]] = []

    for c in dependent_constraints:
        ctype = c.get('type')
        if ctype == 'range':
            v = c['var']
            lo = c.get('lo') if c.get('lo') is not None else 1
            hi = c.get('hi') if c.get('hi') is not None else 10**18
            if v in bounds:
                o_lo, o_hi = bounds[v]
                bounds[v] = (max(o_lo, lo), min(o_hi, hi))
            else:
                bounds[v] = (lo, hi)
        elif ctype == 'chain':
            chains.append(c)
            if c['var'] not in bounds:
                bounds[c['var']] = (c.get('lo') or 1, 10**18)
            if c.get('hi_var') and c['hi_var'] not in bounds:
                bounds[c['hi_var']] = (1, 10**18)
        elif ctype == 'sum_tc':
            sum_tcs.append({'var': c['var'], 'hi': c['hi']})
        elif ctype == 'product':
            if c.get('is_sum_tc') and len(c.get('vars', [])) == 2:
                sum_tcs.append({'var': c['vars'][1], 'hi': c['hi']})
            else:
                products.append(c)
        elif ctype == 'product_div':
            product_divs.append(c)
            if c['var'] not in bounds:
                bounds[c['var']] = (c.get('lo', 0) or 0, 10**18)
            for v in c.get('product_vars', []):
                if v not in bounds:
                    bounds[v] = (1, 10**18)
        elif ctype == 'chain_div':
            product_divs.append({
                'type': 'product_div', 'var': c['var'],
                'product_vars': [c.get('hi_var'), '__div_1__'],
                'div': c.get('div', 1), 'lo': c.get('lo', 0),
                '_is_chain_div': True,
            })
            if c['var'] not in bounds:
                bounds[c['var']] = (c.get('lo', 0) or 0, 10**18)
            if c.get('hi_var') and c['hi_var'] not in bounds:
                bounds[c['hi_var']] = (1, 10**18)

    result = {v: hi for v, (lo, hi) in bounds.items()}

    for stc in sum_tcs:
        v = stc['var']
        t = tcv or 't'
        if t not in bounds:
            bounds[t] = (1, stc['hi'])
            result[t] = stc['hi']
        if v not in bounds:
            bounds[v] = (1, stc['hi'])
            result[v] = stc['hi']
        t_lo, t_hi = bounds[t]
        v_lo, v_hi = bounds[v]
        cap = stc['hi']
        v_star = min(v_hi, cap // max(t_lo, 1))
        t_star = min(t_hi, cap // max(v_star, 1))
        result[v] = v_star
        result[t] = t_star

    for p in products:
        vs = p.get('vars', [])
        if len(vs) != 2:
            continue
        v1, v2 = vs
        if v1 not in bounds:
            bounds[v1] = (1, p['hi'])
            result[v1] = p['hi']
        if v2 not in bounds:
            bounds[v2] = (1, p['hi'])
            result[v2] = p['hi']
        if result[v1] * result[v2] > p['hi']:
            v2_star = min(bounds[v2][1], p['hi'])
            v1_star = min(bounds[v1][1], p['hi'] // max(v2_star, 1))
            result[v1] = v1_star
            result[v2] = v2_star

    for _ in range(5):
        changed = False
        for c in chains:
            v = c['var']
            hv = c.get('hi_var')
            if hv in result and v in result:
                target = result[hv] - 1 if c.get('strict_upper') else result[hv]
                target = max(c.get('lo') or 1, target)
                if result[v] > target:
                    result[v] = target
                    changed = True
        if not changed:
            break

    for pd in product_divs:
        main = pd['var']
        pvars = pd.get('product_vars', [])
        div = pd.get('div', 1)
        if pd.get('_is_chain_div'):
            hv = pvars[0] if pvars else None
            if main in result and hv in result:
                cap = result[hv] // max(div, 1)
                if result[main] > cap:
                    result[main] = max(pd.get('lo', 0), cap)
        elif main in result and len(pvars) == 2 and all(v in result for v in pvars):
            cap = (result[pvars[0]] * result[pvars[1]]) // max(div, 1)
            if result[main] > cap:
                result[main] = max(pd.get('lo', 0), cap)

    for stc in sum_tcs:
        v = stc['var']
        t = tcv or 't'
        if t in result and v in result and result[t] * result[v] > stc['hi']:
            v_hi = bounds.get(v, (1, stc['hi']))[1]
            t_hi = bounds.get(t, (1, stc['hi']))[1]
            v_star = min(v_hi, stc['hi'])
            t_star = min(t_hi, stc['hi'] // max(v_star, 1))
            result[t] = t_star
            result[v] = v_star
    for p in products:
        vs = p.get('vars', [])
        if len(vs) == 2 and all(v in result for v in vs):
            if result[vs[0]] * result[vs[1]] > p['hi']:
                v2_star = min(bounds[vs[1]][1], p['hi'])
                v1_star = min(bounds[vs[0]][1], p['hi'] // max(v2_star, 1))
                result[vs[0]] = v1_star
                result[vs[1]] = v2_star

    if value_strategy != 'max' or pct_override is not None:
        for v, val in list(result.items()):
            if v not in bounds:
                continue
            lo, hi = bounds[v]
            target = _value_strategy_target(lo, hi, value_strategy, pct_override)
            if value_strategy == 'min':
                result[v] = max(lo, target)
            elif value_strategy in ('mid', 'p80') or pct_override is not None:
                result[v] = max(lo, min(val, target))

    return result

def validate(constraints: List[Dict[str, Any]],
             result: Dict[str, int],
             tcv: Optional[str] = None) -> bool:
    if result is None:
        return False
    tcv = _infer_tcv(constraints, tcv) or 't'
    for c in constraints:
        ctype = c.get('type')
        if ctype == 'range':
            v = c['var']
            if v not in result:
                continue
            val = result[v]
            if c.get('lo') is not None and val < c['lo']:
                return False
            if c.get('hi') is not None and val > c['hi']:
                return False
        elif ctype == 'sum_tc':
            t_val = result.get(tcv, 1)
            v_val = result.get(c['var'])
            if v_val is not None and t_val * v_val > c['hi']:
                return False
        elif ctype == 'product':
            vs = c.get('vars', [])
            prod = 1
            for v in vs:
                if v not in result:
                    prod = None
                    break
                prod *= result[v]
            if prod is not None and prod > c['hi']:
                return False
        elif ctype == 'sum':
            vs = c.get('vars', [])
            if len(vs) == 2 and all(v in result for v in vs):
                if result[vs[0]] + result[vs[1]] > c['hi']:
                    return False
        elif ctype == 'chain':
            v = c['var']
            hv = c.get('hi_var')
            if v in result and hv in result:
                if c.get('strict_upper') and result[v] >= result[hv]:
                    return False
                if not c.get('strict_upper') and result[v] > result[hv]:
                    return False
        elif ctype == 'product_div':
            main = c['var']
            pvars = c.get('product_vars', [])
            div = c.get('div', 1)
            if main in result and len(pvars) == 2 and all(v in result for v in pvars):
                if result[main] * div > result[pvars[0]] * result[pvars[1]]:
                    return False
        elif ctype == 'chain_div':
            main = c['var']
            hv = c.get('hi_var')
            div = c.get('div', 1)
            if main in result and hv in result:
                if result[main] * div > result[hv]:
                    return False
    return True

def solve(dependent_constraints: List[Dict[str, Any]],
          known_values: Dict[str, int],
          tcv: Optional[str] = None,
          value_strategy: str = 'max',
          pct_override: Optional[float] = None,
          time_limit_sec: float = 5.0,
          lex_priority: str = 'data') -> Dict[str, int]:
    if not dependent_constraints:
        return {}

    cpsat_result = cpsat_solve(
        dependent_constraints, tcv,
        value_strategy=value_strategy, pct_override=pct_override,
        time_limit_sec=time_limit_sec,
        lex_priority=lex_priority,
    )
    if cpsat_result is not None and validate(dependent_constraints, cpsat_result, tcv):
        return cpsat_result

    return rule_solve(dependent_constraints, tcv,
                      value_strategy=value_strategy, pct_override=pct_override)
