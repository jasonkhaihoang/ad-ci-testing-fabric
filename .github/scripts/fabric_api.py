"""
Fabric REST API wrapper for ephemeral workspace lifecycle management.

Commands:
  provision        --name NAME
                   Find or create workspace + lakehouse. Writes IDs to GITHUB_OUTPUT.

  teardown         --name NAME
                   Find workspace by name and delete it. Exits cleanly if not found.

  cleanup          --repo OWNER/REPO
                   List all vibedata-ephemeral-* workspaces. Delete those whose PR is closed.

  add-contributor  --workspace-id ID --github-login LOGIN
                   Add the PR author as Contributor via the Power BI REST API.
                   UPN is constructed as {github_login}@{AAD_DOMAIN}.
                   In production, GitHub SAML SSO ensures the login matches the UPN prefix.
                   Warns and continues if the user is not found; never blocks CI.

Authentication: GitHub OIDC via azure/login. No SPN credentials stored.
The workflow runs azure/login before invoking this script, establishing an
Azure CLI session. Token is acquired via: az account get-access-token.

Required env var:
  AZURE_KEYVAULT_URL — used by kv_utils to fetch vibedata-fabric-capacity-id

Optional env vars (add-contributor):
  AAD_DOMAIN        — UPN suffix (default: eng.acceleratedata.ai)
  AAD_UPN_OVERRIDE  — Use this UPN directly, bypassing {github_login}@{AAD_DOMAIN}
                      construction. Useful when GitHub login does not match AAD UPN
                      prefix (e.g. personal GitHub accounts not provisioned via SSO).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


FABRIC_API = "https://api.fabric.microsoft.com/v1"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
GITHUB_API = "https://api.github.com"
ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"

AAD_DOMAIN_DEFAULT = "eng.acceleratedata.ai"


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _get_az_token(resource: str) -> str:
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["accessToken"]


def get_fabric_token() -> str:
    return _get_az_token("https://api.fabric.microsoft.com")


def get_powerbi_token() -> str:
    """api.powerbi.com requires a different OAuth audience from api.fabric.microsoft.com.
    Same UAMI session, no additional Azure permissions needed.
    """
    return _get_az_token("https://analysis.windows.net/powerbi/api")


def get_storage_token() -> str:
    """OneLake DFS API requires the Azure Storage audience."""
    return _get_az_token("https://storage.azure.com")


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def fabric_request(method: str, path: str, token: str, body: dict = None, retries: int = 3):
    """Make a Fabric REST API call with retry on 429/500/503."""
    url = f"{FABRIC_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < retries - 1:
                retry_after = int(e.headers.get("Retry-After", 5)) if e.code != 500 else 1
                print(f"HTTP {e.code}, retrying in {retry_after}s…", flush=True)
                time.sleep(retry_after)
                continue
            body_text = e.read().decode(errors="replace")
            print(f"HTTP {e.code} {method} {url}: {body_text}", file=sys.stderr)
            raise
    raise RuntimeError(f"Failed after {retries} retries: {method} {path}")


# ─── Workspace helpers ─────────────────────────────────────────────────────────

def find_workspace_by_name(name: str, token: str) -> dict | None:
    resp = fabric_request("GET", "/workspaces", token)
    for ws in resp.get("value", []):
        if ws["displayName"] == name:
            return ws
    return None


def find_lakehouse_by_name(workspace_id: str, name: str, token: str) -> dict | None:
    resp = fabric_request("GET", f"/workspaces/{workspace_id}/items", token)
    for item in resp.get("value", []):
        if item["type"] == "Lakehouse" and item["displayName"] == name:
            return item
    return None


def write_github_output(key: str, value: str):
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"GITHUB_OUTPUT not set; {key}={value}")


# ─── Contributor helper ───────────────────────────────────────────────────────

def add_workspace_user(workspace_id: str, upn: str, token: str):
    """Add a user as Contributor on the workspace by UPN via the Power BI REST API.

    The Power BI groups/users endpoint accepts the UPN (email address) directly —
    no AAD object ID lookup required. The call is idempotent: if the user already
    has access their role is updated. If the UPN is not found in AAD the API
    returns an error; we log a warning and continue without blocking CI.
    """
    url = f"{POWERBI_API}/groups/{workspace_id}/users"
    data = json.dumps({"emailAddress": upn, "groupUserAccessRight": "Contributor"}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
        print(f"Added '{upn}' as Contributor on workspace {workspace_id}.", flush=True)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        print(
            f"Warning: could not add '{upn}' as Contributor (HTTP {e.code}): {body_text}. "
            "Skipping — provisioning continues.",
            flush=True,
        )


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_provision(args):
    token = get_fabric_token()
    capacity_id = os.environ["FABRIC_CAPACITY_ID"]  # written to GITHUB_ENV by kv_utils fetch-fabric
    name = args.name

    ws = find_workspace_by_name(name, token)
    if ws:
        workspace_id = ws["id"]
        print(f"Reusing existing workspace: {name} ({workspace_id})", flush=True)
    else:
        print(f"Creating workspace: {name}", flush=True)
        ws = fabric_request("POST", "/workspaces", token, {
            "displayName": name,
            "capacityId": capacity_id,
        })
        workspace_id = ws["id"]
        print(f"Workspace created: {workspace_id}", flush=True)

    lakehouse_name = os.environ.get("EPHEMERAL_LAKEHOUSE_NAME", "vdephelh")

    lh = find_lakehouse_by_name(workspace_id, lakehouse_name, token)
    if lh:
        lakehouse_id = lh["id"]
        print(f"Reusing existing lakehouse: {lakehouse_name} ({lakehouse_id})", flush=True)
    else:
        print(f"Creating lakehouse: {lakehouse_name}", flush=True)
        lh = fabric_request("POST", f"/workspaces/{workspace_id}/items", token, {
            "displayName": lakehouse_name,
            "type": "Lakehouse",
        })
        lakehouse_id = lh["id"]
        print(f"Lakehouse created: {lakehouse_id}", flush=True)

    write_github_output("workspace_id", workspace_id)
    write_github_output("lakehouse_id", lakehouse_id)
    write_github_output("lakehouse_name", lakehouse_name)
    print(f"Provision complete: workspace={workspace_id} lakehouse={lakehouse_id} ({lakehouse_name})", flush=True)


def cmd_teardown(args):
    token = get_fabric_token()
    ws = find_workspace_by_name(args.name, token)
    if not ws:
        print(f"Workspace not found: {args.name} — nothing to teardown.", flush=True)
        return
    workspace_id = ws["id"]
    print(f"Deleting workspace: {args.name} ({workspace_id})", flush=True)
    fabric_request("DELETE", f"/workspaces/{workspace_id}", token)
    print("Workspace deleted.", flush=True)


def cmd_cleanup(args):
    token = get_fabric_token()
    gh_token = os.environ.get("GH_TOKEN", "")
    repo = args.repo

    resp = fabric_request("GET", "/workspaces", token)
    ephemeral = [
        ws for ws in resp.get("value", [])
        if ws["displayName"].startswith("vibedata-ephemeral-")
    ]
    print(f"Found {len(ephemeral)} ephemeral workspace(s).", flush=True)
    deleted = 0

    for ws in ephemeral:
        name = ws["displayName"]
        parts = name.split("-")
        if len(parts) < 3 or not parts[-1].isdigit():
            continue
        pr_number = parts[-1]

        pr_url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        req = urllib.request.Request(pr_url)
        req.add_header("Authorization", f"Bearer {gh_token}")
        req.add_header("Accept", "application/vnd.github+json")

        try:
            with urllib.request.urlopen(req) as r:
                pr_state = json.loads(r.read()).get("state", "unknown")
        except urllib.error.HTTPError as e:
            pr_state = "not_found" if e.code == 404 else None
            if pr_state is None:
                print(f"  Skipping {name}: GitHub API error {e.code}", flush=True)
                continue

        if pr_state in ("closed", "not_found"):
            print(f"  Deleting orphan: {name} (PR #{pr_number} is {pr_state})", flush=True)
            try:
                fabric_request("DELETE", f"/workspaces/{ws['id']}", token)
                deleted += 1
            except Exception as exc:
                print(f"  Failed to delete {name}: {exc}", file=sys.stderr)
        else:
            print(f"  Skipping: {name} (PR #{pr_number} is {pr_state})", flush=True)

    print(f"Cleanup complete: {deleted} workspace(s) deleted.", flush=True)


def cmd_add_contributor(args):
    """Add the PR author as Contributor on the ephemeral workspace.

    UPN resolution order:
      1. AAD_UPN_OVERRIDE env var — used as-is (dev/test escape hatch for accounts
         whose GitHub login does not match their AAD UPN prefix, e.g. personal GitHub
         accounts joined via Google OAuth rather than SAML SSO).
      2. {github_login}@{AAD_DOMAIN} — correct in production where GitHub SAML SSO
         enforces that the GitHub login equals the AAD UPN prefix.
    """
    upn = os.environ.get("AAD_UPN_OVERRIDE", "").strip()
    if upn:
        print(f"Using AAD_UPN_OVERRIDE: {upn}", flush=True)
    else:
        aad_domain = os.environ.get("AAD_DOMAIN", "").strip() or AAD_DOMAIN_DEFAULT
        upn = f"{args.github_login}@{aad_domain}"
        print(f"Constructed UPN: {upn}", flush=True)

    token = get_powerbi_token()
    add_workspace_user(args.workspace_id, upn, token)


# ─── OneLake Files upload ─────────────────────────────────────────────────────

def _dfs_request(method: str, url: str, token: str, data: bytes = None, params: dict = None) -> None:
    """Execute an ADLS Gen2 DFS REST API call; raises on non-2xx."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    # Always set Content-Length: ADLS Gen2 requires Content-Length: 0 on flush PATCH.
    effective_data = data if data is not None else b""
    req = urllib.request.Request(url, data=effective_data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Length", str(len(effective_data)))
    try:
        with urllib.request.urlopen(req):
            pass
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        print(f"HTTP {e.code} {method} {url}: {body_text}", file=sys.stderr)
        raise


def upload_onelake_file(
    workspace_id: str, lakehouse_id: str, local_path: str, remote_path: str, token: str
) -> str:
    """Upload a local file to OneLake via the ADLS Gen2 DFS three-step protocol.

    Steps: CREATE (PUT ?resource=file) → APPEND → FLUSH.
    Returns the ABFSS URI of the uploaded file.
    """
    if ".." in remote_path.split("/"):
        raise ValueError(f"remote_path must not contain '..' components: {remote_path!r}")

    url = f"{ONELAKE_DFS}/{workspace_id}/{lakehouse_id}/{remote_path}"

    with open(local_path, "rb") as f:
        data = f.read()
    size = len(data)

    _dfs_request("PUT", url, token, params={"resource": "file"})
    print(f"OneLake file path created: {remote_path}", flush=True)

    _dfs_request("PATCH", url, token, data=data, params={"action": "append", "position": "0"})
    print(f"Data appended ({size} bytes).", flush=True)

    _dfs_request("PATCH", url, token, params={"action": "flush", "position": str(size)})
    print("File flushed and committed.", flush=True)

    return f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/{remote_path}"


def cmd_upload_file(args):
    token = get_storage_token()
    abfss = upload_onelake_file(
        args.workspace_id, args.lakehouse_id, args.local_path, args.remote_path, token
    )
    write_github_output("abfss_path", abfss)
    print(f"ABFSS URI: {abfss}", flush=True)


# ─── Shortcut seeding ─────────────────────────────────────────────────────────

SHORTCUT_REPORT_PATH = "reports/shortcut_seeding.json"


def _build_shortcut_body(entry: dict) -> dict:
    """Pure: derive the Fabric Shortcuts API request body from a manifest entry."""
    return {
        "name": entry["alias"],
        "path": "Tables",
        "target": {
            "oneLake": {
                "workspaceId": entry["source_workspace_id"],
                "itemId": entry["source_lakehouse_id"],
                "path": entry["source_path"],
            }
        },
    }


def _merge_seeding_report(existing: dict, created: int, already_existed: int) -> dict:
    """Pure: merge counts into the existing report, preserving any other keys
    (e.g. `derived`, `zero_state` written by Slice 1)."""
    merged = dict(existing)
    merged["created"] = created
    merged["already_existed"] = already_existed
    return merged


def _write_seeding_report(report_path: str, created: int, already_existed: int) -> None:
    """Read-modify-write the shortcut-seeding report, preserving Slice 1 keys."""
    existing: dict = {}
    if os.path.exists(report_path):
        with open(report_path) as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = {}
    merged = _merge_seeding_report(existing, created, already_existed)
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(merged, f, indent=2)


def seed_shortcuts(
    from_file: str,
    workspace_id: str,
    lakehouse_id: str,
    token: str,
    report_path: str = SHORTCUT_REPORT_PATH,
) -> None:
    """Read derived shortcuts list and POST each to the Fabric Shortcuts API.

    Per-entry behaviour:
      * 201 Created → success counted toward `created`.
      * 409 Conflict → idempotent; counted toward `already_existed`.
      * 403 Forbidden → SystemExit with alias + source path + Viewer-on-prod hint.
      * 404 Not Found → SystemExit with alias + source path; halts further entries.
      * Other errors (incl. 500 after fabric_request retry exhaustion) → SystemExit
        with the alias.

    Updates `report_path` with merged `{created, already_existed}` counts; preserves
    `derived`/`zero_state` keys written by Slice 1 (read-modify-write).
    """
    with open(from_file) as f:
        entries = json.load(f)

    if not entries:
        print("No shortcuts to seed", flush=True)
        return

    created = 0
    already_existed = 0
    api_path = f"/workspaces/{workspace_id}/items/{lakehouse_id}/shortcuts"

    for entry in entries:
        body = _build_shortcut_body(entry)
        try:
            fabric_request("POST", api_path, token, body)
            created += 1
            print(f"Created shortcut: {entry['alias']}", flush=True)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                already_existed += 1
                print(f"Shortcut already exists: {entry['alias']}", flush=True)
                continue
            if e.code == 403:
                raise SystemExit(
                    f"Forbidden creating shortcut '{entry['alias']}' for source "
                    f"'{entry['source_path']}'. The Fabric UAMI must have the "
                    "Viewer role on the production workspace."
                )
            if e.code == 404:
                raise SystemExit(
                    f"Source not found for shortcut '{entry['alias']}': "
                    f"'{entry['source_path']}'."
                )
            raise SystemExit(
                f"Failed creating shortcut '{entry['alias']}' (HTTP {e.code})."
            )

    _write_seeding_report(report_path, created, already_existed)


def cmd_seed_shortcuts(args):
    token = get_fabric_token()
    seed_shortcuts(args.from_file, args.workspace_id, args.lakehouse_id, token)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fabric ephemeral workspace CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("provision").add_argument("--name", required=True)
    sub.add_parser("teardown").add_argument("--name", required=True)
    sub.add_parser("cleanup").add_argument("--repo", required=True)
    p = sub.add_parser("add-contributor")
    p.add_argument("--workspace-id", required=True)
    p.add_argument("--github-login", required=True)
    p2 = sub.add_parser("upload-file")
    p2.add_argument("--workspace-id", required=True)
    p2.add_argument("--lakehouse-id", required=True)
    p2.add_argument("--local-path", required=True)
    p2.add_argument("--remote-path", required=True)
    p3 = sub.add_parser("seed-shortcuts")
    p3.add_argument("--from-file", required=True)
    p3.add_argument("--workspace-id", required=True)
    p3.add_argument("--lakehouse-id", required=True)
    args = parser.parse_args()
    {
        "provision": cmd_provision,
        "teardown": cmd_teardown,
        "cleanup": cmd_cleanup,
        "add-contributor": cmd_add_contributor,
        "upload-file": cmd_upload_file,
        "seed-shortcuts": cmd_seed_shortcuts,
    }[args.command](args)


if __name__ == "__main__":
    main()
