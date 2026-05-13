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
                   Add the PR author as Member via the Power BI REST API.
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
import sys
import urllib.error
import urllib.parse
import urllib.request

import fabric_transport
import runner_io

try:
    from scripts import shortcut_seeding_report
except ImportError:  # invoked as `python3 path/to/fabric_api.py`
    import shortcut_seeding_report


GITHUB_API = "https://api.github.com"
ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"

AAD_DOMAIN_DEFAULT = "eng.acceleratedata.ai"


# ─── Workspace helpers ─────────────────────────────────────────────────────────

def find_workspace_by_name(name: str) -> dict | None:
    resp = fabric_transport.request("GET", "/workspaces")
    for ws in resp.get("value", []):
        if ws["displayName"] == name:
            return ws
    return None


def find_lakehouse_by_name(workspace_id: str, name: str) -> dict | None:
    resp = fabric_transport.request("GET", f"/workspaces/{workspace_id}/items")
    for item in resp.get("value", []):
        if item["type"] == "Lakehouse" and item["displayName"] == name:
            return item
    return None


# ─── Member role helper ───────────────────────────────────────────────────────

def add_workspace_user(workspace_id: str, upn: str):
    """Add a user as Member on the workspace by UPN via the Power BI REST API.

    The Power BI groups/users endpoint accepts the UPN (email address) directly —
    no AAD object ID lookup required. The call is idempotent: if the user already
    has access their role is updated. If the UPN is not found in AAD the API
    returns an error; we log a warning and continue without blocking CI.
    """
    try:
        fabric_transport.request(
            "POST", f"/groups/{workspace_id}/users",
            {"emailAddress": upn, "groupUserAccessRight": "Member"},
            audience="powerbi",
        )
        print(f"Added '{upn}' as Member on workspace {workspace_id}.", flush=True)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        print(
            f"Warning: could not add '{upn}' as Member (HTTP {e.code}): {body_text}. "
            "Skipping — provisioning continues.",
            flush=True,
        )


# ─── Commands ─────────────────────────────────────────────────────────────────

def cmd_provision(args):
    capacity_id = os.environ["FABRIC_CAPACITY_ID"]  # written to GITHUB_ENV by kv_utils fetch-fabric
    name = args.name

    ws = find_workspace_by_name(name)
    if ws:
        workspace_id = ws["id"]
        print(f"Reusing existing workspace: {name} ({workspace_id})", flush=True)
    else:
        print(f"Creating workspace: {name}", flush=True)
        ws = fabric_transport.request("POST", "/workspaces", {
            "displayName": name,
            "capacityId": capacity_id,
        })
        workspace_id = ws["id"]
        print(f"Workspace created: {workspace_id}", flush=True)

    lakehouse_name = os.environ.get("EPHEMERAL_LAKEHOUSE_NAME", "vdephelh")

    lh = find_lakehouse_by_name(workspace_id, lakehouse_name)
    if lh:
        lakehouse_id = lh["id"]
        print(f"Reusing existing lakehouse: {lakehouse_name} ({lakehouse_id})", flush=True)
    else:
        print(f"Creating lakehouse: {lakehouse_name}", flush=True)
        lh = fabric_transport.request("POST", f"/workspaces/{workspace_id}/items", {
            "displayName": lakehouse_name,
            "type": "Lakehouse",
            "creationPayload": {"enableSchemas": True},
        })
        lakehouse_id = lh["id"]
        print(f"Lakehouse created: {lakehouse_id}", flush=True)

    runner_io.set_output("workspace_id", workspace_id)
    runner_io.set_output("lakehouse_id", lakehouse_id)
    runner_io.set_output("lakehouse_name", lakehouse_name)
    print(f"Provision complete: workspace={workspace_id} lakehouse={lakehouse_id} ({lakehouse_name})", flush=True)


def cmd_teardown(args):
    ws = find_workspace_by_name(args.name)
    if not ws:
        print(f"Workspace not found: {args.name} — nothing to teardown.", flush=True)
        return
    workspace_id = ws["id"]
    print(f"Deleting workspace: {args.name} ({workspace_id})", flush=True)
    fabric_transport.request("DELETE", f"/workspaces/{workspace_id}")
    print("Workspace deleted.", flush=True)


def cmd_cleanup(args):
    gh_token = os.environ.get("GH_TOKEN", "")
    repo = args.repo

    resp = fabric_transport.request("GET", "/workspaces")
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
                fabric_transport.request("DELETE", f"/workspaces/{ws['id']}")
                deleted += 1
            except Exception as exc:
                print(f"  Failed to delete {name}: {exc}", file=sys.stderr)
        else:
            print(f"  Skipping: {name} (PR #{pr_number} is {pr_state})", flush=True)

    print(f"Cleanup complete: {deleted} workspace(s) deleted.", flush=True)


def cmd_add_contributor(args):
    """Add the PR author as Member on the ephemeral workspace.

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

    add_workspace_user(args.workspace_id, upn)


# ─── OneLake Files upload ─────────────────────────────────────────────────────

def upload_onelake_file(
    workspace_id: str, lakehouse_id: str, local_path: str, remote_path: str
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

    fabric_transport.dfs_request("PUT", url, params={"resource": "file"})
    print(f"OneLake file path created: {remote_path}", flush=True)

    fabric_transport.dfs_request("PATCH", url, data=data, params={"action": "append", "position": "0"})
    print(f"Data appended ({size} bytes).", flush=True)

    fabric_transport.dfs_request("PATCH", url, params={"action": "flush", "position": str(size)})
    print("File flushed and committed.", flush=True)

    return f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/{remote_path}"


def cmd_upload_file(args):
    abfss = upload_onelake_file(
        args.workspace_id, args.lakehouse_id, args.local_path, args.remote_path
    )
    runner_io.set_output("abfss_path", abfss)
    print(f"ABFSS URI: {abfss}", flush=True)


# ─── Shortcut seeding ─────────────────────────────────────────────────────────


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


def seed_shortcuts(
    from_file: str,
    workspace_id: str,
    lakehouse_id: str,
    report_path: str = shortcut_seeding_report.DEFAULT_PATH,
) -> None:
    """Read derived shortcuts list and POST each to the Fabric Shortcuts API.

    Per-entry behaviour:
      * 201 Created → success counted toward `created`.
      * 409 Conflict → idempotent; counted toward `already_existed`.
      * 403 Forbidden → SystemExit with alias + source path + Viewer-on-prod hint.
      * 404 Not Found → SystemExit with alias + source path; halts further entries.
      * Other errors (incl. 500 after fabric_transport retry exhaustion) → SystemExit
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
            fabric_transport.request("POST", api_path, body)
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

    shortcut_seeding_report.set_seeding(created, already_existed, path=report_path)


def cmd_seed_shortcuts(args):
    seed_shortcuts(args.from_file, args.workspace_id, args.lakehouse_id)


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
