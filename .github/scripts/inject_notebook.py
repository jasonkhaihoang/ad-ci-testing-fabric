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
import sys

import fabric_transport
import runner_io



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
    if prod_state_abfss.endswith("/manifest.json"):
        prod_state_abfss = prod_state_abfss[:-len("/manifest.json")]
    # CI_TARGET is the dbt profile target name used by Slim CI Build/Test/Clone commands.
    # Sourced from ci-config.yml::ci_target via preflight output. Defaults to "ephemeral_ci"
    # when omitted; domain repos can override to match their own profiles.yml convention.
    ci_target = os.environ.get("CI_TARGET", "").strip() or "ephemeral_ci"

    # Read clone_models.json (produced by derive_clone_shortcuts.py). Defaults to []
    # when the file is absent (greenfield or step skipped).
    clone_models_path = os.environ.get("CLONE_MODELS_PATH", "clone_models.json")
    shallow_clone_models = []
    if os.path.exists(clone_models_path):
        try:
            with open(clone_models_path) as _f:
                shallow_clone_models = json.load(_f) or []
        except (ValueError, OSError):
            shallow_clone_models = []

    head_sha = os.environ.get("HEAD_SHA", "").strip()
    prod_workspace_name = os.environ.get("PROD_WORKSPACE_NAME", "").strip()
    prod_lakehouse_name = os.environ.get("PROD_LAKEHOUSE_NAME", "").strip()

    # Build the substituted parameters cell source.
    # Commands use notebook-runtime f-strings ({local_prod_state_path}) — the braces are
    # escaped here so inject_notebook.py does not substitute them at injection time.
    # local_prod_state_path is set by the download cell injected before this cell.
    new_params = [
        "# Parameters — injected by CI (do not edit manually)\n",
        f'prod_state_path = "{prod_state_abfss}"\n',
        f'ci_target = "{ci_target}"\n',
        'dep_command = ["dbt deps"]\n',
        'clone_command = [f"dbt clone --select state:modified+ --state {local_prod_state_path} --profiles-dir .github/profiles --target {ci_target}"]\n',
        'build_command = ["dbt deps", f"dbt build --select state:modified+ --state {local_prod_state_path} --profiles-dir .github/profiles --target {ci_target}"]\n',
        'test_command = [f"dbt test --select state:modified+ --store-failures --profiles-dir .github/profiles --target {ci_target}"]\n',
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
        f'shallow_clone_models = {json.dumps(shallow_clone_models)}\n',
        'run_mode = "interactive"\n',
        'gate = "2"\n',
        'ci_run_id = ""\n',
        f'head_sha = "{head_sha}"\n',
        f'prod_workspace_name = "{prod_workspace_name}"\n',
        f'prod_lakehouse_name = "{prod_lakehouse_name}"\n',
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


def insert_download_cell(notebook: dict, params_idx: int) -> tuple[dict, int]:
    """Insert a download cell BEFORE the Parameters cell.

    Downloads manifest.json from the ABFSS URI in prod_state_path to /tmp/prod-state/
    and sets local_prod_state_path = local_dir. prod_state_path is left unchanged
    (remains the ABFSS URI). The Parameters cell — which uses local_prod_state_path
    in its f-string commands — runs after this cell so local_prod_state_path is
    already defined when those f-strings are evaluated.

    Returns (nb, params_idx + 1) where params_idx + 1 is the new position of the
    Parameters cell (shifted down by the insertion).
    """
    nb = copy.deepcopy(notebook)

    download_cell = {
        "cell_type": "code",
        "source": [
            "# Download prod-state manifest from OneLake (ABFSS → local path)\n",
            "# Sets local_prod_state_path for use in Parameters cell f-string commands.\n",
            "if prod_state_path.startswith('abfss://'):\n",
            "    import os, urllib.request\n",
            "    # abfss://WORKSPACE_ID@onelake.dfs.fabric.microsoft.com/LAKEHOUSE_ID/...\n",
            "    # → https://onelake.dfs.fabric.microsoft.com/WORKSPACE_ID/LAKEHOUSE_ID/...\n",
            "    https_url = prod_state_path.replace('abfss://', 'https://onelake.dfs.fabric.microsoft.com/', 1)\n",
            "    https_url = https_url.replace('@onelake.dfs.fabric.microsoft.com', '', 1)\n",
            "    manifest_url = https_url.rstrip('/') + '/manifest.json'\n",
            "    local_dir = '/tmp/prod-state'\n",
            "    os.makedirs(local_dir, exist_ok=True)\n",
            "    token = notebookutils.credentials.getToken('storage')\n",
            "    req = urllib.request.Request(manifest_url, headers={'Authorization': f'Bearer {token}'})\n",
            "    with urllib.request.urlopen(req) as resp:\n",
            "        with open(f'{local_dir}/manifest.json', 'wb') as f:\n",
            "            f.write(resp.read())\n",
            "    local_prod_state_path = local_dir\n",
        ],
        "metadata": {"tags": ["ci-injected-download"]},
        "outputs": [],
        "execution_count": None,
    }

    nb["cells"].insert(params_idx, download_cell)
    return nb, params_idx + 1


def insert_shallow_clone_cell(notebook: dict, params_idx: int) -> dict:
    """Insert the shallow clone helper cell + interactive caller cell before the Build cell.

    Searches for the first cell after params_idx that contains run_dbt_job
    and inserts both cells before it. Falls back to inserting after params_idx
    with a warning if no Build cell is found.

    Returns a new notebook dict (does not mutate input).
    """
    nb = copy.deepcopy(notebook)

    build_idx = None
    for i in range(params_idx + 1, len(nb["cells"])):
        if "run_dbt_job" in "".join(nb["cells"][i].get("source", [])):
            build_idx = i
            break

    if build_idx is None:
        print("Warning: No Build cell found. Inserting shallow clone cells after download cell as fallback.", flush=True)
        insert_idx = params_idx + 1
    else:
        insert_idx = build_idx

    helper_cell = {
        "cell_type": "code",
        "source": [
            "# Only state:modified+ models are shallow cloned here.\n",
            "# Upstream/ancestor models are covered by read-side shortcuts (Phase 3).\n",
            "def run_shallow_clone(models, spark, prod_workspace_name, prod_lakehouse_name):\n",
            "    for m in models:\n",
            "        spark.sql(f\"DROP TABLE IF EXISTS {m['schema']}.{m['table']}\")\n",
            "        spark.sql(\n",
            "            f\"CREATE TABLE {m['schema']}.{m['table']} \"\n",
            "            f\"SHALLOW CLONE {prod_workspace_name}.{prod_lakehouse_name}.{m['schema']}.{m['table']}\"\n",
            "        )\n",
        ],
        "metadata": {"tags": ["ci-injected-shallow-clone"]},
        "outputs": [],
        "execution_count": None,
    }

    interactive_cell = {
        "cell_type": "code",
        "source": [
            "# Interactive: re-run to reset tables to prod state\n",
            "run_shallow_clone(shallow_clone_models, spark, prod_workspace_name, prod_lakehouse_name)\n",
        ],
        "metadata": {"tags": ["ci-injected-shallow-clone-interactive"]},
        "outputs": [],
        "execution_count": None,
    }

    # Insert helper then interactive, both before the build cell.
    nb["cells"].insert(insert_idx, helper_cell)
    nb["cells"].insert(insert_idx + 1, interactive_cell)
    return nb


def patch_lakehouse_metadata(notebook: dict, lakehouse_id: str, lakehouse_name: str, workspace_id: str) -> dict:
    """Patch metadata.dependencies.lakehouse to the ephemeral lakehouse before upload.

    Fabric restores the default-lakehouse context from # META lines in the serialized
    notebook. Without this patch, spark.sql() and notebookutils.fs calls resolve to
    the prod lakehouse, not the ephemeral one.
    """
    nb = copy.deepcopy(notebook)
    nb.setdefault("metadata", {}).setdefault("dependencies", {})["lakehouse"] = {
        "default_lakehouse": lakehouse_id,
        "default_lakehouse_name": lakehouse_name,
        "default_lakehouse_workspace_id": workspace_id,
        # Replace, not append — ephemeral notebook must have no prod lakehouse entries in scope.
        "known_lakehouses": [{"id": lakehouse_id, "name": lakehouse_name, "workspaceId": workspace_id}],
    }
    return nb


def find_existing_notebook(workspace_id: str, display_name: str) -> str | None:
    """Return item ID of an existing notebook with the given display name, or None."""
    resp = fabric_transport.request("GET", f"/workspaces/{workspace_id}/items")
    for item in resp.get("value", []):
        if item["type"] == "Notebook" and item["displayName"] == display_name:
            return item["id"]
    return None


def upload_notebook(workspace_id: str, display_name: str, notebook: dict) -> str | None:
    """Create or update a notebook in the Fabric workspace via Items API.

    Returns the notebook item ID (emits to GITHUB_OUTPUT as notebook_id).
    """
    nb_content = base64.b64encode(ipynb_to_fabric_py(notebook).encode()).decode()
    definition = {
        "parts": [{
            "path": "notebook-content.py",
            "payload": nb_content,
            "payloadType": "InlineBase64",
        }]
    }

    existing_id = find_existing_notebook(workspace_id, display_name)
    if existing_id:
        print(f"Updating existing notebook: {display_name} ({existing_id})", flush=True)
        fabric_transport.request_long_running(
            "POST",
            f"/workspaces/{workspace_id}/items/{existing_id}/updateDefinition",
            {"definition": definition},
        )
        notebook_id = existing_id
    else:
        print(f"Creating notebook: {display_name}", flush=True)
        body = fabric_transport.request_long_running(
            "POST",
            f"/workspaces/{workspace_id}/items",
            {"displayName": display_name, "type": "Notebook", "definition": definition},
        )
        notebook_id = body.get("id") or find_existing_notebook(workspace_id, display_name)

    print(f"Notebook '{display_name}' is now available in the workspace.", flush=True)
    if notebook_id:
        runner_io.set_output("notebook_id", notebook_id)
        print(f"Notebook ID: {notebook_id}", flush=True)

    return notebook_id


def _insert_ci_gate_cell(notebook: dict) -> dict:
    """Append CI orchestration cell at end of notebook."""
    nb = copy.deepcopy(notebook)
    ci_gate_cell = {
        "cell_type": "code",
        "source": [
            "if run_mode == \"ci\":\n",
            "    if gate == \"2\":\n",
            "        run_shallow_clone(shallow_clone_models, spark, prod_workspace_name, prod_lakehouse_name)\n",
            "        from dbt.adapters.fabricspark.notebook import run_dbt_job, DbtJobConfig, RepoConfig, ConnectionConfig\n",
            "        build_config = DbtJobConfig(\n",
            "            command=[\"dbt deps\", f\"dbt build --select state:modified+ --state {local_prod_state_path} --profiles-dir .github/profiles --target {ci_target}\"],\n",
            "            repo=RepoConfig(url=repo_url, branch=repo_branch, github_app_id=github_app_id, github_installation_id=github_installation_id, github_pem_secret=github_pem_secret, vault_url=vault_url),\n",
            "            connection=ConnectionConfig(lakehouse_name=lakehouse_name, lakehouse_id=lakehouse_id, workspace_id=workspace_id, workspace_name=workspace_name, schema_name=schema_name),\n",
            "        )\n",
            "        run_dbt_job(build_config)\n",
            "        import json, os\n",
            "        run_results_path = os.path.expanduser(\"~/.dbt/run_results.json\")\n",
            "        run_results = json.load(open(run_results_path)) if os.path.exists(run_results_path) else {\"results\": []}\n",
            "        models_out = [{\"name\": r.get(\"unique_id\",\"\").split(\".\")[-1], \"status\": r.get(\"status\",\"\"), \"duration_seconds\": r.get(\"execution_time\",0.0), \"error_message\": (r.get(\"message\") or \"\")[:500] or None} for r in run_results.get(\"results\",[])]\n",
            "        overall = \"pass\" if all(m[\"status\"] in (\"success\",\"pass\") for m in models_out) else \"fail\"\n",
            "        gate_result = {\"gate\": \"2\", \"head_sha\": head_sha, \"overall_status\": overall, \"models\": models_out}\n",
            "        notebookutils.fs.put(f\"Files/ci-artifacts/gate-2/{head_sha}/gate-2.json\", json.dumps(gate_result, indent=2), overwrite=True)\n",
            "    elif gate == \"4\":\n",
            "        from dbt.adapters.fabricspark.notebook import run_dbt_job, DbtJobConfig, RepoConfig, ConnectionConfig\n",
            "        test_config = DbtJobConfig(\n",
            "            command=[\"dbt deps\", f\"dbt test --select state:modified+ --store-failures --profiles-dir .github/profiles --target {ci_target}\"],\n",
            "            repo=RepoConfig(url=repo_url, branch=repo_branch, github_app_id=github_app_id, github_installation_id=github_installation_id, github_pem_secret=github_pem_secret, vault_url=vault_url),\n",
            "            connection=ConnectionConfig(lakehouse_name=lakehouse_name, lakehouse_id=lakehouse_id, workspace_id=workspace_id, workspace_name=workspace_name, schema_name=schema_name),\n",
            "        )\n",
            "        run_dbt_job(test_config)\n",
            "        import json, os\n",
            "        run_results_path = os.path.expanduser(\"~/.dbt/run_results.json\")\n",
            "        run_results = json.load(open(run_results_path)) if os.path.exists(run_results_path) else {\"results\": []}\n",
            "        tests_out = [\n",
            "            {\n",
            "                \"name\": r.get(\"unique_id\", \"\").split(\".\")[-1],\n",
            "                \"model\": (r.get(\"unique_id\", \"\").split(\".\") + [\"\"])[2] if len(r.get(\"unique_id\", \"\").split(\".\")) > 2 else \"\",\n",
            "                \"status\": r.get(\"status\", \"\"),\n",
            "                \"duration_seconds\": r.get(\"execution_time\", 0.0),\n",
            "                \"failures_count\": r.get(\"failures\", 0) or 0,\n",
            "                \"store_failures_table\": f\"dbt_test__audit.{r.get('unique_id', '').split('.')[-1]}\" if r.get(\"status\") in (\"fail\", \"error\") else None,\n",
            "                \"message\": (r.get(\"message\") or \"\")[:500] or None,\n",
            "            }\n",
            "            for r in run_results.get(\"results\", [])\n",
            "        ]\n",
            "        overall = \"fail\" if any(t[\"status\"] in (\"fail\", \"error\") for t in tests_out) else \"pass\"\n",
            "        gate_result = {\"gate\": \"4\", \"head_sha\": head_sha, \"overall_status\": overall, \"tests\": tests_out}\n",
            "        notebookutils.fs.put(f\"Files/ci-artifacts/gate-4/{head_sha}/gate-4.json\", json.dumps(gate_result, indent=2), overwrite=True)\n",
            "    elif gate == \"3\":\n",
            "        from dbt.adapters.fabricspark.notebook import run_dbt_job, DbtJobConfig, RepoConfig, ConnectionConfig\n",
            "        unit_config = DbtJobConfig(\n",
            "            command=[\"dbt deps\", f\"dbt test --select state:modified+,test_type:unit --state {local_prod_state_path} --profiles-dir .github/profiles --target {ci_target}\"],\n",
            "            repo=RepoConfig(url=repo_url, branch=repo_branch, github_app_id=github_app_id, github_installation_id=github_installation_id, github_pem_secret=github_pem_secret, vault_url=vault_url),\n",
            "            connection=ConnectionConfig(lakehouse_name=lakehouse_name, lakehouse_id=lakehouse_id, workspace_id=workspace_id, workspace_name=workspace_name, schema_name=schema_name),\n",
            "        )\n",
            "        run_dbt_job(unit_config)\n",
            "        import json, os\n",
            "        run_results_path = os.path.expanduser(\"~/.dbt/run_results.json\")\n",
            "        run_results = json.load(open(run_results_path)) if os.path.exists(run_results_path) else {\"results\": []}\n",
            "        counts = {\"pass\": 0, \"fail\": 0, \"error\": 0, \"skip\": 0}\n",
            "        failures = []\n",
            "        for r in run_results.get(\"results\", []):\n",
            "            s = r.get(\"status\", \"\")\n",
            "            if s in (\"pass\", \"success\"): counts[\"pass\"] += 1\n",
            "            elif s == \"fail\": counts[\"fail\"] += 1; failures.append({\"name\": r.get(\"unique_id\", \"\"), \"status\": \"fail\", \"message\": (r.get(\"message\") or \"\")[:500]})\n",
            "            elif s == \"skip\": counts[\"skip\"] += 1\n",
            "            else: counts[\"error\"] += 1; failures.append({\"name\": r.get(\"unique_id\", \"\"), \"status\": \"error\", \"message\": (r.get(\"message\") or \"\")[:500]})\n",
            "        overall = \"fail\" if (counts[\"fail\"] or counts[\"error\"]) else \"pass\"\n",
            "        total = sum(counts.values())\n",
            "        truncated = len(failures) > 10\n",
            "        gate_result = {\"gate\": \"3\", \"head_sha\": head_sha, \"overall_status\": overall, \"total\": total, \"counts\": counts, \"failures\": failures[:10], \"truncated\": truncated}\n",
            "        notebookutils.fs.put(f\"Files/ci-artifacts/gate-3/{head_sha}/gate-3.json\", json.dumps(gate_result, indent=2), overwrite=True)\n",
        ],
        "metadata": {"tags": ["ci-injected-gate-cell"]},
        "outputs": [],
        "execution_count": None,
    }
    nb["cells"].append(ci_gate_cell)
    return nb


def main():
    workspace_id = os.environ["EPHEMERAL_WORKSPACE_ID"]
    lakehouse_id = os.environ["EPHEMERAL_LAKEHOUSE_ID"]
    lakehouse_name = os.environ.get("EPHEMERAL_LAKEHOUSE_NAME", "vdephelh")
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

    # Step 2: Insert download cell immediately after Parameters cell
    notebook, params_idx = insert_download_cell(notebook, params_idx)
    print("Download cell inserted.", flush=True)

    # Step 3: Insert shallow clone helper + interactive caller cells before Build cell
    notebook = insert_shallow_clone_cell(notebook, params_idx)
    print("Shallow clone cells inserted.", flush=True)

    # Step 4: Append CI orchestration gate cell
    notebook = _insert_ci_gate_cell(notebook)
    print("CI gate cell appended.", flush=True)

    # Step 5: Patch Fabric default-lakehouse metadata to ephemeral workspace
    notebook = patch_lakehouse_metadata(notebook, lakehouse_id, lakehouse_name, workspace_id)
    print("Lakehouse metadata patched to ephemeral workspace.", flush=True)

    # Step 6: Upload to Fabric workspace
    display_name = os.path.splitext(os.path.basename(notebook_path))[0]
    upload_notebook(workspace_id, display_name, notebook)


if __name__ == "__main__":
    main()
