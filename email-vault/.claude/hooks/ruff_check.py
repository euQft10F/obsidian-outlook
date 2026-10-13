"""PostToolUse hook: run `ruff check` on a .py file after Claude edits or writes it.

Reads the hook JSON from stdin. Exit 0 when the file is clean or not a Python file;
exit 2 with ruff's findings on stderr so Claude sees them and fixes the file.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path or not file_path.endswith(".py") or not Path(file_path).is_file():
        return 0

    if shutil.which("uv"):
        cmd = ["uv", "run", "--frozen", "ruff"]
    else:
        venv = Path(payload.get("cwd") or ".") / ".venv"
        ruff = next((p for p in (venv / "Scripts" / "ruff.exe", venv / "bin" / "ruff") if p.is_file()), None)
        if ruff is None:
            return 0
        cmd = [str(ruff)]

    result = subprocess.run(
        [*cmd, "check", "--output-format", "concise", file_path],
        capture_output=True,
        text=True,
        cwd=payload.get("cwd") or None,
    )
    if result.returncode == 0:
        return 0
    sys.stderr.write(f"ruff found issues in {file_path}:\n{result.stdout}{result.stderr}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
