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

import yaml


ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
ONELAKE_STORAGE_RESOURCE = "https://storage.azure.com"


# ─── Auth ─────────────────────────────────────────────────────────────────────

def get_storage_token() -> str:
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", ONELAKE_STORAGE_RESOURCE],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["accessToken"]


# ─── Output helpers ───────────────────────────────────────────────────────────

def write_github_output(key: str, value: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"GITHUB_OUTPUT not set; {key}={value}", flush=True)


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

def fetch_artifact_mode(cfg: dict) -> bool:
    """Download manifest from the latest successful CD run on main. Returns True on success."""
    workflow = cfg.get("workflow", "")
    artifact_name = cfg.get("artifact_name", "prod-manifest")
    main_branch = cfg.get("main_branch", "main")
    repo = os.environ.get("REPO", "")
    head_sha = os.environ.get("HEAD_SHA", "")

    if not workflow:
        print("::error::prod_manifest_source.workflow is required for artifact mode.", file=sys.stderr)
        return False

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
        print(f"::warning::gh run list failed: {result.stderr.strip()}", flush=True)
        return False

    try:
        runs = json.loads(result.stdout)
    except ValueError:
        print("::warning::gh run list returned invalid JSON — falling back to greenfield.", flush=True)
        return False
    if not runs:
        print("::warning::No successful CD workflow runs found on main — falling back to greenfield.", flush=True)
        return False

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
            print(f"::warning::gh run download failed: {dl_result.stderr.strip()}", flush=True)
            return False

        manifest_src = os.path.join(tmpdir, "manifest.json")
        if not os.path.exists(manifest_src):
            print(f"::warning::manifest.json not found in artifact '{artifact_name}'.", flush=True)
            return False

        os.makedirs("prod-state", exist_ok=True)
        shutil.copy2(manifest_src, "prod-state/manifest.json")

    write_source_json(
        mode="artifact",
        source=f"{repo}/runs/{run_id} (SHA {run_sha[:8]})",
        head_sha=head_sha,
    )
    print(f"Artifact manifest fetched from run {run_id} (SHA {run_sha[:8]}).", flush=True)
    return True


def fetch_onelake_mode(cfg: dict) -> bool:
    """Download manifest from OneLake Files path via Fabric UAMI. Returns True on success."""
    workspace_id = cfg.get("workspace_id", "")
    lakehouse_id = cfg.get("lakehouse_id", "")
    file_path = cfg.get("file_path", "")
    head_sha = os.environ.get("HEAD_SHA", "")

    if not all([workspace_id, lakehouse_id, file_path]):
        print(
            "::error::prod_manifest_source.workspace_id, lakehouse_id, and file_path "
            "are all required for onelake mode.",
            file=sys.stderr,
        )
        return False

    token = get_storage_token()
    url = f"{ONELAKE_DFS}/{workspace_id}/{lakehouse_id}/{file_path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
    except urllib.error.HTTPError as e:
        level = "404" if e.code == 404 else f"HTTP {e.code}"
        print(
            f"::warning::OneLake manifest fetch failed ({level}) — falling back to greenfield.",
            flush=True,
        )
        return False

    os.makedirs("prod-state", exist_ok=True)
    with open("prod-state/manifest.json", "wb") as f:
        f.write(content)

    write_source_json(
        mode="onelake",
        source=f"{workspace_id}/{lakehouse_id}/{file_path}",
        head_sha=head_sha,
    )
    print(f"OneLake manifest fetched from {url}.", flush=True)
    return True


def fetch_greenfield() -> None:
    """Parse the current branch with the dbt_quality profile; use output as prod state."""
    head_sha = os.environ.get("HEAD_SHA", "")
    print("⚠️  No prod manifest available — running dbt parse for greenfield fallback.", flush=True)

    result = subprocess.run(
        ["dbt", "parse", "--profiles-dir", ".github/profiles", "--target", "dbt_quality", "--quiet"],
        capture_output=True, text=True,
    )

    os.makedirs("prod-state", exist_ok=True)
    manifest_src = "target/manifest.json"

    if result.returncode == 0 and os.path.exists(manifest_src):
        shutil.copy2(manifest_src, "prod-state/manifest.json")
        write_source_json(mode="greenfield", source="dbt parse (current branch)", head_sha=head_sha)
        return

    if result.returncode != 0:
        print(
            f"::warning::dbt parse failed (exit {result.returncode}): "
            f"{result.stderr[:300]}",
            flush=True,
        )
    # Minimal manifest so downstream jobs have a valid file to upload
    minimal = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "0.0.0",
        },
        "nodes": {},
        "sources": {},
    }
    with open("prod-state/manifest.json", "w") as f:
        json.dump(minimal, f, indent=2)
    write_source_json(mode="greenfield", source="minimal manifest (dbt parse unavailable)", head_sha=head_sha)


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    config_path = "ci-config.yml"
    if not os.path.exists(config_path):
        print("::warning::ci-config.yml not found — using greenfield fallback.", flush=True)
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()
    manifest_cfg = config.get("prod_manifest_source") or {}
    mode = manifest_cfg.get("mode", "artifact")

    success = False
    if mode == "artifact":
        success = fetch_artifact_mode(manifest_cfg)
    elif mode == "onelake":
        success = fetch_onelake_mode(manifest_cfg)
    else:
        print(f"::warning::Unknown prod_manifest_source.mode '{mode}' — using greenfield fallback.", flush=True)

    if not success:
        fetch_greenfield()
        write_github_output("greenfield_fallback", "true")
    else:
        write_github_output("greenfield_fallback", "false")

    print("fetch-prod-state complete.", flush=True)


if __name__ == "__main__":
    main()
