"""Parse dbt `run_results.json` into a gate summary.

Shared by Gate 3 (unit tests) and Gate 4 (data tests).

Output schema written to --output:
    {
      "overall_status": "pass" | "fail",
      "total": int,
      "counts": {"pass": int, "fail": int, "error": int, "skip": int},
      "failures": [{"name": str, "status": str, "message": str}],  # capped at 10
      "truncated": bool
    }

Exit codes:
    0 — overall_status == "pass" (includes empty results)
    1 — overall_status == "fail"
    2 — input file missing or unreadable / malformed JSON
"""
from __future__ import annotations

import argparse
import json
import sys

import runner_io

_FAILURE_CAP = 10


def summarize(run_results: dict) -> dict:
    counts = {"pass": 0, "fail": 0, "error": 0, "skip": 0}
    failures: list[dict] = []

    results = run_results.get("results") or []
    for r in results:
        status = r.get("status")
        if status in ("pass", "success"):
            counts["pass"] += 1
        elif status == "fail":
            counts["fail"] += 1
            failures.append({
                "name": r.get("unique_id", ""),
                "status": "fail",
                "message": r.get("message") or "",
            })
        elif status == "skip":
            counts["skip"] += 1
        else:
            # "error" and any unknown/unexpected status bucket as error.
            counts["error"] += 1
            failures.append({
                "name": r.get("unique_id", ""),
                "status": "error",
                "message": r.get("message") or "",
            })

    truncated = len(failures) > _FAILURE_CAP
    overall = "fail" if (counts["fail"] or counts["error"]) else "pass"

    return {
        "overall_status": overall,
        "total": len(results),
        "counts": counts,
        "failures": failures[:_FAILURE_CAP],
        "truncated": truncated,
    }


def parse_run_results(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    return summarize(data)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Parse dbt run_results.json into gate summary.")
    p.add_argument("--input", required=True, help="Path to dbt run_results.json")
    p.add_argument("--output", required=True, help="Path to write gate summary JSON")
    args = p.parse_args(argv)

    try:
        summary = parse_run_results(args.input)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        runner_io.error(f"parse_run_results: {e}")
        return 2

    with open(args.output, "w") as f:
        json.dump(summary, f, indent=2)

    print(
        f"parse_run_results: {summary['counts']['pass']} passed / "
        f"{summary['counts']['fail']} failed / "
        f"{summary['counts']['error']} errored / "
        f"{summary['counts']['skip']} skipped "
        f"(overall={summary['overall_status']})"
    )
    return 0 if summary["overall_status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
