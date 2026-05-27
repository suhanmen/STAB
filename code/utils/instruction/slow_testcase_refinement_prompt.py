Slow_testcase_refinement_prompt_v9 = """You are a competitive programming **test case generator**.
Your task is to write **{num_testcases} distinct Python generator functions** for the **{tier}** difficulty tier.
Each generator function takes no arguments and returns a single stdin string that will be fed to a contestant's solution as input.

{tier_instruction}

Each of your {num_testcases} generators MUST use a **different strategy**.

## Problem Description
{problem_desc}

## Input Format
{input_desc}

## Time Limit
Time limit: **{timelimit} seconds**.
{tier_time_guidance}

## Input Constraints (partial reference)
{constraints_summary}

**Important**: This list captures only mechanically-parsed numeric/variable bounds. The problem description above may contain additional constraints not enumerated here, such as:
- structural / graph properties ("the given edges form a tree", "graph is connected", "no self-loops or duplicate edges", "the given input forms a binary tree")
- distinctness / ordering ("pairwise distinct", "permutation of 1..n", "no two elements are the same")
- semantic numeric conditions ("k is prime", "n is even", "x and y are coprime")
- geometric conditions ("no three points are collinear", "in general position")
- guarantees ("the answer always exists", "it is guaranteed that ...")

Always re-read the problem description and respect ANY constraint stated there, even if it isn't enumerated in the list above. The list is a hint, not the full spec.

Each generator must keep its output within the constraint ranges so the input parses correctly:
- Variable ranges (e.g. `1 ≤ N ≤ 10^5`): pick N within [1, 100000].
- Array length: output exactly the declared count of elements.
- Graph: respect any stated structure (connected / tree with N-1 edges / no duplicate edges / no self-loops) when applicable.
- String: produce the declared length using the declared alphabet.
- Multi-test-case format: if the first line is T, follow it with exactly T test cases.
- Cross-variable bounds (e.g. `1 ≤ a_i ≤ N`): keep array values within the corresponding scalar bound.

{m1_boundary_block}

## Adversarial Input Guidance
{routing_section}

## Diversity Requirement
Write **{num_testcases} distinct generator functions**, each using a **DIFFERENT adversarial strategy**.
Do NOT repeat the same data pattern, structure, or approach across generators.

## Output: JSON with {num_testcases} Python Generator Functions

Each generator must be a **self-contained Python function** `def generate() -> str:` that returns the test input as a single string (the exact content to be fed via stdin).

### Generator Requirements
1. Define a function `def generate() -> str:` with NO arguments.
2. Hardcode all scalar values (N, M, etc.) from the M1 boundary values above.
3. The function must return the **complete stdin string** — every line separated by `\\n`.
4. Only use Python standard library (`random`, `math`, `itertools`, `string`, `sys`, etc.).
5. If using `random`, always set a fixed seed (`random.seed(42)`) for reproducibility.
6. Stay within the constraint ranges so the input parses; otherwise feel free to push toward the most adversarial valid configuration.
7. Add a brief comment at the top of the function describing the strategy used.

### Tier-Specific Requirement
{tier_requirement}

### Output Format
Return ONLY a valid JSON object — no explanation, no markdown fences.
Each key is `"tc_1"`, `"tc_2"`, ..., `"tc_{num_testcases}"`, and each value is the Python code string for that generator's `generate()` function.

{{
  "tc_1": "def generate() -> str:\\n    # Strategy: <describe strategy>\\n    ...\\n    return '\\\\n'.join(lines)",
  "tc_2": "def generate() -> str:\\n    # Strategy: <different strategy>\\n    ...\\n    return '\\\\n'.join(lines)",
  "tc_3": "...(same format, each generator with a unique strategy)...",
  "tc_{num_testcases}": "def generate() -> str:\\n    # Strategy: <yet another strategy>\\n    ...\\n    return '\\\\n'.join(lines)"
}}"""

