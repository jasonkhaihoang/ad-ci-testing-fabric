"""
Gate Runner — triggers a Fabric notebook via Item Jobs API, polls until terminal,
downloads the gate result JSON from OneLake, and emits a GitHub commit status.

Usage:
    python3 gate_runner.py run-gate \
        --gate 2 \
        --workspace-id WS_ID \
        --lakehouse-id LH_ID \
        --notebook-id NB_ID \
        --head-sha SHA

Environment variables required:
    GH_TOKEN            — GitHub token for posting commit statuses
    GITHUB_REPOSITORY   — owner/repo (e.g. acme/my-dbt-repo)

Authentication: Azure CLI session (established by azure/login before invocation).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:
    yaml = None

import fabric_transport

FABRIC_API = "https://api.fabric.microsoft.com/v1"
ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
POLL_TERMINAL_STATES = {"Completed", "Failed", "Cancelled", "Deduped"}
POLL_INTERVAL_S = 15
POLL_TIMEOUT_S = 600  # 10 minutes


# ── Pure core ──────────────────────────────────────────────────────────────────

def check_store_failures_config(dbt_project_path: str) -> bool:
    """Return True if dbt_project.yml has tests: +store_failures: true and +store_failures_as: table."""
    if yaml is None:
        return False
    try:
        with open(dbt_project_path) as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, Exception):
        return False
    tests = cfg.get("tests") or {}
    if not isinstance(tests, dict):
        return False
    store_failures = tests.get("+store_failures")
    store_failures_as = tests.get("+store_failures_as")
    return bool(store_failures) and store_failures_as == "table"


def parse_gate_4_result(result_json: dict) -> tuple[str, list]:
    """Extract (overall_status, tests) from a gate-4 result dict."""
    status = result_json.get("overall_status", "")
    tests = result_json.get("tests") or []
    return status, tests


def gate_4_overall_status(tests: list) -> str:
    """Derive pass/fail from a list of test result dicts.

    fail or error → 'fail'; skip alone → 'pass'; empty → 'pass'.
    """
    for t in tests:
        if t.get("status") in ("fail", "error"):
            return "fail"
    return "pass"


def parse_gate_result(result_json: dict) -> tuple[str, list]:
    """Extract (overall_status, models) from the gate result dict."""
    status = result_json.get("overall_status", "")
    models = result_json.get("models") or []
    return status, models


def map_gate_status(overall_status: str) -> str:
    """Map gate overall_status to GitHub commit status state."""
    return "success" if overall_status == "pass" else "failure"


def build_job_trigger_body(gate: str, ci_run_id: str, head_sha: str) -> dict:
    """Build the Fabric Item Jobs API POST body for triggering a notebook run.

    jobType must be a query parameter (?jobType=RunNotebook), not a body field.
    The body contains only executionData.
    """
    def _typed(value: str) -> dict:
        return {"value": str(value), "type": "string"}

    return {
        "executionData": {
            "parameters": {
                "run_mode": _typed("ci"),
                "gate": _typed(gate),
                "ci_run_id": _typed(ci_run_id),
                "head_sha": _typed(head_sha),
            }
        },
    }


# ── I/O shell ─────────────────────────────────────────────────────────────────

def _post_github_status(repo: str, sha: str, context: str, state: str, description: str, target_url: str, gh_token: str) -> None:
    url = f"https://api.github.com/repos/{repo}/statuses/{sha}"
    body = json.dumps({
        "state": state,
        "context": context,
        "description": description[:140],
        "target_url": target_url,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {gh_token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print(f"Failed to post GitHub status: HTTP {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        raise


def _trigger_notebook_job(workspace_id: str, notebook_id: str, body: dict, token: str) -> str:
    """POST to Item Jobs API; return the job instance ID."""
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{notebook_id}/jobs/instances?jobType=RunNotebook"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            parsed = json.loads(raw) if raw else {}
            job_id = parsed.get("id") or parsed.get("jobInstanceId")
            if not job_id:
                # Try Location header convention for 202 responses
                raise RuntimeError(f"No job ID in response: {parsed}")
            return job_id
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        # 202 Accepted — parse Location header for job ID
        if e.code == 202:
            location = e.headers.get("Location", "")
            # Location: .../jobs/instances/{job_id}
            job_id = location.rstrip("/").split("/")[-1]
            if job_id:
                return job_id
        print(f"HTTP {e.code} triggering notebook job: {body_text}", file=sys.stderr)
        raise


def _trigger_notebook_job_v2(workspace_id: str, notebook_id: str, body: dict, token: str) -> str:
    """POST to Item Jobs API; handle 202 Accepted with Location header."""
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{notebook_id}/jobs/instances?jobType=RunNotebook"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            location = resp.getheader("Location", "")
            parsed = json.loads(raw) if raw else {}
            job_id = parsed.get("id") or parsed.get("jobInstanceId")
            if not job_id and location:
                job_id = location.rstrip("/").split("/")[-1]
            if not job_id:
                raise RuntimeError(f"No job ID in response: {parsed}")
            print(f"Notebook job triggered: {job_id}", flush=True)
            return job_id
    except urllib.error.HTTPError as e:
        if e.code in (200, 201, 202):
            location = e.headers.get("Location", "")
            job_id = location.rstrip("/").split("/")[-1]
            if job_id:
                print(f"Notebook job triggered (HTTP {e.code}): {job_id}", flush=True)
                return job_id
        body_text = e.read().decode(errors="replace")
        print(f"HTTP {e.code} triggering notebook job: {body_text}", file=sys.stderr)
        raise


def _poll_job_until_terminal(workspace_id: str, notebook_id: str, job_id: str, token: str) -> str:
    """Poll the job until it reaches a terminal state. Returns the terminal status string."""
    path = f"/workspaces/{workspace_id}/items/{notebook_id}/jobs/instances/{job_id}"
    deadline = time.monotonic() + POLL_TIMEOUT_S
    last_status = "Unknown"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        result = fabric_transport.request("GET", path)
        last_status = result.get("status", "Unknown")
        print(f"  Job status [{attempt}]: {last_status}", flush=True)
        if last_status in POLL_TERMINAL_STATES:
            return last_status
        time.sleep(POLL_INTERVAL_S)
    raise RuntimeError(
        f"Job {job_id} timed out after {POLL_TIMEOUT_S}s (last status: {last_status!r})"
    )


def _download_gate_result(workspace_id: str, lakehouse_id: str, head_sha: str, gate: str, token: str) -> dict:
    """Download gate result JSON from OneLake DFS."""
    remote_path = f"Files/ci-artifacts/gate-{gate}/{head_sha}/gate-{gate}.json"
    url = f"{ONELAKE_DFS}/{workspace_id}/{lakehouse_id}/{remote_path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        print(f"HTTP {e.code} downloading gate result: {body_text}", file=sys.stderr)
        raise


def cmd_run_gate(args):
    gate = args.gate
    workspace_id = args.workspace_id
    lakehouse_id = args.lakehouse_id
    notebook_id = args.notebook_id
    head_sha = args.head_sha

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    gh_token = os.environ.get("GH_TOKEN", "")
    run_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com") + f"/{repo}/actions/runs/" + os.environ.get("GITHUB_RUN_ID", "")

    context = f"ci/gate-{gate}"

    # Emit pending
    if repo and gh_token:
        _post_github_status(repo, head_sha, context, "pending", f"Gate {gate}: notebook running", run_url, gh_token)
        print(f"Posted {context} pending.", flush=True)

    fabric_token = fabric_transport.get_token("fabric")
    storage_token = fabric_transport.get_token("storage")

    import uuid
    ci_run_id = str(uuid.uuid4())
    trigger_body = build_job_trigger_body(gate=gate, ci_run_id=ci_run_id, head_sha=head_sha)

    print(f"Triggering gate-{gate} notebook job…", flush=True)
    job_id = _trigger_notebook_job_v2(workspace_id, notebook_id, trigger_body, fabric_token)

    print(f"Polling job {job_id}…", flush=True)
    terminal_status = _poll_job_until_terminal(workspace_id, notebook_id, job_id, fabric_token)
    print(f"Job terminal status: {terminal_status}", flush=True)

    if terminal_status != "Completed":
        gh_state = "failure"
        if repo and gh_token:
            _post_github_status(repo, head_sha, context, gh_state, f"Gate {gate}: job {terminal_status}", run_url, gh_token)
        print(f"Gate {gate} failed: job status {terminal_status}", file=sys.stderr)
        sys.exit(1)

    # Gate 4 pre-flight: check dbt_project.yml store_failures config
    store_failures_config_ok = True
    if gate == "4":
        store_failures_config_ok = check_store_failures_config("dbt_project.yml")
        if not store_failures_config_ok:
            print(
                "Advisory: dbt_project.yml is missing 'tests: +store_failures: true' "
                "and/or '+store_failures_as: table'. Failure drill-down tables will not "
                "be available. Gate signal is unaffected.",
                flush=True,
            )

    print(f"Downloading gate-{gate} result from OneLake…", flush=True)
    result = _download_gate_result(workspace_id, lakehouse_id, head_sha, gate, storage_token)

    if gate == "4":
        # Inject pre-flight result and re-derive overall status from tests
        result["store_failures_config_ok"] = store_failures_config_ok
        _, tests = parse_gate_4_result(result)
        overall_status = gate_4_overall_status(tests)
        item_count = len(tests)
        item_label = "test(s)"
    elif gate == "3":
        overall_status = result.get("overall_status", "fail")
        item_count = result.get("total", 0)
        item_label = "unit test(s)"
    else:
        overall_status, models = parse_gate_result(result)
        item_count = len(models)
        item_label = "model(s)"

    gh_state = map_gate_status(overall_status)

    description = f"Gate {gate}: {overall_status}"
    if repo and gh_token:
        _post_github_status(repo, head_sha, context, gh_state, description, run_url, gh_token)

    print(f"Gate {gate} result: {overall_status} ({item_count} {item_label}) → GitHub status: {gh_state}", flush=True)

    if args.output:
        import pathlib
        pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Gate {gate} report written to {args.output}", flush=True)

    if gh_state != "success":
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Gate runner — Fabric Item Jobs API")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run-gate")
    p.add_argument("--gate", required=True)
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--lakehouse-id", required=True)
    p.add_argument("--notebook-id", required=True)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--output", default=None)

    args = parser.parse_args()
    {"run-gate": cmd_run_gate}[args.command](args)


if __name__ == "__main__":
    main()
