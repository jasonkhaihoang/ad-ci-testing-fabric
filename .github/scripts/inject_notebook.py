"""
Notebook injection script.

Reads the notebook matching NOTEBOOK_GLOB from the repo, substitutes the
Parameters cell with ephemeral workspace values, inserts a Clone cell before
the Build cell, then uploads the modified notebook to the Fabric workspace
via the Items API.

The notebook in the repo stores template placeholders ({{BRANCH}}, etc.).
This script substitutes them at CI runtime without committing back to the branch,
avoiding re-triggering the CI workflow.

Authentication: GitHub OIDC via azure/login (no SPN credentials stored).
Token acquired via: az account get-access-token.

Environment variables required:
  AZURE_KEYVAULT_URL  — Key Vault URI (used by kv_utils to fetch GitHub App secrets)
  EPHEMERAL_WORKSPACE_ID, EPHEMERAL_WORKSPACE_NAME, EPHEMERAL_LAKEHOUSE_ID
  NOTEBOOK_GLOB       — glob pattern (e.g. intents/**/notebook.ipynb)
  HEAD_BRANCH         — feature branch name
  REPO_URL            — GitHub repo clone URL
  GH_APP_ID_KV_NAME, GH_INSTALLATION_ID_KV_NAME, GH_APP_PEM_KV_NAME  — KV secret name references
"""

import base64
import copy
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_POLL_TIMEOUT_S = 120
FABRIC_POLL_INTERVAL_S = 5


def get_fabric_token() -> str:
    """Get a Fabric access token from the Azure CLI OIDC session."""
    result = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://api.fabric.microsoft.com"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["accessToken"]


def fabric_request(method: str, path: str, token: str, body: dict = None) -> dict:
    url = f"{FABRIC_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {method} {url}: {e.read().decode(errors='replace')}", file=sys.stderr)
        raise


def _fabric_post(path: str, token: str, body: dict) -> tuple[int, str | None, dict]:
    """POST to Fabric API; returns (status_code, operation_url_or_None, body_dict).

    The operation URL is sourced from the Location response header, falling back
    to constructing it from operationId in the response body. Returns None when
    neither is present (callers must raise if they required an operation URL).
    """
    url = f"{FABRIC_API}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            location = resp.getheader("Location")
            raw = resp.read()
            parsed = json.loads(raw) if raw else {}
            if not location and "operationId" in parsed:
                location = f"{FABRIC_API}/operations/{parsed['operationId']}"
            return status, location, parsed
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} POST {url}: {e.read().decode(errors='replace')}", file=sys.stderr)
        raise


