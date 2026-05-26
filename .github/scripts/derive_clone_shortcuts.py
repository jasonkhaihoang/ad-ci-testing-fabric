"""
Derive write-side clone model list for shallow clone prep.

VD-1766. Pure derivation — no Fabric API calls.
The output JSON is consumed by the notebook's shallow clone cell.

Logic:
1. Detect greenfield (prod-state/source.json.mode == "greenfield", or sidecar
   absent) → emit empty clone_models.json, exit 0.
2. Run `dbt ls --select state:modified+ --state ./prod-state
   --profiles-dir .github/profiles --target dbt_fabric_compile` to get the
   write-side node set.
3. For each model.* node, resolve {schema, table} from the prod manifest
   (prod-state/manifest.json).
4. Emit clone_models.json to --models-output path.

# Only state:modified+ models — upstream ancestors are covered by read-side
# shortcuts (derive_shortcuts.py)
"""

import argparse
import json
import os
import subprocess
import sys
from typing import List, Optional

import runner_io


# ─── Pure core ────────────────────────────────────────────────────────────────

def derive_clone_models(
    modified_unique_ids: List[str],
    prod_manifest: dict,
) -> List[dict]:
    """Pure derivation. Returns a deduplicated, sorted list of {schema, table} dicts.

    Only model.* nodes are included — sources and snapshots are read-side
    (covered by derive_shortcuts.py).
    """
    nodes = prod_manifest.get("nodes", {})
    seen = set()
    results = []

    for uid in modified_unique_ids:
        if not uid.startswith("model."):
            continue
        node = nodes.get(uid)
        if node is None:
            continue
        schema = node.get("schema", "")
        table = node.get("alias") or node.get("name", "")
        key = (schema, table)
        if key in seen:
            continue
        seen.add(key)
        results.append({"schema": schema, "table": table})

    return sorted(results, key=lambda d: (d["schema"], d["table"]))


# ─── Mutable shell ────────────────────────────────────────────────────────────

def _read_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _is_greenfield() -> bool:
    """Greenfield = source.json missing or mode == 'greenfield'."""
    src = _read_json("prod-state/source.json")
    if src is None:
        return True
    return src.get("mode") == "greenfield"


def _run_dbt_ls() -> List[str]:
    """Invoke dbt ls and return the list of unique_ids in the state:modified+ set."""
    result = subprocess.run(
        [
            "dbt", "ls",
            "--select", "state:modified+",
            "--state", "./prod-state",
            "--profiles-dir", ".github/profiles",
            "--target", "dbt_fabric_compile",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        runner_io.error(
            f"dbt ls failed (exit {result.returncode}). "
            f"stdout/stderr (first 500 chars): "
            f"{(result.stdout + result.stderr)[:500]}"
        )
        sys.exit(1)
    unique_ids: List[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            unique_ids.append(line)
    return unique_ids


# ─── Entry point ──────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive write-side clone model list (state:modified+) for shallow clone prep."
    )
    parser.add_argument("--models-output", required=True, help="Write clone_models.json to this path.")
    args = parser.parse_args(argv)

    if _is_greenfield():
        with open(args.models_output, "w") as f:
            json.dump([], f, indent=2)
        print("Greenfield detected — emitting empty clone_models.json.")
        return 0

    modified_ids = _run_dbt_ls()

    prod_manifest = _read_json("prod-state/manifest.json") or {"nodes": {}}

    clone_models = derive_clone_models(modified_ids, prod_manifest)

    with open(args.models_output, "w") as f:
        json.dump(clone_models, f, indent=2)

    print(f"Derived {len(clone_models)} clone model(s) → {args.models_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
