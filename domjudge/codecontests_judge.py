#!/usr/bin/env python3

import argparse
import dataclasses
import gc
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import mysql.connector
import requests
from datasets import load_dataset
from tqdm import tqdm

LANG_CODE_MAP = {
    0: ("Unknown", None),
    1: ("Python 2", None),
    2: ("C++", "cpp"),
    3: ("Python 3", "python3"),
    4: ("Java", "java"),
}
_LANG_ID_TO_NAME = {lid: name for _, (name, lid) in LANG_CODE_MAP.items() if lid is not None}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

class DOMjudgeClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (username, password)
        self._team_password = None

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v4/{path.lstrip('/')}"

    def get(self, path: str, **kwargs):
        r = self.session.get(self._url(path), **kwargs)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, **kwargs):
        r = self.session.post(self._url(path), **kwargs)
        r.raise_for_status()
        return r.json()

    def post_raw(self, path: str, **kwargs) -> requests.Response:
        r = self.session.post(self._url(path), **kwargs)
        r.raise_for_status()
        return r

    def ensure_team_for_user(self, username: str, contest_id: str) -> str:
        users = self.get("users", params={"username": username})
        user = next((u for u in users if u["username"] == username), None)
        if user is None:
            raise RuntimeError(f"User '{username}' not found in DOMjudge")

        if user.get("team_id"):
            log.info("User '%s' already linked to team_id=%s", username, user["team_id"])
            return user["team_id"]

        team_name = f"team_{username}"
        team_id = f"team_{username}"
        log.info("Creating team '%s' for user '%s' ...", team_name, username)
        try:
            team = self.post("teams", json={
                "id": team_id,
                "name": team_name,
                "group_ids": ["participants"],
            })
            team_id = team["id"]
        except requests.HTTPError as e:
            if "already exists" in str(e.response.text if hasattr(e, 'response') else e):
                log.info("Team '%s' already exists, reusing.", team_id)
            else:
                raise

        user_id = user["id"]
        user_roles = user.get("roles", ["team"])
        try:
            del_url = self._url(f"users/{user_id}")
            r = self.session.delete(del_url)
            r.raise_for_status()
            log.info("Deleted user '%s', re-creating with team_id=%s ...", username, team_id)
            self.post("users", json={
                "id": user_id,
                "username": username,
                "password": self._team_password,
                "name": username,
                "team_id": team_id,
                "roles": user_roles,
            })
            log.info("Re-created user '%s' -> team_id=%s", username, team_id)
        except (requests.HTTPError, AttributeError):
            log.warning(
                "Could not auto-link user '%s' to team '%s'. "
                "Please link manually in the DOMjudge admin UI: "
                "Jury -> Users -> %s -> Team -> %s",
                username, team_id, username, team_id,
            )
        return team_id

    def upload_problem(self, contest_id: str, problem_zip: bytes, problem_id: str,
                        max_retries: int = 4) -> dict:
        url = self._url(f"contests/{contest_id}/problems")

        def _is_transient(exc: Exception) -> bool:
            s = str(exc).lower()
            return any(k in s for k in (
                "502", "503", "504", "gateway", "timed out", "timeout",
                "connection reset", "connection aborted",
                "service unavailable", "too many",
            ))

        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                r = self.session.post(
                    url,
                    files={"zip": (f"{problem_id}.zip", problem_zip, "application/zip")},
                )
                if r.status_code == 400 and "already used" in r.text:
                    log.info("  Problem '%s' already exists, updating ...", problem_id)
                    r = self.session.post(
                        url,
                        files={"zip": (f"{problem_id}.zip", problem_zip, "application/zip")},
                        data={"problem": problem_id},
                    )
                r.raise_for_status()
                return (r.json() if r.headers.get("content-type", "").startswith("application/json")
                        else {"problem_id": problem_id})
            except Exception as e:
                if not _is_transient(e) or attempt >= max_retries:
                    raise
                wait = 2 * (2 ** attempt)
                log.warning(
                    "  Upload transient error '%s' for %s (attempt %d/%d) — retry in %ds",
                    type(e).__name__, problem_id, attempt + 1, max_retries + 1, wait)
                time.sleep(wait)
                last_exc = e
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("upload_problem: unreachable retry loop end")

    def submit(self, contest_id: str, problem_id: str, language_id: str,
               source_code: str, filename: str) -> dict:
        return self.post_raw(
            f"contests/{contest_id}/submissions",
            files={"code[]": (filename, source_code.encode(), "text/plain")},
            data={"problem_id": problem_id, "language_id": language_id},
        ).json()

    def wait_judgement(self, contest_id: str, submission_id: str,
                       poll_interval: float = 2, poll_timeout: float = 120,
                       max_http_retries: int = 5) -> dict | None:
        deadline = time.monotonic() + poll_timeout
        consecutive_errors = 0
        while time.monotonic() < deadline:
            try:
                judgements = self.get(
                    f"contests/{contest_id}/judgements",
                    params={"submission_id": submission_id},
                )
                consecutive_errors = 0
            except (requests.HTTPError, requests.ConnectionError) as e:
                consecutive_errors += 1
                if consecutive_errors > max_http_retries:
                    log.warning("Judgement polling failed %d times for sid=%s, giving up: %s",
                                consecutive_errors, submission_id, e)
                    return None
                backoff = min(poll_interval * (2 ** (consecutive_errors - 1)), 30)
                log.info("Judgement poll retry %d/%d for sid=%s (backoff %.1fs): %s",
                         consecutive_errors, max_http_retries, submission_id, backoff, e)
                time.sleep(backoff)
                continue

            for j in judgements:
                if j.get("judgement_type_id") is not None:
                    return j
            time.sleep(poll_interval)
        return None

class AdaptiveThrottle:

    def __init__(self, max_pending: int, base_delay: float = 2.0):
        self._lock = threading.Lock()
        self._pending = 0
        self._max_pending = max_pending
        self._base_delay = base_delay

    def acquire(self, worker_tag: str = ""):
        while True:
            with self._lock:
                if self._pending < self._max_pending:
                    self._pending += 1
                    return
                current = self._pending
            delay = self._base_delay * min(current - self._max_pending + 1, 5)
            log.info("%s  Adaptive backoff: %d pending (max %d), waiting %.1fs",
                     worker_tag, current, self._max_pending, delay)
            time.sleep(delay)

    def release(self):
        with self._lock:
            self._pending = max(0, self._pending - 1)

def _wait_with_retry(admin_client, contest_id, submission_id, args, worker_tag=""):
    judgement = admin_client.wait_judgement(
        contest_id, submission_id,
        poll_interval=args.poll_interval,
        poll_timeout=args.poll_timeout,
    )
    if judgement is not None:
        return judgement

    max_retries = getattr(args, 'max_judge_retries', 1)
    for attempt in range(max_retries):
        log.warning("%s  Judgement timeout for sid=%s, retry %d/%d...",
                    worker_tag, submission_id, attempt + 1, max_retries)
        time.sleep(10)
        judgement = admin_client.wait_judgement(
            contest_id, submission_id,
            poll_interval=args.poll_interval,
            poll_timeout=args.poll_timeout,
        )
        if judgement is not None:
            log.info("%s  Judgement received on retry %d for sid=%s",
                     worker_tag, attempt + 1, submission_id)
            return judgement
    return None

def get_runs_from_db(db_config: dict, submit_id: str) -> list[dict]:
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT tc.ranknumber AS ordinal, jr.runresult AS verdict, jr.runtime AS run_time "
            "FROM judging_run jr "
            "JOIN judging j ON jr.judgingid = j.judgingid "
            "JOIN testcase tc ON jr.testcaseid = tc.testcaseid "
            "WHERE j.submitid = %s ORDER BY tc.ranknumber",
            (submit_id,)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [{"testcase": r["ordinal"], "verdict": r["verdict"] or "", "run_time": float(r["run_time"]) if r["run_time"] is not None else None} for r in rows]
    except Exception as e:
        log.warning("  DB query failed for submitid=%s: %s", submit_id, e)
        return []

def delete_problem_from_db(db_config: dict, problem_id: str) -> bool:
    if not db_config:
        raise ValueError("db_config is required for delete_problem_from_db (needed to prevent TC accumulation)")
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM problem WHERE externalid = %s", (problem_id,))
        deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        if deleted:
            log.info("  DB: deleted problem '%s' (cascade: testcases, submissions, etc.)", problem_id)
        return deleted > 0
    except Exception as e:
        log.warning("  DB: could not delete problem '%s': %s", problem_id, e)
        return False

def build_problem_zip(problem_id: str, problem_name: str, timelimit: int,
                      public_tests: dict, private_tests: dict,
                      generated_tests: dict,
                      memory_limit_bytes: int = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        ini = (
            f"short-name = {problem_id}\n"
            f"name = {problem_name}\n"
            f"timelimit = {timelimit}\n"
        )
        if memory_limit_bytes:
            ini += f"memory-limit = {memory_limit_bytes // 1024}\n"
        zf.writestr("domjudge-problem.ini", ini)

        for i, (inp, out) in enumerate(
            zip(public_tests.get("input", []), public_tests.get("output", [])), start=1
        ):
            zf.writestr(f"data/sample/{i}.in", inp)
            zf.writestr(f"data/sample/{i}.ans", out)

        secret_idx = 1
        for tests in (private_tests, generated_tests):
            for inp, out in zip(tests.get("input", []), tests.get("output", [])):
                zf.writestr(f"data/secret/{secret_idx}.in", inp)
                zf.writestr(f"data/secret/{secret_idx}.ans", out)
                secret_idx += 1

    return buf.getvalue()

def _is_abbreviated_input(inp: str) -> bool:
    if not inp or not inp.strip():
        return True
    if re.search(r'(?:^|\s)\.\.\.(?:\s|$)', inp, re.MULTILINE):
        return True
    if re.search(r'\(\d+\s+(?:elements|values|characters|numbers|pairs|lines|times|nodes|edges)\)', inp):
        return True
    if re.search(r'\b(?:followed by|alternating|increasing|decreasing|pattern|each string)\b', inp, re.IGNORECASE):
        return True
    return False

def _is_abbreviated_output(out: str) -> bool:
    return bool(out) and bool(re.search(r'(?:^|\s)\.\.\.(?:\s|$)', out, re.MULTILINE))

def _run_solution_on_inputs(
    lang_id: str,
    source: str,
    inputs: list[str],
    timelimit: int = 10,
    skip_mask: list[bool] = None,
    memory_limit_bytes: int = None,
) -> list[str]:
    outputs: list[str] = []
    san_available = False
    tmpdir = tempfile.mkdtemp(prefix="judge_gold_")

    preexec_fn = None
    if memory_limit_bytes and lang_id == "cpp":
        def _set_rlimit(lim=memory_limit_bytes):
            resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        preexec_fn = _set_rlimit

    try:
        if lang_id == "cpp":
            src_path = os.path.join(tmpdir, "gold.cpp")
            bin_path = os.path.join(tmpdir, "gold")
            san_bin_path = os.path.join(tmpdir, "gold_san")
            with open(src_path, "w") as f:
                f.write(source)
            comp = subprocess.run(
                ["g++", "-O2", "-std=c++17", "-o", bin_path, src_path],
                capture_output=True, timeout=30,
            )
            if comp.returncode != 0:
                log.warning("  Compile failed (%s): %s", lang_id, comp.stderr.decode()[:200])
                return [""] * len(inputs)
            run_cmd = [bin_path]
            san_comp = subprocess.run(
                ["g++", "-O2", "-std=c++17",
                 "-fsanitize=undefined,address", "-fno-sanitize-recover=all",
                 "-o", san_bin_path, src_path],
                capture_output=True, timeout=30,
            )
            san_available = (san_comp.returncode == 0)
            if not san_available:
                log.debug("  UBSan compile failed (non-fatal): %s", san_comp.stderr.decode()[:200])

        elif lang_id == "python3":
            src_path = os.path.join(tmpdir, "gold.py")
            with open(src_path, "w") as f:
                f.write(source)
            run_cmd = [sys.executable, src_path]

        elif lang_id == "java":
            java_src = re.sub(r'\bpublic\s+class\s+\w+', 'public class Main', source, count=1)
            src_path = os.path.join(tmpdir, "Main.java")
            with open(src_path, "w") as f:
                f.write(java_src)
            comp = subprocess.run(
                ["javac", src_path],
                capture_output=True, timeout=30,
            )
            if comp.returncode != 0:
                log.warning("  Compile failed (%s): %s", lang_id, comp.stderr.decode()[:200])
                return [""] * len(inputs)
            mem_mb = memory_limit_bytes // (1024 * 1024) if memory_limit_bytes else None
            run_cmd = ["java", f"-Xmx{mem_mb}m", "-cp", tmpdir, "Main"] if mem_mb else ["java", "-cp", tmpdir, "Main"]
        else:
            return [""] * len(inputs)

        for i, inp in enumerate(inputs):
            if skip_mask and skip_mask[i]:
                outputs.append("")
                continue
            try:
                proc = subprocess.run(
                    run_cmd, input=inp, capture_output=True, text=True,
                    timeout=timelimit + 5,
                    preexec_fn=preexec_fn,
                )
                if proc.returncode != 0:
                    log.debug("  Runtime error on TC %d (%s): %s", i, lang_id, proc.stderr[:100])
                    outputs.append("")
                    continue

                if lang_id == "cpp" and san_available:
                    try:
                        san_proc = subprocess.run(
                            [san_bin_path], input=inp, capture_output=True, text=True,
                            timeout=timelimit + 10,
                        )
                        if san_proc.returncode != 0 or san_proc.stderr:
                            log.warning("  UBSan detected issue on TC %d: %s",
                                        i, san_proc.stderr[:300] if san_proc.stderr else f"exit={san_proc.returncode}")
                            outputs.append("")
                            continue
                    except subprocess.TimeoutExpired:
                        log.debug("  UBSan TLE on TC %d (non-fatal, keeping output)", i)
                    except Exception as e:
                        log.debug("  UBSan exec error on TC %d (non-fatal): %s", i, e)

                outputs.append(proc.stdout)
            except subprocess.TimeoutExpired:
                log.debug("  TLE on TC %d (%s)", i, lang_id)
                outputs.append("")
            except Exception as e:
                log.debug("  Exec error on TC %d (%s): %s", i, lang_id, e)
                outputs.append("")

    except Exception as e:
        log.warning("  Solution setup failed (%s): %s", lang_id, e)
        return [""] * len(inputs)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return outputs

def generate_expected_outputs_per_lang(
    inputs: list[str],
    example: dict,
    timelimit: int,
    sel_problem: dict = None,
    skip_mask: list[bool] = None,
    stream_queue=None,
    solution_strategy: str = "fast_solution",
) -> dict[str, tuple[list[str], int, str]]:
    solutions_data = example.get("solutions", {"language": [], "solution": []})
    sol_texts = solutions_data.get("solution", [])

    result: dict[str, tuple[list[str], int, str]] = {}

    def _avg_rt(s):
        runs = s.get("runs", [])
        if not runs:
            return float("inf")
        return sum(
            r["run_time"] if r.get("run_time") is not None else float("inf")
            for r in runs
        ) / len(runs)

    def _select_by_strategy_eo(ac_solutions, strategy, seed=42, problem_idx=0):
        if not ac_solutions:
            return None
        sorted_sols = sorted(ac_solutions, key=_avg_rt)
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

    if sel_problem:
        lang_items = [(lid, sl) for lid, sl in sel_problem.get("solutions", {}).items()]
        for lang_id, sol_list in lang_items:
            ac_solutions = [s for s in sol_list if s.get("verdict") == "AC"]
            if not ac_solutions:
                continue
            _prob_idx = sel_problem.get("index", 0) if sel_problem else 0
            best = _select_by_strategy_eo(ac_solutions, solution_strategy, problem_idx=_prob_idx)
            if best is None:
                continue
            sol_idx = best["sol_idx"]
            if sol_idx >= len(sol_texts):
                continue
            source = sol_texts[sol_idx]
            memory_limit_bytes = example.get("memory_limit_bytes")
            outputs = _run_solution_on_inputs(lang_id, source, inputs, timelimit, skip_mask, memory_limit_bytes)
            n_ok = sum(1 for o in outputs if o)
            if n_ok > 0:
                log.info("    %s: generated %d/%d expected outputs (sol_idx=%d)",
                         lang_id, n_ok, len(inputs), sol_idx)
            else:
                log.warning("    %s: generated 0/%d expected outputs (sol_idx=%d) — all executions failed",
                            lang_id, len(inputs), sol_idx)
            result[lang_id] = (outputs, sol_idx, source)
            if stream_queue is not None:
                stream_queue.put({lang_id: (outputs, sol_idx, source)})
    else:
        log.warning("generate_expected_outputs_per_lang: no sel_problem provided — "
                    "skipping expected output generation (no verified AC solutions available)")

    if stream_queue is not None:
        stream_queue.put(None)
    return result

def generate_expected_outputs(
    inputs: list[str],
    example: dict,
    timelimit: int = 10,
    sel_problem: dict = None,
) -> list[str]:
    per_lang = generate_expected_outputs_per_lang(inputs, example, timelimit, sel_problem)
    for lang_id in ("cpp", "python3", "java"):
        if lang_id in per_lang:
            outputs, _sol_idx, _source = per_lang[lang_id]
            return outputs
    return [""] * len(inputs)

EXT_MAP = {"cpp": "cpp", "python3": "py", "java": "java", "c": "c"}

def make_filename(lang_id: str, idx: int) -> str:
    ext = EXT_MAP.get(lang_id, "txt")
    if lang_id == "java":
        return "Main.java"
    return f"solution_{idx}.{ext}"

def detect_float_tolerance(description: str) -> float | None:
    match = re.search(r'10\s*\^\s*\{?\s*-\s*(\d+)\s*\}?', description)
    if match and re.search(r'(?:error|precision|tolerance|absolute|relative)', description, re.IGNORECASE):
        exp = int(match.group(1))
        return 10 ** (-exp)
    return None

def set_float_tolerance_in_db(db_config: dict, problem_id: str, tolerance: float) -> bool:
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE problem SET special_compare = 'compare', "
            "special_compare_args = %s WHERE externalid = %s",
            (f"float_tolerance {tolerance:.0e}", problem_id),
        )
        updated = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        if updated:
            log.info("  DB: set float_tolerance=%.0e on problem '%s'", tolerance, problem_id)
        return updated > 0
    except Exception as e:
        log.warning("  DB: could not set float_tolerance on '%s': %s", problem_id, e)
        return False

