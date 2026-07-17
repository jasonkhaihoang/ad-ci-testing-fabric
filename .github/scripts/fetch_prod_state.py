"""
Fetch prod-state/manifest.json for Slim CI deferral (AWAP v1.4 Phase 2).

Modes (from ci-config.yml prod_manifest_source.mode):
  artifact  — download from latest successful CD run on main via gh CLI (default)
  onelake   — download from OneLake Files path via Fabric UAMI

Falls back to greenfield dbt parse when no manifest is available.

Outputs:
  prod-state/manifest.json
  prod-state/source.json  — {mode, source, head_sha, retrieved_at}
  GITHUB_OUTPUT: greenfield_fallback=true|false
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import NamedTuple

import ci_config
import fabric_transport
import runner_io


ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"


# ─── Artifact-mode result types (VD-1596 Phase 2) ─────────────────────────────
#
# Artifact mode distinguishes three outcomes:
#   - success    : manifest fetched and written to prod-state/
#   - greenfield : zero successful CD runs on `main` ever — operator should
#                  expect a full build. Caller invokes fetch_greenfield().
#   - error      : any other fetch failure. Caller must NOT collapse to
#                  greenfield; instead exit non-zero with category + reason
#                  so the PR comment can route the operator to remediation.
#
# Categories (artifact mode):
#   transient — gh CLI non-zero, retryable stderr, network/timeout
#   config    — missing required key in ci-config.yml
#   parse     — manifest absent from artifact, or invalid JSON inside it
# (`auth` is onelake-mode-only, not reachable in artifact mode which uses the
# repo `GITHUB_TOKEN` — see §4.2 of the design doc and OnelakeResult below.)

class ArtifactResult(NamedTuple):
    status: str  # "success" | "greenfield" | "error"
    category: str | None = None
    reason: str | None = None


# ─── Onelake-mode result type (VD-3216) ───────────────────────────────────────
#
# Mirrors ArtifactResult. A 404 on the fixed canonical OneLake manifest path is
# the confirmed-greenfield signal: domain-deploy always publishes to this same
# path with no retention/rotation window, so 404 there is structurally
# equivalent to artifact mode's "zero successful CD runs ever". Any other
# failure is a platform error — never collapsed to greenfield.
#
# Categories (onelake mode):
#   config    — missing workspace_id/lakehouse_id/file_path in ci-config.yml
#   auth      — Fabric UAMI token acquisition failure, or 401/403 on the fetch
#   transient — 5xx, network error, or timeout
#   parse     — response body is not valid JSON

class OnelakeResult(NamedTuple):
    status: str  # "success" | "greenfield" | "error"
    category: str | None = None
    reason: str | None = None


# ─── Output helpers ───────────────────────────────────────────────────────────

def write_source_json(mode: str, source: str, head_sha: str) -> None:
    os.makedirs("prod-state", exist_ok=True)
    data = {
        "mode": mode,
        "source": source,
        "head_sha": head_sha,
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    with open("prod-state/source.json", "w") as f:
        json.dump(data, f, indent=2)


# ─── Fetch modes ──────────────────────────────────────────────────────────────

def fetch_artifact_mode(cfg: dict) -> ArtifactResult:
    """Download manifest from the latest successful CD run on main.

    Returns an ArtifactResult — see ArtifactResult docstring for the three
    possible statuses and category mapping.
    """
    workflow = cfg.get("workflow", "")
    artifact_name = cfg.get("artifact_name", "prod-manifest")
    main_branch = cfg.get("main_branch", "main")
    repo = os.environ.get("REPO", "")
    head_sha = os.environ.get("HEAD_SHA", "")

    if not workflow:
        reason = "prod_manifest_source.workflow is required for artifact mode."
        runner_io.error(reason)
        return ArtifactResult("error", "config", reason)

    result = subprocess.run(
        [
            "gh", "run", "list",
            "--workflow", workflow,
            "--branch", main_branch,
            "--status", "success",
            "--limit", "1",
            "--json", "databaseId,headSha",
            "--repo", repo,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        reason = f"gh run list failed: {result.stderr.strip() or 'exit ' + str(result.returncode)}"
        runner_io.warning(reason)
        return ArtifactResult("error", "transient", reason)

    try:
        runs = json.loads(result.stdout)
    except ValueError:
        reason = "gh run list returned invalid JSON (possible rate-limit or proxy interception)."
        runner_io.warning(reason)
        return ArtifactResult("error", "transient", reason)

    if not runs:
        # The single legitimate greenfield signal: zero successful CD runs ever.
        runner_io.notice(
            "No successful CD workflow runs found on main — true greenfield "
            "(full build)."
        )
        return ArtifactResult("greenfield")

    run_id = runs[0]["databaseId"]
    run_sha = runs[0]["headSha"]

    with tempfile.TemporaryDirectory() as tmpdir:
        dl_result = subprocess.run(
            [
                "gh", "run", "download", str(run_id),
                "--name", artifact_name,
                "--dir", tmpdir,
                "--repo", repo,
            ],
            capture_output=True, text=True,
        )
        if dl_result.returncode != 0:
            reason = (
                f"gh run download failed for run {run_id}: "
                f"{dl_result.stderr.strip() or 'exit ' + str(dl_result.returncode)}"
            )
            runner_io.warning(reason)
            return ArtifactResult("error", "transient", reason)

        manifest_src = os.path.join(tmpdir, "manifest.json")
        if not os.path.exists(manifest_src):
            reason = (
                f"manifest.json not found in artifact '{artifact_name}' "
                f"(run {run_id})."
            )
            runner_io.warning(reason)
            return ArtifactResult("error", "parse", reason)

        # Validate the manifest is parseable JSON before declaring success —
        # downstream Slim CI will choke on a malformed file with a worse error.
        try:
            with open(manifest_src) as f:
                json.load(f)
        except (ValueError, OSError) as e:
            reason = f"manifest.json in artifact '{artifact_name}' is not valid JSON: {e}"
            runner_io.warning(reason)
            return ArtifactResult("error", "parse", reason)

        os.makedirs("prod-state", exist_ok=True)
        shutil.copy2(manifest_src, "prod-state/manifest.json")
        # Also copy prod-target manifest for --defer resolution in gates 2/4 (VD-2142).
        manifest_prod_src = os.path.join(tmpdir, "manifest_prod.json")
        if os.path.exists(manifest_prod_src):
            shutil.copy2(manifest_prod_src, "prod-state/manifest_prod.json")

    write_source_json(
        mode="artifact",
        source=f"{repo}/runs/{run_id} (SHA {run_sha[:8]})",
        head_sha=head_sha,
    )
    print(f"Artifact manifest fetched from run {run_id} (SHA {run_sha[:8]}).", flush=True)
    return ArtifactResult("success")


def fetch_onelake_mode(cfg: dict) -> OnelakeResult:
    """Download manifest from OneLake Files path via Fabric UAMI.

    Returns an OnelakeResult — see OnelakeResult docstring for the three
    possible statuses and category mapping (VD-3216).
    """
    workspace_id = cfg.get("workspace_id", "")
    lakehouse_id = cfg.get("lakehouse_id", "")
    file_path = cfg.get("file_path", "")
    head_sha = os.environ.get("HEAD_SHA", "")

    if not all([workspace_id, lakehouse_id, file_path]):
        reason = (
            "prod_manifest_source.workspace_id, lakehouse_id, and file_path "
            "are all required for onelake mode."
        )
        runner_io.error(reason)
        return OnelakeResult("error", "config", reason)

    base_url = os.environ.get("ONELAKE_DFS_BASE_URL", ONELAKE_DFS)
    url = f"{base_url}/{workspace_id}/{lakehouse_id}/{file_path}"

    try:
        token = fabric_transport.get_token("storage")
    except (subprocess.CalledProcessError, ValueError, KeyError) as e:
        reason = f"Fabric UAMI token acquisition failed: {e}"
        runner_io.warning(reason)
        return OnelakeResult("error", "auth", reason)

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # The single legitimate greenfield signal: domain-deploy always
            # publishes to this same fixed path with no retention window, so
            # a 404 there is the onelake equivalent of "zero prior publishes".
            runner_io.notice(
                "No manifest found at the OneLake prod-state path — true "
                "greenfield (full build)."
            )
            return OnelakeResult("greenfield")
        category = "auth" if e.code in (401, 403) else "transient"
        reason = f"OneLake manifest fetch failed (HTTP {e.code})."
        runner_io.warning(reason)
        return OnelakeResult("error", category, reason)
    except urllib.error.URLError as e:
        reason = f"OneLake manifest fetch failed (network error): {e.reason}"
        runner_io.warning(reason)
        return OnelakeResult("error", "transient", reason)

    # Validate the manifest is parseable JSON before declaring success —
    # mirrors artifact mode's parse validation.
    try:
        json.loads(content)
    except ValueError as e:
        reason = f"OneLake manifest at {file_path} is not valid JSON: {e}"
        runner_io.warning(reason)
        return OnelakeResult("error", "parse", reason)

    os.makedirs("prod-state", exist_ok=True)
    with open("prod-state/manifest.json", "wb") as f:
        f.write(content)

    write_source_json(
        mode="onelake",
        source=f"{workspace_id}/{lakehouse_id}/{file_path}",
        head_sha=head_sha,
    )
    print(f"OneLake manifest fetched from {url}.", flush=True)
    return OnelakeResult("success")


def fetch_greenfield() -> None:
    """Emit a minimal `prod-state/manifest.json` so `state:modified+` selects everything.

    Greenfield = no CD-published manifest available. By design we want Slim CI to
    degrade to a full build (every model is `state:new` against the empty previous
    state). To do that we always write a minimal manifest with `nodes: {}` and
    `sources: {}` — the parse output of the current branch is NEVER used as the
    `--state` source, because that would make `state:modified+` resolve to ∅ and
    Slim CI would silently build nothing.

    A best-effort `dbt deps` + `dbt parse` runs purely as a diagnostic so project-
    level errors surface in the CI log; their outputs are intentionally discarded.
    See ephemeral-ci-workflow-design-v1.4.md §4.3.
    """
    head_sha = os.environ.get("HEAD_SHA", "")
    print("⚠️  No prod manifest available — greenfield fallback (full build).", flush=True)

    # Diagnostic only: surface project-level errors. Output NOT used as prod manifest.
    deps = subprocess.run(
        ["dbt", "deps", "--profiles-dir", ".github/profiles", "--target", "dbt_quality"],
        capture_output=True, text=True,
    )
    if deps.returncode != 0:
        runner_io.warning(
            f"dbt deps failed (exit {deps.returncode}). "
            f"stdout/stderr (first 300 chars): "
            f"{(deps.stdout + deps.stderr)[:300]}"
        )

    parse = subprocess.run(
        [
            "dbt", "parse",
            "--profiles-dir", ".github/profiles",
            "--target", "dbt_quality",
            "--exclude", "package:elementary",
        ],
        capture_output=True, text=True,
    )
    if parse.returncode != 0:
        # dbt prints parse errors to stdout (not stderr); include both.
        runner_io.warning(
            f"dbt parse failed (exit {parse.returncode}). "
            f"stdout/stderr (first 500 chars): "
            f"{(parse.stdout + parse.stderr)[:500]}"
        )

    # Always emit minimal manifest. Empty `nodes` makes every current-branch
    # model count as `state:new`, so `state:modified+` selects everything →
    # Slim CI degrades to a full build. This is the design intent for
    # greenfield: AWAP v1.4 §4.3.
    os.makedirs("prod-state", exist_ok=True)
    minimal = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "0.0.0",
        },
        "nodes": {},
        "sources": {},
        "macros": {},
        "docs": {},
        "exposures": {},
        "metrics": {},
        "groups": {},
        "selectors": {},
        "disabled": {},
        "parent_map": {},
        "child_map": {},
        "group_map": {},
        "saved_queries": {},
        "semantic_models": {},
        "unit_tests": {},
        "functions": {},
    }
    with open("prod-state/manifest.json", "w") as f:
        json.dump(minimal, f, indent=2)
    write_source_json(
        mode="greenfield",
        source="minimal manifest (full build — no prod state available)",
        head_sha=head_sha,
    )


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict | None:
    """Return parsed ci-config.yml, or None when the file is absent.

    A wholly missing ci-config.yml means the repo has not been onboarded to
    Slim CI; callers treat that as greenfield (not a platform error).
    """
    config_path = ci_config.locate_ci_config()
    if not os.path.exists(config_path):
        runner_io.warning("ci-config.yml not found — using greenfield fallback.")
        return None
    with open(config_path) as f:
        yaml_str = f.read()
    return ci_config.parse_ci_config(yaml_str)["config"]


# ─── Entry point ──────────────────────────────────────────────────────────────

def _emit_platform_error(mode: str, category: str, reason: str) -> None:
    """Surface a non-greenfield platform error to the runner + PR comment path.

    Writes structured outputs that `ci.yml`'s "Post gate-1 comment" step picks
    up and renders via `notify_render.render_gate_1_comment(... platform_error=…)`.
    Does NOT write `prod-state/source.json` with `mode: greenfield` — by design,
    a platform error must be distinguishable from a true greenfield run.
    """
    runner_io.set_output("greenfield_fallback", "false")
    runner_io.set_output("mode", mode)
    runner_io.set_output("category", category)
    runner_io.set_output("reason", reason)
    runner_io.error(f"Platform error ({mode}/{category}): {reason}")


def _apply_fetch_result(mode: str, result: ArtifactResult | OnelakeResult) -> None:
    """Dispatch on a fetch result's status — shared by artifact and onelake mode.

    Both ArtifactResult and OnelakeResult carry the same (status, category,
    reason) shape, so the success/greenfield/error branching is identical;
    only the mode name threaded into the platform-error output differs.
    """
    if result.status == "success":
        runner_io.set_output("greenfield_fallback", "false")
    elif result.status == "greenfield":
        fetch_greenfield()
        runner_io.set_output("greenfield_fallback", "true")
    else:  # error — distinguish from greenfield, exit non-zero
        _emit_platform_error(mode, result.category or "transient", result.reason or "")
        sys.exit(1)


def main() -> None:
    config = load_config()
    if config is None:
        # Repo not onboarded to Slim CI — greenfield without platform-error
        # noise. Preserves backwards compatibility for un-onboarded repos.
        fetch_greenfield()
        runner_io.set_output("greenfield_fallback", "true")
        print("fetch-prod-state complete.", flush=True)
        return

    manifest_cfg = config.get("prod_manifest_source") or {}
    mode = manifest_cfg.get("mode", "artifact")

    if mode == "artifact":
        _apply_fetch_result("artifact", fetch_artifact_mode(manifest_cfg))
    elif mode == "onelake":
        _apply_fetch_result("onelake", fetch_onelake_mode(manifest_cfg))
    else:
        runner_io.warning(f"Unknown prod_manifest_source.mode '{mode}' — using greenfield fallback.")
        fetch_greenfield()
        runner_io.set_output("greenfield_fallback", "true")

    print("fetch-prod-state complete.", flush=True)


if __name__ == "__main__":
    main()