def poll_fabric_operation(
    operation_url: str | None,
    token: str,
    timeout_s: int = FABRIC_POLL_TIMEOUT_S,
    poll_interval_s: int = FABRIC_POLL_INTERVAL_S,
) -> None:
    """Poll a Fabric long-running operation URL until Succeeded; raise on failure or timeout."""
    if not operation_url:
        raise RuntimeError(
            "Fabric returned 202 Accepted but no operation URL was found in the "
            "Location header or response body."
        )
    print(f"Polling Fabric operation: {operation_url}", flush=True)
    deadline = time.monotonic() + timeout_s
    last_status = "Unknown"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        req = urllib.request.Request(operation_url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise RuntimeError(
                f"Failed to poll Fabric operation (HTTP {e.code}): {body_text}"
            ) from e
        last_status = result.get("status", "Unknown")
        print(f"  Operation status [{attempt}]: {last_status}", flush=True)
        if last_status == "Succeeded":
            return
        if last_status == "Failed":
            error = result.get("error", {})
            msg = f"{error.get('errorCode', 'UnknownError')}: {error.get('message', str(result))}"
            raise RuntimeError(f"Fabric operation failed — {msg}")
        time.sleep(poll_interval_s)
    raise RuntimeError(
        f"Fabric operation timed out after {timeout_s}s "
        f"(last status: {last_status!r}). "
        "The notebook may not be available in the workspace."
    )


def ipynb_to_fabric_py(notebook: dict) -> str:
    """Convert a Jupyter notebook dict to Fabric's Python notebook format.

    Fabric Items API requires path='notebook-content.py' with content in this
    format — standard Jupyter JSON with path='notebook-content.ipynb' is rejected.
    """
    lines = ["# Fabric notebook source\n"]

    metadata = notebook.get("metadata", {})
    if metadata:
        lines.append("\n# METADATA ********************\n")
        for key, value in metadata.items():
            lines.append(f"# META {json.dumps({key: value})}\n")

    for cell in notebook.get("cells", []):
        cell_type = cell.get("cell_type", "code")
        tags = cell.get("metadata", {}).get("tags", [])
        source = "".join(cell.get("source", []))

        if cell_type == "code":
            if "parameters" in tags:
                lines.append("\n# PARAMETERS CELL ********************\n\n")
            else:
                lines.append("\n# CELL ********************\n\n")
            lines.append(source)
            if source and not source.endswith("\n"):
                lines.append("\n")
        elif cell_type == "markdown":
            lines.append("\n# MARKDOWN CELL ********************\n\n")
            for md_line in source.splitlines(keepends=True):
                lines.append(f"# {md_line}" if md_line.strip() else "#\n")

    return "".join(lines)


def find_notebook(glob_pattern: str) -> str | None:
    matches = glob.glob(glob_pattern, recursive=True)
    if not matches:
        print(
            f"No notebook found matching '{glob_pattern}' — injection aborted.",
            file=sys.stderr, flush=True,
        )
        return None
    if len(matches) > 1:
        print(f"Multiple notebooks found: {matches}. Using first: {matches[0]}", flush=True)
    return matches[0]


def substitute_parameters_cell(notebook: dict) -> dict:
    """Replace placeholder values in the Parameters cell."""
    nb = copy.deepcopy(notebook)

    workspace_id = os.environ["EPHEMERAL_WORKSPACE_ID"]
    workspace_name = os.environ["EPHEMERAL_WORKSPACE_NAME"]
    lakehouse_id = os.environ["EPHEMERAL_LAKEHOUSE_ID"]
    lakehouse_name = os.environ.get("EPHEMERAL_LAKEHOUSE_NAME", "vdephelh")
    branch = os.environ["HEAD_BRANCH"]
    repo_url = os.environ["REPO_URL"]
    github_app_id = os.environ.get("GH_APP_ID_KV_NAME", "")
    github_installation_id = os.environ.get("GH_INSTALLATION_ID_KV_NAME", "")
    github_pem_secret = os.environ.get("GH_APP_PEM_KV_NAME", "")
    vault_url = os.environ.get("AZURE_KEYVAULT_URL", "")
    # PROD_STATE_ABFSS is set by the provision job after uploading manifest.json to OneLake.
    # Falls back to ./prod-state when unset OR empty (GitHub Actions outputs empty string for
    # unset step outputs, so we must guard against both cases).
    prod_state_abfss = os.environ.get("PROD_STATE_ABFSS", "").strip() or "./prod-state"

    # Build the substituted parameters cell source.
    # The command uses notebook-runtime f-strings ({prod_state_path}) — the braces are
    # escaped here so inject_notebook.py does not substitute them at injection time.
    new_params = [
        "# Parameters — injected by CI (do not edit manually)\n",
        f'prod_state_path = "{prod_state_abfss}"\n',
        'command = ["dbt deps", f"dbt build --select state:modified+ --defer --state {prod_state_path} --target ci-ephemeral", f"dbt test --select state:modified+ --store-failures --target ci-ephemeral"]\n',
        f'repo_url = "{repo_url}"\n',
        f'repo_branch = "{branch}"\n',
        f'github_app_id = "{github_app_id}"\n',
        f'github_installation_id = "{github_installation_id}"\n',
        f'github_pem_secret = "{github_pem_secret}"\n',
        f'vault_url = "{vault_url}"\n',
        f'lakehouse_name = "{lakehouse_name}"\n',
        f'lakehouse_id = "{lakehouse_id}"\n',
        f'workspace_id = "{workspace_id}"\n',
        f'workspace_name = "{workspace_name}"\n',
        'schema_name = "dbo"\n',
    ]

    # Find and replace the Parameters cell (first cell with "Parameters" comment or tag)
    params_cell_idx = None
    for i, cell in enumerate(nb.get("cells", [])):
        source = "".join(cell.get("source", []))
        if "Parameters" in source and cell.get("cell_type") == "code":
            params_cell_idx = i
            break

    if params_cell_idx is None:
        # Insert as the first code cell if no Parameters cell found
        print("Warning: No Parameters cell found. Inserting at position 0.", flush=True)
        params_cell_idx = 0
        nb["cells"].insert(0, {
            "cell_type": "code",
            "source": new_params,
            "metadata": {"tags": ["parameters"]},
            "outputs": [],
            "execution_count": None,
        })
    else:
        nb["cells"][params_cell_idx]["source"] = new_params

    return nb, params_cell_idx


def insert_clone_cell(notebook: dict, params_idx: int) -> dict:
    """Insert a Clone cell immediately before the Build cell.

    Searches for the first cell after params_idx that contains run_dbt_job
    and inserts before it. Falls back to inserting after params_idx with a
    warning if no Build cell is found.
    """
    nb = copy.deepcopy(notebook)

    build_idx = None
    for i in range(params_idx + 1, len(nb["cells"])):
        if "run_dbt_job" in "".join(nb["cells"][i].get("source", [])):
            build_idx = i
            break

    if build_idx is None:
        print("Warning: No Build cell found. Inserting Clone cell after Parameters as fallback.", flush=True)
        insert_idx = params_idx + 1
    else:
        insert_idx = build_idx

    clone_cell = {
        "cell_type": "code",
        "source": [
            "# Clone: Reset D and D+ to prod state\n",
            "# Re-run this cell at any time to reset between test iterations.\n",
            "from dbt.adapters.fabricspark.notebook import run_dbt_job, DbtJobConfig, RepoConfig, ConnectionConfig\n",
            "\n",
            "clone_config = DbtJobConfig(\n",
            '    command=["dbt deps", f"dbt clone --select state:modified+ --defer --state {prod_state_path} --target ci-ephemeral"],\n',
            "    repo=RepoConfig(\n",
            "        url=repo_url,\n",
            "        branch=repo_branch,\n",
            "        github_app_id=github_app_id,\n",
            "        github_installation_id=github_installation_id,\n",
            "        github_pem_secret=github_pem_secret,\n",
            "        vault_url=vault_url,\n",
            "    ),\n",
            "    connection=ConnectionConfig(\n",
            '        lakehouse_name=lakehouse_name,\n',
            "        lakehouse_id=lakehouse_id,\n",
            "        workspace_id=workspace_id,\n",
            "        workspace_name=workspace_name,\n",
            "        schema_name=schema_name,\n",
            "    ),\n",
            ")\n",
            "run_dbt_job(clone_config)\n",
        ],
        "metadata": {"tags": ["ci-injected-clone"]},
        "outputs": [],
        "execution_count": None,
    }

    nb["cells"].insert(insert_idx, clone_cell)
    return nb


def find_existing_notebook(workspace_id: str, display_name: str, token: str) -> str | None:
    """Return item ID of an existing notebook with the given display name, or None."""
    resp = fabric_request("GET", f"/workspaces/{workspace_id}/items", token)
    for item in resp.get("value", []):
        if item["type"] == "Notebook" and item["displayName"] == display_name:
            return item["id"]
    return None


def upload_notebook(workspace_id: str, display_name: str, notebook: dict, token: str):
    """Create or update a notebook in the Fabric workspace via Items API."""
    nb_content = base64.b64encode(ipynb_to_fabric_py(notebook).encode()).decode()
    definition = {
        "parts": [{
            "path": "notebook-content.py",
            "payload": nb_content,
            "payloadType": "InlineBase64",
        }]
    }

    existing_id = find_existing_notebook(workspace_id, display_name, token)

    if existing_id:
        print(f"Updating existing notebook: {display_name} ({existing_id})", flush=True)
        status, op_url, _ = _fabric_post(
            f"/workspaces/{workspace_id}/items/{existing_id}/updateDefinition",
            token,
            {"definition": definition},
        )
    else:
        print(f"Creating notebook: {display_name}", flush=True)
        status, op_url, _ = _fabric_post(
            f"/workspaces/{workspace_id}/items",
            token,
            {"displayName": display_name, "type": "Notebook", "definition": definition},
        )

    if status == 202:
        poll_fabric_operation(op_url, token)
        print(f"Notebook '{display_name}' is now available in the workspace.", flush=True)
    elif status in (200, 201):
        print("Notebook upload complete.", flush=True)
    else:
        raise RuntimeError(
            f"Unexpected HTTP {status} from Fabric Items API. "
            "Expected 200, 201, or 202."
        )


def main():
    token = get_fabric_token()
    workspace_id = os.environ["EPHEMERAL_WORKSPACE_ID"]
    notebook_glob = os.environ["NOTEBOOK_GLOB"]

    notebook_path = find_notebook(notebook_glob)
    if notebook_path is None:
        sys.exit(1)
    print(f"Found notebook: {notebook_path}", flush=True)

    with open(notebook_path) as f:
        notebook = json.load(f)

    # Step 1: Substitute Parameters cell
    notebook, params_idx = substitute_parameters_cell(notebook)
    print(f"Parameters cell substituted (cell index {params_idx}).", flush=True)

    # Step 2: Insert Clone cell after Parameters cell
    notebook = insert_clone_cell(notebook, params_idx)
    print("Clone cell inserted.", flush=True)

    # Step 3: Upload to Fabric workspace
    display_name = os.path.splitext(os.path.basename(notebook_path))[0]
    upload_notebook(workspace_id, display_name, notebook, token)


if __name__ == "__main__":
    main()
