"""Thin shell for Gate 2 (ci/run) on Fabric GHA runner (AC-11).

Orchestrates: env setup → dbt deps → dbt clone → dbt run → parse results → emit status.
authentication: CLI in profiles.yml uses az account get-access-token after azure/login.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import emit_status
from fabric_runner_utils import select_names as _select_names, setup_defer as _setup_defer, write_gate_result as _write_gate_result

CONTEXT = "ci/run"
PROFILE = "dbt_fab_spark"
TARGET = "ephemeral_ci"


def _run_url() -> str:
    base = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{base}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"


def _post(head_sha: str, state: str, description: str) -> None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        emit_status.emit_status(repo, head_sha, CONTEXT, state, description, _run_url())


def _model_rows(run_results: dict | None) -> tuple[str, list[dict]]:
    """Extract (status, models) from a dbt run_results dict."""
    if run_results is None:
        return "fail", []
    results = run_results.get("results") or []
    if not results:
        return "pass", []
    rows = []
    all_ok = True
    for r in results:
        if not r.get("unique_id", "").startswith("model."):
            continue
        status = r.get("status", "error")
        ok = status in ("success", "pass", "clone")
        if not ok:
            all_ok = False
        msg = (r.get("message") or "")[:500] or None
        rows.append({
            "name": r.get("unique_id", "").split(".")[-1],
            "status": status,
            "duration_seconds": round(float(r.get("execution_time", 0.0)), 3),
            "error_message": msg,
        })
    return ("pass" if all_ok else "fail"), rows


def assemble_gate2_result(
    head_sha: str,
    clone_run_results: dict | None,
    build_run_results: dict | None,
) -> dict:
    """Pure: assemble gate-2.json from dbt clone and dbt run run_results dicts."""
    clone_status, clone_models = _model_rows(clone_run_results)
    build_status, build_models = _model_rows(build_run_results)
    overall = "pass" if clone_status == "pass" and build_status == "pass" else "fail"
    return {
        "gate": "2",
        "head_sha": head_sha,
        "overall_status": overall,
        "clone": {"status": clone_status, "models": clone_models},
        "build": {"status": build_status, "models": build_models},
    }


def cmd_run_gate(args) -> int:
    head_sha = args.head_sha
    _post(head_sha, "pending", "Gate 2: cloning and running dbt on Fabric runner")

    env = {
        **os.environ,
        "WORKSPACE_ID": args.workspace_id,
        "WORKSPACE_NAME": args.workspace_name,
        "LAKEHOUSE": args.lakehouse_name,
        "LAKEHOUSE_ID": args.lakehouse_id,
        "SCHEMA": args.schema,
        "SESSION_ID_FILE": f"Files/ci-artifacts/{head_sha}/livy-session-id-ci-run.txt",
    }

    names = _select_names(args.deployment_manifest)
    defer_args = _setup_defer(args.prod_state_dir)
    profiles_dir = args.profiles_dir

    subprocess.run(
        ["dbt", "deps", "--profiles-dir", profiles_dir, "--profile", PROFILE,
         "--target", TARGET, "--quiet"],
        env=env,
    )

    clone_run_results: dict | None = None
    build_run_results: dict | None = None

    if not names:
        clone_run_results = {"results": []}
        build_run_results = {"results": []}
    else:
        select_str = " ".join(names)
        if not defer_args:
            # Greenfield: no prod manifest → skip clone, run full build directly against source shortcuts
            clone_run_results = {"results": []}
            subprocess.run([
                "dbt", "run", "--select", select_str,
                "--profiles-dir", profiles_dir, "--profile", PROFILE,
                "--target", TARGET, "--target-path", "target/build",
            ], env=env)
            try:
                with open("target/build/run_results.json") as f:
                    build_run_results = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        else:
            subprocess.run([
                "dbt", "clone", "--select", select_str,
                "--profiles-dir", profiles_dir, "--profile", PROFILE,
                "--target", TARGET, "--target-path", "target/clone",
            ] + defer_args, env=env)
            try:
                with open("target/clone/run_results.json") as f:
                    clone_run_results = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

            if clone_run_results and _model_rows(clone_run_results)[0] == "pass":
                subprocess.run([
                    "dbt", "run", "--select", select_str,
                    "--profiles-dir", profiles_dir, "--profile", PROFILE,
                    "--target", TARGET, "--target-path", "target/build",
                ] + defer_args, env=env)
                try:
                    with open("target/build/run_results.json") as f:
                        build_run_results = json.load(f)
                except (OSError, json.JSONDecodeError):
                    pass

    result = assemble_gate2_result(head_sha, clone_run_results, build_run_results)
    _write_gate_result(args.output, result)

    gh_state = "success" if result["overall_status"] == "pass" else "failure"
    _post(head_sha, gh_state, f"Gate 2: {result['overall_status']}")
    print(f"Gate 2 result: {result['overall_status']} → GitHub status: {gh_state}", flush=True)
    return 0 if gh_state == "success" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate 2 (ci/run) — Fabric GHA runner")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run-gate")
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--workspace-name", required=True)
    p.add_argument("--lakehouse-id", required=True)
    p.add_argument("--lakehouse-name", required=True)
    p.add_argument("--schema", default="dbo")
    p.add_argument("--head-sha", required=True)
    p.add_argument("--deployment-manifest", required=True)
    p.add_argument("--prod-state-dir", default="prod-state")
    p.add_argument("--profiles-dir", default=".github/profiles")
    p.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    return {"run-gate": cmd_run_gate}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
