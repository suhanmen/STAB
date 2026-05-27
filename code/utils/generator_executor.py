import re
import subprocess
import sys
import tempfile
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

def extract_python_from_response(text: str) -> Optional[str]:
    m = re.search(r"```python\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        code = m.group(1).strip()
        if 'def generate' in code:
            return code

    m = re.search(r"(def generate\s*\(.*?\).*)", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    return None

def execute_generator(code: str, timeout: int = 30) -> Tuple[bool, str]:
    wrapper = code + "\n\n" + (
        "if __name__ == '__main__':\n"
        "    import sys\n"
        "    result = generate()\n"
        "    if not isinstance(result, str):\n"
        "        print(f'ERROR: generate() returned {type(result).__name__}, expected str', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "    sys.stdout.write(result)\n"
    )

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False, dir='/tmp'
        ) as f:
            f.write(wrapper)
            tmp_path = f.name

        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        Path(tmp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            err = result.stderr.strip()
            return False, f"Runtime error (exit code {result.returncode}):\n{err}"

        stdout = result.stdout
        if not stdout:
            return False, "generate() returned an empty string"

        return True, stdout

    except subprocess.TimeoutExpired:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        return False, f"Execution timed out after {timeout}s"
    except Exception as e:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
        return False, f"Execution failed: {e}"

def validate_generator(code: str, timeout: int = 30) -> Tuple[bool, str, Optional[str]]:
    if not code or 'def generate' not in code:
        return False, "No generate() function found in code", None

    ok, result = execute_generator(code, timeout=timeout)
    if ok:
        return True, "OK", result
    else:
        return False, result, None