def sanitize_short_name(name: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return s[:max_len]

def sample_test_cases(tests: dict, ratio: float = 0.1, largest_ratio: float = 0.5,
                      min_threshold: int = 20) -> dict:
    inputs = tests.get("input", [])
    outputs = tests.get("output", [])
    n = len(inputs)

    if n <= min_threshold:
        return {
            "input": list(inputs),
            "output": list(outputs),
            "original_count": n,
            "sampled_count": n,
        }

    n_sample = max(min_threshold, math.ceil(n * ratio))
    n_largest = math.ceil(n_sample * largest_ratio)
    n_random = n_sample - n_largest

    indexed = sorted(range(n), key=lambda i: len(inputs[i]), reverse=True)
    largest_indices = set(indexed[:n_largest])

    remaining = [i for i in range(n) if i not in largest_indices]
    if n_random > 0 and remaining:
        random_indices = set(random.sample(remaining, min(n_random, len(remaining))))
    else:
        random_indices = set()

    selected = sorted(largest_indices | random_indices)
    return {
        "input": [inputs[i] for i in selected],
        "output": [outputs[i] for i in selected],
        "original_count": n,
        "sampled_count": len(selected),
    }

def sample_all_test_cases(example: dict, ratio: float = 0.1,
                          largest_ratio: float = 0.5) -> tuple[dict, dict, dict, dict]:
    public = sample_test_cases(
        example.get("public_tests", {"input": [], "output": []}),
        ratio=ratio, largest_ratio=largest_ratio,
    )
    private = sample_test_cases(
        example.get("private_tests", {"input": [], "output": []}),
        ratio=ratio, largest_ratio=largest_ratio,
    )
    generated = sample_test_cases(
        example.get("generated_tests", {"input": [], "output": []}),
        ratio=ratio, largest_ratio=largest_ratio,
    )

    sampling_info = {
        "public": {"original": public["original_count"], "sampled": public["sampled_count"]},
        "private": {"original": private["original_count"], "sampled": private["sampled_count"]},
        "generated": {"original": generated["original_count"], "sampled": generated["sampled_count"]},
    }

    return (
        {"input": public["input"], "output": public["output"]},
        {"input": private["input"], "output": private["output"]},
        {"input": generated["input"], "output": generated["output"]},
        sampling_info,
    )

def _load_process_split_jsonl(path: Path) -> dict:
    metadata = None
    problems = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            first_char = f.read(1)
            f.seek(0)
            if first_char == '{':
                first_line = f.readline().strip()
                try:
                    first_obj = json.loads(first_line)
                except json.JSONDecodeError:
                    f.seek(0)
                    data = json.load(f)
                    return data

                if first_obj.get("type") == "metadata":
                    metadata = {k: v for k, v in first_obj.items() if k != "type"}
                elif "problems" in first_obj:
                    return first_obj
                else:
                    first_obj.pop("type", None)
                    problems.append(first_obj)

                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    if record.get("type") == "metadata":
                        metadata = {k: v for k, v in record.items() if k != "type"}
                    else:
                        record.pop("type", None)
                        problems.append(record)
            elif first_char == '[':
                f.seek(0)
                data = json.load(f)
                return {"metadata": {}, "problems": data}
    except json.JSONDecodeError:
        return {"metadata": {}, "problems": []}

    if metadata is None:
        metadata = {}
    return {"metadata": metadata, "problems": problems}

def _load_selected_solutions_jsonl(path: Path) -> dict:
    metadata = None
    problems = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("type") == "metadata":
                metadata = {k: v for k, v in record.items() if k != "type"}
            else:
                record.pop("type", None)
                problems.append(record)
    if metadata is None:
        metadata = {"split": ""}
    total_ac = sum(
        1 for p in problems
        for sols in p.get("solutions", {}).values()
        for s in sols if s.get("verdict") == "AC"
    )
    metadata["total_ac"] = total_ac
    return {"metadata": metadata, "problems": problems}

def build_selected_solutions(admin_client: DOMjudgeClient, team_client: DOMjudgeClient,
                              ds_split, split_name: str, args,
                              db_config: dict = None, out_path: Path = None,
                              problem_indices: set = None,
                              contest_id_override: str = None,
                              worker_tag: str = "",
                              throttle: AdaptiveThrottle = None) -> dict:
    W = worker_tag
    contest_id = contest_id_override or args.contest_id

    done_indices = set()

    if out_path and out_path.exists() and not args.reset:
        try:
            cached = _load_selected_solutions_jsonl(out_path)
            done_indices = {p["index"] for p in cached.get("problems", [])}
            if done_indices:
                log.info("%sResuming selected_solutions from %s (%d problems done, %d AC)",
                         W, out_path, len(done_indices), cached["metadata"].get("total_ac", 0))
        except Exception as e:
            log.warning("%sCould not load cached selected_solutions (%s), rebuilding", W, e)
            done_indices = set()

    if not done_indices and not args.reset and out_path:
        merged_sel_path = out_path.parent / f"selected_solutions_{split_name}.jsonl"
        if merged_sel_path != out_path and merged_sel_path.exists():
            try:
                merged_sel = _load_selected_solutions_jsonl(merged_sel_path)
                done_indices = {p["index"] for p in merged_sel.get("problems", [])}
                if done_indices:
                    log.info("%sResume from merged selected_solutions: %d problems done",
                             W, len(done_indices))
            except Exception as e:
                log.warning("%sCould not load merged selected_solutions for resume (%s)", W, e)

    if out_path and not done_indices:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "type": "metadata",
            "split": split_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tc_sample_ratio": args.tc_sample_ratio,
            "tc_largest_ratio": args.tc_largest_ratio,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(metadata, ensure_ascii=False) + "\n")

    failures = []
    failure_path = out_path.parent / out_path.name.replace(".jsonl", "_failures.txt") if out_path else None
    _failure_header_written = False

    def _record_failure(fail: dict):
        nonlocal _failure_header_written
        failures.append(fail)
        if failure_path:
            if not _failure_header_written:
                _init_failure_report(failure_path, "Step 1 (Selected Solutions) Failure Report",
                                     split_name, W)
                _failure_header_written = True
            _append_failure(failure_path, fail, len(failures))

    for idx, example in enumerate(ds_split):
        if args.max_problems > 0 and idx >= args.max_problems:
            log.info("%s[selected_solutions] Reached max_problems=%d, stopping.", W, args.max_problems)
            break

        if problem_indices is not None and idx not in problem_indices:
            continue

        if idx in done_indices:
            continue

        problem_name = example.get("name", f"problem_{idx}")

        if getattr(args, "_skip_problems", None) and problem_name in args._skip_problems:
            log.info("%s  [SKIP] '%s' is in --skip_problems list — skipping Step 1", W, problem_name)
            continue

        problem_id = sanitize_short_name(f"sel_{split_name}_{idx}")
        log.info("")
        log.info("%s[SEL %s/%d] %s  (%s)", W, split_name, idx, problem_name, problem_id)

        tl = example.get("time_limit")
        if tl and tl.get("seconds", 0) > 0:
            timelimit = tl["seconds"] + (1 if tl.get("nanos", 0) > 0 else 0)
        else:
            timelimit = args.timelimit
        memory_limit_bytes = example.get("memory_limit_bytes")

        pub_tests, priv_tests, gen_tests, sampling_info = sample_all_test_cases(
            example, ratio=args.tc_sample_ratio, largest_ratio=args.tc_largest_ratio,
        )
        log.info("%s  TC sampling: public %d->%d, private %d->%d, generated %d->%d",
                 W,
                 sampling_info["public"]["original"], sampling_info["public"]["sampled"],
                 sampling_info["private"]["original"], sampling_info["private"]["sampled"],
                 sampling_info["generated"]["original"], sampling_info["generated"]["sampled"])

        try:
            delete_problem_from_db(db_config, problem_id)
            zip_bytes = build_problem_zip(
                problem_id, problem_name, timelimit,
                pub_tests, priv_tests, gen_tests,
                memory_limit_bytes=memory_limit_bytes,
            )
            resp = admin_client.upload_problem(contest_id, zip_bytes, problem_id)
            log.info("%s  Uploaded selection problem -> %s", W, resp)
        except requests.HTTPError as e:
            body = e.response.text if hasattr(e, 'response') and e.response is not None else ""
            log.warning("%s  Selection problem upload failed: %s | %s", W, e, body)
            _record_failure({
                "index": idx, "name": problem_name, "worker": W.strip(),
                "stage": "sel_problem_upload", "error": str(e), "details": body,
            })
            continue
        except Exception as e:
            log.error("%s  Selection problem upload error: %s", W, e)
            _record_failure({
                "index": idx, "name": problem_name, "worker": W.strip(),
                "stage": "sel_problem_upload", "error": str(e), "details": "",
            })
            continue

        float_tol = detect_float_tolerance(example.get("description", ""))
        if float_tol and db_config:
            set_float_tolerance_in_db(db_config, problem_id, float_tol)

        solutions_data = example.get("solutions", {"language": [], "solution": []})
        lang_codes = solutions_data.get("language", [])
        sol_texts = solutions_data.get("solution", [])
        sel_limit = args.sel_max_solutions

        problem_solutions = {}
        lang_submit_counts = {}

        for sol_idx, (lc, source) in enumerate(zip(lang_codes, sol_texts)):
            _, lang_id = LANG_CODE_MAP.get(lc, ("Unknown", None))
            if lang_id is None:
                continue

            if sel_limit > 0 and lang_submit_counts.get(lang_id, 0) >= sel_limit:
                continue

            if lang_id == "java":
                source = re.sub(r'\bpublic\s+class\s+\w+', 'public class Main', source, count=1)

            filename = make_filename(lang_id, sol_idx)
            submission_id = None

            if args.submit_delay > 0:
                time.sleep(args.submit_delay)

            try:
                if throttle:
                    throttle.acquire(worker_tag=W)
                for submit_attempt in range(3):
                    try:
                        sub = team_client.submit(
                            contest_id, problem_id, lang_id, source, filename,
                        )
                        submission_id = sub.get("id", sub.get("submission_id"))
                        break
                    except (requests.HTTPError, requests.ConnectionError) as e:
                        body = ""
                        if hasattr(e, 'response') and e.response is not None:
                            body = e.response.text
                            if e.response.status_code < 500:
                                log.debug("%s  [SEL] Submit failed (%s sol_idx=%d): %s", W, lang_id, sol_idx, body)
                                break
                        if submit_attempt < 2:
                            time.sleep(2 ** submit_attempt + random.random())
                        else:
                            log.debug("%s  [SEL] Submit failed after 3 retries (%s sol_idx=%d): %s",
                                      W, lang_id, sol_idx, body)
                if submission_id is None:
                    _record_failure({
                        "index": idx, "name": problem_name, "worker": W.strip(),
                        "stage": "sel_submit", "error": "submit failed after retries",
                        "details": f"lang={lang_id}, sol_idx={sol_idx}",
                    })
                    continue

                judgement = _wait_with_retry(
                    admin_client, contest_id, submission_id, args, worker_tag=W,
                )
            finally:
                if throttle:
                    throttle.release()

            verdict = "PENDING"
            max_run_time = None
            runs_data = []

            if judgement:
                verdict = judgement.get("judgement_type_id", "UNKNOWN")
                max_run_time = judgement.get("max_run_time")

                if db_config and submission_id:
                    runs_data = get_runs_from_db(db_config, submission_id)
            else:
                log.warning("%s  [SEL] Judgement timeout for sid=%s", W, submission_id)
                _record_failure({
                    "index": idx, "name": problem_name, "worker": W.strip(),
                    "stage": "sel_judgement_timeout",
                    "error": f"judgement polling timeout for sid={submission_id}",
                    "details": f"lang={lang_id}, sol_idx={sol_idx}, sid={submission_id}",
                })

            sol_record = {
                "sol_idx": sol_idx,
                "verdict": verdict,
                "max_run_time": max_run_time,
                "runs": runs_data,
            }

            bucket = problem_solutions.setdefault(lang_id, [])
            bucket.append(sol_record)
            lang_submit_counts[lang_id] = lang_submit_counts.get(lang_id, 0) + 1

            if verdict == "AC":
                log.info("%s  [SEL] %s sol_idx=%d -> AC (%.3fs)", W, lang_id, sol_idx,
                         max_run_time if max_run_time else 0)
            else:
                log.debug("%s  [SEL] %s sol_idx=%d -> %s", W, lang_id, sol_idx, verdict)

        problem_record = {
            "type": "problem",
            "index": idx,
            "name": problem_name,
            "timelimit": timelimit,
            "sampled_test_cases": sampling_info,
            "solutions": problem_solutions,
        }
        if out_path:
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(problem_record, ensure_ascii=False) + "\n")

        log.info("%s  [SEL] Problem %d done: %s",
                 W, idx, {lid: sum(1 for s in sols if s["verdict"] == "AC")
                       for lid, sols in problem_solutions.items()})

    if out_path and out_path.exists():
        results = _load_selected_solutions_jsonl(out_path)
    else:
        results = {"metadata": {"split": split_name, "total_ac": 0}, "problems": []}
    if failures and failure_path:
        _finalize_failure_report(failure_path, failures)
        log.info("%s[SEL] Failure report saved -> %s (%d failures)", W, failure_path, len(failures))

    log.info("%s[selected_solutions] Complete: total_ac=%d across %d problems",
             W, results["metadata"].get("total_ac", 0), len(results["problems"]))
    return results

def _get_active_judgehosts(admin_client: DOMjudgeClient) -> list[dict]:
    try:
        judgehosts = admin_client.get("judgehosts")
        active = [
            j for j in judgehosts
            if j.get("enabled") and j.get("polltime") is not None
        ]
        return sorted(active, key=lambda j: j.get("hostname", ""))
    except Exception as e:
        log.warning("Could not get judgehosts: %s", e)
        return []