TIER_BLOCKS_V9 = {
    "fast": {
        "tier_instruction": (
            "**Fast tier**: each generator should produce input that correct solutions solve QUICKLY.\n"
            "Use SIMPLE, TRIVIAL data patterns — uniform values, sorted arrays, star graphs,\n"
            "small distinct values, or random data with no adversarial structure.\n"
            "The goal is test cases that finish well under the time limit."
        ),
        "tier_time_guidance": (
            "Each generator should produce input that makes the solution finish QUICKLY — "
            "aim for execution well under **{timelimit_fast_target:.2f} seconds** (1/3 of the time limit)."
        ),
        "tier_requirement": (
            "Focus on SIMPLE patterns that minimize execution time — "
            "avoid adversarial structures, degenerate cases, or worst-case patterns."
        ),
        "tier_examples": (
            "Use straightforward data that solutions handle efficiently:\n\n"
            "- **Sorted array**: `list(range(1, n+1))` — best case for many algorithms\n"
            "- **Uniform values**: `[1] * n` — trivial to process\n"
            "- **Star graph**: all nodes connected to node 1 — minimal depth\n"
            "- **Random uniform**: `random.randint(lo, hi)` — average-case behavior\n"
            "- **Short strings**: simple repeated characters like `'a' * length`"
        ),
    },
    "medium": {
        "tier_instruction": (
            "**Medium tier**: each generator should produce input that causes MODERATE execution time.\n"
            "The data should have SOME adversarial properties but NOT be fully worst-case.\n"
            "Introduce partial irregularity — not clean/trivial, but not maximally adversarial.\n\n"
            "## What NOT to do (too fast)\n"
            "- Fully sorted or uniform arrays (`[1]*n`, `range(1,n+1)`)\n"
            "- Star graphs (all nodes to node 1)\n"
            "- Repeated single characters\n\n"
            "## What NOT to do (too slow)\n"
            "- Fully reversed/anti-sorted arrays designed to break quicksort\n"
            "- Chain/bamboo graphs (`i→i+1`) that maximize tree depth to N\n"
            "- Hash collision strings or worst-case string matching patterns\n"
            "- Any pattern explicitly designed to hit O(N^2) or worse"
        ),
        "tier_time_guidance": (
            "Each generator should produce input that causes MODERATE execution time — "
            "roughly between **{timelimit_medium_lo:.2f}** and **{timelimit_medium_hi:.2f} seconds**. "
            "The data should exercise the algorithm but NOT trigger its worst case."
        ),
        "tier_requirement": (
            "Produce data with PARTIAL irregularity — "
            "not trivial (no sorted/uniform data) and not worst-case (no anti-sort, no chain graphs, no hash collisions). "
            "Aim for average-case or mildly adversarial behavior."
        ),
        "tier_examples": (
            "Use data with partial irregularity:\n\n"
            "- **Mostly sorted + swaps**: sort the array, then swap ~20% of random pairs\n"
            "- **Random tree**: `parent[i] = random.randint(1, i-1)` — average depth O(log N)\n"
            "- **Random permutation**: `random.shuffle(list(range(1, n+1)))` — average-case\n"
            "- **Clustered values**: a few distinct value groups with random noise\n"
            "- **Random graph**: random edges with moderate density (~2*N edges)\n"
            "- **Mixed string**: random characters from a small alphabet (3-5 chars), not periodic patterns"
        ),
    },
    "slow": {
        "tier_instruction": (
            "**Slow tier**: each generator should produce input that **maximizes execution time** — forcing\n"
            "suboptimal solutions to exceed the time limit while correct solutions still pass.\n"
            "Refine the DATA PATTERNS to trigger worst-case algorithmic behavior\n"
            "(e.g., O(N^2) pivots, degenerate trees, hash collisions, cache-hostile access).\n\n"
            "**IMPORTANT**: While all constraints must be satisfied, your PRIMARY goal is to\n"
            "maximize execution time. Do NOT play it safe — push data patterns to the most\n"
            "adversarial extreme that is still valid. A slow test case that barely satisfies\n"
            "constraints is far better than a safe test case that runs fast."
        ),
        "tier_time_guidance": (
            "Each generator should produce input that makes the solution take as long as possible — "
            "aim for execution longer than **{timelimit_slow_target:.1f} seconds**."
        ),
        "tier_requirement": (
            "Focus on adversarial patterns that maximize time complexity. "
            "Be aggressive: use the most extreme valid patterns, not safe defaults."
        ),
        "tier_examples": (
            "Use Python's full expressiveness to craft adversarial inputs:\n\n"
            "- **Anti-quicksort**: `sorted(range(n))` or median-of-three killer sequences\n"
            "- **Degenerate tree**: `edges = [(i, i+1) for i in range(1, n)]` (bamboo/chain)\n"
            "- **Hash collision**: strings with known collision patterns for polynomial rolling hashes\n"
            "- **Cache-hostile**: access patterns that thrash CPU cache lines\n"
            "- **Anti-sort**: nearly-sorted with strategic swaps\n"
            "- **Worst-case string matching**: periodic patterns like `\"a\" * (n-1) + \"b\"`"
        ),
    },
}
