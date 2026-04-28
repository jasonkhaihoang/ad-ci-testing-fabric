"""
Wrap a dbt command and write a structured error report JSON.

Usage:
    python dbt_error_report.py <report_path> <dbt args...>

Writes {"passed": bool, "errors": [{"model": str, "message": str}]} to <report_path>.
Exits with the underlying dbt command exit code.
"""
import json
import pathlib
import subprocess
import sys


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


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: dbt_error_report.py <report_path> <dbt args...>", file=sys.stderr)
        sys.exit(1)

    report_path = sys.argv[1]
    dbt_args = sys.argv[2:]

    proc = subprocess.run(["dbt"] + dbt_args)
    passed = proc.returncode == 0
    errors = [] if passed else parse_run_results()

    pathlib.Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump({"passed": passed, "errors": errors}, f, indent=2)

    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