def setup_parallel_contests(db_config: dict, admin_client: DOMjudgeClient,
                            base_contest_id: str, team_user: str,
                            num_workers: int) -> list[dict] | None:
    active = _get_active_judgehosts(admin_client)
    if not active:
        log.error("No active judgehosts — cannot set up parallel contests")
        return None

    actual_workers = min(num_workers, len(active))
    log.info("Setting up %d worker contests (%d active judgehosts available)",
             actual_workers, len(active))

    conn = None
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT * FROM contest WHERE externalid = %s", (base_contest_id,))
        base = cursor.fetchone()
        if not base:
            log.error("Base contest '%s' not found in DB", base_contest_id)
            cursor.close()
            conn.close()
            return None

        cursor.execute(
            "SELECT u.teamid FROM user u WHERE u.username = %s",
            (team_user,)
        )
        team_row = cursor.fetchone()
        team_id = team_row["teamid"] if team_row else None

        assignments = []

        for i in range(actual_workers):
            worker_contest_eid = f"{base_contest_id}_w{i}"

            cursor.execute("SELECT cid FROM contest WHERE externalid = %s", (worker_contest_eid,))
            existing = cursor.fetchone()
            if existing:
                cid = existing["cid"]
                log.info("  Worker %d: reusing contest '%s' (cid=%d)",
                         i, worker_contest_eid, cid)
            else:
                cursor.execute(
                    "INSERT INTO contest "
                    "(externalid, name, shortname, "
                    " activatetime, starttime, endtime, "
                    " activatetime_string, starttime_string, endtime_string, "
                    " enabled, open_to_all_teams, process_balloons, public, allow_submit) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 0, 0, 0, 1)",
                    (worker_contest_eid,
                     f"{base.get('name', 'Contest')} Worker {i}",
                     f"w{i}",
                     base["activatetime"], base["starttime"], base["endtime"],
                     base["activatetime_string"], base["starttime_string"],
                     base["endtime_string"]),
                )
                cid = cursor.lastrowid
                log.info("  Worker %d: created contest '%s' (cid=%d)",
                         i, worker_contest_eid, cid)

            if team_id:
                cursor.execute(
                    "INSERT IGNORE INTO contestteam (cid, teamid) VALUES (%s, %s)",
                    (cid, team_id),
                )

            assignments.append({
                "worker_id": i,
                "contest_id": worker_contest_eid,
                "cid": cid,
            })

        conn.commit()
        cursor.close()
        conn.close()

        log.info("Parallel contest setup complete: %d workers", len(assignments))
        return assignments

    except Exception as e:
        log.error("Failed to setup parallel contests: %s", e)
        if conn:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        return None

def setup_contests_multi_url(domjudge_urls: list[str], db_host: str, db_port_base: int,
                             db_user: str, db_password: str, db_name: str,
                             contest_id: str, team_user: str,
                             ssl_disabled: bool = True) -> list[dict] | None:
    assignments = []
    default_start = 0
    default_end = 2000000000
    default_start_str = "1970-01-01 00:00:00"
    default_end_str = "2033-06-06 00:00:00"
    for i, base_url in enumerate(domjudge_urls):
        db_config = {
            "host": db_host,
            "port": db_port_base + i,
            "user": db_user,
            "password": db_password,
            "database": db_name,
            "ssl_disabled": ssl_disabled,
        }
        conn = None
        try:
            conn = mysql.connector.connect(**db_config)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT cid FROM contest WHERE externalid = %s", (contest_id,))
            row = cursor.fetchone()
            if row:
                cid = row["cid"]
                log.info("  Worker %d: reusing contest '%s' (cid=%d) on %s", i, contest_id, cid, base_url)
            else:
                cursor.execute(
                    "INSERT INTO contest "
                    "(externalid, name, shortname, activatetime, starttime, endtime, "
                    "activatetime_string, starttime_string, endtime_string, "
                    "enabled, open_to_all_teams, process_balloons, public, allow_submit) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 0, 0, 0, 1)",
                    (contest_id, f"Contest {i}", contest_id[:20], default_start, default_start, default_end,
                     default_start_str, default_start_str, default_end_str),
                )
                cid = cursor.lastrowid
                log.info("  Worker %d: created contest '%s' (cid=%d) on %s", i, contest_id, cid, base_url)
            cursor.execute("SELECT u.teamid FROM user u WHERE u.username = %s", (team_user,))
            team_row = cursor.fetchone()
            if team_row:
                cursor.execute("INSERT IGNORE INTO contestteam (cid, teamid) VALUES (%s, %s)", (cid, team_row["teamid"]))
            conn.commit()
            cursor.close()
            conn.close()
            assignments.append({
                "worker_id": i,
                "contest_id": contest_id,
                "domjudge_url": base_url.rstrip("/"),
                "db_config": db_config,
                "cid": cid,
            })
        except Exception as e:
            log.error("Multi-URL setup failed for worker %d (%s): %s", i, base_url, e)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            return None
    log.info("Multi-URL contest setup complete: %d workers", len(assignments))
    return assignments

def cleanup_parallel_contests(db_config: dict, assignments: list[dict], multi_url: bool = False):
    if not assignments:
        return
    if multi_url:
        for a in assignments:
            cfg = a.get("db_config")
            if not cfg:
                continue
            try:
                conn = mysql.connector.connect(**cfg)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM contestteam WHERE cid = %s", (a["cid"],))
                conn.commit()
                cursor.close()
                conn.close()
            except Exception as e:
                log.warning("Cleanup worker %s: %s", a.get("worker_id"), e)
        log.info("Cleanup: cleaned up %d worker contests (multi-URL)", len(assignments))
        return
    conn = None
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        for a in assignments:
            cursor.execute("DELETE FROM contestteam WHERE cid = %s", (a["cid"],))
        conn.commit()
        cursor.close()
        conn.close()
        log.info("Cleanup: cleaned up %d worker contests", len(assignments))
    except Exception as e:
        log.warning("Cleanup error: %s", e)
        if conn:
            try:
                conn.close()
            except Exception:
                pass

def _run_worker(worker_id: int, contest_id: str,
                domjudge_url: str, admin_auth: tuple, team_auth: tuple,
                ds_split, split_name: str, args,
                db_config: dict, custom_tc: dict,
                problem_indices: set,
                selected_solutions: dict,
                sel_out_path: Path, main_out_path: Path,
                throttle: AdaptiveThrottle = None) -> dict:
    admin = DOMjudgeClient(domjudge_url, admin_auth[0], admin_auth[1])
    team = DOMjudgeClient(domjudge_url, team_auth[0], team_auth[1])

    W = f"[W{worker_id}] "
    log.info("%sStarting: contest=%s, %d problems assigned",
             W, contest_id, len(problem_indices))

    worker_sel_solutions = None
    if selected_solutions:
        worker_sel_solutions = selected_solutions
        ac_count = selected_solutions.get("metadata", {}).get("total_ac", 0)
        log.info("%sStep 1 SKIP: using pre-loaded selected_solutions (%d AC)",
                 W, ac_count)
    elif args.source == "dataset":
        worker_sel_solutions = build_selected_solutions(
            admin, team, ds_split, split_name, args,
            db_config=db_config, out_path=sel_out_path,
            problem_indices=problem_indices,
            contest_id_override=contest_id,
            worker_tag=W,
            throttle=throttle,
        )
        ac_count = worker_sel_solutions.get("metadata", {}).get("total_ac", 0)
        log.info("%sStep 1 done: %d AC solutions", W, ac_count)

    result = process_split(
        admin, team, ds_split, split_name, args,
        db_config=db_config, custom_tc=custom_tc, out_path=main_out_path,
        selected_solutions=worker_sel_solutions,
        problem_indices=problem_indices,
        contest_id_override=contest_id,
        worker_tag=W,
        throttle=throttle,
    )

    log.info("%sStep 2 done: %d problems", W, len(result.get("problems", [])))
    return result

def _append_failure(path: Path, fail: dict, fail_num: int) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n[{fail_num}] Problem idx={fail['index']} \"{fail['name']}\"\n")
        f.write(f"    Worker:  {fail['worker']}\n")
        f.write(f"    Stage:   {fail['stage']}\n")
        f.write(f"    Error:   {fail['error']}\n")
        if fail.get("details"):
            f.write(f"    Details: {fail['details']}\n")
        f.write(f"    Time:    {datetime.now(timezone.utc).isoformat()}\n")

