"""
Wrap a dbt command and write a structured error report JSON.

Usage:
    python dbt_error_report.py <report_path> <dbt args...>

Writes {"passed": bool, "errors": [{"model": str, "message": str}]} to <report_path>.
Exits with the underlying dbt command exit code.
"""
import json
import pathlib
import re
import subprocess
import sys

# Matches: "Compilation Error in model my_model (path/to/model.sql)"
# Also handles dbt log-prefixed lines like "16:04:22  Compilation Error in model …"
_COMPILE_ERROR_RE = re.compile(r"Compilation Error in model (\S+)")


def parse_run_results(run_results_path: str = "target/run_results.json") -> list[dict]:
    """Parse dbt run_results.json and return [{model, message}] for failed nodes."""
    try:
        with open(run_results_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    errors = []
    for result in data.get("results", []):
        if result.get("status") in ("error", "fail"):
            uid = result.get("unique_id", "")
            model = uid.split(".")[-1] if uid else uid
            msg = (result.get("message") or "").strip()
            errors.append({"model": model, "message": msg})
    return errors


def parse_output_errors(output: str) -> list[dict]:
    """
    Fallback: extract compile errors from dbt text output when run_results.json
    has no entries (e.g. Jinja macro errors that abort before individual nodes run).
    """
    errors = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        m = _COMPILE_ERROR_RE.search(line)
        if m:
            model = m.group(1).rstrip("(")
            # Collect indented lines that follow as the error message
            msg_lines = []
            for subsequent in lines[i + 1:]:
                stripped = subsequent.strip()
                if not stripped:
                    break
                if subsequent.startswith(" ") or subsequent.startswith("\t"):
                    msg_lines.append(stripped)
                else:
                    break
            errors.append({"model": model, "message": " ".join(msg_lines)})
    return errors


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: dbt_error_report.py <report_path> <dbt args...>", file=sys.stderr)
        sys.exit(1)

    report_path = sys.argv[1]
    dbt_args = sys.argv[2:]

    proc = subprocess.run(["dbt"] + dbt_args, capture_output=True, text=True)

    # Always surface dbt output in CI logs; on failure emit to stderr for visibility
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)

    passed = proc.returncode == 0
    if passed:
        errors = []
    else:
        errors = parse_run_results()
        if not errors:
            # run_results.json had no error entries — fall back to parsing text output
            errors = parse_output_errors(proc.stdout + proc.stderr)

    pathlib.Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump({"passed": passed, "errors": errors}, f, indent=2)

    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
