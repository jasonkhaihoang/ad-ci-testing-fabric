"""Shared pure helpers for Fabric GHA gate runner scripts (AC-11).

Extracted here to eliminate duplication across gate2/3/4/5_fabric_runner.py.
Each function is stateless and takes only pre-fetched scalar inputs.
"""
from __future__ import annotations

import json
import pathlib
import shutil


def _load_artifacts(deployment_manifest_path: str) -> list[dict]:
    """Load the artifact list from the deployment manifest, or [] on read error."""
    try:
        with open(deployment_manifest_path) as f:
            dm = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return dm.get("artifacts") or []


def _leaf_name(artifact: dict) -> str:
    name = artifact.get("name", "")
    return name.split(".")[-1] or name


def mat_map_from_manifest(deployment_manifest_path: str) -> dict[str, str]:
    """Return {model_name: materialized} from the deployment manifest."""
    return {
        _leaf_name(a): a.get("materialized", "")
        for a in _load_artifacts(deployment_manifest_path)
        if a.get("name")
    }


def select_names(deployment_manifest_path: str) -> list[str]:
    """Extract leaf model names from the deployment manifest, excluding ephemeral."""
    return [
        _leaf_name(a)
        for a in _load_artifacts(deployment_manifest_path)
        if a.get("name") and a.get("materialized") != "ephemeral"
    ]


def select_clone_names(deployment_manifest_path: str) -> list[str]:
    """Clone-eligible leaf names: non-ephemeral and non-view.

    Views cannot be shallow-cloned — dbt clone falls back to running the view
    materialization, which the fabricspark macro renders as CREATE VIEW against a
    2-part prod ref that fails in the ephemeral workspace (VD-2336). Views are
    built by the subsequent `dbt run --defer` instead, so they are dropped here.
    """
    return [
        _leaf_name(a)
        for a in _load_artifacts(deployment_manifest_path)
        if a.get("name") and a.get("materialized") not in ("ephemeral", "view")
    ]


def setup_defer(prod_state_dir: str) -> list[str]:
    """Copy manifest_prod.json to target/prod-state-defer/manifest.json.

    Returns --defer --state args if the source file exists, otherwise [].
    Used by Gates 2 and 4 for cross-workspace ref resolution.
    """
    src = pathlib.Path(prod_state_dir) / "manifest_prod.json"
    if not src.exists():
        return []
    dest_dir = pathlib.Path("target/prod-state-defer")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest_dir / "manifest.json")
    return ["--defer", "--state", str(dest_dir)]


def write_gate_result(path: str | None, result: dict) -> None:
    """Write gate result JSON to the output file, creating parent dirs as needed."""
    if not path:
        return
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