def _init_failure_report(path: Path, title: str, split_name: str,
                         worker_tag: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{title}\n{'='*60}\n")
        f.write(f"Split: {split_name}\n")
        f.write(f"Worker: {worker_tag.strip() or 'sequential'}\n")
        f.write(f"Started: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"{'='*60}\n")

def _finalize_failure_report(path: Path, failures: list[dict]) -> None:
    if not failures:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\nSummary (total: {len(failures)} failures)\n{'='*60}\n")
        stage_counts = {}
        for fail in failures:
            stage_counts[fail["stage"]] = stage_counts.get(fail["stage"], 0) + 1
        f.write("Failure count by stage:\n")
        for stage, count in sorted(stage_counts.items()):
            f.write(f"  {stage}: {count}\n")
        failed_indices = sorted(set(fail["index"] for fail in failures))
        f.write(f"Failed problem indices ({len(failed_indices)}): {failed_indices}\n")

def _write_failure_report(path: Path, title: str, split_name: str,
                          worker_tag: str, failures: list[dict]) -> None:
    _init_failure_report(path, title, split_name, worker_tag)
    for i, fail in enumerate(failures, 1):
        _append_failure(path, fail, i)
    _finalize_failure_report(path, failures)

def _merge_failure_reports(worker_paths: list[Path], merged_path: Path,
                           title: str, split_name: str, num_workers: int) -> None:
    existing = [p for p in worker_paths if p.exists()]
    if not existing:
        return
    with open(merged_path, "w", encoding="utf-8") as mf:
        mf.write(f"{title}\n{'='*60}\n")
        mf.write(f"Split: {split_name}\n")
        mf.write(f"Workers with failures: {len(existing)}/{num_workers}\n")
        mf.write(f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n")
        mf.write(f"{'='*60}\n\n")
        for fp in existing:
            mf.write(f"--- {fp.name} ---\n")
            mf.write(fp.read_text(encoding="utf-8"))
            mf.write("\n\n")
    log.info("Merged failure report -> %s (%d workers had failures)",
             merged_path, len(existing))

def _merge_worker_results_jsonl(worker_paths: list[Path], split_name: str, args) -> dict:
    merged = {
        "metadata": {
            "dataset": "deepmind/code_contests",
            "split": split_name,
            "source": args.source,
            "custom_testcases": args.custom_testcases,
            "default_timelimit": args.timelimit,
            "max_solutions": args.max_solutions,
            "num_workers": len(worker_paths),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "problems": [],
    }

    for p in worker_paths:
        if p.exists():
            loaded = _load_process_split_jsonl(p)
            merged["problems"].extend(loaded.get("problems", []))
    merged["problems"].sort(key=lambda p: p.get("index", 0))

    return merged

def _merge_selected_solutions(worker_paths: list[Path]) -> dict:
    all_problems = []
    metadata = None
    for p in worker_paths:
        if p.exists():
            data = _load_selected_solutions_jsonl(p)
            if metadata is None:
                metadata = data.get("metadata", {})
            all_problems.extend(data.get("problems", []))
    all_problems.sort(key=lambda p: p.get("index", 0))
    total_ac = sum(
        1 for p in all_problems
        for sols in p.get("solutions", {}).values()
        for s in sols if s.get("verdict") == "AC"
    )
    if metadata is None:
        metadata = {}
    metadata["total_ac"] = total_ac
    return {"metadata": metadata, "problems": all_problems}

def build_speed_tiers(times: list[float], runs_data: list[dict] = None) -> dict | None:
    if len(times) < 2:
        return None

    t_min = min(times)
    t_max = max(times)
    t_range = t_max - t_min
    fast_upper = t_min + t_range / 3.0
    medium_upper = t_min + t_range * 2.0 / 3.0

    result = {
        "range": {"min": t_min, "max": t_max},
        "thresholds": {
            "fast": f"{t_min:.4f} ~ {fast_upper:.4f}s",
            "medium": f"{fast_upper:.4f} ~ {medium_upper:.4f}s",
            "slow": f"{medium_upper:.4f} ~ {t_max:.4f}s",
        },
    }

    if runs_data is not None:
        fast_runs = [r for r in runs_data if r["run_time"] is not None and r["run_time"] <= fast_upper]
        medium_runs = [r for r in runs_data if r["run_time"] is not None and fast_upper < r["run_time"] <= medium_upper]
        slow_runs = [r for r in runs_data if r["run_time"] is not None and r["run_time"] > medium_upper]
        result["counts"] = {
            "fast": len(fast_runs),
            "medium": len(medium_runs),
            "slow": len(slow_runs),
        }
        result["testcases"] = {
            "fast": [r["testcase"] for r in fast_runs],
            "medium": [r["testcase"] for r in medium_runs],
            "slow": [r["testcase"] for r in slow_runs],
        }
    else:
        fast_count = sum(1 for t in times if t <= fast_upper)
        medium_count = sum(1 for t in times if fast_upper < t <= medium_upper)
        slow_count = sum(1 for t in times if t > medium_upper)
        result["counts"] = {
            "fast": fast_count,
            "medium": medium_count,
            "slow": slow_count,
        }

    return result

@dataclasses.dataclass
class _PhaseAResult:
    idx: int
    problem_name: str
    problem_id: str
    timelimit: int
    memory_limit_bytes: int | None
    path: str
    all_inputs: list[str] | None = None
    per_lang_outputs: dict | None = None
    cache_path: str | None = None
    public_tests: dict | None = None
    private_tests: dict | None = None
    generated_tests: dict | None = None

def _load_pa_data(pa: "_PhaseAResult") -> tuple[dict, list]:
    if pa.per_lang_outputs is not None:
        return pa.per_lang_outputs, pa.all_inputs or []
    if not pa.cache_path or not Path(pa.cache_path).exists():
        log.warning("_load_pa_data: cache file missing for '%s' (%s)",
                    pa.problem_name, pa.cache_path)
        return {}, pa.all_inputs or []
    with open(pa.cache_path, encoding="utf-8") as f:
        data = json.load(f)
    per_lang_raw = data.get("per_lang_outputs", {})
    per_lang_outputs: dict = {}
    for lang, v in per_lang_raw.items():
        if isinstance(v, dict) and "outputs" in v:
            outputs = [o if o is not None else "" for o in v["outputs"]]
            per_lang_outputs[lang] = (outputs, v["sol_idx"], v["source"])
    all_inputs: list = data.get("all_inputs") or pa.all_inputs or []
    return per_lang_outputs, all_inputs

_CACHE_LANGS = ["cpp", "python3", "java"]

def _output_cache_path(args, split_name: str, problem_name: str) -> Path:
    safe_name = re.sub(r'[^\w\-.]', '_', problem_name)
    source_path = Path(args.source) if args.source else Path("dataset")
    parts = source_path.parts
    if len(parts) == 4 and parts[0] == "our_method":
        cache_source = str(Path(parts[0]) / parts[1] / parts[3] / parts[2])
    else:
        cache_source = args.source
    if args.source == "dataset":
        cache_source = f"dataset/{getattr(args, 'solution_strategy', 'fast_solution')}"
    cache_root = (getattr(args, "expected_output_cache_dir", None)
                  or os.environ.get("EXPECTED_OUTPUT_CACHE_DIR"))
    if cache_root:
        base = Path(cache_root)
    else:
        base = Path(args.output_dir).parent / "expected_output_cache"
    return base / "codecontests" / cache_source / split_name / f"{safe_name}.json"

def _load_output_cache(cache_path: Path, input_hash: str) -> dict | None:
    if not cache_path.exists():
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
        if data.get("input_hash") != input_hash:
            return None
        result = {}
        for lang, v in data.get("per_lang_outputs", {}).items():
            if isinstance(v, dict) and "status" in v:
                continue
            if isinstance(v, dict) and "outputs" in v:
                outputs = [o if o is not None else "" for o in v["outputs"]]
                result[lang] = (outputs, v["sol_idx"], v["source"])
        return result if result else None
    except Exception:
        return None

def _save_output_cache(cache_path: Path, input_hash: str,
                       per_lang_outputs: dict, sel_problem: dict | None = None,
                       *, idx: int = None, problem_name: str = None,
                       problem_id: str = None, timelimit: int = None,
                       memory_limit_bytes: int = None,
                       all_inputs: list[str] = None) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {}
        for lang in _CACHE_LANGS:
            if lang in per_lang_outputs:
                outputs, sol_idx, source = per_lang_outputs[lang]
                serializable[lang] = {
                    "sol_idx": sol_idx,
                    "source": source,
                    "outputs": [o if o else None for o in outputs],
                }
            else:
                if sel_problem is not None:
                    ac = [s for s in sel_problem.get("solutions", {}).get(lang, [])
                          if s.get("verdict") == "AC"]
                    status = "all_failed" if ac else "no_selected_solution"
                else:
                    status = "no_selected_solution"
                serializable[lang] = {"status": status}
        with open(cache_path, "w") as f:
            json.dump({
                "input_hash": input_hash,
                "meta": {
                    "idx": idx,
                    "problem_name": problem_name,
                    "problem_id": problem_id,
                    "timelimit": timelimit,
                    "memory_limit_bytes": memory_limit_bytes,
                },
                "all_inputs": all_inputs,
                "per_lang_outputs": serializable,
            }, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning("Could not save output cache to %s: %s", cache_path, e)

def _rebuild_phase_a_cache_from_disk(args, split_name: str,
                                     problem_indices: set | None = None) -> dict:
    source_path = Path(args.source) if args.source else Path("dataset")
    parts = source_path.parts
    if len(parts) == 4 and parts[0] == "our_method":
        cache_source = str(Path(parts[0]) / parts[1] / parts[3] / parts[2])
    else:
        cache_source = args.source
    if args.source == "dataset":
        cache_source = f"dataset/{getattr(args, 'solution_strategy', 'fast_solution')}"
    cache_root = (getattr(args, "expected_output_cache_dir", None)
                  or os.environ.get("EXPECTED_OUTPUT_CACHE_DIR"))
    if cache_root:
        cache_dir = Path(cache_root) / "codecontests" / cache_source / split_name
    else:
        cache_dir = (Path(args.output_dir).parent / "expected_output_cache"
                     / "codecontests" / cache_source / split_name)
    result: dict[int, _PhaseAResult] = {}
    if not cache_dir.exists():
        return result
    for cache_file in cache_dir.glob("*.json"):
        try:
            with open(cache_file) as f:
                data = json.load(f)
            meta = data.get("meta", {})
            idx = meta.get("idx")
            if idx is None:
                continue
            if problem_indices is not None and idx not in problem_indices:
                continue
            if not data.get("per_lang_outputs"):
                continue
            result[idx] = _PhaseAResult(
                idx=idx,
                problem_name=meta.get("problem_name", f"problem_{idx}"),
                problem_id=meta.get("problem_id", f"prob_{idx}"),
                timelimit=meta.get("timelimit", 2),
                memory_limit_bytes=meta.get("memory_limit_bytes"),
                path="per_lang",
                cache_path=str(cache_file),
                all_inputs=None,
                per_lang_outputs=None,
            )
        except Exception as e:
            log.warning("Could not load cache file %s: %s", cache_file, e)
    log.info("Rebuilt phase_a_cache from disk: %d problems", len(result))
    return result

def _phase_a_generate_outputs(
    ds_split,
    split_name: str,
    args,
    custom_tc_lookup: dict,
    sel_solutions_by_idx: dict,
    problem_indices: set | None,
    done_indices: set,
    worker_tag: str = "",
) -> dict:
    W = worker_tag
    cache: dict[int, _PhaseAResult] = {}

    inputs_dir_path = getattr(args, "_inputs_dir_path", None)
    inputs_dir_index = getattr(args, "_inputs_dir_index", set())
    inputs_dir_name_files = getattr(args, "_inputs_dir_name_files", {})

    total = min(args.max_problems, len(ds_split)) if args.max_problems > 0 else len(ds_split)
    outer_bar = tqdm(enumerate(ds_split), total=total,
                     desc="[Local] generating expected outputs", unit="prob", leave=True)
    for idx, example in outer_bar:
        if args.max_problems > 0 and idx >= args.max_problems:
            log.info("%s[Local] reached max_problems=%d, stopping.", W, args.max_problems)
            break
        if problem_indices is not None and idx not in problem_indices:
            continue
        if idx in done_indices:
            continue

        problem_name = example.get("name", f"problem_{idx}")
        outer_bar.set_postfix(prob=problem_name[:30])

        if getattr(args, "_skip_problems", None) and problem_name in args._skip_problems:
            log.info("%s  [SKIP] '%s' is in --skip_problems list — skipping Phase A", W, problem_name)
            continue

        source_tag = "" if args.source == "dataset" else f"_{args.source}"
        problem_id = sanitize_short_name(f"prob_{split_name}_{idx}{source_tag}")

        tl = example.get("time_limit")
        if tl and tl.get("seconds", 0) > 0:
            timelimit = tl["seconds"] + (1 if tl.get("nanos", 0) > 0 else 0)
        else:
            timelimit = args.timelimit
        memory_limit_bytes = example.get("memory_limit_bytes")

        all_inputs = None
        all_outputs = []

        _has_int_match = inputs_dir_path and idx in inputs_dir_index
        _has_name_match = inputs_dir_path and problem_name in inputs_dir_name_files
        if _has_int_match or _has_name_match:
            files_to_load = []
            if _has_int_match:
                idx_prefix = f"{idx:04d}"
                for tier in ("fast", "medium", "slow"):
                    tier_dir = inputs_dir_path / tier
                    if tier_dir.exists():
                        files_to_load.extend(sorted(tier_dir.glob(f"{idx_prefix}_*.json")))
            else:
                files_to_load = sorted(inputs_dir_name_files.get(problem_name, []))
            _loaded = []
            for fpath in files_to_load:
                try:
                    with open(fpath, "r", encoding="utf-8") as _tc_f:
                        tc = json.load(_tc_f)
                    _mf = getattr(args, "method_filter", None)
                    if _mf and _mf not in (tc.get("method") or ""):
                        continue
                    _stdin = tc.get("stdin") if tc.get("stdin") is not None else tc.get("input")
                    if _stdin is None and tc.get("method") == "boundary_slow_compact":
                        _features = getattr(args, "_compact_m1_features", {})
                        if idx in _features:
                            try:
                                from utils.slow_testcase_generator import expand_compact_m1_tc
                                _constraints, _structure = _features[idx]
                                _stdin = expand_compact_m1_tc(tc, _constraints, _structure)
                            except Exception as _expand_err:
                                log.warning("%s  compact M1 expand failed idx=%d %s: %s",
                                            W, idx, fpath.name, _expand_err)
                        else:
                            log.warning("%s  compact M1 idx=%d not in features; "
                                        "submitting empty stdin", W, idx)
                    if _stdin is None:
                        _gen_code = tc.get("_generator_code") or (tc.get("testcase") or {}).get("_generator_code", "")
                        if _gen_code:
                            try:
                                from utils.generator_executor import execute_generator
                                _gen_ok, _gen_result = execute_generator(_gen_code, timeout=30)
                                if _gen_ok:
                                    _stdin = _gen_result
                                else:
                                    log.warning("%s  generator exec failed idx=%d %s: %s",
                                                W, idx, fpath.name, _gen_result)
                            except Exception as _gen_err:
                                log.warning("%s  generator exec error idx=%d %s: %s",
                                            W, idx, fpath.name, _gen_err)
                    _loaded.append(_stdin or "")
                except (json.JSONDecodeError, ValueError, OSError):
                    continue
            if _loaded:
                all_inputs = _loaded
                all_outputs = [""] * len(_loaded)
            else:
                log.warning("%s  [Local] [%s/%d] No TC files in inputs_dir for '%s' — skipping",
                            W, split_name, idx, problem_name)
                continue
        elif inputs_dir_path:
            log.warning("%s  [Local] [%s/%d] '%s' not in inputs_dir index — skipping",
                        W, split_name, idx, problem_name)
            continue
        elif custom_tc_lookup and problem_name in custom_tc_lookup:
            tc_entry = custom_tc_lookup[problem_name]
            tc_data = tc_entry.get("test_cases", {})

            if isinstance(tc_data, dict) and any(k in tc_data for k in ("fast", "medium", "slow")):
                all_inputs = []
                all_outputs = []
                for tier in ("fast", "medium", "slow"):
                    for tc in tc_data.get(tier, []):
                        all_inputs.append(tc.get("input", ""))
                        all_outputs.append(tc.get("expected_output", ""))
            elif isinstance(tc_data, list):
                all_inputs = [tc.get("input", "") for tc in tc_data]
                all_outputs = [tc.get("expected_output", "") for tc in tc_data]
            else:
                log.warning("%s  [Local] [%s/%d] Unknown TC format for '%s', using dataset tests",
                            W, split_name, idx, problem_name)
        elif custom_tc_lookup:
            log.warning("%s  [Local] [%s/%d] '%s' not in custom TCs, skipping",
                        W, split_name, idx, problem_name)
            continue
        elif args.source == "dataset":
            _pub = example.get("public_tests", {"input": [], "output": []}) or {"input": [], "output": []}
            _priv = example.get("private_tests", {"input": [], "output": []}) or {"input": [], "output": []}
            _gen = example.get("generated_tests", {"input": [], "output": []}) or {"input": [], "output": []}
            _ratio = getattr(args, "tc_sample_ratio", 1.0)
            if _ratio is not None and _ratio < 1.0:
                _pub, _priv, _gen, _ = sample_all_test_cases(
                    example, ratio=_ratio,
                    largest_ratio=getattr(args, "tc_largest_ratio", 0.5),
                )
            all_inputs = (
                list(_pub.get("input", []))
                + list(_priv.get("input", []))
                + list(_gen.get("input", []))
            )
            all_outputs = (
                list(_pub.get("output", []))
                + list(_priv.get("output", []))
                + list(_gen.get("output", []))
            )

        if all_inputs is not None:
            abbreviated_mask = [
                _is_abbreviated_input(inp) or _is_abbreviated_output(out)
                for inp, out in zip(all_inputs, all_outputs)
            ]
            n_abbreviated = sum(abbreviated_mask)
            if n_abbreviated > 0:
                log.info("%s  [Local] [%s/%d] Detected %d/%d abbreviated TCs",
                         W, split_name, idx, n_abbreviated, len(all_inputs))

            input_hash = hashlib.md5(
                "\n---\n".join(all_inputs).encode()
            ).hexdigest()
            cache_path = _output_cache_path(args, split_name, problem_name)
            cached = None if getattr(args, "no_output_cache", False) else _load_output_cache(cache_path, input_hash)
            if cached is not None:
                per_lang_outputs = cached
                log.info("%s  [Local] [%s/%d] Cache hit — skipping execution for '%s'",
                         W, split_name, idx, problem_name)
            elif args.source == "dataset":
                _sel_problem = sel_solutions_by_idx.get(idx)
                _solutions_data = example.get("solutions",
                                              {"language": [], "solution": []})
                _sol_texts = _solutions_data.get("solution", [])
                _sol_strategy = getattr(args, "solution_strategy", "fast_solution")
                _ds_outputs = list(all_outputs)

                def _avg_rt_local(s):
                    runs = s.get("runs", [])
                    if not runs:
                        return float("inf")
                    return sum(
                        r["run_time"] if r.get("run_time") is not None else float("inf")
                        for r in runs
                    ) / len(runs)

                def _pick_local(ac_sols, strategy, problem_idx):
                    if not ac_sols:
                        return None
                    sorted_sols = sorted(ac_sols, key=_avg_rt_local)
                    if strategy == "fast_solution":
                        return sorted_sols[0]
                    if strategy == "slow_solution":
                        return sorted_sols[-1]
                    if strategy.startswith("random"):
                        n = int(strategy.replace("random", "").replace("_solution", ""))
                        rng = random.Random(42 + n + problem_idx * 1000)
                        if len(sorted_sols) <= 2:
                            return rng.choice(sorted_sols)
                        return rng.choice(sorted_sols[1:-1])
                    return sorted_sols[0]

                per_lang_outputs = {}
                if _sel_problem:
                    for lang_id, sol_list in _sel_problem.get("solutions", {}).items():
                        ac_solutions = [s for s in sol_list if s.get("verdict") == "AC"]
                        best = _pick_local(ac_solutions, _sol_strategy, idx)
                        if best is None:
                            continue
                        sol_idx = best.get("sol_idx")
                        if sol_idx is None or sol_idx >= len(_sol_texts):
                            continue
                        source = _sol_texts[sol_idx]
                        per_lang_outputs[lang_id] = (_ds_outputs, sol_idx, source)
                if not per_lang_outputs:
                    log.warning("%s  [Local] [%s/%d] No AC solution available for any language in '%s' (strategy=%s) — skipping",
                                W, split_name, idx, problem_name, _sol_strategy)
                    continue
                log.info("%s  [Local] [%s/%d] Strategy=%s picked sol_idx per lang: %s | %d canonical outputs",
                         W, split_name, idx, _sol_strategy,
                         {lid: triple[1] for lid, triple in per_lang_outputs.items()},
                         len(_ds_outputs))
                _save_output_cache(cache_path, input_hash, per_lang_outputs, _sel_problem,
                                   idx=idx, problem_name=problem_name,
                                   problem_id=problem_id, timelimit=timelimit,
                                   memory_limit_bytes=memory_limit_bytes,
                                   all_inputs=all_inputs)
            else:
                sel_problem = sel_solutions_by_idx.get(idx)

                _sol_strategy = getattr(args, "solution_strategy", "fast_solution")

                def _worker(q, all_inputs, example, timelimit, sel_problem, abbreviated_mask, n_abbreviated, sol_strategy):
                    try:
                        with open("/proc/self/oom_score_adj", "w") as _oom_f:
                            _oom_f.write("1000")
                    except Exception:
                        pass
                    try:
                        generate_expected_outputs_per_lang(
                            all_inputs, example, timelimit, sel_problem,
                            skip_mask=abbreviated_mask if n_abbreviated > 0 else None,
                            stream_queue=q,
                            solution_strategy=sol_strategy)
                    except Exception as e:
                        q.put(e)

                q = multiprocessing.Queue()
                p = multiprocessing.Process(
                    target=_worker,
                    args=(q, all_inputs, example, timelimit, sel_problem, abbreviated_mask, n_abbreviated, _sol_strategy),
                )
                p.start()
                _CHILD_TIMEOUT = 900

                import threading as _threading
                per_lang_outputs = {}
                _drain_done = _threading.Event()

                def _drain_queue():
                    while not _drain_done.is_set():
                        try:
                            item = q.get(timeout=0.2)
                        except Exception:
                            continue
                        if item is None:
                            break
                        if isinstance(item, Exception):
                            log.warning("%s  [Local] [%s/%d] Child error: %s",
                                        W, split_name, idx, item)
                            break
                        if isinstance(item, dict):
                            per_lang_outputs.update(item)

                _drain_thread = _threading.Thread(target=_drain_queue, daemon=True)
                _drain_thread.start()
                p.join(timeout=_CHILD_TIMEOUT)
                timed_out = p.is_alive()
                if timed_out:
                    log.warning("%s  [Local] [%s/%d] Child process timed out (%ds) — killing '%s'",
                                W, split_name, idx, _CHILD_TIMEOUT, problem_name)
                    p.kill()
                    p.join()
                _drain_done.set()
                _drain_thread.join(timeout=5)

                if timed_out:
                    if per_lang_outputs:
                        log.warning("%s  [Local] [%s/%d] Timed out but salvaged %d language(s): %s",
                                    W, split_name, idx, len(per_lang_outputs), list(per_lang_outputs.keys()))
                    else:
                        oom_log_path = Path(args.output_dir) / args.source / f"oom_skipped_{split_name}.jsonl"
                        oom_log_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(oom_log_path, "a", encoding="utf-8") as _f:
                            _f.write(json.dumps({
                                "idx": idx,
                                "problem_name": problem_name,
                                "exitcode": -1,
                                "reason": "child_timeout",
                                "timestamp": datetime.now().isoformat(),
                            }, ensure_ascii=False) + "\n")
                        continue

                per_lang_outputs = per_lang_outputs if per_lang_outputs else None

                if p.exitcode != 0 and not timed_out:
                    log.warning("%s  [Local] [%s/%d] Child process killed (exitcode=%d, likely OOM) — skipping '%s'",
                                W, split_name, idx, p.exitcode, problem_name)
                    oom_log_path = Path(args.output_dir) / args.source / f"oom_skipped_{split_name}.jsonl"
                    oom_log_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(oom_log_path, "a", encoding="utf-8") as _f:
                        _f.write(json.dumps({
                            "idx": idx,
                            "problem_name": problem_name,
                            "exitcode": p.exitcode,
                            "timestamp": datetime.now().isoformat(),
                        }, ensure_ascii=False) + "\n")
                    continue

                if not per_lang_outputs:
                    log.warning("%s  [Local] [%s/%d] No language produced outputs, skipping '%s'",
                                W, split_name, idx, problem_name)
                    continue
                _save_output_cache(cache_path, input_hash, per_lang_outputs, sel_problem,
                                   idx=idx, problem_name=problem_name,
                                   problem_id=problem_id, timelimit=timelimit,
                                   memory_limit_bytes=memory_limit_bytes,
                                   all_inputs=all_inputs)

            if not per_lang_outputs:
                log.warning("%s  [Local] [%s/%d] No language produced outputs, skipping '%s'",
                            W, split_name, idx, problem_name)
                continue
            log.info("%s  [Local] [%s/%d] Per-language outputs: %s",
                     W, split_name, idx,
                     {k: sum(1 for o in v[0] if o) for k, v in per_lang_outputs.items()})

            cache[idx] = _PhaseAResult(
                idx=idx, problem_name=problem_name, problem_id=problem_id,
                timelimit=timelimit, memory_limit_bytes=memory_limit_bytes,
                path="per_lang",
                cache_path=str(cache_path),
                all_inputs=None,
                per_lang_outputs=None,
            )
        else:
            log.warning("%s  [%d] %s: no custom test cases available, skipping",
                        W, idx, problem_name)
            continue

    n_per_lang = sum(1 for p in cache.values() if p.path == "per_lang")
    n_dataset = sum(1 for p in cache.values() if p.path == "dataset")
    log.info("%s=== [Local] complete: %d problems cached "
             "(per_lang=%d, dataset=%d) ===",
             W, len(cache), n_per_lang, n_dataset)
    return cache

def _phase_b_domjudge_eval(
    admin_client: DOMjudgeClient,
    team_client: DOMjudgeClient,
    ds_split,
    split_name: str,
    args,
    db_config: dict,
    phase_a_cache: dict,
    sel_solutions_by_idx: dict,
    contest_id: str,
    out_path: Path | None,
    worker_tag: str = "",
    throttle: AdaptiveThrottle = None,
) -> tuple:
    W = worker_tag
    problem_records = []
    failures = []
    failure_path = out_path.parent / out_path.name.replace(".jsonl", "_failures.txt") if out_path else None
    _failure_header_written = False

    def _record_failure(fail: dict):
        nonlocal _failure_header_written
        failures.append(fail)
        if failure_path:
            if not _failure_header_written:
                _init_failure_report(failure_path, "Step 2 Failure Report", split_name, W)
                _failure_header_written = True
            _append_failure(failure_path, fail, len(failures))

    for idx in sorted(phase_a_cache.keys()):
        pa = phase_a_cache[idx]
        example = ds_split[pa.idx]

        log.info("")
        log.info("%s[%s/%d] %s  (%s)  [path=%s]",
                 W, split_name, pa.idx, pa.problem_name, pa.problem_id, pa.path)
        log.info("%s  time_limit: %ds", W, pa.timelimit)

        if pa.path == "per_lang":
            _pa_per_lang_outputs, _pa_all_inputs = _load_pa_data(pa)
            if not _pa_per_lang_outputs:
                log.warning("%s  [%s/%d] No per_lang_outputs for '%s' — skipping",
                            W, split_name, pa.idx, pa.problem_name)
                continue

            solutions_data = example.get("solutions", {"language": [], "solution": []})
            lang_codes_all = solutions_data.get("language", [])

            lang_solution_counts: dict[str, int] = {}
            for lc in lang_codes_all:
                _, lid = LANG_CODE_MAP.get(lc, ("Unknown", None))
                if lid:
                    lang_solution_counts[lid] = lang_solution_counts.get(lid, 0) + 1

            float_tol = detect_float_tolerance(example.get("description", ""))
            problem_solutions = []

            for pl_lang_id, (pl_outputs, sol_idx, source) in _pa_per_lang_outputs.items():
                _suffix = f"_{pl_lang_id}"
                _base = sanitize_short_name(pa.problem_id, 50 - len(_suffix))
                pl_problem_id = _base + _suffix
                pl_private = {"input": _pa_all_inputs, "output": pl_outputs}
                try:
                    delete_problem_from_db(db_config, pl_problem_id)
                    zip_bytes = build_problem_zip(
                        pl_problem_id, f"{pa.problem_name} [{pl_lang_id}]", pa.timelimit,
                        {"input": [], "output": []}, pl_private,
                        {"input": [], "output": []},
                        memory_limit_bytes=pa.memory_limit_bytes,
                    )
                    resp = admin_client.upload_problem(contest_id, zip_bytes, pl_problem_id)
                    log.info("%s  Uploaded %s problem -> %s", W, pl_lang_id, resp)
                except Exception as e:
                    log.warning("%s  Upload failed for %s: %s", W, pl_lang_id, e)
                    _record_failure({
                        "index": pa.idx, "name": pa.problem_name, "worker": W.strip(),
                        "stage": "problem_upload_per_lang",
                        "error": str(e), "details": pl_lang_id,
                    })
                    continue

                if float_tol and db_config:
                    set_float_tolerance_in_db(db_config, pl_problem_id, float_tol)

                lang_name = _LANG_ID_TO_NAME.get(pl_lang_id, pl_lang_id)
                if pl_lang_id == "java":
                    source = re.sub(
                        r'\bpublic\s+class\s+\w+', 'public class Main',
                        source, count=1)
                filename = make_filename(pl_lang_id, 0)
                submission_id = None

                if args.submit_delay > 0:
                    time.sleep(args.submit_delay)
                try:
                    if throttle:
                        throttle.acquire(worker_tag=W)
                    for attempt in range(3):
                        try:
                            sub = team_client.submit(
                                contest_id, pl_problem_id, pl_lang_id, source, filename)
                            submission_id = sub.get("id", sub.get("submission_id"))
                            log.info("%s  Submitted %s [%s] -> sid=%s (per-lang)",
                                     W, pl_lang_id, filename, submission_id)
                            break
                        except (requests.HTTPError, requests.ConnectionError) as e:
                            body = ""
                            if hasattr(e, 'response') and e.response is not None:
                                body = e.response.text
                                if e.response.status_code < 500:
                                    log.warning("%s  Submit failed (%s): %s | %s",
                                                W, pl_lang_id, e, body)
                                    break
                            if attempt < 2:
                                time.sleep(2 ** attempt + random.random())
                            else:
                                log.warning("%s  Submit failed after retries (%s): %s",
                                            W, pl_lang_id, e)

                    if submission_id is None:
                        problem_solutions.append({
                            "solution_index": sol_idx,
                            "language": lang_name, "language_id": pl_lang_id,
                            "submission_id": None, "verdict": "SUBMIT_ERROR",
                            "error": "submit failed", "runs": [],
                            "min_time": None, "max_time": None, "total_time": None,
                            "speed_tiers": None,
                        })
                        continue

                    judgement = _wait_with_retry(
                        admin_client, contest_id, submission_id, args, worker_tag=W)
                finally:
                    if throttle:
                        throttle.release()

                runs_data = []
                times = []
                verdict = "PENDING"
                min_time = max_time = total_time = None
                if judgement:
                    verdict = judgement.get("judgement_type_id", "UNKNOWN")
                    max_time = judgement.get("max_run_time")
                    if db_config and submission_id:
                        runs_data = get_runs_from_db(db_config, submission_id)
                    times = [r["run_time"] for r in runs_data if r["run_time"] is not None]
                    if times:
                        min_time = min(times)
                        max_time = max(times)
                        total_time = sum(times)

                speed_tiers = build_speed_tiers(times, runs_data) if times else None
                problem_solutions.append({
                    "solution_index": sol_idx,
                    "language": lang_name, "language_id": pl_lang_id,
                    "submission_id": submission_id, "verdict": verdict,
                    "min_time": min_time, "max_time": max_time,
                    "total_time": total_time, "speed_tiers": speed_tiers,
                    "runs": runs_data,
                })

            all_times = [
                r["run_time"] for sol in problem_solutions
                for r in sol.get("runs", []) if r["run_time"] is not None
            ]
            problem_speed_tiers = build_speed_tiers(all_times)
            if problem_speed_tiers:
                problem_speed_tiers["total_runs"] = len(all_times)
                problem_speed_tiers["num_solutions"] = len(
                    [s for s in problem_solutions if s.get("runs")])

            problem_record = {
                "type": "problem",
                "index": pa.idx,
                "name": pa.problem_name,
                "problem_id": pa.problem_id,
                "timelimit": pa.timelimit,
                "num_test_cases": {
                    "public": 0,
                    "private": len(_pa_all_inputs),
                    "generated": 0,
                },
                "language_solutions": lang_solution_counts,
                "speed_tiers": problem_speed_tiers,
                "solutions": problem_solutions,
                "per_lang_mode": True,
            }

        else:
            log.warning("%s  [%s/%d] Unexpected non-per_lang path='%s' for '%s', skipping",
                        W, split_name, pa.idx, pa.path, pa.problem_name)
            continue
            public_tests = pa.public_tests
            private_tests = pa.private_tests
            generated_tests = pa.generated_tests

            n_public = len(public_tests.get("input", []))
            n_private = len(private_tests.get("input", []))
            n_generated = len(generated_tests.get("input", []))
            log.info("%s  Using %s test cases: pub=%d priv=%d gen=%d",
                     W, pa.path, n_public, n_private, n_generated)

            try:
                delete_problem_from_db(db_config, pa.problem_id)
                zip_bytes = build_problem_zip(
                    pa.problem_id, pa.problem_name, pa.timelimit,
                    public_tests, private_tests, generated_tests,
                    memory_limit_bytes=pa.memory_limit_bytes,
                )
                resp = admin_client.upload_problem(contest_id, zip_bytes, pa.problem_id)
                log.info("%s  Uploaded problem -> %s", W, resp)
            except requests.HTTPError as e:
                body = e.response.text if hasattr(e, 'response') and e.response is not None else ""
                log.warning("%s  Problem upload failed: %s | %s", W, e, body)
                _record_failure({
                    "index": pa.idx, "name": pa.problem_name, "worker": W.strip(),
                    "stage": "problem_upload", "error": str(e), "details": body,
                })
                continue
            except Exception as e:
                log.error("%s  Problem upload error: %s", W, e)
                _record_failure({
                    "index": pa.idx, "name": pa.problem_name, "worker": W.strip(),
                    "stage": "problem_upload", "error": str(e), "details": "",
                })
                continue

            float_tol = detect_float_tolerance(example.get("description", ""))
            if float_tol and db_config:
                set_float_tolerance_in_db(db_config, pa.problem_id, float_tol)

            solutions_data = example.get("solutions", {"language": [], "solution": []})
            lang_codes = solutions_data.get("language", [])
            sol_texts = solutions_data.get("solution", [])

            lang_solution_counts: dict[str, int] = {}
            for lc in lang_codes:
                _, lang_id = LANG_CODE_MAP.get(lc, ("Unknown", None))
                if lang_id is None:
                    continue
                lang_solution_counts[lang_id] = lang_solution_counts.get(lang_id, 0) + 1

            def _avg_runtime(s):
                runs = s.get("runs", [])
                if not runs:
                    return float("inf")
                return sum(
                    r["run_time"] if r.get("run_time") is not None else float("inf")
                    for r in runs
                ) / len(runs)

            def _select_by_strategy(ac_solutions, strategy, seed=42, problem_idx=0):
                if not ac_solutions:
                    return []
                sorted_sols = sorted(ac_solutions, key=_avg_runtime)
                if strategy == "fast_solution":
                    return sorted_sols
                elif strategy == "slow_solution":
                    return sorted_sols[::-1]
                elif strategy.startswith("random"):
                    n = int(strategy.replace("random", "").replace("_solution", ""))
                    rng = random.Random(seed + n + problem_idx * 1000)
                    if len(sorted_sols) <= 2:
                        shuffled = sorted_sols[:]
                        rng.shuffle(shuffled)
                        return shuffled
                    middle = sorted_sols[1:-1]
                    rng.shuffle(middle)
                    return middle + [sorted_sols[0], sorted_sols[-1]]
                return sorted_sols

            strategy = getattr(args, "solution_strategy", "fast_solution")

            ac_sol_ranked = None
            sel_problem = sel_solutions_by_idx.get(pa.idx)
            if sel_problem:
                ac_sol_ranked = {}
                for lang_id, sol_list in sel_problem.get("solutions", {}).items():
                    ac_solutions = [s for s in sol_list if s["verdict"] == "AC"]
                    if ac_solutions:
                        reordered = _select_by_strategy(ac_solutions, strategy, problem_idx=pa.idx)
                        ac_sol_ranked[lang_id] = [s["sol_idx"] for s in reordered]
                log.info("%s  Selected solutions (strategy=%s): %s",
                         W, strategy, {lid: len(s) for lid, s in ac_sol_ranked.items()})

            lang_buckets: dict[str, list[tuple[int, str]]] = {}
            if ac_sol_ranked is not None:
                for lang_id, ranked_indices in ac_sol_ranked.items():
                    bucket = []
                    for sol_idx in ranked_indices:
                        if len(bucket) >= args.max_solutions:
                            break
                        if sol_idx < len(sol_texts):
                            bucket.append((sol_idx, sol_texts[sol_idx]))
                    if bucket:
                        lang_buckets[lang_id] = bucket
            else:
                for sol_idx, (lc, sol) in enumerate(zip(lang_codes, sol_texts)):
                    _, lang_id = LANG_CODE_MAP.get(lc, ("Unknown", None))
                    if lang_id is None:
                        continue
                    bucket = lang_buckets.setdefault(lang_id, [])
                    if len(bucket) < args.max_solutions:
                        bucket.append((sol_idx, sol))

            problem_solutions = []

            for lang_id, sols in lang_buckets.items():
                lang_name = _LANG_ID_TO_NAME.get(lang_id, lang_id)
                for sub_idx, (sol_idx, source) in enumerate(sols):
                    if lang_id == "java":
                        source = re.sub(
                            r'\bpublic\s+class\s+\w+', 'public class Main',
                            source, count=1)
                    filename = make_filename(lang_id, sub_idx)
                    submission_id = None

                    if args.submit_delay > 0:
                        time.sleep(args.submit_delay)

                    try:
                        if throttle:
                            throttle.acquire(worker_tag=W)
                        for submit_attempt in range(3):
                            try:
                                sub = team_client.submit(
                                    contest_id, pa.problem_id, lang_id, source, filename)
                                submission_id = sub.get("id", sub.get("submission_id"))
                                log.info("%s  Submitted %s [%s] -> sid=%s",
                                         W, lang_id, filename, submission_id)
                                break
                            except (requests.HTTPError, requests.ConnectionError) as e:
                                body = ""
                                if hasattr(e, 'response') and e.response is not None:
                                    body = e.response.text
                                    if e.response.status_code < 500:
                                        log.warning("%s  Submit failed (%s): %s | %s",
                                                    W, lang_id, e, body)
                                        break
                                if submit_attempt < 2:
                                    backoff = 2 ** submit_attempt + random.random()
                                    log.info("%s  Submit retry %d/3 (%s): %s",
                                             W, submit_attempt + 1, lang_id, e)
                                    time.sleep(backoff)
                                else:
                                    log.warning("%s  Submit failed after 3 retries (%s): %s | %s",
                                                W, lang_id, e, body)

                        if submission_id is None:
                            problem_solutions.append({
                                "solution_index": sol_idx,
                                "language": lang_name, "language_id": lang_id,
                                "submission_id": None, "verdict": "SUBMIT_ERROR",
                                "error": "submit failed after retries", "runs": [],
                                "min_time": None, "max_time": None, "total_time": None,
                                "speed_tiers": None,
                            })
                            _record_failure({
                                "index": pa.idx, "name": pa.problem_name, "worker": W.strip(),
                                "stage": "submit", "error": "submit failed after retries",
                                "details": f"lang={lang_id}, sol_idx={sol_idx}",
                            })
                            continue

                        judgement = _wait_with_retry(
                            admin_client, contest_id, submission_id, args, worker_tag=W)
                    finally:
                        if throttle:
                            throttle.release()

                    runs_data = []
                    times = []
                    verdict = "PENDING"
                    min_time = max_time = total_time = None

                    if judgement:
                        verdict = judgement.get("judgement_type_id", "UNKNOWN")
                        max_time = judgement.get("max_run_time")
                        if db_config and submission_id:
                            runs_data = get_runs_from_db(db_config, submission_id)
                        times = [r["run_time"] for r in runs_data if r["run_time"] is not None]
                        if times:
                            min_time = min(times)
                            max_time = max(times)
                            total_time = sum(times)
                    else:
                        log.warning("%s  Judgement timeout for sid=%s", W, submission_id)
                        _record_failure({
                            "index": pa.idx, "name": pa.problem_name, "worker": W.strip(),
                            "stage": "judgement_timeout",
                            "error": f"judgement polling timeout for sid={submission_id}",
                            "details": f"lang={lang_id}, sol_idx={sol_idx}, sid={submission_id}",
                        })

                    speed_tiers = build_speed_tiers(times, runs_data) if times else None
                    problem_solutions.append({
                        "solution_index": sol_idx,
                        "language": lang_name, "language_id": lang_id,
                        "submission_id": submission_id, "verdict": verdict,
                        "min_time": min_time, "max_time": max_time,
                        "total_time": total_time, "speed_tiers": speed_tiers,
                        "runs": runs_data,
                    })

            all_times = [
                r["run_time"] for sol in problem_solutions
                for r in sol.get("runs", []) if r["run_time"] is not None
            ]
            problem_speed_tiers = build_speed_tiers(all_times)
            if problem_speed_tiers:
                problem_speed_tiers["total_runs"] = len(all_times)
                problem_speed_tiers["num_solutions"] = len(
                    [s for s in problem_solutions if s.get("runs")])

            problem_record = {
                "type": "problem",
                "index": pa.idx,
                "name": pa.problem_name,
                "problem_id": pa.problem_id,
                "timelimit": pa.timelimit,
                "num_test_cases": {
                    "public": n_public,
                    "private": n_private,
                    "generated": n_generated,
                },
                "language_solutions": lang_solution_counts,
                "speed_tiers": problem_speed_tiers,
                "solutions": problem_solutions,
            }

        problem_records.append(problem_record)
        if out_path:
            sols = problem_record.get("solutions", [])
            if sols and all(
                s.get("verdict") in ("PENDING", "", None) for s in sols
            ):
                log.warning(
                    "%s  [skip-write] All %d solution(s) timed out for [%d] %s — "
                    "not writing to JSONL (will retry on resume); see _failures.txt",
                    W, len(sols), pa.idx, pa.problem_name,
                )
            else:
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(problem_record, ensure_ascii=False) + "\n")

    return problem_records, failures

def process_split(admin_client: DOMjudgeClient, team_client: DOMjudgeClient,
                   ds_split, split_name: str, args, db_config: dict = None,
                   custom_tc: dict = None, out_path: Path = None,
                   selected_solutions: dict = None,
                   problem_indices: set = None,
                   contest_id_override: str = None,
                   worker_tag: str = "",
                   throttle: AdaptiveThrottle = None,
                   skip_local: bool = False) -> dict:
    W = worker_tag
    contest_id = contest_id_override or args.contest_id

    done_indices = set()
    if not args.reset and out_path and out_path.exists():
        try:
            loaded = _load_process_split_jsonl(out_path)
            contaminated_indices = []
            valid_problems = []
            for p in loaded.get("problems", []):
                sols = p.get("solutions", [])
                if not sols:
                    done_indices.add(p["index"])
                    valid_problems.append(p)
                elif any(s.get("verdict") not in ("PENDING", "", None) for s in sols):
                    done_indices.add(p["index"])
                    valid_problems.append(p)
                else:
                    contaminated_indices.append(p["index"])
            if done_indices:
                log.info("%sResuming: %d already-completed problems from %s",
                         W, len(done_indices), out_path)
            if contaminated_indices:
                log.warning("%sResume: %d problem(s) had all-PENDING solutions "
                            "(judgement timeout) — rewriting JSONL to drop them, "
                            "will be retried: %s%s",
                            W, len(contaminated_indices), contaminated_indices[:10],
                            " ..." if len(contaminated_indices) > 10 else "")
                tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
                meta = loaded.get("metadata", {})
                with open(tmp_path, "w", encoding="utf-8") as _wf:
                    if meta:
                        _wf.write(json.dumps({"type": "metadata", **meta},
                                             ensure_ascii=False) + "\n")
                    for p in valid_problems:
                        rec = {"type": "problem", **p} if "type" not in p else p
                        _wf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                os.replace(tmp_path, out_path)
                log.info("%sRewrote %s: %d valid problems retained, %d contaminated dropped",
                         W, out_path, len(valid_problems), len(contaminated_indices))
        except Exception as e:
            log.warning("%sCould not load previous results (%s), starting fresh", W, e)
            done_indices = set()

    if not done_indices and not args.reset and out_path:
        merged_json_path = out_path.parent / out_path.name.replace(".jsonl", ".json")
        if merged_json_path.exists():
            try:
                with open(merged_json_path) as _f:
                    prev = json.load(_f)
                for p in prev.get("problems", []):
                    sols = p.get("solutions", [])
                    all_judged = not sols or all(s.get("verdict") not in ("PENDING", "") for s in sols)
                    if all_judged:
                        done_indices.add(p["index"])
                if done_indices:
                    log.info("%sResume from merged JSON: %d problems fully judged (no PENDING), "
                             "will retry remaining", W, len(done_indices))
            except Exception as e:
                log.warning("%sCould not load merged JSON for resume (%s)", W, e)

    metadata = {
        "dataset": "deepmind/code_contests",
        "split": split_name,
        "source": args.source,
        "custom_testcases": args.custom_testcases,
        "default_timelimit": args.timelimit,
        "max_solutions": args.max_solutions,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if out_path and not done_indices:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "metadata", **metadata}, ensure_ascii=False) + "\n")

    results = {"metadata": metadata, "problems": []}

    failures = []
    failure_path = out_path.parent / out_path.name.replace(".jsonl", "_failures.txt") if out_path else None
    _failure_header_written = False

    def _record_failure(fail: dict):
        nonlocal _failure_header_written
        failures.append(fail)
        if failure_path:
            if not _failure_header_written:
                _init_failure_report(failure_path, "Step 2 Failure Report", split_name, W)
                _failure_header_written = True
            _append_failure(failure_path, fail, len(failures))

    custom_tc_lookup = {}
    if custom_tc:
        for entry in custom_tc:
            key = entry.get("name") or entry.get("problem_name") or entry.get("problem_id", "")
            custom_tc_lookup[key] = entry

    sel_solutions_by_idx = {}
    if selected_solutions:
        for sp in selected_solutions.get("problems", []):
            sel_solutions_by_idx[sp["index"]] = sp

    if skip_local:
        log.info("%s=== [Local] Skipped (retry) — loading expected outputs from disk cache ===", W)
        phase_a_cache = _rebuild_phase_a_cache_from_disk(args, split_name, problem_indices)
    else:
        log.info("%s=== [Local] Pre-generating expected outputs for all problems ===", W)
        phase_a_cache = _phase_a_generate_outputs(
            ds_split, split_name, args,
            custom_tc_lookup, sel_solutions_by_idx,
            problem_indices, done_indices, worker_tag=W,
        )
    gc.collect()

    log.info("%s=== [DOMjudge] Submission & Judgement (%d problems) ===", W, len(phase_a_cache))
    problem_records, phase_b_failures = _phase_b_domjudge_eval(
        admin_client, team_client, ds_split, split_name, args,
        db_config, phase_a_cache, sel_solutions_by_idx,
        contest_id, out_path, worker_tag=W, throttle=throttle,
    )
    results["problems"] = problem_records
    failures.extend(phase_b_failures)

    if failures and failure_path:
        _finalize_failure_report(failure_path, failures)
        log.info("%sFailure report saved -> %s (%d failures)", W, failure_path, len(failures))

    return results

def main():
    parser = argparse.ArgumentParser(description="CodeContests -> DOMjudge auto judge")
    parser.add_argument("--contest_id", default="dj-2")
    parser.add_argument("--admin_user", default="admin")
    parser.add_argument("--admin_password", default="changeme")
    parser.add_argument("--team_user", default="test_user")
    parser.add_argument("--team_password", default="changeme")
    parser.add_argument("--domjudge_url", default="http://localhost:50002",
                        help="Single domserver URL (used when --domjudge_urls is not set)")
    parser.add_argument("--domjudge_urls", default=None,
                        help="Comma-separated list of domserver URLs (one per worker); problems split by range")
    parser.add_argument("--db_port_base", type=int, default=50034,
                        help="First DB port for multi-URL mode (instance i uses db_port_base+i)")
    parser.add_argument("--cache_dir", default=None)
    parser.add_argument("--split", default="test", help="train/test/valid/all")
    parser.add_argument("--output_dir", default="Research_start_code/domjudge/results")
    parser.add_argument("--expected_output_cache_dir", default=None,
                        help="Override the per-language expected-output cache "
                             "root directory. When unset, defaults to "
                             "{output_dir}/../expected_output_cache (cache lives "
                             "alongside the timing results). Override (or set "
                             "the EXPECTED_OUTPUT_CACHE_DIR env var) to relocate "
                             "the cache — useful when home is quota-limited and "
                             "the cache should live on a shared filesystem like "
                             "/path/to/shared/expected_output_cache.")
    parser.add_argument("--source", default="dataset",
                        help="Test case source: 'dataset' (original code_contests) or custom name (e.g., 'llm_baseline', 'our_method')")
    parser.add_argument("--custom_testcases", default=None,
                        help="Path to custom test cases JSON file (required when --source is not 'dataset')")
    parser.add_argument("--inputs_dir", default=None,
                        help="Path to inputs/ directory produced by slow_testcase_generator.py. "
                             "When set, stdin data is lazy-loaded per-problem from this directory "
                             "instead of being read from --custom_testcases, avoiding the need to "
                             "rebuild and re-save the full JSON before judging.")
    parser.add_argument("--parsed_structures_dir", default=None,
                        help="Path to parsed_structures/ directory (e.g. dataset/parsed_structures). "
                             "Required to expand boundary_slow_compact TCs (stdin=null) from "
                             "--inputs_dir in-memory before submission. If omitted, compact TCs "
                             "are submitted with empty stdin and will receive WA.")
    parser.add_argument("--method_filter", default=None,
                        help="Only submit TCs whose method field contains this substring. "
                             "E.g. 'boundary' for M1-only, 'algorithmic' for M2-only. "
                             "Applies only when --inputs_dir is used.")
    parser.add_argument("--timelimit", type=int, default=2, help="Time limit per problem (sec)")
    parser.add_argument("--max_solutions", type=int, default=1, help="Max solutions per language per problem (Step 2)")
    parser.add_argument("--solution_strategy", type=str, default="fast_solution",
                        choices=["fast_solution", "slow_solution",
                                 "random1_solution", "random2_solution", "random3_solution",
                                 "wedge_solution", "evalperf_sas_solution"],
                        help="Solution selection strategy for Step 2: "
                             "fast_solution (default, fastest AC), slow_solution (slowest AC), "
                             "random{1,2,3}_solution (deterministic random with seed 42+N), "
                             "wedge_solution / evalperf_sas_solution (single AC solution per "
                             "problem from method-specific picks; loads "
                             "selected_solutions_{strategy}_{split}.jsonl pre-built by "
                             "build_method_selected_solutions.py)")
    parser.add_argument("--sel_max_solutions", type=int, default=0,
                        help="Max solutions per language in pre-screening Step 1 (0 = all)")
    parser.add_argument("--max_problems", type=int, default=0, help="Max problems to process (0 = all)")
    parser.add_argument("--poll_interval", type=float, default=2, help="Judgement polling interval (sec)")
    parser.add_argument("--poll_timeout", type=float, default=600, help="Judgement polling timeout (sec)")
    parser.add_argument("--submit_delay", type=float, default=1.0,
                        help="Delay (sec) between submissions within a worker to avoid queue flooding")
    parser.add_argument("--max_judge_retries", type=int, default=1,
                        help="Extra wait_judgement attempts on timeout (0=no retry)")
    parser.add_argument("--db_host", default="localhost", help="MariaDB host for direct DB fallback")
    parser.add_argument("--db_port", type=int, default=50001, help="MariaDB port")
    parser.add_argument("--db_user", default="domjudge", help="MariaDB user")
    parser.add_argument("--db_password", default="domjudge", help="MariaDB password")
    parser.add_argument("--db_name", default="domjudge", help="MariaDB database name")
    parser.add_argument("--db_ssl_disabled", action="store_true", default=True,
                        help="Disable SSL for MariaDB connection (default: True)")
    parser.add_argument("--reset", action="store_true", help="Start from scratch (ignore previous results)")
    parser.add_argument("--skip_problems", default="",
                        help="Comma-separated list of problem names to skip in Phase A "
                             "(e.g. '1607_E. Robot on the Board 1'). "
                             "Useful to avoid OOM on known problematic problems.")
    parser.add_argument("--excluded_path", default=None,
                        help="Path to dataset/excluded_problems.json. When provided, "
                             "the per-split exclusion list (interactive + special_judge) "
                             "is added to --skip_problems automatically for fair "
                             "cross-method comparison.")
    parser.add_argument("--no_output_cache", action="store_true",
                        help="Disable disk cache for expected outputs (always re-run solutions)")
    parser.add_argument("--tc_sample_ratio", type=float, default=0.1,
                        help="Fraction of test cases to sample for solution pre-screening (default: 0.1)")
    parser.add_argument("--tc_largest_ratio", type=float, default=0.5,
                        help="Fraction of sampled TCs chosen by largest input length (default: 0.5)")
    parser.add_argument("--max_pending", type=int, default=0,
                        help="Max concurrent pending submissions across all workers (0=auto, uses max_concurrent)")
    parser.add_argument("--max_concurrent", type=int, default=1,
                        help="Number of parallel workers (1=sequential, >1=contest-per-judgehost parallel)")
    parser.add_argument("--train_sample_ratio", type=float, default=1.0,
                        help="Fraction of train split to use (0.0-1.0, default: 1.0 = all)")
    parser.add_argument("--train_sample_seed", type=int, default=42,
                        help="Random seed for reproducible train split sampling (default: 42)")
    parser.add_argument("--max_phase_retries", type=int, default=5,
                        help="Max retry attempts per phase when submissions fail (default: 5)")
    args = parser.parse_args()

    args._skip_problems: set = {
        s.strip() for s in args.skip_problems.split(",") if s.strip()
    }
    if args.excluded_path:
        try:
            with open(args.excluded_path) as _f:
                _ex_data = json.load(_f)
            for split in args.split.split(","):
                _split = split.strip()
                if _split and _split != "all":
                    for entry in _ex_data.get(_split, []) or []:
                        nm = entry.get("name")
                        if nm:
                            args._skip_problems.add(nm)
                elif _split == "all":
                    for sp in ("test", "valid", "train"):
                        for entry in _ex_data.get(sp, []) or []:
                            nm = entry.get("name")
                            if nm:
                                args._skip_problems.add(nm)
            log.info("Augmented skip list from %s (now %d problems)",
                     args.excluded_path, len(args._skip_problems))
        except Exception as _e:
            log.warning("Could not load excluded_path %s: %s",
                        args.excluded_path, _e)
    if args._skip_problems:
        log.info("Skip list (%d problems): %s", len(args._skip_problems), args._skip_problems)

    try:
        with open("/proc/self/oom_score_adj", "w") as _oom_f:
            _oom_f.write("-100")
    except Exception:
        pass

    output_dir = Path(args.output_dir) / args.source
    if args.source == "dataset":
        output_dir = output_dir / getattr(args, "solution_strategy", "fast_solution")
    output_dir.mkdir(parents=True, exist_ok=True)

    log_dir = Path(args.output_dir).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    splits_tag = args.split.replace(",", "_")
    log_file = log_dir / f"judge_{splits_tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(fh)
    log.info("Log file: %s", log_file)

    custom_tc = None
    if args.source != "dataset":
        if not args.custom_testcases and not args.inputs_dir:
            log.error("--custom_testcases or --inputs_dir is required when --source is not 'dataset'")
            return
        if args.custom_testcases:
            tc_path = Path(args.custom_testcases)
            if not tc_path.exists():
                log.error("Custom test cases file not found: %s", tc_path)
                return
            log.info("Loading custom test cases from: %s", tc_path)
            with open(tc_path, "r", encoding="utf-8") as f:
                custom_tc = json.load(f)
            if isinstance(custom_tc, list):
                for entry in custom_tc:
                    tc_data = entry.get("test_cases") or entry.get("testcases")
                    if tc_data is not None and "test_cases" not in entry:
                        if isinstance(tc_data, list):
                            entry["test_cases"] = [
                                {"input": t.get("stdin", t.get("input", "")), "expected_output": t.get("expected_output", "")}
                                for t in tc_data
                            ]
                        elif isinstance(tc_data, dict) and any(k in tc_data for k in ("fast", "medium", "slow")):
                            entry["test_cases"] = {
                                tier: [{"input": t.get("stdin", t.get("input", "")), "expected_output": t.get("expected_output", "")} for t in tc_data.get(tier, [])]
                                for tier in ("fast", "medium", "slow")
                            }
                        else:
                            entry["test_cases"] = tc_data
            log.info("  Loaded %d problem entries", len(custom_tc))

    args._inputs_dir_path = None
    args._inputs_dir_index = set()
    args._inputs_dir_name_files = {}
    if args.inputs_dir:
        _idp = Path(args.inputs_dir)
        if not _idp.exists():
            log.error("--inputs_dir not found: %s", _idp)
            return
        for _tier in ("fast", "medium", "slow"):
            _tier_dir = _idp / _tier
            if not _tier_dir.exists():
                continue
            for _fp in _tier_dir.iterdir():
                if _fp.suffix != ".json":
                    continue
                _stem4 = _fp.name[:4]
                _underscore_at_4 = len(_fp.name) > 4 and _fp.name[4] == "_"
                _indexed_by_int = False
                if _underscore_at_4 and _stem4.isdigit():
                    _candidate = int(_stem4)
                    if _stem4[0] == "0" or _candidate < 1000:
                        args._inputs_dir_index.add(_candidate)
                        _indexed_by_int = True
                if not _indexed_by_int:
                    try:
                        with open(_fp, "r", encoding="utf-8") as _f:
                            _data = json.load(_f)
                        _name = _data.get("name")
                        if _name:
                            args._inputs_dir_name_files.setdefault(_name, []).append(_fp)
                    except (OSError, json.JSONDecodeError, ValueError):
                        continue
        args._inputs_dir_path = _idp
        log.info("inputs_dir: %s (%d int-prefix indices, %d name-keyed problems)",
                 _idp, len(args._inputs_dir_index), len(args._inputs_dir_name_files))

    domjudge_urls_list = None
    if getattr(args, "domjudge_urls", None) and str(args.domjudge_urls).strip():
        domjudge_urls_list = [u.strip().rstrip("/") for u in args.domjudge_urls.split(",") if u.strip()]
        if domjudge_urls_list:
            args.domjudge_url = domjudge_urls_list[0]
        log.info("Multi-URL mode: %d domserver(s) %s", len(domjudge_urls_list), domjudge_urls_list[:3])
    else:
        args.domjudge_url = (args.domjudge_url or "").rstrip("/")

    base_url = (domjudge_urls_list[0] if domjudge_urls_list else args.domjudge_url)
    admin = DOMjudgeClient(base_url, args.admin_user, args.admin_password)
    admin._team_password = args.team_password

    team = DOMjudgeClient(base_url, args.team_user, args.team_password)

    team_id = admin.ensure_team_for_user(args.team_user, args.contest_id)
    log.info("Team ID for '%s': %s", args.team_user, team_id)

    log.info("Loading deepmind/code_contests (cache_dir=%s) ...", args.cache_dir)
    ds = load_dataset("deepmind/code_contests", cache_dir=args.cache_dir)

    splits = ["train", "test", "valid"] if args.split == "all" else [args.split]

    for split_name in splits:
        if split_name not in ds:
            log.warning("Split '%s' not found in dataset, skipping.", split_name)
            continue

        ds_split = ds[split_name]
        if split_name == "train" and 0 < args.train_sample_ratio < 1.0:
            full_len = len(ds_split)
            sample_size = max(1, int(full_len * args.train_sample_ratio))
            rng = random.Random(args.train_sample_seed)
            sample_indices = sorted(rng.sample(range(full_len), sample_size))
            ds_split = ds_split.select(sample_indices)
            log.info("Train sampling: %d/%d problems (ratio=%.2f, seed=%d)",
                     sample_size, full_len, args.train_sample_ratio, args.train_sample_seed)

        log.info("=== Processing split: %s | source: %s (%d problems) ===",
                 split_name, args.source, len(ds_split))

        args._compact_m1_features = {}
        if getattr(args, 'inputs_dir', None) and getattr(args, 'parsed_structures_dir', None):
            _psd = Path(args.parsed_structures_dir) / f"{split_name}.json"
            if _psd.exists():
                try:
                    with open(_psd, encoding='utf-8') as _f:
                        for _k, _v in json.load(_f).items():
                            args._compact_m1_features[int(_k)] = (
                                _v.get('constraints_parsed', []),
                                _v.get('structure', {}),
                            )
                    log.info("compact M1 features: %d problems loaded from %s",
                             len(args._compact_m1_features), _psd)
                except Exception as _e:
                    log.warning("Failed to load parsed_structures for compact M1 expansion: %s", _e)
            else:
                log.warning("--parsed_structures_dir set but %s not found; "
                            "boundary_slow_compact TCs will get empty stdin", _psd)

        db_config = {
            "host": args.db_host,
            "port": args.db_port,
            "user": args.db_user,
            "password": args.db_password,
            "database": args.db_name,
            "ssl_disabled": args.db_ssl_disabled,
        }

        if not db_config:
            log.error("db_config is required (needed to prevent TC accumulation via DB-level problem deletion)")
            sys.exit(1)

        n_problems = len(ds_split)
        if args.max_problems > 0:
            n_problems = min(n_problems, args.max_problems)

        assignments = None
        use_parallel = args.max_concurrent > 1
        multi_url_mode = bool(domjudge_urls_list and len(domjudge_urls_list) >= 1)

        if use_parallel:
            num_requested = min(args.max_concurrent, len(domjudge_urls_list)) if domjudge_urls_list else args.max_concurrent
            log.info("=== Parallel mode: %d workers requested (multi_url=%s) ===", num_requested, multi_url_mode)

            if multi_url_mode:
                assignments = setup_contests_multi_url(
                    domjudge_urls_list[: num_requested],
                    args.db_host,
                    getattr(args, "db_port_base", 50034),
                    args.db_user,
                    args.db_password,
                    args.db_name,
                    args.contest_id,
                    args.team_user,
                    ssl_disabled=args.db_ssl_disabled,
                )
            else:
                assignments = setup_parallel_contests(
                    db_config, admin, args.contest_id, args.team_user,
                    num_requested,
                )
            if not assignments:
                log.error("Parallel setup failed. Falling back to sequential mode.")
                use_parallel = False

        if use_parallel and assignments:
            num_workers = len(assignments)
            required_indices = set(range(n_problems))
            MAX_PHASE_RETRIES = args.max_phase_retries

            sel_dir = Path(args.output_dir) / "selected_solutions"
            if args.source == "dataset":
                sel_dir.mkdir(parents=True, exist_ok=True)
            merged_sel_path = sel_dir / f"selected_solutions_{split_name}.jsonl"
            merged_timing_path = output_dir / f"codecontests_timing_{split_name}.json"

            worker_sel_paths = []
            worker_main_paths = []
            for i in range(num_workers):
                sp = sel_dir / f"selected_solutions_{split_name}_w{i}.jsonl"
                mp = output_dir / f"codecontests_timing_{split_name}_w{i}.jsonl"
                if args.reset:
                    if sp.exists(): sp.unlink()
                    if mp.exists(): mp.unlink()
                worker_sel_paths.append(sp)
                worker_main_paths.append(mp)

            admin_auth = (args.admin_user, args.admin_password)
            team_auth = (args.team_user, args.team_password)
            max_p = args.max_pending if args.max_pending > 0 else num_workers

            try:
                preloaded_sel = None
                for phase1_attempt in range(1, MAX_PHASE_RETRIES + 1):
                    if args.source == "dataset" and merged_sel_path.exists() and not args.reset:
                        try:
                            preloaded_sel = _load_selected_solutions_jsonl(merged_sel_path)
                            done_indices = {p["index"] for p in preloaded_sel.get("problems", [])}
                            missing = required_indices - done_indices
                            if not missing:
                                log.info("=== Step 1 COMPLETE (%d/%d problems, %d AC) -> SKIP ===",
                                         len(done_indices), n_problems,
                                         preloaded_sel["metadata"].get("total_ac", 0))
                                break
                            else:
                                log.info("=== Step 1 attempt %d/%d: %d/%d done, %d missing ===",
                                         phase1_attempt, MAX_PHASE_RETRIES,
                                         len(done_indices), n_problems, len(missing))
                                preloaded_sel = None
                        except Exception as e:
                            log.warning("Could not load merged selected_solutions (%s)", e)
                            preloaded_sel = None

                    if preloaded_sel:
                        break

                    p1_worker_indices = [set() for _ in range(num_workers)]
                    p1_missing = required_indices.copy()
                    if merged_sel_path.exists() and not args.reset:
                        try:
                            prev = _load_selected_solutions_jsonl(merged_sel_path)
                            p1_missing -= {p["index"] for p in prev.get("problems", [])}
                        except Exception:
                            pass
                    if multi_url_mode:
                        chunk = (len(p1_missing) + num_workers - 1) // num_workers if num_workers else 0
                        sorted_missing = sorted(p1_missing)
                        for wi in range(num_workers):
                            start = wi * chunk
                            end = min((wi + 1) * chunk, len(sorted_missing))
                            for j in range(start, end):
                                p1_worker_indices[wi].add(sorted_missing[j])
                    else:
                        for idx in sorted(p1_missing):
                            p1_worker_indices[idx % num_workers].add(idx)

                    if not any(p1_worker_indices):
                        break

                    throttle = AdaptiveThrottle(max_pending=max_p, base_delay=2.0)
                    log.info("Step 1 attempt %d/%d: %d problems to process, throttle=%d",
                             phase1_attempt, MAX_PHASE_RETRIES, len(p1_missing), max_p)

                    with ThreadPoolExecutor(max_workers=num_workers) as executor:
                        futures = {}
                        for i, a in enumerate(assignments):
                            if not p1_worker_indices[i]:
                                continue
                            f = executor.submit(
                                _run_worker,
                                worker_id=i,
                                contest_id=a["contest_id"],
                                domjudge_url=a.get("domjudge_url") or args.domjudge_url,
                                admin_auth=admin_auth,
                                team_auth=team_auth,
                                ds_split=ds_split,
                                split_name=split_name,
                                args=args,
                                db_config=a.get("db_config") or db_config,
                                custom_tc=custom_tc,
                                problem_indices=p1_worker_indices[i],
                                selected_solutions=None,
                                sel_out_path=worker_sel_paths[i],
                                main_out_path=worker_main_paths[i],
                                throttle=throttle,
                            )
                            futures[f] = i
                        for f in as_completed(futures):
                            try:
                                f.result()
                            except Exception as e:
                                log.error("[Worker %d] Step 1 failed: %s", futures[f], e)

                    merged_sel = _merge_selected_solutions(worker_sel_paths)
                    if merged_sel_path.exists():
                        try:
                            prev_sel = _load_selected_solutions_jsonl(merged_sel_path)
                            new_sel_indices = {p["index"] for p in merged_sel.get("problems", [])}
                            for p in prev_sel.get("problems", []):
                                if p["index"] not in new_sel_indices:
                                    merged_sel["problems"].append(p)
                            merged_sel["problems"].sort(key=lambda p: p.get("index", 0))
                        except Exception:
                            pass
                    total_ac = sum(
                        1 for p in merged_sel["problems"]
                        for sols in p.get("solutions", {}).values()
                        for s in sols if s.get("verdict") == "AC"
                    )
                    merged_sel["metadata"]["total_ac"] = total_ac

                    with open(merged_sel_path, "w", encoding="utf-8") as f:
                        f.write(json.dumps({"type": "metadata", **merged_sel["metadata"]},
                                           ensure_ascii=False) + "\n")
                        for p in merged_sel["problems"]:
                            f.write(json.dumps({"type": "problem", **p},
                                               ensure_ascii=False) + "\n")

                    sel_done = {p["index"] for p in merged_sel["problems"]}
                    sel_missing = required_indices - sel_done
                    log.info("Step 1 attempt %d result: %d/%d done, %d missing",
                             phase1_attempt, len(sel_done), n_problems, len(sel_missing))

                    if not sel_missing:
                        for sp in worker_sel_paths:
                            if sp.exists(): sp.unlink()
                        log.info("Step 1 COMPLETE after %d attempt(s). Cleaned up worker files.",
                                 phase1_attempt)
                        preloaded_sel = merged_sel
                        break

                _merge_failure_reports(
                    [sp.parent / sp.name.replace(".jsonl", "_failures.txt") for sp in worker_sel_paths],
                    sel_dir / f"selected_solutions_{split_name}_failures.txt",
                    "Merged Step 1 Failure Report", split_name, num_workers,
                )

                if not preloaded_sel and args.source == "dataset":
                    try:
                        preloaded_sel = _load_selected_solutions_jsonl(merged_sel_path)
                    except Exception:
                        pass
                    if preloaded_sel:
                        sel_count = len(preloaded_sel.get("problems", []))
                        log.warning("Step 1 incomplete after %d retries (%d/%d). "
                                    "Proceeding to Step 2 with partial data.",
                                    MAX_PHASE_RETRIES, sel_count, n_problems)
                    else:
                        log.error("Step 1 failed completely after %d retries. "
                                  "Cannot proceed to Step 2.", MAX_PHASE_RETRIES)

                result = None
                for phase2_attempt in range(1, MAX_PHASE_RETRIES + 1):
                    phase2_done_indices = set()
                    if merged_timing_path.exists() and not args.reset:
                        try:
                            with open(merged_timing_path) as _f:
                                prev_timing = json.load(_f)
                            for p in prev_timing.get("problems", []):
                                sols = p.get("solutions", [])
                                all_judged = not sols or all(s.get("verdict") not in ("PENDING", "") for s in sols)
                                if all_judged:
                                    phase2_done_indices.add(p["index"])
                            if phase2_done_indices == required_indices:
                                log.info("=== Step 2 COMPLETE (%d/%d problems) -> SKIP ===",
                                         len(phase2_done_indices), n_problems)
                                result = prev_timing
                                break
                        except Exception as e:
                            log.warning("Could not load merged timing (%s)", e)

                    p2_remaining = required_indices - phase2_done_indices
                    if not p2_remaining:
                        break

                    p2_worker_indices = [set() for _ in range(num_workers)]
                    if multi_url_mode:
                        sorted_rem = sorted(p2_remaining)
                        chunk = (len(sorted_rem) + num_workers - 1) // num_workers if num_workers else 0
                        for wi in range(num_workers):
                            start, end = wi * chunk, min((wi + 1) * chunk, len(sorted_rem))
                            for j in range(start, end):
                                p2_worker_indices[wi].add(sorted_rem[j])
                    else:
                        for idx in sorted(p2_remaining):
                            p2_worker_indices[idx % num_workers].add(idx)

                    throttle = AdaptiveThrottle(max_pending=max_p, base_delay=2.0)
                    log.info("=== Step 2 attempt %d/%d: %d/%d done, %d to process, throttle=%d ===",
                             phase2_attempt, MAX_PHASE_RETRIES,
                             len(phase2_done_indices), n_problems, len(p2_remaining), max_p)

                    with ThreadPoolExecutor(max_workers=num_workers) as executor:
                        futures = {}
                        for i, a in enumerate(assignments):
                            if not p2_worker_indices[i]:
                                continue
                            f = executor.submit(
                                _run_worker,
                                worker_id=i,
                                contest_id=a["contest_id"],
                                domjudge_url=a.get("domjudge_url") or args.domjudge_url,
                                admin_auth=admin_auth,
                                team_auth=team_auth,
                                ds_split=ds_split,
                                split_name=split_name,
                                args=args,
                                db_config=a.get("db_config") or db_config,
                                custom_tc=custom_tc,
                                problem_indices=p2_worker_indices[i],
                                selected_solutions=preloaded_sel,
                                sel_out_path=worker_sel_paths[i],
                                main_out_path=worker_main_paths[i],
                                throttle=throttle,
                            )
                            futures[f] = i
                        for f in as_completed(futures):
                            try:
                                f.result()
                            except Exception as e:
                                log.error("[Worker %d] Step 2 failed: %s", futures[f], e)

                    result = _merge_worker_results_jsonl(worker_main_paths, split_name, args)

                    if phase2_done_indices and merged_timing_path.exists():
                        try:
                            with open(merged_timing_path) as _f:
                                prev_merged = json.load(_f)
                            new_indices = {p["index"] for p in result.get("problems", [])}
                            for p in prev_merged.get("problems", []):
                                if p["index"] in phase2_done_indices and p["index"] not in new_indices:
                                    result["problems"].append(p)
                            result["problems"].sort(key=lambda p: p.get("index", 0))
                        except Exception:
                            pass

                    with open(merged_timing_path, "w", encoding="utf-8") as _f:
                        json.dump(result, _f, indent=2, ensure_ascii=False)

                    result_ac = {p["index"] for p in result.get("problems", [])
                                 if not p.get("solutions", [])
                                 or all(s.get("verdict") not in ("PENDING", "") for s in p.get("solutions", []))}
                    still_missing = required_indices - result_ac
                    log.info("Step 2 attempt %d result: %d/%d fully judged (no PENDING), %d still pending",
                             phase2_attempt, len(result_ac), n_problems, len(still_missing))

                    if not still_missing:
                        for wp in worker_main_paths:
                            if wp.exists(): wp.unlink()
                        log.info("Step 2 COMPLETE after %d attempt(s). Cleaned up worker files.",
                                 phase2_attempt)
                        break

                if result is None:
                    log.error("Step 2 produced no results after %d retries.", MAX_PHASE_RETRIES)

                _merge_failure_reports(
                    [mp.parent / mp.name.replace(".jsonl", "_failures.txt") for mp in worker_main_paths],
                    output_dir / f"codecontests_timing_{split_name}_failures.txt",
                    "Merged Step 2 Failure Report", split_name, num_workers,
                )

            finally:
                cleanup_parallel_contests(db_config, assignments, multi_url=multi_url_mode)

        else:
            MAX_PHASE_RETRIES = args.max_phase_retries
            required_indices = set(range(n_problems))
            max_p = args.max_pending if args.max_pending > 0 else 1

            sel_dir = Path(args.output_dir) / "selected_solutions"
            if args.source == "dataset":
                sel_dir.mkdir(parents=True, exist_ok=True)
            sel_path = sel_dir / f"selected_solutions_{split_name}.jsonl"
            if args.reset and sel_path.exists():
                log.info("Reset mode: removing previous selected_solutions %s", sel_path)
                sel_path.unlink()

            selected_solutions = None
            if args.source != "dataset":
                _strategy = getattr(args, "solution_strategy", "fast_solution")
                if _strategy in ("wedge_solution", "evalperf_sas_solution"):
                    _sel_filename = f"selected_solutions_{_strategy}_{split_name}.jsonl"
                else:
                    _sel_filename = f"selected_solutions_{split_name}.jsonl"
                _strategy_sel_path = sel_dir / _sel_filename
                _fallback = (Path(args.output_dir).parent.parent
                             / "selected_solutions"
                             / _sel_filename)
                for _cand in [_strategy_sel_path, _fallback]:
                    if _cand.exists():
                        try:
                            selected_solutions = _load_selected_solutions_jsonl(_cand)
                            log.info("Loaded pre-built selected_solutions from %s "
                                     "(%d problems, %d AC)",
                                     _cand,
                                     len(selected_solutions.get("problems", [])),
                                     selected_solutions.get("metadata", {}).get("total_ac", 0))
                            break
                        except Exception as _e:
                            log.warning("Could not load selected_solutions from %s: %s", _cand, _e)
                if not selected_solutions:
                    log.warning("No pre-built selected_solutions found for split '%s' "
                                "(strategy=%s, expected file: %s); "
                                "expected output generation will fail. "
                                "Run build_method_selected_solutions.py for method-picked "
                                "strategies, or run with --source dataset first to build the "
                                "standard file.", split_name, _strategy, _sel_filename)
            elif args.source == "dataset":
                for phase1_attempt in range(1, MAX_PHASE_RETRIES + 1):
                    if sel_path.exists() and not args.reset:
                        try:
                            cached_sel = _load_selected_solutions_jsonl(sel_path)
                            done_indices = {p["index"] for p in cached_sel.get("problems", [])}
                            missing = required_indices - done_indices
                            if not missing:
                                selected_solutions = cached_sel
                                log.info("=== Step 1 COMPLETE (%d/%d problems, %d AC) -> SKIP ===",
                                         len(done_indices),
                                         cached_sel["metadata"].get("total_ac", 0),
                                         n_problems)
                                break
                            else:
                                log.info("=== Step 1 attempt %d/%d: %d/%d done, %d missing ===",
                                         phase1_attempt, MAX_PHASE_RETRIES,
                                         len(done_indices), n_problems, len(missing))
                        except Exception as e:
                            log.warning("Could not load cached selected_solutions (%s)", e)

                    throttle = AdaptiveThrottle(max_pending=max_p, base_delay=2.0)
                    sel_limit_str = str(args.sel_max_solutions) if args.sel_max_solutions > 0 else "all"
                    log.info("Step 1 attempt %d/%d: Pre-screening (sel_max_solutions=%s)",
                             phase1_attempt, MAX_PHASE_RETRIES, sel_limit_str)
                    selected_solutions = build_selected_solutions(
                        admin, team, ds_split, split_name, args,
                        db_config=db_config, out_path=sel_path,
                        throttle=throttle,
                    )

                    sel_done = {p["index"] for p in selected_solutions.get("problems", [])}
                    sel_missing = required_indices - sel_done
                    ac_count = selected_solutions.get("metadata", {}).get("total_ac", 0)
                    log.info("Step 1 attempt %d result: %d/%d done (%d AC), %d missing",
                             phase1_attempt, len(sel_done), n_problems, ac_count, len(sel_missing))

                    if not sel_missing:
                        log.info("Step 1 COMPLETE after %d attempt(s).", phase1_attempt)
                        break

                if selected_solutions:
                    sel_count = len(selected_solutions.get("problems", []))
                    if sel_count < n_problems:
                        log.warning("Step 1 incomplete after %d retries (%d/%d). "
                                    "Proceeding to Step 2 with partial data.",
                                    MAX_PHASE_RETRIES, sel_count, n_problems)

            jsonl_path = output_dir / f"codecontests_timing_{split_name}.jsonl"
            merged_timing_path = output_dir / f"codecontests_timing_{split_name}.json"
            if args.reset and jsonl_path.exists():
                log.info("Reset mode: removing previous results %s", jsonl_path)
                jsonl_path.unlink()

            result = None
            pending_indices = None
            for phase2_attempt in range(1, MAX_PHASE_RETRIES + 1):
                if merged_timing_path.exists() and not args.reset:
                    try:
                        with open(merged_timing_path) as _f:
                            prev_timing = json.load(_f)
                        done_ac = set()
                        for p in prev_timing.get("problems", []):
                            sols = p.get("solutions", [])
                            all_judged = not sols or all(s.get("verdict") not in ("PENDING", "") for s in sols)
                            if all_judged:
                                done_ac.add(p["index"])
                        if done_ac == required_indices:
                            log.info("=== Step 2 COMPLETE (%d/%d problems) -> SKIP ===",
                                     len(done_ac), n_problems)
                            result = prev_timing
                            break
                        else:
                            log.info("=== Step 2 attempt %d/%d: %d/%d done, %d to process ===",
                                     phase2_attempt, MAX_PHASE_RETRIES,
                                     len(done_ac), n_problems, len(required_indices - done_ac))
                    except Exception as e:
                        log.warning("Could not load merged timing (%s)", e)

                throttle = AdaptiveThrottle(max_pending=max_p, base_delay=2.0)
                log.info("Step 2 attempt %d/%d: Main evaluation (full TCs)",
                         phase2_attempt, MAX_PHASE_RETRIES)
                result = process_split(admin, team, ds_split, split_name, args,
                                       db_config, custom_tc, jsonl_path, selected_solutions,
                                       problem_indices=pending_indices,
                                       throttle=throttle,
                                       skip_local=(phase2_attempt > 1))

                if jsonl_path and jsonl_path.exists():
                    try:
                        jsonl_loaded = _load_process_split_jsonl(jsonl_path)
                        new_indices = {p["index"] for p in result.get("problems", [])}
                        for p in jsonl_loaded.get("problems", []):
                            if p["index"] not in new_indices:
                                result["problems"].append(p)
                        result["problems"].sort(key=lambda p: p.get("index", 0))
                    except Exception as _e:
                        log.warning("Could not merge JSONL into result: %s", _e)

                with open(merged_timing_path, "w", encoding="utf-8") as _f:
                    json.dump(result, _f, indent=2, ensure_ascii=False)

                result_ac = {p["index"] for p in result.get("problems", [])
                             if not p.get("solutions", [])
                             or all(s.get("verdict") not in ("PENDING", "") for s in p.get("solutions", []))}
                still_missing = required_indices - result_ac
                log.info("Step 2 attempt %d result: %d/%d fully judged (no PENDING), %d still pending",
                         phase2_attempt, len(result_ac), n_problems, len(still_missing))

                if not still_missing:
                    log.info("Step 2 COMPLETE after %d attempt(s).", phase2_attempt)
                    break
                pending_indices = still_missing

            if result is None:
                log.error("Step 2 produced no results after %d retries.", MAX_PHASE_RETRIES)

        if result:
            out_path = output_dir / f"codecontests_timing_{split_name}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            log.info("Results saved -> %s", out_path)

            total_subs = sum(len(p["solutions"]) for p in result["problems"])
            verdicts = {}
            for p in result["problems"]:
                for s in p["solutions"]:
                    v = s["verdict"]
                    verdicts[v] = verdicts.get(v, 0) + 1

            result_ac = {p["index"] for p in result["problems"]
                         if not p.get("solutions", [])
                         or all(s.get("verdict") not in ("PENDING", "") for s in p.get("solutions", []))}
            final_missing = set(range(n_problems)) - result_ac

            log.info("Split '%s': %d problems, %d submissions, verdicts=%s",
                     split_name, len(result["problems"]), total_subs, verdicts)

            if final_missing:
                fail_path = output_dir / f"codecontests_timing_{split_name}_final_failures.txt"
                with open(fail_path, "w") as ff:
                    ff.write(f"Final Failure Report for split '{split_name}'\n")
                    ff.write(f"Total: {n_problems}, Complete: {len(result_ac)}, "
                             f"Failed: {len(final_missing)}\n")
                    ff.write(f"Failed indices: {sorted(final_missing)}\n\n")
                    for p in result["problems"]:
                        if p["index"] in final_missing:
                            sols_info = []
                            for s in p.get("solutions", []):
                                sols_info.append(f"  {s.get('language','?')}: "
                                                 f"verdict={s.get('verdict','?')}")
                            ff.write(f"Problem {p['index']} ({p.get('name', '?')}):\n")
                            ff.write("\n".join(sols_info) + "\n\n")
                log.warning("Final failures saved -> %s (%d problems still incomplete)",
                            fail_path, len(final_missing))

                tc_info_lookup = {}
                _tc_idp = getattr(args, "_inputs_dir_path", None)
                if custom_tc:
                    for entry in custom_tc:
                        p_name = (entry.get("name") or entry.get("problem_name")
                                  or entry.get("problem_id", ""))
                        ordinal_map = {}
                        if _tc_idp:
                            p_idx = entry.get("index", -1)
                            idx_prefix = f"{p_idx:04d}"
                            ordinal = 1
                            for tier in ("fast", "medium", "slow"):
                                tier_dir = _tc_idp / tier
                                if not tier_dir.exists():
                                    continue
                                for fpath in sorted(tier_dir.glob(f"{idx_prefix}_*.json")):
                                    ordinal_map[ordinal] = {"tier": tier, "input": ""}
                                    ordinal += 1
                        else:
                            tc_data = entry.get("test_cases", {})
                            if isinstance(tc_data, dict) and any(k in tc_data for k in ("fast", "medium", "slow")):
                                ordinal = 1
                                for tier in ("fast", "medium", "slow"):
                                    for tc in tc_data.get(tier, []):
                                        ordinal_map[ordinal] = {"tier": tier, "input": tc.get("input", "")}
                                        ordinal += 1
                            elif isinstance(tc_data, list):
                                for i, tc in enumerate(tc_data, 1):
                                    ordinal_map[i] = {"tier": "unknown", "input": tc.get("input", "")}
                        tc_info_lookup[p_name] = ordinal_map

                fail_jsonl_path = output_dir / f"codecontests_timing_{split_name}_final_failures.jsonl"
                with open(fail_jsonl_path, "w", encoding="utf-8") as fj:
                    fj.write(json.dumps({
                        "type": "metadata",
                        "split": split_name,
                        "total": n_problems,
                        "complete": len(result_ac),
                        "failed": len(final_missing),
                    }) + "\n")
                    for p in result["problems"]:
                        if p["index"] not in final_missing:
                            continue
                        p_name = p.get("name", "")
                        info_map = tc_info_lookup.get(p_name, {})
                        solutions_info = []
                        for s in p.get("solutions", []):
                            runs_with_tier = []
                            for r in s.get("runs", []):
                                ordinal = r.get("testcase", -1)
                                tc_info = info_map.get(ordinal, {})
                                runs_with_tier.append({
                                    "testcase_ordinal": ordinal,
                                    "tier": tc_info.get("tier", "unknown"),
                                    "input": tc_info.get("input", ""),
                                    "verdict": r.get("verdict", ""),
                                    "run_time": r.get("run_time"),
                                })
                            tier_fail_counts = {}
                            for r in runs_with_tier:
                                if r["verdict"] not in ("correct", ""):
                                    t = r["tier"]
                                    tier_fail_counts[t] = tier_fail_counts.get(t, 0) + 1
                            solutions_info.append({
                                "language": s.get("language"),
                                "language_id": s.get("language_id"),
                                "verdict": s.get("verdict"),
                                "min_time": s.get("min_time"),
                                "max_time": s.get("max_time"),
                                "speed_tiers": s.get("speed_tiers"),
                                "tier_fail_counts": tier_fail_counts,
                                "failing_runs": [
                                    r for r in runs_with_tier if r["verdict"] not in ("correct", "")
                                ],
                                "all_runs": runs_with_tier,
                            })
                        fj.write(json.dumps({
                            "type": "failure",
                            "index": p["index"],
                            "name": p_name,
                            "solutions": solutions_info,
                        }) + "\n")
                log.info("Structured failure JSONL saved -> %s", fail_jsonl_path)
            else:
                log.info("All %d problems complete for split '%s'.", n_problems, split_name)
        else:
            log.error("No result produced for split '%s'.", split_name)

    log.info("Done.")

if __name__ == "__main__":
    main()
