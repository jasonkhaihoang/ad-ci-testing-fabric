"""Thin shell for Gate 5 (ci/data-diff) on Fabric GHA runner (AC-11).

Creates a Livy session from the GHA runner (CLI auth), runs schema/row-count/value
delta SQL, writes gate-5.json. The label-binding evaluation and final ci/data-diff
status post remain in ci.yml (the existing 'Evaluate diff-acknowledged binding' step).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import emit_status
import fabric_transport
from fabric_runner_utils import write_gate_result as _write_gate_result
from data_diff import (
    create_livy_session,
    delete_livy_session,
    execute_livy_statement,
    make_livy_sql_executor,
    execute_spark_schema,
    execute_spark_count,
    execute_value_delta,
    normalize_unique_key,
    compute_artifact_diff,
    build_gate_5_result,
    VALUE_DELTA_MATERIALIZATIONS,
    SKIP_MATERIALIZATIONS,
)

CONTEXT = "ci/data-diff"
# Fabric Livy API host (not the dbt-fabricspark msitapi endpoint)
_FABRIC_API = "https://api.fabric.microsoft.com/v1"


def _run_url() -> str:
    base = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{base}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"


def _post_pending(head_sha: str) -> None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        emit_status.emit_status(repo, head_sha, CONTEXT, "pending",
                                "Gate 5: data-diff running on Fabric runner", _run_url())


def _livy_base_url(workspace_id: str, lakehouse_id: str) -> str:
    # create_livy_session appends /sessions to this base, matching the Fabric Livy API path.
    return f"{_FABRIC_API}/workspaces/{workspace_id}/lakehouses/{lakehouse_id}/livyapi/versions/2023-12-01"


def _ci_table_ref(workspace_name: str, lakehouse_name: str, schema: str, model: str) -> str:
    return f"`{workspace_name}`.`{lakehouse_name}`.`{schema}`.`{model}`"


def _prod_table_ref(prod_workspace_name: str, prod_lakehouse_name: str, prod_schema: str, model: str) -> str:
    return f"`{prod_workspace_name}`.`{prod_lakehouse_name}`.`{prod_schema}`.`{model}`"


def _load_artifacts(path: str) -> list[dict]:
    try:
        with open(path) as f:
            dm = json.load(f)
        return dm.get("artifacts") or []
    except (OSError, json.JSONDecodeError):
        return []


def cmd_run_gate(args) -> int:
    head_sha = args.head_sha
    _post_pending(head_sha)

    artifacts = _load_artifacts(args.deployment_manifest)
    token_fn = lambda: fabric_transport.get_token("fabric")
    livy_base = _livy_base_url(args.workspace_id, args.lakehouse_id)
    session_url: str | None = None
    diff_artifacts: list[dict] = []

    try:
        # create_livy_session returns session_id (str), not a URL.
        session_id = create_livy_session(
            livy_base, f"dbt-ci-data-diff-{head_sha[:7]}", token_fn,
        )
        session_url = f"{livy_base}/sessions/{session_id}"
        executor = make_livy_sql_executor(session_url, token_fn)

        # Warm up the Spark executor before the artifact loop. create_livy_session
        # waits for driver idle, but executor cold-start (60-180s) happens on the
        # first action. Without this, the first artifact's query absorbs the cost
        # and times out at the default 90s.
        execute_livy_statement(
            session_url, "print(spark.range(1).count())", token_fn, timeout_s=300,
        )

        for artifact in artifacts:
            materialized = artifact.get("materialized", "")
            if materialized in SKIP_MATERIALIZATIONS:
                continue

            if not artifact.get("pre_existing_in_prod", True):
                diff_artifacts.append(compute_artifact_diff(artifact, [], [], 0, 0, None))
                continue

            model_full = artifact.get("name", "")
            parts = model_full.rsplit(".", 1)
            model_name = parts[-1]
            artifact_schema = parts[0] if len(parts) > 1 else args.schema

            ci_ref = _ci_table_ref(args.workspace_name, args.lakehouse_name,
                                   artifact_schema, model_name)
            prod_ref = _prod_table_ref(args.prod_workspace_name, args.prod_lakehouse_name,
                                       args.prod_schema, model_name)

            ci_cols = execute_spark_schema(session_url, ci_ref, token_fn)
            prod_cols = execute_spark_schema(session_url, prod_ref, token_fn)
            ci_count = execute_spark_count(session_url, ci_ref, token_fn)
            prod_count = execute_spark_count(session_url, prod_ref, token_fn)

            value_delta: dict | None = None
            if materialized in VALUE_DELTA_MATERIALIZATIONS:
                uk = normalize_unique_key(artifact.get("unique_key"))
                if uk:
                    ci_names = {c["name"] for c in ci_cols}
                    common_cols = [c["name"] for c in prod_cols if c["name"] in ci_names]
                    value_delta = execute_value_delta(executor, prod_ref, ci_ref, uk, common_cols)
                else:
                    value_delta = {"sampled_rows": 0, "rows_with_diffs": 0, "skipped_no_unique_key": True}

            diff_artifacts.append(
                compute_artifact_diff(artifact, prod_cols, ci_cols, prod_count, ci_count, value_delta)
            )

        result = build_gate_5_result(head_sha, diff_artifacts)
        rc = 0

    except Exception as exc:
        result = build_gate_5_result(head_sha, diff_artifacts, session_error=str(exc))
        print(f"Gate 5 session error: {exc}", file=sys.stderr)
        rc = 1

    finally:
        if session_url:
            try:
                delete_livy_session(session_url, token_fn)
            except Exception as e:
                print(f"Warning: failed to delete Livy session: {e}", file=sys.stderr)

    _write_gate_result(args.output, result)
    overall = result["overall_status"]
    print(f"Gate 5 result: {overall}", flush=True)
    # Final ci/data-diff status (success/failure) is posted by the label-binding
    # step in ci.yml. We only post pending above.
    return 0 if overall != "error" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate 5 (ci/data-diff) — Fabric GHA runner")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run-gate")
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--workspace-name", required=True)
    p.add_argument("--lakehouse-id", required=True)
    p.add_argument("--lakehouse-name", required=True)
    p.add_argument("--schema", default="dbo")
    p.add_argument("--prod-workspace-name", required=True)
    p.add_argument("--prod-lakehouse-name", required=True)
    p.add_argument("--prod-schema", default="dbo")
    p.add_argument("--head-sha", required=True)
    p.add_argument("--deployment-manifest", required=True)
    p.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    return {"run-gate": cmd_run_gate}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
