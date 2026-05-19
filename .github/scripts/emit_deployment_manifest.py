"""
Deployment manifest emitter.

Emits a per-head-SHA deployment manifest consumed by Gate 5 (check_gate_5) and
eventually by domain-deploy. The manifest lists every model/snapshot in the
state:modified+ closure with its materialization, whether it exists in production,
and its configured unique_key for value-delta joining.

Pure function: build_deployment_manifest — receives already-fetched values, no I/O.
Shell: main() — reads env vars, invokes dbt ls, loads manifests, writes output file.

ci.yml then uploads the output file to OneLake and as a GHA artifact.
"""
from __future__ import annotations

import json
import os

try:
    from scripts.dbt_ls import run_dbt_ls
except ImportError:
    from dbt_ls import run_dbt_ls


def build_deployment_manifest(
    *,
    head_sha: str,
    closure_uids: list[str],
    current_nodes: dict,
    prod_node_ids: set[str],
) -> dict:
    """Pure: build the deployment manifest dict from already-fetched values.

    prod_node_ids: pass empty set for greenfield (all pre_existing_in_prod: false).
    current_nodes: nodes dict from target/manifest.json (uid -> node).
    closure_uids: unique_ids from dbt ls --select state:modified+.
    """
    artifacts = []
    for uid in closure_uids:
        node = current_nodes.get(uid) or {}
        config = node.get("config") or {}
        artifacts.append(
            {
                "unique_id": uid,
                "name": node.get("name", ""),
                "materialized": config.get("materialized", ""),
                "pre_existing_in_prod": uid in prod_node_ids,
                "unique_key": config.get("unique_key"),
            }
        )
    return {"head_sha": head_sha, "artifacts": artifacts}


# ─── I/O helpers ──────────────────────────────────────────────────────────────


def _read_json(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _is_greenfield() -> bool:
    """Greenfield when prod-state/source.json is missing or mode == 'greenfield'."""
    src = _read_json("prod-state/source.json")
    if src is None:
        return True
    return src.get("mode") == "greenfield"


# ─── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    head_sha = os.environ["HEAD_SHA"].strip()

    greenfield = _is_greenfield()
    closure_uids = run_dbt_ls() if not greenfield else []

    current_nodes = (_read_json("target/manifest.json") or {}).get("nodes", {})

    prod_node_ids: set[str] = set()
    if not greenfield:
        prod_raw = _read_json("prod-state/manifest.json") or {}
        prod_node_ids = set(prod_raw.get("nodes", {}).keys())

    manifest = build_deployment_manifest(
        head_sha=head_sha,
        closure_uids=closure_uids,
        current_nodes=current_nodes,
        prod_node_ids=prod_node_ids,
    )

    os.makedirs("reports", exist_ok=True)
    local_path = f"reports/deployment-manifest-{head_sha}.json"
    with open(local_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {local_path}", flush=True)


if __name__ == "__main__":
    main()
